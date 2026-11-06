"""CLI: python -m screening ingest <files or zips...> | status [--batch N]"""
import argparse
import os
from pathlib import Path

from .convert import convert_paper
from .db import connect
from .intake import ingest


def main(argv=None):
    ap = argparse.ArgumentParser(prog="screening")
    ap.add_argument("--data", default=os.environ.get("SCREENING_DATA", "data"), help="data directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest files, folders or zips and convert them")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--name", help="batch name")
    p.add_argument("--no-convert", action="store_true")
    s = sub.add_parser("status", help="list papers and open problems")
    s.add_argument("--batch", type=int)
    args = ap.parse_args(argv)

    data = Path(args.data)
    con = connect(data / "screening.db")
    if args.cmd == "ingest":
        missing = [p for p in args.paths if not p.exists()]
        if missing:
            ap.error(f"not found: {', '.join(map(str, missing))}")
        batch_id, ids = ingest(con, args.paths, data, args.name)
        if not args.no_convert:
            for pid in ids:
                if con.execute("SELECT status FROM paper WHERE id=?", (pid,)).fetchone()[0] == "ingested":
                    convert_paper(con, pid, data)
        args.batch = batch_id
    show_status(con, args.batch)


def show_status(con, batch_id=None):
    rows = con.execute(
        "SELECT * FROM paper WHERE (? IS NULL OR batch_id=?) ORDER BY id", (batch_id, batch_id)).fetchall()
    for r in rows:
        problems = [f"{s['severity']}:{s['code']}" for s in con.execute(
            "SELECT severity, code FROM signal WHERE paper_id=? AND severity!='info'", (r["id"],))]
        pages = r["page_count"] if r["page_count"] is not None else "-"
        print(f"{r['id']:>4}  {r['format']:<7} {r['status']:<13} pages={pages:<3} {r['external_id']}"
              + (f"  [{', '.join(problems)}]" if problems else ""))
    print(f"{len(rows)} paper(s)")


if __name__ == "__main__":
    main()
