"""Convert each submission to a common representation (title, abstract, sections, text) and a PDF.

External tools run as subprocesses with timeouts. A missing tool or failed conversion
becomes a signal and marks the paper "needs_manual"; nothing is dropped.
"""
import functools
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pypandoc
from pypdf import PdfReader

from .db import add_signal, audit, start_run
from .sniff import sniff

TIMEOUT = 180
MAX_FLAT_CHARS = 5_000_000
MAX_INCLUDES = 500
MAX_LOG_BYTES = 5 * 2**20
BLOCKS = {"Para", "Plain", "Header", "CodeBlock", "BlockQuote", "OrderedList", "BulletList",
          "Table", "Div", "LineBlock", "DefinitionList", "Figure"}
INCLUDE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
COMMENT = re.compile(r"(?<!\\)%.*")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding notes flood the terminal; text is unaffected
FILE_LINE_ERROR = re.compile(r"^\S+:\d+: ")  # how -file-line-error prints errors ("!" only without it)
MISSING_FILE = re.compile(r"File `([\w.-]+\.(?:sty|cls))' not found")


def _kill_tree(p):
    # timeout must kill grandchildren too (pdflatex, soffice.bin), or communicate() hangs on Windows
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        os.killpg(p.pid, signal.SIGKILL)


