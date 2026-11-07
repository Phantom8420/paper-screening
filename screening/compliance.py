"""M2 format and compliance checks.

Each check_* function is pure: it takes extracted data and returns findings
(code, severity, message, evidence, score). run_compliance gathers the inputs for one
paper and stores the findings as one check run, so a re-run supersedes the old flags.
"""
import copy
import json
import re
import tomllib
import zipfile
from collections import Counter
from itertools import islice
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from .convert import IncludeLimit, flatten_latex
from .db import add_signal, audit, start_run
from .sniff import sniff

VERSION = "1"

DEFAULT_RULES = {  # IEEE conference defaults; override any key with a TOML file
    "pages": {"max_body": 6, "max_total": 8},
    "sections": {"required": ["Introduction", "Conclusion", "References"]},
    "abstract": {"required": True, "max_words": 250, "keywords_required": True},
    "anonymization": {"double_blind": True},
    "template": {
        "latex_class": "IEEEtran",
        "latex_options": ["conference"],
        "paper_sizes": ["letter", "a4"],
        "word_columns": 2,
        "fonts": ["Times New Roman", "Times", "TimesNewRoman", "Nimbus Roman"],
        "max_negative_vspace": 10,
        "forbidden_latex_options": ["onecolumn", "draft", "draftcls", "9pt", "8pt"],
    },
    # web/wayback would contact authors' own servers; s2 has its own licence terms
    "references": {"online": True, "disabled_sources": ["web", "wayback", "s2"]},
    # share of the shorter paper's text (references excluded) also found in another paper in the database
    "duplicates": {"warn_overlap": 0.3, "fail_overlap": 0.7},
}
OPTIONAL_SOURCES = {"s2", "openlibrary", "github", "pypi", "cran", "huggingface", "openml", "web", "wayback"}
MAX_TEXT = 2_000_000  # chars of extracted text scanned; bounds every regex below
MAX_PDF_PAGES = 200  # beyond this the page limit has already failed; skip text extraction
MAX_XML = 20 * 2**20  # bytes read from any one part of a .docx (zip bombs)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SECTION_PATTERNS = {  # whole normalised heading must match; "Summary of results" is not a conclusion
    "introduction": r"introduction(?: and .+)?",
    "conclusion": r"(?:.+ and )?conclusions?(?: and .+)?|concluding remarks",
    "references": r"references?|bibliography",
}
PAPER_SIZES = {"letter": (612.0, 792.0), "a4": (595.3, 841.9)}

NUMBERING = re.compile(r"^[ \t]*(?:(?:[ivxlc]+|[a-z])[.)]|\d+(?:\.\d+)*[.)]?)\s+", re.I)
REF_HEADING = re.compile(r"^[ \t]*(?:[IVXLC]+\.|\d+\.?)?[ \t]*(?:references|bibliography)[ \t]*$", re.I | re.M)
BIB_SRC = re.compile(r"\\(?:bibliography|printbibliography)\b|\\begin\{thebibliography\}")
# text.txt holds one paragraph per line (pandoc AST, no wrapping), so (.+) is the whole abstract paragraph
ABSTRACT_LINE = re.compile(r"^[ \t]*abstract[ \t]*[—–\-:.][ \t]*(.+)", re.I | re.M)
KEYWORDS = re.compile(r"^[ \t]*(?:keywords|key\s*words|index\s+terms)\b", re.I | re.M)
KEYWORDS_SRC = re.compile(r"\\begin\{IEEEkeywords\}|\\keywords\s*\{")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")  # guard keeps it linear
AFFILIATION = re.compile(r"\b(?:universit\w*|institut\w*|department|dept\.|laborator\w*|college|school of|faculty"
                         r"|inc\.|ltd\.?|corporation|gmbh)\b", re.I)
