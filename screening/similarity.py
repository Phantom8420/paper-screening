"""Similarity providers. "local" (shingles over every paper in the database and the past proceedings) is the
default and sends nothing anywhere. Any other provider (a commercial plagiarism service, an institutional
index) plugs in here with `sends_full_text = True`, and it only runs once the chairs list it in
[similarity] send_full_text_to: unpublished papers are confidential, so that is an explicit, per-provider
approval in the rules file, never a default.

A provider is any object with `name`, `sends_full_text` and `run(con, paper_ids, rules) -> set[int]` (the ids
it stored a run for), storing its findings as signals with evidence like every other check.
"""
from dataclasses import dataclass
from typing import Callable

from .db import add_signal, start_run
from .duplicates import run_duplicates


@dataclass(frozen=True)
class Provider:
    name: str
    sends_full_text: bool
    run: Callable


PROVIDERS = {"local": Provider("local", False, run_duplicates)}


def run_similarity(con, paper_ids, rules) -> set:
    cfg = rules["similarity"]
    done = set()
    for name in cfg["providers"]:
        p = PROVIDERS.get(name)
        if p is None or (p.sends_full_text and name not in cfg["send_full_text_to"]):
            why = "is not installed" if p is None else "would receive the full text and is not approved in " \
                                                         "[similarity] send_full_text_to"
            for pid in paper_ids:
                add_signal(con, start_run(con, pid, f"similarity:{name}"), "similarity_provider_skipped", "warn",
                           f"Similarity provider '{name}' {why}, so it did not run.", {"provider": name})
            con.commit()
            continue
        done |= p.run(con, paper_ids, rules)
    return done
