from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from gotta import builtin as plugin_api
from gotta import main as cli
from gotta import content, dispatch
from gotta.actor import ACTOR_ID_ENV


def test_should_materialize_respects_help_and_suppression(monkeypatch) -> None:
    assert not dispatch.should_materialize("read", ["--help"])
    assert not dispatch.should_materialize("read", ["artifact:demo.md@abc123", "--head", "20"])
    assert not dispatch.should_materialize("session", [])
    assert not dispatch.should_materialize("session", ["show"])
    assert not dispatch.should_materialize("session", ["analyze"])
    assert not dispatch.should_materialize("want", [])
    assert not dispatch.should_materialize("goal", ["--help"])
    assert not dispatch.should_materialize("gdocs", ["status"])
    assert not dispatch.should_materialize("confluence", ["search", "abc"])
    assert not dispatch.should_materialize("gdocs", ["search", "abc"])
    assert not dispatch.should_materialize("gdrive", ["search", "abc"])
    assert not dispatch.should_materialize("grafana", ["search", "--type", "dash-db"])
    assert not dispatch.should_materialize("grafana", ["search", "abc"])
    assert not dispatch.should_materialize("grafana", ["query", "--datasource", "prom-main", "sum(up)"])
    assert not dispatch.should_materialize("granola", ["search", "abc"])
    assert not dispatch.should_materialize("gsheets", ["search", "abc"])
    assert not dispatch.should_materialize("github", ["search", "abc"])
    assert not dispatch.should_materialize("jira", ["search", "abc"])
    assert not dispatch.should_materialize("slack", ["search", "abc"])
    monkeypatch.setenv(dispatch.SUPPRESS_MATERIALIZATION_ENV, "1")
    assert not dispatch.should_materialize("github", ["https://github.com/acme/widgets"])


def test_derive_preferred_name_for_delegated_read_targets() -> None:
    options = content.CommonOptions()
    cases = {
        "https://github.com/acme/widgets#readme": "widgets.md",
        "https://github.com/acme/widgets/commits/main": "widgets-commits-main.md",
        "https://github.com/acme/widgets/pull/19": "widgets-pr-19.md",
        "granola:11111111-1111-1111-1111-111111111111": "11111111-1111-1111-1111-111111111111.md",
        "github:search --type pr --repo acme/widgets ABC": "github-search-prs-acme-widgets-abc.md",
        "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Platform+Architecture+Overview": "10101.md",
        "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=20202": "20202.md",
        "https://example.atlassian.net/wiki/x/1J0AAA": "40404.md",
        "https://example.atlassian.net/browse/PROJ-3960": "PROJ-3960.md",
        "https://drive.google.com/file/d/drive-file-123/view?usp=sharing": "drive-file-123.md",
        "https://docs.google.com/document/d/1GYOXLzRlO-YmFBgtgU9xmaWsC8AQjydvUthyFnM3VqA/edit?usp=drivesdk": "1GYOXLzRlO-YmFBgtgU9xmaWsC8AQjydvUthyFnM3VqA.md",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0": "sheet-123.md",
        "https://example.slack.com/archives/C07QR8V024X/p1773085070240949": "p1773085070240949.md",
        "slack:example-workspace": "slack-workspace-example-workspace.summary",
    }

    for target, expected in cases.items():
        assert dispatch.derive_preferred_name("read", [target], options) == expected


