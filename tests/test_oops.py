from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from gotta.actors import ACTOR_SPEAKER_ENV
from gotta import content
from gotta import friction
from gotta import session as sessionlib
from gotta.plugins import actor
from gotta.plugins import oops
from gotta.plugins import session as session_plugin


@pytest.fixture(autouse=True)
def local_session_registry(tmp_path: Path, monkeypatch) -> None:
    registry = tmp_path / "sessions"
    monkeypatch.setattr(content, "DEFAULT_SESSION_ROOT", registry)
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)
    monkeypatch.delenv(content.SESSION_ACTOR_ENV, raising=False)


def initialize_session(root: Path) -> None:
    assert session_plugin.main(["init", "--session", str(root)]) == 0


def _run_oops_subprocess(
    argv: list[str],
    *,
    stdin_text: str | None = None,
    closed_stdin: bool = False,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "from gotta.plugins import oops; "
            "raise SystemExit(oops.main(sys.argv[1:]))"
        ),
        *argv,
    ]
    kwargs: dict[str, object] = {
        "cwd": repo_root,
        "capture_output": True,
        "text": True,
    }
    if closed_stdin:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    return subprocess.run(command, **kwargs)


def _projection_entry_count(path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- `")
    )


def test_oops_help_all_is_top_level(capsys) -> None:
    assert oops.main(["--help-all"]) == 0
    output = capsys.readouterr().out
    assert "usage: gotta oops" in output
    assert "Show or record durable session speed bumps" in output
    assert "Bare multiword prose, real piped stdin, `--stdin`, and `--from-file` imply" in output


def test_oops_is_session_rooted_and_canonical(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert (
        oops.main(
            [
                "append",
                "Need clearer next step",
                "--session",
                str(root),
                "--surface",
                "read",
                "--kind",
                "routing",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_root"] == str(root.resolve())
    assert payload["entry_count"] == 1
    assert payload["shown_count"] == 1
    assert payload["kind_counts"]["routing"] == 1
    assert payload["surface_counts"]["read"] == 1
    assert (root / "state" / "oops.jsonl").exists()
    assert "Need clearer next step" in (root / "OOPS.md").read_text(encoding="utf-8")
    activity = content.activity_events(root)
    assert activity[-1]["locator"] == "state/oops.jsonl"
    assert activity[-1]["preferred_name"] == "OOPS.md"
    assert activity[-1]["follow_command"] == "gotta read 'OOPS.md'"


def test_oops_append_supports_from_file(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    payload_path = tmp_path / "oops.txt"
    initialize_session(root)
    payload_path.write_text(
        "Literal `gotta todo append` friction\nwith multiline context.\n",
        encoding="utf-8",
    )

    assert (
        oops.main(
            [
                "append",
                "--session",
                str(root),
                "--from-file",
                str(payload_path),
                "--surface",
                "todo",
                "--kind",
                "argument-shape-mismatch",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert "Literal `gotta todo append` friction" in payload["entries"][0]["message"]
    assert "with multiline context." in (root / "OOPS.md").read_text(encoding="utf-8")


def test_oops_append_resolves_from_file_relative_to_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    payload_path = root / "notes" / "oops.txt"
    initialize_session(root)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text("Session-relative oops payload\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert (
        oops.main(
            [
                "append",
                "--session",
                str(root),
                "--from-file",
                "notes/oops.txt",
                "--surface",
                "read",
                "--kind",
                "routing",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "Session-relative oops payload"


def test_oops_append_supports_from_file_dash_for_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    monkeypatch.setattr(oops.sys, "stdin", io.StringIO("stdin-backed oops payload\n"))

    assert (
        oops.main(
            [
                "append",
                "--session",
                str(root),
                "--from-file",
                "-",
                "--surface",
                "logs",
                "--kind",
                "input-shape",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "stdin-backed oops payload"


def test_oops_append_supports_stdin_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    monkeypatch.setattr(oops.sys, "stdin", io.StringIO("explicit stdin oops payload\n"))

    assert (
        oops.main(
            [
                "append",
                "--session",
                str(root),
                "--stdin",
                "--surface",
                "logs",
                "--kind",
                "input-shape",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "explicit stdin oops payload"


def test_oops_write_only_inputs_imply_append_when_action_is_omitted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    monkeypatch.setattr(oops.sys, "stdin", io.StringIO("implicit stdin oops payload\n"))

    assert (
        oops.main(
            [
                "--session",
                str(root),
                "--stdin",
                "--surface",
                "logs",
                "--kind",
                "input-shape",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "implicit stdin oops payload"


def test_oops_inline_prose_implies_append_when_action_is_omitted(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert (
        oops.main(
            [
                "--session",
                str(root),
                "--surface",
                "session-bootstrap",
                "minimal oops test",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "minimal oops test"
    assert payload["entries"][0]["surface"] == "session-bootstrap"


def test_oops_multiword_prose_implies_append_without_metadata(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert oops.main(["--session", str(root), "generic", "issue"]) == 0
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "generic issue"


def test_oops_single_unknown_token_without_write_intent_fails(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        oops.main(["--session", str(root), "generic"])
    assert "unknown gotta oops action `generic`" in str(exc.value)

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 0


def test_oops_unknown_action_still_fails_instead_of_appending(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        oops.main(["--session", str(root), "smmary"])
    assert "unknown gotta oops action `smmary`" in str(exc.value)

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 0


@pytest.mark.parametrize("legacy_action", ["summary", "list"])
def test_oops_legacy_read_actions_redirect_to_show(
    tmp_path: Path, capsys, legacy_action: str
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        oops.main([legacy_action, "--session", str(root)])
    assert f"`gotta oops {legacy_action}` has been folded into `gotta oops show`" in str(exc.value)


def test_oops_read_defaults_to_all_bound_actors_and_actor_filters_narrow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert actor.main(["bind", "Claude", "Codex", "--session", str(root)]) == 0
    capsys.readouterr()
    claude = sessionlib._resolve_bound_actor_name(root, "Claude")
    codex = sessionlib._resolve_bound_actor_name(root, "Codex")

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        oops.main(
            [
                "append",
                "codex-facing seam",
                "--session",
                str(root),
                "--actor",
                codex,
                "--surface",
                "session",
                "--kind",
                "routing",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert (
        oops.main(
            [
                "append",
                "claude-facing seam",
                "--session",
                str(root),
                "--actor",
                claude,
                "--surface",
                "actor",
                "--kind",
                "contract",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["actor_count"] == 2
    assert payload["entry_count"] == 2
    assert payload["shown_count"] == 2
    assert set(payload["actors"]) == {codex, claude}
    assert set(payload["oops_logs"]) == {codex, claude}

    assert {entry["actor"] for entry in payload["entries"]} == {codex, claude}

    assert oops.main(["show", "--session", str(root), "--actor", claude, "--output", "json"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert filtered["actor_count"] == 1
    assert filtered["actors"] == [claude]
    assert filtered["entry_count"] == 1


def test_oops_read_on_actor_root_defaults_to_session_wide(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert actor.main(["bind", "Claude", "Codex", "--session", str(root)]) == 0
    capsys.readouterr()
    claude = sessionlib._resolve_bound_actor_name(root, "Claude")
    codex = sessionlib._resolve_bound_actor_name(root, "Codex")
    actor_root = sessionlib._session_dir(explicit_session=str(root), explicit_actor=claude)

    monkeypatch.setenv(ACTOR_SPEAKER_ENV, claude)
    assert (
        oops.main(
            [
                "append",
                "claude oops",
                "--session",
                str(actor_root),
                "--surface",
                "actor",
                "--kind",
                "contract",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)
    assert (
        oops.main(
            [
                "append",
                "codex oops",
                "--session",
                str(root),
                "--actor",
                codex,
                "--surface",
                "session",
                "--kind",
                "routing",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(actor_root), "--output", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["actor_count"] == 2
    assert set(listed["actors"]) == {claude, codex}
    assert {entry["actor"] for entry in listed["entries"]} == {claude, codex}
    assert listed["shown_count"] == 2


def test_oops_append_uses_projection_append_hot_path_when_surface_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    friction.sync_oops_projection(root)

    monkeypatch.setattr(
        friction,
        "sync_channel_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing oops surface should use append hot path")
        ),
    )

    friction.append_oops_record(
        root,
        message="append-hot-path entry",
        surface="read",
        kind="routing",
    )

    assert "append-hot-path entry [medium routing read]" in (
        root / "OOPS.md"
    ).read_text(encoding="utf-8")


def test_oops_projection_stays_in_sync_under_concurrent_appends(tmp_path: Path) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    friction.sync_oops_projection(root)

    total_threads = 4
    entries_per_thread = 25
    threads: list[threading.Thread] = []

    def append_entries(prefix: str) -> None:
        for index in range(entries_per_thread):
            friction.append_oops_record(
                root,
                message=f"{prefix}-{index}",
                surface="logs",
                kind="input-shape",
            )

    for thread_index in range(total_threads):
        thread = threading.Thread(
            target=append_entries,
            args=(f"t{thread_index}",),
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    records = friction.oops_records(root)
    surface_path = root / "OOPS.md"
    oops_text = surface_path.read_text(encoding="utf-8")

    assert len(records) == total_threads * entries_per_thread
    assert _projection_entry_count(surface_path) == len(records)
    for thread_index in range(total_threads):
        assert f"t{thread_index}-0" in oops_text
        assert f"t{thread_index}-{entries_per_thread - 1}" in oops_text


def test_oops_extend_supports_multiple_entries(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert (
        oops.main(
            [
                "extend",
                "First friction seam",
                "Second friction seam",
                "--session",
                str(root),
                "--surface",
                "read",
                "--kind",
                "routing",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    messages = [entry["message"] for entry in payload["entries"]]
    assert "First friction seam" in messages
    assert "Second friction seam" in messages


def test_oops_bare_and_actor_reads_ignore_closed_stdin_without_mutating(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    assert actor.main(["bind", "Helper", "--session", str(root)]) == 0
    capsys.readouterr()
    helper = sessionlib._resolve_bound_actor_name(root, "helper")

    assert oops.main(["append", "root friction", "--session", str(root)]) == 0
    capsys.readouterr()
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, helper)
    assert oops.main(["append", "actor friction", "--session", str(root), "--actor", helper]) == 0
    capsys.readouterr()

    root_before = (root / "state" / "oops.jsonl").read_text(encoding="utf-8")
    actor_root = sessionlib._session_dir(explicit_session=str(root), explicit_actor=helper)
    actor_before = (actor_root / "state" / "oops.jsonl").read_text(encoding="utf-8")

    bare = _run_oops_subprocess(["--session", str(root)], closed_stdin=True)
    assert bare.returncode == 0
    assert "entries:" in bare.stdout
    assert "appended oops entry" not in bare.stdout
    assert (root / "state" / "oops.jsonl").read_text(encoding="utf-8") == root_before

    scoped = _run_oops_subprocess(["--session", str(actor_root)], closed_stdin=True)
    assert scoped.returncode == 0
    assert "entries:" in scoped.stdout
    assert "appended oops entry" not in scoped.stdout
    assert (actor_root / "state" / "oops.jsonl").read_text(encoding="utf-8") == actor_before


def test_oops_show_rejects_write_inputs_and_piped_stdin(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    payload_path = tmp_path / "friction.txt"
    initialize_session(root)
    assert oops.main(["append", "existing friction", "--session", str(root)]) == 0
    capsys.readouterr()
    payload_path.write_text("file-backed friction\n", encoding="utf-8")
    before = (root / "state" / "oops.jsonl").read_text(encoding="utf-8")

    piped = _run_oops_subprocess(
        ["show", "--session", str(root)],
        stdin_text="friction from stdin\n",
    )
    assert piped.returncode == 1
    assert "`gotta oops show` is read-only" in piped.stderr
    assert (root / "state" / "oops.jsonl").read_text(encoding="utf-8") == before

    with pytest.raises(SystemExit) as exc:
        oops.main(["show", "--session", str(root), "--stdin"])
    assert "`gotta oops show` is read-only" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        oops.main(["show", "--session", str(root), "--from-file", str(payload_path)])
    assert "`gotta oops show` is read-only" in str(exc.value)


def test_oops_real_piped_stdin_still_implies_append(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    capsys.readouterr()

    result = _run_oops_subprocess(["--session", str(root)], stdin_text="piped friction\n")
    assert result.returncode == 0
    assert "appended oops entry" in result.stdout

    assert oops.main(["show", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "piped friction"


def test_oops_show_limit_zero_means_all_entries(tmp_path: Path, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert oops.main(["append", "first friction", "--session", str(root)]) == 0
    assert oops.main(["append", "second friction", "--session", str(root)]) == 0
    capsys.readouterr()

    assert oops.main(["show", "--session", str(root), "--limit", "0", "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 2
    assert payload["shown_count"] == 2


def test_foreign_writer_cannot_append_actor_oops(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)

    assert actor.main(["bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()
    claude = sessionlib._resolve_bound_actor_name(root, "Claude")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, "foreign-123")

    with pytest.raises(SystemExit) as excinfo:
        oops.main(
            [
                "append",
                "bad oops",
                "--session",
                str(root),
                "--actor",
                claude,
                "--surface",
                "read",
                "--kind",
                "routing",
            ]
        )

    assert "bind and launch a sibling actor" in str(excinfo.value)


def test_unbound_shell_cannot_append_actor_oops(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    assert actor.main(["bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()
    claude = sessionlib._resolve_bound_actor_name(root, "claude")
    monkeypatch.delenv(ACTOR_SPEAKER_ENV, raising=False)
    monkeypatch.delenv(content.ACTOR_ID_ENV, raising=False)
    monkeypatch.delenv(content.SESSION_ACTOR_ENV, raising=False)
    monkeypatch.setattr(
        content,
        "current_context_binding",
        lambda: type("Binding", (), {"binding_id": ""})(),
    )

    with pytest.raises(SystemExit) as excinfo:
        oops.cmd_oops(
            oops.build_parser().parse_args(
                ["append", "bad oops", "--session", str(root), "--actor", claude]
            )
        )

    assert "bind and launch a sibling actor" in str(excinfo.value)
    actor_root = sessionlib._session_dir(explicit_session=str(root), explicit_actor=claude)
    assert friction.oops_records(actor_root) == []


def test_peer_actor_oops_preserves_peer_author(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "session-root"
    initialize_session(root)
    assert actor.main(["bind", "Claude", "Codex", "--session", str(root)]) == 0
    capsys.readouterr()
    claude = sessionlib._resolve_bound_actor_name(root, "claude")
    codex = sessionlib._resolve_bound_actor_name(root, "codex")
    monkeypatch.setenv(ACTOR_SPEAKER_ENV, codex)

    assert (
        oops.cmd_oops(
            oops.build_parser().parse_args(
                ["append", "peer oops", "--session", str(root), "--actor", claude]
            )
        )
        == 0
    )
    capsys.readouterr()

    actor_root = sessionlib._session_dir(explicit_session=str(root), explicit_actor=claude)
    assert friction.oops_records(actor_root)[-1]["actor"] == codex
