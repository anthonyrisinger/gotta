from __future__ import annotations

from gotta.builtin import CommandPath, PackageSpec, SurfaceBinding, SurfaceSpec
from gotta.plugins import ask


def test_ask_dispatches_to_registered_surface(monkeypatch) -> None:
    seen: list[list[str]] = []

    monkeypatch.setattr(ask, "available_asks", lambda: ["docs"])
    monkeypatch.setattr(
        ask,
        "ask_binding",
        lambda name: (
            SurfaceBinding(
                name="docs",
                command_path=CommandPath(("ask", "docs")),
                package=PackageSpec("demo"),
                surface=SurfaceSpec(
                    name="docs",
                    description="demo",
                    runner=lambda argv: seen.append(argv) or 0,
                ),
            )
            if name == "docs"
            else None
        ),
    )

    assert ask.main(["docs", "status", "query"]) == 0
    assert seen == [["status", "query"]]


def test_ask_reports_unknown_surface(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: ["docs", "notes"])
    monkeypatch.setattr(ask, "ask_binding", lambda name: None)

    assert ask.main(["unknown"]) == 2
    assert "unknown gotta ask surface: unknown" in capsys.readouterr().err


def test_ask_reports_no_installed_surfaces(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: [])

    assert ask.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "none installed" in output
    assert "gotta.ask" in output


def test_ask_materializes_by_default_for_named_bindings(monkeypatch) -> None:
    monkeypatch.setattr(
        ask,
        "ask_binding",
        lambda name: (
            SurfaceBinding(
                name="docs",
                command_path=CommandPath(("ask", "docs")),
                package=PackageSpec("demo"),
                surface=SurfaceSpec(
                    name="docs",
                    description="demo",
                    runner=lambda argv: 0,
                ),
            )
            if name == "docs"
            else None
        ),
    )

    assert ask.should_materialize(["docs", "status", "query"]) is True


def test_ask_help_all_does_not_duplicate_binding_header(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: ["docs"])

    def fake_runner(argv: list[str]) -> int:
        assert argv == ["--help-all"]
        print(
            "## gotta ask docs\n\n"
            "usage: gotta ask docs [query ...]\n\n"
            "---\n\n"
            "End of recursive help for `gotta ask docs`.\n"
            "Use plain `--help` for the root surface only.\n"
        )
        return 0

    monkeypatch.setattr(
        ask,
        "ask_binding",
        lambda name: (
            SurfaceBinding(
                name="docs",
                command_path=CommandPath(("ask", "docs")),
                package=PackageSpec("demo"),
                surface=SurfaceSpec(
                    name="docs",
                    description="demo",
                    runner=fake_runner,
                ),
            )
            if name == "docs"
            else None
        ),
    )

    assert ask.main(["--help-all"]) == 0
    output = capsys.readouterr().out
    assert output.count("## gotta ask docs") == 1


def test_ask_help_all_adds_header_for_plain_binding_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: ["docs"])

    def fake_runner(argv: list[str]) -> int:
        assert argv == ["--help-all"]
        print("usage: gotta ask docs [query ...]")
        return 0

    monkeypatch.setattr(
        ask,
        "ask_binding",
        lambda name: (
            SurfaceBinding(
                name="docs",
                command_path=CommandPath(("ask", "docs")),
                package=PackageSpec("demo"),
                surface=SurfaceSpec(
                    name="docs",
                    description="demo",
                    runner=fake_runner,
                ),
            )
            if name == "docs"
            else None
        ),
    )

    assert ask.main(["--help-all"]) == 0
    output = capsys.readouterr().out
    assert "## gotta ask docs" in output
    assert "usage: gotta ask docs [query ...]" in output