def test_derive_preferred_name_for_provider_search_artifacts() -> None:
    options = content.CommonOptions()

    assert (
        dispatch.derive_preferred_name("confluence", ["search", "ABC"], options)
        == "confluence-search-abc.md"
    )
    assert (
        dispatch.derive_preferred_name("gdocs", ["search", "ABC"], options)
        == "gdocs-search-abc.md"
    )
    assert (
        dispatch.derive_preferred_name("gdrive", ["search", "ABC"], options)
        == "gdrive-search-abc.md"
    )
    assert (
        dispatch.derive_preferred_name("granola", ["search", "ABC"], options)
        == "granola-search-abc.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "granola",
            ["list", "--sort", "created", "--order", "asc", "--offset", "10"],
            options,
        )
        == "granola-list-created-asc-offset-10.md"
    )
    assert (
        dispatch.derive_preferred_name("gsheets", ["search", "ABC"], options)
        == "gsheets-search-abc.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "jira",
            ["search", "--limit", "25", "--output", "markdown", "ABCC OR ABCS"],
            options,
        )
        == "jira-search-abcc-or-abcs.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "slack",
            ["search", "--workspace", "example-workspace", "--source", "archive", "ABC"],
            options,
        )
        == "slack-search-example-workspace-abc.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "slack",
            ["status", "--workspace", "example-workspace", "--output", "summary"],
            options,
        )
        == "slack-workspace-example-workspace.summary"
    )
    assert (
        dispatch.derive_preferred_name(
            "grafana",
            ["search", "--type", "dash-db", "prod"],
            options,
        )
        == "grafana-search-dash-db-prod.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "grafana",
            ["search", "Production Overview"],
            options,
        )
        == "grafana-search-production-overview.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "grafana",
            ["query", "--datasource", "prom-main", "sum(up)"],
            options,
        )
        == "grafana-query-sum-up.summary"
    )


def test_derive_preferred_name_for_provider_get_artifacts_with_flags() -> None:
    options = content.CommonOptions()

    assert (
        dispatch.derive_preferred_name(
            "confluence",
            ["get", "--output", "markdown", "10101"],
            options,
        )
        == "10101.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "jira",
            ["get", "--output", "markdown", "PROJ-3960"],
            options,
        )
        == "PROJ-3960.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "gdocs",
            ["get", "--output", "markdown", "https://docs.google.com/document/d/doc-123/edit"],
            options,
        )
        == "doc-123.md"
    )
    assert (
        dispatch.derive_preferred_name(
            "slack",
            [
                "get",
                "--workspace",
                "example-workspace",
                "--output",
                "markdown",
                "https://example.slack.com/archives/C07QR8V024X/p1773085070240949",
            ],
            options,
        )
        == "p1773085070240949.md"
    )


def test_require_operational_session_accepts_initialized_session(tmp_path: Path) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path,
        content_dir=tmp_path / "content",
    )
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    dispatch.require_operational_session(dirs)


def test_require_operational_session_requires_initialized_session(
    tmp_path: Path
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path,
        content_dir=tmp_path / "content",
    )
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(content.ContentError, match="gotta"):
        dispatch.require_operational_session(dirs)


