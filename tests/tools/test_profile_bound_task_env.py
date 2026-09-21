"""S5 RED behavior tests for the current decomposed task-environment owner.

These tests intentionally exercise public task-environment behavior through the
current terminal-tool facade.  Backend control-plane calls are replaced with
local doubles; no Docker daemon or network is involved.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.terminal_tool as tt
import tools.terminal_tool_lifecycle as ttl


@pytest.fixture(autouse=True)
def clean_task_environment_state():
    """Restore task registries and lifecycle state around every falsifier."""
    registry_names = (
        "_task_environment_configs",
        "_task_env_overrides",
        "_active_environments",
        "_last_activity",
        "_session_cwd",
        "_container_aliases",
        "_creation_locks",
    )
    snapshots = {
        name: dict(getattr(tt, name))
        for name in registry_names
        if hasattr(tt, name)
    }
    locks = {
        name: getattr(tt, name, None)
        for name in (
            "_task_environment_lock",
            "_env_lock",
            "_session_cwd_lock",
            "_container_alias_lock",
            "_creation_locks_lock",
        )
    }

    for name in ("_task_environment_configs", "_task_env_overrides"):
        mapping = getattr(tt, name, None)
        lock = locks.get("_task_environment_lock") if name == "_task_environment_configs" else None
        if mapping is not None:
            if lock is None:
                mapping.clear()
            else:
                with lock:
                    mapping.clear()
    for name, lock_name in (
        ("_active_environments", "_env_lock"),
        ("_last_activity", "_env_lock"),
        ("_session_cwd", "_session_cwd_lock"),
        ("_container_aliases", "_container_alias_lock"),
        ("_creation_locks", "_creation_locks_lock"),
    ):
        mapping = getattr(tt, name, None)
        if mapping is not None:
            lock = locks.get(lock_name)
            if lock is None:
                mapping.clear()
            else:
                with lock:
                    mapping.clear()

    yield

    for name, before in snapshots.items():
        mapping = getattr(tt, name, None)
        if mapping is None:
            continue
        lock = locks.get(
            "_task_environment_lock"
            if name == "_task_environment_configs"
            else "_env_lock"
            if name in {"_active_environments", "_last_activity"}
            else "_session_cwd_lock"
            if name == "_session_cwd"
            else "_container_alias_lock"
            if name == "_container_aliases"
            else "_creation_locks_lock"
        )
        if lock is None:
            mapping.clear()
            mapping.update(before)
        else:
            with lock:
                mapping.clear()
                mapping.update(before)


def _api(name):
    """Turn an omitted owner into an assertion-level RED, not collection error."""
    candidate = getattr(tt, name, None)
    assert callable(candidate), f"missing task-environment owner API: {name}"
    return candidate


def profile(backend="docker", **extra):
    terminal = {
        "backend": backend,
        "cwd": "/workspace/task",
        "docker_image": "sentinel-image:1",
        "docker_mount_cwd_to_workspace": False,
        "container_persistent": False,
        "docker_network": False,
        "docker_volumes": ["/data:/data:ro"],
        "docker_env": {"TASK_SENTINEL": "one"},
        "container_cpu": 2,
        "container_memory": 1024,
        "container_disk": 2048,
        "timeout": 180,
    }
    terminal.update(extra)
    return {"terminal": terminal}


def _actual_state_facts():
    return {
        "backend": "docker",
        "containerId": "container-s5-red",
        "imageId": "sha256:" + "1" * 64,
        "inspectDigest": "sha256:" + "2" * 64,
        "workspaceMount": {
            "sourceDigest": "sha256:" + "3" * 64,
            "target": "/workspace",
            "mode": "rw",
        },
        "networkMode": "none",
        "controls": {
            "capDrop": ["ALL"],
            "securityOpt": ["no-new-privileges"],
            "privileged": False,
            "readonlyRootfs": False,
            "pidsLimit": 256,
            "nanoCpus": 500000000,
            "memory": 1073741824,
        },
    }


def _task_digest(snapshot):
    return snapshot.digest if snapshot.digest.startswith("sha256:") else "sha256:" + snapshot.digest


def test_snapshot_is_immutable_and_resolution_is_defensive():
    register = _api("register_task_environment_config")
    resolve = _api("resolve_task_environment_config")

    snapshot = register("s5-immutable", profile())
    with pytest.raises(TypeError):
        snapshot.values["cwd"] = "/changed"

    resolved = resolve("s5-immutable")
    resolved["docker_env"]["TASK_SENTINEL"] = "changed"
    assert resolve("s5-immutable")["docker_env"]["TASK_SENTINEL"] == "one"


def test_equal_duplicate_registration_is_idempotent():
    register = _api("register_task_environment_config")

    first = register("s5-duplicate", profile())
    second = register("s5-duplicate", profile())
    assert second is first


def test_digest_mismatch_and_override_widening_fail_before_creation():
    register = _api("register_task_environment_config")
    binding_error = _api("TaskEnvironmentBindingError")
    register_overrides = _api("register_task_env_overrides")

    register("s5-bound", profile())
    with pytest.raises(binding_error) as mismatch:
        register("s5-bound", profile(docker_image="other:2"))
    assert mismatch.value.code == "task_environment_digest_mismatch"

    with pytest.raises(binding_error) as widening:
        register_overrides("s5-bound", {"docker_network": True})
    assert widening.value.code == "task_environment_widening_rejected"


def test_registered_task_selects_backend_independently_from_ambient_scope(monkeypatch):
    register = _api("register_task_environment_config")
    resolve = _api("resolve_task_environment_config")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    register("s5-registered-docker", profile("docker"))
    assert resolve("s5-registered-docker")["env_type"] == "docker"
    assert resolve(None)["env_type"] == "local"
    assert tt._resolve_container_task_id("s5-registered-docker") == "s5-registered-docker"

    with ThreadPoolExecutor(max_workers=2) as pool:
        selected = list(
            pool.map(
                lambda task_id: resolve(task_id)["env_type"],
                ["s5-registered-docker", None],
            )
        )
    assert selected == ["docker", "local"]


def test_snapshot_preserves_current_creation_policy_fields():
    register = _api("register_task_environment_config")
    resolve = _api("resolve_task_environment_config")
    selected = profile("docker")
    selected["terminal"].update({
        "docker_shared_container_key": "current-profile-shared",
        "docker_snap_compat": True,
    })

    register("s5-current-complete-policy", selected)
    resolved = resolve("s5-current-complete-policy")
    assert isinstance(resolved, dict)

    assert resolved["docker_shared_container_key"] == "current-profile-shared"
    assert resolved["docker_snap_compat"] is True


def test_required_backend_mismatch_fails_before_environment_creation(monkeypatch):
    ensure = _api("ensure_task_env")
    failure_type = _api("TaskEnvironmentFailure")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    with patch.object(tt, "_create_environment", side_effect=AssertionError("must not create"), create=True):
        result = ensure("s5-required-docker", required_backend="docker")

    assert isinstance(result, failure_type)
    assert result.code == "required_backend_mismatch"
    assert result.backend == "local"
    assert ttl.get_active_env("s5-required-docker") is None


def test_actual_state_attestation_has_exact_sanitized_shape_and_digest():
    register = _api("register_task_environment_config")
    describe = _api("describe_task_environment_actual_state")

    task_id = "s5-attestation"
    snapshot = register(task_id, profile("docker"))
    fake = SimpleNamespace(describe_actual_state=_actual_state_facts)
    with tt._env_lock:
        tt._active_environments[task_id] = fake

    attested = describe(task_id, required_backend="docker")
    assert set(attested) == {
        "backend",
        "containerId",
        "imageId",
        "inspectDigest",
        "workspaceMount",
        "networkMode",
        "controls",
        "taskEnvironmentDigest",
    }
    assert attested["backend"] == "docker"
    assert attested["networkMode"] == "none"
    assert attested["workspaceMount"] == {
        "sourceDigest": "sha256:" + "3" * 64,
        "target": "/workspace",
        "mode": "rw",
    }
    assert attested["taskEnvironmentDigest"] == _task_digest(snapshot)
    assert "env" not in attested
    assert "config" not in attested
    assert "labels" not in attested


def test_actual_state_attestation_rejects_malformed_or_secret_projection():
    register = _api("register_task_environment_config")
    describe = _api("describe_task_environment_actual_state")

    task_id = "s5-attestation-reject"
    register(task_id, profile("docker"))
    malformed = _actual_state_facts()
    malformed["env"] = {"TASK_SECRET": "must-not-leak"}
    fake = SimpleNamespace(describe_actual_state=lambda: deepcopy(malformed))
    with tt._env_lock:
        tt._active_environments[task_id] = fake

    with pytest.raises(RuntimeError):
        describe(task_id, required_backend="docker")


def test_cleanup_removes_exactly_one_task_environment_and_owned_state():
    register = _api("register_task_environment_config")
    cleanup = _api("cleanup_task_environment")

    task_id = "s5-cleanup-target"
    sibling_id = "s5-cleanup-sibling"
    register(task_id, profile("docker"))
    register(sibling_id, profile("docker"))
    cleanup_calls = []
    target_env = SimpleNamespace(cleanup=lambda: cleanup_calls.append(task_id))
    sibling_env = SimpleNamespace(cleanup=lambda: cleanup_calls.append(sibling_id))

    tt.record_session_cwd(task_id, "/workspace/target")
    tt.record_session_cwd(sibling_id, "/workspace/sibling")
    tt.register_container_alias(task_id, "s5-parent")
    with tt._env_lock:
        tt._active_environments[task_id] = target_env
        tt._active_environments[sibling_id] = sibling_env
        tt._last_activity[task_id] = 1.0
        tt._last_activity[sibling_id] = 1.0
    with tt._creation_locks_lock:
        import threading

        tt._creation_locks[task_id] = threading.Lock()
        tt._creation_locks[sibling_id] = threading.Lock()

    result = cleanup(task_id)

    assert cleanup_calls == [task_id]
    assert result["taskId"] == task_id
    assert result["status"] == "cleaned"
    assert all(result["registryClean"].values())
    assert tt.get_task_environment_config(task_id) is None
    assert ttl.get_active_env(task_id) is None
    assert task_id not in tt._creation_locks
    assert task_id not in tt._session_cwd
    assert task_id not in tt._container_aliases
    assert tt.get_task_environment_config(sibling_id) is not None
    assert ttl.get_active_env(sibling_id) is sibling_env
    assert sibling_id in tt._creation_locks
    assert sibling_id in tt._session_cwd


def test_adjacent_override_controls_remain_green(monkeypatch):
    register_overrides = getattr(tt, "register_task_env_overrides", None)
    clear_overrides = getattr(tt, "clear_task_env_overrides", None)
    assert callable(register_overrides)
    assert callable(clear_overrides)

    task_id = "s5-adjacent-control"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_overrides(task_id, {"cwd": "/workspace/control"})
    assert tt.resolve_task_overrides(task_id)["cwd"] == "/workspace/control"

    clear_overrides(task_id)
    assert tt.resolve_task_overrides(task_id) == {}
    assert tt.get_session_cwd(task_id) is None
