FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
# Tesseract lets PyMuPDF OCR scanned exam papers.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# The uploads directory must exist, owned by the app user, before a volume is mounted on it:
# Docker initialises an empty named volume from the image's mount point, so without this the
# volume comes up root-owned and the non-root process can't write uploaded papers.
RUN useradd --system --uid 10001 eduhub \
    && mkdir -p /srv/var/uploads \
    && chown -R eduhub:eduhub /srv/var
USER eduhub

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers"]
