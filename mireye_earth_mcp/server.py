"""Compatibility CLI entry point; this package does not run an MCP server.

Only installation, private credential storage and bounded REST diagnostics live
here. MCP clients connect directly to Mireye's hosted OAuth service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import onboarding_config as config

DEFAULT_BASE_URL = "https://api.mireye.com"
MIREYE_BASE_URL = os.getenv("MIREYE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
TOKEN_ENV = "MIREYE_BEARER_TOKEN"
CREDENTIALS_FILE_ENV = "MIREYE_MCP_CREDENTIALS_FILE"


def _credentials_path() -> Path:
    configured = os.getenv(CREDENTIALS_FILE_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config" / "mireye-mcp" / "credentials.json"
    )


def _normalize_token(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _load_stored_credentials() -> dict[str, Any] | None:
    path = _credentials_path()
    if path.is_symlink() or path.parent.is_symlink():
        raise config.SetupError("Credential path is a symlink; existing file preserved.")
    return config.read_json(path) if path.exists() else None


def _store_credentials(data: dict[str, Any]) -> None:
    path = _credentials_path()
    config.private_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())


def _stored_base_url(credentials: dict[str, Any]) -> str:
    raw = credentials.get("base_url")
    return raw.strip().rstrip("/") if isinstance(raw, str) and raw.strip() else DEFAULT_BASE_URL


def _require_token_safe_base_url(base_url: str) -> None:
    try:
        parts = urlsplit(base_url)
        allowed = base_url == DEFAULT_BASE_URL or (
            parts.scheme in {"http", "https"}
            and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (
            not allowed
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError
        _ = parts.port
    except ValueError as exc:
        raise config.SetupError(
            "The helper sends keys only to https://api.mireye.com or a loopback API origin."
        ) from exc


def _configured_token() -> str | None:
    token = _normalize_token(os.getenv(TOKEN_ENV))
    if token:
        return token
    stored = _load_stored_credentials() or {}
    if stored and _stored_base_url(stored) != MIREYE_BASE_URL:
        raise config.SetupError("Stored credentials belong to another API origin; reconnect.")
    return _normalize_token(stored.get("token"))


def _onboarding_attempt_id(token: str) -> str | None:
    stored = _load_stored_credentials() or {}
    if stored.get("token") != token or _stored_base_url(stored) != MIREYE_BASE_URL:
        return None
    attempt = stored.get("onboarding_attempt_id")
    from .onboarding import ATTEMPT_PATTERN

    return attempt if isinstance(attempt, str) and ATTEMPT_PATTERN.fullmatch(attempt) else None


def _sync_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    target = base_url or MIREYE_BASE_URL
    _require_token_safe_base_url(target)
    token = token or _configured_token()
    if not token:
        raise config.SetupError("No saved API key; copy setup from Mireye's onboarding page.")
    headers = {"Authorization": f"Bearer {token}"}
    if path == "/v1/fetch":
        attempt = _onboarding_attempt_id(token)
        if attempt:
            headers["X-Mireye-Onboarding-Attempt"] = attempt
    # Redirects must never move a credential to another host. Diagnostics have
    # a bounded timeout and do not log raw responses, URLs, headers or keys.
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        response = client.request(method, f"{target}{path}", json=payload, headers=headers)
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise config.SetupError("Mireye returned an invalid diagnostic response.") from exc
        if not isinstance(body, dict):
            raise config.SetupError("Mireye returned an invalid diagnostic response.")
        return body


def _logout(args: argparse.Namespace) -> int:
    from .onboarding import ATTEMPT_PATTERN, _context, _error

    try:
        home, base_url = _context(args)
        # Connect, removal and logout must resolve the same home and share the
        # same writer lock, including in a fresh helper process.
        with config.setup_lock(home):
            path = _credentials_path()
            before = config._snapshot(path)
            stored = _load_stored_credentials() or {}
            if config._snapshot(path) != before:
                raise config.SetupError("Credentials changed during logout; file preserved.")
            if args.base_url and stored and _stored_base_url(stored) != base_url:
                raise config.SetupError("Stored credentials belong to another API origin.")
            if args.revoke and stored.get("token"):
                token_id = stored.get("verified_token_id")
                if not isinstance(token_id, str) or not ATTEMPT_PATTERN.fullmatch(token_id):
                    raise config.SetupError(
                        "Revoke this key in the dashboard; its ID is unverified."
                    )
                _sync_request(
                    "DELETE",
                    f"/v1/users/me/tokens/{token_id}",
                    token=stored["token"],
                    base_url=_stored_base_url(stored),
                )
            # A manual edit does not participate in our lock. In particular,
            # never delete a replacement credential after a slow revoke call.
            if config._snapshot(path) != before:
                raise config.SetupError("Credentials changed during logout; file preserved.")
            if before is not None:
                path.unlink()
        print("Saved diagnostic key removed. Hosted MCP OAuth is managed by each client.")
        return 0
    except Exception as exc:
        print(_error(exc))
        return 1


def main(argv: list[str] | None = None) -> None:
    from .onboarding import register_commands

    parser = argparse.ArgumentParser(description="Configure and diagnose Mireye's hosted MCP.")
    subparsers = parser.add_subparsers(dest="command")
    register_commands(subparsers)
    logout = subparsers.add_parser("logout", help="Remove the helper's saved diagnostic API key.")
    logout.add_argument("--config-home", help="Isolate setup and credentials under this directory.")
    logout.add_argument("--base-url", help="Require the saved key to match this API origin.")
    logout.add_argument("--revoke", action="store_true", help="Revoke its verified API-key ID.")
    logout.set_defaults(func=_logout)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        print(
            "The local stdio MCP server is retired. Connect your client to "
            "https://api.mireye.com/mcp. This package supports connect and doctor only.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
