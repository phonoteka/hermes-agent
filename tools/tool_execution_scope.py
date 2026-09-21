from __future__ import annotations

import argparse
import ctypes
import glob
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


_EFFECT_MODES = frozenset({"read-only", "read-write"})
_C0_OR_DEL = frozenset(chr(value) for value in range(32)) | {chr(127)}


@dataclass(frozen=True)
class ToolExecutionScope:
    """Trusted per-run Canon filesystem-effect authority.

    The Canon projection uses root-relative glob patterns.  The scope retains
    those patterns verbatim after canonical validation; callers must satisfy
    both root containment and a matching exact effect pattern.
    """

    run_id: str
    phase_id: str | None
    attempt_ref: str | None
    root_directory: str
    working_directory: str
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    sandbox_profile_id: str
    mode: str
    read_allowlist: tuple[str, ...]
    write_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        root_directory = self._normalize_directory("root_directory", self.root_directory)
        working_directory = self._normalize_directory("working_directory", self.working_directory)
        if not self._contains(root_directory, working_directory):
            raise ValueError("working_directory must stay within root_directory")
        read_roots = self._normalize_roots("read_roots", self.read_roots, require_non_empty=False, root=root_directory)
        write_roots = self._normalize_roots("write_roots", self.write_roots, require_non_empty=False, root=root_directory)
        sandbox_profile_id = str(self.sandbox_profile_id or "").strip()
        if not sandbox_profile_id:
            raise ValueError("sandbox_profile_id is required")

        raw_mode = str(self.mode or "").strip()
        if raw_mode not in _EFFECT_MODES:
            raise ValueError("mode must be read-only or read-write")
        if raw_mode == "read-only" and write_roots:
            raise ValueError("read-only mode must not declare write_roots")
        read_allowlist = self._normalize_patterns("read_allowlist", self.read_allowlist, require_non_empty=False)
        write_allowlist = self._normalize_patterns("write_allowlist", self.write_allowlist, require_non_empty=False)
        if raw_mode == "read-only" and write_allowlist:
            raise ValueError("read-only mode must not declare write_allowlist")
        if raw_mode == "read-write" and (not write_roots or not write_allowlist):
            raise ValueError("read-write mode requires write_roots and write_allowlist")

        run_id = str(self.run_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        object.__setattr__(self, "root_directory", str(root_directory))
        object.__setattr__(self, "working_directory", str(working_directory))
        object.__setattr__(self, "read_roots", tuple(str(path) for path in read_roots))
        object.__setattr__(self, "write_roots", tuple(str(path) for path in write_roots))
        object.__setattr__(self, "sandbox_profile_id", sandbox_profile_id)
        object.__setattr__(self, "mode", raw_mode)
        object.__setattr__(self, "read_allowlist", read_allowlist)
        object.__setattr__(self, "write_allowlist", write_allowlist)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "phase_id", str(self.phase_id).strip() if self.phase_id else None)
        object.__setattr__(self, "attempt_ref", str(self.attempt_ref).strip() if self.attempt_ref else None)

    @property
    def filesystem(self) -> dict[str, object]:
        return {
            "rootDirectory": self.root_directory,
            "readRoots": list(self.read_roots),
            "writeRoots": list(self.write_roots),
            "mode": self.mode,
            "readAllowlist": list(self.read_allowlist),
            "writeAllowlist": list(self.write_allowlist),
        }

    def resolve_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve(strict=False)
        return (Path(self.working_directory) / candidate).resolve(strict=False)

    def contains(self, path: str | Path) -> bool:
        return self._contains(Path(self.root_directory), self._to_path(path))

    def allows_read(self, path: str | Path) -> bool:
        return self._allows(path, roots=self.read_roots, patterns=self.read_allowlist)

    def allows_write(self, path: str | Path) -> bool:
        return self.mode == "read-write" and self._allows(path, roots=self.write_roots, patterns=self.write_allowlist)

    def allows_read_tree(self, path: str | Path) -> bool:
        """Whether *path* is a read-authorized directory subtree, not just a path.

        Search uses this before asking ripgrep to recurse so a partially matched
        directory cannot disclose disallowed descendants.
        """
        candidate = self._to_path(path)
        if not any(self._contains(Path(root), candidate) for root in self.read_roots):
            return False
        relative = self._relative_to_root(candidate)
        return any(self._pattern_authorizes_tree(pattern, relative) for pattern in self.read_allowlist)

    def _allows(self, path: str | Path, *, roots: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
        candidate = self._to_path(path)
        if not any(self._contains(Path(root), candidate) for root in roots):
            return False
        relative = self._relative_to_root(candidate)
        return any(self._pattern_matches(pattern, relative) for pattern in patterns)

    def _relative_to_root(self, path: Path) -> str:
        relative = path.relative_to(Path(self.root_directory)).as_posix()
        return "" if relative == "." else relative

    @staticmethod
    def _pattern_matches(pattern: str, relative: str) -> bool:
        if pattern == "**":
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            return relative == prefix or relative.startswith(prefix + "/")
        return PurePosixPath(relative).match(pattern)

    @staticmethod
    def _pattern_authorizes_tree(pattern: str, relative: str) -> bool:
        if pattern == "**":
            return True
        if not pattern.endswith("/**"):
            return False
        prefix = pattern[:-3]
        return relative == prefix or relative.startswith(prefix + "/")

    @staticmethod
    def _to_path(path: str | Path) -> Path:
        candidate = path if isinstance(path, Path) else Path(path)
        return candidate.expanduser().resolve(strict=False)

    @classmethod
    def _normalize_directory(cls, field_name: str, value: str) -> Path:
        candidate = Path(str(value or "").strip()).expanduser()
        if not candidate.is_absolute() or not candidate.is_dir():
            raise ValueError(f"{field_name} must be an absolute existing directory")
        return candidate.resolve()

    @classmethod
    def _normalize_roots(
        cls, field_name: str, values: tuple[str, ...], *, require_non_empty: bool, root: Path
    ) -> tuple[Path, ...]:
        if not isinstance(values, tuple):
            values = tuple(values or ())
        if require_non_empty and not values:
            raise ValueError(f"{field_name} must not be empty")
        normalized: list[Path] = []
        for entry in values:
            candidate = cls._normalize_directory(field_name, str(entry))
            if not cls._contains(root, candidate):
                raise ValueError(f"{field_name} entries must stay within root_directory")
            if candidate in normalized:
                raise ValueError(f"{field_name} entries must be unique after resolution")
            normalized.append(candidate)
        return tuple(normalized)

    @staticmethod
    def _normalize_patterns(field_name: str, values: tuple[str, ...], *, require_non_empty: bool) -> tuple[str, ...]:
        if not isinstance(values, tuple):
            values = tuple(values or ())
        if require_non_empty and not values:
            raise ValueError(f"{field_name} must not be empty")
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} entries must be non-empty strings")
            if any(char in _C0_OR_DEL for char in value):
                raise ValueError(f"{field_name} entries must not contain control characters")
            if "\\" in value or value.startswith("/") or PurePosixPath(value).is_absolute():
                raise ValueError(f"{field_name} entries must be canonical root-relative patterns")
            parts = value.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise ValueError(f"{field_name} entries must be canonical root-relative patterns")
            canonical = str(PurePosixPath(value))
            if canonical != value:
                raise ValueError(f"{field_name} entries must be canonical root-relative patterns")
            if value in normalized:
                raise ValueError(f"{field_name} entries must be unique")
            normalized.append(value)
        return tuple(normalized)

    @staticmethod
    def _contains(root: Path, candidate: Path) -> bool:
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True


