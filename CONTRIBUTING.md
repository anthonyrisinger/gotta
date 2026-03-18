# Contributing

Thanks for contributing to `gotta`.

## Development Setup

If you do not already have `uv`, install it first from Astral's official
[installation guide](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --python 3.10 --extra dev
```

## Before You Open A Change

Run the full local gate:

```bash
uv run pytest -q
uv run ruff check src tests
uv run python -m py_compile $(find src tests -name '*.py' -print)
uv run python -m vulture src tests --min-confidence 80
uv run python -m radon cc src tests -s
uv run lizard src tests
uv build --python 3.10 --clear
uvx twine check dist/*
```

If you are preparing a PyPI upload, rebuild first and then validate the fresh
artifacts:

```bash
uv build --python 3.10 --clear
uvx twine check dist/*
uv publish --dry-run
```

## Project Expectations

- Prefer native `gotta` surfaces over shell-side workarounds when testing or
  extending behavior.
- Preserve the canonical split between rewrite-on-purpose files and append-only
  canonical state.
- Record operator-visible seams in `oops`; do not hide workflow friction in
  tribal knowledge.
- Remove residue instead of layering new logic on top of dead or transitional
  code.
- Keep examples and fixtures generic and public. Do not introduce private
  corpora, private domains, or company-specific workflows into docs or tests.

## Pull Requests

- Keep changes scoped to one logical unit.
- Include tests for user-visible behavior changes.
- Update docs when changing the public contract.
- Call out intentional breaking changes explicitly.
