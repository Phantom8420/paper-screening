"""M3 reference verification.

The engine is paper-preflight (MIT): it parses .bib/.bbl/thebibliography and Word reference
lists, checks citations against entries, and looks each reference up in Crossref, dblp, arXiv,
DataCite, OpenAlex, doi.org and PubMed, including retraction notices. Only reference metadata
leaves the machine. This module decides what it may read, groups its findings into signals,
and adds the one thing it does not do: numbered [n] citations in Word files.

Swapping engines means replacing check_with_preflight with a function that returns the same
(code, severity, message, evidence, score) findings.
"""
import contextvars
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import paper_preflight
from paper_preflight.tex import project as _pp_project

from .compliance import SECTION_PATTERNS, _f, _norm, load_rules
from .db import add_signal, audit, start_run
from .sniff import sniff

VERSION = f"2+preflight-{paper_preflight.__version__}"
SEVERITY = {"error": "fail", "warning": "warn", "info": "info"}
RANK = {"info": 0, "warn": 1, "fail": 2}
MAX_EVIDENCE = 200

# (signal code, preflight rules, message, severity override)
GROUPS = [
    ("reference_undefined", {"CIT001", "CIT005"}, "Citations point to references that do not exist.", None),
    ("reference_not_found", {"REF001", "REF002", "REF003", "REF010"},
     "References were not found in any scholarly index, or their identifiers point to other works.", None),
    ("reference_retracted", {"REF004", "REF018"}, "Cited work has been retracted or withdrawn.", None),
    ("reference_notice", {"REF005"}, "Cited work has a correction or an expression of concern.", None),
    ("reference_mismatch", {"REF011", "REF012", "REF013", "REF014", "REF015", "REF020"},
     "Reference details differ from the scholarly record.", None),
    ("reference_uncited", {"CIT003"}, "References are listed but never cited.", None),
    # preflight calls these info ("no false accusations"); an unverifiable reference still needs a person
    ("reference_unverified", {"REF090"}, "References could not be verified automatically; check them by hand.",
     "warn"),
    ("reference_check_incomplete", {"RUN001"}, "Some sources did not answer, so results may change on a re-run.",
     "warn"),
]
HYGIENE = ("reference_hygiene", "Bibliography housekeeping issues.")

# --- confinement ------------------------------------------------------------------------
# preflight resolves \input, \import, \bibliography, \addglobalbib ... relative to the bundle but
# never checks the result stays inside it. Rather than re-parse LaTeX to guess what it will follow,
# wrap the two functions that turn names into paths. Pinned to paper-preflight==1.0.0.
COPY_SUFFIXES = {".tex", ".bib", ".bbl"}  # no .aux/.bcf: a forged one would decide what counts as cited
MAX_COPY_BYTES = 50 * 2**20
MAX_COPY_FILES = 2000
MAX_DOCX_BYTES = 100 * 2**20  # uncompressed; preflight reads every part into memory
_SANDBOX = contextvars.ContextVar("reference_sandbox", default=None)
_orig_resolve_tex, _orig_add_bib = _pp_project._resolve_tex, _pp_project._add_bib


def _inside(path, record=True) -> bool:
    box = _SANDBOX.get()
    if box is None:
        return True
    root, escapes = box
    if Path(path).resolve().is_relative_to(root):
        return True
    if record:
        escapes.append(str(path))
    return False


def _confined_resolve_tex(bases, name):
    clean = name.strip().strip('"')  # as preflight cleans it
    # flag the attempt even when nothing exists there (inside the sandbox copy most escapes find nothing);
    # one inside candidate is enough: \import also tries its directory relative to the process cwd
    if clean and not any(_inside(Path(b) / clean, record=False) for b in bases):
        _inside(Path(bases[0]) / clean)
        return None
    hit = _orig_resolve_tex(bases, name)
    return hit if hit is None or _inside(hit) else None


def _confined_add_bib(project, *args, **kwargs):
    n = len(project.bib_resources)
    _orig_add_bib(project, *args, **kwargs)
    project.bib_resources[n:] = [r for r in project.bib_resources[n:] if _inside(r.path)]


_pp_project._resolve_tex, _pp_project._add_bib = _confined_resolve_tex, _confined_add_bib


class TooLarge(Exception):
    pass


def sandbox_copy(src: Path, dest: Path) -> None:
    """Copy only the files preflight needs. Symlinks are skipped; size and count are capped."""
    total = files = 0
    for p in sorted(src.rglob("*")):
        if p.is_symlink() or not p.is_file() or p.suffix.lower() not in COPY_SUFFIXES:
            continue
        total, files = total + p.stat().st_size, files + 1
        if total > MAX_COPY_BYTES or files > MAX_COPY_FILES:
            raise TooLarge(f"more than {MAX_COPY_BYTES // 2**20} MB or {MAX_COPY_FILES} .tex/.bib/.bbl files")
        out = dest / p.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)


