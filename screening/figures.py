"""Figure reuse across papers: each embedded image gets a 64-bit difference hash (dHash), which survives
rescaling, recompression and small colour changes. Two papers sharing a near-identical image (Hamming
distance up to MAX_DIST) are flagged with both page numbers. Images found in many papers (publisher logos,
template art) are ignored, like boilerplate wording in the text check."""
import json
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

from .db import add_signal, audit, start_run

VERSION = "1"
MAX_PAGES, MAX_IMAGES = 60, 60
MIN_SIDE = 64  # pixels; icons and bullets are not figures
MAX_DIST = 5
BANDS = 8  # 8-bit bands: two hashes within distance 7 share at least one band exactly
COMMON = 3  # an image in more papers than this is a logo or template art


def dhash(img) -> int:
    from PIL import Image
    g = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    return sum(1 << i for i, (r, c) in enumerate((r, c) for r in range(8) for c in range(8))
               if px[r * 9 + c] > px[r * 9 + c + 1])


def index_figures(con, paper_id: int, data_dir) -> int:
    """Store the hashes of a paper's images, replacing earlier ones. Returns how many were stored."""
    row = con.execute("SELECT pdf_path FROM paper WHERE id=?", (paper_id,)).fetchone()
    found = []
    if row["pdf_path"]:
        try:
            reader = PdfReader(Path(data_dir) / row["pdf_path"])
            for n, page in enumerate(reader.pages[:MAX_PAGES], 1):
                for im in page.images:
                    if len(found) >= MAX_IMAGES:
                        break
                    try:
                        pic = im.image
                        if min(pic.size) < MIN_SIDE:
                            continue
                        h = dhash(pic)
                    except Exception:  # noqa: BLE001  an image pypdf/Pillow cannot decode is skipped, not fatal
                        continue
                    if 8 <= h.bit_count() <= 56:  # near-flat images (blank, solid fill) hash alike by chance
                        found.append((paper_id, n, h - (1 << 63)))  # SQLite integers are signed 64-bit
        except Exception:  # noqa: BLE001  unreadable PDFs are flagged by the compliance check already
            pass
    con.execute("DELETE FROM figure_hash WHERE paper_id=?", (paper_id,))
    con.executemany("INSERT INTO figure_hash(paper_id, page, hash) VALUES(?,?,?)", found)
    con.commit()
    return len(found)


def _bands(h: int):
    return [(b, (h >> (8 * b)) & 0xFF) for b in range(BANDS)]


def run_figures(con, paper_ids) -> set:
    info = {r["id"]: r for r in con.execute("SELECT p.id, p.external_id, b.name AS batch, b.kind FROM paper p"
                                            " JOIN batch b ON b.id = p.batch_id")}
    hashes = [(r["paper_id"], r["page"], r["hash"] + (1 << 63)) for r in con.execute("SELECT * FROM figure_hash")]
    buckets = defaultdict(list)
    for i, (_, _, h) in enumerate(hashes):
        for band in _bands(h):
            buckets[band].append(i)
    pairs = defaultdict(list)  # paper -> [(page, other, other page, distance)]
    seen = set()
    for i, (pid, page, h) in enumerate(hashes):
        for band in _bands(h):
            for j in buckets[band]:
                if j <= i or (i, j) in seen:
                    continue
                seen.add((i, j))
                oid, opage, oh = hashes[j]
                same_sub = info[pid]["external_id"] and info[pid]["external_id"] == info[oid]["external_id"]
                if oid != pid and not same_sub and (d := (h ^ oh).bit_count()) <= MAX_DIST:
                    pairs[(pid, h)].append((oid, opage, d, page))
                    pairs[(oid, oh)].append((pid, page, d, opage))
    hits = defaultdict(list)
    for (pid, _), group in pairs.items():
        if len({o for o, *_ in group}) < COMMON:
            hits[pid] += [{"page": page, "other_paper": o, "other_external_id": info[o]["external_id"],
                           "other_batch": info[o]["batch"], "other_page": op, "distance": d,
                           "source": "past proceedings" if info[o]["kind"] == "past" else "this conference"}
                          for o, op, d, page in group]
    targets = {p for p in paper_ids if info.get(p) and info[p]["kind"] != "past"}
    targets |= {m["other_paper"] for p in targets for m in hits.get(p, []) if info[m["other_paper"]]["kind"] != "past"}
    indexed = {r[0] for r in con.execute("SELECT DISTINCT paper_id FROM figure_hash")}
    for pid in sorted(targets):
        run = start_run(con, pid, "figures", VERSION, {"max_distance": MAX_DIST})
        found = sorted({json.dumps(m, sort_keys=True) for m in hits.get(pid, [])})  # one row per image pair
        found = [json.loads(m) for m in found]
        if found:
            others = sorted({m["other_paper"] for m in found})
            add_signal(con, run, "figure_reuse", "warn",
                       f"{len(found)} image(s) also appear in {len(others)} other paper(s): check whether the figure "
                       "is reused or shared legitimately.",
                       {"pages": sorted({m["page"] for m in found}), "matches": found[:20]})
        elif pid in indexed:
            add_signal(con, run, "figures_ok", "info", "No image matches another paper.")
        audit(con, "system", "figures", "paper", pid, {"matches": len(found)})
    con.commit()
    return targets
