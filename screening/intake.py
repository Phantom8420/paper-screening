"""Bulk intake: unpack safely, split into submissions, route to the latex / word / unknown queue."""
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .db import add_signal, audit, now
from .sniff import EXT_KIND, sniff

MAX_ENTRIES = 10_000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_DEPTH = 3
PREFERRED_MAIN = ("main.tex", "paper.tex", "manuscript.tex", "article.tex")
DOCUMENTCLASS = re.compile(rb"^[^%\n]*\\documentclass", re.M)


class UnsafeArchive(Exception):
    pass


@dataclass
class Unit:
    root: Path  # folder (LaTeX bundle) or single file
    kind: str  # latex | word | unknown
    main: Path | None
    notes: list = field(default_factory=list)  # (code, severity, message, evidence)
    label: str = ""

    def __post_init__(self):
        self.label = self.label or self.root.name


def new_budget() -> dict:
    return {"bytes": MAX_TOTAL_BYTES}


def safe_extract(zip_path: Path, dest: Path, budget: dict | None = None) -> None:
    """budget is shared across nested archives so a zip of zip bombs is still capped."""
    budget = budget if budget is not None else new_budget()
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsafeArchive(f"archive has {len(infos)} entries (limit {MAX_ENTRIES})")
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or ":" in name:
                raise UnsafeArchive(f"unsafe path in archive: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UnsafeArchive(f"symlink in archive: {info.filename}")
            if parts and parts[0] == "__MACOSX":
                continue
            target = dest / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                # count real bytes: header sizes can lie (zip bombs)
                while chunk := src.read(1 << 20):
                    budget["bytes"] -= len(chunk)
                    if budget["bytes"] < 0:
                        raise UnsafeArchive(f"archive expands beyond {MAX_TOTAL_BYTES // 2**20} MB")
                    out.write(chunk)


JUNK = {".DS_Store", "__MACOSX", "Thumbs.db", "desktop.ini", ".git"}
WORD_EXT = (".docx", ".doc", ".rtf")


def _children(d: Path) -> list[Path]:
    # ~$ files are Word lock stubs (~160 bytes, no content), never submissions
    return sorted(p for p in d.iterdir() if p.name not in JUNK and not p.name.startswith("~$"))


def _unwrap(d: Path) -> Path:
    kids = _children(d)
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else d


def _is_main(p: Path) -> bool:
    return bool(DOCUMENTCLASS.search(p.read_bytes()[:200_000]))


def _latex_unit(root: Path, texs: list[Path]) -> Unit:
    mains = [p for p in texs if _is_main(p)]
    if not mains:
        return Unit(root, "latex", None, [(
            "no_main_tex", "fail", "No .tex file with \\documentclass was found.",
            {"tex_files": [p.name for p in texs]})])
    if len(mains) == 1:
        return Unit(root, "latex", mains[0])
    preferred = [m for m in mains if m.name.lower() in PREFERRED_MAIN]
    main = (preferred or mains)[0]
    # fail, not warn: this may be two papers in one folder, so a person must look
    return Unit(root, "latex", main, [(
        "multiple_main_tex", "fail", f"Several .tex files contain \\documentclass; using {main.name}.",
        {"candidates": [m.name for m in mains]})])


def discover(path: Path, work: Path, depth: int = 0, budget: dict | None = None) -> list[Unit]:
    """Turn one input (file, folder or zip) into submission units."""
    budget = budget if budget is not None else new_budget()
    if path.is_dir():
        kids = _children(path)
        texs = [p for p in kids if p.is_file() and p.suffix.lower() == ".tex"]
        if texs:  # a folder with top-level .tex is one bundle; Word files beside it are their own papers
            words = [k for k in kids if k.is_file() and k.suffix.lower() in WORD_EXT]
            return [_latex_unit(path, texs)] + [u for w in words for u in discover(w, work, depth, budget)]
        return [u for k in kids for u in discover(k, work, depth, budget)]

    kind = sniff(path)
    ext = EXT_KIND.get(path.suffix.lower())
    notes = []
    if ext and ext != kind and kind not in ("corrupt", "unknown"):
        notes.append(("extension_mismatch", "warn",
                      f"{path.name} has a {path.suffix} extension but its content is {kind}.",
                      {"file": path.name, "extension": ext, "content": kind}))

    if kind == "zip":
        if depth >= MAX_DEPTH:
            return [Unit(path, "unknown", None, notes + [(
                "archive_too_deep", "fail", "Archive nested too deeply to unpack.", {"file": path.name})])]
        dest = Path(tempfile.mkdtemp(dir=work))
        try:
            safe_extract(path, dest, budget)
        except Exception as e:  # encrypted, corrupt deflate, unsupported method, bombs: all one outcome
            shutil.rmtree(dest, ignore_errors=True)
            return [Unit(path, "unknown", None, notes + [(
                "bad_archive", "fail", f"Archive could not be unpacked: {type(e).__name__}: {e}",
                {"file": path.name})])]
        root = _unwrap(dest)
        units = discover(root, work, depth + 1, budget)
        if not units:
            return [Unit(path, "unknown", None, notes + [(
                "empty_archive", "fail", "Archive contains no usable files.", {"file": path.name})])]
        for u in units:
            u.notes = notes + u.notes
            if u.root == root and root == dest:
                u.label = path.name
        return units
    if kind in ("docx", "doc", "rtf"):
        return [Unit(path, "word", path, notes)]
    if kind == "tex":
        return [_latex_unit(path, [path])]
    if kind == "pdf":
        msg = "Only a PDF was supplied; LaTeX or Word source is required."
        return [Unit(path, "unknown", None, notes + [("source_missing", "fail", msg, {"file": path.name})])]
    if kind == "corrupt":
        msg = f"{path.name} looks damaged and could not be opened."
        return [Unit(path, "unknown", None, notes + [("corrupt_file", "fail", msg, {"file": path.name})])]
    msg = f"Unsupported file type: {path.name}"
    return [Unit(path, "unknown", None, notes + [("unsupported_file", "fail", msg, {"file": path.name})])]


def ingest(con, inputs, data_dir, name=None, actor="system") -> tuple[int, list[int]]:
    """Store originals, create one paper row per unit. Returns (batch_id, paper_ids)."""
    data_dir = Path(data_dir)
    inputs = [Path(p) for p in inputs]
    batch_id = con.execute("INSERT INTO batch(name, created_at) VALUES(?,?)", (name or now(), now())).lastrowid
    con.commit()  # AUTOINCREMENT + early commit: a later crash cannot reuse this id or its folder
    bdir = data_dir / "batches" / str(batch_id)
    (bdir / "inputs").mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(inputs):  # originals, untouched
        dst = bdir / "inputs" / f"{i}_{p.name}"
        shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)

    paper_ids = []
    work_root = data_dir / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    budget = new_budget()
    with tempfile.TemporaryDirectory(dir=work_root) as work:
        units = []
        for p in inputs:
            try:
                found = discover(p, Path(work), budget=budget)
            except Exception as e:
                found = [Unit(p, "unknown", None, [("intake_error", "fail", f"Could not read input: {e}",
                                                     {"input": p.name, "error": type(e).__name__})])]
            units += found or [Unit(p, "unknown", None, [(
                "empty_input", "fail", f"{p.name} contains no files to screen.", {"input": p.name})])]
        for u in units:
            paper_ids.append(_store_unit(con, u, batch_id, bdir, data_dir))
    audit(con, actor, "ingest", "batch", batch_id, {"inputs": [p.name for p in inputs], "papers": paper_ids})
    con.commit()
    return batch_id, paper_ids