def docx_size(path: Path) -> int:
    # header sizes can lie, but zipfile stops reading at the declared size, so they bound memory
    with zipfile.ZipFile(path) as z:
        return sum(i.file_size for i in z.infolist())


# --- numbered citations (Word) ----------------------------------------------------------
REF_LABEL = re.compile(r"^[ \t]*\[(\d{1,3})\]")
# 1-3 digits: [2020] is a year; not after a word or ]: x[12] is an index
NUM_CITE = re.compile(r"(?<![\w\]])\[(\d{1,3}(?:[ \t]*[,–-][ \t]*\d{1,3}){0,50})\]")
JOINED_RANGE = re.compile(r"\][ \t]*[–-][ \t]*\[")  # [1]–[5] is the range 1..5


def numeric_citations(struct) -> dict | None:
    """[n] citations against a numbered reference list (Word). None when there is nothing to compare."""
    sections = struct.get("sections", [])
    refs = next((s for s in reversed(sections) if s.get("heading")
                 and re.fullmatch(SECTION_PATTERNS["references"], _norm(s["heading"]))), None)
    if refs is None:
        return None
    lines = [ln for ln in refs["text"].splitlines() if ln.strip()]
    labels = [int(m.group(1)) for ln in lines if (m := REF_LABEL.match(ln))]
    how = "typed [n] labels"
    if not labels:  # Word auto-numbering: pandoc gives one line per list item, without the number
        labels, how = list(range(1, len(lines) + 1)), "position in the list"
    body = JOINED_RANGE.sub("–", "\n".join(s["text"] for s in sections if s is not refs))
    cited = set()
    for m in NUM_CITE.finditer(body):
        for part in m.group(1).split(","):
            a, _, b = part.replace("–", "-").partition("-")
            lo, hi = int(a), int(b or a)
            if 1 <= lo <= hi <= lo + 100:  # [0], reversed or huge ranges are not citations
                cited.update(range(lo, hi + 1))
    if not cited:
        return None  # author-year or no numeric citations
    return {"undefined": sorted(cited - set(labels)), "uncited": sorted(set(labels) - cited), "numbered_by": how,
            "entries": len(labels)}


def _merge_numeric(out, num):
    # a heuristic over extracted text: warn, never fail on its own
    for code, nums, msg in (("reference_undefined", num["undefined"], GROUPS[0][2]),
                            ("reference_uncited", num["uncited"], GROUPS[5][2])):
        if not nums:
            continue
        items = [{"key": f"[{n}]", "rule": "numeric", "message": f"[{n}] ({num['numbered_by']})"} for n in nums]
        i = next((i for i, f in enumerate(out) if f[0] == code), None)
        if i is not None:
            _, sev, _, ev, score = out.pop(i)
            items, severity, count = ev["items"] + items, sev, score + len(items)
        else:
            severity, count = "warn", len(items)
        out.append(_f(code, max(severity, "warn", key=RANK.get), f"{msg} ({count})",
                      {"items": items[:MAX_EVIDENCE], "truncated": count > MAX_EVIDENCE}, count))


# --- preflight report -> signals --------------------------------------------------------

def _settings(tmp: Path, rules) -> Path:
    """Our own preflight settings, so a paper-preflight.toml inside a submission is never read."""
    path = tmp / "paper-preflight.toml"
    sources = ", ".join(json.dumps(s) for s in rules["references"]["disabled_sources"])
    path.write_text(f"disable-sources = [{sources}]\n", encoding="utf-8")
    return path


def _matched(ref):
    m = ref.matched if ref else None
    if m is None:
        return None
    return {"source": m.source, "id": m.id, "title": m.title, "year": m.year, "venue": m.venue, "doi": m.doi,
            "url": f"https://doi.org/{m.doi}" if m.doi else None}


