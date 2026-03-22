"""Shared Atlassian OAuth and API helpers."""

from __future__ import annotations

import base64
import binascii
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from gotta.compat import tomllib
from gotta.config import (
    config_file_candidates,
    display_path,
    env_or_config,
    extract_provider_env,
    primary_config_file,
    provider_env_reference,
    user_state_dir,
)
from gotta.vault import (
    load_secret_json_object,
    write_secret_json_atomic,
    write_secret_text_atomic,
)

OAUTH_DIR = user_state_dir() / "auth" / "atlassian"
TOKEN_FILE = OAUTH_DIR / "oauth.json"
CLOUD_ID_FILE = OAUTH_DIR / "oauth-cloud-id"
CONFIG_FILE = primary_config_file()
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_ACCESSIBLE_RESOURCES_URL = (
    "https://api.atlassian.com/oauth/token/accessible-resources"
)
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
DEFAULT_OAUTH_SCOPE = (
    "offline_access read:jira-work write:jira-work"
    " read:board-scope:jira-software read:sprint:jira-software"
    " write:sprint:jira-software read:project:jira"
    " read:confluence-content.all read:confluence-space.summary"
    " read:confluence-props search:confluence read:page:confluence"
    " read:space:confluence read:attachment:confluence"
    " readonly:content.attachment:confluence write:confluence-content"
    " write:confluence-file write:confluence-props write:page:confluence"
    " read:comment:confluence"
)
DISALLOWED_MCP_PASSTHROUGH_FLAGS = {
    "--oauth-setup",
    "--oauth-client-id",
    "--oauth-client-secret",
    "--oauth-redirect-uri",
    "--oauth-scope",
    "--oauth-cloud-id",
    "--oauth-access-token",
}


class AtlassianError(RuntimeError):
    """Raised when Atlassian auth or API operations fail."""


