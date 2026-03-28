from __future__ import annotations

from datetime import timedelta
import io
import json
import os
from pathlib import Path

import pytest

from gotta.actors import ACTOR_CALLEE_ENV, ACTOR_SPEAKER_ENV
from gotta.compat import UTC, datetime
from gotta import content
import gotta.dispatch.main as dispatch
from gotta.friction import oops_records
import gotta.lead.cache as lead_cache
import gotta.lead.extract as lead_extract
import gotta.lead.model as lead_model
from gotta.logs import append_log_record, log_records, render_logs_markdown
from gotta import main as cli
from gotta import stored
from gotta import topology
from gotta.actor import SESSION_ACTOR_ENV
from gotta.notes import (
    actor_notes_records,
    append_actor_note,
    render_actor_notes_markdown,
)
from gotta.session import bootstrap as session_bootstrap
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import status as session_status
from gotta import todo as session_todo
from gotta.plugins import goal
from gotta.plugins import logs
from gotta.plugins import notes
from gotta.plugins import actor
from gotta.plugins.session import analyze as session_analyze
from gotta.plugins.session import graph as session_graph
from gotta.plugins.session import main as session_main
from gotta.plugins.session import parse as session_parse
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
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)


def make_dirs(root: Path) -> content.ResolvedDirs:
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    content.ensure_private_dir(dirs.session_dir)
    content.ensure_private_dir(dirs.content_dir)
    return dirs


def initialize_session(root: Path) -> content.ResolvedDirs:
    dirs = make_dirs(root)
    content.write_state_env(dirs)
    return dirs


def _init_session(root: Path, capsys) -> None:
    assert session_main.main(["init", "--session", str(root)]) == 0
    assert capsys.readouterr().out.strip() == str(root.resolve())


def _bind_actors(root: Path, capsys, *actors: str) -> str:
    assert actor.main(["bind", *actors, "--session", str(root)]) == 0
    return capsys.readouterr().out


def _actor_id(root: Path, actor_ref: str) -> str:
    return session_registry._resolve_bound_actor_name(root, actor_ref)


def _actor_root(root: Path, actor_ref: str) -> Path:
    return session_registry._actor_session_dir(root, _actor_id(root, actor_ref))


def _session_follow(session_root: Path, target: str) -> str:
    return (
        f"gotta read --session {content.sh_quote(str(session_root))} "
        f"{content.sh_quote(target)}"
    )


@pytest.mark.parametrize("command", ["show", "doctor"])
def test_session_commands_require_bootstrap(
    tmp_path: Path, monkeypatch, capsys, command: str
) -> None:
    default_root = tmp_path / "default"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", default_root)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        session_main.main([command])

    assert excinfo.value.code == 2
    assert "start or bind a session first" in capsys.readouterr().err


