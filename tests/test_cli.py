from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from gotta.actors import ACTOR_SPEAKER_ENV
from gotta import builtin
import gotta.content.env as content_env
import gotta.content.model as content_model
import gotta.content.scope as content_scope
import gotta.content.store as content_store
import gotta.cli.argv as cli_argv
import gotta.cli.bind as cli_bind
import gotta.cli.entry as cli
from gotta.notes.file import append_actor_note
from gotta.capture import Capture
from gotta.plugins import github
from gotta.plugins import jira
from gotta.session import bootstrap as session_bootstrap
from gotta.session import registry as session_registry


def _last_stderr_json(stderr: str) -> dict[str, object]:
    lines = [line for line in stderr.splitlines() if line.strip()]
    assert lines, "expected JSON receipt on stderr"
    return json.loads(lines[-1])


def _set_default_session_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(content_scope, "DEFAULT_SESSION_ROOT", root)


def _grouped_root(
    registry: Path, context_id: str, *, identity: str | None = None
) -> Path:
    fingerprint = cli_bind._session_token(context_id)
    return registry / fingerprint / "actors" / (identity or fingerprint)


def _actor_id(shared_root: Path, actor_ref: str) -> str:
    return session_registry._resolve_bound_actor_name(shared_root, actor_ref)


def test_main_rejects_unknown_plugin_without_creating_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["nonsense"]) == 2
    captured = capsys.readouterr()

    assert "unknown gotta plugin: nonsense" in captured.err
    assert "created a new gotta session" not in captured.err
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("argv", [["version"], ["--version"], ["-V"]])
def test_main_version_is_top_level_and_sessionless(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 0
    captured = capsys.readouterr()

    assert captured.out.strip() == f"gotta {cli_argv._gotta_version()}"
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


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
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["todo", "append", "first task"]) == 0
    first_err = capsys.readouterr().err
    session_root = Path(seen[-1][1])
    assert session_root.parent.name == "actors"
    assert session_root.parent.parent == (
        tmp_path / "session" / cli_bind._session_token("thread-123")
    )
    assert session_root.name == cli_bind._session_token("thread-123")
    assert (session_root / "state" / "env").exists()
    assert (session_root / "content").is_dir()
    assert (session_root / "content").is_symlink()
    assert os.readlink(session_root / "content") == "../../content"
    assert not (session_root / "session").exists()
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()
    assert "created a new gotta session" in first_err
    assert "this context is now bound to that session root" in first_err
    assert (
        "same-context fresh-process commands should resolve here automatically"
        in first_err
    )
    assert f"`gotta session bind {cli_bind._session_token('thread-123')}`" in first_err
    assert "`--session <shared-session-id>`" in first_err

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
        ["grafana", "datasources"],
        ["grafana", "query", "--datasource", "prom-main", "sum(up)"],
    ],
)
def test_main_non_session_provider_surfaces_do_not_create_session(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 0

    captured = capsys.readouterr()
    assert seen == [(argv, "")]
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize(
    ("env_name", "context_id", "context_source"),
    [
        ("CODEX_THREAD_ID", "thread-123", "codex_thread"),
        ("TERM_SESSION_ID", "term-session-1", "terminal_session"),
    ],
)
def test_main_stable_fingerprint_read_retrieval_auto_bootstraps_session(
    tmp_path: Path,
    monkeypatch,
    capsys,
    env_name: str,
    context_id: str,
    context_source: str,
) -> None:
    seen: list[tuple[list[str], str, str, str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append(
            (
                list(inner_argv or []),
                os.environ.get("GOTTA_SESSION_DIR", ""),
                os.environ.get("GOTTA_CONTEXT_ID", ""),
                os.environ.get("GOTTA_CONTEXT_SOURCE", ""),
            )
        )
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setenv(env_name, context_id)

    assert cli.main(["read", "https://example.com/manual.txt"]) == 0

    captured = capsys.readouterr()
    session_root = _grouped_root(tmp_path / "session", context_id)
    assert seen == [
        (
            ["read", "https://example.com/manual.txt"],
            str(session_root),
            context_id,
            context_source,
        )
    ]
    assert "created a new gotta session" in captured.err
    assert (session_root / "state" / "env").exists()
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()


def test_main_stable_fingerprint_local_relative_read_stays_sessionless(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("local doc\n", encoding="utf-8")

    assert cli.main(["read", "README.md"]) == 2

    captured = capsys.readouterr()
    assert "requires an active or explicit session root" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "session").exists()


def test_main_stable_fingerprint_local_absolute_read_stays_sessionless(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    local_file = tmp_path / "README.md"
    local_file.write_text("local doc\n", encoding="utf-8")

    assert cli.main(["read", str(local_file)]) == 0

    captured = capsys.readouterr()
    assert captured.out == "local doc\n"
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


def test_main_fallback_fingerprint_read_retrieval_remains_sessionless(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["read", "https://example.com/manual.txt"]) == 0

    captured = capsys.readouterr()
    assert seen == [(["read", "https://example.com/manual.txt"], "")]
    assert captured.err == ""
    assert not (tmp_path / "session").exists()


def test_main_stable_fingerprint_provider_search_auto_bootstraps_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["github", "search", "platform"]) == 0

    captured = capsys.readouterr()
    session_root = _grouped_root(tmp_path / "session", "thread-123")
    assert seen == [(["github", "search", "platform"], str(session_root))]
    assert "created a new gotta session" in captured.err
    assert (session_root / "state" / "env").exists()


def test_main_ambient_provider_search_materializes_discovery_in_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    canonical = b'{"items":[{"kind":"repo","name":"platform"}]}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        assert argv == ["search", "platform"]
        return Capture(
            data=canonical,
            name="github-search-platform.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "search"},
        )

    def fake_github_project(argv: list[str], capture: Capture) -> bytes:
        assert argv == ["search", "platform"]
        assert capture.data == canonical
        return b"# Search Results\n\n- one\n"

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(github, "project", fake_github_project)

    assert cli.main(["github", "search", "platform"]) == 0
    captured = capsys.readouterr()

    receipt = _last_stderr_json(captured.err)
    assert receipt["artifactKind"] == "discovery"
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "discovery"
    assert entries[-1]["actor"] == cli_bind._session_token("thread-123")


def test_main_read_routed_provider_search_preserves_discovery_artifact_kind(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    canonical = b'{"items":[{"kind":"repo","name":"platform"}]}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        assert argv == ["search", "platform"]
        return Capture(
            data=canonical,
            name="github-search-platform.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "search"},
        )

    def fake_github_project(argv: list[str], capture: Capture) -> bytes:
        assert argv == ["search", "platform"]
        assert capture.data == canonical
        return b"# Search Results\n\n- one\n"

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(github, "project", fake_github_project)

    assert cli.main(["read", "github:search platform"]) == 0
    captured = capsys.readouterr()

    receipt = _last_stderr_json(captured.err)
    assert receipt["artifactKind"] == "discovery"
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "discovery"


def test_main_top_level_search_materializes_discovery_in_bound_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    canonical = b'{"items":[{"kind":"repo","name":"platform"}]}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        assert argv == ["search", "platform"]
        return Capture(
            data=canonical,
            name="github-search-platform.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "search"},
        )

    def fake_github_project(argv: list[str], capture: Capture) -> bytes:
        assert argv == ["search", "platform"]
        assert capture.data == canonical
        return b"# Search Results\n\n- one\n"

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(github, "project", fake_github_project)

    assert cli.main(["search", "github:platform"]) == 0
    captured = capsys.readouterr()

    receipt = _last_stderr_json(captured.err)
    assert receipt["artifactKind"] == "discovery"
    manifest_path = registry / "demo" / "content" / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["plugin"] == "github"
    assert entries[-1]["artifact_kind"] == "discovery"


def test_main_top_level_search_accepts_explicit_search_alias(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    canonical = b'{"items":[{"kind":"repo","name":"platform"}]}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        assert argv == ["search", "platform"]
        return Capture(
            data=canonical,
            name="github-search-platform.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "search"},
        )

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(
        github, "project", lambda argv, capture: b"# Search Results\n\n- one\n"
    )

    assert cli.main(["search", "github:search platform"]) == 0
    receipt = _last_stderr_json(capsys.readouterr().err)

    assert receipt["artifactKind"] == "discovery"


def test_main_top_level_search_rejects_extra_unquoted_query_terms(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["search", "github:SomeFunction", "ownership"]) == 2
    captured = capsys.readouterr()

    assert (
        "takes exactly one provider-qualified plain-text query string" in captured.err
    )


def test_main_top_level_search_redirects_provider_native_jql(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["search", "jira:jql project = OPS"]) == 2
    captured = capsys.readouterr()

    assert "gotta jira jql" in captured.err


def test_main_top_level_search_redirects_read_like_provider_target_to_canonical_read(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["search", "jira:get OPS-1"]) == 2
    captured = capsys.readouterr()

    assert "gotta read jira:OPS-1" in captured.err

    assert cli.main(["search", "confluence:get 123"]) == 2
    captured = capsys.readouterr()

    assert "gotta read confluence:123" in captured.err

    assert cli.main(["search", "github:get acme/widgets"]) == 2
    captured = capsys.readouterr()

    assert "gotta read https://github.com/acme/widgets" in captured.err


def test_main_quiet_suppresses_success_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "demo"]) == 0
    capsys.readouterr()

    canonical = b'{"items":[{"kind":"repo","name":"platform"}]}'

    monkeypatch.setattr(
        github,
        "capture",
        lambda argv, _options: Capture(
            data=canonical,
            name="github-search-platform.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "search"},
        ),
    )
    monkeypatch.setattr(
        github, "project", lambda argv, capture: b"# Search Results\n\n- one\n"
    )

    assert cli.main(["search", "github:platform", "--quiet"]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""


def test_main_session_analyze_emits_no_side_effect_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "session-root"
    dirs = content_scope.resolve_dirs(
        content_model.CommonOptions(session_dir=str(root)), create=True
    )
    session_bootstrap.scaffold_session(root)
    content_store.materialize_bytes(
        b"# Example\n\nhello world\n",
        dirs=dirs,
        preferred_name="example.md",
        metadata={
            "tool": "gotta",
            "plugin": "read",
            "locator": "example",
            "canonical_locator": "example",
        },
    )
    _set_default_session_root(monkeypatch, tmp_path / "registry")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert (
        cli.main(["session", "analyze", "--session", str(root), "--mode", "lineage"])
        == 0
    )
    captured = capsys.readouterr()

    assert captured.out.startswith("session:")
    assert captured.err == ""


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

    canonical = b'{"kind":"repo","name":"widgets"}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        return Capture(
            data=canonical,
            name="widgets.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "repo"},
        )

    def fake_github_project(argv: list[str], capture: Capture) -> bytes:
        assert capture.data == canonical
        return b"# Repo\n\nmain body\n"

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(github, "project", fake_github_project)

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

    receipt = _last_stderr_json(captured.err)
    assert receipt["artifactKind"] == "evidence"
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

    canonical = b'{"kind":"repo","name":"widgets"}'

    def fake_github_capture(argv: list[str], _options: object) -> Capture:
        return Capture(
            data=canonical,
            name="widgets.json",
            type="application/json",
            meta={"projector": "github", "github_kind": "repo"},
        )

    def fake_github_project(argv: list[str], capture: Capture) -> bytes:
        assert capture.data == canonical
        return b"# Repo\n\nmain body\n"

    monkeypatch.setattr(github, "capture", fake_github_capture)
    monkeypatch.setattr(github, "project", fake_github_project)

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
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
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
def test_main_read_only_session_surfaces_auto_bootstrap_for_stable_fingerprint(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append((list(inner_argv or []), os.environ.get("GOTTA_SESSION_DIR", "")))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 0

    captured = capsys.readouterr()
    session_root = _grouped_root(tmp_path / "session", "thread-123")
    assert seen == [(argv, str(session_root))]
    assert "created a new gotta session" in captured.err
    assert (session_root / "state" / "env").exists()
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()


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
def test_main_read_only_session_surfaces_stay_sessionless_for_fallback_fingerprint(
    tmp_path: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append(list(inner_argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.chdir(tmp_path)

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert "this command requires an existing session" in captured.err
    assert seen == []
    assert not (tmp_path / "session").exists()


def test_main_stable_fingerprint_does_not_replace_explicit_session_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(inner_argv: list[str] | None = None) -> int:
        seen.append(list(inner_argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    explicit_root = tmp_path / "other-session"
    assert cli.main(["todo", "--session", str(explicit_root)]) == 2

    captured = capsys.readouterr()
    assert "existing initialized session at that exact root" in captured.err
    assert seen == []
    assert not (tmp_path / "session").exists()


def test_main_explicit_actor_requires_existing_session_before_resolution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["notes", "--actor", "helper"]) == 2

    captured = capsys.readouterr()
    assert "explicit actor targeting requires an existing session" in captured.err
    assert not (tmp_path / "session").exists()


def test_main_exact_root_explicit_actor_targets_local_actor_surface(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    root = tmp_path / "workspace"

    assert cli.main(["session", "init", "--session", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(["want", "--session", str(root), "--actor", "Claude"]) == 0
    output = capsys.readouterr().out

    claude = session_registry._resolve_bound_actor_name(root, "Claude")
    assert "Actor Want Placeholder" in output
    assert f"gotta want --actor {claude} --stdin" in output
    assert (root / "actors" / claude).is_dir()
    assert not (tmp_path / "session" / root.name / "actors").exists()


def test_main_exact_root_session_show_stays_on_exact_root_after_bind(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    root = tmp_path / "workspace"

    assert cli.main(["session", "init", "--session", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Claude", "--session", str(root)]) == 0
    capsys.readouterr()

    assert (
        cli.main(["session", "show", "--session", str(root), "--output", "json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["GOTTA_SESSION_DIR"] == str(root.resolve())
    assert payload["GOTTA_SESSION_CONTENT_DIR"] == str((root / "content").resolve())
    assert payload["GOTTA_SESSION_STATE_DIR"] == str((root / "state").resolve())
    assert payload["GOTTA_SESSION_ACTOR"] == ""


def test_main_preserves_session_subcommands(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
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


def test_main_preserves_read_option_ordering(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["read", "--head", "120", "README.md"]) == 0
    assert cli.main(["read", "--help"]) == 0

    assert seen == [["read", "--head", "120", "README.md"], ["read", "--help"]]


def test_main_preserves_todo_subcommands(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    _set_default_session_root(monkeypatch, tmp_path / "session")
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
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
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)

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


def test_main_session_show_and_doctor_auto_bootstrap_for_stable_fingerprint(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "show"]) == 0
    session_root = _grouped_root(registry, "thread-123")
    first = capsys.readouterr()
    assert first.out.strip() == str(session_root.resolve())
    assert "created a new gotta session" in first.err
    assert (session_root / "state" / "env").exists()
    assert (session_root / "content").is_dir()
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()

    assert cli.main(["session", "doctor"]) == 0
    second = capsys.readouterr()
    assert "created a new gotta session" not in second.err
    doctor_output = json.loads(second.out)
    assert doctor_output["session"]["sessionRoot"] == str(session_root.resolve())
    assert doctor_output["session"]["initialized"] is True
    assert doctor_output["runtime"]["contextId"] == "thread-123"
    assert doctor_output["runtime"]["contextSource"] == "codex_thread"
    assert doctor_output["checks"]["durableBindingsPresent"]["status"] == "ok"
    assert doctor_output["checks"]["runtimeBindingMatchesTarget"]["status"] == "ok"
    assert doctor_output["bindings"][0]["contextId"] == "thread-123"


@pytest.mark.parametrize(
    ("argv", "expected_snippet"),
    [
        (["want"], "# Want"),
        (["goal"], "# Seed Goal Placeholder"),
    ],
)
def test_main_charter_surfaces_render_on_first_stable_use(
    tmp_path: Path,
    monkeypatch,
    capsys,
    argv: list[str],
    expected_snippet: str,
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(argv) == 0

    captured = capsys.readouterr()
    session_root = _grouped_root(registry, "thread-123")
    assert expected_snippet in captured.out
    assert "gotta session init" not in captured.err
    assert "created a new gotta session" in captured.err
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()


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

    fingerprint = cli_bind._session_token("thread-123")
    claude = _actor_id(registry / "demo", "claude")
    claude_root = registry / "demo" / "actors" / claude
    notes_records = [
        json.loads(line)
        for line in (claude_root / "state" / "notes.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert notes_records[-1]["actor"] == claude
    assert notes_records[-1]["author"] == fingerprint

    activity_records = [
        json.loads(line)
        for line in (claude_root / "state" / "activity.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
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

    probe_fingerprint = cli_bind._session_token("thread-probe")
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
    state = content_env.load_state_env_at_root(actor_root)
    for key, value in state.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CODEX_THREAD_ID", "conflict-thread")
    monkeypatch.setenv("TERM_SESSION_ID", "terminal-conflict")

    assert cli.main(["actor", "status", "--output", "json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "created a new gotta session" not in captured.err
    assert claude in payload
    assert not (registry / cli_bind._session_token("conflict-thread")).exists()


def test_main_failed_session_init_seed_does_not_leave_half_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "init", "legacy mission"]) == 2
    err = capsys.readouterr().err

    session_root = _grouped_root(registry, "thread-123")
    assert "unrecognized arguments: legacy mission" in err
    assert (session_root / "WANT.md").is_file()
    assert (session_root / "GOAL.md").is_file()
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

    assert cli.main(["want", "inline text"]) == 2

    assert cli.main(["goal", "inline text"]) == 2


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
    claude = _actor_id(registry / cli_bind._session_token("thread-123"), "claude")
    actor_root = registry / cli_bind._session_token("thread-123") / "actors" / claude

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
    expected_root = shared_root / "actors" / cli_bind._session_token("thread-123")

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
    dirs = content_scope.resolve_dirs(
        content_model.CommonOptions(
            session_dir=str(actor_root),
            content_dir=str(shared_root / "content"),
            actor="claude",
        ),
        create=True,
    )
    content_env.write_session_state(
        dirs,
        {
            content_env.SESSION_ID_ENV: "retry-review",
            content_env.SESSION_ACTOR_ENV: "claude",
        },
    )
    shared_root.joinpath("session.json").write_text("{}\n", encoding="utf-8")

    assert (
        cli.main(
            ["session", "timeline", "--session", "retry-review", "--output", "json"]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["sessionDir"] == str(shared_root.resolve())
    assert "created a new gotta session" not in captured.err
    assert not (
        shared_root / "actors" / cli_bind._session_token("thread-123") / "state" / "env"
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
    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "term-session-1")

    assert cli.main(["todo", "append", "real task"]) == 0

    session_root = Path(seen[-1][1])
    assert session_root.parent.name == "actors"
    assert session_root.parent.parent == (
        tmp_path / "session" / cli_bind._session_token("term-session-1")
    )
    assert session_root.name == cli_bind._session_token("term-session-1")
    assert seen[-1][0] == ["todo", "append", "real task"]
    assert seen[-1][2] == "term-session-1"
    assert seen[-1][3] == "terminal_session"
    assert "created a new gotta session" in capsys.readouterr().err


def test_main_explicit_session_init_binds_current_context(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "registry")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    session_root = tmp_path / "explicit-session"
    assert cli.main(["session", "init", "--session", str(session_root)]) == 0
    init_err = capsys.readouterr().err
    assert "this context is now bound to that session root" in init_err
    assert "`gotta session bind '" in init_err
    assert "`--session <session-root>`" in init_err

    assert cli.main(["session", "doctor", "--output", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["sessionRoot"] == str(session_root.resolve())


def test_main_shared_session_init_receipt_prefers_session_id_reuse(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "registry")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-shared")

    assert cli.main(["session", "init", "--session", "review-demo"]) == 0
    init_err = capsys.readouterr().err

    assert "this context is now bound to that session root" in init_err
    assert "`gotta session bind review-demo`" in init_err
    assert "`--session <shared-session-id>`" in init_err
    assert "`gotta session bind '" not in init_err


def test_session_bind_accepts_exact_session_root_reference(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_default_session_root(monkeypatch, tmp_path / "registry")
    session_root = tmp_path / "explicit-session"

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-creator")
    assert cli.main(["session", "init", "--session", str(session_root)]) == 0
    capsys.readouterr()

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-reader")
    assert cli.main(["session", "bind", str(session_root), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["sessionRoot"] == str(session_root.resolve())
    assert payload["sessionId"] == content_scope.session_id(session_root)
    assert payload["actor"] == ""

    assert cli.main(["session", "doctor", "--output", "json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["session"]["sessionRoot"] == str(session_root.resolve())


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

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
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
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert (
        "Supervisor requested `failed` (operator chose to stop this actor run)." in err
    )
    assert "Any further activity may be discarded." in err
    assert f"gotta actor signoff {claude} --summary ..." in err


def test_main_does_not_warn_actor_for_runtime_stop_signal_alone(
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

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": claude,
                "label": "Claude",
                "status": "active",
                "runtime_stop_signal": "SIGTERM",
                "runtime_stop_signal_at": "2026-03-17T00:01:00Z",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert err == ""


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

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
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
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor requested `failed`" not in err


def test_main_warns_actor_when_supervisor_keeps_checking_notes_since_last_note(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Scout"]) == 0
    capsys.readouterr()
    scout = _actor_id(registry / "actor-root", "scout")
    actor_root = registry / "actor-root" / "actors" / scout
    append_actor_note(
        registry / "actor-root",
        scout,
        message="alive: first anchor",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": scout,
                "label": "Scout",
                "status": "active",
                "note_checks_since_update": 3,
                "last_note_check_at": "2026-03-21T00:05:00Z",
                "last_note_check_by": "operator-1",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert "Supervisor has checked your notes 3 times since your last note." in err
    assert "If you have real progress, land one short note now." in err


def test_main_notes_surface_warns_actor_when_note_check_pulse_is_active(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Scout"]) == 0
    capsys.readouterr()
    scout = _actor_id(registry / "actor-root", "scout")
    actor_root = registry / "actor-root" / "actors" / scout
    append_actor_note(
        registry / "actor-root",
        scout,
        message="alive: first anchor",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    (actor_root / "state" / "actor.json").write_text(
        json.dumps(
            {
                "actor": scout,
                "label": "Scout",
                "status": "active",
                "note_checks_since_update": 3,
                "last_note_check_at": "2026-03-21T00:05:00Z",
                "last_note_check_by": "operator-1",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["notes"]) == 0
    err = capsys.readouterr().err

    assert seen == [["notes"]]
    assert "Supervisor has checked your notes 3 times since your last note." in err


def test_main_shared_session_notes_view_does_not_emit_note_check_pulse(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Scout"]) == 0
    capsys.readouterr()
    scout = _actor_id(registry / "actor-root", "scout")
    actor_root = registry / "actor-root" / "actors" / scout
    append_actor_note(
        registry / "actor-root",
        scout,
        message="alive: first anchor",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    (actor_root / "state" / "actor.json").write_text(
        json.dumps(
            {
                "actor": scout,
                "label": "Scout",
                "status": "active",
                "note_checks_since_update": 3,
                "last_note_check_at": "2026-03-21T00:05:00Z",
                "last_note_check_by": "operator-1",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["notes", "--session", str(registry / "actor-root")]) == 0
    err = capsys.readouterr().err

    assert seen == [["notes", "--session", str(registry / "actor-root")]]
    assert "Supervisor has checked your notes" not in err


def test_main_stop_warning_suppresses_note_check_pulse(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Scout"]) == 0
    capsys.readouterr()
    scout = _actor_id(registry / "actor-root", "scout")
    actor_root = registry / "actor-root" / "actors" / scout
    append_actor_note(
        registry / "actor-root",
        scout,
        message="alive: first anchor",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    actor_state_path = actor_root / "state" / "actor.json"
    actor_state_path.write_text(
        json.dumps(
            {
                "actor": scout,
                "label": "Scout",
                "status": "active",
                "requested_status": "failed",
                "requested_summary": "operator chose to stop this actor run",
                "note_checks_since_update": 2,
                "last_note_check_at": "2026-03-21T00:05:00Z",
                "last_note_check_by": "operator-1",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["logs"]) == 0
    err = capsys.readouterr().err

    assert seen == [["logs"]]
    assert (
        "Supervisor requested `failed` (operator chose to stop this actor run)." in err
    )
    assert "Supervisor has checked your notes" not in err


def test_main_shared_session_inspection_does_not_emit_note_check_pulse(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    assert cli.main(["actor", "bind", "Scout"]) == 0
    capsys.readouterr()
    scout = _actor_id(registry / "actor-root", "scout")
    actor_root = registry / "actor-root" / "actors" / scout
    append_actor_note(
        registry / "actor-root",
        scout,
        message="alive: first anchor",
        author=scout,
        timestamp="2026-03-21T00:00:00Z",
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    (actor_root / "state" / "actor.json").write_text(
        json.dumps(
            {
                "actor": scout,
                "label": "Scout",
                "status": "active",
                "note_checks_since_update": 3,
                "last_note_check_at": "2026-03-21T00:05:00Z",
                "last_note_check_by": "operator-1",
            }
        ),
        encoding="utf-8",
    )
    for key, value in content_env.load_state_env_at_root(actor_root).items():
        monkeypatch.setenv(key, value)

    assert (
        cli.main(["session", "doctor", "--session", str(registry / "actor-root")]) == 0
    )
    err = capsys.readouterr().err

    assert seen == [["session", "doctor", "--session", str(registry / "actor-root")]]
    assert "Supervisor has checked your notes" not in err


def test_main_does_not_warn_nonactor_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: list[list[str]] = []
    registry = tmp_path / "session"
    _set_default_session_root(monkeypatch, registry)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert cli.main(["session", "bind", "actor-root"]) == 0
    capsys.readouterr()
    actor_root = (
        registry / "actor-root" / "actors" / cli_bind._session_token("thread-123")
    )

    def fake_gotta_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(cli_argv, "_gotta_main", fake_gotta_main)
    for key, value in content_env.load_state_env_at_root(actor_root).items():
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
        cli_bind._create_session_root(
            canonical,
            context_id=locked_context_id,
            context_source="env",
        )
        yield

    monkeypatch.setattr(cli_bind, "_session_creation_lock", fake_lock)

    root, created = cli_bind._bind_session_root(context_id, "env")

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
