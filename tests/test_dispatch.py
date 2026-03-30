from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gotta import builtin as plugin_api
import gotta.content.context as content_context
import gotta.content.env as content_env
from gotta.content.filesystem import FileSystemLedgerStore
import gotta.content.model as content_model
import gotta.content.scope as content_scope
import gotta.cli.argv as cli_argv
import gotta.cli.entry as cli
import gotta.cli.notice as cli_notice
import gotta.dispatch.budget as dispatch_budget
import gotta.dispatch.main as dispatch
import gotta.dispatch.materialize as dispatch_materialize
import gotta.resolve.canon as resolve_canon
import gotta.resolve.intent as resolve_intent
import gotta.resolve.invoke as invocation
from gotta.actor import ACTOR_ID_ENV
from gotta.capture import Capture
from gotta.projection import projection_bytes
from gotta.plugins import read as read_plugin
from gotta.plugins import search as search_plugin
from gotta.plugins.session import main as session_plugin
from gotta.resolve.search import SearchRouteError, resolve_search_route


def materialize_bytes(
    data: bytes,
    *,
    dirs: content_model.ResolvedDirs,
    preferred_name: str,
    metadata: dict[str, object],
    timestamp: str | None = None,
) -> content_model.Materialization:
    return FileSystemLedgerStore.for_dirs(dirs).materialize_bytes(
        data,
        preferred_name=preferred_name,
        metadata=dict(metadata),
        timestamp=timestamp,
    )


def scan_content_store(content_dir: Path) -> list[content_model.ContentSnapshot]:
    return FileSystemLedgerStore.for_content_dir(content_dir).scan_artifacts()


def test_should_materialize_respects_help_and_suppression(monkeypatch) -> None:
    assert not dispatch.should_materialize("read", ["--help"])
    assert not dispatch.should_materialize(
        "read", ["artifact:demo.md@abc123", "--head", "20"]
    )
    assert dispatch.should_materialize(
        "read", ["https://github.com/acme/widgets", "--head", "3"]
    )
    assert dispatch.should_materialize(
        "read", ["https://example.com/manual.txt", "--tail", "5"]
    )
    assert not dispatch.should_materialize("session", [])
    assert not dispatch.should_materialize("session", ["show"])
    assert not dispatch.should_materialize("session", ["analyze"])
    assert not dispatch.should_materialize("want", [])
    assert not dispatch.should_materialize("goal", ["--help"])
    assert not dispatch.should_materialize("gdocs", ["status"])
    assert dispatch.should_materialize("confluence", ["search", "abc"])
    assert dispatch.should_materialize("confluence", ["get", "10101"])
    assert dispatch.should_materialize("gdocs", ["search", "abc"])
    assert dispatch.should_materialize("gdocs", ["get", "doc-123"])
    assert dispatch.should_materialize("gdrive", ["search", "abc"])
    assert dispatch.should_materialize("gdrive", ["get", "file-123"])
    assert dispatch.should_materialize("grafana", ["search", "--type", "dash-db"])
    assert dispatch.should_materialize("grafana", ["search", "abc"])
    assert dispatch.should_materialize("grafana", ["get", "dash-123"])
    assert not dispatch.should_materialize(
        "grafana", ["query", "--datasource", "prom-main", "sum(up)"]
    )
    assert dispatch.should_materialize("granola", ["search", "abc"])
    assert dispatch.should_materialize("granola", ["get", "note-123"])
    assert dispatch.should_materialize("gsheets", ["search", "abc"])
    assert dispatch.should_materialize("gsheets", ["get", "sheet-123"])
    assert dispatch.should_materialize("github", ["search", "abc"])
    assert dispatch.should_materialize("github", ["https://github.com/acme/widgets"])
    assert dispatch.should_materialize("jira", ["search", "abc"])
    assert dispatch.should_materialize("jira", ["get", "PROJ-1"])
    assert dispatch.should_materialize("slack", ["search", "abc"])
    assert dispatch.should_materialize("slack", ["get", "C12345678:1773085070.240949"])
    assert dispatch.should_materialize(
        "exec", ["--", sys.executable, "-c", "print('hello')"]
    )
    assert not dispatch.should_materialize("exec", ["--help"])
    monkeypatch.setenv(dispatch_materialize.SUPPRESS_MATERIALIZATION_ENV, "1")
    assert not dispatch.should_materialize(
        "github", ["https://github.com/acme/widgets"]
    )


def test_split_common_options_strips_shared_actor_target() -> None:
    options, cleaned = dispatch.split_common_options(
        [
            "search",
            "platform",
            "--session",
            "demo",
            "--actor",
            "claude",
            "--save-as",
            "x.md",
        ],
        strip_actor=True,
    )

    assert options.session_dir == "demo"
    assert options.actor == "claude"
    assert options.save_as == "x.md"
    assert cleaned == ["search", "platform"]


def test_search_plugin_spec_exposes_unary_should_materialize_contract() -> None:
    spec = plugin_api.get_surface("search")

    assert spec is not None
    assert spec.shared_actor_option is True
    assert spec.should_materialize is not None
    assert spec.should_materialize(["jira:Architecture"]) is True
    assert spec.should_materialize(["readme.md"]) is False


def test_provider_binding_declares_shared_actor_option() -> None:
    spec = plugin_api.get_surface("jira")

    assert spec is not None
    assert spec.shared_actor_option is True