PLACEHOLDER = re.compile(  # the whole author string must be placeholder words; any real name left fails it
    r"(?:anonymous|anonymi[sz]ed|blind(?:ed)?|withheld|redacted|hidden|authors?|\(s\)|names?|for|review|double|"
    r"submission|paper|id|no|number|#|\d+|and|of|the|institution|affiliations?|organi[sz]ations?|email|"
    r"address(?:es)?|[\s,.:;()\-])+", re.I)
SELF_REF = re.compile(r"\b(?:our|my)\s+(?:previous|prior|earlier|recent)\s+(?:work|paper|study|studies|publication)s?\b"
                      r"|\bwe\s+(?:have\s+)?previously\s+(?:proposed|presented|shown|introduced|published)\b"
                      r"|\bin\s+our\s+(?:previous|prior|earlier)\b", re.I)
# repo/profile links name their owner; anonymous.4open.science and "anonymous" accounts are the sanctioned way
OWNER_LINK = re.compile(  # quantifiers are bounded: this runs on untrusted text and must stay linear
    r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:(?:(?:github|gitlab|bitbucket|huggingface|kaggle)\.(?:com|org|co)"
    r"|sites\.google\.com/view|scholar\.google\.[a-z.]{2,10}/citations\?user=|orcid\.org|linkedin\.com/in)"
    r"/([\w.~=-]{1,80})|([\w-]{1,40})\.github\.io\b|https?://[^\s/]{1,100}(?:/[^\s/~]{1,100}){0,5}/(~[\w.-]{1,40}))",
    re.I)
ANON_OWNER = re.compile(r"~?(?:anon(?:ymous|ymi[sz]ed)?|redacted|withheld|blinded|double-?blind)(?:[-_.]\w*)?", re.I)
NOT_BODY = ("reference", "bibliograph")
AUTHOR_MACRO = re.compile(r"\\(?:IEEEauthorblock[NA]|author|thanks)\s*\{")
GENERIC_USERS = {"", "user", "admin", "administrator", "author", "owner", "unknown", "microsoft office user",
                 "windows user", "office user"}
ACKNOWLEDGMENTS = ("acknowledgment", "acknowledgement")
DOCCLASS = re.compile(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")
LAYOUT_HACKS = [
    (re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\b(?:geometry|savetrees|fullpage|vmargin|a4wide)\b"),
     "package changes the margins"),
    (re.compile(r"\\(?:newgeometry|geometry)\s*\{"), "geometry changes the margins"),
    (re.compile(r"\\(?:set|addto)length\s*\{?\s*\\(?:textheight|textwidth|topmargin|oddsidemargin|evensidemargin"
                r"|columnsep|headsep|footskip|headheight)\b"), "page dimensions changed"),
    (re.compile(r"\\(?:textheight|textwidth|topmargin|oddsidemargin|evensidemargin|columnsep)\s*=?\s*-?[\d.]"),
     "page dimensions changed"),
    (re.compile(r"\\(?:linespread|setstretch)\s*\{\s*0?\.\d+"), "line spacing reduced"),
    (re.compile(r"\\(?:renewcommand|def)\s*\{?\\baselinestretch\}?\s*\{\s*0?\.\d+"), "line spacing reduced"),
]
NEG_VSPACE = re.compile(r"\\vspace\*?\s*\{\s*(?:\\dimexpr\s*)?-|\\vskip\s*-")


def load_rules(path=None) -> dict:
    """Defaults overlaid with a TOML file. Unknown keys and wrong types are errors, not silent no-ops."""
    rules = copy.deepcopy(DEFAULT_RULES)
    if path:
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                if section not in rules or not isinstance(values, dict):
                    raise ValueError(f"{path}: [{section}] is not a known rules table")
                for key, value in values.items():
                    default = rules[section].get(key)
                    if default is None or type(value) is not type(default):
                        raise ValueError(f"{path}: {section}.{key} is unknown or should be {type(default).__name__}")
                    rules[section][key] = value
    if bad := set(rules["template"]["paper_sizes"]) - PAPER_SIZES.keys():
        raise ValueError(f"unknown paper size(s): {', '.join(sorted(bad))}")
    if bad := set(rules["references"]["disabled_sources"]) - OPTIONAL_SOURCES:
        raise ValueError(f"references.disabled_sources: {', '.join(sorted(bad))} cannot be turned off "
                         f"(optional sources: {', '.join(sorted(OPTIONAL_SOURCES))})")
    return rules


def _f(code, severity, message, evidence=None, score=None):
    return (code, severity, message, evidence or {}, score)


def _norm(heading: str) -> str:
    return NUMBERING.sub("", heading.strip().lower()).strip(" .:—–-")


def _snippets(regex, text, n=5, width=60):
    return [text[max(0, m.start() - width):m.end() + width].replace("\n", " ").strip()
            for m in islice(regex.finditer(text), n)]


def _ok(findings):
    return not any(f[1] in ("warn", "fail") for f in findings)


# --- pages and paper size -------------------------------------------------------------

def check_pages(page_texts: list[str], rules) -> list:
    r = rules["pages"]
    total = len(page_texts)
    ev = {"total_pages": total, "max_total": r["max_total"], "max_body": r["max_body"]}
    out = []
    if total > r["max_total"]:
        out.append(_f("page_limit_exceeded", "fail",
                      f"The paper has {total} pages; the limit is {r['max_total']} including references.", ev, total))
    ref_page = None
    for i in range(total - 1, -1, -1):  # last match: the reference list is at the end
        m = REF_HEADING.search(page_texts[i] or "")
        if m:
            # any real text above the heading is body overflow; only page numbers / tiny header bits are allowed
            above = [ln for ln in page_texts[i][:m.start()].splitlines() if len(ln.strip()) > 4]
            ref_page = i + 1
            body = ref_page if above else ref_page - 1
            break
    if ref_page is None:
        out.append(_f("references_not_located", "warn",
                      "Could not find where the references start, so the main-text length was not checked.", ev))
        body = None
    else:
        ev.update(body_pages=body, references_start_page=ref_page)
        if body > r["max_body"]:
            out.append(_f("body_page_limit_exceeded", "fail",
                          f"The main text runs to page {body}; the limit is {r['max_body']} pages before the "
                          f"references (references start on page {ref_page}).", ev, body))
    if _ok(out):
        detail = f", {body} before the references" if body is not None else ""
        out.append(_f("page_limit_ok", "info", f"{total} pages{detail}.", ev, total))
    return out


def check_page_size(sizes: list[tuple[float, float]], rules) -> list:
    allowed = rules["template"]["paper_sizes"]

    def fits(w, h):
        return any(abs(min(w, h) - a) <= 3 and abs(max(w, h) - b) <= 3
                   for a, b in (PAPER_SIZES[s] for s in allowed))

    if not sizes:
        return [_f("paper_size_unchecked", "warn", "The PDF has no pages, so paper size was not checked.")]
    bad = [i + 1 for i, (w, h) in enumerate(sizes) if not fits(w, h)]
    if bad:
        w, h = sizes[bad[0] - 1]
        return [_f("paper_size", "fail", f"Page {bad[0]} is {w:.0f}x{h:.0f} pt, not {' or '.join(allowed)} size.",
                   {"pages": bad[:20], "size_pt": [round(w), round(h)], "allowed": allowed})]
    return [_f("paper_size_ok", "info", f"All pages are {' or '.join(allowed)} size.")]


# --- structure ------------------------------------------------------------------------

def _find(name, candidates):
    rx = re.compile(SECTION_PATTERNS.get(name.lower(), re.escape(name.lower()) + "s?"))
    return next((c for c in candidates if rx.fullmatch(c)), None)


def check_sections(struct, text, latex_src, rules) -> list:
    heads = [_norm(s["heading"]) for s in struct.get("sections", []) if s.get("heading")]
    lines = [_norm(ln) for ln in text.splitlines() if 0 < len(ln.strip()) <= 60]  # Word headings without styles
    found, missing = {}, []
    for name in rules["sections"]["required"]:
        if _find(name, heads):
            found[name] = "heading"
        elif _find(name, lines):
            found[name] = "short line in text (not a styled heading)"
        elif re.fullmatch(SECTION_PATTERNS["references"], name.lower()) and latex_src and BIB_SRC.search(latex_src):
            found[name] = "bibliography command in LaTeX source"
        else:
            missing.append(name)
    if missing:
        return [_f("missing_sections", "fail", f"Missing required section(s): {', '.join(missing)}.",
                   {"missing": missing, "found": found, "headings": heads[:40]})]
    return [_f("sections_ok", "info", "All required sections are present.", {"found": found})]


def _abstract(struct, text):
    if struct.get("abstract"):
        return struct["abstract"]
    for s in struct.get("sections", []):
        if s.get("heading") and _norm(s["heading"]) == "abstract":
            return s["text"].strip() or None
    m = ABSTRACT_LINE.search(text)
    return m.group(1).strip() if m else None


def check_abstract(struct, text, latex_src, rules) -> list:
    r = rules["abstract"]
    out = []
    abstract = _abstract(struct, text)
    if not abstract:
        if r["required"]:
            out.append(_f("missing_abstract", "fail", "No abstract was found."))
    else:
        words = len(abstract.split())
        ev = {"words": words, "max_words": r["max_words"], "excerpt": abstract[:300]}
        if words > r["max_words"]:
            out.append(_f("abstract_too_long", "warn",
                          f"The abstract has {words} words; the limit is {r['max_words']}.", ev, words))
        else:
            out.append(_f("abstract_ok", "info", f"Abstract present ({words} words).", ev, words))
    if r["keywords_required"]:
        if struct.get("keywords") or KEYWORDS.search(text) or (latex_src and KEYWORDS_SRC.search(latex_src)):
            out.append(_f("keywords_ok", "info", "Keywords or index terms are present."))
        else:
            out.append(_f("missing_keywords", "warn", "No keywords or index terms were found."))
    return out


# --- anonymization --------------------------------------------------------------------

def _clean_tex(s):
    return re.sub(r"\s+", " ", re.sub(r"\\[a-zA-Z]+\*?|[{}\\~]", " ", s)).strip()


def _braced(src, start):
    """Text of the {...} group that opens just before start, brace-balanced, capped at 5000 chars."""
    depth, i = 1, start
    end = min(len(src), start + 5000)
    while i < end and depth:
        if src[i - 1] != "\\":
            depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[start:i - 1 if depth == 0 else i]


def _name_rx(name):
    """'Smith, J.' and 'J. Smith' match the same text; whole words only, any punctuation between."""
    first_last = " ".join(reversed(name.split(",", 1))) if "," in name else name
    tokens = re.findall(r"\w+", first_last)
    return re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, tokens)) + r"(?!\w)", re.I) if tokens else None


