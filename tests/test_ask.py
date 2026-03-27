from __future__ import annotations

from gotta.builtin import PluginSpec
from gotta.plugins import ask


def test_ask_dispatches_to_registered_surface(monkeypatch) -> None:
    seen: list[list[str]] = []

    monkeypatch.setattr(ask, "available_asks", lambda: ["docs"])
    monkeypatch.setattr(
        ask,
        "ask_spec",
        lambda name: (
            PluginSpec(
                name="docs",
                description="demo",
                runner=lambda argv: seen.append(argv) or 0,
            )
            if name == "docs"
            else None
        ),
    )

    assert ask.main(["docs", "status", "query"]) == 0
    assert seen == [["status", "query"]]


def test_ask_reports_unknown_surface(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: ["docs", "notes"])
    monkeypatch.setattr(ask, "ask_spec", lambda name: None)

    assert ask.main(["unknown"]) == 2
    assert "unknown gotta ask surface: unknown" in capsys.readouterr().err


def test_ask_reports_no_installed_surfaces(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ask, "available_asks", lambda: [])

    assert ask.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "none installed" in output
    assert "gotta.ask" in output
