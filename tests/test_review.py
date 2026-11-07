import csv

import pytest

from screening.__main__ import main
from screening.db import connect
from screening.intake import ingest
from screening.references import run_references
from screening.review import dismiss, flags, notice, restore
from tests.test_references import TEX, bundle, offline


def open_codes(con, pid):
    return {s["code"] for s in flags(con, pid)[0]}


def setup(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "screening.db")
    _, [pid] = ingest(con, [bundle(tmp_path)], data)
    run_references(con, pid, data, offline())
    src = data / con.execute("SELECT source_dir FROM paper").fetchone()[0]
    return con, data, pid, src


def test_dismissal_survives_rerun_until_evidence_changes(tmp_path):
    con, data, pid, src = setup(tmp_path)
    assert "reference_undefined" in open_codes(con, pid)
    with pytest.raises(ValueError):
        dismiss(con, pid, "reference_undefined", " ", "chair")
    with pytest.raises(ValueError):
        dismiss(con, pid, "no_such_flag", "x", "chair")
    dismiss(con, pid, "reference_undefined", "key is defined in a style file", "chair")
    assert "reference_undefined" not in open_codes(con, pid)
    run_references(con, pid, data, offline())  # same evidence: stays dismissed
    assert "reference_undefined" not in open_codes(con, pid)
    (src / "main.tex").write_text(TEX.replace("nokey", "otherkey"))
    run_references(con, pid, data, offline())  # new evidence: comes back
    assert "reference_undefined" in open_codes(con, pid)
    dismiss(con, pid, "reference_undefined", "fine", "chair")
    restore(con, pid, "reference_undefined", "second look", "chair2")
    assert "reference_undefined" in open_codes(con, pid)
    assert con.execute("SELECT count(*) FROM audit_log WHERE action IN ('dismiss','restore')").fetchone()[0] == 3


def test_notice_lists_author_fixes_with_evidence_only(tmp_path):
    con, _, pid, _ = setup(tmp_path)
    con.execute("INSERT INTO check_run(paper_id, check_name, check_version, created_at) VALUES(?,?,?,?)",
                (pid, "conversion", "1", "t"))
    con.execute("INSERT INTO signal(paper_id, check_name, check_version, code, severity, message, evidence_json,"
                " created_at, run_id) VALUES(?,?,?,?,?,?,?,?,last_insert_rowid())",
                (pid, "conversion", "1", "converter_unavailable", "warn", "LaTeX missing", "{}", "t"))
    text = notice(con, con.execute("SELECT * FROM paper").fetchone())
    assert "[REQUIRED] Add the missing bibliography entries" in text and "nokey" in text
    assert "LaTeX missing" not in text  # organizer-side problems never reach authors
    dismiss(con, pid, "reference_undefined", "ok", "chair")
    assert notice(con, con.execute("SELECT * FROM paper").fetchone()) is None


def test_cli_dismiss_notices_export_and_check_changes(tmp_path, capsys):
    bundle(tmp_path / "b")
    data, d = tmp_path / "data", ["--data", str(tmp_path / "data")]
    main([*d, "ingest", str(tmp_path / "b" / "p"), "--offline", "--jobs", "1"])
    main([*d, "dismiss", "1", "missing_keywords", "--reason", "has Index Terms", "--by", "ann"])
    main([*d, "notices"])
    main([*d, "export", "--out", str(tmp_path / "s.csv")])
    capsys.readouterr()
    [note] = (data / "notices").iterdir()
    text = note.read_text(encoding="utf-8")
    assert "nokey" in text and "fix: " not in text and "keywords" not in text
    [row] = list(csv.DictReader((tmp_path / "s.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert (row["fails"], row["dismissed"], row["authors_can_fix"]) == ("4", "1", "yes")
    src = data / connect(data / "screening.db").execute("SELECT source_dir FROM paper").fetchone()[0]
    (src / "main.tex").write_text(TEX.replace(r" and \cite{nokey}", ""))
    main([*d, "check", "--offline", "--jobs", "1"])
    out = capsys.readouterr().out
    assert "paper 1: -reference_undefined" in out and "(1 dismissed)" in out
    with pytest.raises(SystemExit):
        main([*d, "dismiss", "1", "nope", "--reason", "x"])


def test_export_escapes_formulas_and_notices_keep_edits(tmp_path, capsys):
    bundle(tmp_path / "b")
    d = ["--data", str(tmp_path / "data")]
    main([*d, "ingest", str(tmp_path / "b" / "p"), "--offline", "--jobs", "1"])
    con = connect(tmp_path / "data" / "screening.db")
    con.execute("UPDATE paper SET title='=HYPERLINK(\"http://x\")', external_id='@SUM(1)'")
    con.commit()
    main([*d, "export", "--out", str(tmp_path / "s.csv")])
    [row] = list(csv.DictReader((tmp_path / "s.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert row["title"].startswith("'=") and row["external_id"] == "'@SUM(1)"
    main([*d, "notices"])
    [note] = (tmp_path / "data" / "notices").iterdir()
    note.write_text("edited by chair")
    main([*d, "notices"])
    assert note.read_text() == "edited by chair" and "1 existing draft(s) kept" in capsys.readouterr().out
    main([*d, "notices", "--force"])
    assert note.read_text(encoding="utf-8") != "edited by chair"
