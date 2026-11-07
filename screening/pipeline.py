"""Run the checks over many papers. Lives outside __main__ so worker processes can import it on Windows."""
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .compliance import run_compliance
from .convert import convert_paper
from .db import add_signal, connect, start_run
from .duplicates import run_duplicates
from .references import run_references


def _convert_and_check(db: Path, data: Path, pid: int, rules, convert: bool) -> None:
    con = connect(db)  # one connection per worker process
    try:
        if convert and con.execute("SELECT status FROM paper WHERE id=?", (pid,)).fetchone()[0] == "ingested":
            convert_paper(con, pid, data)
        run_compliance(con, pid, data, rules)
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
    if not online:
        return
    if ids and not os.environ.get("PAPER_PREFLIGHT_EMAIL"):
        print("note: set PAPER_PREFLIGHT_EMAIL to a contact address; Crossref's polite pool makes lookups"
              " about 3x faster and less likely to be cut off", file=sys.stderr)
    con = connect(db)
    for n, pid in enumerate(ids, 1):
        run_references(con, pid, data, rules)
        _progress("references", n, len(ids))
    con.close()


def _duplicates(db, ids, rules):
    """Needs every paper's text, so it runs once per batch after the workers."""
    if not ids:
        return
    con = connect(db)
    try:
        n = len(run_duplicates(con, ids, rules))
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
