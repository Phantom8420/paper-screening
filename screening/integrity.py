"""PDF integrity: what a reader cannot see but a program (or an AI reviewing assistant) can. Hidden text
(white, invisible render mode, or under 1 pt), instructions addressed to reviewers or language models,
JavaScript and launch actions, embedded files, encryption and incremental revisions. Every finding carries
the text and page it was found on; an all-clear is stored too, as the paper's integrity report."""
import math
import re
from pathlib import Path

from pypdf import PdfReader

from .db import add_signal, audit, start_run

VERSION = "1"
MAX_PAGES = 60
MIN_HIDDEN = 40  # characters; white labels inside a figure are short, a hidden paragraph is not
PROMPT = re.compile(r"ignore (?:all |any )?(?:previous|prior|above|earlier) instructions|give (?:this paper |it )?a "
                    r"(?:positive|favou?rable|high) (?:review|score|rating)|do not (?:highlight|mention) any "
                    r"(?:negatives|weaknesses)|recommend(?:ing)? (?:its |this paper for )?accept|"
                    r"(?:llm|ai|language model)s? (?:reviewing|reading) this|"
                    r"reviewers? (?:should|must) (?:accept|rate)", re.I)


def _white(op: bytes, args) -> bool | None:
    try:
        v = [float(a) for a in args]
    except (TypeError, ValueError):
        return None
    if op in (b"rg", b"g", b"sc", b"scn") and v:
        return min(v) >= 0.97
    if op == b"k" and len(v) == 4:
        return max(v) <= 0.03
    return None


def hidden_text(page) -> tuple[str, str]:
    """(hidden text, all text) of one page. Text inside figures (form XObjects) is left out: exported diagrams
    routinely carry white labels on dark boxes and an invisible search layer over outlined glyphs."""
    state = {"white": False, "invisible": False, "hidden_ops": 0, "shown_ops": 0, "depth": 0}
    stack, saved, hidden = [], [], []

    def before(op, args, cm, tm):
        if op == b"Do":
            saved.append((state["white"], state["invisible"], len(stack)))
            state["depth"] += 1
        elif op == b"q":
            stack.append((state["white"], state["invisible"]))
        elif op == b"Q" and stack:
            state["white"], state["invisible"] = stack.pop()
        elif op == b"Tr" and args:
            state["invisible"] = int(args[0]) == 3
        elif (w := _white(op, args)) is not None:
            state["white"] = w
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            state["hidden_ops" if state["white"] or state["invisible"] else "shown_ops"] += 1

    def after(op, args, cm, tm):
        if op == b"Do" and saved:
            state["white"], state["invisible"], n = saved.pop()
            del stack[n:]
            state["depth"] -= 1
            state["hidden_ops"] = state["shown_ops"] = 0

    def text(t, cm, tm, font, size):
        # shortcut: pypdf hands over text per line, not per operator, so a line counts as hidden when most of
        # its pieces were drawn hidden; upgrade to per-glyph decoding if mixed lines turn out to matter
        tiny = size and abs(size) * math.sqrt(abs(tm[0] * tm[3] - tm[1] * tm[2])) *             math.sqrt(abs(cm[0] * cm[3] - cm[1] * cm[2])) < 1.0
        if not state["depth"] and t.strip() and (state["hidden_ops"] > state["shown_ops"] or tiny):
            hidden.append(t)
        state["hidden_ops"] = state["shown_ops"] = 0

    every = page.extract_text(visitor_operand_before=before, visitor_operand_after=after, visitor_text=text) or ""
    return " ".join(" ".join(hidden).split()), every


def _active(reader) -> dict:
    root = reader.trailer["/Root"]
    names = root.get("/Names") or {}
    names = names.get_object() if hasattr(names, "get_object") else names
    js = "/JavaScript" in names or "/AA" in root
    oa = root.get("/OpenAction")
    oa = oa.get_object() if hasattr(oa, "get_object") else oa
    js = js or (isinstance(oa, dict) and oa.get("/S") in ("/JavaScript", "/Launch"))
    launch = []
    for n, page in enumerate(reader.pages[:MAX_PAGES], 1):
        for a in page.get("/Annots") or []:
            a = a.get_object()
            act = a.get("/A")
            act = act.get_object() if hasattr(act, "get_object") else act
            if isinstance(act, dict) and act.get("/S") in ("/JavaScript", "/Launch"):
                launch.append(n)
    embedded = "/EmbeddedFiles" in names
    return {"javascript": bool(js), "action_pages": sorted(set(launch)), "embedded_files": embedded,
            "xfa_form": "/XFA" in (root.get("/AcroForm") or {})}


