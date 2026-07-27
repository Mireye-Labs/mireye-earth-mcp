"""Mireye Earth MCP server.

Model Context Protocol adapter exposing the public HTTP API (/v1/ask and
/v1/fetch) as MCP tools, plus MCP-native resources and prompts for agents
that want to discover the field catalog without adding extra tools.

The stdio package stays deliberately slim: tool calls proxy to the deployed
API, while read-only catalog resources fetch and cache /v1/meta/fields.

Configuration (environment variables):
    MIREYE_BASE_URL   Base URL of the deployed Mireye API.
                      Defaults to https://api.mireye.com. Stored login
                      credentials only attach when they were created
                      against this same URL; tokens are never sent over
                      plain http except to loopback hosts
                      (localhost/127.0.0.1/[::1]).
    MIREYE_TIMEOUT_S  Per-request timeout in seconds. Defaults to 120.
                      mireye_ask is LLM-backed with a ~110 s server-side
                      deadline, so the client timeout must exceed it — a
                      shorter one abandons requests the server still bills.
    MIREYE_BEARER_TOKEN
                      Optional bearer token for authenticated API calls.
    MIREYE_MCP_CREDENTIALS_FILE
                      Optional path to credentials created by
                      ``mireye-mcp login``.

Transport:
    stdio only. Run with ``uvx mireye-mcp`` (or the installed
    ``mireye-mcp`` entry point) and any MCP-aware client picks
    it up.

Run:
    uvx mireye-mcp
    # or, after `pip install mireye-mcp`:
    mireye-mcp
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ._generated_presets import PRESET_NAMES, MireyePreset

DEFAULT_BASE_URL = "https://api.mireye.com"
MIREYE_BASE_URL: str = os.getenv("MIREYE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
# Default must exceed the server's ~110 s /v1/ask deadline (+ margin): the buffered
# mireye_ask can legitimately take up to that long, and a client timeout under it
# abandons a request the server still (billably) completes. Override for
# fetch-only workloads if a tighter bound is wanted.
TIMEOUT_SECONDS: float = float(os.getenv("MIREYE_TIMEOUT_S", "120"))
TIMEOUT: httpx.Timeout = httpx.Timeout(TIMEOUT_SECONDS)
TOKEN_ENV = "MIREYE_BEARER_TOKEN"
CREDENTIALS_FILE_ENV = "MIREYE_MCP_CREDENTIALS_FILE"
MAX_FIELDS = 50
CATALOG_TTL_S = 3600
CATALOG_RESOURCE_URI = "mireye://catalog/*"
FIELD_CATALOG_URI = "mireye://catalog/fields"
PRESET_CATALOG_URI = "mireye://catalog/presets"

US_ENVELOPE: dict[str, float] = {
    "lat_min": 18.0,
    "lat_max": 72.0,
    "lng_min": -180.0,
    "lng_max": -65.0,
}

# MireyePreset + PRESET_NAMES are generated from catalog.PRESETS by
# `mireye-earth catalog codegen` (imported above). The stdio package stays
# dependency-free: the generated module is a pure Literal, no mireye_earth import.

MireyeLat = Annotated[
    float,
    Field(
        ge=US_ENVELOPE["lat_min"],
        le=US_ENVELOPE["lat_max"],
        description="Latitude in decimal degrees inside the supported US envelope.",
    ),
]
MireyeLng = Annotated[
    float,
    Field(
        ge=US_ENVELOPE["lng_min"],
        le=US_ENVELOPE["lng_max"],
        description="Longitude in decimal degrees inside the supported US envelope.",
    ),
]
MireyeAddress = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description=(
            "US street address. Include a city+state or a ZIP — the upstream "
            "cannot place a bare street line."
        ),
    ),
]
MireyeQuestion = Annotated[
    str,
    Field(
        min_length=1,
        max_length=2000,
        description="Natural-language question about the coordinate.",
    ),
]
MireyeFields = Annotated[
    list[str],
    Field(
        min_length=1,
        max_length=MAX_FIELDS,
        description=(
            "Catalog field names. Read mireye://catalog/fields or "
            "mireye://field/{name} for descriptions."
        ),
    ),
]

SERVER_INSTRUCTIONS = (
    "Use mireye_ask for natural-language questions about a US coordinate. "
    "Use mireye_fetch when the caller names exact fields or a preset. "
    "Read mireye://catalog/* resources to discover fields, presets, and bounds."
)
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    "mireye-earth",
    instructions=SERVER_INSTRUCTIONS,
    website_url="https://api.mireye.com",
)

_catalog_cache: dict[str, Any] | None = None
_catalog_etag: str | None = None
_catalog_fetched_monotonic = 0.0


def _package_version() -> str:
    try:
        return importlib.metadata.version("mireye-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "0+local"


def _log(event: str, **kw: Any) -> None:
    """Minimal stderr log. Helps debug 'MCP call didn't work' tickets."""
    parts = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[mireye-mcp] {event} {parts}".rstrip(), file=sys.stderr, flush=True)


