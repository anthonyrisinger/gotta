from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_COMMIT_HOOK = REPO_ROOT / ".githooks" / "pre-commit"


def _run(
    cmd: list[str], *, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def test_pre_commit_rejects_partially_staged_python_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.name", "Test User"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(PRE_COMMIT_HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    hook_path.chmod(0o755)
    _run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo)

    demo = repo / "demo.py"
    demo.write_text("x=1\n", encoding="utf-8")
    _run(["git", "add", "demo.py"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)

    demo.write_text("x=1\ny=2\n", encoding="utf-8")
    _run(["git", "add", "demo.py"], cwd=repo)
    demo.write_text("x=1\ny=2\nz=3\n", encoding="utf-8")

    result = _run([str(hook_path)], cwd=repo, check=False)

    assert result.returncode == 1
    assert "partially staged Python files" in result.stderr
    assert "demo.py" in result.stderr
    staged = _run(["git", "show", ":demo.py"], cwd=repo)
    assert staged.stdout == "x=1\ny=2\n"
    assert demo.read_text(encoding="utf-8") == "x=1\ny=2\nz=3\n"


@pytest.mark.parametrize(
    ("relative_path", "expected_path"),
    [
        ("src/gotta/bad_name/read.py", "src/gotta/bad_name"),
        ("tests/test_bad_name.py", "tests/test_bad_name.py"),
    ],
)
def test_pre_commit_rejects_invalid_path_shapes(
    tmp_path: Path, relative_path: str, expected_path: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    _run(["git", "init", "-q"], cwd=repo)

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(PRE_COMMIT_HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    hook_path.chmod(0o755)

    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")

    result = _run([str(hook_path)], cwd=repo, check=False)

    assert result.returncode == 1
    assert "path-shape policy violated" in result.stderr
    assert expected_path in result.stderr