def _store_unit(con, u: Unit, batch_id: int, bdir: Path, data_dir: Path) -> int:
    failed = u.kind == "unknown" or any(n[1] == "fail" for n in u.notes)
    pid = con.execute(
        "INSERT INTO paper(batch_id, external_id, format, status, created_at) VALUES(?,?,?,?,?)",
        (batch_id, u.label, u.kind, "needs_manual" if failed else "ingested", now()),
    ).lastrowid
    src = bdir / "papers" / str(pid) / "source"
    notes = list(u.notes)
    try:
        if u.root.is_dir():
            shutil.copytree(u.root, src)
            main = u.main.relative_to(u.root).as_posix() if u.main else None
        else:
            src.mkdir(parents=True)
            shutil.copy2(u.root, src)
            main = u.main.name if u.main else None
        con.execute("UPDATE paper SET source_dir=?, main_file=? WHERE id=?",
                    (src.relative_to(data_dir).as_posix(), main, pid))
    except Exception as e:  # e.g. Windows path-length or reserved names inside a bundle
        con.execute("UPDATE paper SET status='needs_manual' WHERE id=?", (pid,))
        notes.append(("store_failed", "fail", f"Files could not be stored: {e}", {"error": type(e).__name__}))
    for code, severity, message, evidence in notes:
        add_signal(con, pid, "intake", code, severity, message, evidence)
    return pid
