"""Install hosted connections and prove one free REST Fetch with the saved key."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import onboarding_config as config

ATTEMPT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PROOF_ARGUMENTS = {"lat": 40.7128, "lng": -74.0060, "fields": ["elevation"]}


def _context(args: argparse.Namespace) -> tuple[Path, str]:
    from . import server

    configured_home = args.config_home or os.getenv("MIREYE_MCP_SETUP_HOME")
    home = Path(configured_home).expanduser().absolute() if configured_home else Path.home()
    if not configured_home:
        for variable, expected in (
            ("CODEX_HOME", home / ".codex"),
            ("CLAUDE_CONFIG_DIR", home / ".claude"),
        ):
            override = os.getenv(variable)
            if override and Path(override).expanduser().absolute() != expected:
                raise config.SetupError(
                    f"Custom {variable} is not supported by automatic setup; "
                    "use manual MCP/skill configuration or --config-home for an isolated test."
                )
    if configured_home:
        # Process-local only: no shell profile or global HOME override.
        os.environ[server.CREDENTIALS_FILE_ENV] = str(
            home / ".config" / "mireye-mcp" / "credentials.json"
        )
    base_url = (args.base_url or server.MIREYE_BASE_URL).rstrip("/")
    parts = urlsplit(base_url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise config.SetupError("--base-url must be an API origin without credentials or a query.")
    if parts.path not in ("", "/"):
        raise config.SetupError("--base-url must be an API origin without a path.")
    server._require_token_safe_base_url(base_url)
    server.MIREYE_BASE_URL = base_url
    return home, base_url


def _error(exc: Exception) -> str:
    # HTTP exceptions can contain response bodies/URLs. Never render them here.
    if isinstance(exc, config.SetupError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Mireye request failed (HTTP {exc.response.status_code}); retry or sign in again."
    if isinstance(exc, (httpx.HTTPError, TimeoutError)):
        return "Could not reach Mireye before the timeout; check the connection and retry."
    if isinstance(exc, OSError):
        return "Could not access the configuration files."
    return "The operation failed; existing custom configuration was preserved."


def _emit(report: dict[str, Any], args: argparse.Namespace) -> int:
    secrets = [getattr(args, "api_key", None)]
    # Guard the final output too, including unexpected secret-shaped source text.
    from . import server

    secrets.append(server._normalize_token(getattr(args, "api_key", None)))
    secrets.append(os.getenv(server.TOKEN_ENV))
    try:
        credentials = server._load_stored_credentials() or {}
        secrets.append(credentials.get("token"))
    except (config.SetupError, OSError):
        pass

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                if isinstance(secret, str) and secret:
                    value = value.replace(secret, "<redacted>")
            return value
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    safe = redact(report)
    text = json.dumps(safe, indent=2)
    if args.json:
        print(text)
    else:
        print(f"Mireye {safe['status']}.")
        for item in safe.get("clients", []):
            print(
                f"  {item['client']}: {item['status']}"
                + (f" — {item['message']}" if item.get("message") else "")
            )
        for item in safe.get("checks", []):
            print(
                f"  {item['check']}: {item['status']}"
                + (f" — {item['message']}" if item.get("message") else "")
            )
        if safe.get("proof"):
            proof = safe["proof"]
            print(f"  elevation: {proof['value']} {proof.get('unit') or ''}".rstrip())
            print(f"  Source: {proof['source']} — {proof['source_url']}")
        if safe.get("message"):
            print(safe["message"])
    return 1 if safe["status"] == "failed" else 0


def _selected(args: argparse.Namespace, home: Path, state: dict[str, Any]) -> list[str]:
    if args.client and getattr(args, "all", False):
        raise config.SetupError("Choose --client or --all, not both.")
    if args.client:
        return list(dict.fromkeys(args.client))
    if args.command == "doctor" or getattr(args, "remove", False):
        return list(state["clients"]) or list(config.CLIENTS)
    return list(config.CLIENTS)


def _token_metadata(token: str) -> dict[str, str]:
    """Read display/revocation metadata only after the API has authenticated the key."""
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (IndexError, ValueError, UnicodeError, binascii.Error):
        return {}
    if not isinstance(claims, dict):
        return {}
    metadata = {}
    token_id = claims.get("jti")
    if isinstance(token_id, str) and ATTEMPT_PATTERN.fullmatch(token_id):
        metadata["token_id"] = token_id
    expiry = claims.get("exp")
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
        try:
            metadata["expires_at"] = datetime.fromtimestamp(
                expiry,
                timezone.utc,  # noqa: UP017 — adapter supports Python 3.10.
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return metadata


@contextmanager
def _active_token(token: str):
    """Select this operation's credential without editing the parent shell."""
    from . import server

    previous = os.environ.get(server.TOKEN_ENV)
    os.environ[server.TOKEN_ENV] = token
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(server.TOKEN_ENV, None)
        else:
            os.environ[server.TOKEN_ENV] = previous


