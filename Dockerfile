# Full conversion toolchain: TeX Live (incl. IEEEtran), latexmk, LibreOffice. Pandoc ships with pypandoc_binary.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        latexmk texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
        texlive-bibtex-extra texlive-publishers biber libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m app
WORKDIR /app
COPY pyproject.toml ./
COPY screening ./screening
RUN pip install --no-cache-dir .
USER app
ENV SCREENING_DATA=/data
VOLUME /data
ENTRYPOINT ["python", "-m", "screening"]
