from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import time
import urllib.error
import urllib.request
from collections.abc import Iterable


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


def is_grammar_error(payload: bytes) -> bool:
    return b"failed to parse grammar" in payload.lower()


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


class QwenthosProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        *,
        target_base: str,
        retries: int,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.target_base = target_base.rstrip("/")
        self.retries = max(0, retries)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    server: QwenthosProxy

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/__qwenthos_proxy_health":
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
        url = f"{self.server.target_base}{self.path}"
        headers = filtered_headers(self.headers.items())

        def attempt_payload(payload_body: bytes | None) -> tuple[int, bytes, urllib.error.HTTPError] | None:
            request = urllib.request.Request(
                url,
                data=payload_body if self.command != "GET" else None,
                headers=headers,
                method=self.command,
            )

            last_error: tuple[int, bytes, urllib.error.HTTPError] | None = None
            for attempt in range(self.server.retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=300) as upstream:
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
        if status == 400 and is_grammar_error(payload) and stripped_body is not None:
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
    parser = argparse.ArgumentParser(description="Loopback OpenAI-compatible retry proxy for Qwenthos/Ollama.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=11435)
    parser.add_argument("--target-base", default="http://127.0.0.1:11434")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    with QwenthosProxy(
        (args.listen_host, args.listen_port),
        Handler,
        target_base=args.target_base,
        retries=args.retries,
    ) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
