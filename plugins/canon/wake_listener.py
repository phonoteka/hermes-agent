"""Receive Canon pause announcements on the gateway-wide private Unix socket."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from typing import TYPE_CHECKING

from hermes_constants import get_default_hermes_root, get_hermes_home

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

MAX_INLINE_CHARS = 6000
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
WAKE_INSTRUCTION = (
    "Show cardBody to the user as-is (read cardBodyFile when present and verify cardBodySha256), "
    "with actions, via clarify in this session or by the humanGateDelivery route when set. "
    "Then call canon_checkpoint subcommand=continue-paused with workflow=continuePaused.workflow "
    "and request_json=continuePaused.request plus gateDecision {action, and feedback when the action requires it}. "
    "Never decide for the user."
)
_logger = logging.getLogger(__name__)


def wake_socket_path() -> Path:
    """Return the shared gateway endpoint, independent of the current profile."""
    return Path(get_default_hermes_root()) / "runtime" / "canon" / "gateway-wake.sock"


class WakeListener:
    """Own one private socket and stoppable accept worker; never poll Canon stores.

    A live endpoint is left to its existing owner. Each connection carries one
    bounded JSON line and receives an acknowledgement of injection acceptance,
    not proof that the session has already consumed the message.
    """

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        """Bind once, replacing only an unreachable stale endpoint.

        Provision a 0700 directory and 0600 socket before accepting requests;
        propagate startup errors rather than silently disabling delivery.
        """
        with self._lifecycle_lock:
            if self._thread is not None or self._stop_event.is_set():
                return
            path = wake_socket_path()
            # Only the default-root plugin manager receives the gateway injector; secondary
            # profile managers in the same process must never own the endpoint (their
            # inject_message would always refuse). register() runs under the manager's home.
            if Path(get_hermes_home()).resolve() != Path(get_default_hermes_root()).resolve():
                return
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            if path.exists():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(5)
                    try:
                        probe.connect(str(path))
                    except OSError:
                        path.unlink()
                    else:
                        _logger.info("Canon wake listener: live endpoint already owned")
                        return
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(path))
                identity = path.stat()
                self._path = path
                self._identity = (identity.st_dev, identity.st_ino)
                path.chmod(0o600)
                server.listen()
                server.settimeout(0.2)
                self._server = server
                self._thread = threading.Thread(
                    target=self._serve, args=(server,), name="canon-wake-listener", daemon=True,
                )
                self._thread.start()
            except Exception:
                server.close()
                self._server = None
                self._unlink_owned_socket()
                raise

    def stop(self) -> None:
        """Close acceptance, join at most five seconds, unlink only our inode.

        In-flight daemon handlers retain their five-second socket timeout.
        Repeated stop calls and stopping a non-owning listener are harmless.
        """
        with self._lifecycle_lock:
            self._stop_event.set()
            if self._server is not None:
                self._server.close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._unlink_owned_socket()

    def _unlink_owned_socket(self) -> None:
        if self._path is None or self._identity is None:
            return
        try:
            current = self._path.stat()
            if (current.st_dev, current.st_ino) == self._identity:
                self._path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self, server: socket.socket) -> None:
        """Accept until stopped; isolate each bounded exchange in a daemon."""
        while not self._stop_event.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    _logger.warning("Canon wake listener: accept failed")
                return
            threading.Thread(
                target=self._handle_connection, args=(connection,),
                name="canon-wake-request", daemon=True,
            ).start()

    def _handle_connection(self, connection: socket.socket) -> None:
        """Read one UTF-8 JSON line (at most 8 MiB), reply once and close.

        Malformed requests and unexpected failures refuse delivery; logs never
        include the prompt, session key or exception payload.
        """
        with connection:
            connection.settimeout(5)
            try:
                with connection.makefile("rb") as stream:
                    line = stream.readline(_MAX_REQUEST_BYTES + 1)
                if not line.endswith(b"\n") or len(line) > _MAX_REQUEST_BYTES:
                    raise ValueError("Invalid framing")
                reply = self._deliver(json.loads(line.decode("utf-8")))
            except Exception:
                _logger.warning("Canon wake listener: request rejected")
                reply = {"accepted": False, "code": "malformed"}
            try:
                connection.sendall((json.dumps(reply, separators=(',', ':')) + "\n").encode("utf-8"))
            except OSError:
                _logger.warning("Canon wake listener: reply unavailable")

    def _deliver(self, request: object) -> dict:
        """Validate the wire request and inject the unchanged Canon prompt.

        Oversize messages reference an atomic private copy of the full prompt.
        No injection is attempted on invalid input; the context owns consent and
        session routing, and refusal is returned to the caller without retries.
        """
        if not isinstance(request, dict) or request.get("type") != "canon_human_gate_wake":
            raise ValueError("Invalid request")
        session = request.get("hostSession")
        delivery = request["humanGateDelivery"]
        prompt = request.get("prompt")
        if (not isinstance(session, str) or not session.strip()
                or (delivery is not None and not isinstance(delivery, str))
                or not isinstance(prompt, dict)):
            raise ValueError("Invalid request")
        gate = prompt.get("humanGatePrompt")
        if (not isinstance(gate, dict) or not isinstance(gate.get("cardBody"), str)
                or not isinstance(gate.get("actions"), list)
                or not isinstance(prompt.get("continuePaused"), dict)):
            raise ValueError("Invalid prompt")
        run_id = prompt["runId"]
        if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("Invalid run identity")
        message = {
            "type": "canon_human_gate_wake",
            "runId": run_id,
            "workflowRef": prompt["workflowRef"],
            "humanGateDelivery": delivery,
            "instruction": WAKE_INSTRUCTION,
            "cardBody": gate["cardBody"],
            "actions": gate["actions"],
            "continuePaused": prompt["continuePaused"],
        }
        text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        if len(text) > MAX_INLINE_CHARS:
            content = json.dumps(prompt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            path = wake_socket_path().parent / "prompts" / f"{run_id}-{digest[:16]}.json"
            self._write_prompt(path, content)
            del message["cardBody"]
            message["cardBodyFile"] = str(path.resolve())
            message["cardBodySha256"] = digest
            text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        if self._ctx.inject_message(text, session_key=session):
            return {"accepted": True}
        return {"accepted": False, "code": "injection_refused"}

    @staticmethod
    def _write_prompt(path: Path, content: bytes) -> None:
        """Atomically replace a 0600 prompt in a 0700 directory.

        I/O errors propagate while retaining any old file and removing the
        temporary file. Content is the exact serialized request prompt.
        """
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=".prompt-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
