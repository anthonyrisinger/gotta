from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from gotta.actors import ACTOR_SPEAKER_ENV
from gotta import builtin
from gotta import content
from gotta import main as cli
from gotta import session as sessionlib
from gotta.plugins import github
from gotta.plugins import jira


def _set_default_session_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", root)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", root)


def _grouped_root(registry: Path, context_id: str, *, identity: str | None = None) -> Path:
    fingerprint = cli._session_token(context_id)
    return registry / fingerprint / "actors" / (identity or fingerprint)


def _actor_id(shared_root: Path, actor_ref: str) -> str:
    return sessionlib._resolve_bound_actor_name(shared_root, actor_ref)


def test_main_rejects_unknown_plugin(capsys) -> None:
    assert cli.main(["nonsense"]) == 2
    assert "unknown gotta plugin: nonsense" in capsys.readouterr().err


def test_main_creates_and_reuses_context_bound_session_for_write_surfaces(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str, str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(
            (
                list(argv or []),
                os.environ["GOTTA_SESSION_DIR"],
                os.environ["GOTTA_CONTEXT_ID"],
            )
        )
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["todo", "append", "first task"]) == 0
    first_err = capsys.readouterr().err
    session_root = Path(seen[-1][1])
    assert session_root.parent.name == "actors"
    assert session_root.parent.parent == (tmp_path / "session" / cli._session_token("thread-123"))
    assert session_root.name == cli._session_token("thread-123")
    assert (session_root / "state" / "env").exists()
    assert (session_root / "content").is_dir()
    assert (session_root / "content").is_symlink()
    assert os.readlink(session_root / "content") == "../../content"
    assert not (session_root / "session").exists()
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()
    assert (session_root / "TODO.md").is_file()
    assert (session_root / "LOGS.md").is_file()
    assert (session_root / "OOPS.md").is_file()
    assert "created a new gotta session" in first_err

    assert cli.main(["todo", "append", "second task"]) == 0
    second_err = capsys.readouterr().err
    assert seen[-1][1] == str(session_root)
    assert "created a new gotta session" not in second_err
    assert second_err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["jira", "status"],
        ["slack", "status"],
        ["grafana", "status"],
        ["confluence", "search", "platform"],
        ["gdocs", "search", "platform"],
        ["gdrive", "search", "platform"],
        ["grafana", "datasources"],
        ["grafana", "search", "--type", "dash-db"],
        ["grafana", "search", "platform"],
        ["grafana", "query", "--datasource", "prom-main", "sum(up)"],
        ["granola", "search", "platform"],
        ["gsheets", "search", "platform"],
        ["github", "search", "platform"],
        ["jira", "search", "platform"],
        ["slack", "search", "platform"],
    ],
)
def test_main_provider_status_surfaces_do_not_create_session(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 0

    captured = capsys.readouterr()
    assert seen == [(argv, "")]
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


def test_main_read_retrieval_runs_sessionless_without_creating_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["read", "https://example.com/manual.txt"]) == 0

    captured = capsys.readouterr()
    assert seen == [(["read", "https://example.com/manual.txt"], "")]
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


def test_main_ambient_provider_search_materializes_discovery_in_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    def fake_github_main(argv: list[str]) -> int:
        print("# Search Results\n\n- one\n")
        return 0

    monkeypatch.setattr(github, "main", fake_github_main)

    assert cli.main(["github", "search", "platform"]) == 0
    captured = capsys.readouterr()

    assert "stored discovery artifact:" in captured.err
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "discovery"
    assert entries[-1]["actor"] == cli._session_token("thread-123")