def check_anonymity(struct, text, latex_src, file_meta, author_names, rules) -> list:
    if not rules["anonymization"]["double_blind"]:
        return [_f("anonymity_not_required", "info", "Review is not double-blind; anonymity was not checked.")]
    out = []

    named = [a for a in struct.get("authors", []) if not PLACEHOLDER.fullmatch(a.strip())]
    if latex_src:
        named += [c for c in (_clean_tex(_braced(latex_src, m.end())) for m in AUTHOR_MACRO.finditer(latex_src))
                  if c and not PLACEHOLDER.fullmatch(c)]
    if named:
        out.append(_f("author_block_present", "fail", "The paper shows author names or affiliations.",
                      {"authors": list(dict.fromkeys(named))[:10]}))

    sections = struct.get("sections", [])
    front = sections[0]["text"] if sections and not sections[0].get("heading") else ""
    if m := ABSTRACT_LINE.search(front):
        front = front[:m.start()]
    front_hits = [ln.strip() for ln in front.splitlines() if EMAIL.search(ln) or AFFILIATION.search(ln)]
    if front_hits:
        out.append(_f("affiliation_in_front_matter", "fail",
                      "Lines before the abstract look like an author block (email or affiliation).",
                      {"lines": front_hits[:10]}))

    emails = sorted(set(EMAIL.findall(text)))
    if emails:
        out.append(_f("email_in_text", "fail", f"The text contains {len(emails)} email address(es).",
                      {"emails": emails[:10]}))

    hits = [n for n in author_names if len(n) > 3 and (rx := _name_rx(n)) and rx.search(text)]
    if hits:
        out.append(_f("known_author_named", "fail", "Names from the author list appear in the text.",
                      {"names": hits[:10]}))
    elif not author_names:
        out.append(_f("author_list_unavailable", "info",
                      "No author list has been imported, so the text was not searched for author names."))

    if snippets := _snippets(SELF_REF, text):
        out.append(_f("self_reference", "warn", "Phrases that may reveal the authors' earlier work.",
                      {"snippets": snippets}))

    # the owner segment decides; links that are in the reference list cite other people's work
    refs = "\n".join(x["text"] for x in sections if _norm(x.get("heading") or "").startswith(NOT_BODY))
    links = [m.group(0) for m in OWNER_LINK.finditer(text)
             if not ANON_OWNER.fullmatch(next(g for g in m.groups() if g)) and m.group(0) not in refs]
    if links:
        out.append(_f("identifying_link", "warn",
                      "Links to code, profile or personal pages may name the authors; ask for an anonymized link.",
                      {"links": list(dict.fromkeys(links))[:10]}))

    ack = [s for s in sections if s.get("heading") and _norm(s["heading"]).startswith(ACKNOWLEDGMENTS)
           and len(s["text"].strip()) > 20]
    if ack:
        out.append(_f("acknowledgments_present", "warn", "An acknowledgments section may identify the authors.",
                      {"excerpt": ack[0]["text"].strip()[:300]}))

    people = [(field, value) for field, value in file_meta.get("people", [])
              if value.strip().lower() not in GENERIC_USERS]
    if people:
        out.append(_f("file_metadata_identifies", "fail", "File metadata contains names.",
                      {"fields": [{"field": f, "value": v} for f, v in people]}))
    if file_meta.get("markup_authors"):
        out.append(_f("tracked_changes_or_comments", "warn",
                      "The Word file contains comments or tracked changes with author names.",
                      {"authors": file_meta["markup_authors"][:10]}))

    if _ok(out) and text.strip():
        out.append(_f("anonymity_ok", "info", "No identifying information found."))
    return out


