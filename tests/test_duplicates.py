import json
import random

from screening.compliance import load_rules
from screening.db import connect, current_signals
from screening.duplicates import run_duplicates
from screening.review import notice


def prose(seed, n=600):
    rng = random.Random(seed)
    return " ".join(f"w{rng.randrange(5000)}" for _ in range(n))


def paper(title, body, refs="[1] A. Author, Shared reference, 2020."):
    return {"title": title, "abstract": "", "sections": [
        {"heading": "Introduction", "text": body}, {"heading": "References", "text": refs}]}


def add(con, batch, ext, struct):
    return con.execute("INSERT INTO paper(batch_id, external_id, format, status, sections_json, created_at)"
                       " VALUES(?,?,?,?,?,?)", (batch, ext, "word", "converted", json.dumps(struct), "t")).lastrowid


def flags(con, pid):
    return {s["code"]: s for s in current_signals(con, pid) if s["check_name"] == "duplicates"}


def db(tmp_path):
    con = connect(tmp_path / "t.db")
    b = con.execute("INSERT INTO batch(name, created_at) VALUES('round 1','t')").lastrowid
    return con, b


def test_duplicates_overlap_and_unrelated(tmp_path):
    con, b = db(tmp_path)
    base = prose(1)
    tweaked = base.split()
    for i in range(0, len(tweaked), 60):  # a few edits, as when a paper is resubmitted under another id
        tweaked[i] = "changed"
    long_refs = prose(9, 300)  # the same long reference list in two unrelated papers is not duplication
    ids = {
        "a": add(con, b, "A", paper("Title A", base)),
        "b": add(con, b, "B", paper("Other title", " ".join(tweaked))),
        "c": add(con, b, "C", paper("C", " ".join(base.split()[:250]) + " " + prose(3, 400))),
        "d": add(con, b, "D", paper("D", prose(4), refs=long_refs)),
        "e": add(con, b, "E", paper("E", prose(5), refs=long_refs)),
        "tiny": add(con, b, "T", paper("T", "too short")),
    }
    run_duplicates(con, list(ids.values()), load_rules())
    a = flags(con, ids["a"])["duplicate_submission"]
    ev = json.loads(a["evidence_json"])["matches"]
    assert ev[0]["paper_id"] == ids["b"] and ev[0]["overlap"] > 0.8 and ev[0]["batch"] == "round 1"
    assert ev[0]["shared_passages"] and ev[0]["shared_passages"][0].split()[0].startswith("w")
    assert "duplicate_submission" in flags(con, ids["b"])
    assert "overlapping_submission" in flags(con, ids["c"])  # 250 of C's ~650 words come from A
    assert set(flags(con, ids["d"])) == {"duplicates_ok"} and set(flags(con, ids["e"])) == {"duplicates_ok"}
    assert set(flags(con, ids["tiny"])) == {"duplicates_unchecked"}
    row = con.execute("SELECT * FROM paper WHERE id=?", (ids["a"],)).fetchone()
    assert notice(con, row) is None  # another submission's id must never reach the authors


def test_new_batch_flags_the_old_twin_too(tmp_path):
    con, b = db(tmp_path)
    old = add(con, b, "old", paper("X", prose(7)))
    run_duplicates(con, [old], load_rules())
    assert set(flags(con, old)) == {"duplicates_ok"}
    b2 = con.execute("INSERT INTO batch(name, created_at) VALUES('track 2','t')").lastrowid
    new = add(con, b2, "new", paper("X", prose(7)))
    assert run_duplicates(con, [new], load_rules()) == {old, new}
    assert "duplicate_submission" in flags(con, old) and "duplicate_submission" in flags(con, new)


def test_damaged_rows_resubmissions_and_short_texts(tmp_path):
    con, b = db(tmp_path)
    broken = [add(con, b, "bad1", paper("x", "y")) for _ in range(3)]
    con.execute("UPDATE paper SET sections_json='not json' WHERE id=?", (broken[0],))
    con.execute("UPDATE paper SET sections_json='null' WHERE id=?", (broken[1],))
    con.execute("""UPDATE paper SET sections_json='{"sections": null}' WHERE id=?""", (broken[2],))
    full = add(con, b, "full", paper("F", prose(11, 1200)))
    part = add(con, b, "part", paper("P", " ".join(prose(11, 1200).split()[:300])))  # extraction got a quarter
    b2 = con.execute("INSERT INTO batch(name, created_at) VALUES('round 2','t')").lastrowid
    again = add(con, b2, "full", paper("F", prose(11, 1200)))  # same submission id: a revision, not a duplicate
    ids = [*broken, full, part, again]
    run_duplicates(con, ids, load_rules())
    assert all(set(flags(con, p)) == {"duplicates_unchecked"} for p in broken)
    assert "overlapping_submission" in flags(con, part)  # 100% contained, but too short to call it a duplicate
    assert "duplicate_submission" not in flags(con, full)
    ev = json.loads(flags(con, again)["overlapping_submission"]["evidence_json"])["matches"]
    assert [m["paper_id"] for m in ev] == [part]


def test_reference_like_headings_stay_in_the_body():
    from screening.duplicates import words
    s = {"sections": [{"heading": "III. Reference Architecture", "text": "kept"},
                      {"heading": "References", "text": "dropped"}, {"heading": "Acknowledgments", "text": "gone"}]}
    assert words(s) == ["kept"]