def test_canonical_locator_normalizes_common_provider_shapes() -> None:
    assert (
        dispatch.canonical_locator(
            "read",
            ["https://github.com/acme/widgets/tree/main/docs/ARCHITECTURE.md"],
        )
        == "https://github.com/acme/widgets/tree/main/docs/ARCHITECTURE.md"
    )
    assert (
        dispatch.canonical_locator(
            "read",
            ["https://github.com/acme/widgets#readme"],
        )
        == "https://github.com/acme/widgets"
    )
    assert (
        dispatch.canonical_locator(
            "granola",
            ["get", "11111111-1111-1111-1111-111111111111"],
        )
        == "granola:11111111-1111-1111-1111-111111111111"
    )
    assert (
        dispatch.canonical_locator(
            "granola",
            ["get", "Weekly Review"],
        )
        == "granola:get 'Weekly Review'"
    )
    assert (
        dispatch.canonical_locator(
            "granola",
            ["search", "--limit", "5", "latency"],
        )
        == "granola:search --limit 5 latency"
    )
    assert (
        dispatch.canonical_locator(
            "granola",
            ["transcript", "Weekly Review", "--query", "latency"],
        )
        == "granola:transcript 'Weekly Review' --query latency"
    )
    assert (
        dispatch.canonical_locator(
            "jira",
            ["get", "--output", "markdown", "https://example.atlassian.net/browse/PROJ-3960"],
        )
        == "jira:PROJ-3960"
    )
    assert (
        dispatch.canonical_locator(
            "jira",
            ["get", "https://example.atlassian.net/browse/PROJ-3960"],
        )
        == "jira:PROJ-3960"
    )
    assert (
        dispatch.canonical_locator(
            "confluence",
            [
                "get",
                "--output",
                "markdown",
                "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Page",
            ],
        )
        == "confluence:10101"
    )
    assert (
        dispatch.canonical_locator(
            "grafana",
            ["get", "demo-dashboard-uid"],
        )
        == "grafana:get demo-dashboard-uid"
    )
    assert (
        dispatch.canonical_locator(
            "confluence",
            [
                "get",
                "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Page",
            ],
        )
        == "confluence:10101"
    )
    assert (
        dispatch.canonical_locator(
            "confluence",
            [
                "get",
                "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=20202",
            ],
        )
        == "confluence:20202"
    )
    assert (
        dispatch.canonical_locator(
            "read",
            [
                "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Page"
                "?focusedCommentId=30303",
            ],
        )
        == "confluence:30303"
    )
    assert (
        dispatch.canonical_locator(
            "confluence",
            ["get", "https://example.atlassian.net/wiki/x/1J0AAA"],
        )
        == "confluence:40404"
    )
    assert (
        dispatch.canonical_locator(
            "slack",
            [
                "get",
                "--workspace",
                "example-workspace",
                "--output",
                "markdown",
                "https://example.slack.com/archives/C07QR8V024X/p1773085070240949",
            ],
        )
        == "slack:thread:C07QR8V024X:1773085070240949"
    )
    assert (
        dispatch.canonical_locator(
            "slack",
            ["get", "https://example.slack.com/archives/C07QR8V024X/p1773085070240949"],
        )
        == "slack:thread:C07QR8V024X:1773085070240949"
    )
    assert (
        dispatch.canonical_locator(
            "gdrive",
            ["get", "https://drive.google.com/file/d/drive-file-123/view?usp=sharing"],
        )
        == "gdrive:drive-file-123"
    )
    assert (
        dispatch.canonical_locator(
            "gdocs",
            ["get", "--output", "markdown", "https://docs.google.com/document/d/doc-123/edit"],
        )
        == "gdocs:doc-123"
    )
    assert (
        dispatch.canonical_locator(
            "gdocs",
            ["get", "https://docs.google.com/document/d/doc-123/edit"],
        )
        == "gdocs:doc-123"
    )
    assert (
        dispatch.canonical_locator(
            "gsheets",
            ["get", "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"],
        )
        == "gsheets:sheet-123"
    )
    assert (
        dispatch.canonical_locator(
            "read",
            ["https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"],
        )
        == "gsheets:sheet-123"
    )
    assert dispatch.canonical_locator("jira", ["search", "Architecture"]) == "jira:search Architecture"
    assert (
        dispatch.canonical_locator("jira", ["issue-types", "--project", "OPS"])
        == "jira:issue-types --project OPS"
    )
    assert (
        dispatch.canonical_locator("jira", ["sprints", "--project", "OPS"])
        == "jira:sprints --project OPS"
    )
    assert (
        dispatch.canonical_locator(
            "jira",
            ["fields", "--project", "OPS", "--type", "Service Request"],
        )
        == "jira:fields --project OPS --type 'Service Request'"
    )
    assert (
        dispatch.canonical_locator(
            "jira",
            ["add-to-sprint", "OPS-42", "--current", "--project", "OPS"],
        )
        == "jira:add-to-sprint OPS-42 --current --project OPS"
    )
    assert (
        dispatch.canonical_locator("jira", ["transitions", "OPS-42"])
        == "jira:transitions OPS-42"
    )
    assert (
        dispatch.canonical_locator("confluence", ["search", "Architecture"])
        == "confluence:search Architecture"
    )


def test_canonical_locator_normalizes_reordered_read_search_locators() -> None:
    first = dispatch.canonical_locator(
        "read",
        ["slack:search ABC reboot --workspace example-workspace --limit 10"],
    )
    second = dispatch.canonical_locator(
        "read",
        ["slack:search --workspace example-workspace --limit 10 ABC reboot"],
    )

    assert second == first
    assert first.startswith("slack:search --workspace example-workspace --limit 10 ")
    assert "ABC reboot" in first


