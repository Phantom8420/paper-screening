"""Chair web UI: queue, paper review with evidence, dismiss/restore, notice drafts.

Runs locally (python -m screening serve) and binds to 127.0.0.1 by default: there is no login yet, so anyone
who can reach the port acts as the chair named by --by.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .compliance import load_rules
from .db import connect, current_signals
from .importer import import_export
from .intake import ingest
from .pipeline import process
from .review import (FIX_HINTS, appeal, appeals, decide_appeal, dismiss, evidence, export_csv, flags, health, notice,
                     papers, restore)
from .scoring import category, score

HERE = Path(__file__).parent
PAGE_KEYS = ("page", "pages", "body_pages", "references_start_page", "total_pages")
# changes with every edit of the stylesheet, so browsers never keep an old copy after an update
CSS_VERSION = int((HERE / "static" / "app.css").stat().st_mtime)


def first_page(ev: dict) -> int | None:
    """A page number to jump the PDF viewer to, if the evidence names one."""
    for k in PAGE_KEYS:
        v = ev.get(k)
        if isinstance(v, list) and v and isinstance(v[0], int):
            return v[0]
        if isinstance(v, int) and v > 0:
            return v
    return None


def evidence_items(ev: dict, limit=8) -> list[tuple[str, str]]:
    out = []
    for key, val in ev.items():
        if key in ("truncated", "error") or val in (None, "", [], {}):
            continue
        if isinstance(val, list) and not any(isinstance(v, (dict, list)) for v in val):
            val = [", ".join(map(str, val[:limit])) + (f" (+{len(val) - limit} more)" if len(val) > limit else "")]
        for item in (val if isinstance(val, list) else [val])[:limit]:
            if isinstance(item, dict):
                item = ", ".join(f"{k}: {v}" for k, v in item.items() if v not in (None, "", [], {}))
            out.append((key.replace("_", " "), str(item)[:300]))
    return out


def summary(con, p, rules) -> dict:
    open_, dismissed = flags(con, p["id"])
    fails = sum(s["severity"] == "fail" for s in open_)
    return {"p": p, "open": open_, "dismissed": dismissed, "fails": fails, "warns": len(open_) - fails,
            "tier": "wait" if p["status"] in ("ingested", "converted") else "fail" if fails else "look" if open_ else "ok",
            "sc": score(open_, p["status"], rules),
            "appeals": sum(a["status"] == "open" for a in appeals(con, p["id"]).values())}


PAPER_TYPES = (".zip", ".pdf", ".docx", ".doc", ".rtf", ".odt", ".tex")
EXPORT_TYPES = (".xlsx", ".xlsm", ".csv", ".json")


def run_batch(data: Path, work: Path, files: list[Path], export: Path | None, rules, job: dict, kind="submissions"):
    """Ingest, import, check; runs in a thread so the page can show progress. The uploads are copied into
    the batch by ingest, so the work folder is removed afterwards."""
    try:
        con = connect(data / "screening.db")
        try:
            batch_id, ids = ingest(con, files, data, job["name"], actor=job["actor"], kind=kind)
            job.update(batch=batch_id, ids=ids)
            if export:
                res = import_export(con, export, batch_id, job["actor"])
                job["unmatched"] = len(res["unmatched"])
        finally:
            con.close()
        process(data, ids, rules, jobs=min(4, os.cpu_count() or 1), convert=True)
        job["state"] = "done"
    except Exception as err:  # shown on the page; the batch so far stays and `check` finishes it
        job.update(state="error", error=str(err) or type(err).__name__)
    finally:
        shutil.rmtree(work, ignore_errors=True)


DEMO_NOTE = ("This online demo shows made-up sample papers only. To check real papers, run the app on your own computer, "
             "where the papers never leave it.")


def create_app(data: Path, actor: str, rules=None, demo=False) -> FastAPI:
    """demo: a public copy with sample papers; adding papers is switched off, so nothing real is uploaded."""
    data = Path(data)
    rules = rules or load_rules(None)
    job: dict = {}  # the one batch being added; a second upload waits until it finishes
    app = FastAPI(title="Paper screening", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(actor=actor, css=CSS_VERSION, demo=DEMO_NOTE if demo else "", label=lambda c: c.replace("_", " ").capitalize())

    def db() -> sqlite3.Connection:
        return connect(data / "screening.db")

    def paper_or_404(con, pid):
        p = con.execute("SELECT * FROM paper WHERE id=?", (pid,)).fetchone()
        if p is None:
            raise HTTPException(404, "No such paper")
        return p

    @app.get("/")
    def queue(request: Request, show: str = "todo"):
        con = db()
        try:
            rows = [summary(con, p, rules) for p in papers(con)]
        finally:
            con.close()
        counts = {t: sum(r["tier"] == t for r in rows) for t in ("fail", "look", "ok")}
        counts["todo"] = counts["fail"] + counts["look"]
        counts["appeals"] = sum(1 for r in rows if r["appeals"])
        total = len(rows)
        rows.sort(key=lambda r: (-r["fails"], -r["warns"], r["p"]["id"]))
        if show in ("fail", "look", "ok"):
            rows = [r for r in rows if r["tier"] == show]
        elif show == "appeals":
            rows = [r for r in rows if r["appeals"]]
        elif show == "todo":
            rows = [r for r in rows if r["tier"] in ("fail", "look")]
        return tpl.TemplateResponse(request, "queue.html", {"rows": rows, "counts": counts, "show": show,
                                                           "total": total,
                                                           "job": job_view()})

    def job_view():
        if not job:
            return None
        done = 0
        if job.get("ids"):
            con = db()
            try:
                q = ",".join("?" * len(job["ids"]))
                done = con.execute(f"SELECT COUNT(*) FROM paper WHERE id IN ({q}) AND status IN ('checked','needs_manual')",
                                   job["ids"]).fetchone()[0]
            finally:
                con.close()
        return {**job, "done": done, "total": len(job.get("ids") or [])}

    @app.get("/add")
    def add_form(request: Request):
        return tpl.TemplateResponse(request, "add.html", {"papers": ",".join(PAPER_TYPES),
                                                         "exports": ",".join(EXPORT_TYPES), "error": None})

    @app.post("/add")
    def add(request: Request, papers_: list[UploadFile] = File(..., alias="papers"),
            export: UploadFile | None = File(None), final: bool = Form(False), past: bool = Form(False)):
        def fail(msg):
            return tpl.TemplateResponse(request, "add.html", {"papers": ",".join(PAPER_TYPES),
                                        "exports": ",".join(EXPORT_TYPES), "error": msg}, 400)
        if demo:
            return fail(DEMO_NOTE)
        if job.get("state") == "running":
            return fail("Papers are still being checked. Wait until they finish, then add more.")
        uploads = [u for u in papers_ if u.filename]
        if not uploads:
            return fail("Choose the papers first: the .zip from your submission system, or the paper files.")
        bad = [u.filename for u in uploads if Path(u.filename).suffix.lower() not in PAPER_TYPES]
        if bad:
            return fail(f"{', '.join(bad)} is not a paper file. Use a .zip, PDF, Word or LaTeX file.")
        if export and export.filename and Path(export.filename).suffix.lower() not in EXPORT_TYPES:
            return fail("The author list must be the .xlsx, .csv or .json file your submission system exports.")
        (data / "tmp").mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="upload-", dir=data / "tmp"))
        files = []
        for i, u in enumerate(uploads):
            dst = work / "papers" / str(i) / Path(u.filename).name  # name only: no path from the browser
            dst.parent.mkdir(parents=True)
            with dst.open("wb") as f:
                shutil.copyfileobj(u.file, f)
            files.append(dst)
        meta = None
        if export and export.filename:
            meta = work / Path(export.filename).name
            with meta.open("wb") as f:
                shutil.copyfileobj(export.file, f)
        r = rules
        if final:
            r = {**rules, "anonymization": {**rules["anonymization"], "double_blind": False},
                 "camera_ready": {**rules["camera_ready"], "enabled": True}}
        job.clear()
        job.update(state="running", actor=actor, files=len(files), past=past,
                   name=", ".join(u.filename for u in uploads)[:120])
        threading.Thread(target=run_batch, args=(data, work, files, meta, r, job, "past" if past else "submissions"),
                         daemon=True).start()
        return RedirectResponse("/?show=all", status_code=303)

    @app.get("/job")
    def job_status():
        v = job_view()
        return {k: v[k] for k in ("state", "done", "total", "error") if k in v} if v else {}

    @app.get("/paper/{pid}")
    def paper(request: Request, pid: int, tab: str = "flags"):
        con = db()
        try:
            p = paper_or_404(con, pid)
            s = summary(con, p, rules)
            order = [r["p"]["id"] for r in sorted((summary(con, q, rules) for q in papers(con)),
                                             key=lambda r: (-r["fails"], -r["warns"], r["p"]["id"]))]
            history = con.execute("SELECT * FROM audit_log WHERE entity='paper' AND entity_id=? AND actor!='system'"
                                  " ORDER BY id DESC", (pid,)).fetchall()
            history = [{**dict(h), "pl": json.loads(h["payload_json"] or "{}")} for h in history]
            draft = notice(con, p)
            ap = appeals(con, pid)
            checks = [{"s": sig, "ev": evidence_items(evidence(sig))} for sig in current_signals(con, pid)
                      if sig["severity"] == "info"]
        finally:
            con.close()
        i = order.index(pid)
        items = [{"s": sig, "ev": evidence_items(evidence(sig)), "page": first_page(evidence(sig)),
                  "hint": FIX_HINTS.get(sig["code"]), "appeal": ap.get(sig["code"]), "cat": category(sig)}
                 for sig in s["open"]]
        gone = [{"s": sig, "page": first_page(evidence(sig))} for sig in s["dismissed"]]
        return tpl.TemplateResponse(request, "paper.html", {
            **s, "items": items, "gone": gone, "tab": tab, "history": history, "draft": draft, "checks": checks,
            "prev": order[i - 1] if i else None, "next": order[i + 1] if i + 1 < len(order) else None,
            "has_pdf": bool(p["pdf_path"]) and (data / p["pdf_path"]).is_file(),
            "required": sum(1 for it in items if it["s"]["severity"] == "fail" and it["hint"])})

    @app.get("/paper/{pid}/pdf")
    def pdf(pid: int):
        con = db()
        try:
            p = paper_or_404(con, pid)
        finally:
            con.close()
        path = (data / (p["pdf_path"] or "")).resolve()
        if not p["pdf_path"] or not path.is_file() or data.resolve() not in path.parents:
            raise HTTPException(404, "No PDF for this paper")
        return FileResponse(path, media_type="application/pdf")

    def act(pid, fn, *args):
        con = db()
        try:
            paper_or_404(con, pid)
            fn(con, pid, *args)
        except ValueError as err:
            raise HTTPException(400, str(err))
        finally:
            con.close()
        return RedirectResponse(f"/paper/{pid}", status_code=303)

    @app.post("/paper/{pid}/appeal")
    def appeal_open(pid: int, code: str = Form(...), text: str = Form("")):
        return act(pid, appeal, code, text, actor)

    @app.post("/paper/{pid}/appeal/{verdict}")
    def appeal_decide(pid: int, verdict: str, code: str = Form(...), reason: str = Form("")):
        if verdict not in ("grant", "deny"):
            raise HTTPException(404)
        return act(pid, decide_appeal, code, verdict == "grant", reason, actor)

    @app.get("/report")
    def report(request: Request):
        con = db()
        try:
            h = health(con, None, rules)
            past = len(papers(con, past=True))
        finally:
            con.close()
        return tpl.TemplateResponse(request, "report.html", {"h": h, "past": past, "rules": rules})

    @app.post("/paper/{pid}/{action}")
    def decide(pid: int, action: str, code: str = Form(...), reason: str = Form("")):
        if action not in ("dismiss", "restore"):
            raise HTTPException(404)
        return act(pid, dismiss if action == "dismiss" else restore, code, reason, actor)

    @app.get("/paper/{pid}/notice.txt")
    def notice_txt(pid: int):
        con = db()
        try:
            text = notice(con, paper_or_404(con, pid)) or ""
        finally:
            con.close()
        return PlainTextResponse(text)

    @app.get("/export.csv")
    def export():
        con = db()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "screening.csv"
                export_csv(con, out, None, rules)
                body = out.read_bytes()
        finally:
            con.close()
        return Response(body, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="screening.csv"'})

    return app


def create_author_app(rules) -> FastAPI:
    """The public door: authors upload one paper and get the checklist. No queue, no stored papers, so this
    one is safe to put on a reachable address, unlike the chair app."""
    from .precheck import ACCEPTED, MAX_UPLOAD, precheck

    app = FastAPI(title="Paper pre-check", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(actor="", css=CSS_VERSION, label=lambda c: c.replace("_", " ").capitalize())
    ctx = {"accept": ",".join(ACCEPTED), "max_mb": MAX_UPLOAD // 2**20}

    @app.get("/")
    def form(request: Request):
        return tpl.TemplateResponse(request, "author.html", {**ctx, "result": None, "error": None})

    @app.post("/")
    async def check(request: Request, paper: UploadFile = File(...)):
        body = await paper.read(MAX_UPLOAD + 1)
        try:
            result = precheck(body, paper.filename or "", rules)
        except ValueError as err:
            return tpl.TemplateResponse(request, "author.html", {**ctx, "result": None, "error": str(err)}, 400)
        for it in result["items"]:
            it["ev"] = evidence_items(it["evidence"])
        return tpl.TemplateResponse(request, "author.html", {**ctx, "result": result, "error": None,
                                                            "filename": paper.filename})

    return app
