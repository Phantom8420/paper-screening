# paper-screening

Organizer-side screening for conference submissions in LaTeX and Word. It runs the
objective checks, links every flag to its evidence and keeps a full decision log.
It recommends; people decide. Design: [docs/DESIGN.md](docs/DESIGN.md).

**Status:** M1 (intake, format routing, conversion). The other checks follow the milestones in the design doc.

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
