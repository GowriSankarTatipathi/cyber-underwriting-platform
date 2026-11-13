FROM python:3.13-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr postgresql-client-17 clamav clamav-freshclam ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt && useradd --uid 10001 --create-home app
COPY --chown=app:app . /app
RUN mkdir -p /app/.local /app/staticfiles && chown -R app:app /app/.local /app/staticfiles
USER app
CMD ["gunicorn", "pilot.wsgi:application", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=120", "--access-logfile=-", "--access-logformat=%(t)s %(s)s %(L)s"]
