from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cells.generator import Generator
from .config import AppConfig
from .contracts import MemoryStore, OutboundPort
from .gateways.onebot_actions import OneBotActionClient, OneBotHttpOutboundPort
from .memory import FileMemoryStore, InMemoryStore
from .models import InboundEvent, OutboundMessage
from .organs.memories import Memory
from .services import CapturingOutboundPort
from .systems.emotions import EmotionSensor
from .systems.searching import SearchDecider


@dataclass(slots=True)
class WaifuService:
    config: AppConfig
    memory: Memory
    emotions: EmotionSensor
    generator: Generator
    search: SearchDecider
    outbound: OutboundPort

    def handle_event(self, event: InboundEvent) -> OutboundMessage | None:
        text = event.plain_text
        if not text and event.image_count == 0:
            return None

        session = self.memory.save_user_message(
            event.launcher_id,
            event.launcher_type,
            event.sender_name,
            text or "[image]",
        )
        emotion = self.emotions.analyze(event, session)

        image_prompt = self._extract_image_prompt(text)
        if image_prompt is not None:
            return self._handle_image_request(event, image_prompt)

        reply_text = self.generator.generate_reply(event, emotion)
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
        )
        self.outbound.send(message)
        self.memory.save_assistant_message(event.launcher_id, event.launcher_type, reply_text)
        return message

    def _handle_image_request(self, event: InboundEvent, prompt: str) -> OutboundMessage:
        try:
            image = self.generator.generate_image(prompt)
            text = self.generator.generate_image_caption(image.prompt)
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=text,
                images=[image.image_ref],
            )
        except Exception:
            message = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text="image generation failed. please retry later.",
            )
        self.outbound.send(message)
        self.memory.save_assistant_message(event.launcher_id, event.launcher_type, message.text)
        return message

    def _extract_image_prompt(self, text: str) -> str | None:
        stripped = text.strip()
        prefix = self.config.image_command_prefix.strip()
        if not stripped or not prefix:
            return None
        if not stripped.lower().startswith(prefix.lower()):
            return None
        return stripped[len(prefix):].lstrip(" :：,-").strip()


def build_default_service(config: AppConfig | None = None) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    return _build_service(app_config, InMemoryStore(), CapturingOutboundPort())


def build_file_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, CapturingOutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    return _build_service(app_config, FileMemoryStore(root), CapturingOutboundPort())


def build_runtime_service(
    config: AppConfig | None = None,
    store_root: str | Path | None = None,
) -> tuple[WaifuService, OutboundPort]:
    app_config = config or AppConfig()
    root = Path(store_root) if store_root else Path(app_config.data_root) / "sessions"
    outbound = _build_runtime_outbound(app_config)
    return _build_service(app_config, FileMemoryStore(root), outbound)


def _build_runtime_outbound(app_config: AppConfig) -> OutboundPort:
    if app_config.qq_sidecar.dry_run or not app_config.qq_sidecar.outbound_base_url:
        return CapturingOutboundPort()
    client = OneBotActionClient(
        base_url=app_config.qq_sidecar.outbound_base_url,
        timeout=app_config.qq_sidecar.outbound_timeout_seconds,
        access_token=app_config.qq_sidecar.access_token,
    )
    return OneBotHttpOutboundPort(client)


def _build_service(
    app_config: AppConfig,
    store: MemoryStore,
    outbound: OutboundPort,
) -> tuple[WaifuService, OutboundPort]:
    service = WaifuService(
        config=app_config,
        memory=Memory(store),
        emotions=EmotionSensor(),
        generator=Generator(),
        search=SearchDecider(),
        outbound=outbound,
    )
    return service, outbound
