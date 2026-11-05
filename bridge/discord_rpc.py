"""Optional Discord Rich Presence publisher for the bridge.

Publishes heart rate and step count to the local Discord client over the
Discord IPC named pipes (``discord-ipc-0`` .. ``discord-ipc-9``).
Standard library only — no pypresence dependency — and entirely optional:
the service is disabled unless a Discord application client ID is
configured in ``bridge/discord_client_id.txt`` or via
``--discord-client-id``.

Data-integrity rules carry over: heart rate and steps that the watch has
not reported are omitted from the presence text, never rendered as zero.
The presence text is visible to anyone who can see your Discord profile;
that is the point of Rich Presence. Nothing else leaves the machine
through this module — the watch connection stays read-only.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import uuid
from pathlib import Path

DEFAULT_CLIENT_ID_FILE = Path(__file__).resolve().parent / "discord_client_id.txt"

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3

PIPE_NAMES = tuple(f"\\\\.\\pipe\\discord-ipc-{index}" for index in range(10))
CONNECT_BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)
MIN_UPDATE_INTERVAL_S = 10.0
KEEPALIVE_S = 60.0

WATCH_LABEL = "Oraimo Watch 6R"


def load_client_id(
    client_id_file: Path = DEFAULT_CLIENT_ID_FILE,
    explicit: str | None = None,
) -> str | None:
    """Resolve the Discord application client ID; ``None`` disables RPC."""
    if explicit is not None:
        return explicit.strip() or None
    try:
        value = client_id_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


# -- wire format ---------------------------------------------------------------


def encode_frame(opcode: int, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", opcode, len(body)) + body


def read_frame(pipe) -> tuple[int, dict] | None:
    header = pipe.read(8)
    if not header or len(header) < 8:
        return None
    opcode, length = struct.unpack("<II", header)
    body = pipe.read(length)
    if body is None or len(body) < length:
        return None
    return opcode, json.loads(body.decode("utf-8"))


def handshake(pipe, client_id: str) -> None:
    pipe.write(encode_frame(OP_HANDSHAKE, {"v": 1, "client_id": client_id}))
    frame = read_frame(pipe)
    if frame is None or frame[0] != OP_FRAME:
        raise OSError("Discord IPC handshake produced no READY frame")
    event = frame[1].get("evt")
    if event not in (None, "READY"):
        raise OSError(f"Discord IPC handshake rejected: {event}")


def send_set_activity(pipe, activity: dict) -> None:
    pipe.write(
        encode_frame(
            OP_FRAME,
            {
                "nonce": uuid.uuid4().hex,
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
            },
        )
    )
    read_frame(pipe)  # command ack; contents unused


def open_discord_pipe():
    last_error: OSError | None = None
    for name in PIPE_NAMES:
        try:
            return open(name, "r+b", buffering=0)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("no Discord IPC pipe found")


# -- presence text -------------------------------------------------------------


def compose_activity(
    bridge_state: str,
    heart_rate: int | None,
    steps: int | None,
    started_at: float,
) -> dict:
    """Build the activity payload; unavailable values are omitted, never zeroed."""
    if bridge_state == "connected":
        state = f"♥ {heart_rate} bpm" if heart_rate is not None else "Connected"
        details = f"{steps:,} steps" if steps is not None else WATCH_LABEL
    elif bridge_state in ("scanning", "connecting", "reconnecting"):
        state, details = "Connecting…", WATCH_LABEL
    else:  # stopped / error / no-watch-found
        state, details = "Watch disconnected", WATCH_LABEL
    return {
        "details": details,
        "state": state,
        "timestamps": {"start": int(started_at)},
    }


# -- service -------------------------------------------------------------------


class DiscordPresenceService:
    """Owns the Discord IPC pipe on a daemon worker thread.

    ``set_bridge_state`` / ``update_heart_rate`` / ``update_steps`` are safe
    from any thread; they only post into shared state, and the worker
    coalesces them into SET_ACTIVITY frames at most once per
    ``MIN_UPDATE_INTERVAL_S`` (Discord rate-limits presence updates).
    """

    def __init__(self, client_id: str, *, pipe_opener=open_discord_pipe) -> None:
        self._client_id = client_id
        self._pipe_opener = pipe_opener
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._bridge_state = "stopped"
        self._heart_rate: int | None = None
        self._steps: int | None = None
        self._started_at = time.time()
        self._thread: threading.Thread | None = None

    # -- public API (any thread) --------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="bridge-discord-rpc", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._closed = True
        self._wake.set()

    def set_bridge_state(self, state: str) -> None:
        with self._lock:
            self._bridge_state = state
        self._wake.set()

    def update_heart_rate(self, bpm: int | None) -> None:
        with self._lock:
            self._heart_rate = bpm
        self._wake.set()

    def update_steps(self, steps: int | None) -> None:
        with self._lock:
            self._steps = steps
        self._wake.set()

    # -- worker ---------------------------------------------------------------

    def _run(self) -> None:
        backoff_index = 0
        while not self._closed:
            pipe = None
            try:
                pipe = self._pipe_opener()
                handshake(pipe, self._client_id)
                backoff_index = 0
                self._publish_loop(pipe)
            except Exception:
                pass  # cosmetic feature; retry quietly with backoff
            finally:
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            if self._closed:
                break
            delay = CONNECT_BACKOFF_S[min(backoff_index, len(CONNECT_BACKOFF_S) - 1)]
            backoff_index += 1
            self._wake.wait(delay)
            self._wake.clear()

    def _publish_loop(self, pipe) -> None:
        last_sent = 0.0
        last_payload: dict | None = None
        while not self._closed:
            self._wake.wait(KEEPALIVE_S)
            self._wake.clear()
            elapsed_since_send = time.monotonic() - last_sent
            if elapsed_since_send < MIN_UPDATE_INTERVAL_S:
                time.sleep(MIN_UPDATE_INTERVAL_S - elapsed_since_send)
            with self._lock:
                activity = compose_activity(
                    self._bridge_state, self._heart_rate, self._steps, self._started_at
                )
            if activity == last_payload and time.monotonic() - last_sent < KEEPALIVE_S:
                continue  # unchanged and freshly sent
            send_set_activity(pipe, activity)
            last_sent = time.monotonic()
            last_payload = activity
