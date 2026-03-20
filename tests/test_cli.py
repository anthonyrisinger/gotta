from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from gotta import builtin
from gotta import content
from gotta import main as cli
from gotta.actor import SESSION_ACTOR_ENV
from gotta.plugins import jira


def _set_default_session_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", root)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", root)


def _grouped_root(registry: Path, context_id: str, *, identity: str | None = None) -> Path:
    fingerprint = cli._session_token(context_id)
    return registry / fingerprint / "actors" / (identity or fingerprint)


def test_main_rejects_unknown_plugin(capsys) -> None:
    assert cli.main(["nonsense"]) == 2
    assert "unknown gotta plugin: nonsense" in capsys.readouterr().err


def test_main_creates_and_reuses_context_bound_session(
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

    assert cli.main(["jira", "status"]) == 0
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

    assert cli.main(["slack", "status"]) == 0
    second_err = capsys.readouterr().err
    assert seen[-1][1] == str(session_root)
    assert "created a new gotta session" not in second_err
    assert second_err == ""


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

    assert cli.main(["read", "--head", "120", "README.md"]) == 0

    session_root = Path(seen[0][1])
    assert seen == [(["read", "--head", "120", "README.md"], str(session_root))]
    assert (session_root / "state" / "env").exists()
    assert "created a new gotta session" in capsys.readouterr().err


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


def test_main_session_show_and_doctor_create_and_reuse_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "show"]) == 0
    show_output = capsys.readouterr().out
    session_root = _grouped_root(registry, "thread-123")
    assert show_output.strip() == str(session_root.resolve())
    assert (session_root / "state" / "env").exists()
    assert (session_root / "content").is_dir()
    assert (session_root / "TODO.md").is_file()

    assert cli.main(["session", "doctor"]) == 0
    doctor_output = json.loads(capsys.readouterr().out)
    assert doctor_output["GOTTA_SESSION_DIR"] == str(session_root.resolve())
    assert doctor_output["session_initialized"] == "yes"


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
    claude_root = registry / "demo" / "actors" / "claude"
    notes_records = [
        json.loads(line)
        for line in (claude_root / "state" / "notes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert notes_records[-1]["actor"] == "claude"
    assert notes_records[-1]["author"] == fingerprint

    activity_records = [
        json.loads(line)
        for line in (claude_root / "state" / "activity.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert activity_records[-1]["action"] == "append"
    assert activity_records[-1]["actor"] == fingerprint
    assert activity_records[-1]["target_actor"] == "claude"


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

    parent_root = _grouped_root(registry, "thread-123")
    parent_dirs = content.ResolvedDirs(
        session_dir=parent_root,
        content_dir=parent_root / "content",
    )
    parent_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        parent_dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
        },
    )
    parent_root.joinpath("bin").mkdir(parents=True, exist_ok=True)

    actor_root = registry / cli._session_token("thread-123") / "actors" / "claude"
    actor_dirs = content.ResolvedDirs(
        session_dir=actor_root,
        content_dir=registry / cli._session_token("thread-123") / "content",
    )
    actor_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        actor_dirs,
        {
            content.SESSION_ID_ENV: cli._session_token("thread-123"),
            content.SESSION_ACTOR_ENV: "claude",
        },
    )
    actor_root.joinpath("bin").mkdir(parents=True, exist_ok=True)

    assert cli.main(["session", "show", "--actor", "claude"]) == 0
    assert capsys.readouterr().out.strip() == str(actor_root.resolve())


def test_main_resolves_absolute_shared_session_root_to_active_identity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "retry-review", "--actor", "claude"]) == 0
    capsys.readouterr()

    shared_root = registry / "retry-review"
    expected_root = shared_root / "actors" / "claude"

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


