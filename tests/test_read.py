from __future__ import annotations
import io
from pathlib import Path
import socket
from types import SimpleNamespace
import urllib.error

import pytest

from gotta import content
from gotta import session as sessionlib
from gotta import target
from gotta.plugins import actor, read, session


def test_read_local_directory_renders_native_listing(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )
    (root / "docs").mkdir()
    (root / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")

    assert read.main([str(root), "--recursive", "--max-depth", "2"]) == 0

    output = capsys.readouterr().out
    assert f"# Directory: {root.resolve()}" in output
    assert "`.git/`" in output
    assert "`.git/config`" in output
    assert "`docs/`" in output
    assert "`docs/README.md`" in output


def test_read_requires_session_context_for_relative_hidden_file(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = repo / ".git" / "config"
    config.write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n', encoding="utf-8"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOTTA_SESSION_REPO", str(repo))

    assert read.main([".git/config"]) == 2
    err = capsys.readouterr().err
    assert "requires an active or explicit session root" in err


def test_read_prefers_session_relative_target_over_shell_cwd(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    cwd_root = tmp_path / "cwd"
    session_root = tmp_path / "session"
    cwd_root.mkdir()
    session_root.mkdir()
    (cwd_root / "BRIEF.md").write_text("cwd copy\n", encoding="utf-8")
    (session_root / "BRIEF.md").write_text("session copy\n", encoding="utf-8")

    monkeypatch.chdir(cwd_root)
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(session_root))

    assert read.main(["BRIEF.md"]) == 0
    output = capsys.readouterr().out
    assert "session copy" in output
    assert "cwd copy" not in output


def test_read_can_follow_stored_content_digest(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    result = content.materialize_bytes(
        b"# Stored body\n\nline two\n",
        dirs=dirs,
        preferred_name="stored.md",
        metadata={"plugin": "read", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    assert read.main([result.digest]) == 0
    output = capsys.readouterr().out
    assert "# Stored body" in output


def test_read_can_follow_explicit_content_locator(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    result = content.materialize_bytes(
        b"# Stored body\n\nline two\n",
        dirs=dirs,
        preferred_name="stored.md",
        metadata={"plugin": "read", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    assert read.main([f"content:{result.digest}"]) == 0
    output = capsys.readouterr().out
    assert "# Stored body" in output


def test_read_can_follow_explicit_content_locator_with_explicit_session(
    tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    result = content.materialize_bytes(
        b"# Stored body\n\nline two\n",
        dirs=dirs,
        preferred_name="stored.md",
        metadata={"plugin": "read", "locator": "demo", "canonical_locator": "demo"},
    )

    assert (
        read.main(["--session", str(dirs.session_dir), f"content:{result.digest}"]) == 0
    )
    output = capsys.readouterr().out
    assert "# Stored body" in output


def test_read_can_follow_unique_session_artifact_name(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    assert read.main(["slack-search-abc.md"]) == 0
    output = capsys.readouterr().out
    assert "# Search Artifact" in output


def test_read_can_follow_explicit_artifact_locator(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    result = content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    assert (
        read.main([content.artifact_locator("slack-search-abc.md", result.digest)]) == 0
    )
    output = capsys.readouterr().out
    assert "# Search Artifact" in output


def test_read_can_follow_explicit_artifact_locator_with_explicit_session(
    tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    result = content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )

    assert (
        read.main(
            [
                "--session",
                str(dirs.session_dir),
                content.artifact_locator("slack-search-abc.md", result.digest),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "# Search Artifact" in output


def test_read_can_follow_artifact_locator_from_session_root_state_env(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    result = content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.delenv("GOTTA_SESSION_DIR", raising=False)
    monkeypatch.delenv("GOTTA_SESSION_CONTENT_DIR", raising=False)
    monkeypatch.chdir(dirs.session_dir)

    assert (
        read.main([content.artifact_locator("slack-search-abc.md", result.digest)]) == 0
    )
    output = capsys.readouterr().out
    assert "# Search Artifact" in output


def test_read_can_follow_artifact_locator_from_actor_root_state_env(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    content.write_state_env(dirs)
    result = content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )
    actor_root = tmp_path / "session-actor"
    actor_state = actor_root / "state"
    actor_state.mkdir(parents=True, exist_ok=True)
    (actor_state / "env").write_text(
        "\n".join(
            [
                f"export GOTTA_SESSION_DIR='{actor_root}'",
                f"export GOTTA_SESSION_CONTENT_DIR='{dirs.content_dir}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("GOTTA_SESSION_DIR", raising=False)
    monkeypatch.delenv("GOTTA_SESSION_CONTENT_DIR", raising=False)
    monkeypatch.chdir(actor_root)

    assert (
        read.main([content.artifact_locator("slack-search-abc.md", result.digest)]) == 0
    )
    output = capsys.readouterr().out
    assert "# Search Artifact" in output


def test_read_missing_actor_local_surface_reports_missing_local_path(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    actor_root = tmp_path / "session-claude"
    actor_root.mkdir()

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(actor_root / "content"))

    assert read.main(["WANT.md"]) == 2
    err = capsys.readouterr().err
    assert "local target 'WANT.md' does not exist" in err
    assert "unsupported target" not in err


def test_read_missing_actor_goal_surface_reports_missing_local_path(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    actor_root = tmp_path / "session-claude"
    actor_root.mkdir()

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(actor_root / "content"))

    assert read.main(["GOAL.md"]) == 2
    err = capsys.readouterr().err
    assert "local target 'GOAL.md' does not exist" in err
    assert "unsupported target" not in err


def test_read_seeded_actor_local_charter_surfaces(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    root = tmp_path / "session-root"

    assert session.main(["init", "--session", str(root)]) == 0
    capsys.readouterr()
    (root / "WANT.md").write_text("# Want\n\nReal intent.\n", encoding="utf-8")
    (root / "GOAL.md").write_text("# Goal\n\nReal goal.\n", encoding="utf-8")

    assert actor.main(["bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()

    actor_root = sessionlib._actor_session_dir(root, "claude")
    monkeypatch.setenv("GOTTA_SESSION_DIR", str(actor_root))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(actor_root / "content"))

    assert read.main(["WANT.md"]) == 0
    want_output = capsys.readouterr().out
    assert "# Actor Want Placeholder" in want_output

    assert read.main(["GOAL.md"]) == 0
    goal_output = capsys.readouterr().out
    assert "# Seed Actor Goal Placeholder" in goal_output


def test_read_does_not_materialize_local_artifact_rereads(
    monkeypatch, tmp_path: Path
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    result = content.materialize_bytes(
        b"# Search Artifact\n\nbody\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={"plugin": "slack", "locator": "demo", "canonical_locator": "demo"},
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    assert not target.should_materialize(
        [content.artifact_locator("slack-search-abc.md", result.digest), "--head", "2"]
    )


def test_read_ambiguous_session_artifact_name_suggests_explicit_artifact_locators(
    monkeypatch, tmp_path: Path
) -> None:
    session_root = tmp_path / "session"
    dirs = content.ResolvedDirs(
        session_dir=session_root, content_dir=session_root / "content"
    )
    dirs.session_dir.mkdir(parents=True, exist_ok=True)
    dirs.content_dir.mkdir(parents=True, exist_ok=True)
    first = content.materialize_bytes(
        b"# Search Artifact A\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={
            "plugin": "slack",
            "locator": "demo-a",
            "canonical_locator": "demo-a",
        },
    )
    second = content.materialize_bytes(
        b"# Search Artifact B\n",
        dirs=dirs,
        preferred_name="slack-search-abc.md",
        metadata={
            "plugin": "slack",
            "locator": "demo-b",
            "canonical_locator": "demo-b",
        },
    )

    monkeypatch.setenv("GOTTA_SESSION_DIR", str(dirs.session_dir))
    monkeypatch.setenv("GOTTA_SESSION_CONTENT_DIR", str(dirs.content_dir))

    with pytest.raises(SystemExit) as excinfo:
        read.main(["slack-search-abc.md"])
    message = str(excinfo.value)
    assert "artifact:slack-search-abc.md@" in message
    assert content.artifact_locator("slack-search-abc.md", first.digest) in message
    assert content.artifact_locator("slack-search-abc.md", second.digest) in message


def test_read_supports_bounded_local_views(tmp_path: Path, capsys) -> None:
    target = tmp_path / "notes.md"
    target.write_text(
        "# Intro\n\nline 1\nline 2\n## Details\n\na\nb\nc\n",
        encoding="utf-8",
    )

    assert read.main([str(target), "--section", "Details", "--head", "2"]) == 0
    output = capsys.readouterr().out
    assert "## Details" in output
    assert "a" in output
    assert "b" not in output


def test_read_supports_bounded_provider_views(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        read,
        "resolve_read_target",
        lambda argv: target.ReadTarget(
            request=target.parse_args(argv),
            kind="routed",
            path=None,
            routed_plugin="github",
            routed_argv=["fake"],
            canonical_locator="https://github.com/acme/widgets",
            preferred_name="widgets.md",
            should_materialize=True,
        ),
    )

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        print("# Title")
        print("")
        print("line 1")
        print("line 2")
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["https://github.com/acme/widgets", "--head", "3"]) == 0
    output = capsys.readouterr().out
    assert "# Title" in output
    assert "line 1" in output
    assert "line 2" not in output


def test_read_fetch_url_summarizes_html_error_pages(monkeypatch) -> None:
    def fake_urlopen(_request, timeout=None):
        raise urllib.error.HTTPError(
            url="https://github.com/acme/widgets/blob/main/missing.md",
            code=404,
            msg="Not Found",
            hdrs={"Content-Type": "text/html; charset=utf-8"},
            fp=io.BytesIO(
                b"<html><head><title>Page not found \xc2\xb7 GitHub \xc2\xb7 GitHub</title></head><body>missing</body></html>"
            ),
        )

    monkeypatch.setattr(read.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        read.fetch_url("https://github.com/acme/widgets/blob/main/missing.md")

    assert (
        str(excinfo.value)
        == "download failed with 404: Page not found · GitHub · GitHub"
    )


def test_read_fetch_url_uses_timeout_and_caps_remote_body(monkeypatch) -> None:
    seen_timeouts: list[float] = []

    class FakeResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "text/plain"}
            self._chunks = [
                b"a" * (read.REMOTE_FETCH_MAX_BYTES - 8),
                b"b" * 64,
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type or exc or tb:
                return False
            return False

        def read(self, _size: int = -1) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    def fake_urlopen(_request, timeout=None):
        seen_timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(read.urllib.request, "urlopen", fake_urlopen)

    data, content_type, truncated = read.fetch_url("https://example.com/large")

    assert seen_timeouts == [read.REMOTE_FETCH_TIMEOUT_SECONDS]
    assert content_type == "text/plain"
    assert len(data) == read.REMOTE_FETCH_MAX_BYTES
    assert truncated is True


def test_read_fetch_url_reports_timeouts_cleanly(monkeypatch) -> None:
    def fake_urlopen(_request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(read.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        read.fetch_url("https://example.com/slow")

    assert str(excinfo.value) == "download timed out after 15s"


def test_read_response_body_does_not_mark_exact_cap_as_truncated() -> None:
    data, truncated = read._read_response_body(
        io.BytesIO(b"a" * read.REMOTE_FETCH_MAX_BYTES)
    )

    assert len(data) == read.REMOTE_FETCH_MAX_BYTES
    assert truncated is False


def test_read_remote_url_reports_truncation_and_still_applies_head(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        read,
        "resolve_read_target",
        lambda argv: target.ReadTarget(
            request=target.parse_args(argv),
            kind="remote_url",
            path=None,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator="https://example.com/large",
            preferred_name="large.txt",
            should_materialize=True,
        ),
    )
    monkeypatch.setattr(
        read,
        "fetch_url",
        lambda _target: (b"line 1\nline 2\n", "text/plain", True),
    )

    assert read.main(["https://example.com/large", "--head", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "line 1\n"
    assert "truncated remote body" in captured.err


def test_read_view_shaping_makes_routed_targets_non_materializing() -> None:
    resolved = target.resolve_read_target(
        ["https://github.com/acme/widgets", "--head", "3"]
    )

    assert resolved.kind == "routed"
    assert resolved.should_materialize is True


def test_read_view_shaping_makes_remote_urls_non_materializing() -> None:
    resolved = target.resolve_read_target(
        ["https://example.com/manual.txt", "--tail", "5"]
    )

    assert resolved.kind == "remote_url"
    assert resolved.should_materialize is True


def test_read_whitespace_only_section_normalizes_to_plain_read() -> None:
    resolved = target.resolve_read_target(
        ["https://example.com/manual.txt", "--section", "   "]
    )

    assert resolved.kind == "remote_url"
    assert resolved.request.section == ""
    assert resolved.should_materialize is True


def test_read_help_text_describes_plain_vs_shaped_materialization() -> None:
    description = target.build_parser().description or ""

    assert (
        "Remote/provider reads store durable evidence only when an initialized session"
        in description
    )
    assert (
        "`--head`, `--tail`, and `--section` only trim what is shown to the operator"
        in read.USAGE
    )
    assert "--actor" in target.build_parser().format_help()


def test_execute_materializing_read_keeps_full_routed_bytes_under_bounded_view(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read,
        "resolve_read_target",
        lambda argv: target.ReadTarget(
            request=target.parse_args(argv),
            kind="routed",
            path=None,
            routed_plugin="github",
            routed_argv=["fake"],
            canonical_locator="https://github.com/acme/widgets",
            preferred_name="widgets.json",
            should_materialize=True,
        ),
    )

    canonical = b'{"kind":"repo","title":"Title","lines":["line 1","line 2"]}'

    def fake_capture(argv: list[str], _options: object):
        return read.Capture(
            data=canonical,
            name="widgets.json",
            type="application/json",
        )

    def fake_project(argv: list[str], capture):
        assert capture.data == canonical
        return b"# Title\n\nline 1\nline 2\n"

    monkeypatch.setattr(
        read,
        "get_plugin",
        lambda name: (
            SimpleNamespace(capture=fake_capture, project=fake_project)
            if name == "github"
            else None
        ),
    )

    outcome = read.execute_materializing_read(
        ["https://github.com/acme/widgets", "--head", "3"]
    )

    assert outcome.code == 0
    assert outcome.display_bytes.decode("utf-8") == "# Title\n\nline 1\n"
    assert outcome.canonical_bytes == canonical


def test_execute_materializing_read_keeps_full_remote_bytes_under_bounded_view(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read,
        "fetch_url",
        lambda _target: (b"line 1\nline 2\nline 3\n", "text/plain", False),
    )

    outcome = read.execute_materializing_read(
        ["https://example.com/manual.txt", "--tail", "2"]
    )

    assert outcome.code == 0
    assert outcome.display_bytes.decode("utf-8") == "line 2\nline 3\n"
    assert outcome.canonical_bytes.decode("utf-8") == "line 1\nline 2\nline 3\n"


def test_read_passes_provider_flags_through_for_routed_targets(monkeypatch) -> None:
    resolved = target.resolve_read_target(
        ["--limit", "10", "https://github.com/acme/widgets/commits/main"]
    )

    assert resolved.kind == "routed"
    assert resolved.routed_plugin == "github"
    assert resolved.routed_argv == [
        "--limit",
        "10",
        "https://github.com/acme/widgets/commits/main",
    ]


def test_read_passes_provider_flags_through_after_routed_target(monkeypatch) -> None:
    resolved = target.resolve_read_target(
        ["https://github.com/acme/widgets/commits/main", "--limit", "10"]
    )

    assert resolved.kind == "routed"
    assert resolved.routed_plugin == "github"
    assert resolved.routed_argv == [
        "https://github.com/acme/widgets/commits/main",
        "--limit",
        "10",
    ]


def test_read_uses_provider_normalized_fragmentless_url_for_routed_targets() -> None:
    resolved = target.resolve_read_target(
        ["https://docs.google.com/spreadsheets/d/sheet-123/edit#gid=0"]
    )

    assert resolved.kind == "routed"
    assert resolved.routed_plugin == "gsheets"
    assert resolved.routed_argv == [
        "get",
        "https://docs.google.com/spreadsheets/d/sheet-123/edit",
    ]


def test_read_preserves_github_fragment_hint_while_canonicalizing_identity() -> None:
    resolved = target.resolve_read_target(["https://github.com/acme/widgets#readme"])

    assert resolved.kind == "routed"
    assert resolved.routed_plugin == "github"
    assert resolved.routed_argv == ["https://github.com/acme/widgets#readme"]
    assert resolved.canonical_locator == "https://github.com/acme/widgets"


def test_read_routes_github_actions_job_urls() -> None:
    resolved = target.resolve_read_target(
        ["https://github.com/acme/widgets/actions/runs/123456789/job/987654321"]
    )

    assert resolved.kind == "routed"
    assert resolved.routed_plugin == "github"
    assert resolved.routed_argv == [
        "https://github.com/acme/widgets/actions/runs/123456789/job/987654321"
    ]
    assert (
        resolved.canonical_locator
        == "https://github.com/acme/widgets/actions/runs/123456789/job/987654321"
    )


def test_read_delegates_github_commit_history_limit_after_target(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert (
        read.main(["https://github.com/acme/widgets/commits/main", "--limit", "10"])
        == 0
    )
    assert seen == [
        (
            "github",
            ["https://github.com/acme/widgets/commits/main", "--limit", "10"],
        )
    ]


def test_read_can_follow_canonical_jira_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["jira:PROJ-3960"]) == 0
    assert seen == [("jira", ["get", "PROJ-3960"])]


def test_read_can_follow_canonical_search_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["jira:search Architecture"]) == 0
    assert seen == [("jira", ["search", "Architecture"])]


def test_read_can_follow_canonical_granola_document_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["granola:11111111-1111-1111-1111-111111111111"]) == 0
    assert seen == [("granola", ["get", "11111111-1111-1111-1111-111111111111"])]


def test_read_can_follow_canonical_granola_search_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["granola:search --limit 5 latency"]) == 0
    assert seen == [("granola", ["search", "--limit", "5", "latency"])]


def test_read_can_follow_canonical_granola_transcript_search_locator(
    monkeypatch,
) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["granola:search-transcript --all latency"]) == 0
    assert seen == [("granola", ["search-transcript", "--all", "latency"])]


def test_read_can_follow_canonical_granola_transcript_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["granola:transcript 11111111-1111-1111-1111-111111111111"]) == 0
    assert seen == [("granola", ["transcript", "11111111-1111-1111-1111-111111111111"])]


def test_read_can_follow_canonical_search_locator_with_flags(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert (
        read.main(
            [
                "slack:search --workspace example-workspace --source archive --limit 10 --output markdown ABC"
            ]
        )
        == 0
    )
    assert seen == [
        (
            "slack",
            [
                "search",
                "--workspace",
                "example-workspace",
                "--source",
                "archive",
                "--limit",
                "10",
                "--output",
                "markdown",
                "ABC",
            ],
        )
    ]


def test_read_can_follow_canonical_search_locator_with_equals_flags_unquoted(
    monkeypatch,
) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["jira:search", "--limit=10", "Architecture"]) == 0
    assert seen == [("jira", ["search", "--limit", "10", "Architecture"])]


def test_read_can_follow_canonical_search_locator_with_flags_after_query(
    monkeypatch,
) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert (
        read.main(["slack:search ABC reboot --workspace example-workspace --limit 10"])
        == 0
    )
    assert seen == [
        (
            "slack",
            [
                "search",
                "--workspace",
                "example-workspace",
                "--limit",
                "10",
                "ABC reboot",
            ],
        )
    ]


def test_read_can_follow_canonical_github_search_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["github:search --type pr --repo acme/widgets ABC"]) == 0
    assert seen == [
        ("github", ["search", "--type", "pr", "--repo", "acme/widgets", "ABC"])
    ]


def test_read_can_follow_canonical_global_github_search_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["github:search --global ABC"]) == 0
    assert seen == [("github", ["search", "--global", "ABC"])]


def test_read_can_follow_slack_workspace_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["slack:example-workspace"]) == 0
    assert seen == [
        ("slack", ["status", "--workspace", "example-workspace", "--output", "summary"])
    ]


def test_read_joins_unquoted_multiword_search_locator(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["jira:search", "ABC", "reboot"]) == 0
    assert seen == [("jira", ["search", "ABC reboot"])]


def test_read_accepts_option_like_locator_tails_after_end_of_options(
    monkeypatch,
) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_delegate(plugin: str, argv: list[str]) -> int:
        seen.append((plugin, argv))
        return 0

    monkeypatch.setattr(read, "delegate", fake_delegate)

    assert read.main(["--", "jira:search", "ABC", "--head", "180"]) == 0
    assert seen == [("jira", ["search", "ABC --head 180"])]