def _authenticate(args: argparse.Namespace, base_url: str, *, reconnect: bool = False) -> str:
    from . import server

    supplied_key = args.api_key is not None
    if supplied_key:
        # A copied production API key must not be redirected to another host.
        host = urlsplit(base_url).hostname
        if base_url != server.DEFAULT_BASE_URL and host not in {"localhost", "127.0.0.1", "::1"}:
            raise config.SetupError(
                "API keys supplied to connect can be used only at api.mireye.com or loopback."
            )
        for value, label in ((args.attempt_id, "attempt"), (args.token_id, "token")):
            if value is not None and not ATTEMPT_PATTERN.fullmatch(value):
                raise config.SetupError(f"Invalid {label} ID; copy the setup command again.")
        token = server._normalize_token(args.api_key)
    else:
        if args.attempt_id is not None or args.token_id is not None:
            raise config.SetupError("--attempt-id and --token-id require --api-key.")
        stored = server._load_stored_credentials() or {}
        saved_token = server._normalize_token(stored.get("token"))
        if reconnect and saved_token and server._stored_base_url(stored) == base_url:
            # Reconnect the installed agents to their existing account. An inherited
            # shell token is only adopted on first setup or for another API origin.
            token = saved_token
        else:
            token = server._configured_token()
    if not token:
        raise config.SetupError(
            "Copy setup from Mireye onboarding, or connect with --api-key-stdin."
        )
    account = server._sync_request("GET", "/v1/users/me", token=token, base_url=base_url)
    if not isinstance(account, dict) or not isinstance(account.get("user"), dict):
        raise config.SetupError("The auth check returned an invalid account response.")
    uid = account["user"].get("uid")
    if not isinstance(uid, str) or not uid:
        raise config.SetupError("The auth check returned an invalid account identity.")
    stored = server._load_stored_credentials() or {}
    if reconnect and stored:
        previous_uid = stored.get("uid")
        identity_lost = False
        if previous_uid is None and stored.get("token"):
            # 0.7.1 credentials have no saved uid; recover it from the old key.
            try:
                previous = server._sync_request(
                    "GET",
                    "/v1/users/me",
                    token=stored["token"],
                    base_url=server._stored_base_url(stored),
                )
            except httpx.HTTPStatusError as exc:
                # An expired or revoked legacy key can no longer act for any
                # account, so the newly verified key may replace it. Network
                # and server errors still abort without changing anything.
                if exc.response.status_code not in (401, 403):
                    raise
                identity_lost = True
            else:
                user = previous.get("user")
                previous_uid = user.get("uid") if isinstance(user, dict) else None
        if not identity_lost and previous_uid != uid:
            raise config.SetupError(
                "Existing setup belongs to another account. Remove its managed connection "
                "and sign out before connecting a different account."
            )
    metadata = _token_metadata(token)
    attempt = args.attempt_id if supplied_key else stored.get("onboarding_attempt_id")
    if attempt:
        if not isinstance(attempt, str) or not ATTEMPT_PATTERN.fullmatch(attempt):
            raise config.SetupError("Stored setup attempt ID is invalid; reconnect.")
        identity = server._sync_request(
            "GET", f"/v1/onboarding/{attempt}/credential", token=token, base_url=base_url
        )
        verified_id = identity.get("token_id")
        if (
            identity.get("uid") != uid
            or identity.get("attempt_id") != attempt
            or not isinstance(verified_id, str)
            or not ATTEMPT_PATTERN.fullmatch(verified_id)
            or (args.token_id and args.token_id != verified_id)
        ):
            raise config.SetupError("The setup identity does not match the supplied API key.")
        metadata.update({"token_id": verified_id, "verified_token_id": verified_id})
    elif supplied_key and args.token_id:
        # A locally decoded claim is not server confirmation of the key's ID, so
        # it is display metadata only; logout --revoke needs a verified ID.
        if metadata.get("token_id") not in (None, args.token_id):
            raise config.SetupError("The token ID does not match the supplied API key.")
        metadata["token_id"] = args.token_id
    # Save only after account and (for guided setup) exact attempt/key identity
    # validation. Invalid replacements leave the prior login/config untouched.
    if stored.get("token") == token and server._stored_base_url(stored) == base_url:
        credentials = dict(stored)
    else:
        credentials = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    credentials.update({"token": token, "base_url": base_url, "uid": uid, **metadata})
    if attempt:
        credentials["onboarding_attempt_id"] = attempt
    else:
        # Only the guided-attempt credential check confirms a key ID server-side.
        # Drop any ID an earlier helper saved from a locally decoded claim.
        credentials.pop("verified_token_id", None)
    if credentials != stored:
        server._store_credentials(credentials)
    return token


