"""GitHub CLI process and payload helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ensure_gh() -> str:
    gh = shutil.which("gh")
    if gh:
        return gh
    raise RuntimeError("missing required dependency: gh")


def ensure_gh_auth(gh: str) -> None:
    status = subprocess.run([gh, "auth", "status"], check=False, capture_output=True)
    if status.returncode == 0:
        return
    if not is_interactive():
        raise RuntimeError("gh is not authenticated. run: gh auth login -w")
    print("GitHub CLI is not authenticated; starting web login...", file=sys.stderr)
    login = subprocess.run([gh, "auth", "login", "-w"], check=False)
    if login.returncode != 0:
        raise RuntimeError("gh auth login failed")


def gh_json(gh: str, args: list[str]) -> bytes:
    env = os.environ.copy()
    env["GH_PAGER"] = "cat"
    proc = subprocess.run([gh, *args], check=False, capture_output=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.decode("utf-8", errors="replace").strip() or "gh command failed"
        )
    return proc.stdout


def looks_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def gh_json_object(gh: str, args: list[str]) -> dict[str, object]:
    payload = gh_json_value(gh, args)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub CLI returned unexpected JSON payload")
    return payload


def gh_json_value(gh: str, args: list[str]) -> object:
    return json.loads(gh_json(gh, args))


def gh_status_payload() -> dict[str, object]:
    gh = shutil.which("gh")
    payload: dict[str, object] = {
        "ghPath": gh or "",
        "ghPresent": bool(gh),
        "authenticated": False,
    }
    if not gh:
        return payload
    proc = subprocess.run(
        [gh, "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    payload["authenticated"] = proc.returncode == 0
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    payload["detail"] = detail
    return payload


def default_branch_name(gh: str, *, owner: str, repo: str) -> str:
    payload = gh_json_object(gh, ["api", f"repos/{owner}/{repo}"])
    return str(payload.get("default_branch") or "").strip()
