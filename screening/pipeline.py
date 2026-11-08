"""Run the checks over many papers. Lives outside __main__ so worker processes can import it on Windows."""
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .compliance import run_compliance
from .convert import convert_paper
from .db import add_signal, connect, start_run
from .figures import index_figures, run_figures
from .integrity import run_integrity
from .quality import run_quality
from .references import run_references
from .similarity import run_similarity


def _convert_and_check(db: Path, data: Path, pid: int, rules, convert: bool) -> None:
    con = connect(db)  # one connection per worker process
    try:
        if convert and con.execute("SELECT status FROM paper WHERE id=?", (pid,)).fetchone()[0] == "ingested":
            convert_paper(con, pid, data)
        index_figures(con, pid, data)
        if _past(con, [pid]):  # published papers are only compared against
            con.execute("UPDATE paper SET status='checked' WHERE id=? AND status='converted'", (pid,))
            con.commit()
            return
        run_compliance(con, pid, data, rules)
        run_quality(con, pid, rules)
        run_integrity(con, pid, data)
        if not rules["references"]["online"]:
            run_references(con, pid, data, rules)  # offline it is local parsing, no rate limit to respect
    finally:
        con.close()


def process(data: Path, ids, rules, jobs=1, convert=True):
    """Convert and check papers. Conversion and compliance run in worker processes (pandoc, LaTeX and
    LibreOffice are subprocesses; PDF text extraction is pure Python, so threads would share one GIL).
    Offline reference checks run in the workers too. Online lookups run one paper at a time afterwards:
    preflight already queries every reference of a paper concurrently, and more parallel papers would
    only trip the indexes' rate limits."""
    db = data / "screening.db"
    online = rules["references"]["online"]
    what = "converted and checked" if convert else "checked"
    if jobs <= 1:
        for n, pid in enumerate(ids, 1):
            _convert_and_check(db, data, pid, rules, convert)
            _progress(what, n, len(ids))
    else:
        with ProcessPoolExecutor(jobs) as ex:
            futures = [ex.submit(_convert_and_check, db, data, pid, rules, convert) for pid in ids]
            for n, f in enumerate(as_completed(futures), 1):
                f.result()  # both steps record their own failures; anything raised here is a real bug
                _progress(what, n, len(ids))
    _duplicates(db, ids, rules)
    con = connect(db)
    past = _past(con, ids)
    con.close()
    ids = [i for i in ids if i not in past]
    if not online or not ids:
        return
    if ids and not os.environ.get("PAPER_PREFLIGHT_EMAIL"):
        print("note: set PAPER_PREFLIGHT_EMAIL to a contact address; Crossref's polite pool makes lookups"
              " about 3x faster and less likely to be cut off", file=sys.stderr)
    con = connect(db)
    for n, pid in enumerate(ids, 1):
        run_references(con, pid, data, rules)
        _progress("references", n, len(ids))
    con.close()


def _past(con, ids) -> set:
    q = ",".join("?" * len(ids))
    return {r[0] for r in con.execute(f"SELECT p.id FROM paper p JOIN batch b ON b.id = p.batch_id WHERE"
                                      f" b.kind = 'past' AND p.id IN ({q})", list(ids))} if ids else set()


def _duplicates(db, ids, rules):
    """Text and figure comparison need every paper, so they run once per batch after the workers. New past
    proceedings mean every submission is compared again."""
    if not ids:
        return
    con = connect(db)
    try:
        if _past(con, ids):
            from .review import papers
            ids = [p["id"] for p in papers(con)]
        n = len(run_similarity(con, ids, rules) | run_figures(con, ids))
        _progress("compared for duplicates", n, n)
    except Exception as e:  # noqa: BLE001  one bad paper must not stop the batch; every paper gets the flag
        error = f"{type(e).__name__}: {str(e)[:500]}"
        try:
            con.rollback()
            for pid in ids:
                add_signal(con, start_run(con, pid, "duplicates"), "duplicates_crashed", "fail",
                           "The duplicate check stopped with an unexpected error.", {"error": error})
            con.commit()
        except Exception:  # noqa: BLE001  e.g. the database itself is locked or full; say so and go on
            print(f"duplicate check failed and could not be recorded: {error}", file=sys.stderr)
    finally:
        con.close()


def _progress(what, n, total):
    if sys.stderr.isatty() or n == total:
        print(f"\r{what}: {n}/{total}", end="\n" if n == total else "", file=sys.stderr, flush=True)
