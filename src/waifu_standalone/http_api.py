from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .models import InboundEvent, MessageSegment


def parse_onebot_event(payload: dict[str, Any]) -> InboundEvent:
    launcher_type = "group" if payload.get("message_type") == "group" else "person"
    launcher_id = str(payload.get("group_id") or payload.get("user_id") or "unknown")
    sender = payload.get("sender") or {}
    sender_id = str(sender.get("user_id") or payload.get("user_id") or "unknown")
    segments = _parse_segments(payload)
    sender_name = str(sender.get("card") or sender.get("nickname") or "user")
    if _looks_mojibake(sender_name):
        sender_name = f"user_{sender_id}"

    return InboundEvent(
        launcher_id=launcher_id,
        launcher_type=launcher_type,
        sender_id=sender_id,
        sender_name=sender_name,
        segments=segments,
    )


def _parse_segments(payload: dict[str, Any]) -> list[MessageSegment]:
    raw_message = payload.get("message")
    raw_text = payload.get("raw_message")
    items: list[dict[str, Any]] = []
    if isinstance(raw_message, list):
        items = [item for item in raw_message if isinstance(item, dict)]
    elif isinstance(raw_message, dict):
        items = [raw_message]
    elif isinstance(raw_message, str):
        return [MessageSegment(kind="text", text=raw_message)]
    elif isinstance(raw_text, str):
        return [MessageSegment(kind="text", text=str(raw_text))]

    if _should_prefer_raw_message(raw_text, items):
        return [MessageSegment(kind="text", text=str(raw_text).strip())]

    segments: list[MessageSegment] = []
    for item in items:
        item_type = item.get("type")
        data = item.get("data") or {}
        if item_type == "text":
            segments.append(MessageSegment(kind="text", text=str(data.get("text", ""))))
        elif item_type == "image":
            image_ref = str(data.get("url") or data.get("file") or "")
            segments.append(MessageSegment(kind="image", image_url=image_ref))
    return segments


def _should_prefer_raw_message(raw_message: Any, items: list[dict[str, Any]]) -> bool:
    if not isinstance(raw_message, str):
        return False
    if not raw_message.strip():
        return False
    if not items:
        return True
    return all(item.get("type") == "text" for item in items)


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    suspicious = ("闂", "颴", "椤", "锛", "鈥", "銆", "鐨", "浣", "璇", "鍙", "杩", "涓", "鈩", "�")
    return any(token in text for token in suspicious)


@dataclass(slots=True)
class HttpApi:
    service: Any

    def handle_json(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        post_type = payload.get("post_type")
        if post_type and post_type != "message":
            return HTTPStatus.ACCEPTED, {"status": "ignored", "reason": "unsupported post_type"}

        event = parse_onebot_event(payload)
        try:
            message = self.service.handle_event(event)
        except Exception as exc:
            return HTTPStatus.BAD_GATEWAY, {"status": "delivery_failed", "reason": str(exc)}

        if message is None:
            return HTTPStatus.ACCEPTED, {"status": "ignored", "reason": "empty event"}
        return HTTPStatus.OK, {
            "status": "ok",
            "reply": {
                "launcher_id": message.launcher_id,
                "launcher_type": message.launcher_type,
                "text": message.text,
                "images": message.images,
            },
        }


def make_handler(api: HttpApi):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._write_json(HTTPStatus.OK, {"status": "ok"})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/onebot/events":
                self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            try:
                raw = _read_request_body(self)
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return
            try:
                body = _decode_request_body(raw, self.headers.get("Content-Encoding", ""))
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                preview = raw[:200].decode("utf-8", errors="replace")
                print(f"bad request body preview: {preview!r}")
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return
            if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
                payload = payload[0]
            if not isinstance(payload, dict):
                self._write_json(HTTPStatus.BAD_REQUEST, {"status": "bad_request"})
                return
            status, body = api.handle_json(payload)
            if status < HTTPStatus.BAD_REQUEST:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._write_json(status, body)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def run_server(api: HttpApi, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(api))


def _decode_request_body(raw: bytes, content_encoding: str) -> str:
    encoding = (content_encoding or "").strip().lower()
    body = raw
    if encoding == "gzip":
        body = gzip.decompress(raw)
    return body.decode("utf-8-sig")


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    transfer_encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in transfer_encoding.lower():
        return _read_chunked_body(handler)

    content_length = int(handler.headers.get("Content-Length", "0"))
    return handler.rfile.read(content_length)


def _read_chunked_body(handler: BaseHTTPRequestHandler) -> bytes:
    chunks: list[bytes] = []
    while True:
        size_line = handler.rfile.readline()
        if not size_line:
            raise ValueError("missing chunk size")
        size_token = size_line.strip().split(b";", 1)[0]
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc

        if size == 0:
            while True:
                trailer = handler.rfile.readline()
                if trailer in (b"", b"\r\n", b"\n"):
                    break
            break

        chunks.append(handler.rfile.read(size))
        chunk_end = handler.rfile.read(2)
        if chunk_end not in (b"\r\n", b"\n"):
            raise ValueError("invalid chunk terminator")

    return b"".join(chunks)
