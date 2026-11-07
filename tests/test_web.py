import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from screening.review import flags
from screening.web import create_app, first_page
from tests.test_review import setup


def test_queue_review_dismiss_restore_and_pdf_confinement(tmp_path):
    con, data, pid, _ = setup(tmp_path)
    web = TestClient(create_app(data, "chair"))

    q = web.get("/")
    assert q.status_code == 200 and "Reference undefined" in q.text and f"/paper/{pid}" in q.text

    page = web.get(f"/paper/{pid}")
    assert page.status_code == 200 and "nokey" in page.text  # evidence is shown, not just the verdict
    assert "Add the missing bibliography entries" in page.text
    assert web.get(f"/paper/{pid}?tab=notice").text.count("[REQUIRED]") == 1

    assert web.post(f"/paper/{pid}/dismiss", data={"code": "reference_undefined", "reason": " "}).status_code == 400
    r = web.post(f"/paper/{pid}/dismiss", data={"code": "reference_undefined", "reason": "style file defines it"},
                 follow_redirects=False)
    assert r.status_code == 303
    assert "reference_undefined" not in {s["code"] for s in flags(con, pid)[0]}
    hist = web.get(f"/paper/{pid}?tab=history").text
    assert "style file defines it" in hist and "chair" in hist

    web.post(f"/paper/{pid}/restore", data={"code": "reference_undefined", "reason": "second look"})
    assert "reference_undefined" in {s["code"] for s in flags(con, pid)[0]}

    assert web.get("/paper/999").status_code == 404
    assert web.post(f"/paper/{pid}/delete", data={"code": "x", "reason": "y"}).status_code == 404
    con.execute("UPDATE paper SET pdf_path='../../outside.pdf'")  # paper is not append-only
    con.commit()
    (tmp_path / "outside.pdf").write_bytes(b"%PDF-1.4")
    assert web.get(f"/paper/{pid}/pdf").status_code == 404
    assert web.get("/export.csv").text.startswith("﻿")


def test_first_page_picks_a_page_from_evidence():
    assert first_page({"pages": [3, 4]}) == 3
    assert first_page({"references_start_page": 7, "total_pages": 9}) == 7
    assert first_page({"undefined": ["x"]}) is None


def test_evidence_items_joins_plain_lists():
    from screening.web import evidence_items
    assert evidence_items({"pages": [2, 3], "truncated": True}) == [("pages", "2, 3")]
    assert evidence_items({"keys": list("abcdefghij")})[0][1].endswith("(+2 more)")
    assert evidence_items({"fields": {"creator": "X"}}) == [("fields", "creator: X")]
