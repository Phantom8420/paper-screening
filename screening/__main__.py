"""CLI: python -m screening ingest <files or zips...> | check [--batch N] | status [--batch N]"""
import argparse
import os
from pathlib import Path

from .compliance import load_rules, run_compliance
from .convert import convert_paper
from .db import connect, current_signals
from .intake import ingest


def main(argv=None):
    ap = argparse.ArgumentParser(prog="screening")
    ap.add_argument("--data", default=os.environ.get("SCREENING_DATA", "data"), help="data directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest files, folders or zips, convert and check them")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--name", help="batch name")
    p.add_argument("--rules", type=Path, help="TOML file overriding the default IEEE rules")
    p.add_argument("--no-convert", action="store_true")
    c = sub.add_parser("check", help="re-run compliance checks (new run; earlier flags stay in the log)")
    c.add_argument("--batch", type=int)
    c.add_argument("--rules", type=Path)
    s = sub.add_parser("status", help="list papers and their current flags")
    s.add_argument("--batch", type=int)
    s.add_argument("-v", "--verbose", action="store_true", help="show messages, not just codes")
    args = ap.parse_args(argv)

    data = Path(args.data)
    con = connect(data / "screening.db")
    if args.cmd == "ingest":
        missing = [p for p in args.paths if not p.exists()]
        if missing:
            ap.error(f"not found: {', '.join(map(str, missing))}")
        rules = load_rules(args.rules)
        batch_id, ids = ingest(con, args.paths, data, args.name)
        if not args.no_convert:
            for pid in ids:
                if con.execute("SELECT status FROM paper WHERE id=?", (pid,)).fetchone()[0] == "ingested":
                    convert_paper(con, pid, data)
                run_compliance(con, pid, data, rules)
        args.batch, args.verbose = batch_id, False
    elif args.cmd == "check":
        rules = load_rules(args.rules)
        for (pid,) in con.execute("SELECT id FROM paper WHERE (? IS NULL OR batch_id=?) AND status!='ingested'",
                                  (args.batch, args.batch)).fetchall():
            run_compliance(con, pid, data, rules)
        args.verbose = False
    show_status(con, args.batch, args.verbose)


def show_status(con, batch_id=None, verbose=False):
    rows = con.execute(
        "SELECT * FROM paper WHERE (? IS NULL OR batch_id=?) ORDER BY id", (batch_id, batch_id)).fetchall()
    for r in rows:
        flags = [s for s in current_signals(con, r["id"]) if s["severity"] != "info"]
        pages = r["page_count"] if r["page_count"] is not None else "-"
        print(f"{r['id']:>4}  {r['format']:<7} {r['status']:<13} pages={pages:<3} {r['external_id']}"
              + (f"  [{', '.join(f'{s['severity']}:{s['code']}' for s in flags)}]" if flags and not verbose else ""))
        if verbose:
            for s in flags:
                print(f"        {s['severity']:<4} {s['check_name']}/{s['code']}: {s['message']}")
    print(f"{len(rows)} paper(s)")


if __name__ == "__main__":
    main()
