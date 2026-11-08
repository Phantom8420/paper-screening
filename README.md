# paper-screening

Organizer-side screening for conference submissions in LaTeX and Word. It runs the
objective checks, links every flag to its evidence and keeps a full decision log.
It recommends; people decide. Design: [docs/DESIGN.md](docs/DESIGN.md).

**Online demo:** [paper-screening.vercel.app](https://paper-screening.vercel.app) shows seven made-up
submissions, already checked, each with one planted problem (author names in a blind paper, over the page
limit, a hidden prompt to AI reviewers, a copy of a past paper, an invented reference, a Word file). Adding
papers is switched off there, so nothing real is ever uploaded to it; clicks reset when the demo restarts.
`python demo/make_demo.py` rebuilds the sample files in `demo/`; `demo/data` holds their checked results,
which `api/index.py` serves on Vercel (`npx vercel deploy --prod`). A walkthrough video is in [demo/walkthrough.mp4](demo/walkthrough.mp4).

## Start here (no command line)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) once and start it.
2. Double-click **Start Screening.bat** (Windows) or **Start Screening.command** (Mac). The first start sets things up, which takes 10 to 20 minutes once; after that it opens in seconds.
3. Your browser opens the app. Click **Add papers**, choose the .zip from your submission system (and, if you have it, the author list it exports), and click **Add and check papers**.
4. Papers with problems come first. Open one to see each problem next to the paper, dismiss what is wrong, and copy the notice for the authors.

Keep the black window open while you work; closing it stops the app. Papers are kept between sessions.

**Status:** all milestones in the design doc (M1 to M7) are in: intake and conversion, format and compliance, references, similarity against this conference and past proceedings, quality signals, figure reuse, PDF integrity, scores and triage, appeals and the conference report.

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
- `import export.xlsx` reads the submission system's export (EasyChair .xlsx/.csv, HotCRP .json, or any CSV with a paper-number column), sets each paper's title and authors (matched by the number in the file or folder name) and re-checks those papers, so the anonymity check can look for the real author names. `ingest --meta export.xlsx` does both in one go.
- `check --camera-ready` checks final versions instead: anonymity is off, and it requires an author block that matches the imported author list, a copyright notice, embedded fonts and no review leftovers (line numbers, "anonymous", draft marks).
- `report` prints a one-screen health summary of the batch: how many papers are clear, worth a look or failing, the most common problems and whether authors can fix them, page counts and formats.
- `ingest --past old_proceedings.zip` (or the tick box on the Add papers page) stores published papers from earlier years. They are not checked or listed; every submission is compared with them, and text or figures found there give `published_duplicate` / `published_overlap` / `figure_reuse` with the source named. Each paper also gets a `similarity_sources` breakdown.
- Quoted text (“…” and block quotes) is left out of the similarity check, along with references and acknowledgments.
- Other checks per paper: tortured phrases ("counterfeit consciousness" for "artificial intelligence"), whether the paper states where its code and data are, ORCID iDs from the author list with a bad check digit, and a PDF integrity report (hidden or white text, hidden instructions aimed at AI reviewers, scripts, embedded files, encryption).
- Every paper gets a score (100, minus 30 per failed check and 8 per warning), six sub-scores and a recommendation: Ready for review, Needs fixes, Chair check (a problem only a person can judge, such as a duplicate or hidden text) or Desk-reject candidate. The thresholds are yours:

```toml
[triage]
review_at = 80      # Ready for review from this score
reject_below = 40   # Desk-reject candidate below this score

[scope]
topics = ["wireless networks", "signal processing"]   # flags papers that mention none of them

[quality]
require_code_statement = true   # warn when a paper says nothing about code or data
```

- `appeal <paper> <code> --reason "..."` records what the authors say about a flag; `--grant` or `--deny` with `--reason` decides it (a granted appeal dismisses the flag). The paper page has the same buttons.
- Similarity runs through providers. Only the local one exists and nothing leaves the machine. A provider that would send full text runs only if you name it, one by one, under `[similarity] send_full_text_to`.
- The **Conference report** page (and `report`) shows the recommendations, average sub-scores, the most common problems and who can fix them, page counts and formats.
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

Chair web UI (queue riskiest first, PDF beside each flag's evidence, dismiss/restore with a reason, notice drafts, and an Add papers page that uploads a batch and checks it):

```bash
pip install -e ".[web]"
python -m screening serve        # http://127.0.0.1:8000, keys: j/k move, Enter open, u back
```

There is no login yet, so it binds to localhost; `--by` sets the name recorded on decisions.

Author pre-check: `python -m screening serve --authors` serves one page where an author uploads their paper (.zip of LaTeX sources, .tex or Word) and gets the same checks as a checklist with how to fix each item. Nothing is stored and no reference lookups leave the machine.
