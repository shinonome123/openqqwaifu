from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import build_default_service, build_runtime_service
from .cells.config import ConfigManager
from .config import AppConfig
from .gateways.onebot_actions import OneBotActionClient
from .http_api import HttpApi, run_server
from .memory import FileMemoryStore
from .migration import WaifuDataImporter
from .services import CapturingOutboundPort


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "dump-config":
        ConfigManager().dump_default(args.path)
        print(f"default config written to {Path(args.path).resolve()}")
        return

    if args.command == "import-waifu":
        store = FileMemoryStore(args.store_root)
        importer = WaifuDataImporter(store, args.waifu_root, default_launcher_type=args.launcher_type)
        results = (
            [importer.import_launcher(args.launcher_id, args.launcher_type)]
            if args.launcher_id
            else importer.import_all(args.launcher_type)
        )
        for item in results:
            print(f"imported {item.launcher_type}:{item.launcher_id} -> {item.session_path}")
        return

    config = _load_config(args.config)
    if args.command == "check-sidecar":
        _check_sidecar(config)
        return

    if args.command == "export-skill-pack":
        service, _ = build_default_service(config)
        bundle = service.export_skill_pack(
            skill_ids=args.skill_id,
            include_builtin=args.include_builtin,
            name=args.name,
            description=args.description,
        )
        output = json.dumps(bundle, ensure_ascii=False, indent=2)
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output + "\n", encoding="utf-8")
            print(f"skill pack written to {target.resolve()}")
        else:
            print(output)
        return

    if args.command == "import-skill-pack":
        service, _ = build_default_service(config)
        payload = Path(args.input).read_text(encoding="utf-8")
        result = service.import_skill_pack(payload, overwrite=not args.no_overwrite)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    service, outbound = build_runtime_service(config, store_root=args.store_root)
    api = HttpApi(service)
    server = run_server(api, config.qq_sidecar.inbound_host, config.qq_sidecar.inbound_port)
    outbound_mode = (
        "capture(dry-run)"
        if isinstance(outbound, CapturingOutboundPort)
        else f"onebot-http->{config.qq_sidecar.outbound_base_url}"
    )
    print(
        f"{config.service_name} listening on "
        f"http://{config.qq_sidecar.inbound_host}:{config.qq_sidecar.inbound_port} "
        f"[outbound={outbound_mode}]"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _load_config(path: str | None) -> AppConfig:
    return ConfigManager(path).load()


def _check_sidecar(config: AppConfig) -> None:
    client = OneBotActionClient(
        base_url=config.qq_sidecar.outbound_base_url,
        timeout=config.qq_sidecar.outbound_timeout_seconds,
        access_token=config.qq_sidecar.access_token,
    )
    version = client.get_version_info()
    login = client.get_login_info()
    status = client.get_status()
    print(f"adapter={config.qq_sidecar.adapter_name}")
    print(f"base_url={config.qq_sidecar.outbound_base_url}")
    print(f"version={version}")
    print(f"login={login}")
    print(f"status={status}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waifu-standalone")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", default=None)
    serve.add_argument("--store-root", default=None)

    dump = subparsers.add_parser("dump-config")
    dump.add_argument("path")

    migrate = subparsers.add_parser("import-waifu")
    migrate.add_argument("--waifu-root", required=True)
    migrate.add_argument("--store-root", default="data/sessions")
    migrate.add_argument("--launcher-type", default="group")
    migrate.add_argument("--launcher-id", default=None)

    check = subparsers.add_parser("check-sidecar")
    check.add_argument("--config", default=None)

    export_pack = subparsers.add_parser("export-skill-pack")
    export_pack.add_argument("--config", default=None)
    export_pack.add_argument("--output", default=None)
    export_pack.add_argument("--skill-id", action="append", default=[])
    export_pack.add_argument("--include-builtin", action="store_true")
    export_pack.add_argument("--name", default="")
    export_pack.add_argument("--description", default="")

    import_pack = subparsers.add_parser("import-skill-pack")
    import_pack.add_argument("--config", default=None)
    import_pack.add_argument("--input", required=True)
    import_pack.add_argument("--no-overwrite", action="store_true")

    parser.set_defaults(command="serve", config=None, store_root=None)
    return parser


if __name__ == "__main__":
    main()
