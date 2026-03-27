#!/usr/bin/env python3
"""Durable provider configuration and readiness guidance."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import sys
from typing import Any, Callable

from gotta.config import display_path, primary_config_file
from gotta.helptext import is_long_help_request, print_long_help
from gotta.providers import slack as slack_provider


class ToolError(RuntimeError):
    """Raised when the config surface cannot satisfy a request."""


@dataclass(frozen=True)
class ConfigSurface:
    name: str
    description: str
    resolve_target: Callable[[str], str]
    inspect: Callable[[str], dict[str, Any]]
    persist: Callable[[str], None]
    auto_configure: Callable[[str], None]


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _slack_resolve_target(raw: str) -> str:
    workspace = slack_provider.workspace_from_target(raw)
    if workspace:
        return workspace
    raise ToolError(
        "unable to derive a Slack workspace from that target; pass a workspace slug "
        "like `demo`, a workspace-hosted Slack permalink/doc URL, or a generic Slack "
        "link when exactly one local workspace is unambiguous"
    )


def _slack_auto_configure(workspace: str) -> None:
    slack_provider.ensure_workspace_auth(workspace, interactive_ok=True)
    auth_state, _path = slack_provider.ensure_live_search_auth(
        workspace,
        interactive_ok=True,
    )
    slack_provider.slack_auth_test(workspace, auth_state)


SLACK_SURFACE = ConfigSurface(
    name="slack",
    description=(
        "persist one default Slack workspace, derive it from a link when needed, "
        "and inspect or complete Slack readiness"
    ),
    resolve_target=_slack_resolve_target,
    inspect=slack_provider.slack_status_payload,
    persist=slack_provider.persist_selected_workspace,
    auto_configure=_slack_auto_configure,
)

SURFACES = {SLACK_SURFACE.name: SLACK_SURFACE}


def _render_summary_text(payload: dict[str, Any]) -> str:
    lines = [
        f"config_file\t{display_path(primary_config_file())}",
        f"providers\t{','.join(sorted(SURFACES))}",
        f"next_step\trun `{slack_provider.config_command()}`",
    ]
    return "\n".join(lines)


def _render_surface_text(payload: dict[str, Any]) -> str:
    commands = payload.get("commands") or {}
    lines = [
        f"provider\t{payload.get('provider') or ''}",
        f"workspace\t{payload.get('workspace') or ''}",
        f"selected_workspace\t{payload.get('selectedWorkspace') or ''}",
        f"config_file\t{payload.get('configPathDisplay') or display_path(primary_config_file())}",
        f"persisted\t{str(bool(payload.get('persisted'))).lower()}",
        f"ready\t{str(bool(payload.get('ready'))).lower()}",
        f"slackdump_present\t{str(bool(payload.get('slackdumpPresent'))).lower()}",
        f"workspace_auth_configured\t{str(bool(payload.get('slackdumpAuthConfigured'))).lower()}",
        f"live_search_auth_configured\t{str(bool(payload.get('liveSearchAuthConfigured'))).lower()}",
        f"live_search_auth_usable\t{str(bool(payload.get('liveSearchAuthUsable'))).lower()}",
    ]
    derived_from = str(payload.get("derivedFrom") or "").strip()
    if derived_from:
        lines.append(f"derived_from\t{derived_from}")
    setup_error = str(payload.get("setupError") or "").strip()
    if setup_error:
        lines.append(f"setup_error\t{setup_error}")
    lines.append(f"next_step\t{payload.get('nextStep') or ''}")
    for key in ("config", "auth", "status", "workspaces"):
        value = str(commands.get(key) or "").strip()
        if value:
            lines.append(f"{key}_command\t{value}")
    return "\n".join(lines)


def _configure_surface(
    surface: ConfigSurface,
    *,
    target: str,
) -> tuple[dict[str, Any], int]:
    raw_target = target.strip()
    persisted = False
    auth_attempted = False
    setup_error = ""
    if raw_target:
        workspace = surface.resolve_target(raw_target)
        surface.persist(workspace)
        persisted = True
        if is_interactive():
            auth_attempted = True
            try:
                surface.auto_configure(workspace)
            except slack_provider.SlackError as exc:
                setup_error = str(exc)
    else:
        workspace = ""
    payload = dict(surface.inspect(workspace))
    payload.update(
        {
            "provider": surface.name,
            "configFile": str(primary_config_file()),
            "configPathDisplay": display_path(primary_config_file()),
            "persisted": persisted,
            "derivedFrom": raw_target,
            "authAttempted": auth_attempted,
            "setupError": setup_error,
        }
    )
    exit_code = 1 if setup_error else 0
    return payload, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta config",
        description=(
            "Persist durable provider defaults and guide exact readiness/auth setup. "
            "Prototype provider: Slack."
        ),
    )
    parser.add_argument("provider", nargs="?", choices=sorted(SURFACES))
    parser.add_argument(
        "target",
        nargs="?",
        help="provider-specific durable target, such as a workspace slug or a provider permalink",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    try:
        args = parser.parse_args(argv)
        if not args.provider:
            payload = {
                "configFile": str(primary_config_file()),
                "configPathDisplay": display_path(primary_config_file()),
                "providers": sorted(SURFACES),
                "nextStep": f"run `{slack_provider.config_command()}`",
            }
            if args.output == "json":
                print_json(payload)
            else:
                print(_render_summary_text(payload))
            return 0
        payload, exit_code = _configure_surface(
            SURFACES[args.provider], target=str(args.target or "")
        )
        if args.output == "json":
            print_json(payload)
        else:
            print(_render_surface_text(payload))
        return exit_code
    except ToolError as exc:
        return die(str(exc), code=1)
    except slack_provider.SlackError as exc:
        return die(str(exc), code=1)
