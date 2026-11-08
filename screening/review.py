"""Chair workflow on top of the signals: dismissals that survive re-runs, fix hints, author notices, CSV export.

A dismissal is an append-only 'override' decision tied to the flag's evidence. If a re-run produces the same
evidence the flag stays dismissed; if the evidence changes (a new email address, another page over the limit)
it comes back. Restoring a flag is another decision, so every step is reversible and in the audit log.
"""
import csv
import hashlib
import json

from .db import audit, current_signals, now

# What the authors should do. Only flags listed here go into an author notice; the rest (tool gaps, crashes,
# lookups we could not finish) are the organizers' business.
FIX_HINTS = {
    "page_limit_exceeded": "Shorten the paper to the page limit.",
    "body_page_limit_exceeded": "Shorten the main text to the page limit; only references may use the extra pages.",
    "paper_size": "Export the PDF at the required paper size (US Letter for IEEE).",
    "missing_sections": "Add the required sections.",
    "missing_abstract": "Add an abstract.",
    "abstract_too_long": "Shorten the abstract to the word limit.",
    "missing_keywords": "Add a keywords (Index Terms) line after the abstract.",
    "author_block_present": "Remove author names and affiliations for double-blind review.",
    "affiliation_in_front_matter": "Remove the author block (names, emails, affiliations) above the abstract.",
    "email_in_text": "Remove email addresses from the paper.",
    "known_author_named": "Remove author names from the text, or cite your own work in the third person.",
    "self_reference": "Cite your own earlier work in the third person (\"Smith et al. [3] showed\").",
    "identifying_link": "Replace code or profile links with an anonymized one (e.g. anonymous.4open.science).",
    "acknowledgments_present": "Remove the acknowledgments until the camera-ready version.",
    "file_metadata_identifies": "Clear author names from the file properties (Word: File > Info > Inspect "
                                "Document; LaTeX: \\hypersetup{pdfauthor={}}).",
    "tracked_changes_or_comments": "Accept or reject all tracked changes and delete comments before submitting.",
    "wrong_template": "Use the conference template.",
    "layout_modified": "Restore the template's margins and spacing.",
    "negative_vspace": "Remove negative \\vspace / \\vskip that squeeze the layout.",
    "font_mismatch": "Use the template's fonts.",
    "template_options": "Use the template's document class options.",
    "latex_compile_failed": "Make sure the sources compile with pdflatex and include every figure and .bib file.",
    "no_main_tex": "Include the main .tex file in the upload.",
    "multiple_main_tex": "Upload only one paper per submission, or remove unused .tex files with \\documentclass.",
    "corrupt_file": "Upload the file again; the one received cannot be opened.",
    "empty_input": "Upload the paper; the file received is empty.",
    "unsupported_file": "Upload a .tex bundle (zip), .docx or .pdf.",
    "reference_undefined": "Add the missing bibliography entries for these citations.",
    "reference_not_found": "Check these references; they could not be found in any index (typo, wrong DOI or "
                           "invented reference).",
    "reference_retracted": "Remove or justify citations of retracted work.",
    "reference_mismatch": "Correct these references' details (title, authors, year or DOI disagree with the "
                          "published record).",
    "reference_uncited": "Cite these references in the text or remove them.",
    "little_text": "Upload the paper as an editable Word or LaTeX file, not scanned pages or images.",
    "review_leftover": "Replace the anonymous review header with the final author block.",
    "author_list_mismatch": "Make the author list on the paper match the one in the submission system.",
    "author_block_missing": "Add the author names and affiliations to the final version.",
    "copyright_notice_missing": "Add the IEEE copyright notice from your copyright form to the first page.",
    "fonts_not_embedded": "Embed all fonts (LaTeX: pdflatex embeds by default; Word: File > Options > Save > "
                          "Embed fonts, or print to PDF with IEEE PDF eXpress).",
    "no_code_data_statement": "Say where the code and data can be found (a repository link, or why they cannot "
                              "be shared).",
    "orcid_invalid": "Correct the ORCID iD in the submission system (16 digits, from orcid.org).",
}
RISK = {"fail": 2, "warn": 1, "info": 0}


def evidence(signal) -> dict:
    try:
        ev = json.loads(signal["evidence_json"] or "{}")
    except ValueError:
        return {}
    return ev if isinstance(ev, dict) else {}


def _key(sigs) -> str:
    """One hash over every current signal with this code, so a code raised twice is dismissed as one."""
    blob = json.dumps(sorted(json.dumps([s["check_name"], evidence(s)], sort_keys=True) for s in sigs))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _dismissals(con, paper_id) -> dict:
    """code -> latest dismiss/restore value for this paper."""
    latest = {}
    for d in con.execute("SELECT value FROM decision WHERE paper_id=? AND kind='override' ORDER BY id", (paper_id,)):
        try:
            v = json.loads(d["value"])
            latest[v["code"]] = v
        except (ValueError, KeyError, TypeError):  # an override written by something else; not a dismissal
            continue
    return latest


