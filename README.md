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
- `check` ends with what changed per paper since the last run (`+new_flag`, `-resolved_flag`), so a resubmitted batch shows at a glance who fixed what.
- `notices` writes one plain-text fix request per paper into `data/notices/`, listing only problems the authors can fix (tool gaps and lookups that did not finish stay with the chairs), with the evidence. Nothing is sent.
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
