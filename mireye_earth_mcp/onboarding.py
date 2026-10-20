"""Install agent connections, verify them, and prove a real MCP Fetch call."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
        return "Could not access the configuration files or launch the MCP process."
    return "The operation failed; existing custom configuration was preserved."


def _emit(report: dict[str, Any], args: argparse.Namespace) -> int:
    secrets = [getattr(args, "api_key", None)]
    # Guard the final output too, including unexpected secret-shaped source text.
    from . import server

    secrets.append(server._normalize_token(getattr(args, "api_key", None)))
    secrets.append(os.getenv(server.TOKEN_ENV))
    credentials = server._load_stored_credentials() or {}
    secrets.append(credentials.get("token"))

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
        raise config.SetupError("Sign in with mireye-mcp login, or connect with --api-key.")
    account = server._sync_request("GET", "/v1/users/me", token=token, base_url=base_url)
    if not isinstance(account, dict) or not isinstance(account.get("user"), dict):
        raise config.SetupError("The auth check returned an invalid account response.")
    # Save only after validating the exact selected key. An invalid replacement must
    # leave the existing login intact, and GUI agents need a persistent credential.
    if supplied_key or os.getenv(server.TOKEN_ENV):
        stored = server._load_stored_credentials() or {}
        metadata = _token_metadata(token)
        if supplied_key and args.token_id:
            if metadata.get("token_id") not in (None, args.token_id):
                raise config.SetupError("The token ID does not match the supplied API key.")
            metadata["token_id"] = args.token_id
        if supplied_key and not metadata.get("token_id"):
            raise config.SetupError("The API key has no token ID; include --token-id from setup.")
        if stored.get("token") == token and server._stored_base_url(stored) == base_url:
            credentials = dict(stored)
        else:
            credentials = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        credentials.update({"token": token, "base_url": base_url, **metadata})
        if supplied_key and args.attempt_id:
            credentials["onboarding_attempt_id"] = args.attempt_id
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
            if any(value is not None for value in (args.api_key, args.attempt_id, args.token_id)):
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
        entry = config.launch_spec(base_url, server._credentials_path(), args.package_source)
        plans = {}
        for client in selected:
            if client not in config.CLIENTS:
                report["clients"].append({"client": client, "status": "unsupported"})
                continue
            if not config.detected(home, client, isolated=isolated):
                report["clients"].append({"client": client, "status": "not_detected"})
                continue
            try:
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
                current = config.inspect_client(home, client, entry, state)
                if current != plan:
                    raise config.SetupError("Configuration changed; reconnect to review it.")
                config.inspect_skill(config.skill_path(home, client), state)
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
            "Restart the connected agents to load Mireye and use-mireye. "
            "Run mireye-mcp doctor --proof to verify a cited elevation result "
            "through the MCP adapter."
        )
    except Exception as exc:  # CLI boundary: never print raw remote bodies or credentials.
        report["message"] = _error(exc)
        stored = server._load_stored_credentials() or {}
        if args.api_key and stored.get("token") == server._normalize_token(args.api_key):
            report["message"] += " The API key is saved locally. Retry the same connect command."
    return _emit(report, args)


async def _mcp_proof(entry: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The actual installed launch command, not an in-process shortcut or raw API call.
    params = StdioServerParameters(command=entry["command"], args=entry["args"], env=entry["env"])
    with tempfile.TemporaryFile(mode="w+") as error_log:
        async with stdio_client(params, errlog=error_log) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=150)
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                if "mireye_fetch" not in {tool.name for tool in tools.tools}:
                    raise config.SetupError(
                        "The installed MCP server does not advertise mireye_fetch."
                    )
                result = await session.call_tool("mireye_fetch", PROOF_ARGUMENTS)
                if result.isError:
                    raise config.SetupError(
                        "The MCP Fetch call failed; onboarding is not complete."
                    )
                body = result.structuredContent
                if body is None:
                    for item in result.content:
                        if item.type == "text":
                            try:
                                body = json.loads(item.text)
                                break
                            except ValueError:
                                continue
                return _proof_summary(body)


def _proof_summary(body: Any) -> dict[str, Any]:
    field = body.get("fields", {}).get("elevation") if isinstance(body, dict) else None
    if not isinstance(field, dict):
        raise config.SetupError(
            "MCP Fetch returned no elevation field; onboarding is not complete."
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
            "MCP Fetch returned missing, failed, or uncited data; onboarding is not complete."
        )
    return {
        "tool": "mireye_fetch",
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
                if entry.get("env", {}).get("MIREYE_BASE_URL") != base_url:
                    raise config.SetupError(
                        "Configured API URL differs from doctor; use the connected --base-url."
                    )
                if entry.get("env", {}).get("MIREYE_MCP_CREDENTIALS_FILE") != str(
                    server._credentials_path()
                ):
                    raise config.SetupError(
                        "Configured credential path differs from doctor; reconnect."
                    )
                if config.skill_path(home, client).read_bytes() != config.bundled_skill():
                    raise config.SetupError(
                        "Bundled skill is missing or changed; reconnect or review your edits."
                    )
                if not Path(entry.get("command", "")).is_file():
                    raise config.SetupError("The configured MCP executable is missing; reconnect.")
                report["clients"].append({"client": client, "status": "pass"})
                entries.append(entry)
            except (config.SetupError, OSError) as exc:
                report["clients"].append(
                    {"client": client, "status": "failed", "message": _error(exc)}
                )
        # Match the managed launch environment. A shell's unrelated token must
        # not override the credential the installed agents will actually use.
        if entries and all(entry.get("env", {}).get(server.TOKEN_ENV) == "" for entry in entries):
            with _active_token(""):
                token = server._configured_token()
        else:
            token = server._configured_token()
        if not token:
            raise config.SetupError("No credential available; run mireye-mcp login or connect.")
        account = server._sync_request("GET", "/v1/users/me", token=token, base_url=base_url)
        if not isinstance(account, dict) or not isinstance(account.get("user"), dict):
            raise config.SetupError("The auth check returned an invalid account response.")
        report["checks"].append({"check": "authentication", "status": "pass"})
        if not entries or any(row["status"] == "failed" for row in report["clients"]):
            return _emit(report, args)
        with _active_token(token):
            # Recover a lost installed notification after connect finished writing locally.
            _progress("installed", selected, base_url)
            if args.proof:
                # Shared spec is tested once; each client's actual entry was checked above.
                proofs = []
                for entry in entries:
                    if entry not in [item[0] for item in proofs]:
                        result = asyncio.run(asyncio.wait_for(_mcp_proof(entry), timeout=180))
                        proofs.append((entry, result))
                report["proof"] = proofs[0][1]
                report["checks"].append({"check": "mcp_fetch_proof", "status": "pass"})
                report["message"] = (
                    "A real MCP elevation call returned a value and source provenance."
                )
            else:
                report["message"] = (
                    "Configuration and auth verified. --proof makes one real elevation Fetch call."
                )
            # A requested proof is part of verification, including successful result validation.
            _progress("verified", selected, base_url)
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
            parser.add_argument("--api-key", help="Mireye API key to validate and save locally.")
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
            parser.add_argument(
                "--package-source", help="Local package source directory for isolated testing."
            )
        else:
            parser.add_argument(
                "--proof",
                action="store_true",
                help="Make one real elevation Fetch via installed MCP.",
            )
