"""Author pre-check: the same checks the chairs run, on one paper, before it is submitted.

Nothing is kept. The upload lives in a temporary folder that is deleted as soon as the checklist is built,
and no reference metadata leaves the machine unless the rules switch online lookups on.
"""
import tempfile
from pathlib import Path

from .db import connect
from .intake import ingest
from .pipeline import process
from .review import FIX_HINTS, evidence, flags

ACCEPTED = (".zip", ".tex", ".docx", ".doc", ".rtf")
MAX_UPLOAD = 50 * 2**20
# the chair's own bookkeeping, not something an author can act on before submitting
CHAIR_ONLY = {"possible_duplicate", "duplicates_crashed", "reference_summary"}


def precheck(data: bytes, filename: str, rules) -> dict:
    """{"title", "pages", "items": [{code, severity, message, evidence, hint}], "passed": [message]}"""
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED:
        raise ValueError(f"Upload a .zip of your LaTeX sources, a .tex file or a Word file ({', '.join(ACCEPTED)}).")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"The file is larger than {MAX_UPLOAD // 2**20} MB.")
    with tempfile.TemporaryDirectory(prefix="precheck-") as tmp:
        tmp = Path(tmp)
        upload = tmp / "upload" / f"paper{suffix}"
        upload.parent.mkdir()
        upload.write_bytes(data)
        store = tmp / "data"
        rules = {**rules, "references": {**rules["references"], "online": False}}  # an unsubmitted paper stays here
        con = connect(store / "screening.db")
        try:
            _, ids = ingest(con, [upload], store, "pre-check", actor="author")
            process(store, ids, rules, jobs=1)
            pid = ids[0]
            p = con.execute("SELECT title, page_count FROM paper WHERE id=?", (pid,)).fetchone()
            open_, _ = flags(con, pid)
            passed = [s["message"] for s in con.execute(
                "SELECT code, message FROM signal WHERE paper_id=? AND severity='info' ORDER BY id", (pid,))
                if s["code"].endswith("_ok") and s["code"] != "duplicates_ok"]
        finally:
            con.close()
    items = [{"code": s["code"], "severity": s["severity"], "message": s["message"], "evidence": evidence(s),
              "hint": FIX_HINTS.get(s["code"])} for s in open_ if s["code"] not in CHAIR_ONLY]
    items.sort(key=lambda i: i["severity"] != "fail")
    return {"title": p["title"], "pages": p["page_count"], "items": items, "passed": passed}
