"""Chair web UI: queue, paper review with evidence, dismiss/restore, notice drafts.

Runs locally (python -m screening serve) and binds to 127.0.0.1 by default: there is no login yet, so anyone
who can reach the port acts as the chair named by --by.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import connect
from .review import FIX_HINTS, dismiss, evidence, export_csv, flags, notice, papers, restore

HERE = Path(__file__).parent
PAGE_KEYS = ("page", "pages", "body_pages", "references_start_page", "total_pages")


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


def summary(con, p) -> dict:
    open_, dismissed = flags(con, p["id"])
    fails = sum(s["severity"] == "fail" for s in open_)
    return {"p": p, "open": open_, "dismissed": dismissed, "fails": fails, "warns": len(open_) - fails,
            "tier": "fail" if fails else "look" if open_ else "ok"}


def create_app(data: Path, actor: str) -> FastAPI:
    data = Path(data)
    app = FastAPI(title="Paper screening", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(actor=actor, label=lambda c: c.replace("_", " ").capitalize())

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
            rows = [summary(con, p) for p in papers(con)]
        finally:
            con.close()
        counts = {t: sum(r["tier"] == t for r in rows) for t in ("fail", "look", "ok")}
        counts["todo"] = counts["fail"] + counts["look"]
        rows.sort(key=lambda r: (-r["fails"], -r["warns"], r["p"]["id"]))
        if show in ("fail", "look", "ok"):
            rows = [r for r in rows if r["tier"] == show]
        elif show == "todo":
            rows = [r for r in rows if r["tier"] != "ok"]
        return tpl.TemplateResponse(request, "queue.html", {"rows": rows, "counts": counts, "show": show,
                                                           "total": counts["todo"] + counts["ok"]})

    @app.get("/paper/{pid}")
    def paper(request: Request, pid: int, tab: str = "flags"):
        con = db()
        try:
            p = paper_or_404(con, pid)
            s = summary(con, p)
            order = [r["p"]["id"] for r in sorted((summary(con, q) for q in papers(con)),
                                             key=lambda r: (-r["fails"], -r["warns"], r["p"]["id"]))]
            history = con.execute("SELECT * FROM audit_log WHERE entity='paper' AND entity_id=? AND actor!='system'"
                                  " ORDER BY id DESC", (pid,)).fetchall()
            history = [{**dict(h), "pl": json.loads(h["payload_json"] or "{}")} for h in history]
            draft = notice(con, p)
        finally:
            con.close()
        i = order.index(pid)
        items = [{"s": sig, "ev": evidence_items(evidence(sig)), "page": first_page(evidence(sig)),
                  "hint": FIX_HINTS.get(sig["code"])} for sig in s["open"]]
        gone = [{"s": sig, "page": first_page(evidence(sig))} for sig in s["dismissed"]]
        return tpl.TemplateResponse(request, "paper.html", {
            **s, "items": items, "gone": gone, "tab": tab, "history": history, "draft": draft,
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

    @app.post("/paper/{pid}/{action}")
    def decide(pid: int, action: str, code: str = Form(...), reason: str = Form("")):
        if action not in ("dismiss", "restore"):
            raise HTTPException(404)
        con = db()
        try:
            paper_or_404(con, pid)
            (dismiss if action == "dismiss" else restore)(con, pid, code, reason, actor)
        except ValueError as err:
            raise HTTPException(400, str(err))
        finally:
            con.close()
        return RedirectResponse(f"/paper/{pid}", status_code=303)

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
                export_csv(con, out)
                body = out.read_bytes()
        finally:
            con.close()
        return Response(body, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="screening.csv"'})

    return app


def create_author_app(rules) -> FastAPI:
    """The public door: authors upload one paper and get the checklist. No queue, no stored papers, so this
    one is safe to put on a reachable address, unlike the chair app."""
    from fastapi import File, UploadFile

    from .precheck import ACCEPTED, MAX_UPLOAD, precheck

    app = FastAPI(title="Paper pre-check", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(actor="", label=lambda c: c.replace("_", " ").capitalize())
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