def _zread(z, part) -> bytes:
    info = z.getinfo(part)
    if info.file_size > MAX_XML:
        raise ValueError(f"{part} is {info.file_size // 2**20} MB (limit {MAX_XML // 2**20} MB)")
    return z.read(info)


def docx_metadata(path: Path) -> dict:
    people, markup = [], set()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for part, fields in (("docProps/core.xml", ("creator", "lastModifiedBy")), ("docProps/app.xml", ("Company",))):
            if part in names:
                for el in ET.fromstring(_zread(z, part)):
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag in fields and (el.text or "").strip():
                        people.append((tag, el.text.strip()))
        for part in ("word/document.xml", "word/comments.xml"):
            if part in names:
                markup |= {a.decode("utf-8", "replace") for a in re.findall(rb'w:author="([^"]*)"', _zread(z, part))}
    return {"people": people, "markup_authors": sorted(a for a in markup if a.strip())}


# --- template -------------------------------------------------------------------------

def _line_of(src, pos):
    start = src.rfind("\n", 0, pos) + 1
    end = src.find("\n", pos)
    return src[start:end if end != -1 else None].strip()[:200]


def check_template_latex(src, rules) -> list:
    t = rules["template"]
    out = []
    if m := DOCCLASS.search(src):
        cls = m.group(2).strip()
        opts = [o.strip() for o in (m.group(1) or "").split(",") if o.strip()]
        if cls != t["latex_class"]:
            out.append(_f("wrong_template", "fail", f"Uses document class {cls}; the template is {t['latex_class']}.",
                          {"line": m.group(0)}))
        else:
            missing = [o for o in t["latex_options"] if o not in opts]
            forbidden = [o for o in opts if o in t["forbidden_latex_options"]]
            if missing or forbidden:
                parts = ([f"missing {', '.join(missing)}"] if missing else []) + (
                    [f"not allowed {', '.join(forbidden)}"] if forbidden else [])
                out.append(_f("template_options", "warn",
                              f"Document class options differ from the template: {'; '.join(parts)}.",
                              {"line": m.group(0), "missing": missing, "forbidden": forbidden}))
    hacks = [{"issue": desc, "line": _line_of(src, hit.start())}
             for rx, desc in LAYOUT_HACKS for hit in rx.finditer(src)]
    if hacks:
        out.append(_f("layout_modified", "fail", "The source changes the template's page layout.",
                      {"changes": hacks[:20]}))
    neg = len(NEG_VSPACE.findall(src))
    if neg > t["max_negative_vspace"]:
        out.append(_f("negative_vspace", "warn",
                      f"{neg} negative \\vspace commands; space may have been squeezed to fit the page limit.",
                      {"count": neg, "threshold": t["max_negative_vspace"]}, neg))
    if _ok(out):
        out.append(_f("template_ok", "info", f"Uses {t['latex_class']} without layout changes."))
    return out


