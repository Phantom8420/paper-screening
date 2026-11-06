import shutil

import docx
import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from screening.compliance import (
    check_abstract, check_anonymity, check_page_size, check_pages, check_sections, check_template_latex,
    docx_layout, docx_metadata, load_rules, run_compliance,
)
from screening.convert import convert_paper
from screening.db import connect, current_signals
from screening.intake import ingest

RULES = load_rules()
HAS_LATEX = bool(shutil.which("latexmk"))
HAS_OFFICE = bool(shutil.which("soffice") or shutil.which("libreoffice"))


def codes(findings, severity=None):
    return {f[0] for f in findings if severity is None or f[1] == severity}


def pages(n, ref_at=None, top=False):
    out = [f"Body text page {i + 1}\nmore\nmore\nmore" for i in range(n)]
    if ref_at:
        out[ref_at - 1] = ("REFERENCES\n[1] A." if top else out[ref_at - 1] + "\nREFERENCES\n[1] A.")
    return out


# --- pages ---

def test_pages_ok_when_references_start_on_page_six():
    assert codes(check_pages(pages(7, ref_at=6), RULES)) == {"page_limit_ok"}


def test_body_overflow_is_a_fail():
    f = check_pages(pages(8, ref_at=7), RULES)
    assert "body_page_limit_exceeded" in codes(f, "fail")


def test_references_at_top_of_page_seven_means_six_body_pages():
    assert codes(check_pages(pages(8, ref_at=7, top=True), RULES)) == {"page_limit_ok"}


def test_total_overflow_and_unlocated_references():
    f = check_pages(pages(9), RULES)
    assert codes(f) == {"page_limit_exceeded", "references_not_located"}


def test_paper_size():
    assert codes(check_page_size([(612, 792), (595.3, 841.9), (792, 612)], RULES)) == {"paper_size_ok"}
    f = check_page_size([(612, 792), (612, 1008)], RULES)
    assert codes(f, "fail") == {"paper_size"} and f[0][3]["pages"] == [2]


# --- structure ---

def struct(headings=(), **kw):
    return {"sections": [{"heading": h, "level": 1, "text": "x\n"} for h in headings], **kw}


def test_sections_with_numbering_and_bib_command():
    f = check_sections(struct(["I. Introduction", "II. Method", "V. Conclusions"]), "", r"\bibliography{refs}", RULES)
    assert codes(f) == {"sections_ok"}


def test_missing_conclusion():
    f = check_sections(struct(["Introduction", "References"]), "", None, RULES)
    assert codes(f, "fail") == {"missing_sections"} and f[0][3]["missing"] == ["Conclusion"]


def test_unstyled_word_headings_found_in_text():
    text = "1. Introduction\nLong body sentence here.\n5. Conclusion\nReferences\n[1] X"
    assert codes(check_sections(struct(), text, None, RULES)) == {"sections_ok"}


def test_abstract_and_keywords():
    assert codes(check_abstract(struct(abstract="We do things."), "", r"\begin{IEEEkeywords}", RULES)) == {
        "abstract_ok", "keywords_ok"}
    f = check_abstract(struct(), "Abstract—" + "word " * 300 + "\n", None, RULES)
    assert codes(f, "warn") == {"abstract_too_long", "missing_keywords"}
    assert "missing_abstract" in codes(check_abstract(struct(), "", None, RULES), "fail")


# --- anonymity ---

NO_META = {"people": [], "markup_authors": []}


def test_placeholder_authors_pass():
    f = check_anonymity(struct(["Introduction"], authors=["Anonymous Authors"]), "Plain text.", None, NO_META, [], RULES)
    assert codes(f) == {"author_list_unavailable", "anonymity_ok"}


def test_identifying_material_is_flagged():
    s = struct(["Introduction", "Acknowledgments"], authors=["Jane Doe"])
    s["sections"].insert(0, {"heading": None, "text": "Dept. of CS, Example University\nAbstract—We study.\n"})
    s["sections"][-1]["text"] = "We thank the Example Foundation for grant 42."
    text = "Contact jane@example.edu. As in our previous work [3], Jane Doe shows..."
    src = r"\author{\IEEEauthorblockN{Jane Doe}\IEEEauthorblockA{Example University}}"
    meta = {"people": [("creator", "Jane Doe"), ("lastModifiedBy", "Microsoft Office User")],
            "markup_authors": ["J. Doe"]}
    f = check_anonymity(s, text, src, meta, ["Jane Doe"], RULES)
    assert codes(f, "fail") == {"author_block_present", "affiliation_in_front_matter", "email_in_text",
                                "known_author_named", "file_metadata_identifies"}
    assert codes(f, "warn") == {"self_reference", "acknowledgments_present", "tracked_changes_or_comments"}
    meta_ev = next(x for x in f if x[0] == "file_metadata_identifies")[3]
    assert meta_ev["fields"] == [{"field": "creator", "value": "Jane Doe"}]  # generic account name ignored


