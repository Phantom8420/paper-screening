"""Duplicate and overlapping submissions across every paper in the database, including past proceedings.

Each paper's text (title, abstract and sections; the reference list and quoted passages are left out, since
related papers cite the same work and may quote the same sources) becomes a set of 5-word phrases (shingles). An inverted index finds, for each paper, which other papers share
its phrases. Phrases found in many papers (template instructions, stock wording) are ignored, so the score is
"how much of the shorter paper also appears in the other one". Evidence: the score and the shared passages.
The other paper's id is chair-only information; these flags never go into author notices.
"""
import json
import re
from collections import Counter, defaultdict

from .db import add_signal, audit, start_run

VERSION = "3"
N = 5  # words per phrase: long enough that chance matches between unrelated papers are rare
COMMON = 10  # a phrase in more papers than this is boilerplate, not copying
MIN_PHRASES = 200  # about 200 words; a title and abstract alone must not "match" a full paper
COMPARABLE = 0.5  # a fail needs the shorter text to be at least half the longer; otherwise it is a warn
WORD = re.compile(r"[^\W_]+")
QUOTED = re.compile(r"[“\"][^“”\"]{1,600}[”\"]")  # quotation marks pandoc keeps; an unclosed mark removes nothing
NOT_BODY = re.compile(r"^\W*(?:[ivxlc]+\.|\d+\.?)?\s*(?:references?|bibliography|acknowledge?ments?)\W*$", re.I)


def words(struct: dict) -> list[str]:
    parts = [struct.get("title") or "", struct.get("abstract") or ""]
    parts += [s.get("text") or "" for s in (struct.get("sections") or [])
              if isinstance(s, dict) and not NOT_BODY.match(s.get("heading") or "")]
    return WORD.findall(QUOTED.sub(" ", " ".join(p for p in parts if isinstance(p, str))).lower())


def phrases(ws: list[str]) -> list[int]:
    return [hash(" ".join(ws[i:i + N])) for i in range(len(ws) - N + 1)]  # salted per process; never stored


def _passages(ws, seq, shared: set, limit=3, width=300) -> list[str]:
    """Longest runs of consecutive shared phrases, as text from this paper."""
    runs, start = [], None
    for i, h in enumerate([*seq, None]):
        if h is not None and h in shared:
            start = i if start is None else start
        elif start is not None:
            runs.append((i - start, start))
            start = None
    runs.sort(key=lambda r: (-r[0], r[1]))
    return [" ".join(ws[s:s + n + N - 1])[:width] for n, s in runs[:limit]]


def _words_of(row) -> list[str] | None:
    try:
        struct = json.loads(row["sections_json"])
        return words(struct) if isinstance(struct, dict) else None
    except (ValueError, TypeError, AttributeError):  # one damaged row must not stop every later batch
        return None


def _load(con) -> tuple[dict, dict]:
    """(rows by id, phrase set by id). Only the sets stay in memory; text is rebuilt for matches only."""
    rows, sets = {}, {}
    for r in con.execute("SELECT p.id, p.external_id, p.sections_json, b.name AS batch, b.kind FROM paper p"
                         " JOIN batch b ON b.id = p.batch_id WHERE p.sections_json IS NOT NULL"):
        if (ws := _words_of(r)) is not None:
            rows[r["id"]], sets[r["id"]] = r, frozenset(phrases(ws))
    return rows, sets


def _index(sets: dict) -> dict:
    index = defaultdict(list)
    for pid, s in sets.items():
        for h in s:
            index[h].append(pid)
    for h in [h for h, ids in index.items() if len(ids) > COMMON]:
        del index[h]  # boilerplate: neither scored nor shown
    return index


def find_matches(rows, sets, index, ids, warn: float) -> dict:
    """paper id -> [(other id, overlap)] at or above `warn`, for `ids` only (the score is symmetric)."""
    out = {}
    for pid in ids:
        mine = sets.get(pid)
        if mine is None or len(mine) < MIN_PHRASES:
            continue
        same_sub = rows[pid]["external_id"]
        shared = Counter(o for h in mine for o in index.get(h, ()) if o != pid)
        hits = []
        for other, n in shared.items():
            if len(sets[other]) < MIN_PHRASES or (same_sub and rows[other]["external_id"] == same_sub):
                continue  # same submission id in another batch is a resubmission, not a duplicate
            if (overlap := n / min(len(mine), len(sets[other]))) >= warn:
                hits.append((other, round(overlap, 3)))
        out[pid] = sorted(hits, key=lambda x: (-x[1], x[0]))  # stable order keeps dismissals valid
    return out


