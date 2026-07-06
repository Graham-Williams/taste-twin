"""Flask web app wrapping the taste-twin pipeline.

Factory: :func:`create_app`. Serve with a SINGLE gunicorn worker process
(``gunicorn -w 1 --threads 8 'tastetwin.web:create_app()'``) — the job
queue and its 1 req/s politeness budget are process-local, so multiple
worker processes would mean concurrent scrape jobs.
"""

from .app import create_app

__all__ = ["create_app"]
