"""Vercel entry point: the chair app as a public demo with the sample papers from demo/make_demo.py, already
checked (Vercel has no LaTeX or Word converter, so the checks ran beforehand). Each server instance works on its
own copy in /tmp, so a chair's clicks there reset when the instance restarts."""
import shutil
from pathlib import Path

from screening.web import create_app

SEED = Path(__file__).resolve().parent.parent / "demo" / "data"
DATA = Path("/tmp/screening-demo")
if not (DATA / "screening.db").exists():
    shutil.copytree(SEED, DATA, dirs_exist_ok=True)

app = create_app(DATA, "Demo chair", demo=True)