def _source(row) -> str:
    return f"past proceedings: {row['batch']}" if row["kind"] == "past" else "this conference"


def _signal(con, run, hits, past: bool, mine, rows, sets, index, ws, seq, fail):
    items = [{"paper_id": o, "external_id": rows[o]["external_id"], "batch": rows[o]["batch"], "source": _source(rows[o]),
              "overlap": ov, "shared_passages": _passages(ws, seq, (sets[o] & mine) & index.keys())}
             for o, ov in hits[:10]]
    top, other = hits[0][1], hits[0][0]
    comparable = min(len(mine), len(sets[other])) >= COMPARABLE * max(len(mine), len(sets[other]))
    what = f"published paper {rows[other]['external_id'] or other} ({rows[other]['batch']})" if past else f"paper {other}"
    if top >= fail and comparable:
        code, sev, msg = ("published_duplicate" if past else "duplicate_submission"), "fail", (
            f"{top:.0%} of the text matches {what}: " + ("likely already published." if past else
                                                         "likely the same paper submitted twice."))
    else:
        note = "" if comparable else " (one text is much shorter: possibly a part or an extraction gap)"
        code, sev, msg = ("published_overlap" if past else "overlapping_submission"), "warn", (
            f"{top:.0%} of the shorter text matches {what}{note}: check for " + ("reused text." if past else
                                                                                "duplicated work."))
    add_signal(con, run, code, sev, msg, {"matches": items}, score=top)


def run_duplicates(con, paper_ids, rules) -> set:
    """Compare papers against the whole database and store one run for each of `paper_ids` and every
    submission they match (an older paper learns about its new twin too). Past-proceedings papers are only
    compared against: they get no runs. Returns the ids that got a new run."""
    warn, fail = rules["duplicates"]["warn_overlap"], rules["duplicates"]["fail_overlap"]
    rows, sets = _load(con)
    index = _index(sets)
    paper_ids = [p for p in paper_ids if p not in rows or rows[p]["kind"] != "past"]
    matches = find_matches(rows, sets, index, paper_ids, warn)
    partners = {o for hits in matches.values() for o, _ in hits if rows[o]["kind"] != "past"} - set(paper_ids)
    matches |= find_matches(rows, sets, index, partners, warn)
    for pid in sorted(set(paper_ids) | partners):
        run = start_run(con, pid, "duplicates", VERSION, rules["duplicates"])
        mine = sets.get(pid)
        hits = matches.get(pid) or []
        if mine is None or len(mine) < MIN_PHRASES:
            add_signal(con, run, "duplicates_unchecked", "info", "Too little text to compare with other papers.")
        elif hits:
            ws = words(json.loads(rows[pid]["sections_json"]))
            seq = phrases(ws)
            for past in (False, True):  # one flag per source, so a chair sees "also published before" on its own
                if group := [h for h in hits if (rows[h[0]]["kind"] == "past") == past]:
                    _signal(con, run, group, past, mine, rows, sets, index, ws, seq, fail)
        by_source = Counter(_source(rows[o]) for o, _ in hits)
        n_past = sum(r["kind"] == "past" for r in rows.values())
        add_signal(con, run, "similarity_sources", "info",
                   f"Compared with {len(sets) - 1 - n_past} submission(s) and {n_past} past-proceedings paper(s).",
                   {"sources": [{"source": k, "matching_papers": v, "highest_overlap": max(ov for o, ov in hits
                                                                                             if _source(rows[o]) == k)}
                                for k, v in sorted(by_source.items())]})
        if not hits and mine is not None and len(mine) >= MIN_PHRASES:
            add_signal(con, run, "duplicates_ok", "info", f"No overlap with the other {len(sets) - 1} paper(s).")
        audit(con, "system", "duplicates", "paper", pid, {"matches": [o for o, _ in hits]})
    con.commit()
    return set(paper_ids) | partners
