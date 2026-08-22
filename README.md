# mlsc

Multi-source listening and signal. Point it at a piece of software or a theme and it collects public feedback every day — Google Play and App Store reviews, Discourse forums, news, RSS, Hacker News — attaches each document to a topic whose id never changes, rolls the documents up into daily per-topic metrics, and flags what is actually moving: bursts, sustained growth, sentiment flips, brand-new topics. Trend events and generated insights always carry the evidence documents behind them, and every metric surface carries its own data quality, so a scraper that broke does not read as a quiet week.

Sources must be reachable anonymously (no API keys, no paid APIs). The LLM tiers are pluggable — any OpenAI-compatible endpoint, local or cloud.

## How a run moves through the system

    Celery Beat (MonitorAwareScheduler)
      → projects one entry per active monitor from the database, every tick
        → task mlsc.run_monitor
          → RunService.start                      one run per monitor per date, Redis-locked
            → dispatch_run
              → per-source collection             quota, dedupe, per-run stats ledger
              → RunService.finalise               complete | partial | failed
              → evaluate_source_health
              → enrich_documents                  clean, PII-strip, embed, sentiment, intent
              → run_daily_analytics               topic assignment → daily metrics rollup

    HTTP (FastAPI)
      → read endpoints                            overview, timeseries, topics, events, documents
      → write endpoints                           enqueue and return a run id; never long work
        → React frontend

## Run it

Requires Docker. The first build solves the conda environment and is slow (~10 min).

```bash
cp .env.example .env
docker compose up --build
```

Then open http://localhost:8080 for the dashboard and http://localhost:8000/docs for the API.

Compose starts PostgreSQL 16 with TimescaleDB and pgvector, Redis, an Alembic migration job that must finish before anything else starts, the API, a Celery worker, Celery Beat, and the built frontend behind nginx. The frontend proxies the API under its own origin, so `MLSC_LOCAL_FRONTEND_ORIGIN` in `.env` must match `MLSC_LOCAL_FRONTEND_PORT` — the value is compiled into the bundle.

The API has no authentication. Do not publish these ports beyond localhost.

## Develop it

Dependencies live in `environment.yml` only. `docker-compose.local.yml` runs just PostgreSQL and Redis, for working on the host:

```bash
conda env create -f environment.yml
docker compose -f docker-compose.local.yml up -d
conda run -n mlsc python -m pytest tests/
```

## Known gaps

- Trend detection, insight generation, alert delivery, retention, and backfill are reachable only from the test suite — nothing calls them in production. Beat projects per-monitor runs and nothing else, so the weekly and monthly topic cadences do not run either.
- `dispatch_run` implements the Google Play adapter only; every other source is reported as skipped.