def test_main_read_routed_provider_search_preserves_discovery_artifact_kind(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    def fake_github_main(argv: list[str]) -> int:
        print("# Search Results\n\n- one\n")
        return 0

    monkeypatch.setattr(github, "main", fake_github_main)

    assert cli.main(["read", "github:search platform"]) == 0
    captured = capsys.readouterr()

    assert "stored discovery artifact:" in captured.err
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "discovery"


def test_main_ambient_provider_get_honors_explicit_actor_attribution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()

    def fake_github_main(argv: list[str]) -> int:
        print("# Repo\n\nmain body\n")
        return 0

    monkeypatch.setattr(github, "main", fake_github_main)

    assert (
        cli.main(
            [
                "github",
                "https://github.com/acme/widgets",
                "--session",
                "demo",
                "--actor",
                "claude",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert "stored evidence artifact:" in captured.err
    shared_root = registry / "demo"
    claude = _actor_id(shared_root, "claude")
    manifest_path = shared_root / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "evidence"
    assert entries[-1]["actor"] == claude


def test_main_rejects_foreign_actor_attributed_materialization(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "foreign-123")

    def fake_github_main(argv: list[str]) -> int:
        print("# Repo\n\nmain body\n")
        return 0

    monkeypatch.setattr(github, "main", fake_github_main)

    assert (
        cli.main(
            [
                "github",
                "https://github.com/acme/widgets",
                "--session",
                "demo",
                "--actor",
                "claude",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()

    assert "bind and launch a sibling actor" in captured.err
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    assert not manifest_path.exists()


def test_main_dispatches_direct_plugin_args_inside_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append((list(argv or []), os.environ["GOTTA_SESSION_DIR"]))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["todo", "append", "real task"]) == 0

    session_root = Path(seen[0][1])
    assert seen == [(["todo", "append", "real task"], str(session_root))]
    assert (session_root / "state" / "env").exists()
    assert "created a new gotta session" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["oops"],
        ["logs"],
        ["todo"],
        ["want"],
        ["goal"],
        ["session", "show"],
        ["actor", "status"],
    ],
)
def test_main_read_only_session_surfaces_without_bound_session_fail_without_creating_session(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append(list(inner_argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert "this command requires an existing session" in captured.err
    assert seen == []
    assert not (tmp_path / "session").exists()


def test_main_preserves_session_subcommands(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "init"]) == 0

    assert seen == [["session", "init"]]


def test_main_session_init_creates_scaffolded_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "init"]) == 0

    session_root = _grouped_root(registry, "thread-123")
    assert capsys.readouterr().out.strip() == str(session_root.resolve())
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()
    assert (session_root / "TODO.md").is_file()
    assert (session_root / "LOGS.md").is_file()
    assert (session_root / "OOPS.md").is_file()


def test_main_preserves_read_option_ordering(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["read", "--head", "120", "README.md"]) == 0
    assert cli.main(["read", "--help"]) == 0

    assert seen == [["read", "--head", "120", "README.md"], ["read", "--help"]]


def test_main_preserves_todo_subcommands(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["todo", "append", "real task"]) == 0

    assert seen == [["todo", "append", "real task"]]


def test_main_help_paths_do_not_create_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)

    assert cli.main(["--help"]) == 0
    assert cli.main(["--help-all"]) == 0
    assert cli.main(["help"]) == 0

    assert seen == [["--help"], ["--help-all"], ["--help-all"]]
    assert not (tmp_path / "session").exists()
    assert capsys.readouterr().err == ""


def test_main_help_and_bare_gotta_are_discoverable_without_creating_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")

    assert cli.main(["--help"]) == 0
    help_err = capsys.readouterr().err
    assert "usage: gotta <plugin> [args...]" in help_err
    assert "canonical session-binding path: `gotta ...`" in help_err
    assert not (tmp_path / "session").exists()

    assert cli.main([]) == 0
    bare_err = capsys.readouterr().err
    assert "usage: gotta <plugin> [args...]" in bare_err
    assert not (tmp_path / "session").exists()


def test_main_session_show_and_doctor_require_an_existing_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "show"]) == 2
    first_err = capsys.readouterr().err
    assert "this command requires an existing session" in first_err
    assert not registry.exists()

    assert cli.main(["session", "bind"]) == 0
    capsys.readouterr()

    assert cli.main(["session", "show"]) == 0
    show_output = capsys.readouterr().out
    session_root = _grouped_root(registry, "thread-123")
    assert show_output.strip() == str(session_root.resolve())
    assert (session_root / "state" / "env").exists()
    assert (session_root / "content").is_dir()
    assert (session_root / "TODO.md").is_file()

    assert cli.main(["session", "doctor"]) == 0
    doctor_output = json.loads(capsys.readouterr().out)
    assert doctor_output["session"]["sessionRoot"] == str(session_root.resolve())
    assert doctor_output["session"]["initialized"] is True
    assert doctor_output["runtime"]["contextId"] == "thread-123"
    assert doctor_output["runtime"]["contextSource"] == "codex_thread"
    assert doctor_output["checks"]["durableBindingsPresent"]["status"] == "ok"
    assert doctor_output["checks"]["runtimeBindingMatchesTarget"]["status"] == "ok"
    assert doctor_output["bindings"][0]["contextId"] == "thread-123"


def test_main_cross_actor_note_append_preserves_acting_actor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()

    assert cli.main(["notes", "append", "cross-actor note", "--actor", "claude"]) == 0
    capsys.readouterr()

    fingerprint = cli._session_token("thread-123")
    claude = _actor_id(registry / "demo", "claude")
    claude_root = registry / "demo" / "actors" / claude
    notes_records = [
        json.loads(line)
        for line in (claude_root / "state" / "notes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert notes_records[-1]["actor"] == claude
    assert notes_records[-1]["author"] == fingerprint

    activity_records = [
        json.loads(line)
        for line in (claude_root / "state" / "activity.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert activity_records[-1]["action"] == "append"
    assert activity_records[-1]["actor"] == fingerprint
    assert activity_records[-1]["target_actor"] == claude


@pytest.mark.parametrize(
    ("argv", "requires_claude"),
    [
        (["oops"], False),
        (["logs"], False),
        (["todo"], False),
        (["want"], False),
        (["goal"], False),
        (["notes", "show", "--actor", "claude"], True),
    ],
)
def test_main_explicit_session_read_only_surfaces_do_not_scaffold_missing_actor_roots(
    tmp_path: Path,
    monkeypatch,
    capsys,
    argv: list[str],
    requires_claude: bool,
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-owner")

    assert cli.main(["session", "bind", "retry-review"]) == 0
    capsys.readouterr()
    if requires_claude:
        assert cli.main(["actor", "bind", "Claude"]) == 0
        capsys.readouterr()

    probe_fingerprint = cli._session_token("thread-probe")
    probe_root = registry / "retry-review" / "actors" / probe_fingerprint
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-probe")

    assert cli.main([*argv, "--session", "retry-review"]) == 0

    captured = capsys.readouterr()
    assert "created a new gotta session" not in captured.err
    assert not probe_root.exists()


def test_main_actor_status_prefers_sourced_session_env_over_implicit_context_ids(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()

    shared_root = registry / "demo"
    claude = _actor_id(shared_root, "claude")
    actor_root = shared_root / "actors" / claude
    state = content.load_state_env_at_root(actor_root)
    for key, value in state.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CODEX_THREAD_ID", "conflict-thread")
    monkeypatch.setenv("TERM_SESSION_ID", "terminal-conflict")

    assert cli.main(["actor", "status", "--output", "json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "created a new gotta session" not in captured.err
    assert claude in payload
    assert not (registry / cli._session_token("conflict-thread")).exists()


def test_main_failed_session_init_seed_does_not_leave_half_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["session", "init", "legacy mission"])
    err = capsys.readouterr().err

    assert excinfo.value.code == 2
    session_root = _grouped_root(registry, "thread-123")
    assert "unrecognized arguments: legacy mission" in err
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()
    assert (session_root / "TODO.md").is_file()
    assert (session_root / "LOGS.md").is_file()
    assert (session_root / "OOPS.md").is_file()
    (session_root / "intent.txt").write_text("real intent\n", encoding="utf-8")

    assert cli.main(["want", "--from-file", "intent.txt"]) == 0
    assert (session_root / "WANT.md").read_text(encoding="utf-8") == "real intent\n"


def test_main_want_and_goal_reject_inline_positional_text(
    tmp_path: Path, monkeypatch
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    assert cli.main(["session", "bind"]) == 0

    with pytest.raises(SystemExit) as want_exc:
        cli.main(["want", "inline text"])
    assert want_exc.value.code == 2

    with pytest.raises(SystemExit) as goal_exc:
        cli.main(["goal", "inline text"])
    assert goal_exc.value.code == 2


def test_main_explicit_actor_target_resolves_grouped_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    assert cli.main(["session", "bind"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()
    claude = _actor_id(registry / cli._session_token("thread-123"), "claude")
    actor_root = registry / cli._session_token("thread-123") / "actors" / claude

    assert cli.main(["session", "show", "--actor", "claude"]) == 0
    assert capsys.readouterr().out.strip() == str(actor_root.resolve())


def test_main_resolves_absolute_shared_session_root_to_active_identity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "retry-review"]) == 0
    capsys.readouterr()

    shared_root = registry / "retry-review"
    expected_root = shared_root / "actors" / cli._session_token("thread-123")

    assert cli.main(["session", "show", "--session", str(shared_root)]) == 0

    assert capsys.readouterr().out.strip() == str(expected_root.resolve())
    assert not (shared_root / "state" / "env").exists()


def test_main_read_only_explicit_session_inspection_uses_existing_actor_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    shared_root = registry / "retry-review"
    actor_root = shared_root / "actors" / "claude"
    dirs = content.resolve_dirs(
        content.CommonOptions(
            session_dir=str(actor_root),
            content_dir=str(shared_root / "content"),
            actor="claude",
        ),
        create=True,
    )
    content.write_session_state(
        dirs,
        {
            content.SESSION_ID_ENV: "retry-review",
            content.SESSION_ACTOR_ENV: "claude",
        },
    )
    shared_root.joinpath("session.json").write_text("{}\n", encoding="utf-8")

    assert cli.main(["session", "timeline", "--session", "retry-review", "--output", "json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["sessionDir"] == str(actor_root.resolve())
    assert "created a new gotta session" not in captured.err
    assert not (
        shared_root / "actors" / cli._session_token("thread-123") / "state" / "env"
    ).exists()


def test_main_uses_term_session_id_for_deterministic_binding_on_write_surfaces(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str, str, str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(
            (
                list(argv or []),
                os.environ["GOTTA_SESSION_DIR"],
                os.environ["GOTTA_CONTEXT_ID"],
                os.environ["GOTTA_CONTEXT_SOURCE"],
            )
        )
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")

    assert cli.main(["todo", "append", "real task"]) == 0

    session_root = Path(seen[-1][1])
    assert session_root.parent.name == "actors"
    assert session_root.parent.parent == (tmp_path / "session" / cli._session_token("term-session-1"))
    assert session_root.name == cli._session_token("term-session-1")
    assert seen[-1][0] == ["todo", "append", "real task"]
    assert seen[-1][2] == "term-session-1"
    assert seen[-1][3] == "terminal_session"
    assert "created a new gotta session" in capsys.readouterr().err


def test_main_warns_actor_when_supervisor_requested_failed_disposition(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()
    claude = _actor_id(registry / "actor-root", "claude")
    actor_root = registry / "actor-root" / "actors" / claude

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": claude,
                "label": "Claude",
                "status": "active",
                "requested_status": "failed",
                "requested_summary": "operator chose to stop this actor run",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor requested `failed` (operator chose to stop this actor run)." in err
    assert "Any further activity may be discarded." in err
    assert f"gotta actor signoff {claude} --summary ..." in err


def test_main_warns_actor_when_supervisor_requested_graceful_stop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()
    claude = _actor_id(registry / "actor-root", "claude")
    actor_root = registry / "actor-root" / "actors" / claude

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": claude,
                "label": "Claude",
                "status": "active",
                "requested_mode": "stop",
                "requested_status": "signed_off",
                "requested_summary": "finish the current wave and close out",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor requested a graceful stop (finish the current wave and close out)." in err
    assert f"gotta actor signoff {claude} --summary ..." in err


def test_main_does_not_warn_actor_for_nonfailed_pending_disposition(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude"]) == 0
    capsys.readouterr()
    claude = _actor_id(registry / "actor-root", "claude")
    actor_root = registry / "actor-root" / "actors" / claude

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": claude,
                "label": "Claude",
                "status": "active",
                "requested_status": "signed_off",
                "requested_summary": "looked complete from the operator side",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor requested `failed`" not in err


def test_main_does_not_warn_nonactor_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    actor_root = registry / "actor-root" / "actors" / cli._session_token("thread-123")

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    for key, value in content.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor requested `failed`" not in err


def test_bind_session_root_treats_raced_creation_as_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    context_id = "thread-123"
    canonical = _grouped_root(tmp_path / "session", context_id).resolve()

    @contextmanager
    def fake_lock(base_dir: Path, locked_context_id: str):
        cli._create_session_root(
            canonical,
            context_id=locked_context_id,
            context_source="env",
        )
        yield

    monkeypatch.setattr(cli, "_session_creation_lock", fake_lock)

    root, created = cli._bind_session_root(context_id, "env")

    assert root == canonical
    assert created is False


def test_builtin_plugin_toolerror_renders_cleanly(monkeypatch, capsys) -> None:
    def fake_main(argv: list[str] | None = None) -> int:
        raise jira.ToolError("plain-text only")

    monkeypatch.setattr(jira, "main", fake_main)

    runner = builtin._runner("gotta.plugins.jira")

    assert runner(["search", "project = OPS"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "plain-text only"
