import json
import os
import shutil
from types import SimpleNamespace as NS

import docx
import pytest

from screening.compliance import load_rules
from screening.convert import convert_paper
from screening.db import connect, current_signals
from screening.intake import ingest
from screening.references import findings_from_report, numeric_citations, run_references

BIB = """@article{used, author={Ann Author}, title={A Study of Things}, journal={J. Things}, year={2020}}
@article{spare, author={Bob Other}, title={Unused Work}, journal={J. Things}, year={2019}}
"""
TEX = r"""\documentclass{article}
\begin{document}
See \cite{used} and \cite{nokey}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""


def offline():
    rules = load_rules()
    rules["references"]["online"] = False
    return rules


def codes(findings, severity=None):
    return {f[0] for f in findings if severity is None or f[1] == severity}


def bundle(tmp_path, tex=TEX, extra=None):
    b = tmp_path / "p"
    b.mkdir(parents=True)
    (b / "main.tex").write_text(tex)
    (b / "refs.bib").write_text(BIB)
    for name, text in (extra or {}).items():
        (b / name).write_text(text)
    return b


def ref(key, verdict, doi=None):
    matched = NS(source="crossref", id=doi, title="T", year=2020, venue="V", doi=doi) if doi else None
    return NS(key=key, verdict=verdict, matched=matched)


def finding(rule, severity, key, file="main.tex"):
    return NS(rule=rule, severity=severity, key=key, message=f"{rule} {key}", file=file, line=3)


def test_report_mapping_groups_and_severities():
    report = NS(complete=False, verification="online", result=NS(entries=3, cited_keys=3),
                references=[ref("a", "verified", "10.1/x"), ref("b", "not_found"), ref("c", "cannot_determine")],
                findings=[finding("REF004", "error", "a"), finding("REF003", "error", "b"),
                          finding("REF090", "info", "c"), finding("CIT003", "info", "d", file="refs.bib"),
                          finding("REF013", "warning", "a"), finding("CIT006", "warning", "a")])
    out = {f[0]: f for f in findings_from_report(report)}
    assert out["reference_retracted"][1] == "fail"
    assert out["reference_retracted"][3]["items"][0]["matched"]["url"] == "https://doi.org/10.1/x"
    assert out["reference_not_found"][1] == "fail"
    assert out["reference_unverified"][1] == "warn"  # preflight's info is too quiet for a human gate
    assert out["reference_uncited"][1] == "info"  # unused .bib entries never print
    assert out["reference_mismatch"][1] == "warn"
    assert out["reference_hygiene"][1] == "warn"
    assert out["reference_check_incomplete"][1] == "warn"
    assert out["reference_summary"][3]["verdicts"] == {"verified": 1, "not_found": 1, "cannot_determine": 1}


ESCAPES = [
    r"\bibliography{refs,../outside/secret}",
    r"\addglobalbib{../outside/secret.bib}",
    r"\addsectionbib{../outside/secret.bib}",
    r"\input{../outside/leak}",
    r'\input{"../outside/leak"}',
    r"\input ../outside/leak",
    r"\import{../outside/}{leak}",
    r"\subimport{../outside/}{leak}",
    r"\input{ABS}",
]


@pytest.mark.parametrize("cmd", ESCAPES)
def test_preflight_never_reads_outside_the_submission(tmp_path, cmd):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bib").write_text("@misc{SECRETKEY, title={Organizer secret}, year={2020}}\n")
    (outside / "leak.tex").write_text(r"\cite{LEAKEDCITE}")
    cmd = cmd.replace("ABS", (outside / "leak").as_posix())
    _, [pid] = ingest(con, [bundle(tmp_path, tex=TEX.replace(r"\bibliography{refs}", cmd + "\n" r"\bibliography{refs}"))],
                      data)
    # ingest copies the bundle into data/, so point the relative escapes at a copy of outside/ next to it
    src = data / con.execute("SELECT source_dir FROM paper").fetchone()[0]
    shutil.copytree(outside, src.parent / "outside")
    f = run_references(con, pid, data, offline())
    dump = json.dumps(f)
    assert "SECRETKEY" not in dump and "LEAKEDCITE" not in dump
    assert "reference_paths_unsafe" in codes(f, "fail")


def test_includes_inside_the_submission_still_work(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    tex = TEX.replace(r"See \cite{used} and \cite{nokey}.", r"\import{sections/}{intro}\input{sections/more}")
    b = bundle(tmp_path, tex=tex)
    (b / "sections").mkdir()
    (b / "sections" / "intro.tex").write_text(r"See \cite{used}.")
    (b / "sections" / "more.tex").write_text(r"And \cite{nokey}.")
    _, [pid] = ingest(con, [b], data)
    f = {x[0]: x for x in run_references(con, pid, data, offline())}
    assert "reference_paths_unsafe" not in f
    assert [i["key"] for i in f["reference_undefined"][3]["items"]] == ["nokey"]
    assert [i["key"] for i in f["reference_uncited"][3]["items"]] == ["spare"]


def test_forged_aux_cannot_hide_uncited_entries(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    b = bundle(tmp_path, extra={"main.aux": "\\citation{used}\n\\citation{spare}\n\\citation{nokey}\n"})
    _, [pid] = ingest(con, [b], data)
    f = {x[0]: x for x in run_references(con, pid, data, offline())}
    assert [i["key"] for i in f["reference_uncited"][3]["items"]] == ["spare"]
    assert "main.tex" in {i["file"] for i in f["reference_undefined"][3]["items"]}  # relative, not the temp path


def test_oversized_inputs_are_not_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr("screening.references.MAX_COPY_BYTES", 100)
    data = tmp_path / "data"
    con = connect(data / "t.db")
    _, [pid] = ingest(con, [bundle(tmp_path)], data)
    assert codes(run_references(con, pid, data, offline())) == {"reference_input_too_large"}


def test_offline_latex_run_finds_citation_problems(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    # a submitter's own preflight settings must not silence anything
    b = bundle(tmp_path, extra={"paper-preflight.toml": 'ignore-rules = ["CIT001", "CIT003"]\n'})
    _, [pid] = ingest(con, [b], data)
    f = {x[0]: x for x in run_references(con, pid, data, offline())}
    assert f["reference_undefined"][1] == "fail"
    assert [i["key"] for i in f["reference_undefined"][3]["items"]] == ["nokey"]
    assert [i["key"] for i in f["reference_uncited"][3]["items"]] == ["spare"]
    assert f["reference_summary"][3]["verification"] == "skipped"  # no lookups
    assert f["reference_summary"][4] == 2 and "references_missing" not in f
    stored = {s["code"] for s in current_signals(con, pid) if s["check_name"] == "references"}
    assert stored == set(f)


def sections(body, refs):
    return {"sections": [{"heading": "Introduction", "text": body}, {"heading": "References", "text": refs}]}


def test_numeric_citations_typed_labels():
    got = numeric_citations(sections("As shown [1], [3]–[4] and [5, 7-8].",
                                     "[1] A.\n[2] B.\n[3] C.\n[4] D.\n[5] E.\n[6] F.\n[7] G."))
    assert got["undefined"] == [8]
    assert got["uncited"] == [2, 6]
    assert got["numbered_by"] == "typed [n] labels"


def test_numeric_citations_auto_numbered_and_author_year():
    got = numeric_citations(sections("See [2].", "A. Author, T.\n\nB. Author, U."))
    assert got["uncited"] == [1] and got["numbered_by"] == "position in the list"
    assert numeric_citations(sections("See (Smith, 2020).", "Smith, J. (2020).")) is None
    assert numeric_citations({"sections": [{"heading": "Intro", "text": "[1]"}]}) is None
    assert numeric_citations(sections("[1], [1-500]", "[1] A."))["undefined"] == []  # absurd range ignored


def test_numeric_citations_ignore_years_indices_and_join_ranges():
    got = numeric_citations(sections("In [2020] x[12] and [0], see [1]–[4].", "[1] A.\n[2] B.\n[3] C.\n[4] D."))
    assert got["undefined"] == [] and got["uncited"] == []


def test_offline_word_run(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    d = docx.Document()
    d.add_heading("Introduction", 1)
    d.add_paragraph("Prior work [1] and [3] shows this.")
    d.add_heading("References", 1)
    d.add_paragraph("[1] A. Author, “A study of things,” J. Things, vol. 1, 2020.")
    d.add_paragraph("[2] B. Other, “Unused work,” J. Things, vol. 2, 2019.")
    d.save(tmp_path / "w.docx")
    _, [pid] = ingest(con, [tmp_path / "w.docx"], data)
    convert_paper(con, pid, data)
    f = {x[0]: x for x in run_references(con, pid, data, offline())}
    assert "[3]" in [i["key"] for i in f["reference_undefined"][3]["items"]]
    assert "[2]" in [i["key"] for i in f["reference_uncited"][3]["items"]]


def test_crash_becomes_a_signal(tmp_path, monkeypatch):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    _, [pid] = ingest(con, [bundle(tmp_path)], data)

    def boom(*a, **k):
        raise RuntimeError("parser bug")
    monkeypatch.setattr("screening.references.check_with_preflight", boom)
    assert codes(run_references(con, pid, data, offline())) == {"references_crashed"}


def test_unknown_source_names_are_rejected(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[references]\ndisabled_sources = ["crossref"]\n')
    with pytest.raises(ValueError, match="crossref"):
        load_rules(p)


@pytest.mark.skipif(not os.environ.get("SCREENING_NETWORK_TESTS"), reason="set SCREENING_NETWORK_TESTS=1")
def test_live_lookup_flags_retraction(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    b = bundle(tmp_path)
    (b / "refs.bib").write_text(BIB + """@article{wakefield, author={Wakefield, A. J. and others},
 title={Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive developmental disorder in children},
 journal={The Lancet}, year={1998}, doi={10.1016/S0140-6736(97)11096-0}}
