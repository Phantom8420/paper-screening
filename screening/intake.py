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


def safe_extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsafeArchive(f"archive has {len(infos)} entries (limit {MAX_ENTRIES})")
        written = 0
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
                    written += len(chunk)
                    if written > MAX_TOTAL_BYTES:
                        raise UnsafeArchive(f"archive expands beyond {MAX_TOTAL_BYTES // 2**20} MB")
                    out.write(chunk)


def _children(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if not p.name.startswith((".", "__MACOSX", "~$")))


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
    return Unit(root, "latex", main, [(
        "multiple_main_tex", "warn", f"Several .tex files contain \\documentclass; using {main.name}.",
        {"candidates": [m.name for m in mains]})])


def discover(path: Path, work: Path, depth: int = 0) -> list[Unit]:
    """Turn one input (file, folder or zip) into submission units."""
    if path.is_dir():
        kids = _children(path)
        texs = [p for p in kids if p.is_file() and p.suffix.lower() == ".tex"]
        if texs:  # shortcut: loose .tex files in one folder are one bundle, upgrade if batches mix them
            return [_latex_unit(path, texs)]
        return [u for k in kids for u in discover(k, work, depth)]

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
            safe_extract(path, dest)
        except (UnsafeArchive, zipfile.BadZipFile, OSError) as e:
            return [Unit(path, "unknown", None, notes + [(
                "bad_archive", "fail", f"Archive could not be unpacked: {e}", {"file": path.name})])]
        root = _unwrap(dest)
        units = discover(root, work, depth + 1)
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
    bdir = data_dir / "batches" / str(batch_id)
    (bdir / "inputs").mkdir(parents=True)
    for i, p in enumerate(inputs):  # originals, untouched
        dst = bdir / "inputs" / f"{i}_{p.name}"
        shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)

    paper_ids = []
    work_root = data_dir / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root) as work:
        units = [u for p in inputs for u in discover(p, Path(work))]
        for u in units:
            failed = u.kind == "unknown" or any(n[1] == "fail" for n in u.notes)
            pid = con.execute(
                "INSERT INTO paper(batch_id, external_id, format, status, created_at) VALUES(?,?,?,?,?)",
                (batch_id, u.label, u.kind, "needs_manual" if failed else "ingested", now()),
            ).lastrowid
            src = bdir / "papers" / str(pid) / "source"
            if u.root.is_dir():
                shutil.copytree(u.root, src)
                main = u.main.relative_to(u.root).as_posix() if u.main else None
            else:
                src.mkdir(parents=True)
                shutil.copy2(u.root, src)
                main = u.main.name if u.main else None
            con.execute("UPDATE paper SET source_dir=?, main_file=? WHERE id=?",
                        (src.relative_to(data_dir).as_posix(), main, pid))
            for code, severity, message, evidence in u.notes:
                add_signal(con, pid, "intake", code, severity, message, evidence)
            paper_ids.append(pid)
    audit(con, actor, "ingest", "batch", batch_id, {"inputs": [p.name for p in inputs], "papers": paper_ids})
    con.commit()
    return batch_id, paper_ids
