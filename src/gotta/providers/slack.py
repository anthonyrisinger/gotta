"""Shared Slack workspace, auth, and live API helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from gotta.config import (
    env_or_config,
    extract_provider_env,
    load_config,
    set_provider_env_values,
    user_state_dir,
)

DEFAULT_WORKSPACE = ""
SLACK_WORKSPACE_ENV = "GOTTA_SLACK_WORKSPACE"
WORKSPACE_LIST_RE = re.compile(
    r"(?m)^\s*(?:=>\s+)?(?P<workspace>[A-Za-z0-9._-]+)\s+\(file:"
)
SLACK_AUTH_DIR = user_state_dir() / "auth" / "slack"


class SlackError(RuntimeError):
    """Raised when the Slack surface cannot satisfy a request."""


def _iso_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_command(
    args: list[str],
    *,
    check: bool = True,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SlackError(f"{' '.join(args)} timed out") from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise SlackError(f"{' '.join(args)} failed: {detail}")
    return proc


def load_slack_config_env() -> dict[str, str]:
    return extract_provider_env(load_config(), "slack")


def default_workspace() -> str:
    config_env = load_slack_config_env()
    return env_or_config(config_env, SLACK_WORKSPACE_ENV, default=DEFAULT_WORKSPACE).strip()


def known_workspaces() -> list[str]:
    if not shutil.which("slackdump"):
        return []
    proc = _run_command(["slackdump", "workspace", "list"], check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    workspaces = [match.group("workspace") for match in WORKSPACE_LIST_RE.finditer(output)]
    return sorted(dict.fromkeys(workspaces))


def persist_selected_workspace(workspace: str) -> None:
    selected = workspace.strip()
    if not selected:
        return
    set_provider_env_values("slack", {SLACK_WORKSPACE_ENV: selected})


def resolve_workspace(workspace: str) -> str:
    selected = workspace.strip() or default_workspace()
    if selected:
        return selected
    known = known_workspaces()
    return known[0] if len(known) == 1 else ""


def missing_workspace_message() -> str:
    known = known_workspaces()
    if len(known) == 1:
        return (
            f"missing Slack workspace; pass --workspace {known[0]}, "
            f"set {SLACK_WORKSPACE_ENV}, or run `gotta slack auth --workspace {known[0]}`"
        )
    if known:
        return (
            f"missing Slack workspace; pass --workspace or set {SLACK_WORKSPACE_ENV}. "
            f"Known workspaces: {', '.join(known)}"
        )
    return (
        f"missing Slack workspace; pass --workspace, set {SLACK_WORKSPACE_ENV}, "
        "or run `gotta slack auth --workspace <workspace>`"
    )


def ensure_slackdump() -> None:
    if shutil.which("slackdump"):
        return
    raise SlackError("missing required dependency: slackdump")


def _ensure_slack_auth_dir() -> None:
    SLACK_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SLACK_AUTH_DIR, 0o700)


def slack_auth_path(workspace: str) -> Path:
    return SLACK_AUTH_DIR / f"{workspace}.json"


def _write_secret_json(path: Path, payload: dict[str, Any]) -> Path:
    _ensure_slack_auth_dir()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        try:
            os.chmod(path, 0o600)
        except FileNotFoundError:
            pass
    return path


def load_slack_auth_state(workspace: str) -> dict[str, Any] | None:
    path = slack_auth_path(workspace)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlackError(f"invalid Slack auth state {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SlackError(f"invalid Slack auth state {path}: expected JSON object")
    token = str(payload.get("token") or "").strip()
    cookies = payload.get("cookies")
    if not token or not isinstance(cookies, list):
        raise SlackError(
            f"invalid Slack auth state {path}: missing token or cookies. "
            f"rerun `gotta slack auth --workspace {workspace}`."
        )
    return payload


def _current_slackdump_workspace() -> str:
    ensure_slackdump()
    proc = _run_command(["slackdump", "workspace", "list", "-b"], check=False)
    output = (proc.stdout or "").splitlines()
    for line in output:
        text = line.strip()
        if text.startswith("*"):
            return text[1:].strip()
    return ""


def _parse_netscape_cookie(line: str) -> dict[str, Any]:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 7:
        raise SlackError(f"invalid Slack cookie export line: {line!r}")
    domain, _flag, path, secure, expires, name, value = parts
    try:
        expires_at = int(expires)
    except ValueError as exc:
        raise SlackError(f"invalid Slack cookie expiry in export: {line!r}") from exc
    return {
        "domain": domain,
        "path": path,
        "secure": secure.upper() == "TRUE",
        "expires": expires_at,
        "name": name,
        "value": value,
    }


def parse_slackdump_auth_export(text: str) -> dict[str, Any]:
    token = ""
    cookies: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not token and line.startswith("TOKEN="):
            token = line.removeprefix("TOKEN=").strip()
            continue
        cookies.append(_parse_netscape_cookie(raw_line))
    if not token:
        raise SlackError("slackdump auth export did not include a Slack token")
    if not cookies:
        raise SlackError("slackdump auth export did not include any Slack cookies")
    if not any(cookie.get("name") == "d" for cookie in cookies):
        raise SlackError(
            "slackdump auth export did not include the required Slack `d` cookie"
        )
    return {"token": token, "cookies": cookies}


def _select_slackdump_workspace(workspace: str) -> None:
    _run_command(["slackdump", "workspace", "select", workspace])


def export_slack_auth_from_slackdump(workspace: str) -> dict[str, Any]:
    ensure_slackdump()
    previous = _current_slackdump_workspace()
    if previous and previous != workspace:
        _select_slackdump_workspace(workspace)
    elif not previous:
        _select_slackdump_workspace(workspace)
    try:
        try:
            proc = subprocess.run(
                ["slackdump", "tools", "info", "--auth"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=None,
                stdin=None,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlackError(
                "Slack auth export did not complete in time. Rerun "
                f"`gotta slack auth --workspace {workspace}` in an interactive terminal."
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stdout or "").strip() or f"exit code {proc.returncode}"
            raise SlackError(
                "Slack auth export failed. Rerun "
                f"`gotta slack auth --workspace {workspace}` in an interactive terminal. "
                f"detail: {detail}"
            )
        payload = parse_slackdump_auth_export(proc.stdout or "")
        payload.update(
            {
                "workspace": workspace,
                "source": "slackdump-tools-info",
                "exportedAt": _iso_now(),
            }
        )
        return payload
    finally:
        if previous and previous != workspace:
            try:
                _select_slackdump_workspace(previous)
            except SlackError:
                pass


def persist_slack_auth_state(workspace: str, payload: dict[str, Any]) -> Path:
    return _write_secret_json(slack_auth_path(workspace), payload)


def cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if str(cookie.get("name") or "").strip()
    )


def slack_api_post(
    workspace: str,
    auth_state: dict[str, Any],
    method: str,
    *,
    data: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    token = str(auth_state.get("token") or "").strip()
    cookies = auth_state.get("cookies")
    if not token or not isinstance(cookies, list):
        raise SlackError(
            f"missing Slack live-search auth for {workspace}. run "
            f"`gotta slack auth --workspace {workspace}`."
        )
    encoded = urllib.parse.urlencode({"token": token, **data}).encode("utf-8")
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=encoded,
        method="POST",
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Cookie", cookie_header(cookies))
    request.add_header("User-Agent", "gotta/slack")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "").strip()
            raise SlackError(
                "Slack live search is rate limited"
                + (f"; retry after {retry_after}s" if retry_after else "")
                + ". Retry later or use `gotta slack search --source archive ...`."
            ) from exc
        body = exc.read().decode("utf-8", errors="replace")
        raise SlackError(f"Slack live search failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SlackError(f"Slack live search failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise SlackError(f"invalid Slack {method} response: expected JSON object")
    if payload.get("ok") is False:
        error = str(payload.get("error") or "unknown_error")
        if error == "invalid_auth":
            raise SlackError(
                f"Slack live-search auth is invalid for {workspace}. Rerun "
                f"`gotta slack auth --workspace {workspace}`."
            )
        raise SlackError(
            f"Slack {method} failed for {workspace}: {error}. "
            f"Retry or use `gotta slack search --source archive ...`."
        )
    return payload


def slack_auth_test(workspace: str, auth_state: dict[str, Any]) -> dict[str, Any]:
    return slack_api_post(workspace, auth_state, "auth.test", data={}, timeout_seconds=10)


def _workspace_exists(workspace: str) -> bool:
    return workspace in known_workspaces()


def ensure_workspace_auth(workspace: str, *, interactive_ok: bool) -> None:
    workspace = resolve_workspace(workspace)
    if not workspace:
        raise SlackError(missing_workspace_message())
    ensure_slackdump()
    if _workspace_exists(workspace):
        return
    if not interactive_ok:
        raise SlackError(
            f"no Slack auth for {workspace}. run: gotta slack auth --workspace {workspace}"
        )
    proc = subprocess.run(
        ["slackdump", "workspace", "new", workspace],
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not _workspace_exists(workspace):
        raise SlackError(
            f"failed to authenticate Slack workspace {workspace}. rerun "
            f"'gotta slack auth --workspace {workspace}' in an interactive terminal."
        )
    persist_selected_workspace(workspace)


def ensure_live_search_auth(
    workspace: str,
    *,
    interactive_ok: bool,
) -> tuple[dict[str, Any], Path]:
    auth_state = load_slack_auth_state(workspace)
    path = slack_auth_path(workspace)
    if auth_state is not None:
        return auth_state, path
    if not interactive_ok:
        raise SlackError(
            f"missing gotta-owned Slack live-search auth for {workspace}. run "
            f"`gotta slack auth --workspace {workspace}`."
        )
    ensure_workspace_auth(workspace, interactive_ok=True)
    auth_state = export_slack_auth_from_slackdump(workspace)
    path = persist_slack_auth_state(workspace, auth_state)
    return auth_state, path