def _run(cmd, cwd=None, input_text=None, env=None):
    """Return (ok, output). Never raises for tool failures."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed"
    try:
        out, err = p.communicate(input_text, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return False, f"timed out after {TIMEOUT}s"
    return p.returncode == 0, out if p.returncode == 0 else (out + err)[-4000:]


class IncludeLimit(Exception):
    pass


def flatten_latex(main: Path, root: Path, depth: int = 0, budget: dict | None = None) -> str:
    """Inline \\input/\\include, refusing anything outside the bundle (pandoc then runs sandboxed)."""
    budget = budget if budget is not None else {"chars": MAX_FLAT_CHARS, "files": MAX_INCLUDES}
    text = COMMENT.sub("", main.read_text(encoding="utf-8", errors="replace"))
    budget["chars"] -= len(text)
    budget["files"] -= 1
    if budget["chars"] < 0 or budget["files"] < 0:
        raise IncludeLimit(f"\\input expansion exceeds {MAX_INCLUDES} files or {MAX_FLAT_CHARS} characters")
    if depth > 10:
        return text

    def repl(m):
        target = (main.parent / m.group(1).strip())
        if target.suffix != ".tex":
            target = target.with_name(target.name + ".tex")
        target = target.resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            return ""
        return flatten_latex(target, root, depth + 1, budget)

    return INCLUDE.sub(repl, text)


def _text(node) -> str:
    if isinstance(node, list):
        return "".join(_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t, c = node.get("t"), node.get("c")
    if t in ("Str", "MetaString"):
        return c
    if t in ("Space", "SoftBreak", "LineBreak"):
        return " "
    if t in ("Code", "Math", "CodeBlock"):
        return c[-1]
    if t in ("RawInline", "RawBlock"):
        return ""
    if t == "Quoted":  # keep the marks: the similarity check leaves quoted text out
        return f"“{_text(c[1])}”"
    if t == "BlockQuote":
        return f"“{_text(c).strip()}”\n"
    s = _text(c)
    return s + "\n" if t in BLOCKS else s


def structure_from_ast(doc: dict) -> dict:
    meta = doc.get("meta", {})
    sections, cur = [], {"heading": None, "level": 0, "text": ""}
    for b in doc.get("blocks", []):
        if b.get("t") == "Header":
            if cur["heading"] or cur["text"].strip():
                sections.append(cur)
            cur = {"heading": _text(b["c"][2]).strip(), "level": b["c"][0], "text": ""}
        else:
            cur["text"] += _text(b)
    if cur["heading"] or cur["text"].strip():
        sections.append(cur)
    author = meta.get("author")
    authors = author["c"] if author and author.get("t") == "MetaList" else ([author] if author else [])
    return {
        "authors": [a for a in (_text(x).strip() for x in authors) if a],
        "title": _text(meta.get("title")).strip() or None,
        "abstract": _text(meta.get("abstract")).strip() or None,
        "keywords": _text(meta.get("keywords")).strip() or None,
        "sections": sections,
    }


SMALL_CAPS = re.compile(r"\b([A-Z]) (?=[A-Z]{2,})")  # pypdf splits IEEE small caps: "I NTRODUCTION"
PDF_HEADING = re.compile(r"([IVX]{1,6}|\d{1,2})\.\s+([A-Z].{1,80})")
PDF_UNNUMBERED = re.compile(r"(?:references|bibliography|acknowledge?ments?)", re.I)
ROMAN = {r: i for i, r in enumerate("I II III IV V VI VII VIII IX X XI XII XIII XIV XV".split(), 1)}
PDF_ABSTRACT = re.compile(r"^[ \t]*abstract\b[ \t]*[—–\-:.]?[ \t]*", re.I | re.M)
PDF_ABSTRACT_END = re.compile(r"^[ \t]*(?:index\s+terms|keywords|key\s*words)\b", re.I | re.M)


def _unwrap_lines(s: str) -> str:
    return re.sub(r"\s*\n\s*", " ", re.sub(r"(\w)-\n(\w)", r"\1\2", s)).strip()


def structure_from_pdf(pdf: Path) -> dict | None:
    """Fallback when pandoc cannot parse the source: headings from the compiled PDF's text. Numbered headings
    must count up from 1 (I, II, ... or 1, 2, ... in capitals; gaps of up to two), so numbered list items in the text are not
    taken for headings. Returns None unless at least two headings are found."""
    reader = PdfReader(pdf)
    lines = "\n".join((pg.extract_text() or "") for pg in reader.pages[:200]).splitlines()
    sections, cur, want = [], {"heading": None, "level": 1, "text": ""}, 1
    for ln in lines:
        flat = SMALL_CAPS.sub(r"\1", ln.strip())
        m = PDF_HEADING.fullmatch(flat)
        num = m and (ROMAN.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() and m.group(2).isupper()
                                                else None))
        step = bool(num) and want <= num <= want + 2  # a heading wrapped over two lines is missed; allow a gap
        if step or PDF_UNNUMBERED.fullmatch(flat):
            sections.append(cur)
            cur = {"heading": m.group(2).strip() if step else flat, "level": 1, "text": ""}
            want = num + 1 if step else want
        else:
            cur["text"] += ln + "\n"
    sections.append(cur)
    if sum(1 for s in sections if s["heading"]) < 2:
        return None
    front = sections[0]["text"]
    abstract = None
    if m := PDF_ABSTRACT.search(front):
        end = PDF_ABSTRACT_END.search(front, m.end())
        abstract = _unwrap_lines(front[m.end():end.start() if end else None]) or None
    for s in sections[1:]:
        s["text"] = _unwrap_lines(s["text"])
    title = next((ln.strip() for ln in lines if ln.strip()), None)
    return {"authors": [], "title": title, "abstract": abstract, "keywords": None, "sections": sections,
            "from_pdf": True}


def extract_structure(path: Path, fmt: str, root: Path):
    """Return (structure, error)."""
    pandoc = pypandoc.get_pandoc_path()
    if fmt == "latex":
        try:
            flat = flatten_latex(path, root)
        except IncludeLimit as e:
            return None, str(e)
        ok, out = _run([pandoc, "--sandbox", "-f", "latex", "-t", "json"], input_text=flat)
    else:
        ok, out = _run([pandoc, "--sandbox", "-f", fmt, "-t", "json", str(path)])
    if not ok:
        return None, out
    try:
        return structure_from_ast(json.loads(out)), None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        return None, f"unexpected pandoc output: {e}"


@functools.cache
def _which(tool):
    return shutil.which(tool)  # a PATH scan per paper cost ~17 s per 300 papers on Windows


def _unavailable(tool, what):
    return ("converter_unavailable", "warn", f"{tool} is not installed here, so {what} was not produced.",
            {"tool": tool})


def soffice_convert(src: Path, outdir: Path, target: str):
    """LibreOffice headless conversion. Returns (path, problem)."""
    exe = _which("soffice") or _which("libreoffice")
    if not exe:
        return None, _unavailable("LibreOffice", f"a {target.upper()}")
    with tempfile.TemporaryDirectory() as profile:  # private profile avoids lock clashes
        ok, log = _run([exe, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--norestore",
                        "--convert-to", target, "--outdir", str(outdir), str(src)])
    out = outdir / f"{src.stem}.{target}"
    if out.exists():  # soffice can exit 0 without output, so trust the file
        return out, None
    return None, ("office_convert_failed", "fail", f"LibreOffice could not convert the file to {target}.",
                  {"log_tail": log[-2000:]})


def _convert_eps(build: Path) -> None:
    """pdflatex can include .eps only by running repstopdf through shell escape, which stays off. Convert them
    beforehand to the file name pdflatex looks for (epstopdf runs Ghostscript with -dSAFER, no shell)."""
    if not _which("epstopdf"):
        return
    for eps in build.rglob("*.eps"):
        out = eps.with_name(f"{eps.stem}-eps-converted-to.pdf")
        if eps.is_file() and not eps.is_symlink() and not out.exists():
            _run(["epstopdf", f"--outfile=./{out.name}", f"./{eps.name}"], cwd=eps.parent)  # a failure shows up as LaTeX's


def compile_latex(root: Path, main: Path, outdir: Path):
    """latexmk in a throwaway copy, no shell escape, file access limited to the bundle."""
    if not _which("latexmk"):
        return None, _unavailable("LaTeX (latexmk)", "a PDF")
    rel = main.relative_to(root)
    env = {**os.environ, "openin_any": "p", "openout_any": "p", "shell_escape": "f"}
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / "src"
        shutil.copytree(root, build)
        cwd = build / rel.parent
        for p in list(build.rglob("*")):  # latexmkrc is Perl: a submission must never ship one we run
            if p.is_file() and (p.name.lower() in ("latexmkrc", ".latexmkrc") or p.suffix == ".fdb_latexmk"):
                p.unlink()
        (cwd / f"{rel.stem}.pdf").unlink(missing_ok=True)  # never accept a shipped "prebuilt" PDF
        _convert_eps(build)
        # a shipped .bbl is what the authors (and arXiv) compiled with; re-running BibTeX on a .bib with one
        # syntax error would fail the whole build. The reference check still reads the .bib and reports it.
        bib = ["-bibtex-"] if (cwd / f"{rel.stem}.bbl").is_file() else []
        ok, log = _run(["latexmk", "-norc", "-pdf", *bib, "-interaction=nonstopmode", "-halt-on-error",
                        "-no-shell-escape", "-file-line-error", rel.name], cwd=cwd, env=env)
        pdf = cwd / f"{rel.stem}.pdf"
        texlog = cwd / f"{rel.stem}.log"
        if ok and pdf.exists():
            if texlog.is_file():  # the reference check confirms "undefined citation" against what LaTeX saw
                (outdir / "latex.log").write_bytes(texlog.read_bytes()[-MAX_LOG_BYTES:])
            return shutil.copy2(pdf, outdir / "paper.pdf"), None
        lines = texlog.read_text(errors="replace").splitlines() if texlog.exists() else log.splitlines()
        errors = [ln for ln in lines if ln.startswith("!") or FILE_LINE_ERROR.match(ln) or "not found" in ln][:20]
        # a package the paper uses but this machine lacks is our problem, not the authors': never bounce it to them
        missing = sorted({m for ln in errors for m in MISSING_FILE.findall(ln) if not any(build.rglob(m))})
        if missing:
            return None, ("latex_package_missing", "fail",
                          f"This machine lacks {', '.join(missing)}; install it and re-run check (not an author error).",
                          {"missing": missing, "errors": errors, "log_tail": log[-2000:]})
        return None, ("latex_compile_failed", "fail", "LaTeX did not compile.",
                      {"errors": errors, "log_tail": log[-2000:]})


def convert_paper(con, paper_id: int, data_dir) -> str:
    """Convert one paper; returns its new status. Never raises: a crash becomes a signal."""
    try:
        return _convert(con, paper_id, data_dir)
    except Exception as e:
        con.rollback()
        con.execute("UPDATE paper SET status='needs_manual' WHERE id=?", (paper_id,))
        add_signal(con, start_run(con, paper_id, "conversion"), "convert_crashed", "fail",
                   "Conversion stopped with an unexpected error.", {"error": f"{type(e).__name__}: {e}"})
        con.commit()
        return "needs_manual"


def _convert(con, paper_id: int, data_dir) -> str:
    data_dir = Path(data_dir)
    row = con.execute("SELECT * FROM paper WHERE id=?", (paper_id,)).fetchone()
    src = data_dir / row["source_dir"]
    main = src / row["main_file"]
    derived = src.parent / "derived"
    derived.mkdir(exist_ok=True)
    problems = []

    text_input, fmt = main, {"latex": "latex"}.get(row["format"])
    if row["format"] == "word":
        kind = sniff(main)
        fmt = {"doc": None, "docx": "docx", "rtf": "rtf"}[kind]
        if kind == "doc":
            text_input, problem = soffice_convert(main, derived, "docx")
            fmt = "docx" if text_input else None
            if problem:
                problems.append(problem)

    struct = None
    if text_input and fmt:
        struct, err = extract_structure(text_input, fmt, src)
        if err:
            problems.append(("text_extraction_failed", "fail", "The text could not be extracted.",
                             {"log_tail": err[-2000:]}))

    if row["format"] == "latex":
        pdf, problem = compile_latex(src, main, derived)
    else:
        pdf, problem = soffice_convert(main, derived, "pdf")
    if problem:
        problems.append(problem)

    if struct is None and pdf:
        try:
            struct = structure_from_pdf(pdf)
        except Exception:  # noqa: BLE001  pypdf raises many types; the extraction failure is already recorded
            struct = None
        if struct:
            problems = [p for p in problems if p[0] != "text_extraction_failed"]
            problems.append(("text_from_pdf", "warn", "The source could not be parsed, so sections and text were "
                             "read from the compiled PDF; headings may be less exact.", {}))

    page_count = None
    if pdf:
        try:
            page_count = len(PdfReader(pdf).pages)
        except Exception as e:  # pypdf raises many types on malformed PDFs
            problems.append(("pdf_unreadable", "fail", "The produced PDF could not be read.", {"error": str(e)}))

    text_path = None
    if struct:
        text_path = derived / "text.txt"
        parts = [struct["title"], struct["abstract"]] + [
            f"{s['heading'] or ''}\n{s['text']}" for s in struct["sections"]]
        text_path.write_text("\n\n".join(p for p in parts if p), encoding="utf-8")

    status = "needs_manual" if any(p[0] != "text_from_pdf" for p in problems) else "converted"
    rel = lambda p: Path(p).relative_to(data_dir).as_posix() if p else None
    con.execute(
        "UPDATE paper SET title=COALESCE(title, ?), pdf_path=?, text_path=?, sections_json=?, page_count=?, status=? WHERE id=?",
        (struct and struct["title"], rel(pdf), rel(text_path), struct and json.dumps(struct), page_count,
         status, paper_id),
    )
    run_id = start_run(con, paper_id, "conversion")
    for code, severity, message, evidence in problems:
        add_signal(con, run_id, code, severity, message, evidence)
    audit(con, "system", "convert", "paper", paper_id, {"status": status, "pages": page_count})
    con.commit()
    return status