def _ascii_font(el):
    f = el.find(f"{W}rPr/{W}rFonts") if el is not None else None
    return f.get(f"{W}ascii") if f is not None else None


def docx_layout(path: Path) -> dict:
    """Columns of the last section (the body; IEEE titles sit in a 1-column section above it) and the font
    carrying the most text. body_font is None when that text uses a theme font we cannot resolve."""
    with zipfile.ZipFile(path) as z:
        doc = ET.fromstring(_zread(z, "word/document.xml"))
        styles = ET.fromstring(_zread(z, "word/styles.xml")) if "word/styles.xml" in z.namelist() else None
    cols = doc.find(f"{W}body/{W}sectPr/{W}cols")
    num = cols.get(f"{W}num") if cols is not None else None
    columns = int(num) if num and num.isdigit() and len(num) <= 3 else 1
    default = None
    if styles is not None:
        normal = next((s for s in styles.iter(f"{W}style") if s.get(f"{W}styleId") == "Normal"), None)
        default = _ascii_font(normal) or _ascii_font(styles.find(f"{W}docDefaults/{W}rPrDefault"))
    weight = Counter()
    for run in doc.iter(f"{W}r"):
        weight[_ascii_font(run) or default] += sum(len(t.text or "") for t in run.iter(f"{W}t"))
    body_font = weight.most_common(1)[0][0] if weight else default
    return {"columns": columns, "body_font": body_font}