def _credentials_path() -> Path:
    configured = os.getenv(CREDENTIALS_FILE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "mireye-mcp" / "credentials.json"


def _normalize_token(token: str | None) -> str | None:
    if not token:
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    return token or None


def _load_stored_credentials() -> dict[str, Any] | None:
    path = _credentials_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _log("credentials_unreadable", path=str(path), error_type=type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def _store_credentials(data: dict[str, Any]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _delete_credentials() -> bool:
    path = _credentials_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _stored_base_url(credentials: dict[str, Any]) -> str:
    """Base URL a stored credential is bound to.

    Credentials files lacking ``base_url`` (hand-written or created by
    external tooling — ``login`` has always recorded it) are conservatively
    bound to the default production URL.
    """
    raw = credentials.get("base_url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().rstrip("/")
    return DEFAULT_BASE_URL


def _require_token_safe_base_url(base_url: str) -> None:
    """Refuse to attach a bearer token to a URL that could leak it.

    https is always allowed; plain http only for localhost / 127.0.0.1 so
    local dev against a self-hosted API keeps working.
    """
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError as exc:
        raise _mcp_error(
            code="insecure_base_url",
            message=f"Refusing to send a bearer token to unparseable base URL {base_url}: {exc}",
            context={"base_url": base_url},
        ) from exc
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise _mcp_error(
        code="insecure_base_url",
        message=(
            f"Refusing to send a bearer token to non-https base URL {base_url}. "
            "Use an https URL, or http://localhost / http://127.0.0.1 / http://[::1] "
            "for local development."
        ),
        context={"base_url": base_url},
    )


# Token resolution and base-URL binding (S1, 2026-06-10):
#
#     MIREYE_BEARER_TOKEN set?
#       | yes                     | no
#       v                         v
#     explicit user intent;     credentials.json has a token?
#     no binding check            | yes                      | no
#       |                         v                          v
#       |                       stored base_url (default     None -> _post raises
#       |                       prod URL if absent) ==       mcp_auth_required
#       |                       active MIREYE_BASE_URL?
#       |                         | yes    | no
#       |                         |        v
#       |                         |      raise mcp_auth_required naming
#       |                         |      stored + active URL and the fix
#       v                         v
#     _auth_headers() scheme guard: https always; http only for
#     localhost / 127.0.0.1; anything else raises insecure_base_url.
def _configured_token() -> str | None:
    env_token = _normalize_token(os.getenv(TOKEN_ENV))
    if env_token:
        return env_token
    credentials = _load_stored_credentials()
    if not credentials:
        return None
    token = _normalize_token(credentials.get("token"))
    if not token:
        return None
    stored_base_url = _stored_base_url(credentials)
    if stored_base_url != MIREYE_BASE_URL:
        raise _mcp_error(
            code="mcp_auth_required",
            message=(
                f"Stored credentials are bound to {stored_base_url} but "
                f"MIREYE_BASE_URL is {MIREYE_BASE_URL}. Run `mireye-mcp login` "
                f"while MIREYE_BASE_URL points at the URL you want, or set "
                f"{TOKEN_ENV} to a token for {MIREYE_BASE_URL}."
            ),
            http_status=401,
            context={
                "stored_base_url": stored_base_url,
                "active_base_url": MIREYE_BASE_URL,
            },
        )
    return token


def _auth_headers(token: str | None, *, base_url: str) -> dict[str, str]:
    token = _normalize_token(token)
    if not token:
        return {}
    _require_token_safe_base_url(base_url)
    return {"Authorization": f"Bearer {token}"}


def _json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _mcp_error(
    *,
    code: str,
    message: str,
    http_status: int | None = None,
    request_id: str | None = None,
    retryable: bool = False,
    context: dict[str, Any] | None = None,
    tool_name: str | None = None,
    resource_uri: str | None = None,
) -> RuntimeError:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if tool_name:
        payload["tool"] = tool_name
    if resource_uri:
        payload["resource"] = resource_uri
    if http_status is not None:
        payload["http_status"] = http_status
    if request_id:
        payload["request_id"] = request_id
    if context:
        payload["context"] = context
    return RuntimeError(json.dumps(payload, sort_keys=True))


def _tool_error(
    *,
    code: str,
    message: str,
    tool_name: str,
    http_status: int | None = None,
    request_id: str | None = None,
    retryable: bool = False,
    context: dict[str, Any] | None = None,
) -> RuntimeError:
    return _mcp_error(
        code=code,
        message=message,
        tool_name=tool_name,
        http_status=http_status,
        request_id=request_id,
        retryable=retryable,
        context=context,
    )


def _resource_error(
    *,
    code: str,
    message: str,
    resource_uri: str,
    http_status: int | None = None,
    request_id: str | None = None,
    retryable: bool = False,
    context: dict[str, Any] | None = None,
) -> RuntimeError:
    return _mcp_error(
        code=code,
        message=message,
        resource_uri=resource_uri,
        http_status=http_status,
        request_id=request_id,
        retryable=retryable,
        context=context,
    )


def _response_error(
    resp: httpx.Response,
    *,
    tool_name: str | None = None,
    resource_uri: str | None = None,
) -> RuntimeError:
    request_id = resp.headers.get("X-Request-ID")
    code = f"http_{resp.status_code}"
    message = resp.reason_phrase or f"HTTP {resp.status_code}"
    context: dict[str, Any] = {}
    # The API states retryability explicitly; the status code only approximates
    # it. Start from the approximation and let the server override.
    retryable = resp.status_code in {408, 429} or resp.status_code >= 500
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            code = str(detail.get("error") or detail.get("code") or code)
            message = str(detail.get("message") or message)
            # Prefer the server's own flag. Deriving retryability from the
            # status alone contradicts it: `geocode_unconfigured` is a 503 the
            # API marks NOT retryable (the key is missing from the serving
            # env — retrying cannot fix a config gap), while `>= 500` reads it
            # as transient and an agent client retries a permanent failure in a
            # loop. The two MCP surfaces are asserted byte-identical on their
            # tool DESCRIPTIONS, which cannot catch a behavioural split like
            # this one.
            if isinstance(detail.get("retryable"), bool):
                retryable = detail["retryable"]
            context = {
                k: v for k, v in detail.items()
                if k not in {"error", "code", "message", "retryable"}
            }
        elif detail:
            message = str(detail)
            context = {"detail": detail}
    return _mcp_error(
        code=code,
        message=message,
        tool_name=tool_name,
        resource_uri=resource_uri,
        http_status=resp.status_code,
        request_id=request_id,
        retryable=retryable,
        context=context,
    )


def _locator_body(
    lat: float | None, lng: float | None, address: str | None, *, tool_name: str
) -> dict[str, Any]:
    """Build the lat/lng-or-address half of a request body.

    This package is a thin HTTP proxy — the server owns the real contract. The
    check here exists so an agent gets a structured, actionable tool error
    instead of a round trip that comes back 422.
    """
    if address is not None and (lat is not None or lng is not None):
        raise _tool_error(
            code="invalid_locator",
            message="Provide either lat+lng or address, not both.",
            tool_name=tool_name,
        )
    if address is not None:
        return {"address": address}
    if lat is None or lng is None:
        raise _tool_error(
            code="invalid_locator",
            message="Provide either lat+lng or address.",
            tool_name=tool_name,
        )
    _validate_coordinate(lat, lng, tool_name=tool_name)
    return {"lat": lat, "lng": lng}


def _validate_coordinate(lat: float, lng: float, *, tool_name: str) -> None:
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise _tool_error(
            code="coord_invalid",
            message="lat and lng must be finite decimal-degree numbers.",
            tool_name=tool_name,
        )
    if not (US_ENVELOPE["lat_min"] <= lat <= US_ENVELOPE["lat_max"]):
        raise _tool_error(
            code="coord_out_of_bounds",
            message=(
                f"lat={lat} outside US envelope "
                f"[{US_ENVELOPE['lat_min']}, {US_ENVELOPE['lat_max']}]"
            ),
            tool_name=tool_name,
            http_status=400,
        )
    if not (US_ENVELOPE["lng_min"] <= lng <= US_ENVELOPE["lng_max"]):
        raise _tool_error(
            code="coord_out_of_bounds",
            message=(
                f"lng={lng} outside US envelope "
                f"[{US_ENVELOPE['lng_min']}, {US_ENVELOPE['lng_max']}]"
            ),
            tool_name=tool_name,
            http_status=400,
        )


def _validate_fetch_args(
    fields: list[str] | None,
    preset: str | None,
    *,
    tool_name: str,
) -> None:
    if fields is not None and len(fields) > MAX_FIELDS:
        raise _tool_error(
            code="fields_too_many",
            message=f"Requested {len(fields)} fields; max is {MAX_FIELDS}.",
            tool_name=tool_name,
            http_status=400,
            context={"max": MAX_FIELDS, "requested": len(fields)},
        )
    if preset is not None and preset not in PRESET_NAMES:
        raise _tool_error(
            code="preset_unknown",
            message=f"Unknown preset: {preset}",
            tool_name=tool_name,
            http_status=400,
            context={"presets": list(PRESET_NAMES)},
        )


async def _post(path: str, payload: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    """POST JSON to the Mireye API.

    Raises RuntimeError with a machine-readable JSON body on failure. FastMCP
    converts that into an MCP tool error for protocol clients, and direct tests
    can assert the same payload.
    """
    url = f"{MIREYE_BASE_URL}{path}"
    headers = _auth_headers(_configured_token(), base_url=MIREYE_BASE_URL)
    if not headers:
        raise _tool_error(
            code="mcp_auth_required",
            message=(
                "Mireye MCP is not authenticated. Run `mireye-mcp login` "
                f"or set {TOKEN_ENV} to an API bearer token."
            ),
            tool_name=tool_name,
            http_status=401,
        )
    headers.update({
        "X-Mireye-Client-Surface": "mcp_proxy",
        "X-Mireye-MCP-Tool": tool_name,
        "X-Mireye-MCP-Call-ID": f"mcp_call_{uuid4().hex}",
        "X-Mireye-MCP-Package-Version": _package_version(),
    })
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise _tool_error(
            code="upstream_timeout",
            message=f"Mireye API timed out after {TIMEOUT_SECONDS:g} seconds.",
            tool_name=tool_name,
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise _tool_error(
            code="upstream_unreachable",
            message=str(exc),
            tool_name=tool_name,
            retryable=True,
        ) from exc
    rid = resp.headers.get("X-Request-ID", "-")
    _log("tool_call", path=path, status=resp.status_code, request_id=rid)
    if resp.is_error:
        raise _response_error(resp, tool_name=tool_name)
    return resp.json()


def _catalog_payload() -> dict[str, Any]:
    global _catalog_cache, _catalog_etag, _catalog_fetched_monotonic

    now = time.monotonic()
    if _catalog_cache is not None and now - _catalog_fetched_monotonic < CATALOG_TTL_S:
        return _catalog_cache

    headers = {"If-None-Match": _catalog_etag} if _catalog_etag else {}
    url = f"{MIREYE_BASE_URL}/v1/meta/fields"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise _resource_error(
            code="catalog_timeout",
            message=f"Mireye catalog timed out after {TIMEOUT_SECONDS:g} seconds.",
            resource_uri=CATALOG_RESOURCE_URI,
            retryable=True,
            context={"endpoint": "/v1/meta/fields"},
        ) from exc
    except httpx.HTTPError as exc:
        raise _resource_error(
            code="catalog_unreachable",
            message=str(exc),
            resource_uri=CATALOG_RESOURCE_URI,
            retryable=True,
            context={"endpoint": "/v1/meta/fields"},
        ) from exc

    rid = resp.headers.get("X-Request-ID", "-")
    _log("catalog_resource", status=resp.status_code, request_id=rid)
    if resp.status_code == 304:
        if _catalog_cache is not None:
            _catalog_fetched_monotonic = now
            return _catalog_cache
        raise _resource_error(
            code="catalog_cache_miss",
            message="Mireye catalog returned 304 but no cached catalog is available.",
            resource_uri=CATALOG_RESOURCE_URI,
            http_status=304,
            request_id=rid,
            retryable=True,
            context={"endpoint": "/v1/meta/fields"},
        )
    if resp.is_error:
        raise _response_error(resp, resource_uri=CATALOG_RESOURCE_URI)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise _resource_error(
            code="catalog_invalid_json",
            message="Mireye catalog response was not valid JSON.",
            resource_uri=CATALOG_RESOURCE_URI,
            request_id=rid,
            retryable=True,
            context={"endpoint": "/v1/meta/fields"},
        ) from exc

    if not isinstance(payload, dict):
        raise _resource_error(
            code="catalog_invalid_json",
            message="Mireye catalog response was not a JSON object.",
            resource_uri=CATALOG_RESOURCE_URI,
            request_id=rid,
            retryable=True,
            context={"endpoint": "/v1/meta/fields"},
        )
    _catalog_cache = payload
    _catalog_etag = resp.headers.get("ETag") or _catalog_etag
    _catalog_fetched_monotonic = now
    return payload


def _catalog_fields() -> list[dict[str, Any]]:
    fields = _catalog_payload().get("fields", [])
    return fields if isinstance(fields, list) else []


def _catalog_presets() -> dict[str, list[str]]:
    presets = _catalog_payload().get("presets", {})
    return presets if isinstance(presets, dict) else {}


def _field_by_name(name: str) -> dict[str, Any]:
    for spec in _catalog_fields():
        if spec.get("name") == name:
            return spec
    raise _resource_error(
        code="field_unknown",
        message=(
            f"Unknown Mireye field resource: {name}. "
            f"Read {FIELD_CATALOG_URI} for valid field names."
        ),
        resource_uri=f"mireye://field/{name}",
        http_status=404,
        retryable=False,
        context={"name": name, "catalog": FIELD_CATALOG_URI},
    )


def _sync_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    target_base_url = base_url or MIREYE_BASE_URL
    url = f"{target_base_url}{path}"
    headers = _auth_headers(token, base_url=target_base_url)
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.request(method, url, json=payload, headers=headers)
        rid = resp.headers.get("X-Request-ID", "-")
        _log("cli_call", path=path, status=resp.status_code, request_id=rid)
        resp.raise_for_status()
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            # A 2xx with a non-JSON body (proxy interstitial, server bug)
            # must surface as the structured error every CLI path already
            # handles — not a raw traceback.
            raise _mcp_error(
                code="malformed_response",
                message=f"Mireye API returned a non-JSON body for {path}.",
                http_status=resp.status_code,
                request_id=rid,
                retryable=True,
                context={"path": path},
            ) from exc


def _cli_error_message(exc: RuntimeError) -> str:
    """Human-readable message from an ``_mcp_error``-style RuntimeError."""
    try:
        return str(json.loads(str(exc))["message"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return str(exc)


def _cmd_login(args: argparse.Namespace) -> int:
    # Fail fast: a login against a remote plain-http URL would receive the
    # minted token in cleartext and store a credential every other code
    # path refuses to attach.
    try:
        _require_token_safe_base_url(MIREYE_BASE_URL)
    except RuntimeError as exc:
        print(f"Login refused: {_cli_error_message(exc)}", file=sys.stderr)
        return 1

    try:
        start = _sync_request("POST", "/v1/mcp/device/start", payload={})
    except httpx.HTTPStatusError as exc:
        print(f"Login failed: HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # e.g. malformed_response from a non-JSON body.
        print(f"Login failed: {_cli_error_message(exc)}", file=sys.stderr)
        return 1
    verification_uri = start.get("verification_uri")
    user_code = start.get("user_code")
    device_code = start.get("device_code")
    if not (verification_uri and user_code and device_code):
        print("Login failed: malformed device-flow response.", file=sys.stderr)
        return 1
    # The poll interval is server-controlled; clamp so a buggy response
    # can't produce a tight loop (0/negative) or out-sleep the deadline.
    try:
        interval = min(max(int(start.get("interval") or 5), 1), 60)
    except (TypeError, ValueError):
        interval = 5

    print("Mireye MCP login")
    print(f"Open: {verification_uri}")
    print(f"Code: {user_code}")

    if not args.no_open:
        try:
            import webbrowser

            webbrowser.open(verification_uri)
        except Exception as exc:  # noqa: BLE001 - browser launch is best-effort.
            _log("browser_open_failed", error_type=type(exc).__name__)

    deadline = time.monotonic() + float(args.timeout)
    while time.monotonic() < deadline:
        try:
            result = _sync_request(
                "POST",
                "/v1/mcp/device/poll",
                payload={"device_code": device_code},
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if 400 <= status_code < 500 and status_code != 429:
                # Real client error (expired/invalid device code) — give up.
                print(f"Login failed: HTTP {status_code}", file=sys.stderr)
                return 1
            # 5xx / 429: transient — keep polling until the deadline.
            _log("login_poll_retry", status=status_code)
            time.sleep(interval)
            continue
        except httpx.HTTPError as exc:
            # Network blip mid device-flow — keep polling until the deadline.
            _log("login_poll_retry", error_type=type(exc).__name__)
            time.sleep(interval)
            continue
        except RuntimeError as exc:
            # Non-JSON poll body (malformed_response) — transient like a
            # network blip; keep polling until the deadline.
            _log("login_poll_retry", error_type=type(exc).__name__)
            time.sleep(interval)
            continue
        status = result.get("status")
        if status == "approved":
            if not result.get("token"):
                print("Login failed: malformed device-flow response.", file=sys.stderr)
                return 1
            _store_credentials(
                {
                    "base_url": MIREYE_BASE_URL,
                    "token": result["token"],
                    "token_id": result.get("token_id"),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            print(f"Logged in. Credentials saved to {_credentials_path()}.")
            return 0
        if status in {"expired", "already_claimed"}:
            print(f"Login failed: {status}", file=sys.stderr)
            return 1
        time.sleep(interval)

    print("Login timed out before approval.", file=sys.stderr)
    return 1


def _cmd_status(_args: argparse.Namespace) -> int:
    # Stored logins are checked against the base URL they are bound to —
    # not a MIREYE_BASE_URL override — so `status` (like `logout --revoke`)
    # keeps working after a binding mismatch.
    credentials = _load_stored_credentials() or {}
    token = _normalize_token(credentials.get("token"))
    base_url = _stored_base_url(credentials) if token else None
    if not token:
        token = _normalize_token(os.getenv(TOKEN_ENV))
    if not token:
        print("Not logged in.")
        return 1
    try:
        account = _sync_request("GET", "/v1/users/me", token=token, base_url=base_url)
    except httpx.HTTPStatusError as exc:
        print(f"Token check failed: HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        # Network errors (connection refused, DNS, timeout) — clean message,
        # same as the login flow.
        print(f"Token check failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # e.g. insecure_base_url from the token guard — print the payload
        # message instead of a traceback.
        print(f"Token check failed: {_cli_error_message(exc)}", file=sys.stderr)
        return 1
    user = account.get("user") or {}
    email = user.get("email") or user.get("uid") or "unknown"
    print(f"Logged in as {email}.")
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    credentials = _load_stored_credentials() or {}
    token = _normalize_token(credentials.get("token"))
    token_id = credentials.get("token_id")
    if args.revoke and token and token_id:
        try:
            _sync_request(
                "DELETE",
                f"/v1/users/me/tokens/{token_id}",
                token=token,
                base_url=_stored_base_url(credentials),
            )
            print("Revoked backend token.")
        except httpx.HTTPStatusError as exc:
            print(f"Token revoke failed: HTTP {exc.response.status_code}", file=sys.stderr)
            return 1
        except httpx.HTTPError as exc:
            # Network errors — keep the credentials so the user can retry.
            print(f"Token revoke failed: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"Token revoke failed: {_cli_error_message(exc)}", file=sys.stderr)
            return 1
    removed = _delete_credentials()
    print("Logged out." if removed else "No stored credentials found.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mireye-mcp")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="Authenticate this MCP client.")
    login.add_argument("--timeout", type=int, default=900, help="Seconds to wait for approval.")
    login.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    login.set_defaults(func=_cmd_login)

    status = sub.add_parser("status", help="Check stored MCP credentials.")
    status.set_defaults(func=_cmd_status)

    logout = sub.add_parser("logout", help="Remove stored MCP credentials.")
    logout.add_argument("--revoke", action="store_true", help="Revoke the backend API token too.")
    logout.set_defaults(func=_cmd_logout)

    return parser


# Tools are prefixed with ``mireye_`` so they sort together in tool lists
# and don't collide with generic names (``ask``, ``fetch``) from other MCP
# servers like the official mcp-server-fetch.


@mcp.resource(
    "mireye://catalog/fields",
    name="mireye_catalog_fields",
    title="Mireye Field Catalog",
    description="All public Mireye field definitions with descriptions, sources, hints, and TTLs.",
    mime_type="application/json",
)
def _mireye_catalog_fields_resource() -> str:
    catalog = _catalog_payload()
    return _json_dumps(
        {
            "version": catalog.get("version"),
            "fields": catalog.get("fields", []),
        }
    )


@mcp.resource(
    "mireye://catalog/presets",
    name="mireye_catalog_presets",
    title="Mireye Presets",
    description="Preset names and their field expansions for mireye_fetch.",
    mime_type="application/json",
)
def _mireye_catalog_presets_resource() -> str:
    catalog = _catalog_payload()
    return _json_dumps(
        {
            "version": catalog.get("version"),
            "presets": catalog.get("presets", {}),
        }
    )


@mcp.resource(
    "mireye://catalog/us-envelope",
    name="mireye_us_envelope",
    title="Mireye US Envelope",
    description="Supported latitude and longitude bounds for Mireye coordinate tools.",
    mime_type="application/json",
)
def _mireye_us_envelope_resource() -> str:
    catalog = _catalog_payload()
    return _json_dumps(
        {
            "version": catalog.get("version"),
            "us_envelope": catalog.get("us_envelope", US_ENVELOPE),
        }
    )


@mcp.resource(
    "mireye://field/{name}",
    name="mireye_field",
    title="Mireye Field",
    description="Single field definition from the Mireye catalog by field name.",
    mime_type="application/json",
)
def _mireye_field_resource(name: str) -> str:
    return _json_dumps(_field_by_name(name))


@mcp.resource(
    "mireye://preset/{name}",
    name="mireye_preset",
    title="Mireye Preset",
    description="Single preset expansion from the Mireye catalog by preset name.",
    mime_type="application/json",
)
def _mireye_preset_resource(name: str) -> str:
    presets = _catalog_presets()
    if name not in presets:
        raise _resource_error(
            code="preset_unknown",
            message=(
                f"Unknown Mireye preset resource: {name}. "
                f"Read {PRESET_CATALOG_URI} for valid preset names."
            ),
            resource_uri=f"mireye://preset/{name}",
            http_status=404,
            retryable=False,
            context={"name": name, "catalog": PRESET_CATALOG_URI},
        )
    return _json_dumps({"name": name, "fields": presets[name]})


@mcp.tool(
    title="Ask Mireye",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
    structured_output=True,
)
async def mireye_ask(
    question: MireyeQuestion,
    lat: MireyeLat | None = None,
    lng: MireyeLng | None = None,
    address: MireyeAddress | None = None,
) -> dict[str, Any]:
    """Answer a natural-language question about a US location, with citations to authoritative federal data sources. Give EITHER lat+lng OR address, never both. Returns the answer plus per-citation provenance (source, source URL, fetched_at, confidence). Use this when the caller has a specific question about a place (e.g. 'is this in a flood zone?', 'what's the wildfire risk here?'). When you pass an address, the response carries a `geocode` block: if `parcel_grade` is false the location was estimated from the street and can be ~2.9 km out in rural areas, and the answer says so."""  # noqa: E501
    body = _locator_body(lat, lng, address, tool_name="mireye_ask")
    return await _post("/v1/ask", {**body, "question": question}, tool_name="mireye_ask")


@mcp.tool(
    title="Fetch Mireye Fields",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
    structured_output=True,
)
async def mireye_fetch(
    lat: MireyeLat | None = None,
    lng: MireyeLng | None = None,
    address: MireyeAddress | None = None,
    fields: MireyeFields | None = None,
    preset: MireyePreset | None = None,
) -> dict[str, Any]:
    """Fetch specific data fields at a US location with full provenance per field. Give EITHER lat+lng OR address, never both. Use this when the caller knows exactly which fields they need (e.g. 'elevation and slope at this point') or wants to power a custom workflow. Each field includes its value, source, source URL, fetched_at timestamp, and confidence. When you pass an address, the response carries a `geocode` block: check `parcel_grade` before trusting parcel-specific fields — a false value means the coordinate was estimated from the street and can be ~2.9 km out in rural areas."""  # noqa: E501
    payload = _locator_body(lat, lng, address, tool_name="mireye_fetch")
    _validate_fetch_args(fields, preset, tool_name="mireye_fetch")
    if fields:
        payload["fields"] = fields
    if preset:
        payload["preset"] = preset
    return await _post("/v1/fetch", payload, tool_name="mireye_fetch")


@mcp.tool(
    title="Geocode a US Address",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
    structured_output=True,
)
async def mireye_geocode(address: MireyeAddress) -> dict[str, Any]:
    """Resolve a US street address to a coordinate, with the quality of that coordinate. Feed lat/lng to mireye_fetch or mireye_ask. ALWAYS check accuracy_type before trusting the result: 'rooftop' is on the parcel, but 'range_interpolation' is estimated along a street centerline and can be ~2.9 km out in rural areas — far enough to describe a neighbouring property instead. An address that can only be placed at a ZIP/city/county centroid is REJECTED as address_too_coarse rather than returned — if you get that, ask the user for a more complete address instead of retrying. The address you send is retained alongside the coordinate it resolves to, so a result can be audited later; pass lat/lng instead if that does not suit the caller."""  # noqa: E501
    # Thin proxy, like every tool here — no geocoding logic crosses into this
    # package. It stays dependency-light (httpx + mcp only) on purpose, which
    # is also why this is a deliberate copy of the hosted tool rather than a
    # shared import. Keep the signature and docstring in step with
    # api/main.py's mireye_geocode; tests/test_mcp.py asserts surface parity.
    return await _post("/v1/geocode", {"address": address}, tool_name="mireye_geocode")


# Claude Code surfaces MCP prompts as slash commands under the form
# ``/mcp__<server>__<prompt>``. With our server named ``mireye-earth`` these
# render as ``/mcp__mireye-earth__mireye_ask`` and
# ``/mcp__mireye-earth__mireye_fetch``. The slash format isn't customizable;
# the prompts simply nudge the model to call the matching tool.


@mcp.prompt(name="mireye_ask")
def _mireye_ask_prompt(lat: str, lng: str, question: str) -> str:
    """Ask Mireye Earth a natural-language question about a US coordinate."""
    return (
        f"Call the `mireye_ask` tool with lat={lat}, lng={lng}, and "
        f"question={question!r}. Return the cited answer to the user."
    )


@mcp.prompt(name="mireye_fetch")
def _mireye_fetch_prompt(lat: str, lng: str, fields: str = "", preset: str = "") -> str:
    """Fetch specific Mireye Earth fields (or a preset) at a US coordinate."""
    parts = [f"lat={lat}", f"lng={lng}"]
    field_names = [field.strip() for field in fields.split(",") if field.strip()]
    if field_names:
        parts.append(f"fields={field_names!r}")
    if preset:
        parts.append(f"preset={preset!r}")
    args = ", ".join(parts)
    return (
        f"Call the `mireye_fetch` tool with {args}. Return the per-field "
        "values and citations to the user."
    )


@mcp.prompt(
    name="mireye_site_report",
    title="Mireye Site Report",
    description="Create a concise site report using the site_selection preset.",
)
def _mireye_site_report_prompt(lat: str, lng: str, focus: str = "") -> str:
    """Generate a concise site report for a coordinate."""
    focus_line = f" Emphasize: {focus}." if focus else ""
    return (
        f"Call `mireye_fetch` with lat={lat}, lng={lng}, preset='site_selection'."
        f"{focus_line} Summarize terrain, land cover, utilities, boundaries, "
        "and any partial_failures. Include citations/provenance for important claims."
    )


@mcp.prompt(
    name="mireye_flood_check",
    title="Mireye Flood Check",
    description="Assess flood-relevant signals using the flood_risk preset.",
)
def _mireye_flood_check_prompt(lat: str, lng: str) -> str:
    """Check flood-relevant signals for a coordinate."""
    return (
        f"Call `mireye_fetch` with lat={lat}, lng={lng}, preset='flood_risk'. "
        "Frame the answer around elevation, coast distance, floodplain, watershed, "
        "nearby water, and soil drainage. Include provenance and confidence."
    )


@mcp.prompt(
    name="mireye_wildfire_underwrite",
    title="Mireye Wildfire Underwrite",
    description="Assess wildfire underwriting signals using the wildfire preset.",
)
def _mireye_wildfire_underwrite_prompt(lat: str, lng: str) -> str:
    """Check wildfire underwriting signals for a coordinate."""
    return (
        f"Call `mireye_fetch` with lat={lat}, lng={lng}, "
        "preset='wildfire_underwrite'. Summarize slope, vegetation/fuel, "
        "WUI distance, canopy, and access context. Include provenance."
    )


@mcp.prompt(
    name="mireye_pick_fields",
    title="Mireye Pick Fields",
    description="Use catalog resources to choose fields before calling mireye_fetch.",
)
def _mireye_pick_fields_prompt(question: str) -> str:
    """Choose Mireye fields for a user question."""
    return (
        "Read `mireye://catalog/fields` and `mireye://catalog/presets`, then "
        f"choose the smallest useful set of fields for this question: {question!r}. "
        "Return the selected field names, any useful preset, and a short rationale."
    )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        parser = _build_parser()
        args = parser.parse_args(argv)
        if not hasattr(args, "func"):
            parser.print_help()
            raise SystemExit(2)
        raise SystemExit(args.func(args))

    _log("starting", base_url=MIREYE_BASE_URL, timeout_s=TIMEOUT_SECONDS)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