def test_materialize_invocation_attributes_delegated_read_to_provider(tmp_path: Path) -> None:
    dirs = content.ResolvedDirs(session_dir=tmp_path, content_dir=tmp_path / "content")
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "read",
        ["jira:PROJ-1"],
        content.CommonOptions(),
        b"# PROJ-1\n\n- Created: 2026-03-10T12:00:00Z\n",
        dirs=dirs,
    )

    assert result is not None
    snapshot = next(item for item in content.scan_content_store(dirs.content_dir))
    assert snapshot.metadata["plugin"] == "jira"
    assert snapshot.metadata["entrypoint"] == "read"
    assert snapshot.metadata["provider"] == "jira"
    assert snapshot.metadata["canonical_locator"] == "jira:PROJ-1"


def test_external_plugin_can_shadow_builtin(monkeypatch) -> None:
    class Dist:
        def __init__(self, name: str) -> None:
            self.name = name

    class EntryPoint:
        def __init__(self, name: str, dist_name: str, payload) -> None:
            self.name = name
            self.dist = Dist(dist_name)
            self.value = f"{dist_name}:{name}"
            self._payload = payload

        def load(self):
            return self._payload

    builtin = plugin_api.PluginSpec(
        name="github",
        description="builtin",
        runner=lambda argv: 0,
    )
    shadow = plugin_api.PluginSpec(
        name="github",
        description="shadow",
        runner=lambda argv: 0,
    )

    plugin_api.clear_plugin_cache()
    try:
        monkeypatch.setattr(
            plugin_api,
            "entry_points",
            lambda group: [
                EntryPoint("github", "gotta", builtin),
                EntryPoint("github", "gotta-plugin-github", shadow),
            ],
        )

        discovered = plugin_api.discovered_plugins()

        assert discovered["github"].description == "shadow"
    finally:
        plugin_api.clear_plugin_cache()


def test_plugin_discovery_is_group_scoped(monkeypatch) -> None:
    class Dist:
        def __init__(self, name: str) -> None:
            self.name = name

    class EntryPoint:
        def __init__(self, name: str, dist_name: str, payload) -> None:
            self.name = name
            self.dist = Dist(dist_name)
            self.value = f"{dist_name}:{name}"
            self._payload = payload

        def load(self):
            return self._payload

    monkeypatch.setattr(
        plugin_api,
        "entry_points",
        lambda group: [
            EntryPoint(
                "ask",
                "gotta",
                plugin_api.PluginSpec(name="ask", description="", runner=lambda argv: 0),
            )
        ]
        if group == plugin_api.DEFAULT_PLUGIN_GROUP
        else [
            EntryPoint(
                "docs",
                "gotta-plugin-ask-docs",
                plugin_api.PluginSpec(name="docs", description="", runner=lambda argv: 0),
            )
        ],
    )

    plugin_api.clear_plugin_cache()
    try:
        plugins = plugin_api.available_plugins()
        assert "ask" in plugins
        assert "logs" in plugins
        assert "todo" in plugins
        assert plugin_api.available_plugins(group=plugin_api.ASK_PLUGIN_GROUP) == ["docs"]
    finally:
        plugin_api.clear_plugin_cache()


def test_builtin_core_plugins_are_available_without_reinstalled_metadata() -> None:
    plugin_api.clear_plugin_cache()
    try:
        plugins = plugin_api.available_plugins()
        assert "goal" in plugins
        assert "logs" in plugins
        assert "todo" in plugins
        assert "want" in plugins
        assert plugin_api.get_plugin("goal") is not None
        assert plugin_api.get_plugin("logs") is not None
        assert plugin_api.get_plugin("todo") is not None
        assert plugin_api.get_plugin("want") is not None
    finally:
        plugin_api.clear_plugin_cache()


def test_source_seeded_core_plugins_ignore_stale_core_metadata(monkeypatch) -> None:
    class Dist:
        def __init__(self, name: str) -> None:
            self.name = name

    class EntryPoint:
        def __init__(self, name: str, dist_name: str, payload) -> None:
            self.name = name
            self.dist = Dist(dist_name)
            self.value = f"{dist_name}:{name}"
            self._payload = payload

        def load(self):
            return self._payload

    stale = plugin_api.PluginSpec(
        name="todo",
        description="stale installed metadata",
        runner=lambda argv: 0,
    )

    monkeypatch.setattr(
        plugin_api,
        "entry_points",
        lambda group: [EntryPoint("todo", "gotta", stale)] if group == plugin_api.DEFAULT_PLUGIN_GROUP else [],
    )

    plugin_api.clear_plugin_cache()
    try:
        assert (
            plugin_api.get_plugin("logs").description
            == "inspect and mutate the canonical session execution log"
        )
        assert plugin_api.get_plugin("todo").description == "inspect and mutate the canonical session checklist"
    finally:
        plugin_api.clear_plugin_cache()