def check_template_docx(layout, rules) -> list:
    t = rules["template"]
    out = []
    if layout["columns"] < t["word_columns"]:
        out.append(_f("layout_columns", "warn",
                      f"The document has {layout['columns']} column(s); the template uses {t['word_columns']}.",
                      layout))
    if not layout["body_font"]:
        out.append(_f("font_unverified", "warn", "The main font comes from the document theme, so it could not be "
                      "checked against the template.", layout))
    elif layout["body_font"].lower() not in {f.lower() for f in t["fonts"]}:
        out.append(_f("font_mismatch", "warn", f"The main font is {layout['body_font']}; the template uses "
                      f"{t['fonts'][0]}.", layout))
    if _ok(out):
        out.append(_f("template_ok", "info", "Columns and font match the template.", layout))
    return out


# --- runner ---------------------------------------------------------------------------

def _stage(out, code, what, fn, *args):
    """Run one stage; a crash becomes its own finding and the other stages still report."""
    try:
        return fn(*args)
    except Exception as e:  # untrusted files raise anything: zip, XML, PDF, decoding errors
        out.append(_f(code, "fail", f"{what} could not be read, so its checks were skipped.",
                      {"error": f"{type(e).__name__}: {str(e)[:300]}"}))
        return None


def _pdf_facts(path):
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("the PDF is encrypted")
    pages = reader.pages
    n = len(pages)
    texts = [(p.extract_text() or "")[:MAX_TEXT] for p in pages] if n <= MAX_PDF_PAGES else [""] * n
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in islice(pages, MAX_PDF_PAGES)]
    author = reader.metadata.author if reader.metadata else None
    return texts, sizes, author


