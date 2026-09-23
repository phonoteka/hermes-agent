"""Scoped lifecycle ownership for one Hermes profile agent session.

This module is the sole Hermes-side owner of the ordinary profile-agent
prepare/run/continue/close lifecycle used by Canon.  It deliberately delegates
configuration and provider selection to the existing Hermes managers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence, cast
import hashlib
import json
import math
from urllib.parse import urlsplit

from agent.secret_scope import (
    build_profile_secret_scope,
    reset_secret_scope,
    set_secret_scope,
)
from hermes_cli.env_loader import hydrate_profile_secret_sources
from hermes_constants import (
    parse_reasoning_effort,
    resolve_reasoning_config,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state import SessionDB


@dataclass(frozen=True)
class ProfileRuntimeBinding:
    """The immutable identity of the profile active in one runtime scope."""

    profile_name: str
    profile_home: Path


class ProfileRuntimeScopeError(RuntimeError):
    """Raised when a profile runtime scope cannot be entered safely."""


def _resolve_profile_home(profile_home: Path | str) -> Path:
    """Validate and resolve a profile home before installing any context."""
    if not isinstance(profile_home, (Path, str)):
        raise ProfileRuntimeScopeError("profile home must be a Path or string")
    if isinstance(profile_home, str) and not profile_home.strip():
        raise ProfileRuntimeScopeError("profile home cannot be empty")
    candidate = Path(profile_home)
    if not candidate.is_absolute():
        raise ProfileRuntimeScopeError("profile home must be an absolute path")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ProfileRuntimeScopeError("profile home could not be resolved") from exc
    if not resolved.exists():
        raise ProfileRuntimeScopeError("profile home does not exist")
    if not resolved.is_dir():
        raise ProfileRuntimeScopeError("profile home must be a directory")
    if not resolved.name or resolved.name in {".", ".."}:
        raise ProfileRuntimeScopeError("profile home has no valid profile name")
    return resolved


@contextmanager
def profile_runtime_scope(profile_home: Path | str) -> Iterator[ProfileRuntimeBinding]:
    """Install one profile's home and secret context for the current task."""
    resolved_home = _resolve_profile_home(profile_home)
    binding = ProfileRuntimeBinding(profile_name=resolved_home.name, profile_home=resolved_home)
    home_token = None
    secret_token = None
    try:
        home_token = set_hermes_home_override(resolved_home)
        hydrate_profile_secret_sources(resolved_home)
        secret_token = set_secret_scope(build_profile_secret_scope(resolved_home))
    except BaseException as exc:
        if secret_token is not None:
            try:
                reset_secret_scope(secret_token)
            finally:
                if home_token is not None:
                    reset_hermes_home_override(home_token)
        elif home_token is not None:
            reset_hermes_home_override(home_token)
        raise ProfileRuntimeScopeError(f"failed to enter profile runtime scope for {binding.profile_name!r}") from exc
    try:
        yield binding
    finally:
        if secret_token is not None:
            try:
                reset_secret_scope(secret_token)
            finally:
                if home_token is not None:
                    reset_hermes_home_override(home_token)
        elif home_token is not None:
            reset_hermes_home_override(home_token)


class ProfileAgentSessionError(RuntimeError):
    """Raised when a profile-agent session cannot be operated safely."""


class ProfileAgentSessionContinuationUnsupported(ProfileAgentSessionError):
    """Raised when a continuation handle is not the same live session object."""


def _path(value: Path | str, name: str) -> Path:
    if not isinstance(value, (Path, str)):
        raise ProfileAgentSessionError(f"{name} must be a path")
    raw = str(value)
    if not raw or not raw.strip() or raw != raw.strip():
        raise ProfileAgentSessionError(f"{name} must be nonblank")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ProfileAgentSessionError(f"{name} must be absolute")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ProfileAgentSessionError(f"{name} could not be resolved") from exc
    if not resolved.is_dir():
        raise ProfileAgentSessionError(f"{name} must be an existing directory")
    return resolved