def _by_code(con, paper_id) -> dict:
    groups = {}
    for s in current_signals(con, paper_id):
        if s["severity"] != "info":
            groups.setdefault(s["code"], []).append(s)
    return groups


def flags(con, paper_id) -> tuple[list, list]:
    """(open, dismissed) warn/fail signals for a paper."""
    dis = _dismissals(con, paper_id)
    open_, dismissed = [], []
    for code, sigs in _by_code(con, paper_id).items():
        v = dis.get(code, {})
        hit = v.get("action") == "dismiss" and v.get("evidence") == _key(sigs)
        (dismissed if hit else open_).extend(sigs)
    open_.sort(key=lambda s: (-RISK[s["severity"]], s["id"]))  # fails first, then in check order
    return open_, dismissed


def _decide(con, paper_id, code, action, reason, actor):
    if not reason.strip():
        raise ValueError("a reason is required")
    sigs = _by_code(con, paper_id).get(code)
    if not sigs:
        raise ValueError(f"paper {paper_id} has no current {code} flag")
    value = {"action": action, "code": code, "evidence": _key(sigs)}
    con.execute("INSERT INTO decision(paper_id, actor, kind, value, reason, signal_id, created_at)"
                " VALUES(?,?,?,?,?,?,?)", (paper_id, actor, "override", json.dumps(value), reason, sigs[0]["id"], now()))
    audit(con, actor, action, "paper", paper_id, {"code": code, "reason": reason})
    con.commit()


def dismiss(con, paper_id, code, reason, actor):
    _decide(con, paper_id, code, "dismiss", reason, actor)


def restore(con, paper_id, code, reason, actor):
    _decide(con, paper_id, code, "restore", reason, actor)


def appeals(con, paper_id) -> dict:
    """code -> latest appeal state {"status": open|granted|denied, "text", "by", "at", "reason"}."""
    out = {}
    for d in con.execute("SELECT * FROM decision WHERE paper_id=? AND kind='appeal' ORDER BY id", (paper_id,)):
        try:
            v = json.loads(d["value"])
        except ValueError:
            continue
        if v.get("status") == "open":
            out[v["code"]] = {"status": "open", "text": d["reason"], "by": d["actor"], "at": d["created_at"]}
        elif v.get("code") in out:
            out[v["code"]].update(status=v["status"], reason=d["reason"], decided_by=d["actor"])
    return out


def appeal(con, paper_id, code, text, actor):
    """Record an author's appeal against a flag (a chair types it in from the author's reply)."""
    if not text.strip():
        raise ValueError("write down what the authors say")
    if code not in {s["code"] for s in flags(con, paper_id)[0]}:
        raise ValueError(f"paper {paper_id} has no open {code} flag")
    _appeal(con, paper_id, code, "open", text, actor)


def decide_appeal(con, paper_id, code, grant: bool, reason, actor):
    """Granting dismisses the flag (reversible like any dismissal); denying keeps it. Both need a reason."""
    if not reason.strip():
        raise ValueError("a reason is required")
    if appeals(con, paper_id).get(code, {}).get("status") != "open":
        raise ValueError(f"paper {paper_id} has no open appeal on {code}")
    _appeal(con, paper_id, code, "granted" if grant else "denied", reason, actor)
    if grant:
        dismiss(con, paper_id, code, f"Appeal granted: {reason}", actor)


def _appeal(con, paper_id, code, status, text, actor):
    con.execute("INSERT INTO decision(paper_id, actor, kind, value, reason, created_at) VALUES(?,?,?,?,?,?)",
                (paper_id, actor, "appeal", json.dumps({"code": code, "status": status}), text, now()))
    audit(con, actor, f"appeal_{status}", "paper", paper_id, {"code": code, "reason": text})
    con.commit()


def snapshot(con, ids) -> dict:
    return {pid: {s["code"] for s in flags(con, pid)[0]} for pid in ids}


def changes(before: dict, after: dict) -> list[str]:
    lines = []
    for pid in sorted(after):
        new, gone = after[pid] - before.get(pid, set()), before.get(pid, set()) - after[pid]
        if new or gone:
            lines.append(f"  paper {pid}: " + ", ".join([f"+{c}" for c in sorted(new)] + [f"-{c}" for c in sorted(gone)]))
    return lines


def _evidence_lines(evidence: dict, limit=5) -> list[str]:
    out = []
    for key, val in evidence.items():
        if key in ("truncated", "error") or val in (None, "", [], {}):
            continue
        items = val if isinstance(val, list) else [val]
        for item in items[:limit]:
            if isinstance(item, dict):
                item = ", ".join(f"{k}: {v}" for k, v in item.items() if v not in (None, "", [], {}))
            out.append(f"     - {str(item)[:200]}")
        if isinstance(val, list) and len(val) > limit:
            out.append(f"     - ... and {len(val) - limit} more")
    return out


