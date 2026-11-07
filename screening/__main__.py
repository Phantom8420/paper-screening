"""CLI: python -m screening ingest | import | check | status | report | dismiss | restore | notices | export  (-h on each)"""
import argparse
import getpass
import os
import sys
from collections import Counter
from pathlib import Path

from .compliance import load_rules
from .db import connect
from .importer import import_export
from .intake import ingest
from .pipeline import process
from .review import (
    FIX_HINTS,
    changes,
    dismiss,
    export_csv,
    flags,
    health,
    notice,
    papers,
    restore,
    snapshot,
)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="screening")
    ap.add_argument("--data", default=os.environ.get("SCREENING_DATA", "data"), help="data directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest files, folders or zips, convert and check them")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--name", help="batch name")
    p.add_argument("--rules", type=Path, help="TOML file overriding the default IEEE rules")
    p.add_argument("--no-convert", action="store_true")
    p.add_argument("--meta", type=Path, help="EasyChair/HotCRP export (.xlsx, .csv or .json) with titles and authors")
    m = sub.add_parser("import", help="attach titles and authors from an EasyChair/HotCRP export, then re-check")
    m.add_argument("export", type=Path)
    m.add_argument("--batch", type=int)
    m.add_argument("--rules", type=Path)
    c = sub.add_parser("check", help="re-run all checks and list what changed (earlier flags stay in the log)")
    c.add_argument("--batch", type=int)
    c.add_argument("--rules", type=Path)
    for q in (p, c, m):
        q.add_argument("--offline", action="store_true",
                       help="check citations against the bibliography only; no reference lookups")
        q.add_argument("--camera-ready", action="store_true",
                       help="final versions: authors required, copyright notice, embedded fonts; no anonymity")
        q.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1),
                       help="papers converted and checked at once (default %(default)s)")
    s = sub.add_parser("status", help="list papers and their open flags, with a count of each flag")
    s.add_argument("--batch", type=int)
    s.add_argument("-v", "--verbose", action="store_true", help="show messages, evidence counts and fix hints")
    s.add_argument("--sort", choices=["id", "risk"], default="id", help="risk: most failures first")
    for name, verb in (("dismiss", "hide a flag you checked and judged wrong; it returns if its evidence changes"),
                       ("restore", "undo a dismissal")):
        d = sub.add_parser(name, help=verb)
        d.add_argument("paper", type=int)
        d.add_argument("code", help="flag code as shown by status, e.g. self_reference")
        d.add_argument("--reason", required=True)
        d.add_argument("--by", default=getpass.getuser(), help="who decided (default: login name)")
    n = sub.add_parser("notices", help="write a fix-request draft for each paper whose authors have something to fix")
    n.add_argument("--batch", type=int)
    n.add_argument("--out", type=Path, help="folder (default: <data>/notices)")
    n.add_argument("--force", action="store_true", help="replace drafts that already exist")
    r = sub.add_parser("report", help="conference health: how many papers are clear and the most common problems")
    r.add_argument("--batch", type=int)
    e = sub.add_parser("export", help="one CSV row per paper, for a spreadsheet or the submission system")
    e.add_argument("--batch", type=int)
    e.add_argument("--out", type=Path, default=Path("screening.csv"))
    w = sub.add_parser("serve", help="open the chair web UI (needs: pip install -e .[web])")
    w.add_argument("--host", default="127.0.0.1", help="no login yet, so keep this local unless the network is trusted")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--by", default=getpass.getuser(), help="name recorded on dismissals made in the UI")
    w.add_argument("--authors", action="store_true",
                   help="serve the author pre-check page instead: upload one paper, get the checklist, nothing kept")
    w.add_argument("--rules", type=Path, help="TOML file overriding the default IEEE rules (--authors only)")
    args = ap.parse_args(argv)

    data = Path(args.data)
    if args.cmd == "serve":
        import uvicorn
        from .web import create_app
        if args.authors:
            from .web import create_author_app
            app = create_author_app(load_rules(args.rules))
        else:
            connect(data / "screening.db").close()
            app = create_app(data, args.by)
        print(f"Open http://{args.host}:{args.port} (Ctrl+C to stop)")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return
    con = connect(data / "screening.db")
    if args.cmd == "ingest":
        missing = [p for p in args.paths if not p.exists()]
        if missing:
            ap.error(f"not found: {', '.join(map(str, missing))}")
        rules = _rules(args)
        if args.meta and not args.meta.is_file():
            ap.error(f"not found: {args.meta}")
        batch_id, ids = ingest(con, args.paths, data, args.name)
        if args.meta:
            _report_import(import_export(con, args.meta, batch_id))
        if not args.no_convert:
            process(data, ids, rules, args.jobs, convert=True)
        show_status(con, batch_id)
    elif args.cmd == "check":
        rules = _rules(args)
        ids = [r["id"] for r in papers(con, args.batch)]
        before = snapshot(con, ids)
        process(data, ids, rules, args.jobs, convert=True)  # converts only papers an interrupted ingest left behind
        show_status(con, args.batch)
        diff = changes(before, snapshot(con, ids))
        print(f"changes since the last check: {len(diff)} paper(s)" + ("" if diff else ", none"))
        print("\n".join(diff[:50]) + (f"\n  ... {len(diff) - 50} more" if len(diff) > 50 else ""), end="\n" if diff else "")
    elif args.cmd == "import":
        if not args.export.is_file():
            ap.error(f"not found: {args.export}")
        res = import_export(con, args.export, args.batch)
        _report_import(res)
        if res["matched"]:  # the anonymity check needs the new author names
            process(data, res["matched"], _rules(args), args.jobs, convert=True)
            show_status(con, args.batch)
    elif args.cmd == "status":
        show_status(con, args.batch, args.verbose, args.sort)
    elif args.cmd in ("dismiss", "restore"):
        try:
            (dismiss if args.cmd == "dismiss" else restore)(con, args.paper, args.code, args.reason, args.by)
        except ValueError as err:
            sys.exit(f"error: {err}")
        print(f"paper {args.paper}: {args.code} {args.cmd}ed by {args.by}")
    elif args.cmd == "notices":
        out = args.out or data / "notices"
        out.mkdir(parents=True, exist_ok=True)
        written = kept = 0
        for paper in papers(con, args.batch):
            if text := notice(con, paper):
                name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (paper["external_id"] or ""))[:60]
                path = out / f"{paper['id']:04d}_{name}.txt"
                if path.exists() and not args.force:  # a chair may have edited it
                    kept += 1
                    continue
                path.write_text(text, encoding="utf-8")
                written += 1
        print(f"{written} notice draft(s) in {out} (nothing was sent)"
              + (f"; {kept} existing draft(s) kept, --force to replace" if kept else ""))
    elif args.cmd == "report":
        h = health(con, args.batch)
        t = h["tiers"]
        print(f"{h['papers']} paper(s): {t['ok']} clear, {t['look']} worth a look, {t['fail']} failed a check;"
              f" {h['dismissed']} flag(s) dismissed by chairs")
        print("formats: " + ", ".join(f"{k} {v}" for k, v in h["formats"].items()))
        print("pages:   " + ", ".join(f"{k}p x{v}" for k, v in h["pages"].items()))
        print("most common problems (papers affected):")
        for c in h["common"][:15]:
            print(f"  {c['papers']:>4}  {c['severity']:<4}  {c['code']}" + ("  (authors can fix)" if c["authors_can_fix"] else ""))
    elif args.cmd == "export":
        print(f"{export_csv(con, args.out, args.batch)} paper(s) written to {args.out}")


