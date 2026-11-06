import json
import shutil
import sqlite3
import zipfile

import docx
import pytest

from screening import intake
from screening.convert import convert_paper, extract_structure, flatten_latex
from screening.db import add_signal, connect, start_run
from screening.intake import UnsafeArchive, discover, ingest, safe_extract
from screening.sniff import OLE2, sniff

HAS_LATEX = bool(shutil.which("latexmk"))
HAS_OFFICE = bool(shutil.which("soffice") or shutil.which("libreoffice"))

MAIN_TEX = r"""\documentclass{article}
\title{Graph Methods}
\begin{document}
\maketitle
\begin{abstract}We study graphs.\end{abstract}
\section{Introduction}
Intro text.
\input{body}
% \input{/etc/passwd}
\end{document}
"""


def make_docx(path, title="A Word Paper"):
    d = docx.Document()
    d.add_heading(title, level=0)
    d.add_heading("Introduction", level=1)
    d.add_paragraph("Word body text.")
    d.add_heading("Conclusion", level=1)
    d.add_paragraph("Done.")
    d.save(path)
    return path


def make_bundle(folder):
    folder.mkdir(parents=True)
    (folder / "main.tex").write_text(MAIN_TEX)
    (folder / "body.tex").write_text(r"\section{Method}Method text.")
    (folder / "refs.bib").write_text("@article{a, title={T}}")
    return folder


def zip_dir(src, dest):
    with zipfile.ZipFile(dest, "w") as z:
        for p in src.rglob("*"):
            z.write(p, p.relative_to(src.parent))
    return dest


@pytest.fixture
def con(tmp_path):
    return connect(tmp_path / "data" / "t.db")


