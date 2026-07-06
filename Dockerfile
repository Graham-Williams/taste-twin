# taste-twin web app — single container: gunicorn (ONE worker process;
# the job queue + politeness budget are process-local) with the pipeline
# worker as an in-process background thread.
FROM python:3.12-slim

# Non-root runtime user.
RUN useradd --create-home --uid 10001 tastetwin

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tastetwin/ ./tastetwin/

# All state (HTTP cache, pool.db, runs, kagglehub download) lives under
# /app/data — mount a volume there. KAGGLEHUB_CACHE keeps the ~600 MB
# dataset download inside the volume instead of the container layer.
ENV TASTETWIN_DATA=/app/data \
    KAGGLEHUB_CACHE=/app/data/kagglehub \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/data && chown -R tastetwin:tastetwin /app

USER tastetwin
EXPOSE 8080

# --workers MUST stay 1 (see top of file). Do NOT add --max-requests:
# recycling the worker process would kill an in-flight analysis job.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:8080", "--timeout", "120", \
     "--access-logfile", "-", "tastetwin.web:create_app()"]
