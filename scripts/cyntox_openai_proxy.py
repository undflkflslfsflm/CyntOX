from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
MYTHOS_SENTINEL = "CYNTOX_MYTHOS_SYSTEM_PROMPT_V1"
NO_THINK_PREFIX = "/no_think"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_NUM_CTX = 16384


def is_grammar_error(payload: bytes) -> bool:
    return b"failed to parse grammar" in payload.lower()


def is_context_error(payload: bytes) -> bool:
    lowered = payload.lower()
    return b"exceeds the available context size" in lowered or b"exceed_context_size_error" in lowered


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in headers if k.lower() not in HOP_BY_HOP_HEADERS}


def strip_native_tools(body: bytes | None) -> bytes | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or "tools" not in payload:
        return None
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def prepend_text_to_content(content: object, prefix: str) -> object:
    if isinstance(content, str):
        return f"{prefix}\n\n{content}"
    if isinstance(content, list):
        return [{"type": "text", "text": prefix}, *content]
    return f"{prefix}\n\n{content}"


def content_contains_text(content: object, needle: str) -> bool:
    if isinstance(content, str):
        return needle in content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and needle in str(part.get("text", "")):
                return True
    return needle in str(content)


def inject_mythos_prompt(body: bytes | None, system_prompt: str) -> bytes | None:
    if not body or not system_prompt:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body

    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return body

    messages = payload["messages"]
    for message in messages:
        if isinstance(message, dict) and content_contains_text(message.get("content"), MYTHOS_SENTINEL):
            return body

    mythos_block = f"{NO_THINK_PREFIX}\n[{MYTHOS_SENTINEL}]\n{system_prompt.strip()}\n[/{MYTHOS_SENTINEL}]"
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            message["content"] = prepend_text_to_content(message.get("content", ""), mythos_block)
            return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    messages.append({"role": "user", "content": mythos_block})
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def generation_token_cap() -> int:
    raw = os.environ.get("CYNTOX_PROXY_MAX_TOKENS", "").strip()
    if raw.isdigit():
        return max(64, min(int(raw), 8192))
    return DEFAULT_MAX_TOKENS


def generation_context_size() -> int:
    raw = os.environ.get("CYNTOX_PROXY_NUM_CTX", "").strip()
    if raw.isdigit():
        return max(1024, min(int(raw), 262144))
    return DEFAULT_NUM_CTX


def harden_generation_body(body: bytes | None, *, upstream_model: str = "") -> bytes | None:
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body

    requested_model = payload.get("model")
    if upstream_model and requested_model in {"cyntox", "cyntox:latest"}:
        payload["model"] = upstream_model
    payload["reasoning_effort"] = "low"
    payload["enable_thinking"] = False
    payload["thinking_budget"] = 0
    payload["think"] = False
    options = payload.get("options")
    if not isinstance(options, dict):
        options = {}
    options["num_ctx"] = generation_context_size()
    payload["options"] = options
    cap = generation_token_cap()
    for key in ("max_tokens", "max_completion_tokens"):
        existing = payload.get(key)
        if isinstance(existing, int) and existing > 0:
            payload[key] = min(existing, cap)
    if "max_tokens" not in payload and "max_completion_tokens" not in payload:
        payload["max_tokens"] = cap
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class CyntOXProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        *,
        target_base: str,
        retries: int,
        system_prompt: str,
        upstream_model: str,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.target_base = target_base.rstrip("/")
        self.retries = max(0, retries)
        self.system_prompt = system_prompt
        self.upstream_model = upstream_model


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    server: CyntOXProxy

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/__cyntox_proxy_health":
            self.respond_json(200, {"ok": True})
            return
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def forward(self) -> None:
        content_length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(content_length) if content_length else None
        body = inject_mythos_prompt(body, self.server.system_prompt)
        body = harden_generation_body(body, upstream_model=self.server.upstream_model)
        url = f"{self.server.target_base}{self.path}"
        headers = filtered_headers(self.headers.items())

        def attempt_payload(payload_body: bytes | None) -> tuple[int, bytes, urllib.error.HTTPError] | None:
            request = urllib.request.Request(  # noqa: S310
                url,
                data=payload_body if self.command != "GET" else None,
                headers=headers,
                method=self.command,
            )

            last_error: tuple[int, bytes, urllib.error.HTTPError] | None = None
            for attempt in range(self.server.retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=300) as upstream:  # noqa: S310
                        self.send_response(upstream.status)
                        for key, value in upstream.headers.items():
                            if key.lower() not in HOP_BY_HOP_HEADERS:
                                self.send_header(key, value)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while True:
                            chunk = upstream.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        self.close_connection = True
                        return None
                except urllib.error.HTTPError as error:
                    payload = error.read()
                    last_error = (error.code, payload, error)
                    if error.code == 400 and is_grammar_error(payload) and attempt < self.server.retries:
                        time.sleep(0.25 * (attempt + 1))
                        continue
                    break
                except Exception as error:  # pragma: no cover - defensive bridge failure path
                    self.respond_json(502, {"error": str(error)})
                    return None
            return last_error

        last_error = attempt_payload(body)
        if last_error is None:
            return

        status, payload, _error = last_error
        stripped_body = strip_native_tools(body)
        retry_without_tools = (
            status == 400
            and (is_grammar_error(payload) or is_context_error(payload))
            and stripped_body is not None
        )
        if retry_without_tools:
            last_error = attempt_payload(stripped_body)
            if last_error is None:
                return

        if last_error is None:
            self.respond_json(502, {"error": "upstream request failed"})
            return

        status, payload, error = last_error
        self.send_response(status)
        for key, value in error.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback OpenAI-compatible retry proxy for CyntOX/Ollama.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=11435)
    parser.add_argument("--target-base", default="http://127.0.0.1:11434")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--system-prompt-file")
    parser.add_argument("--upstream-model", default=os.environ.get("CYNTOX_UPSTREAM_MODEL", ""))
    args = parser.parse_args()
    system_prompt = ""
    if args.system_prompt_file:
        try:
            system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8").strip()
        except OSError:
            system_prompt = ""

    with CyntOXProxy(
        (args.listen_host, args.listen_port),
        Handler,
        target_base=args.target_base,
        retries=args.retries,
        system_prompt=system_prompt,
        upstream_model=args.upstream_model,
    ) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
