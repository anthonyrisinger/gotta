from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STUDY_PATH = REPO_ROOT / "scripts" / "study"
RELEASE_PATH = REPO_ROOT / "scripts" / "release"
SORTED_CORE_SLICE = tuple(
    sorted(
        (
            "tests/test_cli.py",
            "tests/test_dispatch.py",
            "tests/test_content.py",
            "tests/test_read.py",
            "tests/test_session.py",
        )
    )
)


def _load_driver():
    loader = importlib.machinery.SourceFileLoader("study_test_module", str(STUDY_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    assert spec.loader is loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def test_resolve_mode_defaults_to_quick() -> None:
    driver = _load_driver()

    mode = driver.resolve_mode([])

    assert mode.quick is True
    assert mode.full is False
    assert mode.deep is False
    assert mode.types is False
    assert mode.verbose is False


def test_resolve_mode_supports_discover() -> None:
    driver = _load_driver()

    mode = driver.resolve_mode(["--discover"])

    assert mode.quick is False
    assert mode.full is False
    assert mode.discover is True
    assert mode.deep is False
    assert mode.types is False


def test_resolve_mode_promotes_deep_and_types_to_full() -> None:
    driver = _load_driver()

    deep = driver.resolve_mode(["--deep"])
    types = driver.resolve_mode(["--types"])
    both = driver.resolve_mode(["--full", "--deep", "--types"])

    assert deep.full is True and deep.deep is True
    assert types.full is True and types.types is True
    assert both.full is True and both.deep is True and both.types is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--quick", "--full"],
        ["--quick", "--deep"],
        ["--quick", "--types"],
        ["--quick", "--discover"],
        ["--discover", "--full"],
        ["--discover", "--deep"],
        ["--discover", "--types"],
    ],
)
def test_resolve_mode_rejects_invalid_quick_mixes(argv: list[str]) -> None:
    driver = _load_driver()

    with pytest.raises(SystemExit):
        driver.resolve_mode(argv)


def test_parse_porcelain_z_handles_rename_pairs() -> None:
    driver = _load_driver()

    parsed = driver.parse_porcelain_z(b"RM scripts/study\x00scripts/old_study\x00")

    assert parsed == ("scripts/study", "scripts/old_study")


def test_select_pytest_targets_uses_clean_tree_fallback() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets((), REPO_ROOT)

    assert selection.reason == "clean tree fallback"
    assert selection.targets == SORTED_CORE_SLICE


def test_select_pytest_targets_uses_repo_control_fallback() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(("pyproject.toml",), REPO_ROOT)

    assert selection.reason == "repo-control fallback"
    assert selection.targets == SORTED_CORE_SLICE


def test_residue_paths_find_python_cache_residue(tmp_path: Path) -> None:
    driver = _load_driver()
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__pycache__" / "x.cpython-310.pyc").write_bytes(b"x")

    residue = driver.residue_paths(tmp_path)

    assert residue == (
        "src/pkg/__pycache__",
        "src/pkg/__pycache__/x.cpython-310.pyc",
    )


def test_select_pytest_targets_unions_changed_tests_and_source_mappings() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(
        (
            "tests/test_cli.py",
            "src/gotta/resolve/read.py",
        ),
        REPO_ROOT,
    )

    assert selection.reason == "mapped from changed paths"
    assert selection.targets == (
        "tests/test_cli.py",
        "tests/test_dispatch.py",
        "tests/test_read.py",
    )


def test_select_pytest_targets_prefers_exact_mapping_over_prefix() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(("src/gotta/plugins/read.py",), REPO_ROOT)

    assert selection.targets == (
        "tests/test_dispatch.py",
        "tests/test_read.py",
    )


def test_select_pytest_targets_uses_longest_matching_prefix() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(
        ("src/gotta/plugins/session/graph/payload.py",),
        REPO_ROOT,
    )

    assert selection.targets == ("tests/test_session.py",)


def test_select_pytest_targets_filters_nonexistent_targets_for_deleted_paths() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(
        ("tests/test_missing.py", "src/gotta/resolve/deleted.py"),
        REPO_ROOT,
    )

    assert selection.targets == (
        "tests/test_dispatch.py",
        "tests/test_read.py",
    )


def test_select_pytest_targets_selects_study_test_for_script_changes() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(("scripts/study",), REPO_ROOT)

    assert selection.reason == "mapped from changed paths"
    assert selection.targets == ("tests/test_study.py",)


def test_select_pytest_targets_uses_unmapped_source_fallback() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(
        ("src/gotta/unknown_module.py",), REPO_ROOT
    )

    assert selection.reason == "unmapped source fallback"
    assert selection.targets == SORTED_CORE_SLICE


def test_select_pytest_targets_reports_no_changed_test_targets_for_docs_only() -> None:
    driver = _load_driver()

    selection = driver.select_pytest_targets(("README.md",), REPO_ROOT)

    assert selection.reason == "no changed test targets"
    assert selection.targets == ()


