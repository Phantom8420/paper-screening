# Paper Screening Platform — Design (Phase 1)

An organizer-side screening layer. It takes mixed LaTeX and Word batches, runs the
objective checks, attaches evidence to every flag, and gives chairs a ranked queue.
The system **recommends**; a person confirms every accept or reject.

## Decisions taken (2026-11-07)

| Question | Decision |
|---|---|
| Volume | about 300 papers per conference, mixed LaTeX and Word |
| Paid APIs | none for now; everything runs locally |
| Reference lookups | free public APIs (Crossref, OpenAlex, dblp, Semantic Scholar), **reference metadata only**, cached locally, one switch turns them off |
| Template | IEEE conference, 6 pages + 2 pages of references (configurable) |
| Review model | double-blind, so failing anonymization is an objective failure |
| Hosting | self-hosted on one machine with Docker Compose |
| Users | 2–5 chairs with simple roles (chair, reviewer) |
| Themes, past outcomes | not supplied yet: scope matching stays off and the outcome tables start empty |

What local-only similarity cannot do: it catches duplicates inside the current pool and
against past proceedings we hold. It does **not** catch copying from the open web or from
other publishers. A paid provider can be added behind the same interface later.

## Architecture

```
            bulk zip / files
                  |
            +-----v------+      +-----------------------------+
            |   Intake   |----->| storage/ (originals, never  |
            | sniff+group|      | modified; derived files)    |
            +-----+------+      +-----------------------------+
                  | latex queue / word queue / unknown
            +-----v------+   pandoc (bundled), latexmk, LibreOffice
            | Conversion |   all run sandboxed, with timeouts
            +-----+------+   -> text, sections, PDF, page count
                  |
   +--------------+---------------------------------------------+
   |  Check runners (each one: Check interface -> Signals)      |
   |  compliance | references | similarity | quality            |
   |        |            |             |                        |
   |   pypdf/rules  Provider adapters   Local index  Phrase list,|
   |               (paper-preflight,  (shingles/    embeddings,  |
   |               GROBID, Crossref,  MinHash)      image hashes |
   |               OpenAlex, dblp, S2)                           |
   +--------------+---------------------------------------------+
                  | signals (append-only, versioned, with evidence)
            +-----v------+
            |  Scoring   |  rule-based sub-scores -> strength tier
            |  + Triage  |  Clear Pass / Needs Review / Failed Check
            +-----+------+
                  |
   +--------------v----------------+    +-------------------------+
   | Web UI (FastAPI + Jinja2)     |--->| decisions + audit_log   |
   | queue, evidence, decisions,   |    | (append-only; exports)  |
   | appeals, dashboard, reports   |    +-------------------------+
   +-------------------------------+
```

Every arrow goes through SQLite. Checks never call each other: each reads the converted
paper and writes signals. That keeps checks swappable and lets Phase 2 models read the
stored signals as features.

## Tech stack, with reasons

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | GROBID clients, paper-preflight, sentence-transformers and pypdf are all Python |
| Store | SQLite (WAL) via stdlib `sqlite3` | 300 papers per conference fits easily in one file; no server; the whole database is one exportable file. Move to Postgres only with multi-tenancy (Phase 2.6) |
| Web | FastAPI + Jinja2 templates (+ htmx) | the queue is server-rendered tables and forms, so no SPA build step |
| Jobs | in-process worker thread | 300 papers do not need Celery or Redis |
| Conversion | pandoc (`pypandoc_binary`), latexmk/TeX Live, LibreOffice headless | the formats require them; TeX and LibreOffice live in the Docker image |
| Reference parsing | `.bib`/`.bbl` directly for LaTeX; GROBID (Docker) for Word/PDF | GROBID is the most accurate open reference parser |
| PDF reading | `pypdf` | BSD licence. PyMuPDF is avoided because of AGPL |
| Embeddings | sentence-transformers, small local model | scope match runs offline, so no abstract leaves the machine |
| Tests | pytest | |

## Reuse candidates, licences, data handling

