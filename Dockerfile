# OMR platform: FastAPI + OpenCV + LaTeX (pdflatex builds the answer sheets)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive

# LaTeX packages the sheet template needs: tikz/qrcode (pictures), enumitem/needspace (extra), microtype/eso-pic (recommended)
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-pictures texlive-fonts-recommended \
        texlive-plain-generic cm-super-minimal \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# non-root user; /storage holds the database, generated exams and cached sheet templates (mount a volume there)
RUN useradd --create-home app && mkdir -p /storage && chown -R app:app /storage /app
USER app

ENV OMR_HOST=0.0.0.0 \
    OMR_PORT=8000 \
    OMR_DB=/storage/platform.db \
    OMR_EXAMS_DIR=/storage/exams \
    OMR_FORMATS_DIR=/storage/formats

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# one process on purpose: batch-grading progress is kept in memory
CMD ["python", "server.py"]