def test_broken_external_ask_entry_points_do_not_break_help_all(monkeypatch, capsys) -> None:
    class Dist:
        def __init__(self, name: str) -> None:
            self.name = name

    class EntryPoint:
        def __init__(self, name: str, dist_name: str) -> None:
            self.name = name
            self.dist = Dist(dist_name)
            self.value = f"{dist_name}:{name}"

        def load(self):
            raise ModuleNotFoundError("No module named 'gotta.ask.docs'")

    def fake_entry_points(group: str):
        if group == plugin_api.ASK_PLUGIN_GROUP:
            return [EntryPoint("docs", "gotta-plugin-ask-docs")]
        return []

    monkeypatch.setattr(plugin_api, "entry_points", fake_entry_points)

    plugin_api.clear_plugin_cache()
    try:
        assert plugin_api.available_plugins(group=plugin_api.ASK_PLUGIN_GROUP) == []
        assert cli.main(["--help-all"]) == 0
        captured = capsys.readouterr()
        output = captured.out
        error = captured.err
        assert "## gotta ask" in output
        assert "none installed" in output
        assert "warning: ignoring broken plugin entry point gotta.ask:docs" in error
        assert "ModuleNotFoundError" in error
    finally:
        plugin_api.clear_plugin_cache()


def test_cli_main_exits_zero_on_broken_pipe(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli, "available_plugins", lambda: ["read"])
    monkeypatch.setattr(cli, "_silence_stdout", lambda: calls.append("silenced"))

    def raise_broken_pipe(plugin: str, argv: list[str]) -> int:
        raise BrokenPipeError()

    monkeypatch.setattr(cli, "run_plugin", raise_broken_pipe)

    assert cli.main(["read", "README.md"]) == 0
    assert calls == ["silenced"]