| Tool | Use | Licence / terms | Data sent |
|---|---|---|---|
| [paper-preflight](https://pypi.org/project/paper-preflight/) | reference existence checks for .tex/.bib (Crossref, dblp, arXiv, DataCite, PubMed, OpenAlex) | MIT | reference metadata only |
| [CheckIfExist](https://arxiv.org/abs/2602.15871) (zabbonat/References-Validation) | **not wrapped**: it is a Node web app, not a library, and an independent comparison reported many false positives. Its design (multi-source consensus) informs ours | see repo | n/a |
| GROBID | reference and header parsing for Word/PDF | Apache-2.0, self-hosted | nothing leaves the machine |
| pandoc | text and section extraction | GPL-2.0+, called as a separate program | local |
| Crossref REST API | DOI/title lookup, retraction notices (Retraction Watch data is distributed by Crossref) | free; "polite pool" with a contact email; metadata largely without restrictions | reference metadata |
| OpenAlex | title/DOI lookup | data CC0; **API key required since 2026-02-13**, free daily budget | reference metadata |
| dblp | computer-science bibliography lookup | data CC0 | reference metadata |
| Semantic Scholar API | title lookup | free under the S2 API licence agreement; a key is recommended | reference metadata |
| ManuscriptLint | possible LaTeX structural lint (labels, unresolved refs) | MIT | local |

Licence terms change: each adapter records the provider version and terms URL, and this
table must be rechecked before a public release.

## Data model (SQLite)

```
batch(id, name, created_at)
paper(id, batch_id, external_id, title, format[latex|word|unknown], status
      [ingested|converted|needs_manual|checked], source_dir, main_file,
      pdf_path, text_path, sections_json, page_count, created_at)
author(id, paper_id, position, name, email, orcid, affiliation)
signal(id, paper_id, check, check_version, code, severity[info|warn|fail],
       message, evidence_json, score, created_at)      -- append-only
decision(id, paper_id, actor, kind[triage|accept|reject|override|appeal],
         value, reason, signal_id, created_at)        -- append-only
review_score(id, paper_id, reviewer, score, confidence, source, created_at)
outcome(paper_id, final_decision, decided_at, source)
user(id, name, role[chair|reviewer])
audit_log(id, ts, actor, action, entity, entity_id, payload_json) -- append-only
```

- **Reversible flags:** signals are never edited or deleted. A human override is a
  `decision` row pointing at the signal. The current view is computed from both.
- **Phase 2 readiness:** signals carry `check_version` and `score`, so they become a feature
  table. `review_score` and `outcome` hold the labels the probability model needs.
  Authors carry ORCID and affiliation, which conflict-of-interest detection will need.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Design, scaffold, private repo | this document; repo on GitHub |
| M1 | Intake and format segregation, conversion | zip or files go in; each paper gets a format queue, text, sections and PDF, or a "needs manual look" signal with the reason; tests |
| M2 | Compliance checks | page limit, required sections, abstract/keywords, anonymization (body and file metadata), IEEE template markers |
| M3 | Reference verification | parse via .bib/.bbl or GROBID; adapters for paper-preflight, Crossref, OpenAlex, dblp, S2; cache; flags for unresolvable, mismatched, uncited, undefined keys, retracted |
| M4 | Similarity (local) | shingle/MinHash index of the pool and past proceedings; references and quotes excluded; per-source breakdown; provider interface |
| M5 | Quality signals | tortured phrases, scope match (once themes are supplied), code/data availability statements, basic figure reuse (perceptual hash) |
| M6 | Scoring, triage, review UI | strength tier with sub-scores, triage labels with thresholds you set, queue sorted by risk, evidence view, decisions, overrides, appeals, audit |
| M7 | Reports and interop | author feedback reports, CSV import/export (columns taken from a real export from your submission system), ORCID, chair dashboard, PDF integrity report |

## Security notes

- Submissions are untrusted input. LaTeX is compiled with `-no-shell-escape`, restricted
  file I/O (`openin_any=p`, `openout_any=p`) and a timeout, in a throwaway directory.
- Zip extraction rejects absolute and `..` paths, symlinks, too many entries, and
  oversized archives (zip bombs).
- Originals are stored unmodified; nothing is sent to third parties except reference
  metadata, and that can be switched off.