def _report_import(res):
    print(f"{res['file']}: {res['records']} submission(s) in the export, {len(res['matched'])} paper(s) matched")
    if res["unmatched"]:
        shown = ", ".join(map(str, res["unmatched"][:10])) + (" ..." if len(res["unmatched"]) > 10 else "")
        print(f"  no submission number matched: {shown}")


def _rules(args):
    rules = load_rules(args.rules)
    if args.offline:
        rules["references"]["online"] = False
    if args.camera_ready:
        rules["anonymization"]["double_blind"] = False
        rules["camera_ready"]["enabled"] = True
    return rules


def show_status(con, batch_id=None, verbose=False, sort="id"):
    rows = [(r, *flags(con, r["id"])) for r in papers(con, batch_id)]
    if sort == "risk":
        rows.sort(key=lambda x: (-sum(s["severity"] == "fail" for s in x[1]), -len(x[1]), x[0]["id"]))
    tally, hidden = Counter(), 0
    for r, open_, dismissed in rows:
        tally.update(f"{s['severity']}:{s['code']}" for s in open_)
        hidden += len(dismissed)
        pages = r["page_count"] if r["page_count"] is not None else "-"
        line = f"{r['id']:>4}  {r['format']:<7} {r['status']:<13} pages={pages:<3} {r['external_id']}"
        if open_ and not verbose:
            line += f"  [{', '.join(f'{s['severity']}:{s['code']}' for s in open_)}]"
        if dismissed:
            line += f"  ({len(dismissed)} dismissed)"
        print(line)
        if verbose:
            for s in open_:
                print(f"        {s['severity']:<4} {s['check_name']}/{s['code']}: {s['message']}")
                if hint := FIX_HINTS.get(s["code"]):
                    print(f"             fix: {hint}")
    print(f"{len(rows)} paper(s)" + (f", {hidden} flag(s) dismissed" if hidden else ""))
    for flag, k in sorted(tally.items(), key=lambda kv: (not kv[0].startswith("fail"), -kv[1], kv[0])):
        print(f"  {k:>4}  {flag}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # `status | head` closed the pipe; not an error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
