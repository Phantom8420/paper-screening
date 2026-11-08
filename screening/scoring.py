"""Strength score and triage label from a paper's open flags. Recommendations only: the label changes as soon
as a chair dismisses a flag or grants an appeal, and a person makes every decision.

Score: 100, minus 30 per required fix and 8 per warning (dismissed flags do not count). A failure the
authors cannot fix (a duplicate, hidden text, a check that crashed) does not lower the score; it sends the
paper to "Chair check" instead, since only a person can judge it."""
from .review import FIX_HINTS, flags

ANONYMITY = {"author_block_present", "affiliation_in_front_matter", "email_in_text", "known_author_named",
             "self_reference", "identifying_link", "acknowledgments_present", "file_metadata_identifies"}
CATEGORY = {"compliance": "Format", "conversion": "Format", "intake": "Format", "references": "References",
            "duplicates": "Originality", "similarity": "Originality", "figures": "Originality", "quality": "Quality",
            "integrity": "Integrity"}
NAMES = ("Format", "Anonymity", "References", "Originality", "Quality", "Integrity")
LABELS = {"review": "Ready for review", "fix": "Needs fixes", "chair": "Chair check", "reject": "Desk-reject candidate",
          "wait": "Being checked"}


def category(s) -> str:
    if s["code"] in ANONYMITY:
        return "Anonymity"
    return CATEGORY.get(s["check_name"].split(":")[0], "Format")


def _points(sigs) -> int:
    return max(0, 100 - sum(30 for s in sigs if s["severity"] == "fail")
               - sum(8 for s in sigs if s["severity"] == "warn"))


def score(open_flags, status, rules) -> dict:
    subs = {n: _points([s for s in open_flags if category(s) == n]) for n in NAMES}
    total = _points(open_flags)
    t = rules["triage"]
    if status in ("ingested", "converted"):
        key = "wait"
    elif any(s["severity"] == "fail" and s["code"] not in FIX_HINTS for s in open_flags):
        key = "chair"
    elif total < t["reject_below"]:
        key = "reject"
    elif total < t["review_at"]:
        key = "fix"
    else:
        key = "review"
    return {"score": total, "subscores": subs, "triage": key, "label": LABELS[key]}


def paper_score(con, paper, rules) -> dict:
    return score(flags(con, paper["id"])[0], paper["status"], rules)