def _progress(stage: str, clients: list[str], base_url: str) -> None:
    from . import server

    stored = server._load_stored_credentials() or {}
    attempt = stored.get("onboarding_attempt_id")
    token = server._configured_token()
    if not attempt:
        return
    if not isinstance(attempt, str) or not ATTEMPT_PATTERN.fullmatch(attempt):
        raise config.SetupError("Stored onboarding attempt ID is invalid; reconnect.")
    if stored.get("token") != token or server._stored_base_url(stored) != base_url:
        raise config.SetupError("The active credential does not match this onboarding attempt.")
    server._sync_request(
        "POST",
        f"/v1/onboarding/{attempt}/progress",
        token=token,
        base_url=base_url,
        payload={"stage": stage, "clients": clients, "skill_installed": True},
    )


def connect(args: argparse.Namespace) -> int:
    from . import server

    report: dict[str, Any] = {"status": "failed", "clients": []}
    try:
        home, base_url = _context(args)
        state = config.read_state(home)
        selected = _selected(args, home, state)
        isolated = bool(args.config_home or os.getenv("MIREYE_MCP_SETUP_HOME"))
        if args.remove:
            if args.api_key_stdin or any(
                value is not None for value in (args.api_key, args.attempt_id, args.token_id)
            ):
                raise config.SetupError("--remove cannot be combined with API-key options.")
            for client in selected:
                try:
                    if client not in config.CLIENTS:
                        raise config.SetupError("Unsupported client.")
                    outcome = config.remove_client(home, client, state, dry_run=args.dry_run)
                    report["clients"].append({"client": client, "status": outcome})
                except (config.SetupError, OSError) as exc:
                    report["clients"].append(
                        {"client": client, "status": "failed", "message": _error(exc)}
                    )
            if not any(row["status"] == "failed" for row in report["clients"]):
                report["status"] = "dry_run" if args.dry_run else "removed"
            report["message"] = "Stored login credentials are retained; logout removes them."
            return _emit(report, args)
        plans = {}
        for client in selected:
            if client not in config.CLIENTS:
                report["clients"].append({"client": client, "status": "unsupported"})
                continue
            if not config.detected(home, client, isolated=isolated):
                report["clients"].append({"client": client, "status": "not_detected"})
                continue
            try:
                entry = config.hosted_spec(base_url, client)
                plans[client] = config.inspect_client(home, client, entry, state)
                path = config.skill_path(home, client)
                config.inspect_skill(path, state)
                report["clients"].append(
                    {
                        "client": client,
                        "status": "planned",
                        "config": str(config.config_path(home, client)),
                        "skill": str(path),
                    }
                )
            except (config.SetupError, OSError) as exc:
                plans.pop(client, None)
                report["clients"].append(
                    {"client": client, "status": "failed", "message": _error(exc)}
                )
        failures = any(
            row["status"] in {"failed", "unsupported"}
            or (bool(args.client) and row["status"] == "not_detected")
            for row in report["clients"]
        )
        if not plans or failures:
            report["message"] = "No changes applied. Resolve the listed issues, then reconnect."
            return _emit(report, args)
        if args.dry_run:
            report["status"] = "dry_run"
            report["message"] = (
                "Preview only: no API request made, login saved, or configuration changed."
            )
            return _emit(report, args)
        with config.setup_lock(home):
            # Revalidate after acquiring the shared writer lock, before authentication.
            if config.read_state(home) != state:
                raise config.SetupError("Mireye's setup manifest changed; reconnect to review it.")
            for client, plan in plans.items():
                current = config.inspect_client(
                    home, client, config.hosted_spec(base_url, client), state
                )
                if current != plan:
                    raise config.SetupError("Configuration changed; reconnect to review it.")
                config.inspect_skill(config.skill_path(home, client), state)
            if args.api_key_stdin:
                # Read only after dry-run/config preflight. Never place the key in argv.
                supplied = sys.stdin.read(8193)
                if len(supplied) > 8192 or not supplied.strip():
                    raise config.SetupError("Provide one nonempty API key on standard input.")
                args.api_key = supplied.strip()
            token = _authenticate(args, base_url, reconnect=bool(state["clients"]))
            with _active_token(token):
                for client, plan in plans.items():
                    config.install_client(home, client, plan, state)
                    next(row for row in report["clients"] if row["client"] == client)["status"] = (
                        "installed"
                    )
                _progress("installed", list(plans), base_url)
        report["status"] = "installed"
        report["message"] = (
            "Restart the connected agents and approve their hosted MCP OAuth sign-in. "
            "Run mireye-mcp doctor --proof to verify a free REST Fetch with the saved key. "
            "That proof does not authorize or verify client MCP OAuth."
        )
    except Exception as exc:  # CLI boundary: never print raw remote bodies or credentials.
        report["message"] = _error(exc)
        try:
            stored = server._load_stored_credentials() or {}
        except (config.SetupError, OSError):
            stored = {}
        if args.api_key and stored.get("token") == server._normalize_token(args.api_key):
            report["message"] += " The API key is saved locally. Retry the same connect command."
    return _emit(report, args)


