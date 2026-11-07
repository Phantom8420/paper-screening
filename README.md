# paper-screening

Organizer-side screening for conference submissions in LaTeX and Word. It runs the
objective checks, links every flag to its evidence and keeps a full decision log.
It recommends; people decide. Design: [docs/DESIGN.md](docs/DESIGN.md).

**Status:** M1 (intake, format routing, conversion), M2 (format and compliance checks) and M3 (reference verification). The other checks follow the milestones in the design doc.

Compliance rules default to an IEEE conference (6 body pages + 2, double-blind). Override any of them with `--rules my_rules.toml`, e.g.

```toml
[pages]
max_body = 8
max_total = 10

[anonymization]
double_blind = false
```

`python -m screening check` re-runs the checks (and finishes converting any papers an interrupted ingest left behind); earlier flags stay in the audit history and `status -v` shows only the latest run. `--jobs N` sets how many papers are converted and checked at once (default: up to 4). `status` ends with a count of each flag across the batch.

Measured on a laptop with a synthetic 300-paper batch (half LaTeX, half Word), offline: ingest and all checks in about 45 s, a re-check in about 20 s. Online lookups dominate a real run: roughly 10 to 30 s per paper depending on how many references resolve.

### Reference checks

[paper-preflight](https://pypi.org/project/paper-preflight/) checks every citation against the bibliography and looks each reference up in Crossref, dblp, arXiv, DataCite, OpenAlex, doi.org and PubMed, including retraction notices. Only reference metadata (title, authors, DOI) is sent, never the paper text. Word papers with numbered `[n]` citations also get a cited-vs-listed check. Flags: undefined citations, references not found or pointing to another work, retracted or withdrawn works, mismatched details, uncited entries and references that could not be verified (a person checks those).

- `--offline` skips the lookups and only checks citations against the bibliography.
- Set `PAPER_PREFLIGHT_EMAIL` to a contact address to use Crossref's faster "polite pool"; without it Crossref may rate-limit and the paper gets a `reference_check_incomplete` warning. `OPENALEX_API_KEY` is optional.
- Lookups run one paper at a time (each paper's references are queried concurrently), to stay inside the indexes' rate limits.
- Lookups are cached in `data/reference_cache.sqlite3`.
- Sources that would contact authors' own servers (`web`, `wayback`) and Semantic Scholar (`s2`, separate licence) are off by default:

```toml
[references]
disabled_sources = ["web", "wayback"]   # turns s2 back on
```

### Working through a batch

- `status --sort risk` puts papers with the most failures first; `status -v` adds each flag's message and, where the authors can fix it, a one-line fix.
- `dismiss <paper> <code> --reason "..."` hides a flag you checked and judged wrong. It stays hidden on re-runs as long as its evidence is the same, and comes back if the evidence changes (a new email address, another page over). `restore` undoes it. Both are recorded with who and why.
- A LaTeX paper that fails only because this machine lacks a package gets `latex_package_missing` (naming the package) instead of a compile failure, and never reaches an author notice. Install the package and run `check`.
- `check` ends with what changed per paper since the last run (`+new_flag`, `-resolved_flag`), so a resubmitted batch shows at a glance who fixed what.
- `notices` writes one plain-text fix request per paper into `data/notices/`, listing only problems the authors can fix (tool gaps and lookups that did not finish stay with the chairs), with the evidence. Nothing is sent.
- Every paper is compared with every other paper in the database, across batches and tracks (references and acknowledgments left out, wording shared by many papers ignored). `duplicate_submission` (fail, 70%+ of the shorter paper's text also in another paper) and `overlapping_submission` (warn, 10%+; on real ICC papers unrelated pairs share under 2% and a paper and its extended version 13%) show the other paper, its batch and the shared passages. A fail needs both texts to be of comparable length (a paper whose text was only partly extracted gets a warning instead), and a paper ingested again under the same submission id counts as a revision, not a duplicate. Thresholds are in `[duplicates]` of the rules file. These flags are for chairs only and never go into author notices. 300 papers compare in about 2 s.
- `export --out screening.csv` writes one row per paper (fails, warnings, dismissed, flags, whether the authors have something to fix) for a spreadsheet.

## Run

Full toolchain (LaTeX, LibreOffice) in Docker:

```bash
docker build -t paper-screening .
docker run --rm -v "$PWD/data:/data" -v "$PWD/incoming:/in:ro" paper-screening ingest /in/batch.zip
```

Locally, without LaTeX/LibreOffice (text extraction works; PDFs are marked "needs manual look"):

```bash
pip install -e ".[dev]"
python -m screening ingest batch.zip paper.docx
python -m screening status
pytest
```

Chair web UI (queue riskiest first, PDF beside each flag's evidence, dismiss/restore with a reason, notice drafts, dark and light themes):

```bash
pip install -e ".[web]"
python -m screening serve        # http://127.0.0.1:8000, keys: j/k move, Enter open, u back
```

There is no login yet, so it binds to localhost; `--by` sets the name recorded on decisions.