def test_main_uses_term_session_id_for_deterministic_binding(
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

    assert cli.main(["jira", "status"]) == 0

    session_root = Path(seen[-1][1])
    assert session_root.parent.name == "actors"
    assert session_root.parent.parent == (tmp_path / "session" / cli._session_token("term-session-1"))
    assert session_root.name == cli._session_token("term-session-1")
    assert seen[-1][2] == "term-session-1"
    assert seen[-1][3] == "term_session"
    assert "created a new gotta session" in capsys.readouterr().err


def test_main_warns_actor_when_supervisor_requested_failed_disposition(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    shared_root = registry / "actor-root"
    actor_root = shared_root / "actors" / "claude"
    actor_dirs = content.ResolvedDirs(
        session_dir=actor_root,
        content_dir=shared_root / "content",
    )
    actor_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        actor_dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
            content.SESSION_ID_ENV: "actor-root",
            content.SESSION_ACTOR_ENV: "claude",
            SESSION_ACTOR_ENV: "claude",
        },
    )
    actor_root.joinpath("bin").mkdir(parents=True, exist_ok=True)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.parent.mkdir(parents=True, exist_ok=True)
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": "claude",
                "label": "Claude",
                "status": "active",
                "requested_status": "failed",
                "requested_summary": "operator chose to stop this actor run",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_CONTEXT_ID", "thread-123")
    monkeypatch.setenv("GOTTA_CONTEXT_SOURCE", "codex_thread")
    monkeypatch.setenv("GOTTA_SESSION_ACTOR", "claude")

    assert cli.main(["jira", "status"]) == 0
    err = capsys.readouterr().err

    assert seen == [["jira", "status"]]
    assert "Supervisor requested `failed` (operator chose to stop this actor run)." in err
    assert "Any further activity may be discarded." in err
    assert "gotta actor signoff claude --summary ..." in err


def test_main_warns_actor_when_supervisor_requested_graceful_stop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    shared_root = registry / "actor-root"
    actor_root = shared_root / "actors" / "claude"
    actor_dirs = content.ResolvedDirs(
        session_dir=actor_root,
        content_dir=shared_root / "content",
    )
    actor_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        actor_dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
            content.SESSION_ID_ENV: "actor-root",
            content.SESSION_ACTOR_ENV: "claude",
            SESSION_ACTOR_ENV: "claude",
        },
    )
    actor_root.joinpath("bin").mkdir(parents=True, exist_ok=True)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.parent.mkdir(parents=True, exist_ok=True)
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": "claude",
                "label": "Claude",
                "status": "active",
                "requested_mode": "stop",
                "requested_status": "signed_off",
                "requested_summary": "finish the current wave and close out",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_CONTEXT_ID", "thread-123")
    monkeypatch.setenv("GOTTA_CONTEXT_SOURCE", "codex_thread")
    monkeypatch.setenv("GOTTA_SESSION_ACTOR", "claude")

    assert cli.main(["jira", "status"]) == 0
    err = capsys.readouterr().err

    assert seen == [["jira", "status"]]
    assert "Supervisor requested a graceful stop (finish the current wave and close out)." in err
    assert "gotta actor signoff claude --summary ..." in err


def test_main_does_not_warn_actor_for_nonfailed_pending_disposition(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    actor_root = registry / "actor-root"
    actor_dirs = content.ResolvedDirs(
        session_dir=actor_root,
        content_dir=actor_root / "content",
    )
    actor_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        actor_dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
            SESSION_ACTOR_ENV: "claude",
        },
    )
    actor_root.joinpath("bin").mkdir(parents=True, exist_ok=True)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.parent.mkdir(parents=True, exist_ok=True)
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": "claude",
                "label": "Claude",
                "status": "active",
                "requested_status": "signed_off",
                "requested_summary": "looked complete from the operator side",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_CONTEXT_ID", "thread-123")
    monkeypatch.setenv("GOTTA_CONTEXT_SOURCE", "codex_thread")
    monkeypatch.setenv("GOTTA_SESSION_ACTOR", "claude")

    assert cli.main(["jira", "status"]) == 0
    err = capsys.readouterr().err

    assert seen == [["jira", "status"]]
    assert "Supervisor requested `failed`" not in err


def test_main_does_not_warn_nonactor_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli, "_gotta_main", fake_gotta_main)
    root = registry / "session-root"
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        dirs,
        {
            content.CONTEXT_ID_ENV: "thread-123",
            content.CONTEXT_SOURCE_ENV: "codex_thread",
        },
    )
    root.joinpath("bin").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(root))
    monkeypatch.setenv("GOTTA_CONTEXT_ID", "thread-123")
    monkeypatch.setenv("GOTTA_CONTEXT_SOURCE", "codex_thread")
    monkeypatch.delenv("GOTTA_SESSION_ACTOR", raising=False)

    assert cli.main(["jira", "status"]) == 0
    err = capsys.readouterr().err

    assert seen == [["jira", "status"]]
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
            activation="gotta",
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
