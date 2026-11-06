"""Convert each submission to a common representation (title, abstract, sections, text) and a PDF.

External tools run as subprocesses with timeouts. A missing tool or failed conversion
becomes a signal and marks the paper "needs_manual"; nothing is dropped.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pypandoc
from pypdf import PdfReader

from .db import add_signal, audit
from .sniff import sniff

TIMEOUT = 180
BLOCKS = {"Para", "Plain", "Header", "CodeBlock", "BlockQuote", "OrderedList", "BulletList",
          "Table", "Div", "LineBlock", "DefinitionList", "Figure"}
INCLUDE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
COMMENT = re.compile(r"(?<!\\)%.*")


def _run(cmd, cwd=None, input_text=None, env=None):
    """Return (ok, output). Never raises for tool failures."""
    try:
        p = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env)
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"
    return p.returncode == 0, p.stdout if p.returncode == 0 else (p.stdout + p.stderr)[-4000:]


def flatten_latex(main: Path, root: Path, depth: int = 0) -> str:
    """Inline \\input/\\include, refusing anything outside the bundle (pandoc then runs sandboxed)."""
    text = COMMENT.sub("", main.read_text(encoding="utf-8", errors="replace"))
    if depth > 10:
        return text

    def repl(m):
        target = (main.parent / m.group(1).strip())
        if target.suffix != ".tex":
            target = target.with_name(target.name + ".tex")
        target = target.resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            return ""
        return flatten_latex(target, root, depth + 1)

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
    return {
        "title": _text(meta.get("title")).strip() or None,
        "abstract": _text(meta.get("abstract")).strip() or None,
        "keywords": _text(meta.get("keywords")).strip() or None,
        "sections": sections,
    }


def extract_structure(path: Path, fmt: str, root: Path):
    """Return (structure, error)."""
    pandoc = pypandoc.get_pandoc_path()
    if fmt == "latex":
        ok, out = _run([pandoc, "--sandbox", "-f", "latex", "-t", "json"], input_text=flatten_latex(path, root))
    else:
        ok, out = _run([pandoc, "--sandbox", "-f", fmt, "-t", "json", str(path)])
    if not ok:
        return None, out
    try:
        return structure_from_ast(json.loads(out)), None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        return None, f"unexpected pandoc output: {e}"


def _unavailable(tool, what):
    return ("converter_unavailable", "warn", f"{tool} is not installed here, so {what} was not produced.",
            {"tool": tool})


def soffice_convert(src: Path, outdir: Path, target: str):
    """LibreOffice headless conversion. Returns (path, problem)."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
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


def compile_latex(root: Path, main: Path, outdir: Path):
    """latexmk in a throwaway copy, no shell escape, file access limited to the bundle."""
    if not shutil.which("latexmk"):
        return None, _unavailable("LaTeX (latexmk)", "a PDF")
    rel = main.relative_to(root)
    env = {**os.environ, "openin_any": "p", "openout_any": "p", "shell_escape": "f"}
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp) / "src"
        shutil.copytree(root, build)
        cwd = build / rel.parent
        ok, log = _run(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
                        "-file-line-error", rel.name], cwd=cwd, env=env)
        pdf = cwd / f"{rel.stem}.pdf"
        if ok and pdf.exists():
            return shutil.copy2(pdf, outdir / "paper.pdf"), None
        texlog = cwd / f"{rel.stem}.log"
        lines = texlog.read_text(errors="replace").splitlines() if texlog.exists() else log.splitlines()
        errors = [ln for ln in lines if ln.startswith("!") or ":error:" in ln or "not found" in ln][:20]
        return None, ("latex_compile_failed", "fail", "LaTeX did not compile.",
                      {"errors": errors, "log_tail": log[-2000:]})


def convert_paper(con, paper_id: int, data_dir) -> str:
    """Convert one paper; returns its new status."""
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

    status = "needs_manual" if problems else "converted"
    rel = lambda p: Path(p).relative_to(data_dir).as_posix() if p else None  # noqa: E731
    con.execute(
        "UPDATE paper SET title=?, pdf_path=?, text_path=?, sections_json=?, page_count=?, status=? WHERE id=?",
        (struct and struct["title"], rel(pdf), rel(text_path), struct and json.dumps(struct), page_count,
         status, paper_id),
    )
    for code, severity, message, evidence in problems:
        add_signal(con, paper_id, "conversion", code, severity, message, evidence)
    audit(con, "system", "convert", "paper", paper_id, {"status": status, "pages": page_count})
    con.commit()
    return status