def _fetch_proof(token: str, base_url: str) -> dict[str, Any]:
    from . import server

    return _proof_summary(
        server._sync_request(
            "POST", "/v1/fetch", token=token, base_url=base_url, payload=PROOF_ARGUMENTS
        )
    )


def _proof_summary(body: Any) -> dict[str, Any]:
    field = body.get("fields", {}).get("elevation") if isinstance(body, dict) else None
    if not isinstance(field, dict):
        raise config.SetupError(
            "REST Fetch returned no elevation field; onboarding is not complete."
        )
    value = field.get("value")
    source_url = field.get("source_url")
    if (
        field.get("status") != "ok"
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not field.get("source")
        or not field.get("fetched_at")
        or not isinstance(source_url, str)
        or urlsplit(source_url).scheme != "https"
    ):
        raise config.SetupError(
            "REST Fetch returned missing, failed, or uncited data; onboarding is not complete."
        )
    return {
        "endpoint": "/v1/fetch",
        "transport": "rest",
        "price_usd": "0.00",
        "arguments": PROOF_ARGUMENTS,
        **{
            key: field.get(key)
            for key in ("value", "unit", "source", "source_url", "fetched_at", "status")
        },
    }


def doctor(args: argparse.Namespace) -> int:
    from . import server

    report: dict[str, Any] = {"status": "failed", "clients": [], "checks": []}
    try:
        home, base_url = _context(args)
        state = config.read_state(home)
        selected = _selected(args, home, state)
        entries = []
        for client in selected:
            try:
                if client not in config.CLIENTS:
                    raise config.SetupError("Unsupported client.")
                record = state["clients"].get(client)
                if not record:
                    raise config.SetupError("Not connected by Mireye; run connect first.")
                servers, _ = config.read_entry(config.config_path(home, client), client)
                if servers.get(config.SERVER_NAME) != record.get("entry"):
                    raise config.SetupError("MCP configuration changed or is missing; reconnect.")
                entry = record["entry"]
                if entry != config.hosted_spec(base_url, client):
                    raise config.SetupError(
                        "MCP configuration is not the expected hosted URL. Run connect for "
                        "this client to upgrade it, or check upgraded clients with --client."
                    )
                if config.skill_path(home, client).read_bytes() != config.bundled_skill():
                    raise config.SetupError(
                        "Bundled skill is missing or changed; reconnect or review your edits."
                    )
                report["clients"].append({"client": client, "status": "pass"})
                entries.append(entry)
            except (config.SetupError, OSError) as exc:
                report["clients"].append(
                    {"client": client, "status": "failed", "message": _error(exc)}
                )
        stored = server._load_stored_credentials() or {}
        if server._stored_base_url(stored) != base_url:
            raise config.SetupError("The saved key belongs to another API origin; reconnect.")
        token = server._normalize_token(stored.get("token"))
        if not token:
            raise config.SetupError("No saved API key; copy setup from Mireye's onboarding page.")
        account = server._sync_request("GET", "/v1/users/me", token=token, base_url=base_url)
        uid = account.get("user", {}).get("uid")
        if not isinstance(uid, str) or not uid or stored.get("uid") != uid:
            raise config.SetupError("The saved key account does not match this setup; reconnect.")
        report["checks"].append({"check": "authentication", "status": "pass"})
        if not entries or any(row["status"] == "failed" for row in report["clients"]):
            return _emit(report, args)
        with _active_token(token):
            # Recover a lost installed notification after connect finished writing locally.
            _progress("installed", selected, base_url)
            if args.proof:
                report["proof"] = _fetch_proof(token, base_url)
                report["checks"].append({"check": "free_rest_fetch_proof", "status": "pass"})
                report["message"] = (
                    "A free REST Fetch returned cited elevation data. Hosted MCP OAuth "
                    "must still be authorized and verified in each connected client."
                )
            else:
                report["message"] = (
                    "Configuration and API key verified. --proof makes one free REST Fetch. "
                    "Hosted MCP OAuth remains a separate client sign-in."
                )
            # A requested proof is part of verification, including successful result validation.
            _progress("verified", selected, base_url)
        report["hosted_mcp_authorization"] = "client_managed_not_verified"
        report["status"] = "complete" if args.proof else "verified"
    except Exception as exc:
        report["status"] = "failed"
        report["message"] = _error(exc)
    return _emit(report, args)


