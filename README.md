# Waifu Standalone

This project is a local scaffold for a standalone `waifu` service.

It keeps the upstream split in a simplified form:

- `cells`: text and image generation helpers
- `organs`: persistent memory
- `systems`: emotion and search decisions
- `gateways`: OneBot ingress and OneBot action egress

The scaffold uses only the Python standard library so it can run locally without extra dependencies.

## What works now

- file-backed session storage
- import of existing Waifu session/config files
- local HTTP server for inbound OneBot-style events
- OneBot HTTP action client for outbound sidecar delivery
- dry-run mode for local development without a QQ sidecar
- NapCat-oriented local and Docker config templates
- a sidecar health check command

## Run tests

```powershell
cd C:\path\to\waifu-standalone
$env:PYTHONDONTWRITEBYTECODE='1'
python -B -m unittest discover -s tests -v
```

## Dump default config

```powershell
python .\run_cli.py dump-config .\data\config.json
```

## Run the sample server

The default config uses `dry_run=true`, so the service will accept inbound events and keep outbound messages in memory instead of posting to a sidecar.

```powershell
python .\run_cli.py serve --config .\data\config.json
```

Then send a `POST` request to `http://127.0.0.1:8080/onebot/events`.

## Enable OneBot outbound delivery

Set `qq_sidecar.dry_run` to `false` and point `qq_sidecar.outbound_base_url` at NapCat or Lagrange.

## Check NapCat sidecar

```powershell
python .\run_cli.py check-sidecar --config .\examples\config.napcat.local.json
```

## Run with Docker Compose

```powershell
docker compose -f .\compose.napcat.yml up --build
```

See [NAPCAT_INTEGRATION.md](./docs/NAPCAT_INTEGRATION.md) for the expected NapCat network settings.

## Import existing Waifu data

```powershell
python .\run_cli.py import-waifu --waifu-root C:\path\to\Typer_Body__Waifu! --store-root .\data\sessions
```