def notice(con, paper) -> str | None:
    """Plain-text draft asking the authors to fix the open flags that are theirs to fix; None if there are none.
    Nothing is sent: a chair reads, edits and sends it."""
    mine = [s for s in flags(con, paper["id"])[0] if s["code"] in FIX_HINTS]
    if not mine:
        return None
    ref = paper["external_id"] or f"#{paper['id']}"
    lines = [f"Subject: Submission {ref}: please fix before review", "",
             "Dear authors,", "",
             f"An automated check of submission {ref}"
             + (f" (\"{paper['title']}\")" if paper["title"] else "")
             + " found the following. Items marked REQUIRED must be fixed for the paper to go to review;",
             "the others are worth a look. If you think a finding is wrong, reply and a chair will check it.", ""]
    for n, s in enumerate(mine, 1):
        tag = "REQUIRED" if s["severity"] == "fail" else "CHECK"
        lines.append(f"{n}. [{tag}] {FIX_HINTS[s['code']]}")
        lines.append(f"   Found: {s['message']}")
        lines += _evidence_lines(evidence(s))
    lines += ["", "Best regards,", "The program chairs"]
    return "\n".join(lines) + "\n"


def _cell(v):
    """Spreadsheets run cells that start with = + - @ as formulas; titles and ids come from submitters."""
    return "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v


def papers(con, batch_id=None, past=False):
    """Submissions (or, with past=True, the past-proceedings papers they are compared against)."""
    return con.execute("SELECT p.* FROM paper p JOIN batch b ON b.id = p.batch_id WHERE (? IS NULL OR p.batch_id=?)"
                       " AND (b.kind = 'past') = ? ORDER BY p.id", (batch_id, batch_id, past)).fetchall()


def export_csv(con, out, batch_id=None, rules=None) -> int:
    from .compliance import load_rules
    from .scoring import score
    rules = rules or load_rules()
    rows = papers(con, batch_id)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8 titles correctly
        w = csv.writer(fh)
        w.writerow(["paper_id", "external_id", "title", "format", "status", "pages", "fails", "warnings",
                    "dismissed", "flags", "authors_can_fix", "score", "triage", "open_appeals"])
        for p in rows:
            open_, dismissed = flags(con, p["id"])
            w.writerow([p["id"], _cell(p["external_id"]), _cell(p["title"]), p["format"], p["status"], p["page_count"],
                        sum(s["severity"] == "fail" for s in open_), sum(s["severity"] == "warn" for s in open_),
                        len(dismissed), "; ".join(f"{s['severity']}:{s['code']}" for s in open_),
                        "yes" if any(s["code"] in FIX_HINTS for s in open_) else "no",
                        (sc := score(open_, p["status"], rules))["score"], sc["label"],
                        sum(a["status"] == "open" for a in appeals(con, p["id"]).values())])
    return len(rows)


def health(con, batch_id=None, rules=None) -> dict:
    """The whole conference at a glance: how many papers are clear, which problems are common, and how long
    the papers run. Counts papers, not signals: one paper with five undefined citations counts once."""
    from .compliance import load_rules
    from .scoring import LABELS, NAMES, score
    rules = rules or load_rules()
    rows = papers(con, batch_id)
    tiers = {"fail": 0, "look": 0, "ok": 0}
    triage = dict.fromkeys(LABELS.values(), 0)
    subs = {n: 0 for n in NAMES}
    open_appeals = 0
    by_code: dict[str, dict] = {}
    pages: dict[int, int] = {}
    formats: dict[str, int] = {}
    dismissed = 0
    for p in rows:
        open_, gone = flags(con, p["id"])
        dismissed += len(gone)
        sc = score(open_, p["status"], rules)
        triage[sc["label"]] += 1
        for n, v in sc["subscores"].items():
            subs[n] += v
        open_appeals += sum(a["status"] == "open" for a in appeals(con, p["id"]).values())
        tiers["fail" if any(s["severity"] == "fail" for s in open_) else "look" if open_ else "ok"] += 1
        for s in open_:
            c = by_code.setdefault(s["code"], {"code": s["code"], "papers": 0, "severity": s["severity"],
                                               "authors_can_fix": s["code"] in FIX_HINTS})
            c["papers"] += 1
            c["severity"] = max(c["severity"], s["severity"], key=RISK.get)
        if p["page_count"]:
            pages[p["page_count"]] = pages.get(p["page_count"], 0) + 1
        formats[p["format"]] = formats.get(p["format"], 0) + 1
    common = sorted(by_code.values(), key=lambda c: (-c["papers"], -RISK[c["severity"]], c["code"]))
    return {"papers": len(rows), "tiers": tiers, "common": common, "pages": dict(sorted(pages.items())),
            "formats": formats, "dismissed": dismissed, "triage": triage, "open_appeals": open_appeals,
            "subscores": {n: round(v / len(rows)) if rows else 100 for n, v in subs.items()}}