""")
    (b / "main.tex").write_text(TEX.replace(r"\cite{nokey}", r"\cite{wakefield}"))
    _, [pid] = ingest(con, [b], data)
    assert "reference_retracted" in codes(run_references(con, pid, data), "fail")


def test_cli_ingest_runs_every_check_in_parallel(tmp_path, capsys):
    from screening.__main__ import main
    for i in range(3):
        bundle(tmp_path / f"b{i}")
    data = tmp_path / "data"
    main(["--data", str(data), "ingest", *(str(tmp_path / f"b{i}" / "p") for i in range(3)), "--offline", "--jobs", "2"])
    out = capsys.readouterr()
    assert "     3  fail:reference_undefined" in out.out
    assert "converted and checked: 3/3" in out.err
    con = connect(data / "screening.db")
    runs = con.execute("SELECT check_name, count(*) FROM check_run GROUP BY check_name").fetchall()
    assert {r[0]: r[1] for r in runs} == {"intake": 3, "conversion": 3, "compliance": 3, "references": 3,
                                             "duplicates": 3}


def test_cli_check_resumes_an_interrupted_ingest_and_tallies_flags(tmp_path, capsys):
    from screening.__main__ import main
    bundle(tmp_path / "b")
    data = tmp_path / "data"
    main(["--data", str(data), "ingest", str(tmp_path / "b" / "p"), "--no-convert"])
    capsys.readouterr()
    main(["--data", str(data), "check", "--offline", "--jobs", "1"])
    out = capsys.readouterr().out
    con = connect(data / "screening.db")
    assert con.execute("SELECT status FROM paper").fetchone()[0] != "ingested"
    assert "     1  fail:reference_undefined" in out
