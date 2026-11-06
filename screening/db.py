"""SQLite store. signal, decision and audit_log are append-only (enforced by triggers)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES batch(id),
  external_id TEXT, title TEXT,
  format TEXT NOT NULL CHECK(format IN ('latex','word','unknown')),
  status TEXT NOT NULL CHECK(status IN ('ingested','converted','needs_manual','checked')),
  source_dir TEXT, main_file TEXT, pdf_path TEXT, text_path TEXT,
  sections_json TEXT, page_count INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS author(
  id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES paper(id),
  position INTEGER, name TEXT, email TEXT, orcid TEXT, affiliation TEXT);
CREATE TABLE IF NOT EXISTS signal(
  id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES paper(id),
  check_name TEXT NOT NULL, check_version TEXT NOT NULL, code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info','warn','fail')),
  message TEXT NOT NULL, evidence_json TEXT, score REAL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision(
  id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES paper(id),
  actor TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('triage','accept','reject','override','appeal')),
  value TEXT, reason TEXT, signal_id INTEGER REFERENCES signal(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_score(
  id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES paper(id),
  reviewer TEXT, score REAL, confidence REAL, source TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcome(
  paper_id INTEGER PRIMARY KEY REFERENCES paper(id),
  final_decision TEXT NOT NULL, decided_at TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS app_user(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('chair','reviewer')));
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT, entity_id INTEGER, payload_json TEXT);
CREATE INDEX IF NOT EXISTS ix_paper_batch ON paper(batch_id);
CREATE INDEX IF NOT EXISTS ix_signal_paper ON signal(paper_id);
CREATE INDEX IF NOT EXISTS ix_decision_paper ON decision(paper_id);
""" + "".join(
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op} BEFORE {op.upper()} ON {t} "
    f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
    for t in ("signal", "decision", "audit_log")
    for op in ("update", "delete")
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def audit(con, actor, action, entity=None, entity_id=None, payload=None):
    con.execute(
        "INSERT INTO audit_log(ts, actor, action, entity, entity_id, payload_json) VALUES(?,?,?,?,?,?)",
        (now(), actor, action, entity, entity_id, json.dumps(payload) if payload is not None else None),
    )


def add_signal(con, paper_id, check, code, severity, message, evidence=None, score=None, version="1"):
    con.execute(
        "INSERT INTO signal(paper_id, check_name, check_version, code, severity, message, evidence_json, score,"
        " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (paper_id, check, version, code, severity, message, json.dumps(evidence or {}), score, now()),
    )
    audit(con, "system", "signal", "paper", paper_id, {"check": check, "code": code, "severity": severity})