_ACTIVE_TOOL_EXECUTION_SCOPE: ContextVar[ToolExecutionScope | None] = ContextVar(
    "active_tool_execution_scope", default=None
)


def get_tool_execution_scope() -> ToolExecutionScope | None:
    return _ACTIVE_TOOL_EXECUTION_SCOPE.get()


@contextmanager
def bind_tool_execution_scope(scope: ToolExecutionScope) -> Iterator[ToolExecutionScope]:
    token = _ACTIVE_TOOL_EXECUTION_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_TOOL_EXECUTION_SCOPE.reset(token)


# Linux Landlock ABI-v1 constants.  This launcher intentionally makes no
# attempt to emulate Landlock: unsupported kernel/setup/path-rule conditions
# exit before the user command runs.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_READ = _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
_LANDLOCK_SYSTEM_READ_EXECUTE = _LANDLOCK_READ | _LANDLOCK_ACCESS_FS_EXECUTE
_LANDLOCK_WRITE = (
    _LANDLOCK_ACCESS_FS_WRITE_FILE | _LANDLOCK_ACCESS_FS_REMOVE_DIR | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR | _LANDLOCK_ACCESS_FS_MAKE_DIR | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK | _LANDLOCK_ACCESS_FS_MAKE_FIFO | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
)


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32), ("_reserved", ctypes.c_uint32)]


