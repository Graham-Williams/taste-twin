# Deploying taste-twin

Runbook for the home server (same box and posture as `todoist-points` /
`km-tracker`). The app runs 24/7 in one Docker container, joins the existing
Cloudflare tunnel's network, and is public at
**https://taste-twin.graham-williams.com** behind a Cloudflare Access app
(one-time PIN). The container also verifies the Access JWT itself, so a
sibling container on the shared network can't reach it unauthenticated.

## Layout on the box

- Repo checkout: `~/taste-twin` (deploy from `main` only)
- Secrets: `~/taste-twin/.env` (untracked; copy from `.env.example` and fill
  in the Access app's AUD tag + team domain)
- Data: named Docker volume `taste-twin_taste-twin-data`, mounted at
  `/app/data` in the container — HTTP cache, `pool.db`, `runs/<user>/`
  (job state + logs + reports), and the kagglehub download cache
  (`KAGGLEHUB_CACHE=/app/data/kagglehub`).

## Deploy / update

```bash
cd ~/taste-twin
git pull                      # main only
docker compose up -d --build
```

There is no staging instance, and no in-progress-game concern like
km-tracker — but **avoid redeploying while a run is mid-flight** if you can:
a restart kills the running job (it is marked failed with a Re-run button;
the week-long HTTP cache makes the re-run cheap).

## First boot (one-time)

`data/pool.db` is missing on a fresh volume, so the worker automatically
downloads the CC0 Kaggle dataset (~600 MB, anonymous — no Kaggle account)
and builds the SQLite pool before taking the first job. Watch it:

```bash
docker compose logs -f taste-twin
```

The homepage shows "One-time setup in progress" until it finishes (a few
minutes on decent bandwidth). If the download fails (state shows
`error: ...` on the homepage), manual fallback: fetch the zip from
https://www.kaggle.com/datasets/freeth/letterboxd-film-ratings and unzip
`ratings.csv` + `films.csv` into the volume at `data/dataset/`:

```bash
docker cp ratings.csv taste-twin:/app/data/dataset/
docker cp films.csv  taste-twin:/app/data/dataset/
```

then enqueue a run (ingest retries automatically).

## Checks & logs

```bash
docker compose ps                                  # healthcheck hits /healthz
docker compose logs -f taste-twin                  # gunicorn + worker + ingest
docker exec taste-twin ls /app/data/runs           # per-user run dirs
docker exec taste-twin cat /app/data/runs/<u>/job.log   # one run's pipeline log
```

## Cloudflare side (managed by Hopper via API, not this repo)

One-time, before first deploy:

1. Access application for `taste-twin.graham-williams.com` (self-hosted,
   one-time PIN, Graham-only allow-list) → note its **AUD** for `.env`.
2. Tunnel ingress rule on the `km-tracker` tunnel (before the catch-all
   404): `taste-twin.graham-williams.com` → `http://taste-twin:8080`.
3. Proxied CNAME `taste-twin` → `<tunnel-id>.cfargotunnel.com`.

Remember the cert gotcha: single-label subdomains only.

## Invariants

- **No host port mapping** — the app is reachable only via the tunnel
  network; in-app Access-JWT verification is the second lock.
- **One gunicorn worker process** (`--workers 1`) — the FIFO job queue and
  the 1 req/s scraping budget are process-local.
- **No off-box backup yet** — the volume holds only re-derivable data
  (dataset + scrape cache + reports), so a disk loss costs re-runs, not
  data. Revisit if that changes.
