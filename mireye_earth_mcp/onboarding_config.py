"""Scoped, reversible agent configuration. Never rewrites project instructions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CLIENTS = ("claude-code", "codex", "cursor")
SERVER_NAME = "mireye-earth"
SKILL_NAME = "use-mireye"
BEGIN = "# BEGIN Mireye managed MCP\n"
END = "# END Mireye managed MCP\n"
PACKAGE_SPEC = "mireye-mcp==0.7.3"
_UNSET = object()
_LOCK_CONTEXT = threading.local()


class SetupError(ValueError):
    """Safe, locally authored error; never contains a credential or config body."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(path: Path) -> bytes | None:
    if path.is_symlink() or path.parent.is_symlink():
        raise SetupError(f"Refusing to replace a symlink: {path}")
    return path.read_bytes() if path.exists() else None


def private_write(
    path: Path, data: bytes, *, backup: bool = False, expected: bytes | None | object = _UNSET
) -> None:
    before = _snapshot(path)
    if expected is not _UNSET and before != expected:
        raise SetupError("Configuration changed during setup; file preserved.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup and before is not None:
        private_write(path.with_name(f"{path.name}.mireye-backup-{uuid4().hex[:12]}"), before)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if _snapshot(path) != before:
            raise SetupError("Configuration changed during setup; file preserved.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SetupError(f"Cannot read valid JSON at {path}; existing file preserved.") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object at {path}; existing file preserved.")
    return value


def state_path(home: Path) -> Path:
    return home / ".config" / "mireye-mcp" / "onboarding.json"


def read_state(home: Path) -> dict[str, Any]:
    state = read_json(state_path(home))
    for key in ("clients", "skills"):
        if not isinstance(state.setdefault(key, {}), dict):
            raise SetupError("Invalid Mireye onboarding manifest; file preserved.")
    return state


@contextmanager
def setup_lock(home: Path):
    """Serialize cooperating writers; keep the lock inode stable between invocations."""
    key = (os.getpid(), str(home.resolve()))
    active = getattr(_LOCK_CONTEXT, "active", set())
    if key in active:
        yield
        return
    path = state_path(home).with_suffix(".lock")
    if path.is_symlink():
        raise SetupError("Mireye's setup lock is a symlink; file preserved.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise SetupError(
                "Another Mireye setup is modifying these files; retry after it completes."
            ) from exc
        active.add(key)
        _LOCK_CONTEXT.active = active
        yield
    finally:
        if acquired:
            active.discard(key)
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _commit_files(
    writes: dict[Path, bytes | None],
    originals: dict[Path, bytes | None],
    *,
    backup_paths: set[Path],
) -> None:
    completed: list[Path] = []
    try:
        for target, data in writes.items():
            # Recheck the whole transaction: a changed manifest or shared skill can
            # invalidate an otherwise unchanged client's removal decision.
            for observed, before in originals.items():
                expected = writes[observed] if observed in completed else before
                if _snapshot(observed) != expected:
                    raise SetupError("Configuration changed during setup; files preserved.")
            if data is None:
                target.unlink()
            else:
                private_write(
                    target, data, backup=target in backup_paths, expected=originals[target]
                )
            completed.append(target)
    except Exception:
        for target in reversed(completed):
            # Roll back our own changes without replacing an intervening manual edit.
            if not target.is_symlink() and _snapshot(target) == writes[target]:
                if originals[target] is None:
                    target.unlink()
                else:
                    private_write(target, originals[target], expected=writes[target])
        raise


def config_path(home: Path, client: str) -> Path:
    return {
        "claude-code": home / ".claude.json",
        "codex": home / ".codex" / "config.toml",
        "cursor": home / ".cursor" / "mcp.json",
    }[client]


def skill_path(home: Path, client: str) -> Path:
    # Codex and Cursor share the cross-agent skill directory.
    root = ".claude" if client == "claude-code" else ".agents"
    return home / root / "skills" / SKILL_NAME / "SKILL.md"


def detected(home: Path, client: str, *, isolated: bool) -> bool:
    directory = home / {"claude-code": ".claude", "codex": ".codex", "cursor": ".cursor"}[client]
    if directory.is_dir() or config_path(home, client).exists():
        return True
    if isolated:
        return False
    command = {"claude-code": "claude", "codex": "codex", "cursor": "cursor"}[client]
    return shutil.which(command) is not None


def hosted_spec(base_url: str, client: str) -> dict[str, Any]:
    """Native remote configurations; each client owns its OAuth authorization.

    Claude Code requires an explicit HTTP type; Codex and Cursor select the
    remote transport from url. No key, local command or local server is installed.
    """
    if client not in CLIENTS:
        raise SetupError("Unsupported client.")
    from .server import _require_token_safe_base_url

    _require_token_safe_base_url(base_url)
    entry: dict[str, Any] = {"url": f"{base_url}/mcp"}
    if client == "claude-code":
        entry = {"type": "http", **entry}
    return entry


def read_entry(path: Path, client: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise SetupError(f"Config is a symlink; edit it manually: {path}")
    kind = "TOML" if client == "codex" else "JSON"
    try:
        exists = path.exists()
        raw = path.read_bytes().decode("utf-8") if exists else ""
        config = tomllib.loads(raw) if client == "codex" else json.loads(raw if exists else "{}")
    except (OSError, ValueError, UnicodeError) as exc:
        raise SetupError(f"Cannot read valid {kind} at {path}; existing file preserved.") from exc
    if not isinstance(config, dict):
        raise SetupError(f"Expected an object at {path}; existing file preserved.")
    servers = config.get("mcp_servers" if client == "codex" else "mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"MCP servers must be an object at {path}; existing file preserved.")
    return servers, raw


def _block(entry: dict[str, Any]) -> str:
    if set(entry) != {"url"}:
        raise SetupError("Codex's managed connection must use the hosted URL only.")
    return BEGIN + f"[mcp_servers.{SERVER_NAME}]\nurl = {json.dumps(entry['url'])}\n" + END


def _remove_block(raw: str) -> str:
    if raw.count(BEGIN) != 1 or raw.count(END) != 1:
        raise SetupError("Mireye's TOML ownership markers changed; config preserved.")
    start = raw.index(BEGIN)
    end = raw.index(END) + len(END)
    if end < start:
        raise SetupError("Mireye's TOML ownership markers changed; config preserved.")
    remaining = raw[:start] + raw[end:]
    before = tomllib.loads(raw)
    before.get("mcp_servers", {}).pop(SERVER_NAME, None)
    after = tomllib.loads(remaining)
    # TOML drops an empty parent table when its last child disappears.
    for document in (before, after):
        if document.get("mcp_servers") == {}:
            document.pop("mcp_servers")
    if before != after:
        raise SetupError("Mireye's TOML block contains other settings; config preserved.")
    return remaining


def inspect_client(
    home: Path, client: str, entry: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    path = config_path(home, client)
    servers, raw = read_entry(path, client)
    old = servers.get(SERVER_NAME)
    previous = state["clients"].get(client, {})
    if old is not None and old != entry:
        if not previous.get("managed") or old != previous.get("entry"):
            raise SetupError(f"{client} already has a custom {SERVER_NAME} entry; preserved.")
        if client == "codex":
            _remove_block(raw)
    if old is None and client == "codex" and (BEGIN in raw or END in raw):
        raise SetupError("Mireye's TOML ownership markers changed; config preserved.")
    plan = {
        "before": old,
        "entry": entry,
        "managed": old is None or bool(previous.get("managed")),
        "config": str(path),
        "skill": str(skill_path(home, client)),
        "change": old != entry,
    }
    # Some valid TOML (notably inline mcp_servers tables) cannot be extended
    # with a child table. Discover that before authentication.
    _client_bytes(client, plan, raw)
    return plan


def _client_bytes(client: str, plan: dict[str, Any], raw: str) -> bytes | None:
    parsed = tomllib.loads(raw) if client == "codex" else json.loads(raw or "{}")
    servers = parsed.get("mcp_servers" if client == "codex" else "mcpServers", {})
    if servers.get(SERVER_NAME) != plan["before"]:
        raise SetupError(f"{client} configuration changed during setup; reconnect to review it.")
    if not plan["change"]:
        return None
    if client == "codex":
        if SERVER_NAME in servers:
            raw = _remove_block(raw)
        if raw and not raw.endswith("\n"):
            raw += "\n"
        new = raw + _block(plan["entry"])
        # Catch accidental table nesting or syntax errors before replacing anything.
        try:
            proposed = tomllib.loads(new)
        except tomllib.TOMLDecodeError as exc:
            raise SetupError(
                "Cannot safely extend Codex MCP tables; config preserved. "
                "Use standard [mcp_servers.name] tables instead of inline tables."
            ) from exc
        if proposed.get("mcp_servers", {}).get(SERVER_NAME) != plan["entry"]:
            raise SetupError("Cannot safely add the Mireye TOML entry; config preserved.")
        return new.encode()
    parsed.setdefault("mcpServers", {})[SERVER_NAME] = plan["entry"]
    return (json.dumps(parsed, indent=2) + "\n").encode()


def install_client(home: Path, client: str, plan: dict[str, Any], state: dict[str, Any]) -> None:
    """Apply one client's files together, rolling back a failed local write."""
    with setup_lock(home):
        _install_client(home, client, plan, state)


def _install_client(home: Path, client: str, plan: dict[str, Any], state: dict[str, Any]) -> None:
    manifest = state_path(home)
    manifest_before = _snapshot(manifest)
    if read_state(home) != state:
        raise SetupError("Mireye's setup manifest changed during setup; reconnect to review it.")
    path = config_path(home, client)
    _, raw = read_entry(path, client)
    config_bytes = _client_bytes(client, plan, raw)
    skill = skill_path(home, client)
    skill_before = _snapshot(skill)
    skill_changes = inspect_skill(skill, state)
    updated = deepcopy(state)
    if skill_changes or str(skill) in updated["skills"]:
        updated["skills"][str(skill)] = {"sha256": digest(bundled_skill())}
    updated["clients"][client] = {
        key: value for key, value in plan.items() if key not in {"change", "before"}
    }
    writes: dict[Path, bytes | None] = {}
    if config_bytes is not None:
        writes[path] = config_bytes
    if skill_changes:
        writes[skill] = bundled_skill()
    writes[manifest] = (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
    originals = {target: _snapshot(target) for target in writes}
    if path in writes and (originals[path] or b"") != raw.encode():
        raise SetupError(f"{client} configuration changed during setup; reconnect to review it.")
    if skill in writes and originals[skill] != skill_before:
        raise SetupError("The Mireye skill changed during setup; file preserved.")
    if originals[manifest] != manifest_before:
        raise SetupError("Mireye's setup manifest changed during setup; file preserved.")
    _commit_files(writes, originals, backup_paths={path})
    state.clear()
    state.update(updated)


def bundled_skill() -> bytes:
    return (Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md").read_bytes()


def inspect_skill(path: Path, state: dict[str, Any]) -> bool:
    if path.is_symlink() or path.parent.is_symlink():
        raise SetupError(f"Skill is a symlink; preserved: {path}")
    if not path.exists():
        if path.parent.exists() and any(path.parent.iterdir()):
            raise SetupError(f"Custom skill directory already exists; preserved: {path.parent}")
        return True
    current = path.read_bytes()
    if current == bundled_skill():
        return False
    previous = state["skills"].get(str(path), {})
    if previous.get("sha256") != digest(current):
        raise SetupError(f"Custom or edited {SKILL_NAME} skill preserved: {path}")
    return True


def remove_client(home: Path, client: str, state: dict[str, Any], *, dry_run: bool) -> str:
    if dry_run:
        return _remove_client(home, client, state, dry_run=True)
    with setup_lock(home):
        return _remove_client(home, client, state, dry_run=False)


def _remove_client(home: Path, client: str, state: dict[str, Any], *, dry_run: bool) -> str:
    manifest = state_path(home)
    manifest_before = _snapshot(manifest)
    if read_state(home) != state:
        raise SetupError("Mireye's setup manifest changed during removal; retry to review it.")
    record = state["clients"].get(client)
    if not record:
        return "not_managed"
    path = config_path(home, client)
    config_before = _snapshot(path)
    servers, raw = read_entry(path, client)
    if (config_before or b"") != raw.encode():
        raise SetupError("Configuration changed during removal; retry to review it.")
    current = servers.get(SERVER_NAME)
    if record.get("managed") and current is not None and current != record.get("entry"):
        raise SetupError(f"Edited {client} MCP entry preserved; remove it manually.")
    originals = {manifest: manifest_before, path: config_before}
    writes: dict[Path, bytes | None] = {}
    if record.get("managed") and current is not None:
        if client == "codex":
            writes[path] = _remove_block(raw).encode()
        else:
            contents = json.loads(raw)
            del contents["mcpServers"][SERVER_NAME]
            writes[path] = (json.dumps(contents, indent=2) + "\n").encode()
    skill = skill_path(home, client)
    other_users = [
        key for key in state["clients"] if key != client and skill_path(home, key) == skill
    ]
    skill_record = state["skills"].get(str(skill))
    if not other_users and skill_record:
        if skill.parent.is_symlink():
            raise SetupError(f"Skill directory is a symlink; preserved: {skill.parent}")
        originals[skill] = _snapshot(skill)
        if originals[skill] is not None and digest(originals[skill]) != skill_record.get("sha256"):
            raise SetupError(f"Edited skill preserved; remove it manually: {skill}")
        if originals[skill] is not None:
            writes[skill] = None
    updated = deepcopy(state)
    del updated["clients"][client]
    if not other_users and skill_record:
        updated["skills"].pop(str(skill), None)
    writes[manifest] = (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
    if dry_run:
        for target, before in originals.items():
            if _snapshot(target) != before:
                raise SetupError("Configuration changed during removal; retry to review it.")
        return "would_remove"
    _commit_files(writes, originals, backup_paths={path})
    state.clear()
    state.update(updated)
    if skill in writes and writes[skill] is None:
        try:
            skill.parent.rmdir()
        except OSError:
            pass  # Keep a directory if the user added other content meanwhile.
    return "removed"