def test_run_summary_prints_compact_success_line(capsys) -> None:
    driver = _load_driver()

    result = driver.run_summary(
        [sys.executable, "-c", "print('ok')"],
        cwd=REPO_ROOT,
        label="demo step",
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "ok " in output
    assert "demo step" in output
    assert "command:" not in output


def test_summarize_lizard_hotspots_orders_by_ccn_then_length() -> None:
    driver = _load_driver()

    summary = driver.summarize_lizard_hotspots(
        "\n".join(
            [
                "================================================",
                "  NLOC    CCN   token  PARAM  length  location  ",
                "------------------------------------------------",
                "      20     10    100      1      20 alpha@1-20@src/a.py",
                "      30     10    120      2      30 beta@1-30@src/b.py",
                "      10      3     50      1      10 gamma@1-10@src/c.py",
            ]
        ),
        limit=2,
    )

    assert "beta@1-30@src/b.py" in summary.splitlines()[1]
    assert "alpha@1-20@src/a.py" in summary.splitlines()[2]


def test_summarize_lizard_hotspots_dedupes_duplicate_locations() -> None:
    driver = _load_driver()

    summary = driver.summarize_lizard_hotspots(
        "\n".join(
            [
                "      20     10    100      1      20 alpha@1-20@src/a.py",
                "      20     10    100      1      20 alpha@1-20@src/a.py",
            ]
        ),
        limit=5,
    )

    assert summary.splitlines() == [
        "ccn  len  nloc  location",
        " 10   20    20  alpha@1-20@src/a.py",
    ]


def test_summarize_pyright_groups_diagnostics_by_file() -> None:
    driver = _load_driver()

    summary = driver.summarize_pyright(
        "\n".join(
            [
                f"{REPO_ROOT}/src/gotta/a.py:1:1 - error: first",
                f"{REPO_ROOT}/src/gotta/a.py:2:1 - error: second",
                f"{REPO_ROOT}/src/gotta/b.py:3:1 - error: third",
            ]
        ),
        repo_root=REPO_ROOT,
        limit=5,
    )

    assert "3 diagnostics across 2 files" in summary
    assert "   2  src/gotta/a.py" in summary
    assert "   1  src/gotta/b.py" in summary


def test_summarize_semgrep_counts_findings_by_rule() -> None:
    driver = _load_driver()

    summary = driver.summarize_semgrep(
        '{"results":[{"check_id":"study.env-read"},{"check_id":"study.env-read"},{"check_id":"study.durable-write"}]}',
        limit=5,
    )

    assert "3 findings across 2 rules" in summary
    assert "   2  study.env-read" in summary
    assert "   1  study.durable-write" in summary


def test_summarize_semgrep_accepts_json_with_trailing_text() -> None:
    driver = _load_driver()

    summary = driver.summarize_semgrep(
        (
            '{"results":[{"check_id":"study.env-read"}]}'
            "\n\nScan completed successfully.\n"
        ),
        limit=5,
    )

    assert summary == "\n".join(
        [
            "1 findings across 1 rules",
            "   1  study.env-read",
        ]
    )


def test_run_quick_prints_caveat_and_selection_reason(monkeypatch, capsys) -> None:
    driver = _load_driver()
    calls: list[str] = []

    monkeypatch.setattr(driver, "run_namespace_check", lambda repo_root, verbose: 0)
    monkeypatch.setattr(driver, "run_residue_check", lambda repo_root, verbose: 0)
    monkeypatch.setattr(
        driver,
        "run_summary",
        lambda command, cwd, label: calls.append(label) or 0,
    )
    monkeypatch.setattr(
        driver,
        "changed_paths",
        lambda repo_root: ("src/gotta/resolve/read.py",),
    )

    assert driver.run_quick(REPO_ROOT, verbose=False) == 0
    output = capsys.readouterr().out

    assert "working-tree loop, not a repo-health signal" in output
    assert (
        "pytest targets (mapped from changed paths): tests/test_dispatch.py, tests/test_read.py"
        in output
    )
    assert calls == ["ruff check", "ruff format --check", "pytest"]


def test_run_discover_prints_discovery_caveat(monkeypatch, capsys) -> None:
    driver = _load_driver()
    calls: list[str] = []

    monkeypatch.setattr(driver, "run_namespace_check", lambda repo_root, verbose: 0)
    monkeypatch.setattr(driver, "run_residue_check", lambda repo_root, verbose: 0)
    monkeypatch.setattr(
        driver,
        "print_discovery_section",
        lambda title, command, _renderer, cwd: calls.append(title),
    )

    assert driver.run_discover(REPO_ROOT) == 0
    output = capsys.readouterr().out

    assert "discover mode is for choosing the next squeeze" in output
    assert calls == [
        "Slow Tests",
        "Hotspot Functions",
        "Complexity Radar",
        "Type Pressure",
        "Architecture",
        "Semantic Probes",
    ]


def test_release_script_uses_full_study_gate() -> None:
    assert "./scripts/study --full" in RELEASE_PATH.read_text(encoding="utf-8")


def test_study_script_is_python_owner() -> None:
    script = (REPO_ROOT / "scripts" / "study").read_text(encoding="utf-8")

    assert script.startswith("#!/usr/bin/env python3")
    assert "raise SystemExit(main())" in script