def decode_confluence_tiny_page_id(token: str) -> str | None:
    normalized = token.strip()
    if not normalized:
        return None
    if len(normalized) > 6:
        return None
    # Confluence tiny links drop trailing "A" bytes from the 6-character payload
    # and use "-" / "_" substitutions in their URL form.
    normalized = normalized.replace("-", "/").replace("_", "+").ljust(6, "A")
    try:
        raw = base64.b64decode(normalized + "==", validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(raw) != 4:
        return None
    return str(int.from_bytes(raw, "little", signed=False))


def extract_confluence_page_id(raw: str) -> str | None:
    candidate = raw.strip()
    if candidate.startswith("confluence:"):
        candidate = candidate.removeprefix("confluence:")
    if candidate.isdigit():
        return candidate
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        params = urllib.parse.parse_qs(parsed.query)
        page_id = str(params.get("pageId", [""])[0] or "").strip()
        if page_id.isdigit():
            return page_id
        match = re.search(r"/pages/(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(1)
        blog_match = re.search(r"/blog(?:/[^/]+)*/(\d+)(?:/|$)", parsed.path)
        if blog_match:
            return blog_match.group(1)
        short_match = re.search(r"/wiki/x/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        if short_match:
            return decode_confluence_tiny_page_id(short_match.group(1))
    return None


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_json_file(path: Path) -> dict[str, Any]:
    try:
        data, _recovered = load_secret_json_object(path)
    except ValueError as exc:
        raise AtlassianError(f"invalid OAuth state file {path}: {exc}") from exc
    return data


def parse_toml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise AtlassianError(f"invalid TOML config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AtlassianError(f"invalid TOML config {path}: expected table")
    return data


def load_atlassian_config_env() -> dict[str, str]:
    for config_file in config_file_candidates():
        if not config_file.exists():
            continue
        config = parse_toml_file(config_file)
        result = extract_provider_env(config, "atlassian")
        if result:
            return result
    return {}


def load_mcp_server_env() -> dict[str, str]:
    return load_atlassian_config_env()


def jwt_expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = data.get("exp")
    if isinstance(exp, int):
        return exp
    if isinstance(exp, float):
        return int(exp)
    return None


def token_is_expired(
    token: str, *, expires_at: float | None = None, skew_seconds: int = 300
) -> bool:
    now = time.time() + skew_seconds
    if expires_at is not None:
        return now >= expires_at
    exp = jwt_expiry(token)
    return exp is not None and now >= exp


def ensure_oauth_dir() -> None:
    OAUTH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OAUTH_DIR, 0o700)


def write_secret_text(path: Path, value: str) -> None:
    write_secret_text_atomic(path, value, ensure_dir=ensure_oauth_dir)


def sync_aux_oauth_files(cloud_id: str) -> None:
    ensure_oauth_dir()
    if cloud_id:
        write_secret_text(CLOUD_ID_FILE, cloud_id + "\n")


def persist_oauth_state(client_id: str, oauth_state: dict[str, Any]) -> dict[str, Any]:
    ensure_oauth_dir()
    write_secret_json_atomic(TOKEN_FILE, oauth_state, ensure_dir=ensure_oauth_dir)
    cloud_id = str(oauth_state.get("cloud_id") or "").strip()
    sync_aux_oauth_files(cloud_id)
    return oauth_state


def load_cached_oauth_json() -> dict[str, Any] | None:
    if not TOKEN_FILE.exists():
        return None
    return parse_json_file(TOKEN_FILE)


def has_local_oauth_state() -> bool:
    return bool(TOKEN_FILE.exists() or CLOUD_ID_FILE.exists())


def _default_base_url(
    *,
    config_env: dict[str, str],
    oauth_state: dict[str, Any] | None,
    explicit_base_url: str,
) -> str:
    if explicit_base_url:
        return site_root(explicit_base_url)
    for name in ("GOTTA_JIRA_BASE_URL", "GOTTA_CONFLUENCE_BASE_URL"):
        value = env_or_config(config_env, name)
        if value:
            return site_root(value)
    if oauth_state:
        cached = str(oauth_state.get("base_url") or "").strip()
        if cached:
            return site_root(cached)
    return ""


def _next_atlassian_step(payload: dict[str, Any], *, auth_command: str) -> str:
    refresh_first = (
        f"rerun a native {auth_command} command first; gotta will usually refresh "
        "automatically when a refresh token is present. Run "
        f"`gotta {auth_command} auth` only if refresh does not recover the session"
    )
    if not payload.get("credentialsConfigured"):
        return (
            "configure Atlassian OAuth credentials under "
            f"{provider_env_reference('atlassian')}"
        )
    session_status = str(payload.get("sessionStatus") or "missing")
    token_preflight = str(payload.get("tokenPreflight") or "")
    has_refresh_token = bool(payload.get("hasRefreshToken"))
    if session_status in {"missing", "invalid"}:
        return f"run `gotta {auth_command} auth`"
    if session_status == "expired":
        return refresh_first if has_refresh_token else f"run `gotta {auth_command} auth`"
    if token_preflight == "invalid":
        return refresh_first if has_refresh_token else f"run `gotta {auth_command} auth`"
    if not payload.get("baseUrl"):
        return (
            "persist the Atlassian tenant after auth or set "
            "GOTTA_JIRA_BASE_URL / GOTTA_CONFLUENCE_BASE_URL"
        )
    return "ready"


def atlassian_status_payload(
    *,
    base_url: str = "",
    check_token: bool = False,
    auth_command: str = "jira",
) -> dict[str, Any]:
    config_env = load_atlassian_config_env()
    oauth_state: dict[str, Any] | None = None
    payload: dict[str, Any] = {
        "oauthDir": str(OAUTH_DIR),
        "tokenFile": str(TOKEN_FILE),
        "tokenFileExists": TOKEN_FILE.exists(),
        "cloudIdFile": str(CLOUD_ID_FILE),
        "cloudIdFileExists": CLOUD_ID_FILE.exists(),
        "configFile": str(CONFIG_FILE),
        "configFileExists": CONFIG_FILE.exists(),
        "credentialsConfigured": False,
        "credentialSources": {
            "envClientId": bool(env_or_config({}, "GOTTA_ATLASSIAN_OAUTH_CLIENT_ID")),
            "envClientSecret": bool(
                env_or_config({}, "GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET")
            ),
            "configClientId": bool(
                env_or_config(config_env, "GOTTA_ATLASSIAN_OAUTH_CLIENT_ID")
            ),
            "configClientSecret": bool(
                env_or_config(config_env, "GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET")
            ),
        },
        "baseUrl": "",
        "sessionStatus": "missing",
    }
    try:
        client_id, client_secret = load_client_credentials()
        payload["credentialsConfigured"] = bool(client_id and client_secret)
    except AtlassianError as exc:
        payload["credentialsError"] = str(exc)

    payload["cloudId"] = load_cloud_id()
    try:
        oauth_state = load_cached_oauth_json()
    except AtlassianError as exc:
        payload["sessionStatus"] = "invalid"
        payload["error"] = str(exc)
        payload["baseUrl"] = _default_base_url(
            config_env=config_env,
            oauth_state=None,
            explicit_base_url=base_url,
        )
        payload["nextStep"] = _next_atlassian_step(payload, auth_command=auth_command)
        return payload
    if oauth_state:
        access_token = str(oauth_state.get("access_token") or "").strip()
        expires_at = oauth_state.get("expires_at")
        payload["expiresAt"] = expires_at
        payload["hasRefreshToken"] = bool(str(oauth_state.get("refresh_token") or "").strip())
        payload["hasCachedAccessToken"] = bool(access_token)
        if access_token:
            payload["sessionStatus"] = (
                "expired"
                if token_is_expired(
                    access_token,
                    expires_at=float(expires_at)
                    if isinstance(expires_at, (int, float))
                    else None,
                    skew_seconds=0,
                )
                else "usable"
            )
    if check_token and payload["sessionStatus"] != "missing":
        token = str(oauth_state.get("access_token") or "").strip() if oauth_state else ""
        if token:
            payload["tokenPreflight"] = token_preflight_status(token)
    payload["baseUrl"] = _default_base_url(
        config_env=config_env,
        oauth_state=oauth_state,
        explicit_base_url=base_url,
    )
    payload["nextStep"] = _next_atlassian_step(payload, auth_command=auth_command)
    return payload


def load_client_credentials() -> tuple[str, str]:
    config_env = load_atlassian_config_env()
    client_id = env_or_config(config_env, "GOTTA_ATLASSIAN_OAUTH_CLIENT_ID")
    client_secret = env_or_config(config_env, "GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    raise AtlassianError(
        "missing Atlassian OAuth client credentials for token refresh; "
        "set GOTTA_ATLASSIAN_OAUTH_CLIENT_ID and GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET or "
        f"configure them under {provider_env_reference('atlassian')}"
    )


def load_oauth_runtime_config() -> tuple[str, str, str, str]:
    config_env = load_atlassian_config_env()
    client_id, client_secret = load_client_credentials()
    redirect_uri = env_or_config(
        config_env,
        "GOTTA_ATLASSIAN_OAUTH_REDIRECT_URI",
        default=DEFAULT_REDIRECT_URI,
    )
    scope = env_or_config(
        config_env,
        "GOTTA_ATLASSIAN_OAUTH_SCOPE",
        default=DEFAULT_OAUTH_SCOPE,
    )
    if not redirect_uri:
        raise AtlassianError("missing Atlassian OAuth redirect URI")
    if not scope:
        raise AtlassianError("missing Atlassian OAuth scope")
    return client_id, client_secret, redirect_uri, scope


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def authorization_url(client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{ATLASSIAN_AUTHORIZE_URL}?{params}"


def start_oauth_callback_server(
    redirect_uri: str,
) -> tuple[socketserver.TCPServer, dict[str, Any]]:
    parsed = urllib.parse.urlparse(redirect_uri)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if hostname not in {"localhost", "127.0.0.1"}:
        raise AtlassianError(
            f"unsupported Atlassian OAuth redirect host for local bootstrap: {hostname}"
        )

    callback_state: dict[str, Any] = {
        "code": None,
        "state": None,
        "error": None,
        "event": threading.Event(),
    }

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "error" in params:
                callback_state["error"] = params["error"][0]
                callback_state["event"].set()
                self._send_response(
                    f"Authorization failed: {callback_state['error']}", status=400
                )
                return
            if "code" in params:
                callback_state["code"] = params["code"][0]
                callback_state["state"] = params.get("state", [""])[0]
                callback_state["event"].set()
                self._send_response("Authorization successful. You can close this window.")
                return
            self._send_response("Invalid callback: missing authorization code.", status=400)

        def _send_response(self, message: str, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                (
                    "<!doctype html><html><body><p>"
                    + message
                    + "</p><script>window.close()</script></body></html>"
                ).encode("utf-8")
            )

        def log_message(self, _format: str, *args: Any) -> None:
            return

    try:
        server = ReusableTCPServer((hostname, port), CallbackHandler)
    except OSError as exc:
        raise AtlassianError(
            f"failed to start Atlassian OAuth callback server on {redirect_uri}: {exc}"
        ) from exc
    return server, callback_state


def api_json(
    method: str, url: str, token: str, *, payload: dict[str, Any] | None = None
) -> dict[str, Any] | list[Any]:
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            if getattr(response, "status", None) in {204, 205}:
                return {}
            body = response.read()
            if not body.strip():
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise AtlassianError(f"{method} {url} returned invalid JSON: {exc}") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AtlassianError(f"{method} {url} failed with {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise AtlassianError(f"{method} {url} failed: {exc.reason}") from exc


def exchange_authorization_code(
    client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    response = api_json("POST", ATLASSIAN_TOKEN_URL, "", payload=payload)
    if not isinstance(response, dict):
        raise AtlassianError("unexpected Atlassian OAuth token exchange response")
    return response


def site_root(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/wiki")


def _dedupe_accessible_resources(
    resources: list[Any],
) -> tuple[dict[str, str], dict[str, str]]:
    deduped: dict[str, str] = {}
    ids_to_urls: dict[str, str] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        url = site_root(str(resource.get("url") or "").strip())
        cloud_id = str(resource.get("id") or "").strip()
        if not url or not cloud_id:
            continue
        deduped.setdefault(url, cloud_id)
        ids_to_urls.setdefault(cloud_id, url)
    return deduped, ids_to_urls


def resolve_accessible_resource(
    token: str,
    base_url: str,
    *,
    base_url_env: str,
    cloud_id: str = "",
) -> tuple[str, str]:
    resources = api_json("GET", ATLASSIAN_ACCESSIBLE_RESOURCES_URL, token)
    if isinstance(resources, list):
        deduped, ids_to_urls = _dedupe_accessible_resources(resources)
        if base_url:
            root = site_root(base_url)
            if root in deduped:
                return deduped[root], root
        normalized_cloud_id = cloud_id.strip()
        if normalized_cloud_id and normalized_cloud_id in ids_to_urls:
            return normalized_cloud_id, ids_to_urls[normalized_cloud_id]
        if len(deduped) == 1:
            only_url, only_cloud_id = next(iter(deduped.items()))
            return only_cloud_id, only_url
        if len(resources) == 1 and isinstance(resources[0], dict):
            only_cloud_id = str(resources[0].get("id") or "").strip()
            only_url = site_root(str(resources[0].get("url") or "").strip())
            if only_cloud_id and only_url:
                return only_cloud_id, only_url
        urls = ", ".join(sorted(deduped) or [(resource.get("url") or "?") for resource in resources])
        if base_url:
            raise AtlassianError(
                f"could not determine Atlassian cloud id for {site_root(base_url)}; "
                f"available resources: {urls}"
            )
        if normalized_cloud_id:
            raise AtlassianError(
                f"could not determine Atlassian site URL for cached cloud id {normalized_cloud_id}; "
                f"available resources: {urls}"
            )
        raise AtlassianError(
            "multiple Atlassian resources are available; set "
            f"{base_url_env} to disambiguate. available resources: {urls}"
        )
    raise AtlassianError("unexpected accessible-resources response")


def discover_cloud_id(token: str, base_url: str, *, base_url_env: str) -> str:
    cloud_id, _ = resolve_accessible_resource(
        token,
        base_url,
        base_url_env=base_url_env,
    )
    return cloud_id


def run_oauth_bootstrap(*, base_url: str = "", base_url_env: str) -> dict[str, Any]:
    if not is_interactive():
        raise AtlassianError(
            "Atlassian OAuth re-authorization requires an interactive terminal"
        )
    client_id, client_secret, redirect_uri, scope = load_oauth_runtime_config()
    state = secrets.token_urlsafe(16)
    auth_url = authorization_url(client_id, redirect_uri, scope, state)
    server, callback_state = start_oauth_callback_server(redirect_uri)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    print("Atlassian OAuth authorization is required; opening browser", file=sys.stderr)
    print(f"authorization URL: {auth_url}", file=sys.stderr)
    thread.start()
    try:
        if not webbrowser.open(auth_url):
            print(
                "failed to auto-open browser; open the authorization URL manually",
                file=sys.stderr,
            )
        if not callback_state["event"].wait(300):
            raise AtlassianError("timed out waiting for Atlassian OAuth callback")
        if callback_state["error"]:
            raise AtlassianError(
                f"Atlassian OAuth returned an error: {callback_state['error']}"
            )
        if callback_state["state"] != state:
            raise AtlassianError("OAuth state mismatch in callback")
        token_response = exchange_authorization_code(
            client_id,
            client_secret,
            redirect_uri,
            str(callback_state["code"] or ""),
        )
    finally:
        server.shutdown()
        server.server_close()

    access_token = str(token_response.get("access_token") or "").strip()
    refresh_token = str(token_response.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise AtlassianError(
            "Atlassian OAuth re-authorization did not return both access and refresh tokens"
        )
    expires_in = token_response.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = time.time() + float(expires_in)
    cloud_id, resolved_base_url = resolve_accessible_resource(
        access_token,
        base_url,
        base_url_env=base_url_env,
    )
    oauth_state = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "cloud_id": cloud_id,
        "base_url": resolved_base_url,
    }
    if expires_at is not None:
        oauth_state["expires_at"] = expires_at
    persist_oauth_state(client_id, oauth_state)
    return oauth_state


def refresh_cached_oauth_state() -> dict[str, Any]:
    oauth_state = load_cached_oauth_json()
    if not oauth_state:
        raise AtlassianError("no cached Atlassian OAuth state is available for refresh")
    refresh_token = str(oauth_state.get("refresh_token") or "").strip()
    if not refresh_token:
        raise AtlassianError("cached Atlassian OAuth state is missing a refresh token")
    client_id, client_secret = load_client_credentials()
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    response = api_json("POST", ATLASSIAN_TOKEN_URL, "", payload=payload)
    if not isinstance(response, dict):
        raise AtlassianError("unexpected Atlassian OAuth refresh response")
    access_token = str(response.get("access_token") or "").strip()
    if not access_token:
        raise AtlassianError("Atlassian OAuth refresh response did not include access_token")
    new_refresh_token = str(response.get("refresh_token") or refresh_token).strip()
    expires_in = response.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = time.time() + float(expires_in)
    oauth_state["access_token"] = access_token
    oauth_state["refresh_token"] = new_refresh_token
    if expires_at is not None:
        oauth_state["expires_at"] = expires_at
    return persist_oauth_state(client_id, oauth_state)


def is_auth_failure(exc: AtlassianError) -> bool:
    message = str(exc)
    return (
        "refresh_token is invalid" in message
        or "failed with 401" in message
        or '"code":401' in message
        or "Unauthorized" in message
    )


def token_preflight_status(token: str) -> str:
    try:
        resources = api_json("GET", ATLASSIAN_ACCESSIBLE_RESOURCES_URL, token)
    except AtlassianError as exc:
        return "invalid" if is_auth_failure(exc) else "unknown"
    return "valid" if isinstance(resources, list) else "unknown"


def load_cloud_id() -> str:
    if CLOUD_ID_FILE.exists():
        return read_text(CLOUD_ID_FILE).strip()
    return ""


def load_token(
    base_url: str = "",
    *,
    auth_command: str,
    base_url_env: str,
) -> str:
    oauth_state = load_cached_oauth_json()
    oauth_json_token = ""
    oauth_json_expires_at = None
    if oauth_state:
        oauth_json_token = str(oauth_state.get("access_token") or "").strip()
        raw_expires_at = oauth_state.get("expires_at")
        if isinstance(raw_expires_at, (int, float)):
            oauth_json_expires_at = float(raw_expires_at)
    if oauth_json_token and not token_is_expired(
        oauth_json_token, expires_at=oauth_json_expires_at
    ):
        return oauth_json_token
    if oauth_state:
        try:
            refreshed = refresh_cached_oauth_state()
        except AtlassianError as exc:
            if is_interactive() and "refresh_token is invalid" in str(exc):
                refreshed = run_oauth_bootstrap(
                    base_url=base_url,
                    base_url_env=base_url_env,
                )
            else:
                raise
        token = str(refreshed.get("access_token") or "").strip()
        if token and not token_is_expired(
            token,
            expires_at=float(refreshed["expires_at"])
            if isinstance(refreshed.get("expires_at"), (int, float))
            else None,
            skew_seconds=0,
        ):
            return token
        raise AtlassianError(
            "Atlassian OAuth refresh completed but no usable access token was stored"
        )
    if is_interactive():
        refreshed = run_oauth_bootstrap(
            base_url=base_url,
            base_url_env=base_url_env,
        )
        token = str(refreshed.get("access_token") or "").strip()
        if token:
            return token
    raise AtlassianError(
        f"missing Atlassian OAuth access token; run 'gotta {auth_command} auth' "
        f"or populate {display_path(TOKEN_FILE)}"
    )