def test_emit_budgeted_output_truncates_interactive_text_with_footer(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    payload = ("\n".join(f"line {index}" for index in range(400)) + "\n").encode(
        "utf-8"
    )

    emitted = dispatch.emit_budgeted_output(
        payload,
        output_format="text",
        budget_output=True,
        follow_command="gotta read artifact:demo@abc123",
    )
    captured = capsys.readouterr()

    assert emitted.output_truncated is True
    assert emitted.truncate_reason == "lines"
    assert "output truncated by lines budget" in captured.out
    assert "gotta read artifact:demo@abc123" in captured.out
    assert len(captured.out.encode("utf-8")) <= dispatch_budget.OUTPUT_EMIT_BYTE_LIMIT


def test_emit_budgeted_output_omits_overlong_follow_command_in_text_footer(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    payload = ("\n".join(f"line {index}" for index in range(400)) + "\n").encode(
        "utf-8"
    )

    dispatch.emit_budgeted_output(
        payload,
        output_format="text",
        budget_output=True,
        follow_command="gotta read " + ("x" * 500),
    )
    captured = capsys.readouterr()

    assert "output truncated by lines budget" in captured.out
    assert "follow: " not in captured.out
    assert "rerun the same command with --full-output" in captured.out


def test_emit_budgeted_output_emits_json_preview_envelope_for_interactive_json(
    capsys,
) -> None:
    payload = {"items": [{"id": index, "text": "x" * 64} for index in range(500)]}

    emitted = dispatch.emit_budgeted_output(
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        output_format="json",
        budget_output=True,
        follow_command="gotta read artifact:demo@abc123",
    )
    rendered = json.loads(capsys.readouterr().out)

    assert emitted.output_truncated is True
    assert rendered["outputTruncated"] is True
    assert rendered["truncateReason"] == "bytes"
    assert rendered["followCommand"] == "gotta read artifact:demo@abc123"


def test_emit_budgeted_output_keeps_json_preview_valid_with_long_follow_command(
    capsys,
) -> None:
    payload = {"items": [{"id": index, "text": "x" * 64} for index in range(500)]}

    emitted = dispatch.emit_budgeted_output(
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        output_format="json",
        budget_output=True,
        follow_command="x" * 20000,
    )
    raw = capsys.readouterr().out.encode("utf-8")
    rendered = json.loads(raw)

    assert emitted.output_truncated is True
    assert len(raw) <= dispatch_budget.OUTPUT_BUDGET_BYTE_LIMIT
    assert rendered["outputTruncated"] is True
    assert rendered["requestedFormat"] == "json"
    assert rendered["truncateReason"] == "bytes"
    assert "followCommand" not in rendered
    assert (
        len(rendered.get("preview", "")) <= dispatch_budget.JSON_PREVIEW_CHAR_LIMIT + 3
    )


def test_select_text_cutoff_can_use_flex_to_finish_paragraph(monkeypatch) -> None:
    monkeypatch.setattr(dispatch_budget, "OUTPUT_BUDGET_FLEX_BYTE_LIMIT", 16)

    payload = ("A" * 71 + "\n\n" + "B" * 80).encode("utf-8")
    cutoff = dispatch_budget._select_text_cutoff(payload, soft_limit=70, hard_limit=86)

    assert cutoff == 73


def test_run_surface_actor_launch_streams_live_without_buffered_capture(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def fake_runner(_argv: list[str]) -> int:
        print("launch stdout")
        print("launch stderr", file=sys.stderr, flush=True)
        return 0

    def fake_resolve_invocation(
        _plugin: str, _argv: list[str], _options: content_model.CommonOptions
    ):
        return SimpleNamespace(should_materialize=False, artifact_intent="none")

    def forbidden_capture(*_args, **_kwargs):
        raise AssertionError("actor launch should bypass buffered capture")

    monkeypatch.setattr(dispatch, "load_surface_runner", lambda _plugin: fake_runner)
    monkeypatch.setattr(dispatch, "resolve_invocation", fake_resolve_invocation)
    monkeypatch.setattr(dispatch, "session_access_mode", lambda _plugin, _argv: "none")
    monkeypatch.setattr(dispatch, "capture_stdout", forbidden_capture)
    monkeypatch.setattr(dispatch, "capture_stderr", forbidden_capture)

    assert (
        dispatch.run_surface(
            "actor", ["launch", "helper", "--session", str(tmp_path / "session")]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert "launch stdout" in captured.out
    assert "launch stderr" in captured.err


def test_session_access_mode_tracks_artifact_bearing_surfaces() -> None:
    assert dispatch.session_access_mode("jira", ["search", "platform"]) == "ambient"
    assert dispatch.session_access_mode("jira", ["status"]) == "none"
    assert dispatch.session_access_mode("confluence", ["get", "10101"]) == "ambient"
    assert (
        dispatch.session_access_mode("confluence", ["replace", "10101", "a", "b"])
        == "none"
    )
    assert dispatch.session_access_mode("read", ["README.md"]) == "ambient"
    assert dispatch.session_access_mode("search", ["jira:platform"]) == "ambient"


def test_artifact_intent_follows_surface_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        resolve_intent,
        "get_surface",
        lambda plugin: (
            SimpleNamespace(
                artifact_intent=lambda _argv: "discovery",
                should_materialize=lambda _argv: False,
            )
            if plugin == "demo"
            else None
        ),
    )

    assert resolve_intent.artifact_intent("demo", ["whatever"]) == "discovery"


def test_search_resolve_invocation_routes_provider_search_with_implicit_search() -> (
    None
):
    resolved = invocation.resolve_invocation(
        "search", ["jira:Architecture"], content_model.CommonOptions()
    )

    assert resolved.entry_plugin == "search"
    assert resolved.resolved_plugin == "jira"
    assert resolved.resolved_argv == ["search", "Architecture"]
    assert resolved.artifact_intent == "discovery"
    assert resolved.should_materialize is True


def test_search_resolve_invocation_accepts_explicit_search_alias() -> None:
    resolved = invocation.resolve_invocation(
        "search",
        ["slack:search ABC reboot"],
        content_model.CommonOptions(),
    )

    assert resolved.resolved_plugin == "slack"
    assert resolved.resolved_argv == ["search", "ABC reboot"]


def test_top_level_search_capture_requires_explicit_provider_capture_hook(
    monkeypatch,
) -> None:
    original_get_surface = plugin_api.get_surface

    monkeypatch.setattr(
        search_plugin,
        "load_surface_runner",
        lambda *_args, **_kwargs: pytest.fail("runner fallback should be dead"),
    )
    monkeypatch.setattr(
        plugin_api,
        "get_surface",
        lambda provider: (
            SimpleNamespace(capture=None, project=lambda *_args: None)
            if provider == "jira"
            else original_get_surface(provider)
        ),
    )

    with pytest.raises(
        RuntimeError,
        match=r"requires `jira` to define an explicit search capture hook",
    ):
        search_plugin.capture(["jira:Architecture"], object())


def test_top_level_search_project_requires_explicit_provider_projection_hook(
    monkeypatch,
) -> None:
    original_get_surface = plugin_api.get_surface

    monkeypatch.setattr(
        plugin_api,
        "get_surface",
        lambda provider: (
            SimpleNamespace(
                capture=lambda *_args: Capture(data=b"{}"),
                project=None,
            )
            if provider == "jira"
            else original_get_surface(provider)
        ),
    )

    with pytest.raises(
        RuntimeError,
        match=r"requires `jira` to define an explicit search projection hook",
    ):
        search_plugin.project(["jira:Architecture"], Capture(data=b"{}"))


def test_search_resolve_route_rejects_extra_unquoted_query_terms() -> None:
    with pytest.raises(
        SearchRouteError,
        match=r"top-level `gotta search` takes exactly one provider-qualified plain-text query string; quote the full `<provider>:<query>` argument or use `gotta github search \.\.\.` for structured flags",
    ):
        resolve_search_route(["github:SomeFunction", "ownership"])


def test_search_resolve_route_rejects_unknown_flag_shaped_tokens() -> None:
    with pytest.raises(
        SearchRouteError,
        match=r"top-level `gotta search` takes exactly one provider-qualified plain-text query string; quote the full `<provider>:<query>` argument or use `gotta jira search \.\.\.` for structured flags",
    ):
        resolve_search_route(["jira:retry", "--bogus", "bar"])


def test_search_resolve_route_preserves_flag_shaped_text_inside_quoted_query() -> None:
    route = resolve_search_route(["jira:retry --bogus bar"])

    assert route.provider == "jira"
    assert route.provider_argv == ["search", "retry --bogus bar"]


def test_search_resolve_route_redirects_github_get_targets_to_read() -> None:
    with pytest.raises(
        SearchRouteError,
        match=r"read-like provider targets belong on `gotta read`; use `gotta read https://github\.com/acme/widgets`",
    ):
        resolve_search_route(["github:get acme/widgets"])


def test_search_resolve_route_redirects_slack_workspace_locators_to_read() -> None:
    with pytest.raises(
        SearchRouteError,
        match=r"read-like provider targets belong on `gotta read`; use `gotta read slack:workspace:demo`",
    ):
        resolve_search_route(["slack:workspace:demo"])


def test_slack_binding_declares_default_source_metadata() -> None:
    binding = plugin_api.get_binding("slack")

    assert binding is not None
    assert binding.default_source_metadata is not None


def test_search_resolve_invocation_disables_materialization_on_invalid_target() -> None:
    resolved = invocation.resolve_invocation(
        "search", ["jira:jql project = OPS"], content_model.CommonOptions()
    )

    assert resolved.should_materialize is False
    assert resolved.artifact_intent == "none"


@pytest.mark.parametrize(
    (
        "argv",
        "expected_plugin",
        "expected_args",
        "expected_intent",
        "expected_kind",
        "expected_materialize",
    ),
    [
        (
            ["github:search platform"],
            "github",
            ["search", "platform"],
            "discovery",
            "discovery",
            True,
        ),
        (
            ["granola:list"],
            "granola",
            ["list"],
            "discovery",
            "discovery",
            True,
        ),
        (
            ["slack:workspace:demo"],
            "slack",
            ["status", "--workspace", "demo", "--output", "summary"],
            "none",
            None,
            False,
        ),
        (
            ["grafana:status"],
            "grafana",
            ["status"],
            "none",
            None,
            False,
        ),
    ],
)
def test_read_resolve_invocation_preserves_routed_provider_artifact_intent(
    argv: list[str],
    expected_plugin: str,
    expected_args: list[str],
    expected_intent: str,
    expected_kind: str | None,
    expected_materialize: bool,
) -> None:
    resolved = dispatch.resolve_invocation("read", argv, content_model.CommonOptions())

    assert resolved.resolved_plugin == expected_plugin
    assert resolved.resolved_argv == expected_args
    assert resolved.artifact_intent == expected_intent
    assert resolved.artifact_kind == expected_kind
    assert resolved.should_materialize is expected_materialize


def test_derive_preferred_name_for_delegated_read_targets() -> None:
    options = content_model.CommonOptions()
    cases = {
        "https://github.com/acme/widgets#readme": "widgets.json",
        "https://github.com/acme/widgets/commits/main": "widgets-commits-main.json",
        "https://github.com/acme/widgets/pull/19": "widgets-pr-19.json",
        "granola:11111111-1111-1111-1111-111111111111": "11111111-1111-1111-1111-111111111111.json",
        "github:search --type pr --repo acme/widgets ABC": "github-search-prs-acme-widgets-abc.json",
        "https://example.atlassian.net/wiki/spaces/ENG/pages/10101/Platform+Architecture+Overview": "10101.html",
        "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=20202": "20202.html",
        "https://example.atlassian.net/wiki/x/1J0AAA": "40404.html",
        "https://example.atlassian.net/browse/PROJ-3960": "PROJ-3960.json",
        "https://drive.google.com/file/d/drive-file-123/view?usp=sharing": "drive-file-123.bin",
        "https://docs.google.com/document/d/doc-123/edit?usp=drivesdk": "doc-123.html",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0": "sheet-123.json",
        "https://example.slack.com/archives/C12345678/p1773085070240949": "p1773085070240949.json",
        "slack:example-workspace": "slack-workspace-example-workspace.summary",
    }

    for target, expected in cases.items():
        assert dispatch.derive_preferred_name("read", [target], options) == expected


def test_derive_preferred_name_for_provider_search_artifacts() -> None:
    options = content_model.CommonOptions()

    assert (
        dispatch.derive_preferred_name("confluence", ["search", "ABC"], options)
        == "confluence-search-abc.json"
    )
    assert (
        dispatch.derive_preferred_name("gdocs", ["search", "ABC"], options)
        == "gdocs-search-abc.json"
    )
    assert (
        dispatch.derive_preferred_name("gdrive", ["search", "ABC"], options)
        == "gdrive-search-abc.json"
    )
    assert (
        dispatch.derive_preferred_name("granola", ["search", "ABC"], options)
        == "granola-search-abc.json"
    )
    assert (
        dispatch.derive_preferred_name(
            "granola",
            ["list", "--sort", "created", "--order", "asc", "--offset", "10"],
            options,
        )
        == "granola-list-created-asc-offset-10.json"
    )
    assert (
        dispatch.derive_preferred_name("gsheets", ["search", "ABC"], options)
        == "gsheets-search-abc.json"
    )
    assert (
        dispatch.derive_preferred_name(
            "jira",
            ["search", "--limit", "25", "--output", "markdown", "ABCC OR ABCS"],
            options,
        )
        == "jira-search-abcc-or-abcs.json"
    )
    assert (
        dispatch.derive_preferred_name(
            "slack",
            [
                "search",
                "--workspace",
                "example-workspace",
                "--source",
                "archive",
                "ABC",
            ],
            options,
        )
        == "slack-search-example-workspace-abc.json"
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
        == "grafana-search-dash-db-prod.json"
    )
    assert (
        dispatch.derive_preferred_name(
            "grafana",
            ["search", "Production Overview"],
            options,
        )
        == "grafana-search-production-overview.json"
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
    options = content_model.CommonOptions()

    assert (
        dispatch.derive_preferred_name(
            "confluence",
            ["get", "--output", "markdown", "10101"],
            options,
        )
        == "10101.html"
    )
    assert (
        dispatch.derive_preferred_name(
            "jira",
            ["get", "--output", "markdown", "PROJ-3960"],
            options,
        )
        == "PROJ-3960.json"
    )
    assert (
        dispatch.derive_preferred_name(
            "gdocs",
            [
                "get",
                "--output",
                "markdown",
                "https://docs.google.com/document/d/doc-123/edit",
            ],
            options,
        )
        == "doc-123.html"
    )
    assert (
        dispatch.derive_preferred_name(
            "gdrive",
            ["get", "--output", "markdown", "file-123"],
            options,
        )
        == "file-123.bin"
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
                "https://example.slack.com/archives/C12345678/p1773085070240949",
            ],
            options,
        )
        == "p1773085070240949.json"
    )


def test_root_help_exposes_session_aware_read_storage_contract(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(content_scope, "DEFAULT_SESSION_ROOT", tmp_path / "session")

    assert cli.main(["--help"]) == 0

    output = capsys.readouterr().err
    assert (
        "acquire one target through the right retrieval surface with session-aware storage"
        in output
    )


def test_require_operational_session_accepts_initialized_session(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path,
        content_dir=tmp_path / "content",
    )
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content_env.write_state_env(dirs)
    dispatch.require_operational_session(dirs)


def test_require_operational_session_requires_initialized_session(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path,
        content_dir=tmp_path / "content",
    )
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(content_model.ContentError, match="gotta"):
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
            [
                "get",
                "--output",
                "markdown",
                "https://example.atlassian.net/browse/PROJ-3960",
            ],
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
            "confluence",
            ["get", "https://example.atlassian.net/wiki/x/GoD9AgE"],
        )
        == "confluence:4345135130"
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
                "https://example.slack.com/archives/C12345678/p1773085070240949",
            ],
        )
        == "slack:thread:C12345678:1773085070240949"
    )
    assert (
        dispatch.canonical_locator(
            "slack",
            ["get", "https://example.slack.com/archives/C12345678/p1773085070240949"],
        )
        == "slack:thread:C12345678:1773085070240949"
    )
    assert (
        dispatch.canonical_locator(
            "slack",
            ["get", "https://example.slack.com/docs/T12345678/F12345678"],
        )
        == "slack:doc:T12345678:F12345678"
    )
    assert (
        dispatch.canonical_locator(
            "read",
            ["https://example.slack.com/docs/T12345678/F12345678"],
        )
        == "slack:doc:T12345678:F12345678"
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
            [
                "get",
                "--output",
                "markdown",
                "https://docs.google.com/document/d/doc-123/edit",
            ],
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
    assert (
        dispatch.canonical_locator(
            "read",
            [
                "https://github.com/acme/widgets/actions/runs/123456789/job/987654321#summary"
            ],
        )
        == "https://github.com/acme/widgets/actions/runs/123456789/job/987654321"
    )
    assert (
        dispatch.canonical_locator("jira", ["search", "Architecture"])
        == "jira:search Architecture"
    )
    assert (
        dispatch.canonical_locator("jira", ["issue-types", "--project", "OPS"])
        == "jira:issue-types --project OPS"
    )
    assert (
        dispatch.canonical_locator(
            "jira",
            ["projects", "--limit", "25", "--offset", "25"],
        )
        == "jira:projects --limit 25 --offset 25"
    )
    assert (
        dispatch.canonical_locator("jira", ["sprints", "--project", "OPS"])
        == "jira:sprints --project OPS"
    )
    assert (
        dispatch.canonical_locator(
            "grafana",
            ["datasources", "--limit", "25", "--offset", "25"],
        )
        == "grafana:datasources --limit 25 --offset 25"
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


def test_canonical_locator_falls_back_to_generic_surface_free_shape(
    monkeypatch,
) -> None:
    monkeypatch.setattr(resolve_canon, "get_surface", lambda _plugin: None)

    assert (
        resolve_canon.canonical_locator("demo", ["subcommand", "--limit", "5", "abc"])
        == "demo:subcommand --limit 5 abc"
    )


def test_exec_canonical_locator_tracks_cwd_and_command(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    expected_command = shlex.join([sys.executable, "-c", "print('hello')"])

    locator = dispatch.canonical_locator(
        "exec",
        ["--", sys.executable, "-c", "print('hello')"],
    )

    assert locator == f"exec:{tmp_path.resolve()}::{expected_command}"


def test_materialize_invocation_attributes_delegated_read_to_provider(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path, content_dir=tmp_path / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "read",
        ["jira:PROJ-1"],
        content_model.CommonOptions(),
        b"# PROJ-1\n\n- Created: 2026-03-10T12:00:00Z\n",
        dirs=dirs,
    )

    assert result is not None
    snapshot = next(item for item in scan_content_store(dirs.content_dir))
    assert snapshot.artifact.metadata["plugin"] == "jira"
    assert snapshot.artifact.metadata["entrypoint"] == "read"
    assert snapshot.artifact.metadata["provider"] == "jira"
    assert snapshot.artifact.metadata["canonical_locator"] == "jira:PROJ-1"


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

    builtin = plugin_api.SurfaceSpec(
        name="github",
        description="builtin",
        runner=lambda argv: 0,
    )
    shadow = plugin_api.SurfaceSpec(
        name="github",
        description="shadow",
        runner=lambda argv: 0,
    )

    plugin_api.clear_binding_cache()
    try:
        monkeypatch.setattr(
            plugin_api,
            "entry_points",
            lambda group: [
                EntryPoint("github", "gotta", builtin),
                EntryPoint("github", "gotta-plugin-github", shadow),
            ],
        )

        discovered = plugin_api.discovered_surfaces()

        assert discovered["github"].description == "shadow"
    finally:
        plugin_api.clear_binding_cache()


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
        lambda group: (
            [
                EntryPoint(
                    "ask",
                    "gotta",
                    plugin_api.SurfaceSpec(
                        name="ask", description="", runner=lambda argv: 0
                    ),
                )
            ]
            if group == plugin_api.DEFAULT_BINDING_GROUP
            else [
                EntryPoint(
                    "docs",
                    "gotta-plugin-ask-docs",
                    plugin_api.SurfaceSpec(
                        name="docs", description="", runner=lambda argv: 0
                    ),
                )
            ]
        ),
    )

    plugin_api.clear_binding_cache()
    try:
        plugins = plugin_api.available_surfaces()
        assert "ask" in plugins
        assert "logs" in plugins
        assert "todo" in plugins
        assert plugin_api.available_surfaces(group=plugin_api.ASK_BINDING_GROUP) == [
            "docs"
        ]
    finally:
        plugin_api.clear_binding_cache()


def test_builtin_core_plugins_are_available_without_reinstalled_metadata() -> None:
    plugin_api.clear_binding_cache()
    try:
        plugins = plugin_api.available_surfaces()
        assert "goal" in plugins
        assert "logs" in plugins
        assert "todo" in plugins
        assert "want" in plugins
        assert plugin_api.get_surface("goal") is not None
        assert plugin_api.get_surface("logs") is not None
        assert plugin_api.get_surface("todo") is not None
        assert plugin_api.get_surface("want") is not None
    finally:
        plugin_api.clear_binding_cache()


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

    stale = plugin_api.SurfaceSpec(
        name="todo",
        description="stale installed metadata",
        runner=lambda argv: 0,
    )

    monkeypatch.setattr(
        plugin_api,
        "entry_points",
        lambda group: (
            [EntryPoint("todo", "gotta", stale)]
            if group == plugin_api.DEFAULT_BINDING_GROUP
            else []
        ),
    )

    plugin_api.clear_binding_cache()
    try:
        assert (
            plugin_api.get_surface("logs").description
            == "inspect and mutate the canonical session procedural trace"
        )
        assert (
            plugin_api.get_surface("todo").description
            == "inspect and mutate the canonical session checklist"
        )
    finally:
        plugin_api.clear_binding_cache()


def test_broken_external_ask_entry_points_do_not_break_help_all(
    monkeypatch, capsys
) -> None:
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
        if group == plugin_api.ASK_BINDING_GROUP:
            return [EntryPoint("docs", "gotta-plugin-ask-docs")]
        return []

    monkeypatch.setattr(plugin_api, "entry_points", fake_entry_points)

    plugin_api.clear_binding_cache()
    try:
        assert plugin_api.available_surfaces(group=plugin_api.ASK_BINDING_GROUP) == []
        assert cli.main(["--help-all"]) == 0
        captured = capsys.readouterr()
        output = captured.out
        error = captured.err
        assert "## gotta ask" in output
        assert "none installed" in output
        assert "warning: ignoring broken plugin entry point gotta.ask:docs" in error
        assert "ModuleNotFoundError" in error
    finally:
        plugin_api.clear_binding_cache()


def test_cli_main_exits_zero_on_broken_pipe(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli_argv, "available_surfaces", lambda: ["read"])
    monkeypatch.setattr(cli_notice, "_silence_stdout", lambda: calls.append("silenced"))

    def raise_broken_pipe(plugin: str, argv: list[str]) -> int:
        raise BrokenPipeError()

    monkeypatch.setattr(cli_argv, "run_surface", raise_broken_pipe)

    assert cli.main(["read", "README.md"]) == 0
    assert calls == ["silenced"]


def test_cli_help_all_includes_recursive_sections(capsys) -> None:
    assert cli.main(["--help-all"]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "# gotta" in output
    assert "Session synthesis surfaces live under `gotta session`" in output
    assert "`manifest`, `timeline`, `graph`, `leads`, `analyze`, `scan`" in output
    assert "This top-level long help shows only top-level surface bindings." in output
    assert (
        "Use `gotta <surface> --help-all` for recursive help within one surface."
        in output
    )
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
    assert "scan" in output
    assert "Use --help-all for recursive command help." not in output
    assert "Use --help-all for the same long-form usage output." not in output
    assert "End of top-level long help for `gotta`." in output
    assert "Nested surface trees were intentionally omitted at this level." in output


@pytest.mark.parametrize("plugin", ["slack", "jira", "confluence", "gdocs"])
def test_cli_plugin_help_all_works_from_top_level_dispatch(plugin: str, capsys) -> None:
    assert cli.main([plugin, "--help-all"]) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert f"## gotta {plugin}" in output
    assert "required: command" not in output


def test_cli_pipe_close_exits_cleanly_for_local_read_views(tmp_path: Path) -> None:
    root = tmp_path / "session-root"
    dirs = content_model.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content_env.write_state_env(dirs)

    large_file = tmp_path / "large.txt"
    large_file.write_text(
        "".join(
            f"line {index:06d} abcdefghijklmnopqrstuvwxyz\n" for index in range(200000)
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env[content_env.SESSION_ENV] = str(dirs.session_dir)

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

    snapshots = scan_content_store(dirs.content_dir)
    assert snapshots == []


def test_run_surface_local_read_does_not_emit_stored_content_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    dirs = content_model.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content_env.write_state_env(dirs)

    sample = tmp_path / "sample.txt"
    sample.write_text("hello\n", encoding="utf-8")

    monkeypatch.setenv(content_env.SESSION_ENV, str(dirs.session_dir))

    assert dispatch.run_surface("read", [str(sample)]) == 0
    captured = capsys.readouterr()

    assert captured.out == "hello\n"
    assert captured.err == ""


def test_emit_budgeted_output_skips_default_budget_with_full_output_escape(
    capsys,
) -> None:
    payload = ("\n".join(f"line {index}" for index in range(400)) + "\n").encode(
        "utf-8"
    )

    emitted = dispatch.emit_budgeted_output(
        payload,
        output_format="text",
        budget_output=False,
        follow_command="gotta read artifact:demo@abc123",
    )
    captured = capsys.readouterr()

    assert emitted.output_budget_applied is False
    assert emitted.output_truncated is False
    assert captured.out == payload.decode("utf-8")


def test_run_surface_read_section_miss_returns_clean_error_for_local_view(
    tmp_path: Path, capsys
) -> None:
    sample = tmp_path / "sample.md"
    sample.write_text("# Intro\n\nbody\n", encoding="utf-8")

    assert dispatch.run_surface("read", [str(sample), "--section", "Missing"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "no section heading matched: Missing" in captured.err
    assert "Traceback" not in captured.err


def test_run_surface_materializing_read_section_miss_returns_clean_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    monkeypatch.setattr(
        read_plugin,
        "fetch_url",
        lambda _target: (b"# Intro\n\nbody\n", "text/markdown", False),
    )

    assert (
        dispatch.run_surface(
            "read",
            [
                "https://example.com/doc.md",
                "--section",
                "Missing",
                "--session",
                str(local_root),
            ],
        )
        == 1
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "no section heading matched: Missing" in captured.err
    assert "Traceback" not in captured.err
    assert scan_content_store(local_root / "content") == []


def test_run_surface_session_scan_invalid_regex_fails_even_when_manifest_is_empty(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    assert (
        dispatch.run_surface(
            "session",
            ["scan", "[", "--match", "regex", "--session", str(local_root)],
        )
        == 1
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "invalid scan pattern:" in captured.err
    assert "Traceback" not in captured.err


def test_run_surface_read_invalid_confluence_shortlink_returns_clean_error(
    capsys,
) -> None:
    assert (
        dispatch.run_surface("read", ["https://example.atlassian.net/wiki/x/!!!!!"])
        == 1
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "could not parse Confluence page ID from input" in captured.err
    assert "Traceback" not in captured.err


def test_materialize_invocation_carries_slack_thread_source_timestamps(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "read",
        ["https://example.slack.com/archives/C12345678/p1773085070240949"],
        content_model.CommonOptions(),
        b"thread body\n",
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-03-09T19:37:50.240949Z"
    assert metadata["source_updated_at"] == "2026-03-09T19:37:50.240949Z"


def test_materialize_invocation_carries_slack_channel_source_window(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "slack",
        ["get", "https://example.slack.com/archives/C12345678", "--output", "meta"],
        content_model.CommonOptions(),
        json.dumps(
            {
                "firstTs": "1770935417.208289",
                "lastTs": "1773347711.122509",
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-02-12T22:30:17.208289Z"
    assert metadata["source_updated_at"] == "2026-03-12T20:35:11.122509Z"


def test_materialize_invocation_extracts_slack_markdown_source_times(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "slack",
        [
            "get",
            "https://example.slack.com/archives/C12345678/p1773085070240949",
            "--output",
            "markdown",
        ],
        content_model.CommonOptions(),
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
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-03-09T19:37:50.240949Z"
    assert metadata["source_updated_at"] == "2026-03-09T20:01:11.000000Z"


def test_materialize_invocation_extracts_markdown_source_times(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "jira",
        ["get", "PROJ-1", "--output", "markdown"],
        content_model.CommonOptions(),
        (
            b"# PROJ-1: Example\n\n"
            b"- Created: 2026-03-10T12:00:00Z\n"
            b"- Updated: 2026-03-11T09:30:00Z\n"
        ),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-03-10T12:00:00Z"
    assert metadata["source_updated_at"] == "2026-03-11T09:30:00Z"


def test_materialize_invocation_extracts_json_source_times(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "github",
        ["https://github.com/example/repo/pull/1", "--output", "json"],
        content_model.CommonOptions(),
        json.dumps(
            {
                "createdAt": "2026-02-01T10:00:00Z",
                "updatedAt": "2026-02-03T11:30:00Z",
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-02-01T10:00:00Z"
    assert metadata["source_updated_at"] == "2026-02-03T11:30:00Z"


def test_materialize_invocation_persists_visibility_metadata(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "github",
        ["https://github.com/example/repo", "--output", "json"],
        content_model.CommonOptions(),
        json.dumps(
            {
                "name": "repo",
                "url": "https://github.com/example/repo",
                "visibility": "private",
                "createdAt": "2026-02-01T10:00:00Z",
            }
        ).encode("utf-8"),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (dirs.content_dir / "manifest.jsonl").read_text(encoding="utf-8")
    )

    assert metadata["visibility_level"] == "restricted"
    assert metadata["visibility_boundary"] == "same_company"
    assert metadata["visibility_confidence"] == "high"
    assert metadata["visibility_basis"] == [
        "provider=github",
        "repo.visibility=private",
    ]
    assert manifest["visibility_level"] == "restricted"
    assert manifest["visibility_boundary"] == "same_company"
    assert manifest["visibility_confidence"] == "high"
    assert manifest["visibility_basis"] == [
        "provider=github",
        "repo.visibility=private",
    ]


def test_materialize_invocation_extracts_visibility_from_markdown(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "slack",
        [
            "get",
            "https://example.slack.com/archives/C12345678/p1773085070240949",
            "--output",
            "markdown",
        ],
        content_model.CommonOptions(),
        (
            b"### Slack Thread: Example\n\n"
            b"- _Channel_: `#ops`\n"
            b"- _Source_: https://example.slack.com/archives/C12345678/p1773085070240949\n"
            b"- Visibility: internal (same_company, high)\n"
            b"- Created: 2026-03-09T19:37:50.240949Z\n"
        ),
        dirs=dirs,
    )

    assert result is not None
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["visibility_level"] == "internal"
    assert metadata["visibility_boundary"] == "same_company"
    assert metadata["visibility_confidence"] == "high"


def test_materialize_invocation_derives_nested_search_source_times_from_json(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "gdocs",
        ["search", "ABC", "--output", "json"],
        content_model.CommonOptions(),
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
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-01-01T10:00:00Z"
    assert metadata["source_updated_at"] == "2026-03-07T08:30:00Z"


def test_materialize_invocation_extracts_github_commit_history_markdown_source_range(
    tmp_path: Path,
) -> None:
    dirs = content_model.ResolvedDirs(
        session_dir=tmp_path / "session-root",
        content_dir=tmp_path / "session-root" / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)

    result = dispatch._materialize_invocation(
        "github",
        ["https://github.com/example/repo/commits/main", "--output", "summary"],
        content_model.CommonOptions(),
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
    metadata = json.loads(
        (dirs.content_dir / result.digest / "meta.json").read_text(encoding="utf-8")
    )
    assert metadata["source_created_at"] == "2026-03-09T17:32:25Z"
    assert metadata["source_updated_at"] == "2026-03-11T02:14:24Z"


def test_materialize_invocation_captures_actor_actor_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    dirs = content_model.ResolvedDirs(
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
        content_model.CommonOptions(),
        b"hello\n",
        dirs=dirs,
    )

    assert result is not None
    manifest = json.loads(
        (dirs.content_dir / "manifest.jsonl").read_text(encoding="utf-8")
    )
    assert manifest["actor"] == "claude"
    assert manifest["actor_dir"].endswith("/actors/claude")


def test_materialize_invocation_rejects_unbound_actor_shell(
    tmp_path: Path, monkeypatch
) -> None:
    shared_root = tmp_path / "session-root"
    dirs = content_model.ResolvedDirs(
        session_dir=shared_root / "actors" / "claude",
        content_dir=shared_root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content_env.write_state_env(dirs)
    monkeypatch.delenv(ACTOR_ID_ENV, raising=False)
    monkeypatch.delenv(content_env.SESSION_ACTOR_ENV, raising=False)
    monkeypatch.delenv("GOTTA_ACTOR_SPEAKER", raising=False)
    monkeypatch.setattr(
        content_context,
        "current_context_binding",
        lambda: type("Binding", (), {"binding_id": ""})(),
    )

    with pytest.raises(content_model.ContentError) as excinfo:
        dispatch._materialize_invocation(
            "read",
            ["README.md"],
            content_model.CommonOptions(),
            b"hello\n",
            dirs=dirs,
        )

    assert "bind and launch a sibling actor" in str(excinfo.value)
    assert not (dirs.content_dir / "manifest.jsonl").exists()


def test_run_surface_materializes_full_bytes_for_bounded_routed_read(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    canonical = b'{"kind":"repo","title":"Title","lines":["line 1","line 2"]}'

    def fake_capture(argv: list[str], _options: object) -> Capture:
        return Capture(
            data=canonical,
            preferred_name="widgets.json",
            content_type="application/json",
        )

    def fake_project(argv: list[str], capture: Capture):
        assert capture.data == canonical
        return projection_bytes(b"# Title\n\nline 1\nline 2\n")

    monkeypatch.setattr(
        read_plugin,
        "get_surface",
        lambda name: (
            SimpleNamespace(capture=fake_capture, project=fake_project)
            if name == "github"
            else plugin_api.get_surface(name)
        ),
    )

    assert (
        dispatch.run_surface(
            "read",
            [
                "https://github.com/acme/widgets",
                "--head",
                "3",
                "--session",
                str(local_root),
            ],
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == "# Title\n\nline 1\n"

    snapshots = scan_content_store(local_root / "content")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert (
        snapshot.artifact.metadata["canonical_locator"]
        == "https://github.com/acme/widgets"
    )
    assert snapshot.artifact.metadata["content_type"] == "application/json"
    assert snapshot.layout.blob_path.read_bytes() == canonical


def test_run_surface_exec_materializes_canonical_execution_evidence(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    assert (
        dispatch.run_surface(
            "exec",
            [
                "--session",
                str(local_root),
                "--",
                sys.executable,
                "-c",
                "print('hello from exec')",
            ],
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "command:" in captured.out
    assert "hello from exec" in captured.out

    snapshots = scan_content_store(local_root / "content")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.artifact.artifact_kind == "evidence"
    assert snapshot.artifact.metadata["plugin"] == "exec"
    assert snapshot.artifact.metadata["content_type"] == "application/json"
    assert snapshot.artifact.metadata["source_kind"] == "exec"
    assert snapshot.artifact.metadata["visibility_level"] == "personal"
    payload = json.loads(snapshot.layout.blob_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "exec"
    assert payload["argv"][0] == sys.executable
    assert payload["exit_status"] == 0
    assert payload["stdout"]["text"] == "hello from exec\n"
    assert payload["stderr"]["text"] == ""


def test_run_surface_exec_nonzero_materializes_and_returns_exit_status(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    assert (
        dispatch.run_surface(
            "exec",
            [
                "--session",
                str(local_root),
                "--",
                sys.executable,
                "-c",
                "import sys; print('bad'); print('boom', file=sys.stderr); raise SystemExit(7)",
            ],
        )
        == 7
    )
    captured = capsys.readouterr()
    assert "bad" in captured.out
    assert "boom" in captured.out

    snapshots = scan_content_store(local_root / "content")
    assert len(snapshots) == 1
    payload = json.loads(snapshots[0].layout.blob_path.read_text(encoding="utf-8"))
    assert payload["exit_status"] == 7
    assert payload["stderr"]["text"] == "boom\n"


def test_repeated_bounded_and_unbounded_read_share_one_canonical_snapshot(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    assert session_plugin.main(["init", "--session", str(local_root)]) == 0
    capsys.readouterr()

    canonical = b'{"kind":"repo","title":"Title","lines":["line 1","line 2"]}'

    def fake_capture(argv: list[str], _options: object) -> Capture:
        return Capture(
            data=canonical,
            preferred_name="widgets.json",
            content_type="application/json",
        )

    def fake_project(argv: list[str], capture: Capture):
        assert capture.data == canonical
        return projection_bytes(b"# Title\n\nline 1\nline 2\n")

    monkeypatch.setattr(
        read_plugin,
        "get_surface",
        lambda name: (
            SimpleNamespace(capture=fake_capture, project=fake_project)
            if name == "github"
            else plugin_api.get_surface(name)
        ),
    )

    assert (
        dispatch.run_surface(
            "read",
            [
                "https://github.com/acme/widgets",
                "--head",
                "3",
                "--session",
                str(local_root),
            ],
        )
        == 0
    )
    capsys.readouterr()
    assert (
        dispatch.run_surface(
            "read", ["https://github.com/acme/widgets", "--session", str(local_root)]
        )
        == 0
    )
    capsys.readouterr()

    snapshots = scan_content_store(local_root / "content")
    assert len(snapshots) == 1
    assert (
        snapshots[0].artifact.metadata["canonical_locator"]
        == "https://github.com/acme/widgets"
    )

    assert (
        session_plugin.main(
            ["manifest", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1
    assert payload["fetchRecordCount"] == 2
    assert (
        payload["entries"][0]["canonical_locator"] == "https://github.com/acme/widgets"
    )
    assert payload["entries"][0]["fetchCount"] == 2


def test_github_route_prefers_markdown_for_common_github_targets() -> None:
    route_target = plugin_api.get_surface("github").route_target
    assert route_target is not None

    assert route_target("https://github.com/acme/widgets") == [
        "https://github.com/acme/widgets"
    ]
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
    assert route_target("https://github.com/acme/widgets/actions/runs/123456789") == [
        "https://github.com/acme/widgets/actions/runs/123456789"
    ]
    assert route_target(
        "https://github.com/acme/widgets/actions/runs/123456789/job/987654321"
    ) == ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"]
    assert route_target("github:github.com/acme/widgets/blob/main/README.md") == [
        "https://github.com/acme/widgets/blob/main/README.md"
    ]
    assert route_target(
        "github:github.com/acme/widgets/actions/runs/123456789/job/987654321"
    ) == ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"]
    assert route_target("github:search --type pr --repo acme/widgets ABC") == [
        "search",
        "--type",
        "pr",
        "--repo",
        "acme/widgets",
        "ABC",
    ]


def test_canonical_locator_routes_are_followable_through_read_contract() -> None:
    assert plugin_api.get_surface("jira").route_target("jira:PROJ-3960") == [
        "get",
        "PROJ-3960",
    ]
    assert plugin_api.get_surface("jira").route_target("jira:status") == ["status"]
    assert plugin_api.get_surface("jira").route_target("jira:projects") == ["projects"]
    assert plugin_api.get_surface("granola").route_target("granola:status") == [
        "status"
    ]
    assert plugin_api.get_surface("granola").route_target("granola:list --limit 5") == [
        "list",
        "--limit",
        "5",
    ]
    assert plugin_api.get_surface("granola").route_target(
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
    assert plugin_api.get_surface("granola").route_target(
        "granola:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("granola").route_target(
        "granola:search --time-range last_30_days Architecture"
    ) == [
        "search",
        "--time-range",
        "last_30_days",
        "Architecture",
    ]
    assert plugin_api.get_surface("granola").route_target(
        "granola:transcript 11111111-1111-1111-1111-111111111111"
    ) == ["transcript", "11111111-1111-1111-1111-111111111111"]
    assert plugin_api.get_surface("granola").route_target(
        "granola:search-transcript --all latency"
    ) == ["search-transcript", "--all", "latency"]
    assert plugin_api.get_surface("granola").route_target(
        "granola:11111111-1111-1111-1111-111111111111"
    ) == ["get", "11111111-1111-1111-1111-111111111111"]
    assert plugin_api.get_surface("granola").route_target(
        "granola:get 'Weekly Review'"
    ) == [
        "get",
        "Weekly Review",
    ]
    assert plugin_api.get_surface("grafana").route_target("grafana:status") == [
        "status"
    ]
    assert plugin_api.get_surface("grafana").route_target(
        "grafana:datasources --limit 25 --offset 25"
    ) == [
        "datasources",
        "--limit",
        "25",
        "--offset",
        "25",
    ]
    assert plugin_api.get_surface("grafana").route_target(
        "grafana:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("grafana").route_target(
        "grafana:get demo-dashboard-uid"
    ) == [
        "get",
        "demo-dashboard-uid",
    ]
    assert plugin_api.get_surface("jira").route_target("jira:search Architecture") == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("jira").route_target(
        "jira:projects --limit 25 --offset 25"
    ) == [
        "projects",
        "--limit",
        "25",
        "--offset",
        "25",
    ]
    assert plugin_api.get_surface("jira").route_target(
        "jira:sprints --project OPS"
    ) == [
        "sprints",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_surface("jira").route_target(
        "jira:issue-types --project OPS"
    ) == [
        "issue-types",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_surface("jira").route_target(
        "jira:fields --project OPS --type 'Service Request'"
    ) == [
        "fields",
        "--project",
        "OPS",
        "--type",
        "Service Request",
    ]


def test_granola_route_target_rejects_invalid_locators_quietly(capsys) -> None:
    assert (
        plugin_api.get_surface("granola").route_target(
            "granola:search --after not-a-date latency"
        )
        is None
    )
    assert capsys.readouterr().err == ""
    assert plugin_api.get_surface("jira").route_target("jira:transitions OPS-42") == [
        "transitions",
        "OPS-42",
    ]
    assert plugin_api.get_surface("jira").route_target(
        "jira:add-to-sprint OPS-42 --current --project OPS"
    ) == [
        "add-to-sprint",
        "OPS-42",
        "--current",
        "--project",
        "OPS",
    ]
    assert plugin_api.get_surface("confluence").route_target("confluence:10101") == [
        "get",
        "10101",
    ]
    assert plugin_api.get_surface("confluence").route_target(
        "confluence:search Architecture"
    ) == ["search", "Architecture"]
    assert plugin_api.get_surface("gdocs").route_target("gdocs:doc-123") == [
        "get",
        "doc-123",
    ]
    assert plugin_api.get_surface("gdocs").route_target(
        "https://docs.google.com/document/d/doc-123/edit#heading=h.demo"
    ) == ["get", "https://docs.google.com/document/d/doc-123/edit"]
    assert plugin_api.get_surface("gdocs").route_target(
        "gdocs:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("gsheets").route_target(
        "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"
    ) == [
        "get",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    ]
    assert plugin_api.get_surface("gsheets").route_target("gsheets:sheet-123") == [
        "get",
        "sheet-123",
    ]
    assert plugin_api.get_surface("gsheets").route_target(
        "gsheets:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert (
        plugin_api.get_surface("gdrive").route_target(
            "https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"
        )
        is None
    )
    assert plugin_api.get_surface("slack").route_target(
        "https://example.slack.com/archives/C12345678/p1773085070240949"
        "?thread_ts=1773085070.240949#replies"
    ) == [
        "get",
        "https://example.slack.com/archives/C12345678/p1773085070240949"
        "?thread_ts=1773085070.240949",
    ]
    assert plugin_api.get_surface("gdrive").route_target("gdrive:file-123") == [
        "get",
        "file-123",
    ]
    assert plugin_api.get_surface("gdrive").route_target(
        "gdrive:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("slack").route_target(
        "slack:thread:C12345678:1773085070240949"
    ) == ["get", "C12345678:1773085070.240949"]
    assert plugin_api.get_surface("slack").route_target(
        "https://example.slack.com/docs/T12345678/F12345678#fragment"
    ) == ["get", "https://example.slack.com/docs/T12345678/F12345678"]
    assert plugin_api.get_surface("slack").route_target(
        "slack:doc:T12345678:F12345678"
    ) == [
        "get",
        "slack:doc:T12345678:F12345678",
    ]
    assert plugin_api.get_surface("slack").route_target(
        "slack:search Architecture"
    ) == [
        "search",
        "Architecture",
    ]
    assert plugin_api.get_surface("slack").route_target(
        "slack:search --workspace demo --source archive ABC"
    ) == ["search", "--workspace", "demo", "--source", "archive", "ABC"]
    assert plugin_api.get_surface("slack").route_target("slack:workspace:demo") == [
        "status",
        "--workspace",
        "demo",
        "--output",
        "summary",
    ]
    assert plugin_api.get_surface("slack").route_target("slack:demo") == [
        "status",
        "--workspace",
        "demo",
        "--output",
        "summary",
    ]