def test_session_init_bootstraps_state_env(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "session-root"

    assert session_main.main(["init", "--session", str(root)]) == 0

    assert capsys.readouterr().out.strip() == str(root.resolve())
    assert (root / "state" / "env").exists()
    assert (root / "state").is_dir()


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
    for name in ("WANT.md", "GOAL.md"):
        assert (root / name).exists()
    assert "_empty_" in (root / "WANT.md").read_text(encoding="utf-8")
    assert "Mission seed" not in (root / "WANT.md").read_text(encoding="utf-8")


def test_exact_root_scaffolds_local_metadata_content_and_actor_surfaces(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "workspace"
    shared_root = content.shared_session_root(root.name)

    _init_session(root, capsys)

    assert session_main.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload[content.SESSION_ENV] == str(root.resolve())
    assert payload[content.CONTENT_ENV] == str((root / "content").resolve())
    assert payload[content.SESSION_ACTOR_ENV] == ""
    assert payload[content.STATE_DIR_ENV] == str((root / "state").resolve())
    state = content.load_state_env_at_root(root)
    assert state.get(content.SESSION_ACTOR_ENV, "") == ""
    assert (root / "content").is_dir()
    assert not (root / "content").is_symlink()
    assert not (root / "session.json").exists()
    assert not (shared_root / "session.json").exists()

    output = _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "Claude")
    actor_root = root / "actors" / claude

    assert actor_root.is_dir()
    assert str(actor_root / "WANT.md") in output
    assert not (shared_root / "actors").exists()
    assert (root / "session.json").is_file()

    assert session_main.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[content.SESSION_ENV] == str(root.resolve())
    assert payload[content.SESSION_ACTOR_ENV] == ""
    assert payload[content.STATE_DIR_ENV] == str((root / "state").resolve())
    state = content.load_state_env_at_root(root)
    assert state.get(content.SESSION_ACTOR_ENV, "") == ""

    assert want.main(["--session", str(root)]) == 0
    actorless_output = capsys.readouterr().out
    assert actorless_output == (root / "WANT.md").read_text(encoding="utf-8")

    assert want.main(["--session", str(root), "--actor", "Claude"]) == 0
    actor_output = capsys.readouterr().out
    assert "Actor Want Placeholder" in actor_output
    assert "Claude" in actor_output

    metadata = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert metadata["members"] == [claude]
    assert claude in metadata["actors"]


def test_exact_root_bind_payload_and_binding_record_stay_actorless(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "workspace"

    _init_session(root, capsys)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert session_main.main(["bind", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    binding_id = content.session_token("thread-123")
    record = topology.load_binding_record(binding_id)

    assert payload["sessionRoot"] == str(root.resolve())
    assert payload["sessionDir"] == str(root.resolve())
    assert payload["actor"] == ""
    assert record is not None
    assert record["actor"] == ""


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


def test_session_bootstrap_todo_teaches_bind_precedes_actor_charter_rewrites(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    texts = [str(item["text"]) for item in session_todo.todo_items(root)]
    assert any(
        "actor-targeted `gotta want|goal --actor ...` rewrites only become addressable after that bind completes"
        in text
        for text in texts
    )


def test_want_and_goal_support_show_and_session_relative_from_file(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"
    charter_dir = root / "charters"
    charter_dir.mkdir(parents=True)
    want_source = charter_dir / "want.txt"
    goal_source = charter_dir / "goal.txt"
    want_source.write_text("# Want\n\nTrack retry ownership.\n", encoding="utf-8")
    goal_source.write_text(
        "# Goal\n\nMaterialize one strong source first.\n", encoding="utf-8"
    )

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

    monkeypatch.setattr(
        session_charter.sys, "stdin", io.StringIO("# Want\n\nStay focused.\n")
    )
    assert want.main(["--session", str(root), "--stdin"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(
        session_charter.sys, "stdin", io.StringIO("# Goal\n\nLand one anchor.\n")
    )
    assert goal.main(["--session", str(root), "--stdin"]) == 0
    capsys.readouterr()

    assert (root / "WANT.md").read_text(encoding="utf-8") == "# Want\n\nStay focused.\n"
    assert (root / "GOAL.md").read_text(
        encoding="utf-8"
    ) == "# Goal\n\nLand one anchor.\n"


def test_want_and_goal_reject_inline_positional_text(tmp_path: Path, capsys) -> None:
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

    assert (
        want.main(["--session", str(actor_root), "--from-file", "charters/want.txt"])
        == 0
    )
    capsys.readouterr()
    assert (
        goal.main(["--session", str(actor_root), "--from-file", "charters/goal.txt"])
        == 0
    )
    capsys.readouterr()

    assert (actor_root / "WANT.md").read_text(
        encoding="utf-8"
    ) == "Actor-specific intent\n"
    assert (actor_root / "GOAL.md").read_text(
        encoding="utf-8"
    ) == "Actor-specific goal\n"


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
    assert "This bind completed the addressable actor target" in output
    assert "minimal actor-local canonical state" in output
    assert "actor-local notes/logs/oops surfaces" not in output
    for actor_name in (claude, codex):
        actor_root = session_registry._actor_session_dir(root, actor_name)
        assert actor_root.exists()
        assert (actor_root / "state" / "todo.jsonl").is_file()
        assert session_registry._read_actor_state(root, actor_name)["status"] == "bound"


def test_actor_launch_blockers_emit_native_actor_charter_commands(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")

    blockers = session_status._actor_launch_blockers(root, actor_name="claude")

    assert any(
        f"gotta want --actor {claude} --stdin" in blocker for blocker in blockers
    )
    assert any(
        f"gotta goal --actor {claude} --stdin" in blocker for blocker in blockers
    )


def test_actor_launch_blockers_report_linked_actor_paths(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")

    blockers = session_status._actor_launch_blockers(root, actor_name="claude")

    assert any(
        str(session_registry._actor_session_dir(root, claude) / "WANT.md") in blocker
        for blocker in blockers
    )
    assert any(
        str(session_registry._actor_session_dir(root, claude) / "GOAL.md") in blocker
        for blocker in blockers
    )


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
        actor.session_status,
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
    monkeypatch.setattr(
        actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread()
    )

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


def test_actor_launch_records_immediate_launcher_heartbeat(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    actor_root = _actor_root(root, "claude")

    class FakeProc:
        pid = 4242
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self) -> int:
            return 0

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            if timeout is None:
                return None
            return None

    monkeypatch.setattr(
        actor.session_status,
        "_actor_launch_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(actor, "_actor_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(
        actor,
        "_spawn_actor_process",
        lambda *_args, **_kwargs: FakeProc(),
    )
    monkeypatch.setattr(
        actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread()
    )

    assert actor.main(["launch", claude, "--session", str(root)]) == 0
    captured = capsys.readouterr()

    records = actor_notes_records(actor_root, claude)
    assert records[-1]["author"] == actor.LAUNCHER_AUTHOR
    assert records[-1]["message"] == actor.LAUNCHER_HEARTBEAT_NOTE
    payload = session_status._actor_status_payload(actor_root, claude)
    assert payload["notes_status"] == "present"
    assert payload["voice"] == "setup"
    assert payload["launched_at"]
    assert payload.get("launched_by", "") == ""
    assert "already is the actor" in captured.err
    assert "Do not pair another agent into this actor" in captured.err


def test_actor_launch_records_passive_launcher_provenance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Scout")
    claude = _actor_id(root, "claude")
    scout = _actor_id(root, "scout")

    class FakeProc:
        pid = 4242
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self) -> int:
            return 0

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            if timeout is None:
                return None
            return None

    monkeypatch.setattr(
        actor.session_status,
        "_actor_launch_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(actor, "_actor_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(
        actor,
        "_spawn_actor_process",
        lambda *_args, **_kwargs: FakeProc(),
    )
    monkeypatch.setattr(
        actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread()
    )
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, scout)

    assert actor.main(["launch", claude, "--session", str(root)]) == 0
    capsys.readouterr()

    payload = session_status._actor_status_payload(root, claude)
    assert payload["launched_by"] == scout
    assert payload["launched_at"]


def test_actor_launch_consumes_feedback_directives_and_updates_actor_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    actor_root = _actor_root(root, "claude")

    class FakeProc:
        pid = 4242

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                "ordinary stdout\n"
                + (
                    '@@gotta {"actor":"%s","surface":"notes","message":"first durable heartbeat note"}\n'
                    % claude
                )
                + (
                    '@@gotta {"actor":"%s","surface":"logs","message":"hydrated slack thread root"}\n'
                    % claude
                )
            )
            self.stderr = io.StringIO(
                "ordinary stderr\n"
                + (
                    '@@gotta {"actor":"%s","surface":"oops","message":"reply permalink lost thread context"}'
                    % claude
                )
            )

        def wait(self) -> int:
            return 0

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            if timeout is None:
                return None
            return None

    monkeypatch.setattr(
        actor.session_status,
        "_actor_launch_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(actor, "_actor_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(
        actor,
        "_spawn_actor_process",
        lambda *_args, **_kwargs: FakeProc(),
    )
    monkeypatch.setattr(
        actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread()
    )

    assert actor.main(["launch", claude, "--session", str(root)]) == 0
    captured = capsys.readouterr()
    status = session_status._actor_status_payload(actor_root, claude)
    activity = content.activity_events(actor_root)

    assert "ordinary stdout" in captured.out
    assert "ordinary stderr" in captured.err
    assert "@@gotta" not in captured.out
    assert "@@gotta" not in captured.err
    rendered_notes = render_actor_notes_markdown(
        actor_root,
        claude,
        label=session_registry._actor_label(claude, work_dir=actor_root),
        status_payload=session_status._actor_status_payload(actor_root, claude),
    )
    assert "first durable heartbeat note" in rendered_notes
    assert (
        actor_notes_records(actor_root, claude)[-1]["message"]
        == "first durable heartbeat note"
    )
    assert actor_notes_records(actor_root, claude)[-1]["author"] == claude
    assert (
        "logs directives are disabled; use surface `notes` for heartbeat/anchor/wave/signoff narration"
        in captured.err
    )
    assert not any(
        record["message"] == "hydrated slack thread root" and record["actor"] == claude
        for record in log_records(actor_root)
    )
    assert any(
        record["message"] == f"[{claude}] noted: first durable heartbeat note"
        and record["actor"] == claude
        for record in log_records(actor_root)
    )
    assert any(
        record["message"] == "reply permalink lost thread context"
        and record["actor"] == claude
        for record in oops_records(actor_root)
    )
    assert status["notes_status"] == "present"
    assert status["voice"] == "present"
    assert "heartbeat note now" not in str(status.get("next_step") or "")
    assert not any(
        str(event.get("actor") or "") == claude
        and str(event.get("plugin") or "") == "logs"
        for event in activity
    )
    assert any(
        str(event.get("actor") or "") == claude
        and str(event.get("plugin") or "") == "oops"
        for event in activity
    )


def test_actor_launch_consumes_invalid_feedback_directives_and_warns(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    actor_root = _actor_root(root, "claude")

    class FakeProc:
        pid = 4242

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                "ordinary stdout\n"
                '@@gotta {"actor":"wrong","surface":"notes","message":"bad"}\n'
                '@@gotta {"actor":"%s","surface":"todo","message":"bad"}\n' % claude
            )
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return 0

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            if timeout is None:
                return None
            return None

    monkeypatch.setattr(
        actor.session_status,
        "_actor_launch_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(actor, "_actor_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(
        actor,
        "_spawn_actor_process",
        lambda *_args, **_kwargs: FakeProc(),
    )
    monkeypatch.setattr(
        actor, "_with_heartbeat", lambda *_args, **_kwargs: FakeThread()
    )

    assert actor.main(["launch", claude, "--session", str(root)]) == 0
    captured = capsys.readouterr()

    assert "ordinary stdout" in captured.out
    assert "@@gotta" not in captured.out
    assert "@@gotta" not in captured.err
    assert captured.err.count("gotta actor feedback ignored:") == 2
    records = actor_notes_records(actor_root, claude)
    assert len(records) == 1
    assert records[0]["author"] == actor.LAUNCHER_AUTHOR
    assert records[0]["message"] == actor.LAUNCHER_HEARTBEAT_NOTE
    assert oops_records(actor_root) == []


def test_actor_status_empty_guidance_points_to_actor_bind(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    assert actor.main(["status", "--session", str(root)]) == 0
    output = capsys.readouterr().out
    assert "no actors bound for this session" in output
    assert "gotta actor bind Claude" in output


def test_actor_bind_without_actor_teaches_canonical_usage(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    with pytest.raises(SystemExit) as exc:
        actor.main(["bind", "--session", str(root)])
    err = str(exc.value)

    assert "missing actor for `gotta actor bind`" in err
    assert "gotta actor bind Claude" in err


def test_actor_bind_output_json_returns_structured_bindings(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    assert (
        actor.main(["bind", "Scout", "--session", str(root), "--output", "json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["sessionRoot"] == str(root.resolve())
    assert len(payload["bindings"]) == 1
    binding = payload["bindings"][0]
    assert binding["label"] == "Scout"
    assert binding["created"] is True
    assert binding["alreadyBound"] is False
    assert binding["status"] == "bound"
    assert binding["actorRoot"] == str((root / "actors" / binding["actor"]).resolve())
    assert binding["wantPath"] == str(
        (root / "actors" / binding["actor"] / "WANT.md").resolve()
    )
    assert binding["goalPath"] == str(
        (root / "actors" / binding["actor"] / "GOAL.md").resolve()
    )
    assert binding["wantCommand"] == f"gotta want --actor {binding['actor']} --stdin"
    assert binding["goalCommand"] == f"gotta goal --actor {binding['actor']} --stdin"
    assert binding["todoCommand"] == f"gotta todo --actor {binding['actor']}"
    assert binding["launchCommand"] == (
        f"gotta actor launch {binding['actor']} --session {content.sh_quote(str(root))}"
    )
    assert binding["message"].startswith(f"bound {binding['actor']} (Scout) session")


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
    session_bootstrap.scaffold_session(actor_root)
    (root / "session.json").write_text(
        json.dumps({"session_id": root.name, "members": []}) + "\n",
        encoding="utf-8",
    )

    assert actor.main(["status", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert discovered in payload


def test_notes_show_defaults_to_all_bound_actors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        notes.main(["append", "codex note", "--actor", codex, "--session", str(root)])
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert (
        notes.main(["append", "claude note", "--actor", claude, "--session", str(root)])
        == 0
    )
    capsys.readouterr()

    assert notes.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["actor_count"] == 2
    assert codex in payload["actors"]
    assert claude in payload["actors"]
    assert {entry["actor"] for entry in payload["entries"]} == {codex, claude}


def test_notes_append_infers_single_bound_actor_without_flag(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "Claude")
    monkeypatch.setenv(SESSION_ACTOR_ENV, claude)

    assert notes.main(["append", "ambient note", "--session", str(root)]) == 0
    output = capsys.readouterr().out

    assert f"appended actor note for {claude}" in output
    record = actor_notes_records(root, claude)[-1]
    assert record["author"] == claude
    assert record["message"] == "ambient note"


def test_notes_append_on_actor_root_authors_as_target_actor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "Claude")
    actor_root = _actor_root(root, "Claude")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)

    assert notes.main(["append", "actor-root note", "--session", str(actor_root)]) == 0
    capsys.readouterr()

    record = actor_notes_records(actor_root, claude)[-1]
    assert record["actor"] == claude
    assert record["author"] == claude
    assert record["message"] == "actor-root note"


def test_notes_show_on_actor_root_defaults_to_session_wide(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")
    actor_root = _actor_root(root, "Claude")

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert notes.main(["append", "claude note", "--session", str(actor_root)]) == 0
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        notes.main(["append", "codex note", "--session", str(root), "--actor", codex])
        == 0
    )
    capsys.readouterr()

    assert notes.main(["show", "--session", str(actor_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["actor_count"] == 2
    assert set(payload["actors"]) == {claude, codex}
    assert {entry["author"] for entry in payload["entries"]} == {claude, codex}


def test_notes_append_infers_ambient_bound_actor_without_flag(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")

    monkeypatch.setenv(content.SESSION_ENV, str(root))
    monkeypatch.setenv(SESSION_ACTOR_ENV, claude)

    assert notes.main(["append", "ambient shared-root note"]) == 0
    output = capsys.readouterr().out

    assert f"appended actor note for {claude}" in output
    record = actor_notes_records(root, claude)[-1]
    assert record["author"] == claude
    assert record["message"] == "ambient shared-root note"


def test_notes_append_requires_explicit_actor_when_multiple_bound_actors(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["append", "ambiguous note", "--session", str(root)])

    assert "actor note append is ambiguous" in str(excinfo.value)


def test_notes_show_unbound_actor_fails_without_materializing_surface(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["show", "--actor", "claude", "--session", str(root)])

    assert "claude is not bound for this session" in str(excinfo.value)
    assert not session_registry._actor_session_dir(root, "claude").exists()


def test_actor_status_filters_with_actor_flag(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "Claude")

    assert (
        actor.main(
            ["status", "--session", str(root), "--actor", claude, "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert list(payload) == [claude]


def test_actor_status_defaults_to_all_bound_actors_from_actor_root(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")
    actor_root = _actor_root(root, "Claude")

    assert actor.main(["status", "--session", str(actor_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {claude, codex}


def test_logs_show_defaults_to_all_bound_actors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        logs.main(["append", "codex log", "--session", str(root), "--actor", codex])
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert (
        logs.main(["append", "claude log", "--session", str(root), "--actor", claude])
        == 0
    )
    capsys.readouterr()

    assert logs.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["actor_count"] == 2
    assert payload["entry_count"] == 2
    assert {entry["actor"] for entry in payload["entries"]} == {codex, claude}


def test_logs_show_on_actor_root_defaults_to_session_wide(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")
    actor_root = _actor_root(root, "Claude")

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert logs.main(["append", "claude log", "--session", str(actor_root)]) == 0
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        logs.main(["append", "codex log", "--session", str(root), "--actor", codex])
        == 0
    )
    capsys.readouterr()

    assert logs.main(["show", "--session", str(actor_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["actor_count"] == 2
    assert payload["entry_count"] == 2
    assert {entry["actor"] for entry in payload["entries"]} == {claude, codex}


def test_exact_root_charter_surfaces_stay_on_root_without_explicit_actor(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")

    root_goal = root / "GOAL.md"
    root_goal.write_text("# Goal\n\nRoot-specific goal.\n", encoding="utf-8")
    root_want = root / "WANT.md"
    root_want.write_text("# Want\n\nRoot-specific want.\n", encoding="utf-8")
    actor_goal = session_registry._actor_session_dir(root, claude) / "GOAL.md"
    actor_goal.write_text("# Goal\n\nClaude-specific goal.\n", encoding="utf-8")
    actor_want = session_registry._actor_session_dir(root, codex) / "WANT.md"
    actor_want.write_text("# Want\n\nCodex-specific want.\n", encoding="utf-8")

    assert want.main(["--session", str(root)]) == 0
    want_output = capsys.readouterr().out
    assert want_output == root_want.read_text(encoding="utf-8")

    assert goal.main(["--session", str(root)]) == 0
    goal_output = capsys.readouterr().out
    assert goal_output == root_goal.read_text(encoding="utf-8")

    assert want.main(["--session", str(root), "--actor", codex]) == 0
    assert "Codex-specific want." in capsys.readouterr().out

    assert goal.main(["--session", str(root), "--actor", claude]) == 0
    assert "Claude-specific goal." in capsys.readouterr().out


def test_charter_surfaces_on_actor_root_stay_actor_local(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")
    claude_root = _actor_root(root, "Claude")

    (session_registry._actor_session_dir(root, claude) / "GOAL.md").write_text(
        "# Goal\n\nClaude-specific goal.\n",
        encoding="utf-8",
    )
    (session_registry._actor_session_dir(root, codex) / "GOAL.md").write_text(
        "# Goal\n\nCodex-specific goal.\n",
        encoding="utf-8",
    )

    assert goal.main(["--session", str(claude_root)]) == 0
    output = capsys.readouterr().out

    assert "Claude-specific goal." in output
    assert "Codex-specific goal." not in output


def test_explicit_shared_session_charters_default_to_session_id_actor(
    tmp_path: Path, capsys
) -> None:
    shared_root = topology.shared_session_root_for("bacabaa8d03e")
    primary_root = shared_root / "actors" / "bacabaa8d03e"

    _init_session(primary_root, capsys)
    _bind_actors(primary_root, capsys, "Claude")
    claude_root = _actor_root(primary_root, "Claude")
    (primary_root / "WANT.md").write_text(
        "# Want\n\nPrimary actor want.\n", encoding="utf-8"
    )
    (claude_root / "WANT.md").write_text(
        "# Want\n\nClaude sibling want.\n", encoding="utf-8"
    )
    (primary_root / "GOAL.md").write_text(
        "# Goal\n\nPrimary actor goal.\n", encoding="utf-8"
    )
    (claude_root / "GOAL.md").write_text(
        "# Goal\n\nClaude sibling goal.\n", encoding="utf-8"
    )

    assert want.main(["--session", str(shared_root)]) == 0
    want_output = capsys.readouterr().out
    assert "Primary actor want." in want_output
    assert "Claude sibling want." not in want_output

    assert goal.main(["--session", str(shared_root)]) == 0
    goal_output = capsys.readouterr().out
    assert "Primary actor goal." in goal_output
    assert "Claude sibling goal." not in goal_output

    assert (
        session_main.main(["show", "--session", str(shared_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["GOTTA_SESSION_ACTOR"] == "bacabaa8d03e"
    assert payload["GOTTA_SESSION_DIR"] == str(primary_root)
    assert payload["GOTTA_SESSION_STATE_DIR"] == str(primary_root / "state")


def test_explicit_shared_session_actor_rooted_surfaces_require_actor_when_primary_is_ambiguous(
    tmp_path: Path, capsys
) -> None:
    shared_root = topology.shared_session_root_for("shared-session")
    actor_root = shared_root / "actors" / "scout"

    _init_session(actor_root, capsys)
    _bind_actors(actor_root, capsys, "Claude", "Codex")

    with pytest.raises(SystemExit) as want_exc:
        want.main(["--session", str(shared_root)])
    assert "pass `--actor <actor>` explicitly" in str(want_exc.value)

    assert (
        session_main.main(["show", "--session", str(shared_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["GOTTA_SESSION_ACTOR"] == ""
    assert payload["GOTTA_SESSION_DIR"] == str(shared_root)
    assert payload["GOTTA_SESSION_STATE_DIR"] == ""


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


def test_actor_complete_stays_pending_while_runtime_is_live_and_survives_heartbeat(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(root, claude, {"status": "active"})

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
    payload = session_status._actor_status_payload(root, claude)
    assert payload["status"] == "closing"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "completed"
    assert payload.get("summary") in {"", None}

    assert actor.main(["heartbeat", claude, "--session", str(root)]) == 0
    capsys.readouterr()
    heartbeat_payload = session_status._actor_status_payload(root, claude)
    assert heartbeat_payload["status"] == "closing"
    assert heartbeat_payload["requested_pending"] is True


def test_actor_signoff_stays_pending_while_runtime_is_live_and_survives_heartbeat(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(root, claude, {"status": "active"})

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
    payload = session_status._actor_status_payload(root, claude)
    assert payload["status"] == "closing"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "signed_off"
    assert payload.get("signoff_summary") in {"", None}

    assert actor.main(["heartbeat", claude, "--session", str(root)]) == 0
    capsys.readouterr()
    heartbeat_payload = session_status._actor_status_payload(root, claude)
    assert heartbeat_payload["status"] == "closing"
    assert heartbeat_payload["requested_pending"] is True


def test_actor_runtime_exit_finalizes_pending_signoff_authoritatively(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(root, claude, {"status": "active"})

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

    assert (
        actor._finalize_actor_runtime_exit(
            root,
            claude,
            returncode=0,
            finished_at="2026-03-17T00:03:00Z",
        )
        == 0
    )

    payload = session_status._actor_status_payload(root, claude)
    assert payload["status"] == "signed_off"
    assert payload["requested_pending"] is False
    assert payload["signoff_summary"] == "accepted by operator"


def test_actor_status_treats_signoff_timestamp_as_authoritative(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "heartbeat_at": "2026-03-17T00:00:00Z",
            "signoff_at": "2026-03-17T00:01:00Z",
            "signoff_summary": "actor closed itself cleanly",
        },
    )

    payload = session_status._actor_status_payload(root, claude)

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
    session_registry._write_actor_state(root, claude, {"status": "active"})

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
    payload = session_status._actor_status_payload(root, claude)

    assert "authoritative status stays active" in output
    assert payload["status"] == "closing"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "failed"
    notes_text = render_actor_notes_markdown(
        root,
        claude,
        label=session_registry._actor_label(claude, work_dir=root),
        status_payload=payload,
    )
    assert "Supervisor requested `failed` (operator stopped this run)." in notes_text
    assert "pending_disposition: failed: operator stopped this run" in notes_text


def test_actor_stop_stays_pending_while_live_and_notes_render_graceful_warning(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(root, claude, {"status": "active"})

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
    payload = session_status._actor_status_payload(root, claude)

    assert f"recorded stop request for {claude}" in output
    assert payload["status"] == "closing"
    assert payload["requested_pending"] is True
    assert payload["requested_status"] == "signed_off"
    assert payload["requested_mode"] == "stop"
    assert payload["requested_label"] == "stop"
    notes_text = render_actor_notes_markdown(
        root,
        claude,
        label=session_registry._actor_label(claude, work_dir=root),
        status_payload=payload,
    )
    assert (
        "Supervisor requested a graceful stop (finish the current wave and close out)."
        in notes_text
    )
    assert (
        "pending_disposition: stop: finish the current wave and close out" in notes_text
    )


def test_notes_projection_skips_supervisor_warning_for_nonfailed_pending_requests(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "requested_status": "signed_off",
            "requested_summary": "looked done from the operator side",
        },
    )
    notes_text = render_actor_notes_markdown(
        root,
        claude,
        label=session_registry._actor_label(claude, work_dir=root),
        status_payload=session_status._actor_status_payload(root, claude),
    )
    assert "Supervisor requested `failed`" not in notes_text
    assert (
        "pending_disposition: signed off: looked done from the operator side"
        in notes_text
    )


def test_actor_bind_canonical_root_has_no_projection_files(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    assert actor.main(["bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()

    assert (
        session_status._actor_status_payload(root, _actor_id(root, "claude"))["status"]
        == "bound"
    )


def test_actor_status_reports_recent_activity_and_recent_artifacts(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "signed_off",
            "signoff_at": "2026-03-17T00:03:00Z",
            "signoff_summary": "accepted by operator",
        },
    )
    events_path = session_registry._actor_events_path(root, claude)
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
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    append_actor_note(
        root,
        claude,
        message="Wave 2 landed",
        author=claude,
        timestamp="2026-03-17T00:03:00Z",
    )
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    for index, locator in enumerate(
        [
            "jira:DEMO-6292",
            "jira:DEMO-6361",
            "github:search --type pr edge connector acme",
            "jira:search connector",
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
    assert "progress: evidence" in output
    assert "recent_note: 2026-03-17T00:03:00Z Wave 2 landed" in output
    assert (
        "recent_progress: 2026-03-17T00:04:00.000001Z evidence: jira:search connector"
        in output
    )
    assert (
        "recent_lifecycle: 2026-03-17T00:02:00Z signed off: accepted by operator"
        in output
    )
    assert "recent_artifacts:" in output
    assert "`jira:search connector`" in output
    assert "`github:search --type pr edge connector acme`" in output
    assert "`jira:DEMO-6361`" in output
    assert "(+1 more)" in output
    assert "heartbeat" not in output


def test_actor_status_json_separates_progress_from_lifecycle(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(root, claude, {"status": "active"})
    events_path = session_registry._actor_events_path(root, claude)
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
                        "event": "runtime_exit",
                        "detail": "actor process exited with code 0",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    append_actor_note(
        root,
        claude,
        message="Wave 1 landed",
        author=claude,
        timestamp="2026-03-17T00:01:00Z",
    )

    assert (
        actor.main(["status", claude, "--session", str(root), "--output", "json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)[claude]
    assert payload["last_activity_at"] == "2026-03-17T00:01:00Z"
    assert payload["last_activity_summary"] == "Wave 1 landed"
    assert payload["last_note_at"] == "2026-03-17T00:01:00Z"
    assert payload["last_note_summary"] == "Wave 1 landed"
    assert isinstance(payload["notes_stale"], bool)
    assert payload["last_lifecycle_at"] == "2026-03-17T00:01:00Z"
    assert (
        payload["last_lifecycle_summary"]
        == "runtime exit: actor process exited with code 0"
    )
    assert payload["progress_kind"] == "narration"
    assert [item["event"] for item in payload["recent_progress"]] == ["note"]
    assert [item["event"] for item in payload["recent_lifecycle"]] == ["runtime_exit"]
    assert payload["artifact_count"] == 0


def test_actor_status_reports_clean_runtime_exit_after_honoring_stop_request(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    events_path = session_registry._actor_events_path(root, claude)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:01:00Z",
                        "actor": claude,
                        "event": "stop_requested",
                        "detail": "operator requested graceful stop",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-17T00:03:00Z",
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
    append_actor_note(
        root,
        claude,
        message="Reached a stable checkpoint",
        author=claude,
        timestamp="2026-03-17T00:01:00Z",
    )

    assert actor.main(["status", claude, "--session", str(root)]) == 0
    output = capsys.readouterr().out

    assert "recent_note: 2026-03-17T00:01:00Z Reached a stable checkpoint" in output
    assert "recent_progress: 2026-03-17T00:01:00Z Reached a stable checkpoint" in output
    assert (
        "recent_lifecycle: 2026-03-17T00:03:00Z runtime exit: actor process exited cleanly "
        "after honoring graceful stop request" in output
    )


def test_actor_status_highlights_missing_heartbeat_note_for_live_actor(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "started_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    payload = session_status._actor_status_payload(root, claude)
    assert payload["notes_status"] == "empty"
    assert "brief startup window" in payload["next_step"]
    assert "one heartbeat interval" in payload["next_step"]
    assert "short actor-authored note" in payload["next_step"]


def test_actor_status_ignores_actor_log_for_voice_before_note(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    actor_root = _actor_root(root, "claude")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "started_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    append_log_record(
        actor_root,
        message="hydrated the thread root",
        actor=claude,
        timestamp="2026-03-21T00:00:00Z",
    )

    payload = session_status._actor_status_payload(root, claude)

    assert payload["notes_status"] == "empty"
    assert payload["voice"] == "missing"
    assert payload["progress_kind"] == "none"
    assert payload["last_note_at"] == ""
    assert "brief startup window" in payload["next_step"]
    assert "short actor-authored note" in payload["next_step"]


def test_actor_status_marks_live_narration_only_actor_as_low_signal_when_progress_is_stale(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    stale_started = (datetime.now(tz=UTC) - timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stale_heartbeat = (datetime.now(tz=UTC) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "pid": os.getpid(),
            "started_at": stale_started,
            "heartbeat_at": stale_heartbeat,
        },
    )
    append_actor_note(
        root,
        claude,
        message="alive: tracing importer continuity",
        author=claude,
        timestamp="2026-03-21T00:00:00Z",
    )

    payload = session_status._actor_status_payload(root, claude)

    assert payload["still_running"] is True
    assert payload["progress_kind"] == "narration"
    assert payload["progress_stale"] is True
    assert payload["notes_stale"] is True
    assert payload["last_note_summary"] == "alive: tracing importer continuity"
    assert payload["artifact_count"] == 0
    assert "low-signal run" in payload["next_step"]


def test_actor_status_reports_pulse_after_actor_evidence_before_note(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    actor_name = _actor_id(root, "claude")
    session_registry._write_actor_state(
        root,
        actor_name,
        {
            "status": "active",
            "started_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    content.materialize_bytes(
        b"# Evidence\n\nSomething landed.\n",
        dirs=dirs,
        preferred_name="evidence.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "https://example.com/evidence",
            "canonical_locator": "https://example.com/evidence",
            "actor": actor_name,
        },
        timestamp="2026-03-21T00:00:00Z",
    )

    payload = session_status._actor_status_payload(root, actor_name)

    assert payload["status"] == "producing_evidence"
    assert payload["notes_status"] == "empty"
    assert payload["voice"] == "pulse"
    assert "friction or shared evidence" in payload["next_step"]
    assert "first short actor note has not landed yet" in payload["next_step"]


def test_actor_status_requires_durable_note_when_pending_actor_already_has_evidence(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    actor_name = _actor_id(root, "claude")
    session_registry._write_actor_state(root, actor_name, {"status": "pending"})
    content.materialize_bytes(
        b"# Evidence\n\nSomething landed.\n",
        dirs=dirs,
        preferred_name="evidence.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "https://example.com/evidence",
            "canonical_locator": "https://example.com/evidence",
            "actor": actor_name,
        },
        timestamp="2026-03-21T00:00:00Z",
    )

    payload = session_status._actor_status_payload(root, actor_name)
    assert payload["status"] == "pending"
    assert payload["artifact_count"] == 1
    assert payload["notes_status"] == "empty"
    assert payload["voice"] == "pulse"
    assert "Land one short actor-authored note now" in payload["next_step"]


def test_actor_status_guides_pending_actor_with_notes_and_evidence(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    dirs = content.ResolvedDirs(session_dir=root, content_dir=root / "content")
    actor_name = _actor_id(root, "claude")
    session_registry._write_actor_state(root, actor_name, {"status": "pending"})
    notes.append_actor_note(
        root,
        actor_name,
        message="Captured the first durable summary.",
        author=actor_name,
        timestamp="2026-03-21T00:00:00Z",
    )
    content.materialize_bytes(
        b"# Evidence\n\nSomething landed.\n",
        dirs=dirs,
        preferred_name="evidence.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "https://example.com/evidence",
            "canonical_locator": "https://example.com/evidence",
            "actor": actor_name,
        },
        timestamp="2026-03-21T00:00:01Z",
    )

    payload = session_status._actor_status_payload(root, actor_name)
    assert payload["status"] == "pending"
    assert payload["notes_status"] == "present"
    assert payload["artifact_count"] == 1
    assert "new evidence landed after the last short note" in payload["next_step"]
    assert "Land a short note now" in payload["next_step"]


def test_foreign_notes_show_records_checks_and_append_resets_feedback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Scout")
    scout = _actor_id(root, "scout")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "operator-1")

    assert notes.main(["--session", str(root), "--actor", scout]) == 0
    capsys.readouterr()

    status = session_status._actor_status_payload(root, scout)
    assert status["note_checks_since_update"] == 1
    assert status["last_note_check_at"]
    assert status["last_note_check_by"] == "operator-1"

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, scout)
    assert (
        notes.main(
            ["append", "alive: first anchor", "--session", str(root), "--actor", scout]
        )
        == 0
    )
    capsys.readouterr()

    reset_status = session_status._actor_status_payload(root, scout)
    assert reset_status["note_checks_since_update"] == 0
    assert reset_status["last_note_check_at"] == ""
    assert reset_status["last_note_check_by"] == ""


def test_explicit_actor_notes_show_counts_cold_start_supervisor_read(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Scout")
    scout = _actor_id(root, "scout")
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(SESSION_ACTOR_ENV, raising=False)
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)

    assert notes.main(["--session", str(root), "--actor", scout]) == 0
    capsys.readouterr()

    status = session_status._actor_status_payload(root, scout)
    assert status["note_checks_since_update"] == 1
    assert status["last_note_check_at"]
    assert status["last_note_check_by"]
    assert status["last_note_check_by"] != scout


def test_notes_show_does_not_count_self_reads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Scout")
    scout = _actor_id(root, "scout")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, scout)

    assert notes.main(["--session", str(root), "--actor", scout]) == 0
    capsys.readouterr()

    status = session_status._actor_status_payload(root, scout)
    assert status["note_checks_since_update"] == 0
    assert status["last_note_check_at"] == ""
    assert status["last_note_check_by"] == ""


def test_session_wide_notes_show_does_not_increment_actor_note_check_counters(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Scout", "Beacon")
    scout = _actor_id(root, "scout")
    beacon = _actor_id(root, "beacon")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "operator-1")

    assert notes.main(["--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["actor_count"] == 2
    assert (
        session_status._actor_status_payload(root, scout)["note_checks_since_update"]
        == 0
    )
    assert (
        session_status._actor_status_payload(root, beacon)["note_checks_since_update"]
        == 0
    )


def test_actor_status_preserves_note_read_pulse_when_low_signal_progress_is_active(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Scout")
    scout = _actor_id(root, "scout")
    stale_started = (datetime.now(tz=UTC) - timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stale_heartbeat = (datetime.now(tz=UTC) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    append_actor_note(
        root,
        scout,
        message="alive: tracing importer continuity",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )
    session_registry._write_actor_state(
        root,
        scout,
        {
            "status": "active",
            "pid": os.getpid(),
            "started_at": stale_started,
            "heartbeat_at": stale_heartbeat,
            "note_checks_since_update": 2,
            "last_note_check_at": "2026-03-21T00:05:00Z",
            "last_note_check_by": "operator-1",
        },
    )

    payload = session_status._actor_status_payload(root, scout)

    assert (
        "Supervisor has checked this actor's notes 2 times since the last note."
        in payload["next_step"]
    )
    assert "low-signal run" in payload["next_step"]


def test_actor_notes_projection_describes_notes_as_canonical_narration(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")

    rendered = render_actor_notes_markdown(
        root,
        claude,
        label=session_registry._actor_label(claude, work_dir=root),
        status_payload=session_status._actor_status_payload(root, claude),
    )

    assert "canonical actor-authored narration surface" in rendered
    assert "short one-line notes are valid" in rendered
    assert "`gotta logs` remains procedural/system trace" in rendered


def test_logs_projection_describes_logs_as_procedural_trace(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    rendered = render_logs_markdown(root, [])

    assert "procedural/system trace" in rendered
    assert "Prefer `gotta notes ...` for actor-authored narration." in rendered


def test_actor_recent_activity_carries_cross_actor_author(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "claude")
    codex = _actor_id(root, "codex")
    events_path = session_registry._actor_events_path(root, claude)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-03-17T00:03:00Z",
                "actor": claude,
                "author": codex,
                "event": "note",
                "detail": "Wave 2 landed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = session_status._actor_status_payload(root, claude)
    assert payload["recent_activity"][0]["author"] == codex
    assert payload["recent_activity"][0]["summary"] == f"{codex}: Wave 2 landed"
    assert payload["recent_lifecycle"] == []
    assert payload["last_lifecycle_summary"] == ""


def test_actor_status_ignores_foreign_note_for_voice(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    notes.append_actor_note(
        root,
        claude,
        message="foreign pulse",
        author="foreign-123",
        timestamp="2026-03-21T00:00:00Z",
    )

    payload = session_status._actor_status_payload(root, claude)

    assert payload["notes_status"] == "empty"
    assert payload["voice"] == "missing"


def test_foreign_writer_cannot_append_actor_note(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "foreign-123")

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["append", "bad note", "--session", str(root), "--actor", claude])

    assert "bind and launch a sibling actor" in str(excinfo.value)


def test_unbound_shell_cannot_append_actor_note(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(SESSION_ACTOR_ENV, raising=False)
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)
    monkeypatch.setattr(
        content,
        "current_context_binding",
        lambda: type("Binding", (), {"binding_id": ""})(),
    )

    with pytest.raises(SystemExit) as excinfo:
        notes.main(["append", "bad note", "--session", str(root), "--actor", claude])

    assert "bind and launch a sibling actor" in str(excinfo.value)
    assert actor_notes_records(_actor_root(root, claude), claude) == []


def test_foreign_writer_cannot_append_actor_log(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "foreign-123")

    with pytest.raises(SystemExit) as excinfo:
        logs.main(["append", "bad log", "--session", str(root), "--actor", claude])

    assert "bind and launch a sibling actor" in str(excinfo.value)


def test_unbound_shell_cannot_append_actor_log(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(SESSION_ACTOR_ENV, raising=False)
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)
    monkeypatch.setattr(
        content,
        "current_context_binding",
        lambda: type("Binding", (), {"binding_id": ""})(),
    )

    with pytest.raises(SystemExit) as excinfo:
        logs.main(["append", "bad log", "--session", str(root), "--actor", claude])

    assert "bind and launch a sibling actor" in str(excinfo.value)
    assert log_records(_actor_root(root, claude)) == []


def test_peer_actor_log_preserves_peer_author(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "claude")
    codex = _actor_id(root, "codex")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)

    assert (
        logs.main(["append", "peer log", "--session", str(root), "--actor", claude])
        == 0
    )
    capsys.readouterr()

    assert log_records(_actor_root(root, claude))[-1]["actor"] == codex


def test_session_bind_can_switch_active_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setattr(cli, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "legacy-mission"]) == 0
    payload = dict(
        line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines()
    )
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
    payload = dict(
        line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines()
    )
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
    assert not (
        registry / cli._session_token("thread-123") / "actors" / claude
    ).exists()


def test_session_show_works_from_initialized_session_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)

    monkeypatch.chdir(local_root)

    assert session_main.main(["show"]) == 0
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

    assert session_main.main([argv[0], "--session", str(local_root), *argv[1:]]) == 0

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

    assert session_main.main([]) == 0
    assert capsys.readouterr().out.strip() == str(local_root.resolve())


def test_session_doctor_reports_live_codex_runtime_and_matching_binding(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    binding_id = content.session_token("thread-123")
    topology.write_binding(
        binding_id,
        local_root,
        context_id="thread-123",
        context_source="codex_thread",
        session_id=content.session_id(local_root),
        actor=content.session_identity(local_root),
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )

    assert (
        session_main.main(["doctor", "--session", str(local_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["contextId"] == "thread-123"
    assert payload["runtime"]["contextSource"] == "codex_thread"
    assert payload["runtime"]["bindingId"] == binding_id
    assert payload["bindings"][0]["contextId"] == "thread-123"
    assert payload["checks"]["durableBindingsPresent"]["status"] == "ok"
    assert payload["checks"]["runtimeBindingMatchesTarget"]["status"] == "ok"
    assert payload["checks"]["sessionTopologyConsistent"]["status"] == "ok"


def test_session_doctor_reports_historical_binding_when_runtime_points_elsewhere(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.setenv("CODEX_THREAD_ID", "other-thread")
    topology.write_binding(
        content.session_token("historic-thread"),
        local_root,
        context_id="historic-thread",
        context_source="codex_thread",
        session_id=content.session_id(local_root),
        actor=content.session_identity(local_root),
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )

    assert (
        session_main.main(["doctor", "--session", str(local_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["contextId"] == "other-thread"
    assert payload["bindings"][0]["contextId"] == "historic-thread"
    assert payload["checks"]["durableBindingsPresent"]["status"] == "ok"
    assert payload["checks"]["runtimeBindingMatchesTarget"]["status"] == "mismatch"


def test_session_doctor_ignores_invalid_binding_record_instead_of_crashing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    binding_id = content.session_token("thread-123")
    binding_dir = topology.binding_path_for(binding_id)
    binding_dir.mkdir(parents=True, exist_ok=True)
    topology.binding_root_path_for(binding_id).symlink_to(
        os.path.relpath(local_root.resolve(), start=binding_dir.resolve()),
    )
    topology.binding_record_path_for(binding_id).write_text("{bad", encoding="utf-8")

    assert (
        session_main.main(["doctor", "--session", str(local_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["contextId"] == "thread-123"
    assert payload["checks"]["durableBindingsPresent"]["status"] == "missing"
    assert payload["bindings"] == []


def test_session_doctor_matches_durable_bindings_at_shared_session_boundary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    _bind_actors(local_root, capsys, "Claude")
    claude_root = _actor_root(local_root, "claude")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    binding_id = content.session_token("thread-123")
    topology.write_binding(
        binding_id,
        local_root,
        context_id="thread-123",
        context_source="codex_thread",
        session_id=content.session_id(local_root),
        actor=content.session_identity(local_root),
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )

    assert (
        session_main.main(["doctor", "--session", str(claude_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["bindings"][0]["bindingId"] == binding_id
    assert payload["checks"]["durableBindingsPresent"]["status"] == "ok"
    assert payload["checks"]["runtimeBindingMatchesTarget"]["status"] == "ok"
    assert payload["checks"]["sessionTopologyConsistent"]["status"] == "ok"


def test_session_doctor_does_not_merge_unrelated_local_sessions_with_same_name(
    tmp_path: Path, capsys
) -> None:
    left_root = tmp_path / "left" / "session"
    right_root = tmp_path / "right" / "session"
    initialize_session(left_root)
    initialize_session(right_root)
    topology.write_binding(
        "left-binding",
        left_root,
        context_id="ctx-left",
        context_source="codex_thread",
        session_id=content.session_id(left_root),
        actor=content.session_identity(left_root),
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )

    assert (
        session_main.main(["doctor", "--session", str(right_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["bindings"] == []
    assert payload["checks"]["durableBindingsPresent"]["status"] == "missing"


def test_session_doctor_ignores_binding_with_missing_root_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    binding_id = content.session_token("thread-123")
    topology.write_binding(
        binding_id,
        local_root,
        context_id="thread-123",
        context_source="codex_thread",
        session_id=content.session_id(local_root),
        actor=content.session_identity(local_root),
        created_at="2026-03-22T00:00:00Z",
        updated_at="2026-03-22T00:00:00Z",
    )
    topology.binding_root_path_for(binding_id).unlink()

    assert (
        session_main.main(["doctor", "--session", str(local_root), "--output", "json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["bindings"] == []
    assert payload["checks"]["durableBindingsPresent"]["status"] == "missing"
    assert payload["checks"]["sessionTopologyConsistent"]["status"] == "broken"


def test_actor_launch_rejects_live_closing_actor(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude")
    claude = _actor_id(root, "claude")
    for path in (
        root / "WANT.md",
        root / "GOAL.md",
        _actor_root(root, claude) / "WANT.md",
        _actor_root(root, claude) / "GOAL.md",
    ):
        path.write_text("# Ready\n\nintentional\n", encoding="utf-8")
    session_registry._write_actor_state(
        root,
        claude,
        {
            "status": "active",
            "requested_status": "signed_off",
            "requested_summary": "done",
        },
    )

    with pytest.raises(SystemExit) as excinfo:
        actor.main(["launch", claude, "--session", str(root)])

    assert f"{claude} is already closing" in str(excinfo.value)


def test_session_analyze_writes_summary_and_graph(
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

    assert session_main.main(["analyze", "--session", str(local_root)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("session:")
    assert captured.err == ""
    assert not (local_root / "summary.json").exists()
    assert not (local_root / "graph.json").exists()
    assert not (local_root / "graph.mmd").exists()
    assert not (local_root / "graph.mmd.md").exists()
    assert not (local_root / "semantic-graph.json").exists()
    assert not (local_root / "semantic-graph.mmd").exists()
    assert not (local_root / "semantic-graph.mmd.md").exists()
    assert result.data_path.name == "data"


def test_session_analyze_output_json_returns_combined_payload_by_default(
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(["analyze", "--session", str(local_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "all"
    assert payload["focus"] == ""
    assert "lineage" in payload
    assert "semantic" in payload
    assert payload["lineage"]["contentCount"] == 1
    assert payload["semantic"]["nodeCount"] >= 1


def test_session_analyze_output_mermaid_requires_explicit_mode(
    capsys,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        session_main.main(["analyze", "--output", "mermaid"])

    assert excinfo.value.code == 2
    assert (
        "`--output mermaid` requires `--mode lineage` or `--mode semantic`"
        in capsys.readouterr().err
    )


def test_session_analyze_output_mermaid_prints_raw_lineage_mermaid(
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--mode",
                "lineage",
                "--output",
                "mermaid",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert output.startswith("---\ntitle: gotta session analysis\n---\nflowchart LR\n")


def test_session_analyze_output_markdown_bundles_lineage_and_semantic_for_all_mode(
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "markdown",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert output.startswith("# gotta session analyze\n\nSession: `")
    assert "## Anchor Shortlist" in output
    assert "## Lineage Preview" in output
    assert "## Lead Preview" in output
    assert "```mermaid" not in output


def test_session_analyze_receipt_keeps_stdout_pure_for_mermaid(
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--mode",
                "lineage",
                "--output",
                "mermaid",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert captured.out.startswith(
        "---\ntitle: gotta session analysis\n---\nflowchart LR\n"
    )
    assert captured.err == ""
    assert not (local_root / "summary.json").exists()
    assert not (local_root / "graph.mmd").exists()


def test_session_analyze_build_parser_rejects_stdout_flag() -> None:
    with pytest.raises(SystemExit):
        session_parse.build_parser().parse_args(["analyze", "--stdout"])


def test_session_analyze_build_parser_rejects_receipt_flag() -> None:
    with pytest.raises(SystemExit):
        session_parse.build_parser().parse_args(["analyze", "--receipt"])


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

    payload = session_graph.graph_payload(dirs)

    assert payload["sourceCount"] == 1
    assert payload["contentCount"] == 1
    assert payload["edgeCount"] == 2
    assert payload["sources"] == [
        {
            "locator": "jira:PROJ-3960",
            "followCommand": "gotta read 'jira:PROJ-3960'",
            "contentCount": 1,
            "artifactKind": "",
            "artifactKinds": [],
            "collision": False,
            "variant": False,
            "variantCount": 0,
            "variants": [],
            "visibility_level": "restricted",
            "visibility_boundary": "same_company",
            "visibility_confidence": "medium",
            "visibility_basis": [
                "provider=jira",
                "subcommand=default",
                "classification=authenticated_jira_surface",
            ],
        }
    ]
    assert payload["content"][0]["contentLocator"] == "content:abc123"
    assert payload["content"][0]["artifactLocator"] == "artifact:PROJ-3960.md@abc123"
    assert (
        payload["content"][0]["followCommand"]
        == "gotta read 'artifact:PROJ-3960.md@abc123'"
    )


def test_session_empty_graph_and_analyze_make_empty_state_explicit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    monkeypatch.chdir(tmp_path)

    assert session_main.main(["graph", "--session", str(local_root)]) == 0
    graph_output = capsys.readouterr().out
    assert "No materialized artifacts yet." in graph_output
    assert "gotta read &lt;locator&gt;" in graph_output

    assert (
        session_main.main(["graph", "--session", str(local_root), "--output", "json"])
        == 0
    )
    graph_payload = json.loads(capsys.readouterr().out)
    assert graph_payload["empty"] is True
    assert graph_payload["nextStep"].startswith("No materialized artifacts yet.")

    assert session_main.main(["analyze", "--session", str(local_root)]) == 0
    analyze_output = capsys.readouterr().out
    assert "No materialized artifacts yet." in analyze_output
    assert "gotta read <locator>" in analyze_output


def test_session_discovery_only_graph_and_analyze_surface_need_for_evidence(
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
            "artifact_kind": "discovery",
            "locator": "search platform",
            "canonical_locator": "slack:search platform",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(tmp_path)

    assert (
        session_main.main(["graph", "--session", str(local_root), "--output", "json"])
        == 0
    )
    graph_payload = json.loads(capsys.readouterr().out)
    assert graph_payload["empty"] is False
    assert graph_payload["discoveryArtifactCount"] == 1
    assert graph_payload["evidenceArtifactCount"] == 0
    assert graph_payload["nextStep"].startswith("Discovery artifacts are present")
    assert graph_payload["content"][0]["artifactKind"] == "discovery"

    assert session_main.main(["analyze", "--session", str(local_root)]) == 0
    analyze_output = capsys.readouterr().out
    assert (
        "Discovery artifacts are present, but no evidence artifacts exist yet."
        in analyze_output
    )
    assert (
        "focus: use `gotta session analyze --focus <locator|keyword>" in analyze_output
    )


def test_session_graph_filter_prunes_to_matching_subgraph_and_supports_text_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"# Generic Item\n\nReference.\n",
        dirs=dirs,
        preferred_name="generic-item.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "artifact_kind": "evidence",
            "locator": "get GEN-1",
            "canonical_locator": "jira:GEN-1",
            "actor": "generic-actor",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# Generic Page\n\nReference.\n",
        dirs=dirs,
        preferred_name="generic-page.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "artifact_kind": "evidence",
            "locator": "get 202",
            "canonical_locator": "confluence:202",
            "actor": "generic-actor",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(tmp_path)

    assert (
        session_main.main(
            [
                "graph",
                "--session",
                str(local_root),
                "--filter",
                "jira:GEN-(1|9)",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["filter"] == "jira:GEN-(1|9)"
    assert payload["sourceCount"] == 1
    assert payload["contentCount"] == 1
    assert payload["edgeCount"] == 1
    assert payload["sources"][0]["locator"] == "jira:GEN-1"

    assert (
        session_main.main(
            [
                "graph",
                "--session",
                str(local_root),
                "--filter",
                "jira:GEN-(1|9)",
                "--output",
                "text",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert "filter 'jira:GEN-(1|9)'" in rendered
    assert "top providers:" in rendered
    assert "jira:GEN-1" in rendered
    assert "confluence:202" not in rendered


def test_session_manifest_and_timeline_text_surface_hotspots_first(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"# GEN-1\n\nReference.\n",
        dirs=dirs,
        preferred_name="GEN-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "artifact_kind": "evidence",
            "locator": "get GEN-1",
            "canonical_locator": "jira:GEN-1",
            "actor": "generic-actor",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# GEN-2\n\nReference.\n",
        dirs=dirs,
        preferred_name="GEN-2.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "artifact_kind": "evidence",
            "locator": "get GEN-2",
            "canonical_locator": "jira:GEN-2",
            "actor": "generic-actor",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    content.materialize_bytes(
        b"# Generic Page\n\nReference.\n",
        dirs=dirs,
        preferred_name="generic-page.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "artifact_kind": "discovery",
            "locator": "get 202",
            "canonical_locator": "confluence:202",
            "actor": "other-actor",
        },
        timestamp="2026-03-11T00:00:02.000001Z",
    )
    monkeypatch.chdir(tmp_path)

    assert session_main.main(["manifest", "--session", str(local_root)]) == 0
    manifest_output = capsys.readouterr().out
    assert "top plugins:" in manifest_output
    assert "jira: 2" in manifest_output
    assert "top actors:" in manifest_output
    assert "generic-actor: 2" in manifest_output

    assert session_main.main(["timeline", "--session", str(local_root)]) == 0
    timeline_output = capsys.readouterr().out
    assert "top plugins:" in timeline_output
    assert "jira: 2" in timeline_output
    assert "top actors:" in timeline_output
    assert "generic-actor: 2" in timeline_output


def test_session_analyze_defaults_to_text_overview_with_middle_sections(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"# Search\n\nSee also jira:ABC-1\n",
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "search ABC-1",
            "canonical_locator": "slack:search ABC-1",
            "subcommand": "search",
            "artifact_kind": "discovery",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert session_main.main(["analyze", "--session", str(local_root)]) == 0

    output = capsys.readouterr().out
    assert "provider clusters:" in output
    assert "dominant relations:" in output
    assert "materialized anchors:" in output
    assert "best leads:" in output
    assert "focus: use `gotta session analyze --focus <locator|keyword>" in output


def test_session_analyze_focus_surfaces_local_neighborhood(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    issue = content.materialize_bytes(
        (
            "# ABC-1\n\nDepends on ABC-2.\nPR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "artifact_kind": "evidence",
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--focus",
                "ABC-1.md",
            ]
        )
        == 0
    )
    text_output = capsys.readouterr().out
    assert "matched: ABC-1.md (content, content)" in text_output
    assert "neighbors:" in text_output
    assert "ABC-2 (source, jira;" in text_output

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "json",
                "--mode",
                "semantic",
                "--focus",
                issue.digest[:8],
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["root"]["kind"] == "content"
    assert payload["root"]["materialized"] is True
    assert payload["root"]["followCommand"].startswith(
        f"gotta read --session {content.sh_quote(str(local_root))} 'artifact:ABC-1.md@"
    )


def test_session_analyze_focus_respects_lineage_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "# ABC-1\n\nDepends on ABC-2.\nPR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "artifact_kind": "evidence",
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "json",
                "--mode",
                "lineage",
                "--focus",
                "jira:ABC-1",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["root"]["kind"] == "source"
    assert payload["root"]["label"] == "jira:ABC-1"
    assert "nodes" not in payload
    assert "sources" in payload
    assert any(item["locator"] == "jira:ABC-1" for item in payload["sources"])
    assert any(item["preferredName"] == "ABC-1.md" for item in payload["content"])
    assert any(item["visibility_level"] == "restricted" for item in payload["content"])
    assert payload["leadSourceCount"] == len(payload["leadSources"])


def test_session_analyze_focus_can_match_projected_corpus_without_label_hits(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"Generic synthetic body describing example connector worker behavior.\n",
        dirs=dirs,
        preferred_name="artifact-a.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"Generic synthetic body describing example connector importer behavior.\n",
        dirs=dirs,
        preferred_name="artifact-b.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 12345",
            "canonical_locator": "confluence:12345",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "json",
                "--mode",
                "semantic",
                "--focus",
                "example connector",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert payload["matchedCount"] >= 2
    assert payload["root"]["kind"] == "content"
    matched_labels = {
        payload["root"]["label"],
        *(item["label"] for item in payload["anchors"]),
    }
    assert "artifact-a.md" in matched_labels
    assert "artifact-b.md" in matched_labels


def test_session_analyze_lineage_focus_keeps_search_provenance_without_unrelated_siblings(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "- [acme/relevant:infra/syntheticconcept.tf]"
            "(https://github.com/acme/relevant/blob/main/infra/syntheticconcept.tf)\n"
            "- [psf/black:docs/overview.md](https://github.com/psf/black)\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="github-search-code-syntheticconcept.json",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "locator": "search --type code SyntheticConcept",
            "canonical_locator": "github:search --type code SyntheticConcept",
            "artifact_kind": "discovery",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b'resource "demo" "syntheticconcept" {}\n',
        dirs=dirs,
        preferred_name="syntheticconcept.tf",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "locator": "https://github.com/acme/relevant/blob/main/infra/syntheticconcept.tf",
            "canonical_locator": "https://github.com/acme/relevant/blob/main/infra/syntheticconcept.tf",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    content.materialize_bytes(
        b'resource "demo" "plain" {}\n',
        dirs=dirs,
        preferred_name="plain.tf",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "locator": "https://github.com/psf/black",
            "canonical_locator": "https://github.com/psf/black",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:02.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "json",
                "--mode",
                "lineage",
                "--focus",
                "SyntheticConcept",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched"] is True
    assert any(
        item["locator"]
        == "https://github.com/acme/relevant/blob/main/infra/syntheticconcept.tf"
        for item in payload["sources"]
    )
    assert all(
        item["locator"] != "https://github.com/psf/black" for item in payload["sources"]
    )
    assert all(
        item["locator"] != "https://github.com/psf/black"
        for item in payload["leadSources"]
    )


def test_session_analyze_all_mode_focus_returns_combined_outputs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    issue = content.materialize_bytes(
        (
            "# ABC-1\n\nDepends on ABC-2.\nPR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "artifact_kind": "evidence",
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
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--focus",
                issue.digest[:8],
            ]
        )
        == 0
    )
    text_output = capsys.readouterr().out
    assert text_output.startswith("## Lineage\n\nsession:")
    assert "\n## Semantic\n\nsession:" in text_output

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "json",
                "--focus",
                issue.digest[:8],
            ]
        )
        == 0
    )
    json_payload = json.loads(capsys.readouterr().out)
    assert json_payload["mode"] == "all"
    assert json_payload["lineage"]["matched"] is True
    assert json_payload["semantic"]["matched"] is True

    with pytest.raises(SystemExit) as excinfo:
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "mermaid",
                "--focus",
                issue.digest[:8],
            ]
        )
    assert excinfo.value.code == 2

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "markdown",
                "--focus",
                issue.digest[:8],
            ]
        )
        == 0
    )
    markdown_output = capsys.readouterr().out
    assert markdown_output.startswith("# gotta session analyze\n\nSession: `")
    assert "\nFocus: `" in markdown_output
    assert "\n## Lineage Focus\n" in markdown_output
    assert "\n## Semantic Focus\n" in markdown_output
    assert "```mermaid" not in markdown_output


def test_session_analyze_mode_markdown_stays_dossier_not_mermaid(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "# ABC-1\n\nDepends on ABC-2.\nPR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "markdown",
                "--mode",
                "lineage",
            ]
        )
        == 0
    )
    lineage_output = capsys.readouterr().out
    assert lineage_output.startswith("# gotta session analyze\n\nSession: `")
    assert "\n## Materialized Sources\n" in lineage_output
    assert "```mermaid" not in lineage_output

    assert (
        session_main.main(
            [
                "analyze",
                "--session",
                str(local_root),
                "--output",
                "markdown",
                "--mode",
                "semantic",
            ]
        )
        == 0
    )
    semantic_output = capsys.readouterr().out
    assert semantic_output.startswith("# gotta session analyze\n\nSession: `")
    assert "\n## Provider Clusters\n" in semantic_output
    assert "```mermaid" not in semantic_output


def test_session_scan_searches_projected_materialized_corpus(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    monkeypatch.setattr(
        stored,
        "html_markdown",
        lambda _data: b"# Example Heading\n\nGeneric synthetic body.\n",
    )
    content.materialize_bytes(
        b"<h1>Example Heading</h1><p>Generic synthetic body.</p>",
        dirs=dirs,
        preferred_name="3925246070.html",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 3925246070",
            "canonical_locator": "confluence:3925246070",
            "artifact_kind": "evidence",
            "content_type": "text/html",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b'{"title":"Other branch"}',
        dirs=dirs,
        preferred_name="DO-1.json",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get DO-1",
            "canonical_locator": "jira:DO-1",
            "artifact_kind": "discovery",
            "content_type": "application/json",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    assert (
        session_main.main(
            [
                "scan",
                "Example",
                "--session",
                str(local_root),
                "--plugin",
                "confluence",
                "--kind",
                "evidence",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["entryCount"] == 1
    assert payload["pluginFilter"] == "confluence"
    assert payload["kindFilter"] == "evidence"
    entry = payload["entries"][0]
    assert entry["canonical_locator"] == "confluence:3925246070"
    assert entry["artifactKind"] == "evidence"
    assert entry["hitCount"] == 1
    assert entry["followCommand"] == _session_follow(
        local_root, "confluence:3925246070"
    )
    assert entry["artifactFollowCommand"].startswith(
        f"gotta read --session {content.sh_quote(str(local_root))} 'artifact:3925246070.html@"
    )
    assert any(
        line["text"] == "# Example Heading" for line in entry["snippets"][0]["lines"]
    )


def test_session_scan_rejects_invalid_regex_even_without_entries(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)

    with pytest.raises(SystemExit) as excinfo:
        session_main.main(
            [
                "scan",
                "[",
                "--match",
                "regex",
                "--session",
                str(local_root),
            ]
        )

    assert "invalid scan pattern:" in str(excinfo.value.code)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["manifest", "--output", "json"],
        ["timeline", "--output", "json"],
        ["graph", "--output", "json"],
        ["leads", "--output", "json"],
    ],
)
def test_session_aggregate_filters_reject_invalid_regex_even_without_entries(
    tmp_path: Path,
    capsys,
    argv: list[str],
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)

    with pytest.raises(SystemExit) as excinfo:
        session_main.main(
            [argv[0], "--session", str(local_root), "--filter", "[", *argv[1:]]
        )

    assert "invalid filter pattern:" in str(excinfo.value.code)
    assert capsys.readouterr().out == ""


def test_session_manifest_falls_back_to_jira_visibility_when_snapshot_metadata_is_unknown(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"# OPS-1\n\nBody.\n",
        dirs=dirs,
        preferred_name="OPS-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "subcommand": "get",
            "locator": "get OPS-1",
            "canonical_locator": "jira:OPS-1",
            "visibility_level": "unknown",
            "visibility_boundary": "unknown",
            "visibility_confidence": "low",
            "visibility_basis": [
                "provider=jira",
                "classification=insufficient_evidence",
            ],
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            ["manifest", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"][0]["visibility_level"] == "restricted"
    assert payload["entries"][0]["visibility_boundary"] == "same_company"
    assert payload["entries"][0]["visibility_confidence"] == "medium"


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

    graph_payload = session_graph.graph_payload(dirs)
    analysis_payload = session_analyze.analysis_payload(dirs)

    assert graph_payload["sources"][0]["collision"] is False
    assert graph_payload["sources"][0]["variant"] is True
    assert graph_payload["sources"][0]["variantCount"] == 2
    assert analysis_payload["collisionCount"] == 0
    assert analysis_payload["variantCount"] == 1
    assert analysis_payload["revisionEdgeCount"] == 0
    assert analysis_payload["sources"][0]["variant"] is True
    assert analysis_payload["sources"][0]["variantCount"] == 2

    mermaid = session_analyze.render_analysis_mermaid(analysis_payload)
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

    analysis_payload = session_analyze.analysis_payload(dirs)

    assert analysis_payload["collisionCount"] == 0
    assert analysis_payload["variantCount"] == 0
    assert analysis_payload["duplicateMaterializationCount"] == 1
    assert analysis_payload["duplicateMaterializations"] == [url]
    assert analysis_payload["sources"][0]["duplicateMaterialization"] is True
    assert analysis_payload["sources"][0]["contentCount"] == 2

    mermaid = session_analyze.render_analysis_mermaid(analysis_payload)
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
            "visibility_level": "restricted",
            "visibility_boundary": "same_company",
            "visibility_confidence": "high",
            "visibility_basis": ["provider=jira", "policy=test"],
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

    analysis_payload = session_analyze.analysis_payload(dirs)
    semantic_payload = session_analyze.semantic_payload(dirs)
    analysis_mermaid = session_analyze.render_analysis_mermaid(analysis_payload)

    assert analysis_payload["leadSourceCount"] == 3
    assert any(
        source["locator"] == "jira:ABC-1"
        and source["visibility_level"] == "restricted"
        and source["visibility_boundary"] == "same_company"
        for source in analysis_payload["sources"]
    )
    assert analysis_payload["materializedLeadSourceCount"] == 1
    assert analysis_payload["unmaterializedLeadSourceCount"] == 2
    assert analysis_payload["leadEdgeCount"] == 3
    assert any(
        edge["targetLocator"] == "jira:ABC-2" for edge in analysis_payload["leadEdges"]
    )
    assert any(
        edge["targetLocator"] == "confluence:12345"
        for edge in analysis_payload["leadEdges"]
    )
    assert any(
        edge["targetLocator"] == "https://github.com/acme/widgets/pull/7"
        for edge in analysis_payload["leadEdges"]
    )
    assert any(
        source["locator"] == "jira:ABC-2" and source["materialized"]
        for source in analysis_payload["leadSources"]
    )
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
            "# ABC-1\n\nDepends on ABC-2.\nPR: https://github.com/acme/widgets/pull/7\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="ABC-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get ABC-1",
            "canonical_locator": "jira:ABC-1",
            "visibility_level": "restricted",
            "visibility_boundary": "same_company",
            "visibility_confidence": "high",
            "visibility_basis": ["provider=jira", "policy=test"],
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

    assert (
        session_main.main(
            ["leads", "--session", str(local_root), artifact, "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["artifactCount"] == 1
    assert payload["leadCount"] == 2
    assert payload["artifacts"][0]["artifactLocator"] == artifact
    assert payload["artifacts"][0]["visibility_level"] == "restricted"
    assert payload["artifacts"][0]["visibility_boundary"] == "same_company"
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

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
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


def test_session_leads_filter_filters_surviving_leads_without_reordering_them(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "# GEN-1\n\nDepends on GEN-2.\nRunbook: https://docs.example.test/runbook\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="GEN-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get GEN-1",
            "canonical_locator": "jira:GEN-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "leads",
                "--session",
                str(local_root),
                "--filter",
                "GEN-2|runbook",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["filter"] == "GEN-2|runbook"
    assert payload["leadCount"] == 2
    assert payload["artifactCount"] == 1
    assert [lead["locator"] for lead in payload["leadSources"]] == [
        "jira:GEN-2",
        "https://docs.example.test/runbook",
    ]


def test_session_leads_filter_no_match_guides_toward_corpus_search(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"Runbook: https://docs.example.test/runbook\n",
        dirs=dirs,
        preferred_name="GEN-1.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "get GEN-1",
            "canonical_locator": "jira:GEN-1",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert (
        session_main.main(
            [
                "leads",
                "--session",
                str(local_root),
                "--filter",
                "slack:thread:missing",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["leadCount"] == 0
    assert payload["nextStep"] == (
        "No leads matched the current filter. Use `gotta session scan <query>` "
        "when you need corpus-wide search instead of field-level lead filtering."
    )


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

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["leadCount"] == 1
    assert payload["shownCount"] == 1
    assert (
        payload["leadSources"][0]["locator"]
        == "https://kubernetes.io/docs/reference/networking/virtual-ips/"
    )
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


def test_session_leads_demote_low_signal_service_urls_but_keep_them_visible(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "Service root: https://admin.demo.internal\n"
            "Auth: https://login.demo.internal/.well-known/jwks.json\n"
            "Docs: https://kubernetes.io/docs/reference/networking/virtual-ips/\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "search service routing",
            "canonical_locator": "slack:search service routing",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    monkeypatch.chdir(local_root)

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert [lead["locator"] for lead in payload["leadSources"]] == [
        "https://kubernetes.io/docs/reference/networking/virtual-ips/",
        "https://admin.demo.internal",
        "https://login.demo.internal/.well-known/jwks.json",
    ]


def test_session_leads_support_offset_and_all_paging(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        (
            "https://docs.demo.internal/runbook\n"
            "https://admin.demo.internal\n"
            "https://grafana.demo.internal/d/abc123/service-overview\n"
        ).encode("utf-8"),
        dirs=dirs,
        preferred_name="search.md",
        metadata={
            "tool": "gotta",
            "plugin": "slack",
            "locator": "search service routing",
            "canonical_locator": "slack:search service routing",
            "subcommand": "search",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    assert (
        session_main.main(
            [
                "leads",
                "--session",
                str(local_root),
                "--limit",
                "1",
                "--offset",
                "1",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["leadCount"] == 3
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert len(payload["leadSources"]) == 1

    assert (
        session_main.main(
            [
                "leads",
                "--session",
                str(local_root),
                "--offset",
                "1",
                "--all",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 2
    assert payload["offset"] == 1
    assert payload["nextOffset"] is None
    assert payload["truncated"] is False
    assert len(payload["leadSources"]) == 2


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

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
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
            "The gateway client or the example connector will have a tunnel over cellular connectivity.\n"
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

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    locators = {lead["locator"] for lead in payload["leadSources"]}
    assert "confluence:search Network configuration debugging" in locators
    confluence_lead = next(
        lead
        for lead in payload["leadSources"]
        if lead["locator"] == "confluence:search Network configuration debugging"
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
            "### Slack Thread: connector retrospective\n\n"
            "- _Source_: https://demo.slack.com/archives/C12345678/p1773085070240949\n\n"
            "- _2026-03-13 14:05:26.726_ **operator**: "
            "@teammate is always elevated as the one who reined in connector "
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

    assert (
        session_main.main(["leads", "--session", str(local_root), "--output", "json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    locators = {lead["locator"] for lead in payload["leadSources"]}
    assert "slack:search --workspace demo connector retrospective" in locators
    slack_lead = next(
        lead
        for lead in payload["leadSources"]
        if lead["locator"] == "slack:search --workspace demo connector retrospective"
    )
    assert slack_lead["firstParty"] is True
    assert slack_lead["materialized"] is False
    assert slack_lead["searchSeed"] is True
    assert slack_lead["relationKinds"] == ["suggests_search"]


def test_extract_explicit_leads_strips_wrapped_url_noise() -> None:
    mentions = lead_extract.extract_explicit_leads(
        "PR: <https://github.com/acme/widgets/pull/7|PR 7>\n"
        "Issue: https://github.com/acme/widgets/issues/9)*\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://github.com/acme/widgets/pull/7" in locators
    assert "https://github.com/acme/widgets/issues/9" in locators
    assert all("|" not in locator for locator in locators)


def test_extract_explicit_leads_collapses_duplicated_markdown_urls() -> None:
    mentions = lead_extract.extract_explicit_leads(
        "Granola: https://notes.granola.ai/t/demo](https://notes.granola.ai/t/demo)\n"
    )

    assert [mention.canonical_locator for mention in mentions] == [
        "https://notes.granola.ai/t/demo"
    ]


def test_extract_explicit_leads_trims_trailing_quote_and_normalizes_shortlinks() -> (
    None
):
    mentions = lead_extract.extract_explicit_leads(
        "Auth: https://login.demo.internal'\n"
        "Page: https://example.atlassian.net/wiki/x/1J0AAA\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://login.demo.internal" in locators
    assert "confluence:40404" in locators


def test_extract_explicit_leads_drops_partial_atlassian_browse_urls() -> None:
    mentions = lead_extract.extract_explicit_leads(
        "Truncated: <https://example.atlassian.net/browse/AB|AB>\n"
        "Exact: <https://example.atlassian.net/browse/ABC-12|ABC-12>\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "jira:ABC-12" in locators
    assert "https://example.atlassian.net/browse/AB" not in locators


def test_extract_explicit_leads_drops_malformed_atlassian_wiki_embed_urls() -> None:
    mentions = lead_extract.extract_explicit_leads(
        "2100446110108744560547851Untitled Diagram-1773161260589.drawio22"
        "https://example.atlassian.net/wikiUntitled Diagram-1773161260589.drawio01170.5521\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://example.atlassian.net/wikiUntitled" not in locators


def test_extract_explicit_leads_drops_obvious_placeholder_and_asset_urls() -> None:
    mentions = lead_extract.extract_explicit_leads(
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


def test_extract_explicit_leads_prefers_root_thread_from_slack_reply_permalink() -> (
    None
):
    mentions = lead_extract.extract_explicit_leads(
        "Reply: https://demo.slack.com/archives/C12345678/p1773081279142849?thread_ts=1773075428.384009\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "slack:thread:C12345678:1773075428384009" in locators
    assert "slack:thread:C12345678:1773081279142849" not in locators


def test_extract_explicit_leads_drops_ellipsized_host_only_urls() -> None:
    mentions = lead_extract.extract_explicit_leads(
        "#### Hi there <https://kubernetes.i...\n"
        "Real: https://kubernetes.io/docs/concepts/services-networking/service/\n"
    )

    locators = {mention.canonical_locator for mention in mentions}
    assert "https://kubernetes.i" not in locators
    assert (
        "https://kubernetes.io/docs/concepts/services-networking/service/" in locators
    )


def test_materialize_bytes_eagerly_writes_lead_cache(
    tmp_path: Path, monkeypatch
) -> None:
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
    assert payload["version"] == lead_model.LEADS_CACHE_VERSION
    assert {entry["canonical_locator"] for entry in payload["entries"]} == {
        "jira:ABC-2",
        "confluence:12345",
    }

    snapshot = content.scan_content_store(dirs.content_dir)[0]

    def fail_extract(_text: str) -> list[lead_model.LeadMention]:
        raise AssertionError("lead extraction should reuse the eager cache")

    monkeypatch.setattr(lead_cache, "extract_explicit_leads", fail_extract)
    mentions = lead_cache.lead_mentions_for_snapshot(snapshot)

    assert {mention.canonical_locator for mention in mentions} == {
        "jira:ABC-2",
        "confluence:12345",
    }


def test_materialize_bytes_eagerly_mines_projected_display_for_leads(
    tmp_path: Path, monkeypatch
) -> None:
    dirs = initialize_session(tmp_path / "local")
    monkeypatch.setattr(
        stored,
        "html_markdown",
        lambda _data: b"Design doc: https://docs.google.com/document/d/doc-123/edit\n",
    )

    result = content.materialize_bytes(
        b'<h1>Example Heading</h1><p><a href="https://www.google.com/url?q=https://docs.google.com/document/d/doc-123/edit&amp;ust=1&amp;usg=2">Design</a></p>',
        dirs=dirs,
        preferred_name="40404.html",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 40404",
            "canonical_locator": "confluence:40404",
            "content_type": "text/html",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    payload = json.loads(
        (result.content_dir / "leads.json").read_text(encoding="utf-8")
    )

    assert {entry["canonical_locator"] for entry in payload["entries"]} == {
        "gdocs:doc-123"
    }


def test_materialize_bytes_records_explicit_projection_degradation(
    tmp_path: Path,
) -> None:
    dirs = initialize_session(tmp_path / "local")

    result = content.materialize_bytes(
        b"<p>Design doc: confluence:12345</p>",
        dirs=dirs,
        preferred_name="artifact.html",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "get 40404",
            "canonical_locator": "confluence:40404",
            "content_type": "text/html",
            "projector": "missing.projector",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    payload = json.loads(
        (result.content_dir / "leads.json").read_text(encoding="utf-8")
    )

    assert payload["degradations"] == [
        "stored projector `missing.projector` is unavailable; using canonical projection"
    ]
    assert {entry["canonical_locator"] for entry in payload["entries"]} == {
        "confluence:12345"
    }


def test_materialize_bytes_eagerly_drops_structural_github_repo_navigation_leads(
    tmp_path: Path, monkeypatch
) -> None:
    dirs = initialize_session(tmp_path / "local")
    monkeypatch.setattr(
        stored,
        "stored_display",
        lambda _path: stored.StoredDisplay(
            data=(
                "# acme/widgets\n\n"
                "- **URL:** https://github.com/acme/widgets\n"
                "- **README:** [README.md](https://github.com/acme/widgets/blob/main/README.md)\n\n"
                "## Contents\n\n"
                "- [README.md](https://github.com/acme/widgets/blob/main/README.md)\n"
                "- [src/](https://github.com/acme/widgets/tree/main/src)\n"
                "- [runbook.md](https://github.com/acme/widgets/blob/main/docs/runbook.md)\n"
            ).encode("utf-8"),
            language="markdown",
        ),
    )

    result = content.materialize_bytes(
        b'{"synthetic": true}',
        dirs=dirs,
        preferred_name="acme-widgets.json",
        metadata={
            "tool": "gotta",
            "plugin": "github",
            "locator": "https://github.com/acme/widgets",
            "canonical_locator": "https://github.com/acme/widgets",
            "artifact_kind": "evidence",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )

    payload = json.loads(
        (result.content_dir / "leads.json").read_text(encoding="utf-8")
    )
    assert payload["leadCount"] == 0
    assert payload["entries"] == []


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
    mentions = lead_cache.lead_mentions_for_snapshot(snapshot)

    assert [mention.canonical_locator for mention in mentions] == ["jira:ABC-2"]
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["version"] == lead_model.LEADS_CACHE_VERSION
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
            "artifact_kind": "evidence",
            "locator": "demo",
            "canonical_locator": "demo",
            "actor": "claude",
            "visibility_level": "personal",
            "visibility_boundary": "same_user",
            "visibility_confidence": "high",
            "visibility_basis": ["provider=gotta", "plugin=read"],
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--actor",
                "claude",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1
    assert payload["entries"][0]["actor"] == "claude"
    assert payload["entries"][0]["canonical_locator"] == "demo"
    assert payload["entries"][0]["artifactKind"] == "evidence"
    assert payload["evidenceArtifactCount"] == 1
    assert payload["entries"][0]["follow_command"] == _session_follow(
        local_root, "demo"
    )
    assert payload["entries"][0]["content_locator"].startswith("content:")
    assert payload["entries"][0]["artifact_locator"].startswith("artifact:demo.md@")
    assert payload["entries"][0]["content_follow_command"].startswith(
        f"gotta read --session {content.sh_quote(str(local_root))} 'content:"
    )
    assert payload["entries"][0]["artifact_follow_command"].startswith(
        f"gotta read --session {content.sh_quote(str(local_root))} 'artifact:demo.md@"
    )
    assert payload["entries"][0]["visibility_level"] == "personal"
    assert payload["entries"][0]["visibility_boundary"] == "same_user"


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
    assert (
        session_main.main(
            ["manifest", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1


def test_session_manifest_filter_filters_rows_before_paging(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"north",
        dirs=dirs,
        preferred_name="north-ledger.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "GEN-1",
            "canonical_locator": "jira:GEN-1",
            "actor": "generic-actor-a",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"south",
        dirs=dirs,
        preferred_name="south-runbook.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "202",
            "canonical_locator": "confluence:202",
            "actor": "generic-actor-b",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--filter",
                "south-(runbook|ledger)",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["filter"] == "south-(runbook|ledger)"
    assert payload["entryCount"] == 1
    assert payload["fetchRecordCount"] == 1
    assert payload["entries"][0]["canonical_locator"] == "confluence:202"


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
    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--actor",
                "claude",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"][0]["follow_command"] == _session_follow(
        local_root,
        f"content:{result.digest}",
    )
    assert payload["entries"][0]["content_locator"] == f"content:{result.digest}"
    assert payload["entries"][0]["artifact_locator"] == content.artifact_locator(
        "demo.md", result.digest
    )


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
            "artifact_kind": "evidence",
            "locator": "PROJ-1",
            "canonical_locator": "jira:PROJ-1",
            "visibility_level": "restricted",
            "visibility_boundary": "same_company",
            "visibility_confidence": "high",
            "visibility_basis": ["provider=jira", "policy=test"],
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
            "artifact_kind": "discovery",
            "locator": "3904339970",
            "canonical_locator": "confluence:3904339970",
            "actor": "claude",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )
    assert (
        session_main.main(
            ["timeline", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "acquired"
    assert payload["eventCount"] == 2
    assert payload["discoveryArtifactCount"] == 1
    assert payload["evidenceArtifactCount"] == 1
    assert payload["events"][0]["mode"] == "acquired"
    assert payload["events"][0]["artifactKind"] == "evidence"
    assert payload["events"][0]["follow_command"] == _session_follow(
        local_root, "jira:PROJ-1"
    )
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][0]["content_locator"].startswith("content:")
    assert payload["events"][0]["artifact_locator"].startswith("artifact:first.md@")
    assert payload["events"][0]["visibility_level"] == "restricted"
    assert payload["events"][0]["visibility_boundary"] == "same_company"
    assert payload["events"][1]["actor"] == "claude"
    assert payload["events"][1]["artifactKind"] == "discovery"


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
    assert (
        session_main.main(
            ["timeline", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["eventCount"] == 1


def test_session_timeline_filter_filters_events_before_paging(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"north",
        dirs=dirs,
        preferred_name="north.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "GEN-1",
            "canonical_locator": "jira:GEN-1",
            "actor": "generic-actor-a",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"south",
        dirs=dirs,
        preferred_name="south.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "202",
            "canonical_locator": "confluence:202",
            "actor": "generic-actor-b",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--filter",
                "south|GEN-404",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["filter"] == "south|GEN-404"
    assert payload["eventCount"] == 1
    assert payload["events"][0]["locator"] == "confluence:202"
    assert payload["events"][0]["plugin"] == "confluence"


def test_session_timeline_filter_filters_coverage_gaps_in_source_modes(
    tmp_path: Path, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    content.materialize_bytes(
        b"north content",
        dirs=dirs,
        preferred_name="north-gap.md",
        metadata={
            "tool": "gotta",
            "plugin": "jira",
            "locator": "GEN-11",
            "canonical_locator": "jira:GEN-11",
        },
        timestamp="2026-03-11T00:00:00.000001Z",
    )
    content.materialize_bytes(
        b"south content",
        dirs=dirs,
        preferred_name="south-gap.md",
        metadata={
            "tool": "gotta",
            "plugin": "confluence",
            "locator": "909",
            "canonical_locator": "confluence:909",
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "created",
                "--filter",
                "south",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["filter"] == "south"
    assert payload["eventCount"] == 0
    assert payload["coverageGapCount"] == 1


def test_session_timeline_default_limit_keeps_latest_window(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    for index in range(3):
        content.materialize_bytes(
            f"artifact-{index}".encode("utf-8"),
            dirs=dirs,
            preferred_name=f"artifact-{index}.md",
            metadata={
                "tool": "gotta",
                "plugin": "jira",
                "locator": f"PROJ-{index}",
                "canonical_locator": f"jira:PROJ-{index}",
            },
            timestamp=f"2026-03-11T00:00:0{index}.000001Z",
        )

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--limit",
                "1",
                "--output",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["eventCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 2
    assert payload["events"][0]["locator"] == "jira:PROJ-2"


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
            "locator": "logs:session",
            "preferred_name": "logs:session",
            "follow_command": "gotta logs",
            "detail": "appended 1 logs entry",
            "time_field": "session_recorded_at",
        },
    )
    assert (
        session_main.main(
            ["timeline", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["activityPath"].endswith("state/activity.jsonl")
    assert payload["eventCount"] == 2
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][1]["locator"] == "logs:session"
    assert payload["events"][1]["event_kind"] == "local"
    assert payload["events"][1]["fetched_at"] == "2026-03-11T00:00:01Z"


def test_session_timeline_merges_bound_actor_activity_logs_without_sibling_note_snapshots(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session"

    _init_session(root, capsys)
    _bind_actors(root, capsys, "Claude", "Codex")
    claude = _actor_id(root, "Claude")
    codex = _actor_id(root, "Codex")
    claude_root = _actor_root(root, "Claude")
    codex_root = _actor_root(root, "Codex")

    content.append_activity_event(
        claude_root,
        {
            "timestamp": "2026-03-11T00:00:01Z",
            "plugin": "notes",
            "surface": "notes",
            "action": "append",
            "actor": claude,
            "locator": f"notes:actor:{claude}",
            "preferred_name": f"notes:actor:{claude}",
            "follow_command": f"gotta notes --actor {claude}",
            "detail": "claude durable note",
            "time_field": "session_recorded_at",
        },
    )
    content.append_activity_event(
        codex_root,
        {
            "timestamp": "2026-03-11T00:00:02Z",
            "plugin": "logs",
            "surface": "logs",
            "action": "append",
            "actor": codex,
            "locator": f"logs:actor:{codex}",
            "preferred_name": f"logs:actor:{codex}",
            "follow_command": f"gotta logs --actor {codex}",
            "detail": "codex execution pulse",
            "time_field": "session_recorded_at",
        },
    )
    content.append_activity_event(
        codex_root,
        {
            "timestamp": "2026-03-11T00:00:03Z",
            "plugin": "oops",
            "surface": "oops",
            "action": "append",
            "actor": codex,
            "locator": f"oops:actor:{codex}",
            "preferred_name": f"oops:actor:{codex}",
            "follow_command": f"gotta oops --actor {codex}",
            "detail": "codex noted friction",
            "time_field": "session_recorded_at",
        },
    )

    assert (
        session_main.main(
            ["timeline", "--session", str(claude_root), "--all", "--output", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["activityPaths"]) == 2
    assert {Path(path).parent.parent.name for path in payload["activityPaths"]} == {
        claude,
        codex,
    }
    local_events = [event for event in payload["events"] if event["mode"] == "local"]
    assert [event["actor"] for event in local_events[:3]] == [claude, codex, codex]
    assert [event["locator"] for event in local_events[:3]] == [
        f"notes:actor:{claude}",
        f"logs:actor:{codex}",
        f"oops:actor:{codex}",
    ]


def test_session_timeline_labels_local_surface_snapshots_as_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    initialize_session(local_root)
    local_root.joinpath("GOAL.md").write_text("# Goal\n", encoding="utf-8")

    assert (
        session_main.main(
            ["timeline", "--session", str(local_root), "--output", "json"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    goal_event = next(
        event for event in payload["events"] if event["locator"] == "goal:session"
    )
    assert goal_event["plugin"] == "session"
    assert goal_event["mode"] == "local"
    assert goal_event["follow_command"] == "gotta goal"


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
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "best-effort",
                "--output",
                "json",
            ]
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
            "locator": "todo:session",
            "preferred_name": "todo:session",
            "follow_command": "gotta todo",
            "detail": "appended 1 TODO item",
            "time_field": "session_recorded_at",
        },
    )
    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "best-effort",
                "--output",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["coverageGapCount"] == 0
    assert payload["eventCount"] == 2
    assert payload["events"][0]["locator"] == "jira:PROJ-1"
    assert payload["events"][0]["source_time_field"] == "source_created_at"
    assert payload["events"][1]["locator"] == "todo:session"
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
    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "created",
                "--output",
                "json",
            ]
        )
        == 0
    )
    created_payload = json.loads(capsys.readouterr().out)
    assert created_payload["mode"] == "created"
    assert created_payload["coverageGapCount"] == 0
    assert created_payload["events"][0]["source_time_field"] == "source_created_at"
    assert created_payload["events"][0]["source_time"].startswith("2026-03-09T09:00:00")

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "updated",
                "--output",
                "json",
            ]
        )
        == 0
    )
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
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "best-effort",
                "--output",
                "json",
            ]
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
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "best-effort",
                "--output",
                "json",
            ]
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
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "best-effort",
                "--output",
                "json",
            ]
        )
        == 0
    )
    best_effort_payload = json.loads(capsys.readouterr().out)
    assert best_effort_payload["coverageGapCount"] == 0
    assert best_effort_payload["eventCount"] == 0

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--mode",
                "created",
                "--output",
                "json",
            ]
        )
        == 0
    )
    created_payload = json.loads(capsys.readouterr().out)
    assert created_payload["coverageGapCount"] == 0
    assert created_payload["eventCount"] == 0


def test_session_timeline_supports_offset_and_reports_exhausted_pages(
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
        },
        timestamp="2026-03-11T00:00:01.000001Z",
    )

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--limit",
                "1",
                "--offset",
                "5",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["eventCount"] == 2
    assert payload["totalCount"] == 2
    assert payload["shownCount"] == 0
    assert payload["offset"] == 5
    assert payload["nextOffset"] is None
    assert payload["truncated"] is False
    assert payload["events"] == []

    assert (
        session_main.main(
            [
                "timeline",
                "--session",
                str(local_root),
                "--limit",
                "1",
                "--offset",
                "5",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "page: 2 total; showing 0; offset 5" in text
    assert "page: no results in this page window" in text


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
    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--plugin",
                "jira",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 1


def test_session_manifest_supports_offset_and_all_paging(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    for index in range(3):
        content.materialize_bytes(
            f"artifact-{index}".encode("utf-8"),
            dirs=dirs,
            preferred_name=f"artifact-{index}.md",
            metadata={
                "tool": "gotta",
                "plugin": "jira",
                "locator": f"PROJ-{index}",
                "canonical_locator": f"jira:PROJ-{index}",
            },
            timestamp=f"2026-03-11T00:00:0{index}.000001Z",
        )

    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--limit",
                "1",
                "--offset",
                "1",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entryCount"] == 3
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert payload["entries"][0]["preferred_name"] == "artifact-1.md"

    assert (
        session_main.main(
            [
                "manifest",
                "--session",
                str(local_root),
                "--offset",
                "1",
                "--all",
                "--output",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 2
    assert payload["offset"] == 1
    assert payload["nextOffset"] is None
    assert payload["truncated"] is False
    assert payload["entries"][0]["plugin"] == "jira"
    assert payload["entries"][0]["canonical_locator"] == "jira:PROJ-1"


def test_session_manifest_collapses_repeated_fetches_into_one_canonical_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_root = tmp_path / "local"
    dirs = initialize_session(local_root)
    for index in range(2):
        content.materialize_bytes(
            b"same body",
            dirs=dirs,
            preferred_name="demo.md",
            metadata={
                "tool": "gotta",
                "plugin": "read",
                "artifact_kind": "evidence",
                "locator": "https://github.com/acme/widgets",
                "canonical_locator": "https://github.com/acme/widgets",
                "actor": "claude",
            },
            timestamp=f"2026-03-11T00:00:0{index}.000001Z",
        )

    assert (
        session_main.main(
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
    assert payload["entries"][0]["firstFetchedAt"] == "2026-03-11T00:00:00.000001Z"
    assert payload["entries"][0]["lastFetchedAt"] == "2026-03-11T00:00:01.000001Z"


def test_session_analyze_does_not_treat_same_name_cross_provider_as_revision(
    tmp_path: Path,
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

    payload = session_analyze.analysis_payload(dirs)

    assert payload["revisionEdgeCount"] == 0
    provider_sets = {tuple(item["providers"]) for item in payload["content"]}
    assert ("confluence",) in provider_sets
    assert ("jira",) in provider_sets


def test_session_analyze_marks_same_name_collisions_with_resource_hints(
    tmp_path: Path,
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

    payload = session_analyze.analysis_payload(dirs)

    assert payload["nameCollisionCount"] == 1
    assert payload["nameCollisions"] == ["shared-thread.md"]
    assert all(item["nameCollision"] for item in payload["content"])
    assert any(
        any("slack-thread:" in hint for hint in item["resourceHints"])
        for item in payload["content"]
    )

    mermaid = session_analyze.render_analysis_mermaid(payload)
    assert "slack-thread:C1:111" in mermaid or "slack-thread:C1:222" in mermaid
