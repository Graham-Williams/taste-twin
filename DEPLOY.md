# Deploying taste-twin

Runbook for the home server (same box and posture as `todoist-points` /
`km-tracker`). The app runs 24/7 in one Docker container, joins the existing
Cloudflare tunnel's network, and is public at
**https://taste-twin.graham-williams.com** behind a Cloudflare Access app
(one-time PIN). The container also verifies the Access JWT itself, so a
sibling container on the shared network can't reach it unauthenticated.

## View-only architecture (box hosts, Mac generates)

Letterboxd is behind Cloudflare bot management that serves a JS challenge to
the **box's** server IP, so live scraping from the box fails. Graham's Mac has
a residential IP that is not challenged. So the box runs in **view-only mode**
(`TASTE_TWIN_VIEWER_MODE=1`) — a read-only gallery of pre-generated reports:
the homepage username form is hidden, `POST /run` is refused, and no background
worker or ~600 MB dataset ingest runs (so `pool.db` isn't even required on the
box). New reports are generated on the Mac and pushed over:

```bash
# On Graham's Mac, from the repo root (residential IP, full mode):
python scripts/publish.py <letterboxd_username>
#   [--box graham@100.101.1.28] [--container taste-twin]
#   [--url-base https://taste-twin.graham-williams.com]
```

`publish.py` validates the username (same rule as the app), runs the pipeline
locally (`python -m tastetwin run <user>`), then copies just `report.html` +
`matches_verified.json` into the container's `data/runs/<user>/`, clears any
stale job state so the UI shows a completed run, and prints the public URL. It
uses only subprocess arg lists (never a shell), since the username reaches
ssh/docker/scp. Requires SSH access to the box and Docker there.

`TASTE_TWIN_VIEWER_MODE` is set to `"1"` in `docker-compose.yml` (and mirrored
in `.env.example`). To run a full, self-scraping instance instead (e.g. local
dev on a residential IP), leave the flag unset/`0`.

## Layout on the box

- Repo checkout: `~/taste-twin` (deploy from `main` only)
- Secrets: `~/taste-twin/.env` (untracked; copy from `.env.example` and fill
  in the Access app's AUD tag + team domain, plus the shared-password gate
  vars once you cut over — see next section)
- Data: named Docker volume `taste-twin_taste-twin-data`, mounted at
  `/app/data` in the container — HTTP cache, `pool.db`, `runs/<user>/`
  (job state + logs + reports), and the kagglehub download cache
  (`KAGGLEHUB_CACHE=/app/data/kagglehub`).

## Sign-in: shared-password gate (replaces the emailed Access PIN)

The app has a built-in sign-in gated by `APP_PASSWORD`. When set, every request
(except `/login`, `/logout`, static assets, and `/healthz`) is redirected to
`/login` until the visitor enters the one shared password; a correct password
grants a signed, HttpOnly+Secure+SameSite=Lax session cookie (~30-day lifetime),
so re-auth is rare. Wrong passwords are rate-limited per client IP (10 fails /
15 min → temporary 429). **Unset/empty `APP_PASSWORD` = gate OFF** (the app
falls back to whatever else is configured, e.g. CF Access) — so this ships
dormant behind Access and is switched on at cutover.

Box `.env` vars:

```
APP_PASSWORD=<the-one-shared-password-Graham-hands-out>
SESSION_SECRET=<random-32+-byte-hex>   # signs the cookie; keep it stable
```

Generate `SESSION_SECRET` once with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Cutover (operator, at go-time):** add `APP_PASSWORD` + `SESSION_SECRET` to the
box `.env`, redeploy, verify `/login` works, THEN remove the Cloudflare Access
app for `taste-twin.graham-williams.com` (and drop `CF_ACCESS_AUD` /
`CF_ACCESS_TEAM_DOMAIN` from `.env`). The CF-Access verification code stays in
the app, just unconfigured. Note viewer mode still applies: after signing in,
the box gallery is read-only (form hidden, `POST /run` refused) exactly as
before — the gate sits in front of it. **Keep `APP_HOST` set** at cutover
(`docker-compose.yml` already sets it) — it is what enforces the Origin/Referer
CSRF pin on `POST /login`.

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

**In view-only mode (the box default) there is no first-boot ingest** — no
worker runs and `pool.db` is never built or required. This section applies only
to a **full-mode** instance (`TASTE_TWIN_VIEWER_MODE` unset/`0`).

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