def _landlock_fail(message: str) -> None:
    raise RuntimeError(f"Landlock setup failed: {message}")


def _landlock_add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: Path, rights: int) -> None:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        _landlock_fail(f"cannot open rule path {path}: {exc}")
    try:
        assert fd is not None
        rule = _LandlockPathBeneathAttr(rights, fd, 0)
        if libc.syscall(_LANDLOCK_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0) != 0:
            _landlock_fail(f"cannot add rule for {path}: {os.strerror(ctypes.get_errno())}")
    finally:
        if fd is not None:
            os.close(fd)


def _landlock_matching_paths(root: Path, pattern: str) -> tuple[list[Path], bool]:
    """Materialize allowlist matches and identify recursive subtree grants."""
    if pattern == "**":
        return [root], True
    if pattern.endswith("/**"):
        target = root / pattern[:-3]
        if not target.is_dir():
            _landlock_fail(f"subtree rule path does not exist: {target}")
        target = target.resolve()
        if not ToolExecutionScope._contains(root, target):
            _landlock_fail(f"subtree rule escapes Canon root: {target}")
        return [target], True
    matches: list[Path] = []
    for raw in glob.glob(str(root / pattern), recursive=True):
        candidate = Path(raw).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            _landlock_fail(f"glob match escapes Canon root: {candidate}")
        if candidate.exists():
            matches.append(candidate)
    return sorted(set(matches)), False


def _landlock_runtime_roots(root: Path) -> tuple[Path, ...]:
    """Resolve required interpreter roots and reject every Canon overlap first."""
    resolved: list[Path] = []
    for raw_root in ("/usr", "/bin", "/lib", "/lib64"):
        candidate = Path(raw_root)
        if not candidate.exists():
            _landlock_fail(f"runtime root does not exist: {raw_root}")
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            _landlock_fail(f"cannot resolve runtime root {raw_root}: {exc}")
        if ToolExecutionScope._contains(root, candidate) or ToolExecutionScope._contains(candidate, root):
            _landlock_fail(f"Canon root overlaps runtime root: {root} and {candidate}")
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _landlock_add_allowlist_rules(
    libc: ctypes.CDLL,
    ruleset_fd: int,
    root: Path,
    effect_roots: tuple[Path, ...],
    patterns: tuple[str, ...],
    *,
    write: bool,
) -> None:
    for pattern in patterns:
        paths, recursive = _landlock_matching_paths(root, pattern)
        for path in paths:
            authorized: set[Path] = set()
            for effect_root in effect_roots:
                if recursive and ToolExecutionScope._contains(path, effect_root):
                    authorized.add(effect_root)
                elif ToolExecutionScope._contains(effect_root, path):
                    authorized.add(path)
            for authorized_path in sorted(authorized):
                rights = _LANDLOCK_WRITE if write else _LANDLOCK_READ | _LANDLOCK_ACCESS_FS_EXECUTE
                if not recursive:
                    if authorized_path.is_dir():
                        _landlock_fail(
                            f"non-subtree rule matches directory and would widen authority: {authorized_path}"
                        )
                    if authorized_path.is_file():
                        rights = (
                            _LANDLOCK_ACCESS_FS_WRITE_FILE
                            if write
                            else _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_EXECUTE
                        )
                    else:
                        _landlock_fail(f"non-subtree rule does not match a regular file: {authorized_path}")
                _landlock_add_path_rule(libc, ruleset_fd, authorized_path, rights)


def _landlock_sanitized_path(
    root: Path,
    runtime_roots: tuple[Path, ...],
    read_roots: tuple[Path, ...],
    read_allowlist: tuple[str, ...],
) -> str:
    """Keep executable search directories that the ruleset can actually traverse."""
    allowed: list[str] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve(strict=True)
        except OSError:
            continue
        runtime_allowed = any(ToolExecutionScope._contains(runtime_root, candidate) for runtime_root in runtime_roots)
        root_allowed = False
        if ToolExecutionScope._contains(root, candidate):
            relative = candidate.relative_to(root).as_posix()
            root_allowed = (
                any(ToolExecutionScope._contains(read_root, candidate) for read_root in read_roots)
                and any(ToolExecutionScope._pattern_authorizes_tree(pattern, relative) for pattern in read_allowlist)
            )
        text = str(candidate)
        if (runtime_allowed or root_allowed) and text not in allowed:
            allowed.append(text)
    return os.pathsep.join(allowed)