def test_anonymity_off_when_not_double_blind():
    rules = load_rules()
    rules["anonymization"]["double_blind"] = False
    assert codes(check_anonymity(struct(authors=["Jane"]), "", None, NO_META, [], rules)) == {"anonymity_not_required"}


# --- template ---

IEEE = r"\documentclass[conference]{IEEEtran}" + "\n"


def test_template_latex():
    assert codes(check_template_latex(IEEE, RULES)) == {"template_ok"}
    assert codes(check_template_latex(r"\documentclass{article}", RULES), "fail") == {"wrong_template"}
    assert codes(check_template_latex(r"\documentclass{IEEEtran}", RULES), "warn") == {"template_options"}
    hacked = IEEE + "\\usepackage[margin=0.5in]{geometry}\n\\setlength{\\textheight}{10in}\n" + "\\vspace{-2mm}" * 11
    f = check_template_latex(hacked, RULES)
    assert codes(f) == {"layout_modified", "negative_vspace"}
    assert len(next(x for x in f if x[0] == "layout_modified")[3]["changes"]) == 2


def word_doc(path, author="", columns=1, font=None):
    d = docx.Document()
    d.core_properties.author = author
    d.core_properties.last_modified_by = ""
    if font:
        d.styles["Normal"].font.name = font
    if columns > 1:
        sect = d.sections[0]._sectPr
        cols = sect.find(qn("w:cols"))
        if cols is None:
            cols = OxmlElement("w:cols")
            sect.append(cols)
        cols.set(qn("w:num"), str(columns))
    d.add_heading("Paper", 0)
    for h in ("Introduction", "Conclusion", "References"):
        d.add_heading(h, 1)
        d.add_paragraph("Text.")
    d.save(path)
    return path


def test_docx_metadata_and_layout(tmp_path):
    p = word_doc(tmp_path / "a.docx", author="Jane Doe", columns=2, font="Times New Roman")
    assert docx_metadata(p)["people"] == [("creator", "Jane Doe")]
    assert docx_layout(p) == {"columns": 2, "body_font": "Times New Roman"}
    assert docx_layout(word_doc(tmp_path / "b.docx", font="Arial"))["columns"] == 1


# --- runner ---

