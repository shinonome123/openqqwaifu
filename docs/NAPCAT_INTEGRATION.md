# NapCat Integration

This project uses NapCat as a protocol sidecar instead of embedding QQ protocol logic in the main service.

## Target topology

```text
QQ Client
  |
  v
NapCat
  |  \
  |   \-- HTTP API -> send_group_msg / send_private_msg
  |
  +------ HTTP event push -> waifu-standalone /onebot/events
```

## Local mode

1. Start NapCat locally and finish QQ login in NapCat.
2. Point NapCat HTTP API at `http://127.0.0.1:3000`.
3. Configure NapCat event push to `http://127.0.0.1:8080/onebot/events`.
4. Start the service:

```powershell
python .\run_cli.py serve --config .\examples\config.napcat.local.json
```

5. Validate sidecar connectivity:

```powershell
python .\run_cli.py check-sidecar --config .\examples\config.napcat.local.json
```

## Compose mode

Run both services with:

```powershell
docker compose -f .\compose.napcat.yml up --build
```

Then:

1. Open NapCat WebUI on `http://127.0.0.1:6099`.
2. Complete QQ login.
3. In NapCat network configuration, enable:
   - HTTP API on port `3000`
   - HTTP event push to `http://waifu:8080/onebot/events`

The compose file keeps `NapCat` and `waifu-standalone` on the same Docker network, so `http://napcat:3000` works from the `waifu` container.

## Why this boundary

- `waifu-standalone` owns prompts, memory, and model calls
- `NapCat` owns login state, message transport, and reconnect behavior
- upgrades on the QQ protocol side do not force business-layer refactors