def apply_landlock_and_exec(
    *,
    root_directory: str,
    read_roots: tuple[str, ...],
    write_roots: tuple[str, ...],
    read_allowlist: tuple[str, ...],
    write_allowlist: tuple[str, ...],
    command: str,
) -> None:
    """Install ABI-v1 Landlock rules, then exec opaque shell command.

    Only this child applies the kernel boundary.  The terminal transport remains
    responsible for creating/tracking foreground, background, and PTY processes.
    """
    if sys.platform != "linux":
        _landlock_fail("Linux Landlock is required")
    root = Path(root_directory)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        _landlock_fail(f"cannot resolve Canon root: {exc}")
    if not root.is_dir():
        _landlock_fail(f"Canon root is not a directory: {root}")
    normalized_read_roots = ToolExecutionScope._normalize_roots(
        "read_roots", read_roots, require_non_empty=False, root=root
    )
    normalized_write_roots = ToolExecutionScope._normalize_roots(
        "write_roots", write_roots, require_non_empty=False, root=root
    )
    runtime_roots = _landlock_runtime_roots(root)
    sanitized_path = _landlock_sanitized_path(
        root, runtime_roots, normalized_read_roots, read_allowlist
    )
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(_LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 1:
        _landlock_fail(f"Landlock ABI v1 is unavailable: {os.strerror(ctypes.get_errno())}")
    handled = _LANDLOCK_SYSTEM_READ_EXECUTE | _LANDLOCK_WRITE
    ruleset = _LandlockRulesetAttr(handled)
    ruleset_fd = libc.syscall(_LANDLOCK_CREATE_RULESET, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if ruleset_fd < 0:
        _landlock_fail(f"cannot create ruleset: {os.strerror(ctypes.get_errno())}")
    try:
        for runtime_root in runtime_roots:
            _landlock_add_path_rule(libc, ruleset_fd, runtime_root, _LANDLOCK_SYSTEM_READ_EXECUTE)
        # Do not grant the Canon root: each individual read/write allowlist
        # entry is materialized below, with no implicit directory or read grant.
        _landlock_add_allowlist_rules(
            libc, ruleset_fd, root, normalized_read_roots, read_allowlist, write=False
        )
        _landlock_add_allowlist_rules(
            libc, ruleset_fd, root, normalized_write_roots, write_allowlist, write=True
        )
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _landlock_fail(f"cannot set no_new_privs: {os.strerror(ctypes.get_errno())}")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            _landlock_fail(f"cannot restrict self: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset_fd)
    os.environ["PATH"] = sanitized_path
    os.execv("/bin/bash", ["bash", "--noprofile", "--norc", "-c", command])


def _landlock_launcher_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--landlock-launch", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--read-roots", required=True)
    parser.add_argument("--write-roots", required=True)
    parser.add_argument("--read", required=True)
    parser.add_argument("--write", required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args(argv)
    if not args.landlock_launch:
        _landlock_fail("launcher mode is required")
    try:
        read_roots = tuple(__import__("json").loads(args.read_roots))
        write_roots = tuple(__import__("json").loads(args.write_roots))
        read_patterns = tuple(__import__("json").loads(args.read))
        write_patterns = tuple(__import__("json").loads(args.write))
        # Validate untrusted CLI data with the same canonical policy as the parent.
        ToolExecutionScope._normalize_patterns("read_allowlist", read_patterns, require_non_empty=False)
        ToolExecutionScope._normalize_patterns("write_allowlist", write_patterns, require_non_empty=False)
        apply_landlock_and_exec(
            root_directory=args.root,
            read_roots=read_roots,
            write_roots=write_roots,
            read_allowlist=read_patterns,
            write_allowlist=write_patterns,
            command=args.command,
        )
    except Exception as exc:  # exec only happens on successful setup
        sys.stderr.write(f"Landlock setup failed: {exc}\n")
        return 126
    return 127


if __name__ == "__main__":
    raise SystemExit(_landlock_launcher_main(sys.argv[1:]))
