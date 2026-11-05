"""Local-only HTTP bridge for the Chrome new-tab dashboard.

Security model:

- Binds exclusively to ``127.0.0.1``; never exposed to the LAN.
- Every request must carry the bridge token (``X-Oraimo-Token`` header or
  ``?token=`` query parameter). The token is generated on first run and
  stored next to the bridge.
- CORS is granted only to the exact configured unpacked Chrome extension
  origin; requests with any other Origin are rejected.
- The API is read-only with respect to the watch. Its commands perform BLE
  reads, and reconnect requests issue no BLE writes.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import CommandStore, DiagnosticsStore, HistoryStore, SnapshotStore, timestamp
from .discord_rpc import DEFAULT_CLIENT_ID_FILE, DiscordPresenceService, load_client_id
from .watch_manager import WatchManager

BRIDGE_VERSION = "1.0.0"
DEFAULT_PORT = 8765
DEFAULT_TOKEN_FILE = Path(__file__).resolve().parent / "token.txt"
DEFAULT_ORIGIN_FILE = Path(__file__).resolve().parent / "extension_origin.txt"
DEFAULT_EXTENSION_ORIGIN = "chrome-extension://dapmggaiphdbbkjegdkodemhgoceekof"
ALLOWED_CORS_METHODS = frozenset({"GET", "POST", "OPTIONS"})
ALLOWED_CORS_HEADERS = frozenset({"x-oraimo-token", "content-type", "authorization"})
MAX_REQUEST_BODY = 4096


def normalize_extension_origin(origin: str | None) -> str | None:
    if origin is None:
        return None
    parsed = urlsplit(origin.strip())
    if (
        parsed.scheme != "chrome-extension"
        or not re.fullmatch(r"[a-p]{32}", parsed.netloc)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("extension origin must be chrome-extension:// followed by its 32-character ID")
    return f"chrome-extension://{parsed.netloc}"


def load_extension_origin(origin_file: Path, explicit_origin: str | None) -> str | None:
    if explicit_origin:
        return normalize_extension_origin(explicit_origin)
    try:
        configured = origin_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        configured = ""
    return normalize_extension_origin(configured) if configured else DEFAULT_EXTENSION_ORIGIN


def load_or_create_token(token_file: Path, explicit_token: str | None) -> str:
    if explicit_token:
        return explicit_token
    try:
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    token_file.write_text(token, encoding="utf-8")
    return token


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        token: str,
        manager: WatchManager,
        stores,
        allowed_origin: str | None = None,
        command_store: CommandStore | None = None,
    ) -> None:
        self.token = token
        self.manager = manager
        self.snapshot, self.history, self.diagnostics = stores
        self.allowed_origin = normalize_extension_origin(allowed_origin)
        self.commands = command_store or getattr(manager, "commands", None) or CommandStore()
        super().__init__(address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = f"OraimoBridge/{BRIDGE_VERSION}"

    # -- plumbing -------------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the console clean; diagnostics live in /api/diagnostics

    def _origin_allowed(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin is None or self.server.allowed_origin is None:
            return None
        try:
            normalized = normalize_extension_origin(origin)
        except ValueError:
            return None
        return origin if origin == normalized == self.server.allowed_origin else None

    def _host_allowed(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.headers.get("Host", "").casefold() == expected.casefold()

    def _send_cors(self) -> None:
        origin = self._origin_allowed()
        if origin is None:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Oraimo-Token, Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")

    def _preflight_allowed(self) -> bool:
        requested_method = self.headers.get("Access-Control-Request-Method", "").upper()
        if requested_method not in ALLOWED_CORS_METHODS:
            return False
        requested_headers = {
            header.strip().casefold()
            for header in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if header.strip()
        }
        return requested_headers.issubset(ALLOWED_CORS_HEADERS)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Oraimo-Token")
        if not supplied:
            supplied = parse_qs(urlsplit(self.path).query).get("token", [None])[0]
        if not supplied:
            return False
        return hmac.compare_digest(supplied, self.server.token)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, *, body: bytes = b"", authorized_required: bool = True) -> None:
        if not self._host_allowed():
            self._send_json(403, {"ok": False, "error": "Host header must target 127.0.0.1 on the bridge port"})
            return
        if self.headers.get("Origin") is not None and self._origin_allowed() is None:
            self._send_json(403, {"ok": False, "error": "extension origin is not allowed"})
            return
        if authorized_required and not self._authorized():
            self._send_json(401, {"ok": False, "error": "missing or invalid bridge token"})
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        method = self.command

        if method == "OPTIONS":
            if self._origin_allowed() is None or not self._preflight_allowed():
                self._send_json(403, {"ok": False, "error": "origin or preflight request is not allowed"})
            else:
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self._send_cors()
                self.end_headers()
            return

        if path == "/api/status" and method == "GET":
            self._send_json(200, self._status_payload())
        elif path == "/api/snapshot" and method == "GET":
            self._send_json(200, self._payload(self.server.snapshot.snapshot()))
        elif path == "/api/history" and method == "GET":
            self._send_json(200, self._payload(self.server.history.snapshot()))
        elif path == "/api/diagnostics" and method == "GET":
            self._send_json(200, self._payload(self.server.diagnostics.snapshot()))
        elif path == "/api/telemetry/latest" and method == "GET":
            self._send_json(200, self._payload(self.server.commands.latest_telemetry()))
        elif path == "/api/commands" and method == "POST":
            try:
                request = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "request body must be valid JSON"})
                return
            if not isinstance(request, dict) or request.get("type") != "FETCH_STEPS":
                self._send_json(400, {"ok": False, "error": "supported command type is FETCH_STEPS"})
                return
            command = self.server.manager.submit_fetch_steps()
            self._send_json(202, {"ok": True, "data": command})
        elif path.startswith("/api/commands/") and method == "GET":
            command_id = path.removeprefix("/api/commands/")
            command = self.server.commands.get(command_id)
            if command is None:
                self._send_json(404, {"ok": False, "error": "command not found"})
            else:
                self._send_json(200, self._payload(command))
        elif path == "/api/reconnect" and method == "POST":
            self.server.manager.request_reconnect()
            self._send_json(200, {"ok": True, "data": {"reconnect_requested": True}})
        else:
            self._send_json(404, {"ok": False, "error": f"no route for {method} {path}"})

    def _payload(self, data: dict) -> dict:
        return {"ok": True, "data": data, "generated_at": timestamp()}

    def _status_payload(self) -> dict:
        status = self.server.manager.status()
        status["bridge_version"] = BRIDGE_VERSION
        status["generated_at"] = timestamp()
        status["ok"] = True
        return status

    # -- HTTP verbs ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
            return
        if length < 0:
            self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
            return
        if length > MAX_REQUEST_BODY:
            self._send_json(413, {"ok": False, "error": "request body is too large"})
            return
        body = self.rfile.read(length) if length else b""
        self._handle(body=body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        self._handle(authorized_required=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bridge",
        description="Local HTTP bridge between the Oraimo Watch 6R SDK and the dashboard extension.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--token", help="explicit bridge token (default: generated and persisted)")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"where the token is stored (default {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--origin",
        help=(
            "pin the exact Chrome extension origin; defaults to "
            f"{DEFAULT_ORIGIN_FILE} or {DEFAULT_EXTENSION_ORIGIN}"
        ),
    )
    parser.add_argument("--scan-timeout", type=float, default=12.0, help="BLE scan window seconds")
    parser.add_argument(
        "--discord-client-id",
        help=(
            "Discord application client ID for Rich Presence; defaults to "
            f"{DEFAULT_CLIENT_ID_FILE} if present"
        ),
    )
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="disable Discord Rich Presence even when a client ID is configured",
    )
    return parser.parse_args(argv)


async def run_bridge(args: argparse.Namespace) -> None:
    snapshot, history, diagnostics = SnapshotStore(), HistoryStore(), DiagnosticsStore()
    commands = CommandStore()
    discord_client_id = (
        None if args.no_discord else load_client_id(DEFAULT_CLIENT_ID_FILE, args.discord_client_id)
    )
    presence = DiscordPresenceService(discord_client_id) if discord_client_id else None
    manager = WatchManager(
        snapshot,
        history,
        diagnostics,
        scan_timeout=args.scan_timeout,
        command_store=commands,
        presence=presence,
    )
    token = load_or_create_token(args.token_file, args.token)
    allowed_origin = load_extension_origin(DEFAULT_ORIGIN_FILE, args.origin)

    server = BridgeServer(
        ("127.0.0.1", args.port),
        token,
        manager,
        (snapshot, history, diagnostics),
        allowed_origin=allowed_origin,
        command_store=commands,
    )

    print(f"Oraimo Watch 6R bridge v{BRIDGE_VERSION}")
    print(f"  HTTP:  http://127.0.0.1:{args.port}  (loopback only)")
    print(f"  Token: {token}")
    if args.token_file == DEFAULT_TOKEN_FILE and not args.token:
        print(f"         (stored in {args.token_file})")
    if allowed_origin:
        print(f"  CORS:  {allowed_origin}")
    else:
        print(f"  CORS:  {allowed_origin}")
    print("  Read-only: no settings writes, no vendor commands, no firmware operations.")
    if presence is not None:
        print(f"  Discord RPC: enabled (client ID {discord_client_id})")
    else:
        print("  Discord RPC: disabled (no client ID configured)")
    print("Press Ctrl+C to stop.")

    manager.start()
    if presence is not None:
        presence.start()
    thread = threading.Thread(target=server.serve_forever, name="bridge-http", daemon=True)
    thread.start()
    try:
        await asyncio.Event().wait()  # run until interrupted
    finally:
        if presence is not None:
            presence.stop()
        await manager.stop()
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not (0 < args.port < 65536):
        print(f"invalid port: {args.port}", file=sys.stderr)
        raise SystemExit(2)
    try:
        asyncio.run(run_bridge(args))
    except KeyboardInterrupt:
        print("\nBridge stopped.")


if __name__ == "__main__":
    main()