def _gather(con, paper_id, data_dir: Path, rules) -> list:
    row = con.execute("SELECT * FROM paper WHERE id=?", (paper_id,)).fetchone()
    out = []
    struct = json.loads(row["sections_json"]) if row["sections_json"] else None
    text = ""
    if row["text_path"]:
        text = (data_dir / row["text_path"]).read_text(encoding="utf-8", errors="replace")[:MAX_TEXT]
    src = data_dir / row["source_dir"] if row["source_dir"] else None
    main = src / row["main_file"] if src and row["main_file"] else None

    latex_src = None
    if row["format"] == "latex" and main and main.exists():
        try:
            latex_src = flatten_latex(main, src)
        except IncludeLimit:
            pass
    docx = None
    if row["format"] == "word" and main and main.exists():
        docx = main if sniff(main) == "docx" else src.parent / "derived" / f"{main.stem}.docx"
        docx = docx if docx.exists() else None
        if docx is None:
            out.append(_f("word_checks_skipped", "warn", "No readable .docx was available, so Word file metadata "
                          "and layout were not checked."))

    file_meta = (docx and _stage(out, "docx_unreadable", "The Word file's metadata", docx_metadata, docx)) or {
        "people": [], "markup_authors": []}
    if row["pdf_path"]:
        facts = _stage(out, "pdf_unreadable", "The PDF", _pdf_facts, data_dir / row["pdf_path"])
        if facts:
            texts, sizes, author = facts
            out += check_pages(texts, rules)
            out += check_page_size(sizes, rules)
            if author:
                file_meta["people"].append(("pdf_author", str(author)))
    else:
        out.append(_f("page_check_skipped", "warn", "There is no PDF, so page limit and paper size were not checked."))

    if struct is None:
        out.append(_f("structure_unavailable", "warn",
                      "The text could not be extracted, so section, abstract and text anonymity checks were skipped."))
    else:
        out += check_sections(struct, text, latex_src, rules)
        out += check_abstract(struct, text, latex_src, rules)
    names = [r[0] for r in con.execute("SELECT name FROM author WHERE paper_id=? AND name IS NOT NULL", (paper_id,))]
    out += check_anonymity(struct or {}, text, latex_src, file_meta, names, rules)
    if latex_src:
        out += check_template_latex(latex_src, rules)
    if docx and (layout := _stage(out, "docx_unreadable", "The Word file's layout", docx_layout, docx)):
        out += check_template_docx(layout, rules)
    return out


def run_compliance(con, paper_id: int, data_dir, rules=None) -> list:
    """Run all compliance checks for one paper and store them as one run. Never raises."""
    rules = rules or load_rules()
    fmt = con.execute("SELECT format FROM paper WHERE id=?", (paper_id,)).fetchone()[0]
    if fmt == "unknown":
        return []  # nothing to check; intake already flagged it for a person
    try:
        findings = _gather(con, paper_id, Path(data_dir), rules)
    except Exception as e:
        findings = [_f("compliance_crashed", "fail", "Compliance checks stopped with an unexpected error.",
                       {"error": f"{type(e).__name__}: {e}"})]
    run_id = start_run(con, paper_id, "compliance", VERSION, rules)
    for code, severity, message, evidence, score in findings:
        add_signal(con, run_id, code, severity, message, evidence, score)
    con.execute("UPDATE paper SET status='checked' WHERE id=? AND status='converted'", (paper_id,))
    audit(con, "system", "compliance", "paper", paper_id,
          {"run_id": run_id, "fail": sum(f[1] == "fail" for f in findings),
           "warn": sum(f[1] == "warn" for f in findings)})
    con.commit()
    return findings
