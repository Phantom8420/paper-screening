# Full conversion toolchain: TeX Live (incl. IEEEtran), latexmk, LibreOffice. Pandoc ships with pypandoc_binary.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        latexmk texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
        texlive-bibtex-extra texlive-publishers texlive-science \
        texlive-plain-generic texlive-fonts-extra texlive-font-utils lmodern texlive-lang-cjk texlive-lang-korean latex-cjk-common ghostscript biber libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m app
WORKDIR /app
COPY pyproject.toml ./
COPY screening ./screening
RUN pip install --no-cache-dir ".[web]" && mkdir /data && chown app /data
# a new named volume copies this ownership; without it the app user cannot create the database
USER app
ENV SCREENING_DATA=/data
VOLUME /data
ENTRYPOINT ["python", "-m", "screening"]