def run_integrity(con, paper_id: int, data_dir) -> None:
    row = con.execute("SELECT pdf_path FROM paper WHERE id=?", (paper_id,)).fetchone()
    if not row["pdf_path"]:
        return
    path = Path(data_dir) / row["pdf_path"]
    run = start_run(con, paper_id, "integrity", VERSION)
    try:
        report = _check(con, run, path)
        add_signal(con, run, "integrity_report", "info", "PDF integrity checks ran.", report)
    except Exception as e:  # noqa: BLE001  pypdf raises many types on malformed PDFs
        add_signal(con, run, "integrity_unchecked", "warn", "The PDF could not be inspected for hidden content.",
                   {"error": f"{type(e).__name__}: {str(e)[:300]}"})
    audit(con, "system", "integrity", "paper", paper_id, {"run_id": run})
    con.commit()


def _check(con, run, path: Path) -> dict:
    raw = path.read_bytes()
    reader = PdfReader(path)
    encrypted = reader.is_encrypted
    if encrypted:
        add_signal(con, run, "pdf_encrypted", "warn", "The PDF is encrypted; printing, copying or text extraction may be"
                   " restricted, which hides it from checks.")
        reader.decrypt("")
    act = _active(reader)
    if act["javascript"] or act["action_pages"] or act["xfa_form"]:
        add_signal(con, run, "pdf_active_content", "warn", "The PDF contains scripts or actions that run when it is "
                   "opened or clicked; a paper needs none.", {k: v for k, v in act.items() if k != "embedded_files"}
                   | {"pages": act["action_pages"]})
    if act["embedded_files"]:
        add_signal(con, run, "pdf_embedded_files", "warn", "The PDF carries attached files.")
    hidden, prompts = [], []
    for n, page in enumerate(reader.pages[:MAX_PAGES], 1):
        h, every = hidden_text(page)
        if len(h) >= MIN_HIDDEN or (h and PROMPT.search(h)):
            hidden.append({"page": n, "text": h[:400]})
        runs = []  # (start, stop, hidden) per passage; matches whose context overlaps become one entry
        for m in PROMPT.finditer(every):
            start, stop = max(0, m.start() - 80), m.end() + 80
            hit = " ".join(m.group().split()).lower() in h.lower()
            if runs and start <= runs[-1][1]:
                runs[-1] = (runs[-1][0], stop, runs[-1][2] or hit)
            else:
                runs.append((start, stop, hit))
        prompts += [{"page": n, "hidden": hit, "context": " ".join(every[a:b].split())} for a, b, hit in runs]
    if any(p["hidden"] for p in prompts):
        add_signal(con, run, "hidden_prompt", "fail", "Hidden text in the PDF gives instructions to reviewers or AI "
                   "tools (prompt injection).", {"pages": sorted({p["page"] for p in prompts if p["hidden"]}),
                                                 "found": prompts[:5], "hidden_text": hidden[:5]})
    elif prompts:
        add_signal(con, run, "prompt_text", "warn", "The text addresses reviewers or AI tools directly; check the "
                   "context.", {"pages": sorted({p["page"] for p in prompts}), "found": prompts[:5]})
    if hidden and not any(p["hidden"] for p in prompts):
        add_signal(con, run, "hidden_text", "warn", f"Text a reader cannot see (white, invisible or under 1 pt) on "
                   f"{len(hidden)} page(s).", {"pages": [h["page"] for h in hidden], "hidden_text": hidden[:5]})
    return {"pages_scanned": min(len(reader.pages), MAX_PAGES), "encrypted": encrypted, **act,
            "pages_with_hidden_text": [h["page"] for h in hidden], "revisions": max(1, raw.count(b"%%EOF"))}
