"""Shared Google OAuth and Drive helpers."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from gotta.compat import tomllib
from gotta.config import (
    config_file_candidates,
    env_or_config,
    extract_provider_env,
    primary_config_file,
    provider_env_reference,
    user_state_dir,
)

OAUTH_DIR = user_state_dir() / "auth" / "google"
TOKEN_FILE = OAUTH_DIR / "oauth.json"
CONFIG_FILE = primary_config_file()
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DOCS_API_URL = "https://docs.googleapis.com/v1/documents"
GOOGLE_DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DEFAULT_REDIRECT_URI = "http://localhost:8091/callback"
DEFAULT_SCOPE = (
    "https://www.googleapis.com/auth/documents.readonly "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/spreadsheets.readonly"
)
DEFAULT_DRIVE_FIELDS = (
    "id,name,mimeType,createdTime,modifiedTime,webViewLink,size,"
    "owners(displayName,emailAddress)"
)


class GoogleError(RuntimeError):
    """Raised when the Google surface cannot satisfy a request."""


def parse_toml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise GoogleError(f"invalid TOML config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GoogleError(f"invalid TOML config {path}: expected table")
    return data


def load_provider_config_env(*provider_names: str) -> dict[str, str]:
    for config_file in config_file_candidates():
        if not config_file.exists():
            continue
        config = parse_toml_file(config_file)
        for provider_name in provider_names:
            result = extract_provider_env(config, provider_name)
            if result:
                return result
    return {}


def load_google_config_env() -> dict[str, str]:
    return load_provider_config_env("google")


def google_credentials_present() -> bool:
    config_env = load_google_config_env()
    client_id = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_CLIENT_ID",
    )
    client_secret = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_CLIENT_SECRET",
    )
    return bool(client_id and client_secret)


def load_oauth_runtime_config() -> tuple[str, str, str, str]:
    config_env = load_google_config_env()
    client_id = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_CLIENT_ID",
    )
    client_secret = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_CLIENT_SECRET",
    )
    redirect_uri = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_REDIRECT_URI",
        default=DEFAULT_REDIRECT_URI,
    )
    scope = env_or_config(
        config_env,
        "GOTTA_GOOGLE_OAUTH_SCOPE",
        default=DEFAULT_SCOPE,
    )
    if not client_id or not client_secret:
        raise GoogleError(
            "missing Google OAuth client credentials; set "
            "GOTTA_GOOGLE_OAUTH_CLIENT_ID and GOTTA_GOOGLE_OAUTH_CLIENT_SECRET "
            "or configure them under "
            f"{provider_env_reference('google')}"
        )
    if not redirect_uri:
        raise GoogleError("missing Google OAuth redirect URI")
    if not scope:
        raise GoogleError("missing Google OAuth scope")
    return client_id, client_secret, redirect_uri, scope


def ensure_oauth_dir() -> None:
    OAUTH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OAUTH_DIR, 0o700)


def write_secret_text(path: Path, value: str) -> None:
    ensure_oauth_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    finally:
        try:
            os.chmod(path, 0o600)
        except FileNotFoundError:
            pass


def persist_oauth_state(oauth_state: dict[str, Any]) -> dict[str, Any]:
    write_secret_text(TOKEN_FILE, json.dumps(oauth_state))
    return oauth_state


def parse_json_bytes(data: bytes, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise GoogleError(f"invalid JSON from {context}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GoogleError(f"unexpected JSON payload from {context}: expected object")
    return payload


def request_bytes(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> bytes:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GoogleError(f"{method} {url} failed with {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise GoogleError(f"{method} {url} failed: {exc.reason}") from exc


def post_form_json(url: str, payload: dict[str, str], *, context: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    body = request_bytes(
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=encoded,
    )
    return parse_json_bytes(body, context=context)


def authorization_url(client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTHORIZE_URL}?{params}"


def start_oauth_callback_server(
    redirect_uri: str,
) -> tuple[socketserver.TCPServer, dict[str, Any]]:
    parsed = urllib.parse.urlparse(redirect_uri)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if hostname not in {"localhost", "127.0.0.1"}:
        raise GoogleError(
            f"unsupported Google OAuth redirect host for local bootstrap: {hostname}"
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
        raise GoogleError(
            f"failed to start Google OAuth callback server on {redirect_uri}: {exc}"
        ) from exc
    return server, callback_state


def exchange_authorization_code(
    client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    return post_form_json(
        GOOGLE_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        context="Google OAuth token exchange",
    )


def run_oauth_bootstrap(*, interactive_ok: bool) -> dict[str, Any]:
    if not interactive_ok:
        raise GoogleError("Google OAuth bootstrap requires an interactive terminal")
    client_id, client_secret, redirect_uri, scope = load_oauth_runtime_config()
    state = secrets.token_urlsafe(16)
    auth_url = authorization_url(client_id, redirect_uri, scope, state)
    server, callback_state = start_oauth_callback_server(redirect_uri)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    print("Google authorization is required; opening browser", file=sys.stderr)
    print(f"authorization URL: {auth_url}", file=sys.stderr)
    thread.start()
    try:
        if not webbrowser.open(auth_url):
            print(
                "failed to auto-open browser; open the authorization URL manually",
                file=sys.stderr,
            )
        if not callback_state["event"].wait(300):
            raise GoogleError("timed out waiting for Google OAuth callback")
        if callback_state["error"]:
            raise GoogleError(f"Google OAuth returned an error: {callback_state['error']}")
        if callback_state["state"] != state:
            raise GoogleError("OAuth state mismatch in callback")
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
    if not access_token:
        raise GoogleError("Google OAuth bootstrap did not return an access token")
    expires_in = token_response.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = time.time() + float(expires_in)
    oauth_state = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scope": token_response.get("scope") or "",
        "token_type": token_response.get("token_type") or "Bearer",
        "expires_at": expires_at,
    }
    return persist_oauth_state(oauth_state)


def load_cached_oauth_state() -> dict[str, Any] | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GoogleError(f"invalid Google OAuth state file {TOKEN_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise GoogleError(
            f"invalid Google OAuth state file {TOKEN_FILE}: expected JSON object"
        )
    return data


def google_status_payload() -> dict[str, Any]:
    config_env = load_google_config_env()
    payload: dict[str, Any] = {
        "oauthDir": str(OAUTH_DIR),
        "tokenFile": str(TOKEN_FILE),
        "tokenFileExists": TOKEN_FILE.exists(),
        "configFile": str(CONFIG_FILE),
        "configFileExists": CONFIG_FILE.exists(),
        "credentialsConfigured": google_credentials_present(),
        "credentialSources": {
            "envClientId": bool(
                env_or_config({}, "GOTTA_GOOGLE_OAUTH_CLIENT_ID")
            ),
            "envClientSecret": bool(
                env_or_config({}, "GOTTA_GOOGLE_OAUTH_CLIENT_SECRET")
            ),
            "configClientId": bool(
                env_or_config(config_env, "GOTTA_GOOGLE_OAUTH_CLIENT_ID")
            ),
            "configClientSecret": bool(
                env_or_config(config_env, "GOTTA_GOOGLE_OAUTH_CLIENT_SECRET")
            ),
        },
    }
    try:
        oauth_state = load_cached_oauth_state()
    except GoogleError as exc:
        payload["sessionStatus"] = "invalid"
        payload["error"] = str(exc)
        payload["nextStep"] = "run `gotta gdocs auth` or `gotta gdrive auth`"
        return payload
    if oauth_state is None:
        payload["sessionStatus"] = "missing"
        payload["nextStep"] = (
            "run `gotta gdocs auth` or `gotta gdrive auth` after configuring "
            f"{provider_env_reference('google')}"
            if payload["credentialsConfigured"]
            else f"configure Google OAuth credentials under {provider_env_reference('google')}"
        )
        return payload
    expires_at = oauth_state.get("expires_at")
    payload["sessionStatus"] = (
        "expired" if token_is_expired(oauth_state, skew_seconds=0) else "usable"
    )
    payload["expiresAt"] = expires_at
    payload["hasRefreshToken"] = bool(str(oauth_state.get("refresh_token") or "").strip())
    payload["scope"] = str(oauth_state.get("scope") or "")
    payload["tokenType"] = str(oauth_state.get("token_type") or "")
    if not payload["credentialsConfigured"]:
        payload["nextStep"] = (
            f"configure Google OAuth credentials under {provider_env_reference('google')}"
        )
    elif payload["sessionStatus"] in {"invalid", "missing"}:
        payload["nextStep"] = "run `gotta gdocs auth` or `gotta gdrive auth`"
    elif payload["sessionStatus"] == "expired":
        if payload["hasRefreshToken"]:
            payload["nextStep"] = (
                "rerun a native Google command first; gotta will usually refresh "
                "automatically when a refresh token is present. Re-run auth only "
                "if refresh does not recover the session"
            )
        else:
            payload["nextStep"] = "run `gotta gdocs auth` or `gotta gdrive auth`"
    else:
        payload["nextStep"] = "ready"
    return payload


def token_is_expired(oauth_state: dict[str, Any], *, skew_seconds: int = 300) -> bool:
    expires_at = oauth_state.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return True
    return time.time() + skew_seconds >= float(expires_at)


def refresh_oauth_state(oauth_state: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(oauth_state.get("refresh_token") or "").strip()
    if not refresh_token:
        raise GoogleError("cached Google OAuth state is missing a refresh token")
    client_id, client_secret, _, _ = load_oauth_runtime_config()
    response = post_form_json(
        GOOGLE_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        context="Google OAuth token refresh",
    )
    access_token = str(response.get("access_token") or "").strip()
    if not access_token:
        raise GoogleError("Google OAuth refresh did not return an access token")
    expires_in = response.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = time.time() + float(expires_in)
    oauth_state["access_token"] = access_token
    oauth_state["token_type"] = response.get("token_type") or oauth_state.get("token_type") or "Bearer"
    oauth_state["scope"] = response.get("scope") or oauth_state.get("scope") or ""
    oauth_state["expires_at"] = expires_at
    return persist_oauth_state(oauth_state)


def ensure_google_session(
    *,
    allow_bootstrap: bool,
    interactive_ok: bool,
    auth_command: str,
) -> dict[str, Any]:
    oauth_state = load_cached_oauth_state()
    if oauth_state is None:
        if allow_bootstrap and interactive_ok:
            return run_oauth_bootstrap(interactive_ok=interactive_ok)
        if google_credentials_present():
            raise GoogleError(
                f"no cached Google OAuth session is available; run 'gotta {auth_command} auth' "
                f"to create {TOKEN_FILE}"
            )
        raise GoogleError(
            f"missing Google OAuth credentials and session state; run 'gotta {auth_command} auth' "
            "after configuring GOTTA_GOOGLE_OAUTH_CLIENT_ID and GOTTA_GOOGLE_OAUTH_CLIENT_SECRET or "
            f"{provider_env_reference('google')}"
        )
    if token_is_expired(oauth_state):
        try:
            oauth_state = refresh_oauth_state(oauth_state)
        except GoogleError:
            if allow_bootstrap and interactive_ok:
                return run_oauth_bootstrap(interactive_ok=interactive_ok)
            raise
    return oauth_state


def google_json(url: str, access_token: str) -> dict[str, Any]:
    body = request_bytes(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return parse_json_bytes(body, context=url)


def google_bytes(url: str, access_token: str) -> bytes:
    return request_bytes(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
    )


def parse_doc_ref(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc == "docs.google.com":
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0] == "document" and "d" in parts:
            d_index = parts.index("d")
            if d_index + 1 < len(parts):
                doc_id = parts[d_index + 1]
                return doc_id, f"https://docs.google.com/document/d/{doc_id}/edit"
    if raw and "/" not in raw and " " not in raw:
        doc_id = raw
        return doc_id, f"https://docs.google.com/document/d/{doc_id}/edit"
    raise GoogleError(f"could not parse Google Docs document ID from input: {raw}")


def parse_drive_ref(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc == "drive.google.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
                file_id = parts[2]
                return file_id, f"https://drive.google.com/file/d/{file_id}/view"
            query = urllib.parse.parse_qs(parsed.query)
            if "id" in query and query["id"]:
                file_id = query["id"][0]
                return file_id, f"https://drive.google.com/open?id={file_id}"
        if parsed.netloc == "docs.google.com":
            parts = [part for part in parsed.path.split("/") if part]
            if "d" in parts:
                d_index = parts.index("d")
                if d_index + 1 < len(parts):
                    file_id = parts[d_index + 1]
                    return file_id, raw
    if raw and "/" not in raw and " " not in raw:
        return raw, f"https://drive.google.com/open?id={raw}"
    raise GoogleError(f"could not parse Google Drive file ID from input: {raw}")


def parse_sheet_ref(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc == "docs.google.com":
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0] == "spreadsheets" and "d" in parts:
            d_index = parts.index("d")
            if d_index + 1 < len(parts):
                sheet_id = parts[d_index + 1]
                return sheet_id, f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    if raw and "/" not in raw and " " not in raw:
        return raw, f"https://docs.google.com/spreadsheets/d/{raw}/edit"
    raise GoogleError(f"could not parse Google Sheets spreadsheet ID from input: {raw}")


def drive_file_meta(
    access_token: str,
    file_id: str,
    *,
    fields: str = DEFAULT_DRIVE_FIELDS,
) -> dict[str, Any]:
    url = (
        f"{GOOGLE_DRIVE_API_URL}/{urllib.parse.quote(file_id)}"
        f"?fields={urllib.parse.quote(fields, safe=',()')}"
    )
    return google_json(url, access_token)


def drive_export(access_token: str, file_id: str, mime_type: str) -> bytes:
    url = (
        f"{GOOGLE_DRIVE_API_URL}/{urllib.parse.quote(file_id)}/export"
        f"?mimeType={urllib.parse.quote(mime_type, safe='')}"
    )
    return google_bytes(url, access_token)


def drive_download_bytes(access_token: str, file_id: str) -> bytes:
    url = f"{GOOGLE_DRIVE_API_URL}/{urllib.parse.quote(file_id)}?alt=media"
    return google_bytes(url, access_token)


def escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def drive_search_files(access_token: str, q: str, *, limit: int, fields: str) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "q": q,
            "pageSize": str(limit),
            "orderBy": "modifiedTime desc",
            "fields": f"files({fields})",
        }
    )
    payload = google_json(f"{GOOGLE_DRIVE_API_URL}?{params}", access_token)
    files = payload.get("files")
    if not isinstance(files, list):
        raise GoogleError("unexpected Google Drive search response: missing files list")
    return [item for item in files if isinstance(item, dict)]


def sheets_spreadsheet_meta(access_token: str, spreadsheet_id: str) -> dict[str, Any]:
    url = (
        f"{GOOGLE_SHEETS_API_URL}/{urllib.parse.quote(spreadsheet_id)}"
        "?includeGridData=false&fields="
        "spreadsheetId,properties(title),sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))"
    )
    payload = google_json(url, access_token)
    payload["url"] = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return payload


def sheets_values(
    access_token: str,
    spreadsheet_id: str,
    *,
    a1_range: str,
) -> dict[str, Any]:
    encoded_range = urllib.parse.quote(a1_range, safe="!':,")
    params = urllib.parse.urlencode(
        {
            "majorDimension": "ROWS",
            "valueRenderOption": "FORMATTED_VALUE",
            "dateTimeRenderOption": "FORMATTED_STRING",
        }
    )
    url = f"{GOOGLE_SHEETS_API_URL}/{urllib.parse.quote(spreadsheet_id)}/values/{encoded_range}?{params}"
    return google_json(url, access_token)
