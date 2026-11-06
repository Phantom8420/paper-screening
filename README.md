# paper-screening

Organizer-side screening for conference submissions in LaTeX and Word. It runs the
objective checks, links every flag to its evidence and keeps a full decision log.
It recommends; people decide. Design: [docs/DESIGN.md](docs/DESIGN.md).

**Status:** M1 (intake, format routing, conversion) and M2 (format and compliance checks). The other checks follow the milestones in the design doc.

Compliance rules default to an IEEE conference (6 body pages + 2, double-blind). Override any of them with `--rules my_rules.toml`, e.g.

```toml
[pages]
max_body = 8
max_total = 10

[anonymization]
double_blind = false
```

`python -m screening check` re-runs the checks; earlier flags stay in the audit history and `status -v` shows only the latest run.

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