def test_sniff_by_content(tmp_path):
    d = make_docx(tmp_path / "a.docx")
    renamed = tmp_path / "b.doc"
    shutil.copy(d, renamed)
    (tmp_path / "c.doc").write_bytes(OLE2 + b"\0" * 100)
    (tmp_path / "d.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "e.tex").write_text(MAIN_TEX)
    (tmp_path / "f.docx").write_bytes(b"PK\x03\x04garbage")
    (tmp_path / "g.docx").write_bytes(b"not a document")
    assert [sniff(tmp_path / n) for n in ("a.docx", "b.doc", "c.doc", "d.pdf", "e.tex", "f.docx", "g.docx")] == [
        "docx", "docx", "doc", "pdf", "tex", "corrupt", "corrupt"]


def test_safe_extract_rejects_traversal(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../escape.txt", "x")
    with pytest.raises(UnsafeArchive):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_counts_real_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "MAX_TOTAL_BYTES", 1000)
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("big.txt", "0" * 100_000)
    with pytest.raises(UnsafeArchive):
        safe_extract(z, tmp_path / "out")


def test_discover_mixed_batch(tmp_path):
    batch = tmp_path / "batch"
    make_bundle(batch / "paperA")
    zip_dir(make_bundle(tmp_path / "paperB"), batch / "paperB.zip")
    make_docx(batch / "paperC.docx")
    (batch / "paperD.pdf").write_bytes(b"%PDF-1.4\n")
    (batch / "__MACOSX").mkdir()
    batch_zip = zip_dir(batch, tmp_path / "all.zip")
    work = tmp_path / "work"
    work.mkdir()
    units = {u.label: u for u in discover(batch_zip, work)}
    assert set(units) == {"paperA", "paperB", "paperC.docx", "paperD.pdf"}
    assert units["paperA"].kind == "latex" and units["paperA"].main.name == "main.tex"
    assert units["paperB"].kind == "latex"
    assert units["paperC.docx"].kind == "word"
    assert units["paperD.pdf"].kind == "unknown"
    assert units["paperD.pdf"].notes[0][0] == "source_missing"


def test_multiple_mains_prefers_main_tex(tmp_path):
    b = make_bundle(tmp_path / "p")
    (b / "response.tex").write_text(r"\documentclass{letter}")
    [u] = discover(b, tmp_path)
    assert u.main.name == "main.tex" and u.notes[0][0] == "multiple_main_tex"


def test_ingest_never_drops_and_logs(tmp_path, con):
    data = tmp_path / "data"
    make_docx(tmp_path / "ok.docx")
    (tmp_path / "broken.docx").write_bytes(b"PK\x03\x04garbage")
    batch_id, ids = ingest(con, [tmp_path / "ok.docx", tmp_path / "broken.docx"], data)
    rows = {r["external_id"]: r for r in con.execute("SELECT * FROM paper")}
    assert rows["ok.docx"]["status"] == "ingested"
    assert rows["broken.docx"]["status"] == "needs_manual"
    assert (data / rows["ok.docx"]["source_dir"] / "ok.docx").exists()
    assert len(list((data / "batches" / str(batch_id) / "inputs").iterdir())) == 2
    codes = [r["code"] for r in con.execute("SELECT code FROM signal WHERE paper_id=?", (rows["broken.docx"]["id"],))]
    assert codes == ["corrupt_file"]
    assert con.execute("SELECT count(*) FROM audit_log WHERE action='ingest'").fetchone()[0] == 1


def test_signals_are_append_only(con):
    con.execute("INSERT INTO batch(name, created_at) VALUES('b','t')")
    con.execute("INSERT INTO paper(batch_id, format, status, created_at) VALUES(1,'word','ingested','t')")
    add_signal(con, start_run(con, 1, "intake"), "x", "warn", "m")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE signal SET severity='info'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM audit_log")


def test_flatten_stays_inside_bundle(tmp_path):
    b = make_bundle(tmp_path / "p")
    (tmp_path / "secret.tex").write_text("SECRET")
    (b / "main.tex").write_text(MAIN_TEX.replace(r"\input{body}", r"\input{body}\input{../secret}"))
    flat = flatten_latex(b / "main.tex", b)
    assert "Method text." in flat and "SECRET" not in flat and "passwd" not in flat


def test_latex_structure(tmp_path):
    b = make_bundle(tmp_path / "p")
    s, err = extract_structure(b / "main.tex", "latex", b)
    assert err is None
    assert s["title"] == "Graph Methods" and s["abstract"] == "We study graphs."
    assert [x["heading"] for x in s["sections"]] == ["Introduction", "Method"]


def test_convert_docx(tmp_path, con):
    data = tmp_path / "data"
    _, [pid] = ingest(con, [make_docx(tmp_path / "w.docx")], data)
    status = convert_paper(con, pid, data)
    row = con.execute("SELECT * FROM paper WHERE id=?", (pid,)).fetchone()
    s = json.loads(row["sections_json"])
    assert row["title"] == "A Word Paper"
    assert [x["heading"] for x in s["sections"]] == ["Introduction", "Conclusion"]
    assert "Word body text." in (data / row["text_path"]).read_text()
    if HAS_OFFICE:
        assert status == "converted" and row["page_count"] >= 1
    else:
        assert status == "needs_manual"
        assert con.execute("SELECT code FROM signal WHERE paper_id=?", (pid,)).fetchone()[0] == "converter_unavailable"


@pytest.mark.skipif(not HAS_LATEX, reason="latexmk not installed")
def test_latex_compile_failure_is_surfaced(tmp_path, con):
    data = tmp_path / "data"
    b = make_bundle(tmp_path / "p")
    (b / "main.tex").write_text(MAIN_TEX.replace("Intro text.", r"\includegraphics{missing.png}"))
    _, [pid] = ingest(con, [b], data)
    assert convert_paper(con, pid, data) == "needs_manual"
    codes = [r[0] for r in con.execute("SELECT code FROM signal WHERE paper_id=?", (pid,))]
    assert "latex_compile_failed" in codes


@pytest.mark.skipif(not HAS_LATEX, reason="latexmk not installed")
def test_latex_compiles(tmp_path, con):
    data = tmp_path / "data"
    _, [pid] = ingest(con, [make_bundle(tmp_path / "p")], data)
    assert convert_paper(con, pid, data) == "converted"
    assert con.execute("SELECT page_count FROM paper").fetchone()[0] >= 1


# --- regressions from the M1 review ---

def test_bad_zip_does_not_sink_batch(tmp_path, con):
    z = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("main.tex", MAIN_TEX * 200)
    raw = bytearray(z.read_bytes())
    raw[60:200] = b"\xff" * 140  # wreck the deflate stream, keep the directory intact
    z.write_bytes(bytes(raw))
    _, ids = ingest(con, [z, make_docx(tmp_path / "ok.docx")], tmp_path / "data")
    rows = {r["external_id"]: (r["status"], r["format"]) for r in con.execute("SELECT * FROM paper")}
    assert len(ids) == 2
    assert rows["corrupt.zip"] == ("needs_manual", "unknown")
    assert rows["ok.docx"] == ("ingested", "word")


def test_empty_input_is_recorded(tmp_path, con):
    (tmp_path / "empty").mkdir()
    _, [pid] = ingest(con, [tmp_path / "empty"], tmp_path / "data")
    assert con.execute("SELECT code FROM signal WHERE paper_id=?", (pid,)).fetchone()[0] == "empty_input"


def test_word_file_beside_tex_is_its_own_paper(tmp_path):
    b = make_bundle(tmp_path / "p")
    make_docx(b / "other.docx")
    kinds = sorted(u.kind for u in discover(b, tmp_path))
    assert kinds == ["latex", "word"]


def test_include_bomb_is_bounded(tmp_path):
    b = tmp_path / "p"
    b.mkdir()
    (b / "main.tex").write_text(r"\documentclass{article}\begin{document}\input{a}\end{document}")
    (b / "a.tex").write_text(r"\input{c}" * 30)
    (b / "c.tex").write_text(r"\input{a}" * 30)
    s, err = extract_structure(b / "main.tex", "latex", b)
    assert s is None and "exceeds" in err


def test_timeout_kills_grandchildren(monkeypatch):
    import sys
    import time

    from screening import convert
    monkeypatch.setattr(convert, "TIMEOUT", 2)
    child = "import subprocess,sys; subprocess.run([sys.executable,'-c','import time; time.sleep(60)'])"
    t = time.monotonic()
    ok, out = convert._run([sys.executable, "-c", child])
    assert not ok and "timed out" in out and time.monotonic() - t < 30


@pytest.mark.skipif(not HAS_LATEX, reason="latexmk not installed")
def test_shipped_latexmkrc_is_not_run(tmp_path, con):
    data = tmp_path / "data"
    b = make_bundle(tmp_path / "p")
    marker = tmp_path / "pwned"
    (b / "latexmkrc").write_text(f'open(my $f, ">", "{marker.as_posix()}"); close $f;')
    _, [pid] = ingest(con, [b], data)
    convert_paper(con, pid, data)
    assert not marker.exists()