def test_rerun_supersedes_previous_flags(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    _, [pid] = ingest(con, [word_doc(tmp_path / "w.docx", author="Jane Doe")], data)
    convert_paper(con, pid, data)
    first = run_compliance(con, pid, data)
    run_compliance(con, pid, data)
    current = [s for s in current_signals(con, pid) if s["check_name"] == "compliance"]
    assert len(current) == len(first)
    total = con.execute("SELECT count(*) FROM signal WHERE check_name='compliance'").fetchone()[0]
    assert total == 2 * len(first)  # history kept
    got = {s["code"] for s in current}
    assert {"file_metadata_identifies", "sections_ok", "missing_abstract"} <= got
    if not HAS_OFFICE:
        assert "page_check_skipped" in got


def test_unknown_papers_are_not_checked(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4\n")
    _, [pid] = ingest(con, [tmp_path / "x.pdf"], data)
    assert run_compliance(con, pid, data) == []


@pytest.mark.skipif(not HAS_LATEX, reason="latexmk not installed")
def test_ieee_latex_end_to_end(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    b = tmp_path / "p"
    b.mkdir()
    (b / "main.tex").write_text(IEEE + r"""\begin{document}
\title{Edge Caching}
\author{\IEEEauthorblockN{Jane Doe}\IEEEauthorblockA{Example University\\ jane@example.edu}}
\maketitle
\begin{abstract}We cache things at the edge.\end{abstract}
\begin{IEEEkeywords}caching, edge\end{IEEEkeywords}
\section{Introduction}Text.
\section{Conclusion}Done.
\begin{thebibliography}{1}\bibitem{a} A. Author, ``T,'' 2020.\end{thebibliography}
\end{document}
""")
    _, [pid] = ingest(con, [b], data)
    assert convert_paper(con, pid, data) == "converted"
    f = run_compliance(con, pid, data)
    assert codes(f, "fail") == {"author_block_present"}
    assert {"page_limit_ok", "paper_size_ok", "sections_ok", "abstract_ok", "keywords_ok", "template_ok"} <= codes(f)
    assert con.execute("SELECT status FROM paper WHERE id=?", (pid,)).fetchone()[0] == "checked"


@pytest.mark.skipif(not HAS_OFFICE, reason="LibreOffice not installed")
def test_word_end_to_end(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    _, [pid] = ingest(con, [word_doc(tmp_path / "w.docx", columns=2, font="Times New Roman")], data)
    assert convert_paper(con, pid, data) == "converted"
    f = run_compliance(con, pid, data)
    assert {"page_limit_ok", "paper_size_ok", "sections_ok", "template_ok"} <= codes(f)
    assert "file_metadata_identifies" not in codes(f)


# --- review regressions ---

def test_regexes_stay_linear_on_hostile_text():
    import time
    from screening.compliance import EMAIL, REF_HEADING, ABSTRACT_LINE
    t0 = time.perf_counter()
    REF_HEADING.search("\n" * 50_000)
    ABSTRACT_LINE.search(" \n" * 50_000)
    EMAIL.search("a" * 200_000)
    assert time.perf_counter() - t0 < 2


def test_overflow_lines_above_references_count_as_body():
    texts = ["a long body line\n"] * 6 + ["tail of the body text\nmore of it here\nReferences\n[1] x"]
    assert "body_page_limit_exceeded" in codes(check_pages(texts, RULES), "fail")
    assert codes(check_pages(["x\n"] * 6 + ["7\nReferences\n[1] x"], RULES)) == {"page_limit_ok"}  # page number only


def test_summary_is_not_a_conclusion():
    f = check_sections(struct(["Introduction", "Summary of results", "References"]), "", None, RULES)
    assert f[0][3]["missing"] == ["Conclusion"]
    assert codes(check_sections(struct(["Introduction", "Conclusions and Future Work", "References"]), "", None,
                                RULES)) == {"sections_ok"}
    f = check_sections(struct(["Introduction", "Conclusion"]), "References are listed below\n", None, RULES)
    assert f[0][3]["missing"] == ["References"]


def test_placeholder_does_not_hide_real_names():
    s = struct(["Introduction"], authors=["Anonymous Submission, John Smith, MIT"])
    assert "author_block_present" in codes(check_anonymity(s, "", None, NO_META, [], RULES), "fail")
    src = r"\IEEEauthorblockN{John \textbf{\emph{Smith}}}\IEEEauthorblockN{Paper ID 123}"
    f = check_anonymity(struct(), "", src, NO_META, [], RULES)
    assert next(x for x in f if x[0] == "author_block_present")[3]["authors"] == ["John Smith"]


def test_author_name_forms():
    f = check_anonymity(struct(), "As J. Smith showed earlier.", None, NO_META, ["Smith, J."], RULES)
    assert "known_author_named" in codes(f)


def test_more_layout_hacks():
    for hack in (r"\usepackage{savetrees}", r"\textheight=10in", r"\newgeometry{margin=1cm}"):
        assert "layout_modified" in codes(check_template_latex(IEEE + hack, RULES)), hack
    assert "template_options" in codes(check_template_latex(r"\documentclass[conference,onecolumn]{IEEEtran}", RULES))


def test_theme_font_and_body_columns(tmp_path):
    from screening.compliance import check_template_docx
    p = word_doc(tmp_path / "t.docx")  # python-docx default: theme font
    assert docx_layout(p)["body_font"] is None
    assert "font_unverified" in codes(check_template_docx(docx_layout(p), RULES))
    lay = {"columns": 2, "body_font": "times new roman"}
    assert codes(check_template_docx(lay, RULES)) == {"template_ok"}


def test_rules_file_is_validated(tmp_path):
    bad = tmp_path / "r.toml"
    bad.write_text('pages = 5\n')
    with pytest.raises(ValueError):
        load_rules(bad)
    bad.write_text('[sections]\nrequired = "Introduction"\n')
    with pytest.raises(ValueError):
        load_rules(bad)
    good = tmp_path / "g.toml"
    good.write_text('[pages]\nmax_body = 8\n')
    assert load_rules(good)["pages"] == {"max_body": 8, "max_total": 8}


def test_bad_docx_is_its_own_finding(tmp_path):
    data = tmp_path / "data"
    con = connect(data / "t.db")
    _, [pid] = ingest(con, [word_doc(tmp_path / "w.docx")], data)
    convert_paper(con, pid, data)
    src = next((data / "batches").rglob("source/w.docx"))
    import zipfile
    with zipfile.ZipFile(src, "w") as z:  # still a .docx by its parts, but the XML is broken
        z.writestr("word/document.xml", "<w:document")
        z.writestr("docProps/core.xml", "<not xml")
    f = run_compliance(con, pid, data)
    assert "docx_unreadable" in codes(f, "fail") and "compliance_crashed" not in codes(f)
    assert "sections_ok" in codes(f)  # other stages still report