def findings_from_report(report, root: Path | None = None) -> list:
    """Group preflight findings into one signal per kind; each lists every reference involved."""
    refs = {r.key: r for r in report.references}
    groups: dict[str, dict] = {}
    for f in report.findings:
        code, message, override = next(((c, msg, o) for c, rules, msg, o in GROUPS if f.rule in rules),
                                        (*HYGIENE, None))
        severity = override or SEVERITY.get(f.severity, "info")
        if code == "reference_uncited" and (f.file or "").lower().endswith(".bib"):
            severity = "info"  # BibTeX only prints cited entries, so an unused .bib entry is harmless
        file = f.file
        if file and root and Path(file).is_relative_to(root):
            file = Path(file).relative_to(root).as_posix()  # temp sandbox path means nothing to a reader
        g = groups.setdefault(code, {"message": message, "severity": "info", "items": []})
        g["severity"] = max(g["severity"], severity, key=RANK.get)
        g["items"].append({"key": f.key, "rule": f.rule, "message": f.message, "file": file, "line": f.line,
                           "matched": _matched(refs.get(f.key))})
    if not report.complete and "reference_check_incomplete" not in groups:
        groups["reference_check_incomplete"] = {"message": GROUPS[-1][2], "severity": "warn", "items": []}
    out = [_f(code, g["severity"], f"{g['message']} ({len(g['items'])})",
              {"items": g["items"][:MAX_EVIDENCE], "truncated": len(g["items"]) > MAX_EVIDENCE}, len(g["items"]))
           for code, g in groups.items()]
    verdicts = {}
    for r in report.references:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    entries = getattr(report.result, "entries", len(report.references))  # offline, references is empty
    if not entries:
        out.append(_f("references_missing", "warn", "No references were found, so none could be checked."))
    looked_up = f": {', '.join(f'{n} {v}' for v, n in sorted(verdicts.items()))}" if verdicts else ""
    out.append(_f("reference_summary", "info",
                  f"{entries} references, lookups {report.verification}{looked_up}.",
                  {"entries": entries, "cited_keys": getattr(report.result, "cited_keys", None),
                   "verdicts": verdicts, "verification": report.verification, "complete": report.complete},
                  entries))
    return out


def check_with_preflight(target: Path, main: Path | None, data_dir: Path, rules) -> list:
    """target is a bundle folder (with main) or a .docx. Never lets preflight read outside it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        try:
            if target.is_dir():
                box = tmp / "src"
                box.mkdir()
                sandbox_copy(target, box)
                target, main = box, box / main.relative_to(target)
            elif docx_size(target) > MAX_DOCX_BYTES:
                raise TooLarge(f"the .docx expands beyond {MAX_DOCX_BYTES // 2**20} MB")
        except TooLarge as e:
            return [_f("reference_input_too_large", "warn", "The bibliography input is too large to check "
                       "automatically; check the references by hand.", {"reason": str(e)})]
        escapes = []
        token = _SANDBOX.set((target.resolve() if target.is_dir() else target.parent.resolve(), escapes))
        try:
            report = paper_preflight.check_paper(
                target, main=main, online=rules["references"]["online"], settings=_settings(tmp, rules),
                cache=data_dir / "reference_cache.sqlite3")
        finally:
            _SANDBOX.reset(token)
        out = findings_from_report(report, target if target.is_dir() else None)
    if escapes:
        root = str(tmp)
        out.insert(0, _f("reference_paths_unsafe", "fail",
                         "The source points to files outside the submission; they were not read.",
                         {"paths": sorted({e.replace(root, "<submission>") for e in escapes})[:20]}, len(escapes)))
    return out


def _gather(row, data_dir: Path, rules) -> list:
    src = data_dir / row["source_dir"]
    main = src / row["main_file"]
    if row["format"] == "latex":
        return check_with_preflight(src, main, data_dir, rules)
    out = []
    docx = main if sniff(main) == "docx" else src.parent / "derived" / f"{main.stem}.docx"
    if docx.exists():
        out += check_with_preflight(docx, None, data_dir, rules)
    else:
        out.append(_f("reference_lookup_skipped", "warn",
                      "No readable .docx was available, so references were not looked up."))
    if row["sections_json"] and (num := numeric_citations(json.loads(row["sections_json"]))):
        _merge_numeric(out, num)
    return out


def run_references(con, paper_id: int, data_dir, rules=None) -> list:
    """Verify one paper's references and store the findings as one run. Never raises."""
    rules = rules or load_rules()
    row = con.execute("SELECT * FROM paper WHERE id=?", (paper_id,)).fetchone()
    if row["format"] == "unknown" or not row["source_dir"] or not row["main_file"]:
        return []
    try:
        findings = _gather(row, Path(data_dir), rules)
    except Exception as e:  # malformed bibliographies, network stacks, parser bugs: one signal, batch goes on
        findings = [_f("references_crashed", "fail", "Reference checks stopped with an unexpected error.",
                       {"error": f"{type(e).__name__}: {str(e)[:500]}"})]
    run_id = start_run(con, paper_id, "references", VERSION, rules["references"])
    for code, severity, message, evidence, score in findings:
        add_signal(con, run_id, code, severity, message, evidence, score)
    audit(con, "system", "references", "paper", paper_id,
          {"run_id": run_id, "fail": sum(f[1] == "fail" for f in findings),
           "warn": sum(f[1] == "warn" for f in findings)})
    con.commit()
    return findings
