"""Deliver Canon default-store Human Gate pauses through the native gateway."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

from hermes_constants import get_hermes_home
from hermes_cli import plugins

from . import transport

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

TICK_SECONDS = 30.0
# Canon `index` returns a bounded window; the notifier pages until a short page.
INDEX_PAGE_SIZE = 200
MAX_INLINE_CHARS = 6000
WAKE_INSTRUCTION = (
    "Show cardBody to the user as-is (read cardBodyFile when present and verify cardBodySha256), "
    "with actions, via clarify in this session or by the humanGateDelivery route when set. "
    "Then call canon_checkpoint subcommand=continue-paused with workflow=continuePaused.workflow "
    "and request_json=continuePaused.request plus gateDecision {action, and feedback when the action requires it}. "
    "Never decide for the user."
)
_logger = logging.getLogger(__name__)


class PauseNotifier:
    """Pre: context belongs to the loaded plugin. Post: owns one stoppable worker;
    successful notification keys live only in the private state file. Raises:
    construction/resource errors; tick failures are contained and logged safely.
    """

    def __init__(self, ctx: PluginContext) -> None:
        """Pre: ctx supports injection. Post: idle worker with no I/O or delivery.
        Raises: resource allocation errors.
        """
        self._ctx = ctx
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned_keys: set[str] = set()

    def start(self) -> None:
        """Pre: notifier has not been stopped. Post: one daemon worker polls using
        TICK_SECONDS captured now; repeated starts do not create another worker.
        Raises: RuntimeError if thread creation fails.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, args=(TICK_SECONDS,),
            name="canon-pause-notifier", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Pre: none. Post: signals shutdown and waits at most five seconds for
        the worker; an in-flight CLI call retains its 60-second timeout.
        Raises: thread join errors; stopping before start is harmless.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self, interval: float) -> None:
        """Pre: positive polling interval. Post: ticks serially until stopped,
        with an interruptible wait between ticks. Raises: none from ticks.
        """
        while not self._stop_event.is_set():
            self._tick()
            if self._stop_event.wait(interval):
                break

    def _tick(self) -> None:
        """Pre: called serially by this worker. Post: runs one delivery pass only
        while holding the profile-wide exclusive notifier lock, so concurrent
        notifier instances (plugin reloads, several managers in one profile)
        never deliver the same pause twice; a busy lock skips this tick.
        Raises: none; lock failures warn and skip the tick.
        """
        lock_path = Path(get_hermes_home()) / "runtime" / "canon" / "pause-notifier.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            _logger.warning("Canon pause notifier: lock unavailable; skipping tick")
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._deliver()
        finally:
            os.close(fd)

    def _deliver(self) -> None:
        """Pre: caller holds the notifier lock. Post: no action without a gateway;
        otherwise reads every CLI index page, removes obsolete prompt files and
        reads Canon's card for each new bound pause. Oversize wakes carry a private
        atomic file containing the exact prompt stdout bytes instead of cardBody.
        Only accepted injection keys are persisted. Prompt failures warn without
        payloads and leave that row retryable; other failures skip the tick.
        Raises: none; owns CLI carrier and temporary prompt-file cleanup.
        """
        try:
            if not plugins.get_plugin_manager().has_gateway_message_injector:
                return
            root = transport._private_root()
            rows: list = []
            try:
                while True:
                    argv = transport._build_argv("index", {"limit": INDEX_PAGE_SIZE, "offset": len(rows)}, root)
                    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        _logger.warning("Canon pause notifier: index failed; skipping tick")
                        return
                    page = json.loads(result.stdout)
                    if not isinstance(page, list):
                        raise ValueError("Invalid index result")
                    rows.extend(page)
                    if len(page) < INDEX_PAGE_SIZE:
                        break
                state = Path(get_hermes_home()) / "runtime" / "canon" / "pause-notifier.json"
                prompts = state.parent / "prompts"
                current = {
                    f"{row['runId']}-{row['eventCount']}"
                    for row in rows
                    if row["status"] == "paused"
                    and isinstance(row["hostSession"], str) and row["hostSession"]
                }
                for path in prompts.glob("*.json"):
                    if path.stem not in current:
                        path.unlink(missing_ok=True)
                notified = self._read_state(state)
                for row in rows:
                    if row["status"] != "paused":
                        continue
                    session = row["hostSession"]
                    if not isinstance(session, str) or not session:
                        continue
                    run_id = row["runId"]
                    key = f"{run_id}:{row['eventCount']}"
                    if key in notified:
                        continue
                    try:
                        argv = transport._build_argv("inspect", {"surface": "prompt", "run_id": run_id}, root)
                        result = subprocess.run(argv, capture_output=True, timeout=60)
                        if result.returncode != 0:
                            _logger.warning("Canon pause notifier: prompt failed; will retry")
                            continue
                        prompt = json.loads(result.stdout)
                        message = {
                            "type": "canon_human_gate_wake",
                            "runId": run_id,
                            "workflowRef": row["workflowRef"],
                            "humanGateDelivery": row["humanGateDelivery"],
                            "instruction": WAKE_INSTRUCTION,
                            "cardBody": prompt["humanGatePrompt"]["cardBody"],
                            "actions": prompt["humanGatePrompt"]["actions"],
                            "continuePaused": prompt["continuePaused"],
                        }
                    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
                        _logger.warning("Canon pause notifier: prompt failed; will retry")
                        continue
                    text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
                    if len(text) > MAX_INLINE_CHARS:
                        path = prompts / f"{run_id}-{row['eventCount']}.json"
                        self._write_prompt(path, result.stdout)
                        del message["cardBody"]
                        message["cardBodyFile"] = str(path.resolve())
                        message["cardBodySha256"] = hashlib.sha256(result.stdout).hexdigest()
                        text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
                    if self._ctx.inject_message(text, session_key=session):
                        notified.add(key)
                        self._write_state(state, notified)
                    elif key not in self._warned_keys:
                        self._warned_keys.add(key)
                        _logger.warning("Canon pause notifier: injection refused; will retry")
            finally:
                shutil.rmtree(root, ignore_errors=True)
        except Exception:
            _logger.warning("Canon pause notifier: tick failed; will retry")

    @staticmethod
    def _write_prompt(path: Path, content: bytes) -> None:
        """Pre: path is plugin-owned and content is exact Canon prompt stdout.
        Post: atomically replaces a 0600 file in a 0700 directory. Raises: I/O
        errors retaining any old file and removing the temporary file.
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

    @staticmethod
    def _read_state(path: Path) -> set[str]:
        """Pre: path is plugin-owned. Post: returns persisted keys, or none for a
        missing/empty file. Raises: I/O or invalid-state errors without delivery.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        if not content.strip():
            return set()
        value = json.loads(content)
        keys = value["notified"]
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError("Invalid notification state")
        return set(keys)

    @staticmethod
    def _write_state(path: Path, notified: set[str]) -> None:
        """Pre: path is plugin-owned and keys were accepted for injection. Post:
        replaces state atomically with mode 0600; removes temporary files on
        failure. Raises: filesystem/serialization errors, retaining old state.
        """
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".pause-notifier-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"notified": sorted(notified)}, stream)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
