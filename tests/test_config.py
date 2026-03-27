from __future__ import annotations

import json
from pathlib import Path

from gotta.config import load_config
from gotta.plugins import config as config_plugin
from gotta.plugins import slack
from gotta.providers import slack as slack_provider


def test_config_slack_persists_workspace_from_permalink_without_auth_when_noninteractive(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    permalink = "https://demo.slack.com/archives/C12345678/p1773085070240949"

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_plugin, "is_interactive", lambda: False)
    monkeypatch.setattr(slack_provider.shutil, "which", lambda _cmd: "")

    assert config_plugin.main(["slack", permalink, "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["provider"] == "slack"
    assert payload["workspace"] == "demo"
    assert payload["persisted"] is True
    assert payload["derivedFrom"] == permalink
    assert (
        payload["nextStep"]
        == "install slackdump, then run `gotta config slack demo` in an interactive terminal"
    )
    assert load_config()["providers"]["slack"]["env"]["GOTTA_SLACK_WORKSPACE"] == "demo"


def test_config_slack_generic_app_link_uses_unique_known_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    app_link = "https://app.slack.com/client/T12345678/C12345678"

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_plugin, "is_interactive", lambda: False)
    monkeypatch.setattr(slack_provider.shutil, "which", lambda _cmd: "/usr/bin/slackdump")
    monkeypatch.setattr(slack_provider, "known_workspaces", lambda: ["demo"])

    assert config_plugin.main(["slack", app_link, "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"] == "demo"
    assert payload["persisted"] is True
    assert payload["derivedFrom"] == app_link
    assert load_config()["providers"]["slack"]["env"]["GOTTA_SLACK_WORKSPACE"] == "demo"


def test_config_slack_rejects_generic_app_link_without_unambiguous_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    app_link = "https://app.slack.com/client/T12345678/C12345678"

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_plugin, "is_interactive", lambda: False)
    monkeypatch.setattr(slack_provider, "known_workspaces", lambda: ["demo", "prod"])

    assert config_plugin.main(["slack", app_link, "--output", "json"]) == 1
    captured = capsys.readouterr()

    assert "unable to derive a Slack workspace from that target" in captured.err
    assert "generic Slack link when exactly one local workspace is unambiguous" in captured.err
    assert load_config() == {}


def test_config_slack_interactive_target_attempts_auth_after_persist(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    calls: list[tuple[str, str, bool]] = []

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_plugin, "is_interactive", lambda: True)
    monkeypatch.setattr(
        slack_provider,
        "ensure_workspace_auth",
        lambda workspace, interactive_ok: calls.append(
            ("workspace", workspace, interactive_ok)
        ),
    )
    monkeypatch.setattr(
        slack_provider,
        "ensure_live_search_auth",
        lambda workspace, interactive_ok: (
            calls.append(("live", workspace, interactive_ok))
            or (
                {"token": "xoxp-demo", "cookies": [{"name": "d", "value": "cookie"}]},
                tmp_path / "demo.json",
            )
        ),
    )
    monkeypatch.setattr(
        slack_provider,
        "slack_auth_test",
        lambda workspace, auth_state: calls.append(("test", workspace, True)) or {},
    )
    monkeypatch.setattr(
        slack_provider,
        "slack_status_payload",
        lambda workspace="": {
            "workspace": workspace,
            "selectedWorkspace": workspace,
            "knownWorkspaces": [workspace],
            "slackdumpPath": "/usr/bin/slackdump",
            "slackdumpPresent": True,
            "authConfigured": True,
            "slackdumpAuthConfigured": True,
            "liveSearchAuthConfigured": True,
            "liveSearchAuthUsable": True,
            "liveSearchAuthPath": str(tmp_path / "demo.json"),
            "configFile": str(config_file),
            "configPathDisplay": str(config_file),
            "ready": True,
            "nextStep": "ready",
            "commands": {
                "config": "gotta config slack demo",
                "auth": "gotta slack auth --workspace demo",
                "status": "gotta slack status --workspace demo",
                "workspaces": "gotta slack workspaces",
            },
            "configReference": f"[providers.slack.env] in {config_file}",
        },
    )

    assert config_plugin.main(["slack", "demo", "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"] == "demo"
    assert payload["persisted"] is True
    assert payload["authAttempted"] is True
    assert calls == [
        ("workspace", "demo", True),
        ("live", "demo", True),
        ("test", "demo", True),
    ]
    assert load_config()["providers"]["slack"]["env"]["GOTTA_SLACK_WORKSPACE"] == "demo"


def test_config_slack_interactive_setup_failure_returns_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config_plugin, "is_interactive", lambda: True)
    monkeypatch.setattr(
        slack_provider,
        "ensure_workspace_auth",
        lambda workspace, interactive_ok: (_ for _ in ()).throw(
            slack_provider.SlackError("auth failed")
        ),
    )
    monkeypatch.setattr(
        slack_provider,
        "slack_status_payload",
        lambda workspace="": {
            "workspace": workspace,
            "selectedWorkspace": workspace,
            "knownWorkspaces": [workspace],
            "slackdumpPath": "/usr/bin/slackdump",
            "slackdumpPresent": True,
            "authConfigured": False,
            "slackdumpAuthConfigured": False,
            "liveSearchAuthConfigured": False,
            "liveSearchAuthUsable": False,
            "liveSearchAuthPath": "",
            "configFile": str(config_file),
            "configPathDisplay": str(config_file),
            "ready": False,
            "nextStep": f"run `gotta config slack {workspace}` in an interactive terminal",
            "commands": {
                "config": f"gotta config slack {workspace}",
                "auth": f"gotta slack auth --workspace {workspace}",
                "status": f"gotta slack status --workspace {workspace}",
                "workspaces": "gotta slack workspaces",
            },
            "configReference": f"[providers.slack.env] in {config_file}",
        },
    )

    assert config_plugin.main(["slack", "demo", "--output", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"] == "demo"
    assert payload["persisted"] is True
    assert payload["authAttempted"] is True
    assert payload["setupError"] == "auth failed"
    assert load_config()["providers"]["slack"]["env"]["GOTTA_SLACK_WORKSPACE"] == "demo"


def test_slack_auth_does_not_persist_default_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    auth_file = tmp_path / "demo-auth.json"

    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(slack, "ensure_workspace_auth", lambda workspace, interactive_ok: None)
    monkeypatch.setattr(
        slack,
        "export_slack_auth_from_slackdump",
        lambda workspace: {"token": "xoxp-demo", "cookies": [{"name": "d", "value": "cookie"}]},
    )
    monkeypatch.setattr(slack, "persist_slack_auth_state", lambda workspace, payload: auth_file)
    monkeypatch.setattr(slack, "slack_auth_test", lambda workspace, auth_state: {})
    monkeypatch.setattr(slack, "known_workspaces", lambda: ["demo"])

    assert slack.main(["auth", "--workspace", "demo"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"] == "demo"
    assert load_config() == {}


def test_slack_status_and_missing_workspace_guidance_point_to_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slack_provider, "known_workspaces", lambda: ["demo"])
    monkeypatch.setattr(slack_provider.shutil, "which", lambda _cmd: "/usr/bin/slackdump")
    monkeypatch.setattr(slack_provider, "default_workspace", lambda: "")
    monkeypatch.setattr(slack_provider, "slack_auth_path", lambda workspace: tmp_path / f"{workspace}.json")
    monkeypatch.setattr(slack, "workspace_archive_result", lambda workspace: None)
    monkeypatch.setattr(slack, "directory_db_path", lambda workspace: tmp_path / "_directory.sqlite")

    assert "gotta config slack demo" in slack_provider.missing_workspace_message()

    assert slack.main(["status", "--workspace", "demo", "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert "gotta config slack demo" in payload["nextStep"]
    assert payload["commands"]["config"] == "gotta config slack demo"
