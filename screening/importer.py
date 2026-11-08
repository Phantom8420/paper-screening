"""Read a submission system's export (EasyChair, HotCRP, or any CSV with a paper-number column) and attach
titles and authors to the papers in a batch. Authors are what the anonymity check looks for in the text and
what the conflict-of-interest view groups by, so a batch is only fully checked once this has run."""
import csv
import json
import re
from pathlib import Path

from .db import audit

ID = {"#", "submission#", "submission", "submissionid", "submissionnumber", "paper", "paperid", "paper#", "pid",
      "id", "number"}
TITLE = {"title", "papertitle", "submissiontitle"}
FIRST = {"firstname", "first", "givenname", "given"}
LAST = {"lastname", "last", "familyname", "surname", "family"}
NAME = {"name", "author", "authorname", "fullname"}
AUTHORS = {"authors", "authorlist", "authornames"}
EMAIL = {"email", "emailaddress", "mail"}
ORCID = {"orcid", "orcidid"}  # "ORCID iD" after _key
AFFIL = {"affiliation", "organization", "organisation", "institution", "company"}
LAST_NUMBER = re.compile(r"(\d+)(?!.*\d)")


def _key(h) -> str:
    return re.sub(r"[\s_\-.]", "", str(h or "").lower())


def _pick(row: dict, names: set) -> str:
    return next((str(v).strip() for k, v in row.items() if k in names and v not in (None, "")), "")


def _rows_csv(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
    return [{_key(k): v for k, v in r.items()} for r in csv.DictReader(text.splitlines(), dialect=dialect)]


def _rows_xlsx(path: Path) -> dict[str, list[dict]]:
    try:
        import openpyxl
    except ImportError as e:
        raise SystemExit("reading .xlsx needs openpyxl: pip install openpyxl, or save the sheet as CSV") from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if rows:
            head = [_key(h) for h in rows[0]]
            out[ws.title.lower()] = [dict(zip(head, r)) for r in rows[1:] if any(c not in (None, "") for c in r)]
    return out


def _from_rows(rows: list[dict], into: dict) -> None:
    """One row per paper (with an authors column) or one row per author (first/last/email); both merge."""
    for r in rows:
        sid = _pick(r, ID)
        if not (m := LAST_NUMBER.search(sid)):
            continue
        p = into.setdefault(int(m.group(1)), {"title": "", "authors": []})
        p["title"] = p["title"] or _pick(r, TITLE)
        name = _pick(r, NAME) or " ".join(x for x in (_pick(r, FIRST), _pick(r, LAST)) if x)
        if name:
            p["authors"].append({"name": name, "email": _pick(r, EMAIL), "affiliation": _pick(r, AFFIL),
                                 "orcid": _pick(r, ORCID)})
        elif joined := _pick(r, AUTHORS):  # EasyChair's Submissions sheet: "Ann Lee, Bo Chen and Cy Diaz"
            p.setdefault("listed", [n.strip() for n in re.split(r",|;|\band\b", joined) if n.strip()])


def _from_json(data, into: dict) -> None:
    """HotCRP's JSON export: [{pid, title, authors: [{first, last, email, affiliation}]}]."""
    for rec in data if isinstance(data, list) else data.get("papers", []):
        if not isinstance(rec, dict) or not str(rec.get("pid", rec.get("id", ""))).isdigit():
            continue
        p = into.setdefault(int(rec.get("pid", rec.get("id"))), {"title": "", "authors": []})
        p["title"] = p["title"] or (rec.get("title") or "").strip()
        for a in rec.get("authors") or []:
            a = {_key(k): v for k, v in a.items()} if isinstance(a, dict) else {"name": str(a)}
            name = _pick(a, NAME) or " ".join(x for x in (_pick(a, FIRST), _pick(a, LAST)) if x)
            if name:
                p["authors"].append({"name": name, "email": _pick(a, EMAIL), "affiliation": _pick(a, AFFIL),
                                     "orcid": _pick(a, ORCID)})


def read_export(path: Path) -> dict[int, dict]:
    """{submission number: {"title", "authors": [{name, email, affiliation, orcid}]}}"""
    path = Path(path)
    out: dict[int, dict] = {}
    suffix = path.suffix.lower()
    if suffix == ".json":
        _from_json(json.loads(path.read_text(encoding="utf-8-sig")), out)
    elif suffix in (".xlsx", ".xlsm"):
        for sheet in sorted(_rows_xlsx(path).items(), key=lambda kv: kv[0] != "authors"):  # authors first
            _from_rows(sheet[1], out)
    else:
        _from_rows(_rows_csv(path), out)
    for p in out.values():  # only a paper list with no author rows: fall back to the joined names
        if not p["authors"]:
            p["authors"] = [{"name": n, "email": "", "affiliation": ""} for n in p.pop("listed", [])]
        p.pop("listed", None)
    return out


def import_export(con, path: Path, batch_id=None, actor="system") -> dict:
    """Match by the last number in each paper's file or folder name (EasyChair's 123.pdf, HotCRP's
    conf-paper123.pdf, paper_123/). Re-importing replaces a paper's authors; the audit log keeps the history."""
    meta = read_export(path)
    q = "SELECT id, external_id FROM paper" + (" WHERE batch_id=?" if batch_id else "")
    papers = con.execute(q, (batch_id,) if batch_id else ()).fetchall()
    matched, unmatched = [], []
    for pid, ext in papers:
        stem = Path(ext or "").stem
        m = LAST_NUMBER.search(stem)
        rec = meta.get(int(m.group(1))) if m else None
        if not rec:
            unmatched.append(ext)
            continue
        if rec["title"]:
            con.execute("UPDATE paper SET title=? WHERE id=?", (rec["title"], pid))
        con.execute("DELETE FROM author WHERE paper_id=?", (pid,))
        con.executemany("INSERT INTO author(paper_id, position, name, email, affiliation, orcid) VALUES(?,?,?,?,?,?)",
                        [(pid, i, a["name"], a["email"] or None, a["affiliation"] or None, a.get("orcid") or None)
                         for i, a in enumerate(rec["authors"], 1)])
        matched.append(pid)
    result = {"file": Path(path).name, "records": len(meta), "matched": matched, "unmatched": unmatched}
    audit(con, actor, "import_export", "batch", batch_id, result)
    con.commit()
    return result
