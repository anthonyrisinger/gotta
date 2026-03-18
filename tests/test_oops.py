from __future__ import annotations

import io
import json
from pathlib import Path
import threading

from gotta import content
from gotta import friction
from gotta.plugins import oops


def initialize_session(root: Path) -> None:
    dirs = content.ResolvedDirs(
        session_dir=root,
        content_dir=root / "content",
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)


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
    assert "Append, extend, list, or summarize durable session speed bumps," in output


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

    assert oops.main(["summary", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_root"] == str(root.resolve())
    assert payload["entry_count"] == 1
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

    assert oops.main(["list", "--session", str(root), "--output", "json"]) == 0
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

    assert oops.main(["list", "--session", str(root), "--output", "json"]) == 0
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

    assert oops.main(["list", "--session", str(root), "--output", "json"]) == 0
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

    assert oops.main(["list", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["message"] == "explicit stdin oops payload"


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

    assert oops.main(["list", "--session", str(root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    messages = [entry["message"] for entry in payload["entries"]]
    assert "First friction seam" in messages
    assert "Second friction seam" in messages