def register_commands(subparsers: Any) -> None:
    for name, function in (("connect", connect), ("doctor", doctor)):
        parser = subparsers.add_parser(
            name,
            help="Connect agents to Mireye."
            if name == "connect"
            else "Verify Mireye auth, configuration, skills and an optional real call.",
        )
        parser.add_argument(
            "--client", action="append", help="claude-code, codex, or cursor; repeatable."
        )
        parser.add_argument("--base-url", help="API origin (default: configured MIREYE_BASE_URL).")
        parser.add_argument(
            "--config-home", help="Isolate agent configs and login under this directory."
        )
        parser.add_argument(
            "--json", action="store_true", help="Machine-readable, credential-redacted result."
        )
        parser.set_defaults(func=function)
        if name == "connect":
            parser.add_argument(
                "--all", action="store_true", help="Connect all detected supported agents."
            )
            keys = parser.add_mutually_exclusive_group()
            keys.add_argument("--api-key", help="Legacy key input; prefer --api-key-stdin.")
            keys.add_argument(
                "--api-key-stdin",
                action="store_true",
                help="Read a key privately from standard input.",
            )
            parser.add_argument("--attempt-id", help="Setup attempt ID from Mireye's setup page.")
            parser.add_argument(
                "--token-id", help="API key ID (derived from the validated key when omitted)."
            )
            parser.add_argument(
                "--dry-run",
                action="store_true",
                help="Preview; never contact the API, save a key, or write files.",
            )
            parser.add_argument(
                "--remove",
                action="store_true",
                help="Remove unchanged entries installed by Mireye.",
            )
        else:
            parser.add_argument(
                "--proof",
                action="store_true",
                help="Make one free REST Fetch; this does not authorize client MCP OAuth.",
            )
