# Install

Retrace supports two install paths: a quick local Python setup and a Docker Compose stack.

## Requirements

- Python 3.11+ (for local install)
- Docker + Docker Compose (for container install)
- git

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/main/install.sh | bash
```

This clones the repo into `~/retrace`, creates a virtual environment with `uv`, installs the package with dev dependencies, and validates the CLI.

After install:

```bash
cd ~/retrace
source .venv/bin/activate
retrace quickstart
```

## Docker install

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/main/install.sh | bash -s -- --docker
```

This clones the repo and runs `docker compose up -d` with the multi-service stack:

- API on `http://127.0.0.1:8788`
- UI on `http://127.0.0.1:8787`
- Worker for replay finalization
- Browser runner for queued UI tests
- Cron for scheduled ingestion and digests

After install:

```bash
cd ~/retrace
docker compose logs -f api
```

Generate an SDK key:

```bash
docker compose exec api retrace api create-sdk-key --project Web --environment production
```

## Build from source

If you prefer to build manually:

```bash
git clone https://github.com/txmed82/retrace.git
cd retrace
uv venv
uv pip install -e ".[dev]"
```

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `uv` not found | The installer will auto-install `uv` if missing. If it fails, install manually from [astral.sh](https://docs.astral.sh/uv/getting-started/installation/). |
| `git` not found | Install git via your system package manager. |
| Docker commands fail | Ensure Docker and Docker Compose are installed and the daemon is running. |
| Port conflicts | Edit `docker-compose.yml` or set `RETRACE_API_PORT`/`RETRACE_UI_PORT` env vars. |

## Next steps

- [Quickstart](/quickstart) — 5-minute walkthrough
- [Usage](/usage) — command reference
- [Architecture](/architecture) — how it works
