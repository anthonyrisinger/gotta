from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from gotta.actors import ACTOR_CALLEE_ENV, ACTOR_SPEAKER_ENV
from gotta import content, dispatch
from gotta import leads
from gotta import main as cli
from gotta.actor import SESSION_ACTOR_ENV
from gotta import session as sessionlib
from gotta import todo as session_todo
from gotta.notes import actor_notes_surface_path
from gotta.plugins import goal
from gotta.plugins import notes
from gotta.plugins import actor
from gotta.plugins import session
from gotta.plugins import want


@pytest.fixture(autouse=True)
def local_session_registry(tmp_path: Path, monkeypatch) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.delenv(content.SESSION_ENV, raising=False)
    monkeypatch.delenv(content.CONTENT_ENV, raising=False)
    monkeypatch.delenv(content.SESSION_REPO_ENV, raising=False)
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(ACTOR_CALLEE_ENV, raising=False)
    monkeypatch.delenv(SESSION_ACTOR_ENV, raising=False)


def make_dirs(root: Path) -> content.ResolvedDirs:
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    return dirs


def initialize_session(root: Path) -> content.ResolvedDirs:
    dirs = make_dirs(root)
    content.write_state_env(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    return dirs


def _init_session(root: Path, capsys) -> None:
    assert session.main(["init", "--session", str(root)]) == 0
    assert capsys.readouterr().out.strip() == str(root.resolve())


def _bind_actors(root: Path, capsys, *actors: str) -> str:
    assert actor.main(["bind", *actors, "--session", str(root)]) == 0
    return capsys.readouterr().out


def _actor_id(root: Path, actor_ref: str) -> str:
    return sessionlib._resolve_bound_actor_name(root, actor_ref)


def _actor_root(root: Path, actor_ref: str) -> Path:
    return sessionlib._actor_session_dir(root, _actor_id(root, actor_ref))


@pytest.mark.parametrize("command", ["show", "doctor"])
def test_session_commands_require_bootstrap(
    tmp_path: Path, monkeypatch, capsys, command: str
) -> None:
    default_root = tmp_path / "default"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        session.main([command])

    assert excinfo.value.code == 2
    assert "start or bind a session first" in capsys.readouterr().err


def test_session_init_bootstraps_state_env_and_bin(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "session-root"

    assert session.main(["init", "--session", str(root)]) == 0

    assert capsys.readouterr().out.strip() == str(root.resolve())
    assert (root / "state" / "env").exists()
    assert (root / "state").is_dir()
    assert (root / "bin").is_dir()


def test_session_init_scaffolds_surface_and_drops_ephemeral_context_state(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        dirs,
        {
            content.CONTEXT_ID_ENV: "ctx-123",
            content.CONTEXT_SOURCE_ENV: "test",
        },
    )

    _init_session(root, capsys)

    state = content.load_state_env_at_root(root)
    assert content.CONTEXT_ID_ENV not in state
    assert content.CONTEXT_SOURCE_ENV not in state
    assert state[content.SESSION_INITIALIZED_ENV] == "1"
    for name in ("WANT.md", "GOAL.md", "TODO.md", "LOGS.md", "OOPS.md"):
        assert (root / name).exists()
    assert "_empty_" in (root / "WANT.md").read_text(encoding="utf-8")
    assert "Mission seed" not in (root / "WANT.md").read_text(encoding="utf-8")


def test_session_init_is_idempotent_and_preserves_rewritten_charters(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    want_text = "# Want\n\nStay narrow.\n"
    goal_text = "# Goal\n\nCollect two anchors.\n"
    (root / "WANT.md").write_text(want_text, encoding="utf-8")
    (root / "GOAL.md").write_text(goal_text, encoding="utf-8")
    initial_todo_count = len(session_todo.todo_items(root))

    _init_session(root, capsys)

    assert (root / "WANT.md").read_text(encoding="utf-8") == want_text
    assert (root / "GOAL.md").read_text(encoding="utf-8") == goal_text
    assert len(session_todo.todo_items(root)) == initial_todo_count


def test_want_and_goal_support_show_and_session_relative_from_file(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"
    charter_dir = root / "charters"
    charter_dir.mkdir(parents=True)
    want_source = charter_dir / "want.txt"
    goal_source = charter_dir / "goal.txt"
    want_source.write_text("# Want\n\nTrack retry ownership.\n", encoding="utf-8")
    goal_source.write_text("# Goal\n\nMaterialize one strong source first.\n", encoding="utf-8")

    _init_session(root, capsys)

    assert want.main(["--session", str(root), "--from-file", "charters/want.txt"]) == 0
    capsys.readouterr()
    assert goal.main(["--session", str(root), "--from-file", "charters/goal.txt"]) == 0
    capsys.readouterr()

    assert want.main(["--session", str(root)]) == 0
    want_output = capsys.readouterr().out
    assert want_output == "# Want\n\nTrack retry ownership.\n"

    assert goal.main(["--session", str(root)]) == 0
    goal_output = capsys.readouterr().out
    assert goal_output == "# Goal\n\nMaterialize one strong source first.\n"


def test_want_and_goal_support_explicit_stdin_rewrites(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    monkeypatch.setattr(sessionlib.sys, "stdin", io.StringIO("# Want\n\nStay focused.\n"))
    assert want.main(["--session", str(root), "--stdin"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(sessionlib.sys, "stdin", io.StringIO("# Goal\n\nLand one anchor.\n"))
    assert goal.main(["--session", str(root), "--stdin"]) == 0
    capsys.readouterr()

    assert (root / "WANT.md").read_text(encoding="utf-8") == "# Want\n\nStay focused.\n"
    assert (root / "GOAL.md").read_text(encoding="utf-8") == "# Goal\n\nLand one anchor.\n"


def test_want_and_goal_reject_inline_positional_text(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    with pytest.raises(SystemExit) as want_exc:
        want.main(["inline text", "--session", str(root)])
    assert want_exc.value.code == 2

    with pytest.raises(SystemExit) as goal_exc:
        goal.main(["inline text", "--session", str(root)])
    assert goal_exc.value.code == 2


def test_want_and_goal_can_target_actor_sessions_by_identity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    monkeypatch.setenv(content.SESSION_ENV, str(root))
    monkeypatch.setenv(content.CONTENT_ENV, str(root / "content"))
    actor_root = _actor_root(root, "claude")
    charter_dir = actor_root / "charters"
    charter_dir.mkdir(parents=True, exist_ok=True)
    (charter_dir / "want.txt").write_text("Actor-specific intent\n", encoding="utf-8")
    (charter_dir / "goal.txt").write_text("Actor-specific goal\n", encoding="utf-8")

    assert want.main(["--session", str(actor_root), "--from-file", "charters/want.txt"]) == 0
    capsys.readouterr()
    assert goal.main(["--session", str(actor_root), "--from-file", "charters/goal.txt"]) == 0
    capsys.readouterr()

    assert (actor_root / "WANT.md").read_text(encoding="utf-8") == "Actor-specific intent\n"
    assert (actor_root / "GOAL.md").read_text(encoding="utf-8") == "Actor-specific goal\n"


def test_actor_bind_binds_grouped_actor_surfaces_without_launching(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    output = _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "claude")
    codex = _actor_id(root, "codex")

    assert f"bound {claude} (Claude) session" in output
    assert f"bound {codex} (Codex) session" in output
    assert f"gotta want --actor {claude} --stdin" in output
    assert f"gotta goal --actor {claude} --stdin" in output
    for actor_name in (claude, codex):
        actor_root = sessionlib._actor_session_dir(root, actor_name)
        assert actor_root.exists()
        assert (root / "bin" / actor_name).is_file()
        assert (actor_root / "README.md").is_file()
        assert (actor_root / "TODO.md").is_file()
        assert sessionlib._read_actor_state(root, actor_name)["status"] == "bound"


def test_actor_launch_blockers_emit_native_actor_charter_commands(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")

    blockers = sessionlib._actor_launch_blockers(root, actor_name="claude")

    assert any(f"gotta want --actor {claude} --stdin" in blocker for blocker in blockers)
    assert any(f"gotta goal --actor {claude} --stdin" in blocker for blocker in blockers)


def test_actor_launch_blockers_report_linked_actor_paths(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")

    blockers = sessionlib._actor_launch_blockers(root, actor_name="claude")

    assert any(str(sessionlib._actor_session_dir(root, claude) / "WANT.md") in blocker for blocker in blockers)
    assert any(str(sessionlib._actor_session_dir(root, claude) / "GOAL.md") in blocker for blocker in blockers)


def test_actor_launch_uses_isolated_copilot_config_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    actor_root = _actor_root(root, "claude")
    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

        def wait(self) -> int:
            return 0

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            if timeout is None:
                return None
            return None

    monkeypatch.setattr(
        actor.session_plugin,
        "_actor_launch_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(actor, "_actor_prompt", lambda **_kwargs: "prompt")

    def fake_spawn(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeProc:
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return FakeProc()

    monkeypatch.setattr(actor, "_spawn_actor_process", fake_spawn)
    monkeypatch.setattr(actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread())

    assert actor.main(["launch", claude, "--session", str(root)]) == 0
    capsys.readouterr()

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--experimental" not in argv
    assert "--config-dir" in argv
    config_dir = Path(argv[argv.index("--config-dir") + 1])
    assert config_dir == actor_root / "state" / "copilot"
    assert config_dir.is_dir()
    assert captured["cwd"] == actor_root


def test_actor_status_empty_guidance_points_to_actor_bind(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    assert actor.main(["status", "--session", str(root)]) == 0
    output = capsys.readouterr().out
    assert "no actors bound for this session" in output
    assert "gotta actor bind Claude" in output


def test_actor_status_discovers_initialized_fingerprint_actors_missing_from_metadata(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    discovered = "aaaaaaaaaaaa"
    actor_root = root / "actors" / discovered
    actor_dirs = content.ResolvedDirs(
        session_dir=actor_root,
        content_dir=root / "content",
    )
    actor_dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        actor_dirs,
        {
            content.SESSION_ID_ENV: root.name,
            content.SESSION_ACTOR_ENV: discovered,
        },
    )
    actor_root.joinpath("bin").mkdir(parents=True, exist_ok=True)
    sessionlib.scaffold_session(actor_root)
    (root / "session.json").write_text(
        json.dumps({"session_id": root.name, "members": []}) + "\n",
        encoding="utf-8",
    )

    assert actor.main(["status", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert discovered in payload


def test_notes_show_requires_explicit_actor(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["show", "--session", str(root)])
    assert "missing actor; use `gotta notes show --actor <actor>`" in str(excinfo.value)


def test_notes_show_unbound_actor_fails_without_materializing_surface(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["show", "--actor", "claude", "--session", str(root)])

    assert "claude is not bound for this session" in str(excinfo.value)
    assert not sessionlib._actor_session_dir(root, "claude").exists()


def test_bound_session_root_prefers_explicit_identity_over_hyphen_split(
    tmp_path: Path, monkeypatch
) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)

    foo_root = registry / "foo" / "actors" / "bar"
    foo_content = registry / "foo" / "content"
    foo_root.mkdir(parents=True, exist_ok=True)
    foo_content.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        content.ResolvedDirs(session_dir=foo_root, content_dir=foo_content),
        {
            content.SESSION_ID_ENV: "foo",
            content.SESSION_ACTOR_ENV: "bar",
        },
    )

    foo_bar_root = registry / "foo-bar" / "actors" / "baz"
    foo_bar_content = registry / "foo-bar" / "content"
    foo_bar_root.mkdir(parents=True, exist_ok=True)
    foo_bar_content.mkdir(parents=True, exist_ok=True)
    content.write_session_state(
        content.ResolvedDirs(session_dir=foo_bar_root, content_dir=foo_bar_content),
        {
            content.SESSION_ID_ENV: "foo-bar",
            content.SESSION_ACTOR_ENV: "baz",
        },
    )

    monkeypatch.delenv(content.SESSION_ENV, raising=False)
    monkeypatch.setenv(content.SESSION_ID_ENV, "foo-bar")
    monkeypatch.setenv(content.SESSION_ACTOR_ENV, "baz")

    assert (
        content.bound_session_root(include_context_session=False)
        == foo_bar_root.resolve()
    )


def test_actor_complete_becomes_terminal_immediately_and_survives_heartbeat(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(root, claude, {"status": "active"})

    assert (
        actor.main(
            [
                "complete",
                claude,
                "--session",
                str(root),
                "--summary",
                "ready for review",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = sessionlib._actor_status_payload(root, claude)
    assert payload["status"] == "completed"
    assert payload["requested_pending"] is False
    assert payload["summary"] == "ready for review"

    assert actor.main(["heartbeat", claude, "--session", str(root)]) == 0
    capsys.readouterr()
    assert sessionlib._actor_status_payload(root, claude)["status"] == "completed"


def test_actor_signoff_becomes_terminal_immediately_and_survives_heartbeat(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(root, claude, {"status": "active"})

    assert (
        actor.main(
            [
                "signoff",
                claude,
                "--session",
                str(root),
                "--summary",
                "accepted by operator",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = sessionlib._actor_status_payload(root, claude)
    assert payload["status"] == "signed_off"
    assert payload["requested_pending"] is False
    assert payload["signoff_summary"] == "accepted by operator"

    assert actor.main(["heartbeat", claude, "--session", str(root)]) == 0
    capsys.readouterr()
    assert sessionlib._actor_status_payload(root, claude)["status"] == "signed_off"


def test_actor_status_treats_signoff_timestamp_as_authoritative(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "heartbeat_at": "2026-03-17T00:00:00Z",
            "signoff_at": "2026-03-17T00:01:00Z",
            "signoff_summary": "actor closed itself cleanly",
        },
    )

    payload = sessionlib._actor_status_payload(root, claude)

    assert payload["status"] == "signed_off"
    assert payload["still_running"] is False
    assert payload["signoff_summary"] == "actor closed itself cleanly"


def test_actor_fail_stays_pending_while_live_and_notes_render_stop_warning(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(root, claude, {"status": "active"})

    assert (
        actor.main(
            [
                "fail",
                claude,
                "--session",
                str(root),
                "--summary",
                "operator stopped this run",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = sessionlib._actor_status_payload(root, claude)

    assert "authoritative status stays active" in output
    assert payload["status"] == "active"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "failed"
    notes_text = actor_notes_surface_path(root, claude).read_text(encoding="utf-8")
    assert "Supervisor requested `failed` (operator stopped this run)." in notes_text
    assert "pending_disposition: failed: operator stopped this run" in notes_text


def test_actor_stop_stays_pending_while_live_and_notes_render_graceful_warning(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(root, claude, {"status": "active"})

    assert (
        actor.main(
            [
                "stop",
                claude,
                "--session",
                str(root),
                "--summary",
                "finish the current wave and close out",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = sessionlib._actor_status_payload(root, claude)

    assert f"recorded stop request for {claude}" in output
    assert payload["status"] == "active"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "signed_off"
    assert payload["requested_mode"] == "stop"
    assert payload["requested_label"] == "stop"
    notes_text = actor_notes_surface_path(root, claude).read_text(encoding="utf-8")
    assert (
        "Supervisor requested a graceful stop (finish the current wave and close out)."
        in notes_text
    )
    assert "pending_disposition: stop: finish the current wave and close out" in notes_text


def test_notes_projection_skips_supervisor_warning_for_nonfailed_pending_requests(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "requested_status": "signed_off",
            "requested_summary": "looked done from the operator side",
        },
    )
    sessionlib._sync_actor_projection_surfaces(root, claude)

    notes_text = actor_notes_surface_path(root, claude).read_text(encoding="utf-8")
    assert "Supervisor requested `failed`" not in notes_text
    assert "pending_disposition: signed off: looked done from the operator side" in notes_text


def test_actor_bind_preserves_preexisting_actor_local_log_file(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    actor_root = sessionlib._actor_session_dir(root, "claude")
    actor_root.mkdir(parents=True, exist_ok=True)
    (actor_root / "LOGS.md").write_text("stale log placeholder\n", encoding="utf-8")
    actor_state_dir = actor_root / "state"
    actor_state_dir.mkdir(parents=True, exist_ok=True)
    (actor_state_dir / "actor.json").write_text(
        json.dumps(
            {
                "actor": "claude",
                "label": "Claude",
                "status": "signed_off",
                "signoff_summary": "stale orphaned actor state",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (actor_root / "NOTES.md").write_text("# Claude Notes\n\nstale\n", encoding="utf-8")

    assert actor.main(["bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()

    assert (actor_root / "LOGS.md").is_file()
    assert "stale log placeholder" in (actor_root / "LOGS.md").read_text(encoding="utf-8")
    assert sessionlib._actor_status_payload(root, _actor_id(root, "claude"))["status"] == "bound"


def test_actor_status_reports_recent_activity_and_recent_artifacts(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(
        root,
        claude,
        {
            "status": "signed_off",
            "signoff_at": "2026-03-17T00:03:00Z",
            "signoff_summary": "accepted by operator",
        },
    )
    events_path = sessionlib._actor_events_path(root, claude)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:01:00Z",
                        "actor": claude,
                        "event": "heartbeat",
                        "detail": "",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:02:00Z",
                        "actor": claude,
                        "event": "signed_off",
                        "detail": "accepted by operator",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:03:00Z",
                        "actor": claude,
                        "event": "note",
                        "detail": "Wave 2 landed",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    for index, locator in enumerate(
        [
            "jira:DEMO-6292",
            "jira:DEMO-6361",
            "github:search --type pr edge rmp acme",
            "jira:search RMP",
        ],
        start=1,
    ):
        content.materialize_bytes(
            f"artifact {index}".encode("utf-8"),
            dirs=dirs,
            preferred_name=f"artifact-{index}.md",
            metadata={
                "tool": "gotta",
                "plugin": "jira" if locator.startswith("jira:") else "github",
                "locator": locator,
                "canonical_locator": locator,
                "actor": claude,
            },
            timestamp=f"2026-03-17T00:0{index}:00.000001Z",
        )

    assert actor.main(["status", claude, "--session", str(root)]) == 0
    output = capsys.readouterr().out
    assert "artifacts: 4" in output
    assert "recent_activity: 2026-03-17T00:03:00Z Wave 2 landed" in output
    assert "recent_artifacts:" in output
    assert "`jira:search RMP`" in output
    assert "`github:search --type pr edge rmp acme`" in output
    assert "`jira:DEMO-6361`" in output
    assert "(+1 more)" in output
    assert "heartbeat" not in output


def test_actor_status_json_reports_recent_activity_and_last_activity_summary(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(root, claude, {"status": "active"})
    events_path = sessionlib._actor_events_path(root, claude)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:00:00Z",
                        "actor": claude,
                        "event": "heartbeat",
                        "detail": "",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:01:00Z",
                        "actor": claude,
                        "event": "note",
                        "detail": "Wave 1 landed",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:02:00Z",
                        "actor": claude,
                        "event": "runtime_exit",
                        "detail": "actor process exited with code 0",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert actor.main(["status", claude, "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)[claude]
    assert payload["last_activity_at"] == "2026-03-17T00:02:00Z"
    assert payload["last_activity_summary"] == "runtime exit: actor process exited with code 0"
    assert [item["event"] for item in payload["recent_activity"]] == ["runtime_exit", "note"]
    assert payload["artifact_count"] == 0


def test_actor_status_highlights_missing_heartbeat_note_for_live_actor(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    sessionlib._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "started_at": sessionlib.datetime.now(tz=sessionlib.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    payload = sessionlib._actor_status_payload(root, claude)
    assert payload["notes_status"] == "empty"
    assert "heartbeat note now" in payload["next_step"]


def test_actor_recent_activity_carries_cross_actor_author(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    events_path = sessionlib._actor_events_path(root, claude)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-03-17T00:03:00Z",
                "actor": claude,
                "author": "thread-123",
                "event": "note",
                "detail": "Wave 2 landed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = sessionlib._actor_status_payload(root, claude)
    assert payload["recent_activity"][0]["author"] == "thread-123"
    assert payload["recent_activity"][0]["summary"] == "thread-123: Wave 2 landed"


def test_session_bind_can_switch_active_session(tmp_path: Path, monkeypatch, capsys) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "legacy-mission"]) == 0
    payload = dict(line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines())
    assert payload["session"] == "legacy-mission"
    assert payload["actor"] == cli._session_token("thread-123")
    assert payload["root"].endswith(
        f"/sessions/legacy-mission/actors/{cli._session_token('thread-123')}"
    )


def test_session_bind_without_id_returns_to_private_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "legacy-mission"]) == 0
    capsys.readouterr()

    assert cli.main(["session", "bind"]) == 0
    payload = dict(line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines())
    fingerprint = cli._session_token("thread-123")
    assert payload["session"] == fingerprint
    assert payload["actor"] == fingerprint
    assert payload["root"].endswith(f"/sessions/{fingerprint}/actors/{fingerprint}")


def test_actor_bind_uses_current_bound_shared_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "retry-review"]) == 0
    capsys.readouterr()

    assert cli.main(["actor", "bind", "Claude"]) == 0
    output = capsys.readouterr().out
    fingerprint = cli._session_token("thread-123")
    active_root = registry / "retry-review" / "actors" / fingerprint
    claude = _actor_id(registry / "retry-review", "claude")
    claude_root = registry / "retry-review" / "actors" / claude
    assert f"bound {claude} (Claude) session" in output
    assert (claude_root / "WANT.md").exists()
    assert (active_root / "content").is_symlink()
    assert os.readlink(active_root / "content") == "../../content"
    assert not (active_root / "session").exists()
    assert (claude_root / "content").is_symlink()
    assert os.readlink(claude_root / "content") == "../../content"
    assert not (claude_root / "session").exists()
    assert not (registry / cli._session_token("thread-123") / "actors" / claude).exists()


def test_session_show_works_from_initialized_session_root(tmp_path: Path, monkeypatch, capsys) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)

    monkeypatch.chdir(local_root)

    assert session.main(["show"]) == 0
    assert capsys.readouterr().out.strip() == str(local_root.resolve())


@pytest.mark.parametrize(
    "argv",
    [
        ["show"],
        ["doctor"],
        ["manifest", "--output", "json"],
        ["timeline", "--output", "json"],
        ["graph", "--output", "json"],
    ],
)
def test_session_commands_accept_explicit_root_without_ambient_activation(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)

    monkeypatch.chdir(tmp_path)

    assert session.main([argv[0], "--session", str(local_root), *argv[1:]]) == 0

    output = capsys.readouterr().out
    assert output.strip()


def test_session_show_reports_local_state_over_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    default_root = tmp_path / "default"
    local_root = tmp_path / "local"
    initialize_session(default_root)
    initialize_session(local_root)

    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(local_root)

    assert session.main([]) == 0
    assert capsys.readouterr().out.strip() == str(local_root.resolve())


def test_session_analyze_writes_summary_and_graph(tmp_path: Path, monkeypatch, capsys) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    result = content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "demo",
            "canonical_locator": "demo",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"hello again",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "demo-edit",
            "canonical_locator": "demo",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert session.main(["analyze", "--session", str(local_root)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["sessionDir"] == str(local_root.resolve())
    assert summary["contentCount"] == 2
    assert summary["revisionEdgeCount"] == 1
    assert summary["semanticNodeCount"] >= 2
    assert Path(summary["graphMermaidPath"]).read_text(encoding="utf-8").startswith("---")
    graph_markdown = Path(summary["graphMermaidMarkdownPath"]).read_text(encoding="utf-8")
    assert graph_markdown.startswith("# gotta session analysis\n\n```mermaid\n")
    assert 'flowchart LR' in graph_markdown
    assert summary["graphMermaidMarkdownPath"].endswith(".mmd.md")
    graph_payload = json.loads(Path(summary["graphJsonPath"]).read_text(encoding="utf-8"))
    semantic_payload = json.loads(
        Path(summary["semanticGraphJsonPath"]).read_text(encoding="utf-8")
    )
    semantic_graph_markdown = Path(summary["semanticGraphMermaidMarkdownPath"]).read_text(
        encoding="utf-8"
    )
    assert semantic_graph_markdown.startswith("# gotta semantic graph\n\n```mermaid\n")
    assert summary["semanticGraphMermaidMarkdownPath"].endswith(".mmd.md")
    assert graph_payload["sessionDir"] == str(local_root.resolve())
    assert graph_payload["contentCount"] == 2
    assert semantic_payload["nodeCount"] >= 2
    assert any(edge["to"] != edge["from"] for edge in graph_payload["revisionEdges"])
    assert result.data_path.name == "data"


def test_session_graph_prefers_canonical_locator_for_binding(tmp_path: Path) -> None:
    dirs = initialize_session(tmp_path / "local")
    manifest_path = dirs.content_dir / "manifest.jsonl"
    manifest_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "locator": "https://example.atlassian.net/browse/PROJ-3960",
                        "canonical_locator": "jira:PROJ-3960",
                        "plugin": "read",
                        "checksum": "abc123",
                        "preferred_name": "PROJ-3960.md",
                    }
                ),
                json.dumps(
                    {
                        "locator": "get https://example.atlassian.net/browse/PROJ-3960 --output markdown",
                        "canonical_locator": "jira:PROJ-3960",
                        "plugin": "jira",
                        "checksum": "abc123",
                        "preferred_name": "PROJ-3960.md",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = session._graph_payload(dirs)

    assert payload["sourceCount"] == 1
    assert payload["contentCount"] == 1
    assert payload["edgeCount"] == 2
    assert payload["sources"] == [
        {
            "locator": "jira:PROJ-3960",
            "followCommand": "gotta read 'jira:PROJ-3960'",
            "contentCount": 1,
            "collision": False,
            "variant": False,
            "variantCount": 0,
            "variants": [],
        }
    ]
    assert payload["content"][0]["contentLocator"] == "content:abc123"
    assert payload["content"][0]["artifactLocator"] == "artifact:PROJ-3960.md@abc123"
    assert payload["content"][0]["followCommand"] == "gotta read 'artifact:PROJ-3960.md@abc123'"


def test_session_empty_graph_and_analyze_make_empty_state_explicit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.chdir(tmp_path)

    assert session.main(["graph", "--session", str(local_root)]) == 0
    graph_output = capsys.readouterr().out
    assert "No evidence nodes yet." in graph_output
    assert "gotta read &lt;locator&gt;" in graph_output

    assert session.main(["graph", "--session", str(local_root), "--output", "json"]) == 0
    graph_payload = json.loads(capsys.readouterr().out)
    assert graph_payload["empty"] is True
    assert graph_payload["nextStep"].startswith("No evidence nodes yet.")

    assert session.main(["analyze", "--session", str(local_root), "--stdout"]) == 0
    analyze_output = capsys.readouterr().out
    assert "No evidence nodes yet." in analyze_output
    assert "gotta read &lt;locator&gt;" in analyze_output


def test_session_analyze_treats_multiple_renderings_as_variants_not_collisions(
    tmp_path: Path,
) -> None:
    dirs = initialize_session(tmp_path / "local")
    url = "https://github.com/acme/widgets"
    content.materialize_bytes(
        b"summary view",
        dirs=dirs,
        preferred_name="widgets.summary",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "subcommand": "default",
            "argv": ["--output", "summary", url],
            "locator": url,
            "canonical_locator": url,
            "content_type": "text/plain",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"markdown view",
        dirs=dirs,
        preferred_name="widgets.md",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "subcommand": "default",
            "argv": ["--output", "markdown", url],
            "locator": url,
            "canonical_locator": url,
            "content_type": "text/markdown",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    graph_payload = session._graph_payload(dirs)
    analysis_payload = session._analysis_payload(dirs)

    assert graph_payload["sources"][0]["collision"] is False
    assert graph_payload["sources"][0]["variant"] is True
    assert graph_payload["sources"][0]["variantCount"] == 2
    assert analysis_payload["collisionCount"] == 0
    assert analysis_payload["variantCount"] == 1
    assert analysis_payload["revisionEdgeCount"] == 0
    assert analysis_payload["sources"][0]["variant"] is True
    assert analysis_payload["sources"][0]["variantCount"] == 2

    mermaid = session._render_analysis_mermaid(analysis_payload)
    assert "renderings: 2" in mermaid


def test_session_analyze_reports_duplicate_materializations_without_variant_drift(
    tmp_path: Path,
) -> None:
    dirs = initialize_session(tmp_path / "local")
    url = "confluence:4373708801"
    content.materialize_bytes(
        b"primary body",
        dirs=dirs,
        preferred_name="4373708801.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 4373708801",
            "canonical_locator": url,
            "actor": "primary",
            "content_type": "text/markdown",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"actor body",
        dirs=dirs,
        preferred_name="4373708801.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 4373708801",
            "canonical_locator": url,
            "actor": "claude",
            "content_type": "text/markdown",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    analysis_payload = session._analysis_payload(dirs)

    assert analysis_payload["collisionCount"] == 0
    assert analysis_payload["variantCount"] == 0
    assert analysis_payload["duplicateMaterializationCount"] == 1
    assert analysis_payload["duplicateMaterializations"] == [url]
    assert analysis_payload["sources"][0]["duplicateMaterialization"] is True
    assert analysis_payload["sources"][0]["contentCount"] == 2

    mermaid = session._render_analysis_mermaid(analysis_payload)
    assert "materializations: 2" in mermaid


def test_session_analyze_extracts_explicit_leads_and_surfaces_gaps(
    tmp_path: Path,
) -> None:
    dirs = initialize_session(tmp_path / "local")
    content.materialize_bytes(
        (
            "# ABC-1\n\n"
            "Depends on ABC-2.\n"
            "Design doc: confluence:12345\n"
            "PR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# ABC-2\n\nDone.\n",
        dirs=dirs,
        preferred_name="ABC-2.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-2",
            "canonical_locator": "jira:ABC-2",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    analysis_payload = session._analysis_payload(dirs)
    semantic_payload = session._semantic_payload(dirs)
    analysis_mermaid = session._render_analysis_mermaid(analysis_payload)

    assert analysis_payload["leadSourceCount"] == 3
    assert analysis_payload["materializedLeadSourceCount"] == 1
    assert analysis_payload["unmaterializedLeadSourceCount"] == 2
    assert analysis_payload["leadEdgeCount"] == 3
    assert any(edge["targetLocator"] == "jira:ABC-2" for edge in analysis_payload["leadEdges"])
    assert any(edge["targetLocator"] == "confluence:12345" for edge in analysis_payload["leadEdges"])
    assert any(
        edge["targetLocator"] == "https://github.com/acme/widgets/pull/7"
        for edge in analysis_payload["leadEdges"]
    )
    assert any(source["locator"] == "jira:ABC-2" and source["materialized"] for source in analysis_payload["leadSources"])
    assert any(
        source["locator"] == "confluence:12345" and not source["materialized"]
        for source in analysis_payload["leadSources"]
    )
    assert "-.->|mentions|" in analysis_mermaid or "-.->|mentions x" in analysis_mermaid
    assert "not yet materialized" in analysis_mermaid
    assert any(
        node["id"] == "source:confluence:12345"
        and node["kind"] == "source"
        and node["materialized"] is False
        and node["discovered"] is True
        for node in semantic_payload["nodes"]
    )


def test_session_leads_can_focus_one_artifact_by_artifact_locator(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    source = content.materialize_bytes(
        (
            "# ABC-1\n\n"
            "Depends on ABC-2.\n"
            "PR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# ABC-2\n\nDone.\n",
        dirs=dirs,
        preferred_name="ABC-2.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-2",
            "canonical_locator": "jira:ABC-2",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    artifact = content.artifact_locator("ABC-1.md", source.digest)

    monkeypatch.chdir(local_root)

    assert session.main(
        ["leads", "--session", str(local_root), artifact, "--output", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["artifactCount"] == 1
    assert payload["leadCount"] == 2
    assert payload["artifacts"][0]["artifactLocator"] == artifact
    assert any(
        lead["targetLocator"] == "jira:ABC-2" and lead["materialized"] is True
        for lead in payload["artifacts"][0]["leads"]
    )
    assert any(
        lead["targetLocator"] == "https://github.com/acme/widgets/pull/7"
        and lead["materialized"] is False
        for lead in payload["artifacts"][0]["leads"]
    )


def test_session_leads_orders_best_first_without_quality_thresholds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"# ABC-1\n\nDepends on ABC-2.\n",
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# Search Results\n\nhttps://kubernetes.io/docs/reference/networking/virtual-ips/\n",
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "search ABC",
            "canonical_locator": "slack:search ABC",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert session.main(["leads", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["leadCount"] == 2
    assert payload["shownCount"] == 2
    assert payload["materializedLeadCount"] == 0
    assert payload["unmaterializedLeadCount"] == 2
    assert [lead["locator"] for lead in payload["leadSources"]] == [
        "jira:ABC-2",
        "https://kubernetes.io/docs/reference/networking/virtual-ips/",
    ]
    assert payload["leadSources"][0]["firstParty"] is True
    assert payload["leadSources"][0]["materialized"] is False
    assert payload["leadSources"][1]["firstParty"] is False
    assert payload["leadSources"][1]["materialized"] is False
    assert payload["leadSources"][1]["searchLikeSourceCount"] == 1
    search_artifact = next(
        artifact
        for artifact in payload["artifacts"]
        if artifact["sourceLocator"] == "slack:search ABC"
    )
    direct_artifact = next(
        artifact
        for artifact in payload["artifacts"]
        if artifact["sourceLocator"] == "jira:ABC-1"
    )
    assert payload["leadSources"][1]["bestSearchRank"] == 1
    assert payload["leadSources"][1]["searchOrigins"] == [
        {
            "artifactLocator": search_artifact["artifactLocator"],
            "provider": "slack",
            "rank": 1,
            "sourceLocator": "slack:search ABC",
            "subcommand": "search",
        }
    ]
    assert search_artifact["leads"][0]["sourceRank"] == 1
    assert direct_artifact["leads"][0]["sourceRank"] == 0
    assert "minQuality" not in payload
    assert "suppressedLeadCount" not in payload


def test_session_leads_shows_low_signal_only_case_without_hiding_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"See also https://kubernetes.io/docs/reference/networking/virtual-ips/\n",
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "search ABC",
            "canonical_locator": "slack:search ABC",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert session.main(["leads", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["leadCount"] == 1
    assert payload["shownCount"] == 1
    assert payload["leadSources"][0]["locator"] == "https://kubernetes.io/docs/reference/networking/virtual-ips/"
    assert payload["leadSources"][0]["firstParty"] is False
    assert payload["leadSources"][0]["materialized"] is False
    assert payload["leadSources"][0]["bestSearchRank"] == 1
    assert payload["leadSources"][0]["searchOrigins"] == [
        {
            "artifactLocator": payload["artifacts"][0]["artifactLocator"],
            "provider": "slack",
            "rank": 1,
            "sourceLocator": "slack:search ABC",
            "subcommand": "search",
        }
    ]
    assert payload["nextStep"] == ""


def test_session_leads_preserve_search_result_order_within_search_artifacts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "1. https://kubernetes.io/docs/reference/networking/virtual-ips/\n"
            "2. https://docs.python.org/3/library/pathlib.html\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "search networking",
            "canonical_locator": "confluence:search networking",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert session.main(["leads", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [lead["locator"] for lead in payload["leadSources"]] == [
        "https://kubernetes.io/docs/reference/networking/virtual-ips/",
        "https://docs.python.org/3/library/pathlib.html",
    ]
    assert payload["leadSources"][0]["bestSearchRank"] == 1
    assert payload["leadSources"][1]["bestSearchRank"] == 2
    assert payload["leadSources"][0]["searchOrigins"][0]["provider"] == "confluence"
    assert payload["leadSources"][0]["searchOrigins"][0]["subcommand"] == "search"


def test_session_leads_falls_back_to_same_provider_search_for_prose_heavy_confluence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "# Network configuration for debugging devices remotely\n\n"
            "- URL: https://example.atlassian.net/wiki/pages/viewpage.action?pageId=4456054785\n\n"
            "The RMP client or the MeshLink will have a tunnel over cellular connectivity.\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="4456054785.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 4456054785",
            "canonical_locator": "confluence:4456054785",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert session.main(["leads", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    locators = {lead["locator"] for lead in payload["leadSources"]}
    assert "confluence:search RMP MeshLink" in locators
    confluence_lead = next(
        lead
        for lead in payload["leadSources"]
        if lead["locator"] == "confluence:search RMP MeshLink"
    )
    assert confluence_lead["firstParty"] is True
    assert confluence_lead["materialized"] is False
    assert confluence_lead["searchSeed"] is True
    assert confluence_lead["relationKinds"] == ["suggests_search"]


def test_session_leads_falls_back_to_workspace_scoped_slack_search_for_semantic_threads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "### Slack Thread: RMP retrospective\n\n"
            "- _Source_: https://demo.slack.com/archives/C12345678/p1773085070240949\n\n"
            "- _2026-03-13 14:05:26.726_ **operator**: "
            "@teammate is always elevated as the one who reined in RMP "
            "via the edge services change lol\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="p1773085070240949.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "get C12345678:1773085070.240949",
            "canonical_locator": "slack:thread:C12345678:1773085070240949",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert session.main(["leads", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    locators = {lead["locator"] for lead in payload["leadSources"]}
    assert "slack:search --workspace demo RMP services" in locators
    slack_lead = next(
        lead
        for lead in payload["leadSources"]
        if lead["locator"] == "slack:search --workspace demo RMP services"
    )
    assert slack_lead["firstParty"] is True
    assert slack_lead["materialized"] is False
    assert slack_lead["searchSeed"] is True
    assert slack_lead["relationKinds"] == ["suggests_search"]


def test_extract_explicit_leads_strips_wrapped_url_noise() -> None:
    mentions = leads.extract_explicit_leads(
        "PR: <https://github.com/acme/widgets/pull/7|PR 7>\n"
        "Issue: https://github.com/acme/widgets/issues/9)*\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://github.com/acme/widgets/pull/7" in locators
    assert "https://github.com/acme/widgets/issues/9" in locators
    assert all("|" not in locator for locator in locators)


def test_extract_explicit_leads_drops_partial_atlassian_browse_urls() -> None:
    mentions = leads.extract_explicit_leads(
        "Truncated: <https://example.atlassian.net/browse/AB|AB>\n"
        "Exact: <https://example.atlassian.net/browse/ABC-12|ABC-12>\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "jira:ABC-12" in locators
    assert "https://example.atlassian.net/browse/AB" not in locators


def test_extract_explicit_leads_drops_malformed_atlassian_wiki_embed_urls() -> None:
    mentions = leads.extract_explicit_leads(
        "2100446110108744560547851Untitled Diagram-1773161260589.drawio22"
        "https://example.atlassian.net/wikiUntitled Diagram-1773161260589.drawio01170.5521\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://example.atlassian.net/wikiUntitled" not in locators


def test_extract_explicit_leads_drops_obvious_placeholder_and_asset_urls() -> None:
    mentions = leads.extract_explicit_leads(
        "Badge: https://img.shields.io/badge/build-passing-brightgreen.svg\n"
        "Local example: http://127.0.0.1:7400\n"
        "Placeholder: http://www.example.com:8080\n"
        "Real doc: https://kubernetes.io/docs/reference/networking/virtual-ips/\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://kubernetes.io/docs/reference/networking/virtual-ips/" in locators
    assert "https://img.shields.io/badge/build-passing-brightgreen.svg" not in locators
    assert "http://127.0.0.1:7400" not in locators
    assert "http://www.example.com:8080" not in locators


def test_extract_explicit_leads_prefers_root_thread_from_slack_reply_permalink() -> None:
    mentions = leads.extract_explicit_leads(
        "Reply: https://demo.slack.com/archives/C12345678/p1773081279142849?thread_ts=1773075428.384009\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "slack:thread:C12345678:1773075428384009" in locators
    assert "slack:thread:C12345678:1773081279142849" not in locators


def test_extract_explicit_leads_drops_ellipsized_host_only_urls() -> None:
    mentions = leads.extract_explicit_leads(
        "#### Hi there <https://kubernetes.i...\n"
        "Real: https://kubernetes.io/docs/concepts/services-networking/service/\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://kubernetes.i" not in locators
    assert "https://kubernetes.io/docs/concepts/services-networking/service/" in locators


def test_materialize_bytes_eagerly_writes_lead_cache(tmp_path: Path, monkeypatch) -> None:
    dirs = initialize_session(tmp_path / "local")
    result = content.materialize_bytes(
        b"Depends on ABC-2.\nDesign doc: confluence:12345\n",
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    cache_path = result.content_dir / "leads.json"

    assert cache_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["version"] == leads.LEADS_CACHE_VERSION
    assert {entry["canonical_locator"] for entry in payload["entries"]} == {
        "jira:ABC-2",
        "confluence:12345",
    }

    snapshot = content.scan_content_store(dirs.content_dir)[0]

    def fail_extract(_text: str) -> list[leads.LeadMention]:
        raise AssertionError("lead extraction should reuse the eager cache")

    monkeypatch.setattr(leads, "extract_explicit_leads", fail_extract)
    mentions = leads.lead_mentions_for_snapshot(snapshot)

    assert {mention.canonical_locator for mention in mentions} == {
        "jira:ABC-2",
        "confluence:12345",
    }


def test_lead_mentions_for_snapshot_rebuilds_stale_cache(tmp_path: Path) -> None:
    dirs = initialize_session(tmp_path / "local")
    result = content.materialize_bytes(
        b"Depends on ABC-2.\n",
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    cache_path = result.content_dir / "leads.json"
    cache_path.write_text(
        json.dumps({"version": 0, "entries": []}) + "\n",
        encoding="utf-8",
    )

    snapshot = content.scan_content_store(dirs.content_dir)[0]
    mentions = leads.lead_mentions_for_snapshot(snapshot)

    assert [mention.canonical_locator for mention in mentions] == ["jira:ABC-2"]
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["version"] == leads.LEADS_CACHE_VERSION
    assert payload["leadCount"] == 1


def test_session_manifest_has_native_summary_surface(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "demo",
            "canonical_locator": "demo",
            "actor": "claude",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert session.main(
        ["manifest", "--session", str(local_root), "--actor", "claude", "--output", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1
    assert payload["entries"][0]["actor"] == "claude"
    assert payload["entries"][0]["canonical_locator"] == "demo"
    assert payload["entries"][0]["follow_command"] == "gotta read 'demo'"
    assert payload["entries"][0]["content_locator"].startswith("content:")
    assert payload["entries"][0]["artifact_locator"].startswith("artifact:demo.md@")
    assert payload["entries"][0]["content_follow_command"].startswith("gotta read 'content:")
    assert payload["entries"][0]["artifact_follow_command"].startswith("gotta read 'artifact:demo.md@")


def test_session_manifest_accepts_stdout_flag_for_uniformity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "demo",
            "canonical_locator": "demo",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert session.main(["manifest", "--session", str(local_root), "--output", "json", "--stdout"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1


def test_session_manifest_falls_back_to_content_locator_when_canonical_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    result = content.materialize_bytes(
        b"hello world",
        dirs=dirs,
        preferred_name="demo.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "",
            "canonical_locator": "",
            "actor": "claude",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert session.main(
        ["manifest", "--session", str(local_root), "--actor", "claude", "--output", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"][0]["follow_command"] == f"gotta read 'content:{result.digest}'"
    assert payload["entries"][0]["content_locator"] == f"content:{result.digest}"
    assert payload["entries"][0]["artifact_locator"] == content.artifact_locator("demo.md", result.digest)


def test_session_timeline_has_native_continuity_surface(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"first",
        dirs=dirs,
        preferred_name="first.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"second",
        dirs=dirs,
        preferred_name="second.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "3904339970",
            "canonical_locator": "confluence:3904339970",
            "actor": "claude",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    assert session.main(["timeline", "--session", str(local_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "acquired"
    assert payload["eventCount"] == 2
    assert payload["events"][0]["mode"] == "acquired"
    assert payload["events"][0]["follow_command"] == "gotta read 'jira:PROJ-1'"
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][0]["content_locator"].startswith("content:")
    assert payload["events"][0]["artifact_locator"].startswith("artifact:first.md@")
    assert payload["events"][1]["actor"] == "claude"


def test_session_timeline_accepts_stdout_flag_for_uniformity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"first",
        dirs=dirs,
        preferred_name="first.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert session.main(["timeline", "--session", str(local_root), "--output", "json", "--stdout"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eventCount"] == 1


def test_session_timeline_acquired_includes_native_local_activity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="PROJ-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.append_activity_event(
        local_root,
        {
            "timestamp": "2026-03-11T00:00:01Z",
            "plugin": "logs",
            "surface": "logs",
            "action": "append",
            "locator": "LOGS.md",
            "preferred_name": "LOGS.md",
            "follow_command": "gotta read 'LOGS.md'",
            "detail": "appended 1 logs entry",
            "time_field": "session_recorded_at",
        },
    )
    assert session.main(["timeline", "--session", str(local_root), "--output", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["activityPath"].endswith("state/activity.jsonl")
    assert payload["eventCount"] == 2
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][1]["locator"] == "LOGS.md"
    assert payload["events"][1]["event_kind"] == "local"
    assert payload["events"][1]["fetched_at"] == "2026-03-11T00:00:01Z"


def test_session_timeline_labels_local_surface_snapshots_as_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    local_root.joinpath("GOAL.md").write_text("# Goal\n", encoding="utf-8")

    assert session.main(["timeline", "--session", str(local_root), "--output", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    goal_event = next(event for event in payload["events"] if event["locator"] == "GOAL.md")
    assert goal_event["plugin"] == "session"
    assert goal_event["mode"] == "local"
    assert goal_event["follow_command"] == "gotta read 'GOAL.md'"


def test_session_timeline_best_effort_mode_prefers_created_and_surfaces_gaps(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="jira.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "created": "2026-03-09T09:00:00Z",
            "updated": "2026-03-10T12:00:00Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"readme body",
        dirs=dirs,
        preferred_name="README.md",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "locator": "https://github.com/example/repo",
            "canonical_locator": "https://github.com/example/repo",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "best-effort", "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "best-effort"
    assert payload["coverageGapCount"] == 1
    assert payload["eventCount"] == 1
    assert payload["events"][0]["source_time_field"] == "source_created_at"
    assert payload["events"][0]["source_time"].startswith("2026-03-09T09:00:00")
    assert payload["events"][0]["source_created_at"].startswith("2026-03-09T09:00:00")
    assert payload["events"][0]["source_updated_at"].startswith("2026-03-10T12:00:00")


def test_session_timeline_best_effort_includes_local_activity_with_explicit_provenance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="jira.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "created": "2026-03-09T09:00:00Z",
            "updated": "2026-03-10T12:00:00Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.append_activity_event(
        local_root,
        {
            "timestamp": "2026-03-11T00:00:01Z",
            "plugin": "todo",
            "surface": "todo",
            "action": "append",
            "locator": "TODO.md",
            "preferred_name": "TODO.md",
            "follow_command": "gotta read 'TODO.md'",
            "detail": "appended 1 TODO item",
            "time_field": "session_recorded_at",
        },
    )
    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "best-effort", "--output", "json"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["coverageGapCount"] == 0
    assert payload["eventCount"] == 2
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][0]["source_time_field"] == "source_created_at"
    assert payload["events"][1]["locator"] == "TODO.md"
    assert payload["events"][1]["source_time_field"] == "session_recorded_at"
    assert payload["events"][1]["event_kind"] == "local"


def test_session_timeline_created_and_updated_modes_split_cleanly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="jira.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "created": "2026-03-09T09:00:00Z",
            "updated": "2026-03-10T12:00:00Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert session.main(["timeline", "--session", str(local_root), "--mode", "created", "--output", "json"]) == 0
    created_payload = json.loads(capsys.readouterr().out)
    assert created_payload["mode"] == "created"
    assert created_payload["coverageGapCount"] == 0
    assert created_payload["events"][0]["source_time_field"] == "source_created_at"
    assert created_payload["events"][0]["source_time"].startswith("2026-03-09T09:00:00")

    assert session.main(["timeline", "--session", str(local_root), "--mode", "updated", "--output", "json"]) == 0
    updated_payload = json.loads(capsys.readouterr().out)
    assert updated_payload["mode"] == "updated"
    assert updated_payload["coverageGapCount"] == 0
    assert updated_payload["events"][0]["source_time_field"] == "source_updated_at"
    assert updated_payload["events"][0]["source_time"].startswith("2026-03-10T12:00:00")

def test_session_timeline_source_mode_ignores_local_reads_and_schema_probes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="jira.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "created": "2026-03-09T09:00:00Z",
            "updated": "2026-03-10T12:00:00Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"goal body",
        dirs=dirs,
        preferred_name="GOAL.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "GOAL.md",
            "canonical_locator": "GOAL.md",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    content.materialize_bytes(
        b"schema body",
        dirs=dirs,
        preferred_name="workspace.tsv",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "slack:sql --workspace demo --output tsv PRAGMA table_info(MESSAGE)",
            "canonical_locator": "slack:sql --workspace demo --output tsv PRAGMA table_info(MESSAGE)",
        },
        timestamp="2026-03-11T00:00:02.000001Z",
    )
    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "best-effort", "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverageGapCount"] == 0
    assert payload["eventCount"] == 1
    assert payload["events"][0]["locator"] == "jira:PROJ-1"


def test_session_timeline_best_effort_ignores_local_artifact_rereads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    source = content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="jira.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "created": "2026-03-09T09:00:00Z",
            "updated": "2026-03-10T12:00:00Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# Jira\n\nsnippet\n",
        dirs=dirs,
        preferred_name="jira-head.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": f"artifact:jira.md@{source.digest[:12]} --head 20",
            "canonical_locator": f"artifact:jira.md@{source.digest[:12]} --head 20",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "best-effort", "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverageGapCount"] == 0
    assert payload["eventCount"] == 1
    assert payload["events"][0]["locator"] == "jira:PROJ-1"


def test_session_timeline_best_effort_ignores_aggregate_search_artifact_dates(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"# Search Results\n\n- hit\n",
        dirs=dirs,
        preferred_name="gdocs-search-abc.md",
        metadata={
            "tool": "gotta",
            "plugin": "gdocs",
            "subcommand": "search",
            "locator": "search --limit 8 ABC",
            "canonical_locator": "gdocs:search --limit 8 ABC",
            "source_created_at": "2025-10-03T18:43:19.172Z",
            "source_updated_at": "2026-02-25T19:24:20.112Z",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "best-effort", "--output", "json"]
        )
        == 0
    )
    best_effort_payload = json.loads(capsys.readouterr().out)
    assert best_effort_payload["coverageGapCount"] == 0
    assert best_effort_payload["eventCount"] == 0

    assert (
        session.main(
            ["timeline", "--session", str(local_root), "--mode", "created", "--output", "json"]
        )
        == 0
    )
    created_payload = json.loads(capsys.readouterr().out)
    assert created_payload["coverageGapCount"] == 0
    assert created_payload["eventCount"] == 0


def test_session_manifest_plugin_filter_sees_provider_attributed_read_artifacts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    dispatch._materialize_invocation(
        "read",
        ["jira:PROJ-1"],
        content.CommonOptions(),
        b"# PROJ-1\n\n- Created: 2026-03-10T12:00:00Z\n",
        dirs=dirs,
    )
    assert session.main(["manifest", "--session", str(local_root), "--plugin", "jira", "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1
    assert payload["entries"][0]["plugin"] == "jira"
    assert payload["entries"][0]["canonical_locator"] == "jira:PROJ-1"


def test_session_analyze_does_not_treat_same_name_cross_provider_as_revision(
    tmp_path: Path
) -> None:
    dirs = initialize_session(tmp_path / "local")
    content.materialize_bytes(
        b"jira body",
        dirs=dirs,
        preferred_name="ABC.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"confluence body",
        dirs=dirs,
        preferred_name="ABC.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "3904339970",
            "canonical_locator": "confluence:3904339970",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    payload = session._analysis_payload(dirs)

    assert payload["revisionEdgeCount"] == 0
    provider_sets = {tuple(item["providers"]) for item in payload["content"]}
    assert ("confluence",) in provider_sets
    assert ("jira",) in provider_sets


def test_session_analyze_marks_same_name_collisions_with_resource_hints(
    tmp_path: Path
) -> None:
    dirs = initialize_session(tmp_path / "local")
    content.materialize_bytes(
        b"thread one",
        dirs=dirs,
        preferred_name="shared-thread.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "get https://example.slack.com/archives/C1/p111",
            "canonical_locator": "slack:thread:C1:111",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"thread two",
        dirs=dirs,
        preferred_name="shared-thread.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "get https://example.slack.com/archives/C1/p222",
            "canonical_locator": "slack:thread:C1:222",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    payload = session._analysis_payload(dirs)

    assert payload["nameCollisionCount"] == 1
    assert payload["nameCollisions"] == ["shared-thread.md"]
    assert all(item["nameCollision"] for item in payload["content"])
    assert any(
        any("slack-thread:" in hint for hint in item["resourceHints"])
        for item in payload["content"]
    )

    mermaid = session._render_analysis_mermaid(payload)
    assert "slack-thread:C1:111" in mermaid or "slack-thread:C1:222" in mermaid