def test_cli_help_all_includes_recursive_sections(capsys) -> None:
    assert cli.main(["--help-all"]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "# gotta" in output
    assert "Session synthesis surfaces live under `gotta session`" in output
    assert "This top-level long help shows only plugin root surfaces." in output
    assert "Use `gotta <plugin> --help-all` for recursive help within one plugin." in output
    assert "## gotta ask" in output
    assert "## gotta logs" in output
    assert "## gotta notes" in output
    assert "## gotta oops" in output
    assert "## gotta actor" in output
    assert "## gotta todo" in output
    assert "usage: gotta ask <surface> [args...]" in output
    assert "usage: gotta logs" in output
    assert "usage: gotta notes" in output
    assert "usage: gotta oops" in output
    assert "usage: gotta actor" in output
    assert "usage: gotta todo" in output
    assert "## gotta session" in output
    assert (
        "{init,bind,show,doctor,manifest,timeline,graph,analyze,leads}" in output
        or "{init,bind,show,doctor,manifest,timeline,graph,leads,analyze}" in output
    )
    assert "Use --help-all for recursive command help." not in output
    assert "Use --help-all for the same long-form usage output." not in output
    assert "End of top-level long help for `gotta`." in output
    assert "Plugin subtrees were intentionally omitted at this level." in output


@pytest.mark.parametrize("plugin", ["slack", "jira", "confluence", "gdocs"])
def test_cli_plugin_help_all_works_from_top_level_dispatch(
    plugin: str, capsys
) -> None:
    assert cli.main([plugin, "--help-all"]) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert f"## gotta {plugin}" in output
    assert "required: command" not in output


def test_cli_pipe_close_exits_cleanly_for_local_read_views(tmp_path: Path) -> None:
    root = tmp_path / "session-root"
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)

    large_file = tmp_path / "large.txt"
    large_file.write_text(
        "".join(f"line {index:06d} abcdefghijklmnopqrstuvwxyz\n" for index in range(200000)),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env[content.SESSION_ENV] = str(dirs.session_dir)

    proc = subprocess.Popen(
        [sys.executable, "-m", "gotta", "read", str(large_file)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    first_line = proc.stdout.readline().rstrip("\n")
    proc.stdout.close()
    stderr = proc.stderr.read()
    returncode = proc.wait()

    assert first_line == "line 000000 abcdefghijklmnopqrstuvwxyz"
    assert returncode == 0
    assert "BrokenPipeError" not in stderr

    snapshots = content.scan_content_store(dirs.content_dir)
    assert snapshots == []


def test_run_plugin_local_read_does_not_emit_stored_content_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)

    sample = tmp_path / "sample.txt"
    sample.write_text("hello\n", encoding="utf-8")

    monkeypatch.setenv(content.SESSION_ENV, str(dirs.session_dir))

    assert dispatch.run_plugin("read", [str(sample)]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert captured.out == "hello\n"


def test_materialize_invocation_carries_slack_thread_source_timestamps(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "read",
        ["https://example.slack.com/archives/C12345678/p1773085070240949"],
        content.CommonOptions(),
        b"thread body\n",
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-03-09T19:37:50.240949Z"
    assert metadata["source_updated_at"] == "2026-03-09T19:37:50.240949Z"


def test_materialize_invocation_carries_slack_channel_source_window(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "slack",
        ["get", "https://example.slack.com/archives/C12345678", "--output", "meta"],
        content.CommonOptions(),
        json.dumps(
            {
                "firstTs": "1770935417.208289",
                "lastTs": "1773347711.122509",
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-02-12T22:30:17.208289Z"
    assert metadata["source_updated_at"] == "2026-03-12T20:35:11.122509Z"


def test_materialize_invocation_extracts_slack_markdown_source_times(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "slack",
        ["get", "https://example.slack.com/archives/C12345678/p1773085070240949", "--output", "markdown"],
        content.CommonOptions(),
        (
            b"### Example thread\n\n"
            b"- _Channel_: `#ops`\n"
            b"- _Source_: https://example.slack.com/archives/C12345678/p1773085070240949\n"
            b"- Created: 2026-03-09T19:37:50.240949Z\n"
            b"- Updated: 2026-03-09T20:01:11.000000Z\n\n"
            b"- _2026-03-09 14:37:50 CST_ **Alice**: example\n"
        ),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-03-09T19:37:50.240949Z"
    assert metadata["source_updated_at"] == "2026-03-09T20:01:11.000000Z"


def test_materialize_invocation_extracts_markdown_source_times(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "jira",
        ["get", "PROJ-1", "--output", "markdown"],
        content.CommonOptions(),
        (
            b"# PROJ-1: Example\n\n"
            b"- Created: 2026-03-10T12:00:00Z\n"
            b"- Updated: 2026-03-11T09:30:00Z\n"
        ),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-03-10T12:00:00Z"
    assert metadata["source_updated_at"] == "2026-03-11T09:30:00Z"


def test_materialize_invocation_extracts_json_source_times(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "github",
        ["https://github.com/example/repo/pull/1", "--output", "json"],
        content.CommonOptions(),
        json.dumps(
            {
                "createdAt": "2026-02-01T10:00:00Z",
                "updatedAt": "2026-02-03T11:30:00Z",
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-02-01T10:00:00Z"
    assert metadata["source_updated_at"] == "2026-02-03T11:30:00Z"


def test_materialize_invocation_derives_nested_search_source_times_from_json(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "gdocs",
        ["search", "ABC", "--output", "json"],
        content.CommonOptions(),
        json.dumps(
            {
                "query": "ABC",
                "results": [
                    {
                        "title": "Older doc",
                        "createdTime": "2026-01-01T10:00:00Z",
                        "modifiedTime": "2026-01-05T12:00:00Z",
                    },
                    {
                        "title": "Newer doc",
                        "createdTime": "2026-02-01T09:00:00Z",
                        "modifiedTime": "2026-03-07T08:30:00Z",
                    },
                ],
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-01-01T10:00:00Z"
    assert metadata["source_updated_at"] == "2026-03-07T08:30:00Z"


def test_materialize_invocation_extracts_github_commit_history_markdown_source_range(
    tmp_path: Path,
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "github",
        ["https://github.com/example/repo/commits/main", "--output", "summary"],
        content.CommonOptions(),
        (
            b"# example/repo commit history for `main`\n\n"
            b"- **Created:** 2026-03-09T17:32:25Z\n"
            b"- **Updated:** 2026-03-11T02:14:24Z\n"
            b"- **Commits shown:** 14\n\n"
            b"- [abc1234](https://github.com/example/repo/commit/abc1234) Example\n"
        ),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads((dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8"))
    assert metadata["source_created_at"] == "2026-03-09T17:32:25Z"
    assert metadata["source_updated_at"] == "2026-03-11T02:14:24Z"


def test_materialize_invocation_captures_actor_actor_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    dirs = content.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(ACTOR_ID_ENV, "claude")
    monkeypatch.setenv("GOTTA_ACTOR_DIR", str(dirs.session_dir / "actors" / "claude"))

    result = dispatch._materialize_invocation(
        "read",
        ["README.md"],
        content.CommonOptions(),
        b"hello\n",
        dirs=dirs,
    )

    assert result is not None
    manifest = json.loads((dirs.content_dir / "manifest.jsonl").read_text(encoding="utf-8"))
    assert manifest["actor"] == "claude"
    assert manifest["actor_dir"].endswith("/actors/claude")


def test_github_route_prefers_markdown_for_common_github_targets() -> None:
    route_target = plugin_api.get_plugin("github").route_target
    assert route_target is not None

    assert route_target("https://github.com/acme/widgets") == ["https://github.com/acme/widgets"]
    assert route_target("https://github.com/acme/widgets#readme") == [
        "https://github.com/acme/widgets#readme"
    ]
    assert route_target("https://github.com/acme/widgets/tree/main/docs") == [
        "https://github.com/acme/widgets/tree/main/docs"
    ]
    assert route_target("https://github.com/acme/widgets/tree/main/docs#readme") == [
        "https://github.com/acme/widgets/tree/main/docs#readme"
    ]
    assert route_target("https://github.com/acme/widgets/pull/42") == [
        "https://github.com/acme/widgets/pull/42"
    ]
    assert route_target("https://github.com/acme/widgets/releases/tag/v1.2.3") == [
        "https://github.com/acme/widgets/releases/tag/v1.2.3"
    ]
    assert route_target("github:github.com/acme/widgets/blob/main/README.md") == [
        "https://github.com/acme/widgets/blob/main/README.md"
    ]
    assert route_target("github:search --type pr --repo acme/widgets ABC") == [
        "search",
        "--type",
        "pr",
        "--repo",
        "acme/widgets",
        "ABC",
    ]


def test_canonical_locator_routes_are_followable_through_read_contract() -> None:
    assert plugin_api.get_plugin("jira").route_target("jira:PROJ-3960") == ["get", "PROJ-3960"]
    assert plugin_api.get_plugin("jira").route_target("jira:status") == ["status"]
    assert plugin_api.get_plugin("jira").route_target("jira:projects") == ["projects"]
    assert plugin_api.get_plugin("granola").route_target("granola:status") == ["status"]
    assert plugin_api.get_plugin("granola").route_target("granola:list --limit 5") == [
        "list",
        "--limit",
        "5",
    ]
    assert plugin_api.get_plugin("granola").route_target(
        "granola:list --sort created --order asc --offset 10 --limit 5"
    ) == [
        "list",
        "--sort",
        "created",
        "--order",
        "asc",
        "--offset",
        "10",
        "--limit",
        "5",
    ]
    assert plugin_api.get_plugin("granola").route_target("granola:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin("granola").route_target(
        "granola:search --time-range last_30_days Architecture"
    ) == [
        "search",
        "--time-range",
        "last_30_days",
        "Architecture",
    ]
    assert plugin_api.get_plugin("granola").route_target(
        "granola:transcript 11111111-1111-1111-1111-111111111111"
    ) == ["transcript", "11111111-1111-1111-1111-111111111111"]
    assert plugin_api.get_plugin("granola").route_target(
        "granola:search-transcript --all latency"
    ) == ["search-transcript", "--all", "latency"]
    assert plugin_api.get_plugin("granola").route_target(
        "granola:11111111-1111-1111-1111-111111111111"
    ) == ["get", "11111111-1111-1111-1111-111111111111"]
    assert plugin_api.get_plugin("granola").route_target("granola:get 'Weekly Review'") == [
        "get",
        "Weekly Review",
    ]
    assert plugin_api.get_plugin("grafana").route_target("grafana:status") == ["status"]
    assert plugin_api.get_plugin("grafana").route_target("grafana:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin("grafana").route_target("grafana:get demo-dashboard-uid") == [
        "get",
        "demo-dashboard-uid",
    ]
    assert plugin_api.get_plugin("jira").route_target("jira:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin("jira").route_target("jira:sprints --project OPS") == [
        "sprints",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_plugin("jira").route_target("jira:issue-types --project OPS") == [
        "issue-types",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_plugin(
        "jira"
    ).route_target("jira:fields --project OPS --type 'Service Request'") == [
        "fields",
        "--project",
        "OPS",
        "--type",
        "Service Request",
    ]


def test_granola_route_target_rejects_invalid_locators_quietly(capsys) -> None:
    assert plugin_api.get_plugin("granola").route_target(
        "granola:search --after not-a-date latency"
    ) is None
    assert capsys.readouterr().err == ""
    assert plugin_api.get_plugin("jira").route_target("jira:transitions OPS-42") == [
        "transitions",
        "OPS-42",
    ]
    assert plugin_api.get_plugin(
        "jira"
    ).route_target("jira:add-to-sprint OPS-42 --current --project OPS") == [
        "add-to-sprint",
        "OPS-42",
        "--current",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_plugin("confluence").route_target("confluence:10101") == [
        "get",
        "10101",
    ]
    assert plugin_api.get_plugin("confluence").route_target(
        "confluence:search Architecture"
    ) == ["search", "Architecture"]
    assert plugin_api.get_plugin("gdocs").route_target("gdocs:doc-123") == ["get", "doc-123"]
    assert plugin_api.get_plugin("gdocs").route_target(
        "https://docs.google.com/document/d/doc-123/edit#heading=h.demo"
    ) == ["get", "https://docs.google.com/document/d/doc-123/edit"]
    assert plugin_api.get_plugin("gdocs").route_target("gdocs:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin(
        "gsheets"
    ).route_target("https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0") == [
        "get",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    ]
    assert plugin_api.get_plugin("gsheets").route_target("gsheets:sheet-123") == [
        "get",
        "sheet-123",
    ]
    assert plugin_api.get_plugin("gsheets").route_target("gsheets:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert (
        plugin_api.get_plugin("gdrive").route_target(
            "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"
        )
        is None
    )
    assert plugin_api.get_plugin("slack").route_target(
        "https://example.slack.com/archives/C07QR8V024X/p1773085070240949"
        "?thread_ts=1773085070.240949#replies"
    ) == [
        "get",
        "https://example.slack.com/archives/C07QR8V024X/p1773085070240949"
        "?thread_ts=1773085070.240949",
    ]
    assert plugin_api.get_plugin("gdrive").route_target("gdrive:file-123") == ["get", "file-123"]
    assert plugin_api.get_plugin("gdrive").route_target("gdrive:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin("slack").route_target(
        "slack:thread:C07QR8V024X:1773085070240949"
    ) == ["get", "C07QR8V024X:1773085070.240949"]
    assert plugin_api.get_plugin("slack").route_target("slack:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_plugin("slack").route_target(
        "slack:search --workspace demo --source archive ABC"
    ) == ["search", "--workspace", "demo", "--source", "archive", "ABC"]
    assert plugin_api.get_plugin("slack").route_target("slack:workspace:demo") == [
        "status",
        "--workspace",
        "demo",
        "--output",
        "summary",
    ]
    assert plugin_api.get_plugin("slack").route_target("slack:demo") == [
        "status",
        "--workspace",
        "demo",
        "--output",
        "summary",
    ]