def _exact_string(value: Any, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProfileAgentSessionError(f"{name} must be an exact nonblank string")
    return value


def _exact_names(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileAgentSessionError(f"{name} must be a sequence of exact strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ProfileAgentSessionError(f"{name} contains a malformed exact string")
        if item in result:
            raise ProfileAgentSessionError(f"{name} must contain unique exact strings")
        result.append(item)
    return tuple(result)


def _json_value(value: Any, *, key: str = "") -> Any:
    """Validate JSON data and return a detached copy suitable for a request."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProfileAgentSessionError("request mappings must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or not child_key:
                raise ProfileAgentSessionError("request mappings must have nonblank string keys")
            result[child_key] = _json_value(child_value, key=child_key)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, key=key) for item in value]
    raise ProfileAgentSessionError("request mappings must contain JSON-compatible values")


def _freeze_json(value: Any) -> Any:
    """Recursively freeze an already detached JSON value."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _toolsets(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileAgentSessionError(f"{name} must be a sequence of exact strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ProfileAgentSessionError(f"{name} contains a malformed toolset")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ProfileAgentSessionRequest:
    """Detached, validated inputs for one profile-agent lifecycle."""

    profile_home: Path | str
    working_directory: Path | str
    session_id: str
    task_id: str
    platform: str
    request_overrides: Mapping[str, Any]
    enabled_toolsets: Sequence[str]
    disabled_toolsets: Sequence[str]
    allowed_tool_names: Sequence[str] = ()
    skill_refs: Sequence[str] = ()
    mcp_server_refs: Sequence[str] = ()
    continuation_root_ref: str | None = None
    continuation_owner_ref: str | None = None
    agent_session_ref: str | None = None
    agent_checkpoint_ref: str | None = None

    exact_model: str | None = None
    exact_provider: str | None = None
    exact_max_turns: int | None = None
    exact_max_tokens: int | None = None
    exact_reasoning: str | None = None
    cleanup_callback: Callable[[], Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_home", _path(self.profile_home, "profile_home"))
        object.__setattr__(self, "working_directory", _path(self.working_directory, "working_directory"))
        object.__setattr__(self, "session_id", _exact_string(self.session_id, "session_id"))
        object.__setattr__(self, "task_id", _exact_string(self.task_id, "task_id"))
        object.__setattr__(self, "platform", _exact_string(self.platform, "platform"))
        if not isinstance(self.request_overrides, Mapping):
            raise ProfileAgentSessionError("request_overrides must be a mapping")
        detached = _json_value(self.request_overrides)
        object.__setattr__(self, "request_overrides", _freeze_json(detached))
        object.__setattr__(self, "enabled_toolsets", _toolsets(self.enabled_toolsets, "enabled_toolsets"))
        object.__setattr__(self, "disabled_toolsets", _toolsets(self.disabled_toolsets, "disabled_toolsets"))
        object.__setattr__(self, "allowed_tool_names", _exact_names(self.allowed_tool_names, "allowed_tool_names"))
        object.__setattr__(self, "skill_refs", _exact_names(self.skill_refs, "skill_refs"))
        object.__setattr__(self, "mcp_server_refs", _exact_names(self.mcp_server_refs, "mcp_server_refs"))
        root_present = self.continuation_root_ref is not None
        owner_present = self.continuation_owner_ref is not None
        if root_present != owner_present:
            raise ProfileAgentSessionError("continuation root and owner refs must be supplied together")
        object.__setattr__(self, "continuation_root_ref", _exact_string(self.continuation_root_ref, "continuation_root_ref", optional=True))
        object.__setattr__(self, "continuation_owner_ref", _exact_string(self.continuation_owner_ref, "continuation_owner_ref", optional=True))
        session_present = self.agent_session_ref is not None
        checkpoint_present = self.agent_checkpoint_ref is not None
        if session_present != checkpoint_present:
            raise ProfileAgentSessionError("agent session and checkpoint refs must be supplied together")
        if session_present and not (root_present and owner_present):
            raise ProfileAgentSessionError("agent continuation refs require root and owner refs")
        object.__setattr__(self, "agent_session_ref", _exact_string(self.agent_session_ref, "agent_session_ref", optional=True))
        object.__setattr__(self, "agent_checkpoint_ref", _exact_string(self.agent_checkpoint_ref, "agent_checkpoint_ref", optional=True))

        object.__setattr__(self, "exact_model", _exact_string(self.exact_model, "exact_model", optional=True))
        object.__setattr__(self, "exact_provider", _exact_string(self.exact_provider, "exact_provider", optional=True))
        if self.exact_max_turns is not None:
            if isinstance(self.exact_max_turns, bool) or not isinstance(self.exact_max_turns, int) or self.exact_max_turns <= 0:
                raise ProfileAgentSessionError("exact_max_turns must be a positive integer")
        if self.exact_max_tokens is not None:
            if isinstance(self.exact_max_tokens, bool) or not isinstance(self.exact_max_tokens, int) or self.exact_max_tokens <= 0:
                raise ProfileAgentSessionError("exact_max_tokens must be a positive integer")
        if self.exact_reasoning is not None:
            _exact_string(self.exact_reasoning, "exact_reasoning")
            if parse_reasoning_effort(self.exact_reasoning) is None:
                raise ProfileAgentSessionError("exact_reasoning is unsupported")
        if self.cleanup_callback is not None and not callable(self.cleanup_callback):
            raise ProfileAgentSessionError("cleanup_callback must be callable")


@dataclass(frozen=True, slots=True)
class ProfileAgentSessionSnapshot:
    """Credential-free effective settings captured at prepare time."""

    effective_provider: str
    effective_model: str
    api_mode: str
    max_turns: int
    max_tokens: int | None
    reasoning: Mapping[str, Any] | None
    enabled_toolsets: tuple[str, ...]
    disabled_toolsets: tuple[str, ...]
    request_settings: Mapping[str, Any]
    profile_digest: str
    soul_digest: str
    allowed_tool_names: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    loaded_skill_names: tuple[str, ...] = ()
    skill_prompt_digest: str = ""
    mcp_server_refs: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ProfileAgentContinuationFacts:
    """Opaque, credential-free continuation facts exposed by one prepared session."""

    agent_session_ref: str = ""
    agent_checkpoint_ref: str = ""
    continuation_mode: str = "fresh"
    history_count: int = 0


_CREDENTIAL_MARKERS = (
    "api_key",
    "token",
    "password",
    "secret",
    "credential",
    "authorization",
    "auth",
    "cookie",
    "private_key",
    "access_key",
)


def _safe_snapshot(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        if any(not isinstance(child_key, str) or not child_key for child_key in value):
            raise ProfileAgentSessionError("snapshot mappings must have nonblank string keys")
        return {
            child_key: _safe_snapshot(child_value, key=child_key)
            for child_key, child_value in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_safe_snapshot(item, key=key) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ProfileAgentSessionError("snapshot contains a nonfinite value")
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return "[REDACTED]"
        except ValueError:
            return "[REDACTED]"
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProfileAgentSessionError("snapshot contains a non-JSON value") from exc
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _profile_digest(profile_home: Path, config: Mapping[str, Any]) -> str:
    skills = []
    skills_dir = profile_home / "skills"
    if skills_dir.is_dir():
        for path in sorted(item for item in skills_dir.rglob("*") if item.is_file()):
            skill_path = path.relative_to(skills_dir)
            if "__pycache__" in skill_path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            skills.append({"path": path.relative_to(profile_home).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"config": _safe_snapshot(config), "skills": skills}
    return "sha256:" + hashlib.sha256(_canonical(manifest)).hexdigest()


def _soul_digest(profile_home: Path) -> str:
    soul = profile_home / "SOUL.md"
    if not soul.is_file():
        return "missing"
    return "sha256:" + hashlib.sha256(soul.read_bytes()).hexdigest()


def _config_mapping(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    return value if isinstance(value, Mapping) else {}


def _merge_request_overrides(config: Mapping[str, Any], provider: str, request: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (
        config.get("request_overrides"),
        _config_mapping(config, "model").get("request_overrides"),
        _config_mapping(_config_mapping(config, "providers"), provider).get("request_overrides"),
        request,
    ):
        if source is None:
            continue
        if not isinstance(source, Mapping):
            raise ProfileAgentSessionError("request override configuration must be a mapping")
        merged.update(_json_value(source))
    return merged


def _effective_reasoning(
    config: Mapping[str, Any],
    model: str,
    request: ProfileAgentSessionRequest,
) -> dict[str, Any] | None:
    if request.exact_reasoning is not None:
        value = parse_reasoning_effort(request.exact_reasoning)
    else:
        value = resolve_reasoning_config(dict(config), model)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProfileAgentSessionError("effective reasoning configuration is malformed")
    detached = _json_value(value)
    if not isinstance(detached, dict):
        raise ProfileAgentSessionError("effective reasoning configuration is malformed")
    return detached


def _effective_max_turns(agent_config: Mapping[str, Any], request: ProfileAgentSessionRequest) -> int:
    value = request.exact_max_turns if request.exact_max_turns is not None else agent_config.get("max_turns", 500)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileAgentSessionError("effective max turns must be a positive integer")
    return value


def _continuation_identity(
    request: ProfileAgentSessionRequest,
    snapshot: ProfileAgentSessionSnapshot,
) -> tuple[str, str]:
    """Return the private agent identity and its opaque stable session reference."""
    root = request.continuation_root_ref or f"implicit-root:{snapshot.profile_digest}:{request.session_id}"
    owner = request.continuation_owner_ref or f"implicit-owner:{request.platform}:{request.task_id}"
    identity_request_settings = _safe_snapshot(snapshot.request_settings)
    if isinstance(identity_request_settings, dict):
        text_settings = identity_request_settings.get("text")
        if isinstance(text_settings, dict):
            format_settings = text_settings.get("format")
            if (
                isinstance(format_settings, dict)
                and format_settings.keys() == {"type", "name", "strict", "schema"}
                and format_settings.get("type") == "json_schema"
                and format_settings.get("name") == "canon_phase_output"
                and format_settings.get("strict") is True
                and isinstance(format_settings.get("schema"), dict)
            ):
                del format_settings["schema"]
    identity_input = {
        "root_ref": root,
        "owner_ref": owner,
        "profile_digest": snapshot.profile_digest,
        "provider": snapshot.effective_provider,
        "model": snapshot.effective_model,
        "api_mode": snapshot.api_mode,
        "max_turns": snapshot.max_turns,
        "max_tokens": snapshot.max_tokens,
        "reasoning": _safe_snapshot(snapshot.reasoning),
        "enabled_toolsets": snapshot.enabled_toolsets,
        "disabled_toolsets": snapshot.disabled_toolsets,
        "allowed_tool_names": snapshot.allowed_tool_names,
        "skill_refs": snapshot.skill_refs,
        "loaded_skill_names": snapshot.loaded_skill_names,
        "skill_prompt_digest": snapshot.skill_prompt_digest,
        "mcp_server_refs": snapshot.mcp_server_refs,
        "request_settings": identity_request_settings,
    }
    digest = hashlib.sha256(_canonical(identity_input)).hexdigest()
    private_identity = "canon-" + digest
    return private_identity, "sha256:" + hashlib.sha256(private_identity.encode("ascii")).hexdigest()


def _durable_history(db: SessionDB, session_id: str) -> list[dict[str, Any]]:
    """Load and validate the exact durable model history required for replay."""
    try:
        history = db.get_messages_as_conversation(session_id, repair_alternation=True)
    except BaseException as exc:
        raise ProfileAgentSessionContinuationUnsupported("durable continuation history could not be read") from exc
    if not isinstance(history, list) or not history:
        raise ProfileAgentSessionContinuationUnsupported("durable continuation history is empty")
    validated: list[dict[str, Any]] = []
    for message in history:
        if not isinstance(message, Mapping):
            raise ProfileAgentSessionContinuationUnsupported("durable continuation history is malformed")
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ProfileAgentSessionContinuationUnsupported("durable continuation history is malformed")
        try:
            detached = _json_value(dict(message))
        except ProfileAgentSessionError as exc:
            raise ProfileAgentSessionContinuationUnsupported("durable continuation history is malformed") from exc
        if not isinstance(detached, dict):
            raise ProfileAgentSessionContinuationUnsupported("durable continuation history is malformed")
        validated.append(detached)
    return validated


def _checkpoint_ref(private_identity: str, history: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "private_identity": private_identity,
        "history_count": len(history),
        "history": list(history),
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _filter_agent_tools(agent: Any, allowed_tool_names: tuple[str, ...], mcp_server_refs: tuple[str, ...]) -> None:
    """Restrict one ordinary agent to the exact Canon-declared tool surface."""
    raw_tools = getattr(agent, "tools", None)
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        raise ProfileAgentSessionError("ordinary agent returned malformed tool definitions")
    by_name: dict[str, dict[str, Any]] = {}
    for definition in raw_tools:
        if not isinstance(definition, Mapping):
            raise ProfileAgentSessionError("ordinary agent returned malformed tool definition")
        function = definition.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(name, str) or not name or name != name.strip():
            raise ProfileAgentSessionError("ordinary agent returned a tool without an exact name")
        if name in by_name:
            raise ProfileAgentSessionError("ordinary agent returned duplicate tool names")
        by_name[name] = dict(definition)

    missing = tuple(name for name in allowed_tool_names if name not in by_name)
    if missing:
        raise ProfileAgentSessionError("declared tool capability is unavailable: " + ", ".join(missing))
    selected_names = set(allowed_tool_names)
    agent.tools = [definition for definition in raw_tools if definition["function"]["name"] in selected_names]
    agent.valid_tool_names = selected_names
    for server in mcp_server_refs:
        prefix = f"mcp__{server}__"
        if not any(name.startswith(prefix) for name in agent.valid_tool_names):
            raise ProfileAgentSessionError(f"declared MCP server capability is unavailable: {server}")


class PreparedProfileAgentSession:
    """One live ordinary agent and its lock-protected exactly-once close state."""

    __slots__ = (
        "_agent",
        "_request",
        "_snapshot",
        "_session_db",
        "_owns_session_db",
        "_private_identity",
        "_continuation_state",
        "_cleanup_callback",
        "_lock",
        "_closed",
        "_has_result",
        "_last_result",
    )

    def __init__(
        self,
        agent: Any,
        request: ProfileAgentSessionRequest,
        snapshot: ProfileAgentSessionSnapshot,
        session_db: SessionDB,
        owns_session_db: bool,
        private_identity: str,
        continuation_state: ProfileAgentContinuationFacts,
        cleanup_callback: Callable[[], Any] | None,
        *,
        _internal: object,
    ) -> None:
        if _internal is not _SESSION_CONSTRUCTOR:
            raise ProfileAgentSessionError("prepared session construction is internal")
        self._agent = agent
        self._request = request
        self._snapshot = snapshot
        self._session_db = session_db
        self._owns_session_db = owns_session_db
        self._private_identity = private_identity
        self._continuation_state = continuation_state
        self._cleanup_callback = cleanup_callback
        self._lock = Lock()
        self._closed = False
        self._has_result = False
        self._last_result = None

    @property
    def agent(self) -> Any:
        return self._agent

    @property
    def request(self) -> ProfileAgentSessionRequest:
        return self._request

    @property
    def snapshot(self) -> ProfileAgentSessionSnapshot:
        return self._snapshot

    @property
    def continuation_state(self) -> ProfileAgentContinuationFacts:
        return self._continuation_state

    def _set_continuation_state(self, state: ProfileAgentContinuationFacts) -> None:
        self._continuation_state = state

    def _claim_close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            return True

_SESSION_CONSTRUCTOR = object()


@contextmanager
def _runtime_scopes(request: ProfileAgentSessionRequest) -> Iterator[None]:
    """Enter both runtime scopes without leaking the first on partial entry."""
    from agent.runtime_cwd import session_cwd_scope

    with profile_runtime_scope(request.profile_home):
        with session_cwd_scope(request.working_directory):
            yield


def _run_cleanup(callback: Callable[[], Any] | None) -> BaseException | None:
    if callback is None:
        return None
    try:
        callback()
    except BaseException as exc:
        return exc
    return None


def _is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, InterruptedError, SystemExit, GeneratorExit)) or type(exc).__name__ == "CancelledError"


def prepare_profile_agent_session(
    request: ProfileAgentSessionRequest,
    *,
    agent_factory: Callable[..., Any] | None = None,
) -> PreparedProfileAgentSession:
    """Prepare one manager-backed ordinary agent under both runtime scopes."""
    if type(request) is not ProfileAgentSessionRequest:
        raise ProfileAgentSessionError("prepare requires ProfileAgentSessionRequest")
    agent = None
    session_db: SessionDB | None = None
    owns_session_db = False
    private_identity = ""
    initial_state = ProfileAgentContinuationFacts()
    skills_prompt = ""
    loaded_skill_names: tuple[str, ...] = ()
    skill_prompt_digest = ""
    try:
        with _runtime_scopes(request):
            # Keep all manager calls, declared-skill loading, and construction inside
            # the exact profile and session-CWD scopes.
            from agent.skill_commands import build_preloaded_skills_prompt
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider

            skills_result = build_preloaded_skills_prompt(
                list(request.skill_refs),
                task_id=request.task_id,
            )
            if not isinstance(skills_result, tuple) or len(skills_result) != 3:
                raise ProfileAgentSessionError("skill loader returned a malformed result")
            skills_prompt, raw_loaded_skill_names, raw_missing_skill_refs = skills_result
            if not isinstance(skills_prompt, str):
                raise ProfileAgentSessionError("skill loader returned a malformed prompt")
            loaded_skill_names = _exact_names(raw_loaded_skill_names, "loaded_skill_names")
            missing_skill_refs = _exact_names(raw_missing_skill_refs, "missing_skill_refs")
            if missing_skill_refs or len(loaded_skill_names) != len(request.skill_refs):
                missing = missing_skill_refs or request.skill_refs
                raise ProfileAgentSessionError("declared skill capability is unavailable: " + ", ".join(missing))
            if request.skill_refs and not skills_prompt:
                raise ProfileAgentSessionError("declared skills produced no preloaded prompt")
            skill_prompt_digest = "sha256:" + hashlib.sha256(skills_prompt.encode("utf-8")).hexdigest()

            config = load_config()
            if not isinstance(config, Mapping):
                raise ProfileAgentSessionError("profile configuration is not a mapping")
            raw_model_config = config.get("model")
            model_config = raw_model_config if isinstance(raw_model_config, Mapping) else {}
            configured_model = raw_model_config if isinstance(raw_model_config, str) else model_config.get("default") or model_config.get("model")
            configured_provider = model_config.get("provider")
            requested_provider = request.exact_provider or configured_provider
            requested_provider = cast(str, _exact_string(requested_provider, "effective provider"))
            model = request.exact_model or configured_model
            model = cast(str, _exact_string(model, "effective model"))
            runtime = resolve_runtime_provider(requested=requested_provider, target_model=model)
            if not isinstance(runtime, Mapping):
                raise ProfileAgentSessionError("runtime provider manager returned a malformed result")
            resolved_provider = cast(str, _exact_string(runtime.get("provider"), "resolved provider"))
            api_mode = cast(str, _exact_string(runtime.get("api_mode"), "resolved API mode"))
            if resolved_provider != requested_provider:
                raise ProfileAgentSessionError("runtime provider manager identity drifted")
            base_url = runtime.get("base_url")
            api_key = runtime.get("api_key")
            max_turns = _effective_max_turns(_config_mapping(config, "agent"), request)
            reasoning = _effective_reasoning(config, model, request)
            overrides = _merge_request_overrides(config, resolved_provider, request.request_overrides)
            safe_overrides = _safe_snapshot(overrides)
            profile_digest = _profile_digest(request.profile_home, config)
            snapshot = ProfileAgentSessionSnapshot(
                effective_provider=resolved_provider,
                effective_model=model,
                api_mode=api_mode,
                max_turns=max_turns,
                max_tokens=request.exact_max_tokens,
                reasoning=_freeze_json(reasoning) if reasoning is not None else None,
                enabled_toolsets=tuple(request.enabled_toolsets),
                disabled_toolsets=tuple(request.disabled_toolsets),
                request_settings=_freeze_json(safe_overrides),
                profile_digest=profile_digest,
                soul_digest=_soul_digest(request.profile_home),
                allowed_tool_names=request.allowed_tool_names,
                skill_refs=request.skill_refs,
                loaded_skill_names=loaded_skill_names,
                skill_prompt_digest=skill_prompt_digest,
                mcp_server_refs=request.mcp_server_refs,
            )
            private_identity, agent_session_ref = _continuation_identity(request, snapshot)
            session_db = SessionDB(db_path=Path(request.profile_home) / "state.db")
            owns_session_db = True
            try:
                existing_row = session_db.get_session(private_identity)
            except BaseException as exc:
                if request.agent_session_ref is not None:
                    raise ProfileAgentSessionContinuationUnsupported("private continuation session could not be read") from exc
                existing_row = None
            if request.agent_session_ref is not None:
                if existing_row is None:
                    raise ProfileAgentSessionContinuationUnsupported("private continuation session is missing")
                history = _durable_history(session_db, private_identity)
                initial_state = ProfileAgentContinuationFacts(
                    agent_session_ref=agent_session_ref,
                    agent_checkpoint_ref=_checkpoint_ref(private_identity, history),
                    continuation_mode="replayed",
                    history_count=len(history),
                )
            else:
                if existing_row is not None:
                    try:
                        prior_history = session_db.get_messages_as_conversation(private_identity, repair_alternation=True)
                    except BaseException as exc:
                        raise ProfileAgentSessionContinuationUnsupported("private session already has unreadable history") from exc
                    if prior_history:
                        raise ProfileAgentSessionContinuationUnsupported("initial profile session already has durable history")
                initial_state = ProfileAgentContinuationFacts(agent_session_ref=agent_session_ref)

            agent_kwargs = {
                "provider": resolved_provider,
                "requested_provider": runtime.get("requested_provider") or requested_provider,
                "api_mode": api_mode,
                "base_url": base_url,
                "api_key": api_key,
                "credential_pool": runtime.get("credential_pool"),
                "model": model,
                "max_iterations": max_turns,
                "max_tokens": request.exact_max_tokens,
                "reasoning_config": reasoning,
                "request_overrides": overrides,
                "ephemeral_system_prompt": skills_prompt,
                "session_id": private_identity,
                "enabled_toolsets": list(request.enabled_toolsets),
                "disabled_toolsets": list(request.disabled_toolsets),
                "skip_context_files": True,
                "skip_memory": True,
                "skip_background_review": True,
                "load_soul_identity": True,
                "session_db": session_db,
                "parent_session_id": None,
                "quiet_mode": True,
                "platform": request.platform,
            }
            if agent_factory is None:
                from run_agent import AIAgent
                agent = AIAgent(**agent_kwargs)
                if request.allowed_tool_names:
                    from model_tools import get_tool_definitions

                    agent.tools = get_tool_definitions(
                        enabled_toolsets=list(request.enabled_toolsets),
                        disabled_toolsets=list(request.disabled_toolsets),
                        quiet_mode=True,
                        skip_tool_search_assembly=True,
                    )
            else:
                agent = agent_factory(**agent_kwargs)
            if agent is None or not callable(getattr(agent, "run_conversation", None)):
                raise ProfileAgentSessionError("agent construction returned an invalid ordinary agent")
            _filter_agent_tools(agent, request.allowed_tool_names, request.mcp_server_refs)
            return PreparedProfileAgentSession(
                agent,
                request,
                snapshot,
                session_db,
                owns_session_db,
                private_identity,
                initial_state,
                request.cleanup_callback,
                _internal=_SESSION_CONSTRUCTOR,
            )
    except BaseException as exc:
        if agent is not None:
            try:
                with _runtime_scopes(request):
                    close = getattr(agent, "close", None)
                    if callable(close):
                        close()
            except BaseException:
                pass
        db_close_error = None
        if owns_session_db and session_db is not None:
            try:
                session_db.close()
            except BaseException as db_exc:
                db_close_error = db_exc
        cleanup_error = _run_cleanup(request.cleanup_callback)
        if _is_cancellation(exc):
            raise
        if isinstance(exc, ProfileAgentSessionError):
            raise
        if db_close_error is not None or cleanup_error is not None:
            raise ProfileAgentSessionError("profile agent session preparation and cleanup failed") from exc
        raise ProfileAgentSessionError("profile agent session preparation failed") from exc


def _require_open_session(session: Any, *, continuation: bool = False) -> PreparedProfileAgentSession:
    if type(session) is not PreparedProfileAgentSession:
        if continuation:
            raise ProfileAgentSessionContinuationUnsupported("continuation requires the same live prepared session")
        raise ProfileAgentSessionError("profile agent session is not live")
    return session


def _run(
    session: PreparedProfileAgentSession,
    prompt: Any,
    *,
    continuation: bool,
    task_id: str | None,
    final_response_validator: Callable[..., Any] | None,
    max_final_response_validation_retries: int,
) -> Any:
    if not isinstance(prompt, str):
        raise ProfileAgentSessionError("session prompt must be a string")
    if not isinstance(max_final_response_validation_retries, int) or isinstance(max_final_response_validation_retries, bool) or max_final_response_validation_retries < 0:
        raise ProfileAgentSessionError("validation retries must be a nonnegative integer")
    with session._lock:
        if session._closed:
            if continuation:
                raise ProfileAgentSessionContinuationUnsupported("continuation requires the same live prepared session")
            raise ProfileAgentSessionError("profile agent session is not live")
        if not continuation and session.request.agent_session_ref is not None:
            raise ProfileAgentSessionContinuationUnsupported(
                "prepared continuation refs require continue_profile_agent_session"
            )
        with _runtime_scopes(session.request):
            history: list[dict[str, Any]] = []
            pre_mode = "fresh"
            pre_count = 0
            if continuation:
                history = _durable_history(session._session_db, session.agent.session_id)
                pre_mode = "replayed"
                pre_count = len(history)
                supplied_session_ref = session.request.agent_session_ref
                supplied_checkpoint_ref = session.request.agent_checkpoint_ref
                current_checkpoint_ref = _checkpoint_ref(session._private_identity, history)
                if supplied_session_ref is not None and (
                    supplied_session_ref != session._continuation_state.agent_session_ref
                    or supplied_checkpoint_ref != current_checkpoint_ref
                ):
                    raise ProfileAgentSessionContinuationUnsupported("continuation refs do not match durable state")
            kwargs: dict[str, Any] = {
                "task_id": task_id or session.request.task_id,
                "conversation_history": history,
            }
            if final_response_validator is not None:
                if not callable(final_response_validator):
                    raise ProfileAgentSessionError("final response validator must be callable")
                kwargs["final_response_validator"] = final_response_validator
                kwargs["max_final_response_validation_retries"] = max_final_response_validation_retries
            result = session._agent.run_conversation(prompt, **kwargs)
            durable_after = _durable_history(session._session_db, session.agent.session_id)
            session._set_continuation_state(
                ProfileAgentContinuationFacts(
                    agent_session_ref=session._continuation_state.agent_session_ref,
                    agent_checkpoint_ref=_checkpoint_ref(session._private_identity, durable_after),
                    continuation_mode=pre_mode,
                    history_count=pre_count,
                )
            )
            session._last_result = result
            session._has_result = True
            return result


def run_profile_agent_session(
    session: PreparedProfileAgentSession,
    prompt: str,
    *,
    task_id: str | None = None,
    final_response_validator: Callable[..., Any] | None = None,
    max_final_response_validation_retries: int = 2,
) -> Any:
    """Run the already-prepared live agent under profile and cwd scopes."""
    return _run(_require_open_session(session), prompt, continuation=False, task_id=task_id, final_response_validator=final_response_validator, max_final_response_validation_retries=max_final_response_validation_retries)


def continue_profile_agent_session(
    session: PreparedProfileAgentSession,
    prompt: str,
    *,
    task_id: str | None = None,
    final_response_validator: Callable[..., Any] | None = None,
    max_final_response_validation_retries: int = 2,
) -> Any:
    """Continue only through the same live prepared session and same agent."""
    prepared = _require_open_session(session, continuation=True)
    return _run(prepared, prompt, continuation=True, task_id=task_id, final_response_validator=final_response_validator, max_final_response_validation_retries=max_final_response_validation_retries)


def wait_profile_agent_session(session: PreparedProfileAgentSession) -> Any:
    """Wait behind an active synchronous run and return its exact result.

    ``AIAgent.run_conversation`` is synchronous, so the session lock is the
    completion barrier.  This adds no background queue or execution path.
    """
    prepared = _require_open_session(session)
    with prepared._lock:
        if prepared._closed:
            raise ProfileAgentSessionError("profile agent session is not live")
        if not prepared._has_result:
            raise ProfileAgentSessionError("profile agent session has no completed run to wait for")
        return prepared._last_result


def close_profile_agent_session(session: PreparedProfileAgentSession) -> None:
    """Close one prepared session exactly once; repeated close is a no-op."""
    if type(session) is not PreparedProfileAgentSession:
        raise ProfileAgentSessionError("close requires PreparedProfileAgentSession")
    if not session._claim_close():
        return
    primary: BaseException | None = None
    db_close_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        try:
            with _runtime_scopes(session.request):
                close = getattr(session._agent, "close", None)
                if callable(close):
                    close()
        except BaseException as exc:
            primary = exc
        finally:
            if session._owns_session_db:
                try:
                    session._owns_session_db = False
                    session._session_db.close()
                except BaseException as exc:
                    db_close_error = exc
            cleanup_error = _run_cleanup(session._cleanup_callback)
    finally:
        if primary is not None:
            raise primary
        if db_close_error is not None:
            raise db_close_error
        if cleanup_error is not None:
            raise cleanup_error


__all__ = [
    "ProfileRuntimeBinding",
    "ProfileRuntimeScopeError",
    "profile_runtime_scope",

    "ProfileAgentSessionRequest",
    "ProfileAgentSessionSnapshot",
    "ProfileAgentContinuationFacts",
    "PreparedProfileAgentSession",
    "ProfileAgentSessionError",
    "ProfileAgentSessionContinuationUnsupported",
    "prepare_profile_agent_session",
    "run_profile_agent_session",
    "continue_profile_agent_session",
    "wait_profile_agent_session",
    "close_profile_agent_session",
]
