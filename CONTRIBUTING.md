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
uv run python -m vulture src tests --min-confidence 80
uv run python -m radon cc src tests -s
uv run lizard src tests
uv build --python 3.10 --clear
uvx twine check dist/*
```

If you are preparing a PyPI upload, rebuild first and then validate the fresh
artifacts through the canonical release wrapper:

```bash
./scripts/release patch
./scripts/release minor
```

`./scripts/release` is the canonical path for shipping. It bumps the version
with `uv`, runs the release gate, validates fresh artifacts, smoke-installs the
wheel, commits the release metadata, pushes `main`, publishes to PyPI, and
waits for public propagation. It reads the PyPI token from `~/.pypirc` under
`[pypi].password`.

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

## Architecture Overview

`gotta` is a plugin-based CLI where the core orchestrates session lifecycle,
content materialization, and actor coordination. Plugins provide the
domain-specific surfaces.

### How A Command Flows

```
gotta jira search "retry budget"
  │
  ├─ main.py          resolve fingerprint, bind/discover session, detect repo
  ├─ builtin.py        discover plugin via entry points (gotta.plugins group)
  ├─ dispatch.py        split common options, run plugin, capture stdout
  ├─ invocation.py      derive canonical locator, preferred name, content type
  ├─ content.py         materialize captured output to content store
  └─ stdout             emit receipt with artifact and content locators
```

### Module Tour

Core infrastructure:

- **`main.py`** — CLI entrypoint. Normalizes help aliases, resolves session
  context, dispatches to plugin runners, shows actor stop warnings.
- **`builtin.py`** — Plugin contract (`PluginSpec`), discovery via setuptools
  entry points, core plugin factory registrations. Plugins declare a runner,
  session access mode, and optional routing/materialization/naming callbacks.
- **`dispatch.py`** — Plugin runtime. Splits common options, captures stdout
  through `CapturedStdout`, materializes output to the content store, derives
  source metadata from JSON/Markdown timestamps, emits receipts.
- **`content.py`** — Evidence store. SHA-256-keyed content directory with atomic
  writes, append-only manifest (`manifest.jsonl`), activity logging, session
  environment export/import, context binding resolution.
- **`invocation.py`** — Invocation resolution. Routes provider-agnostic targets
  to provider plugins, derives canonical locators per provider, determines
  preferred output names and whether to materialize.
- **`session.py`** — Session-level synthesis surfaces: manifest, timeline, graph,
  leads, analyze. Actor lifecycle management (bind, launch, stall detection,
  signoff). Charter surface operations (want, goal, todo).

Session topology and identity:

- **`topology.py`** — Filesystem schema for sessions. Path structure, identity
  normalization, fingerprint binding symlinks, grouped vs shared session roots.
- **`binding.py`** — Maps current execution context to a session root. Ensures
  actor session directories, links content, updates session metadata.
- **`actor.py`** — Actor-session path helpers. Identity resolution, session root
  derivation, supervisor stop state inspection.
- **`actors.py`** — Thread-local actor context (`ActorContext`) for the current
  execution frame. Resolves speaker and callee identities.

Evidence and synthesis:

- **`target.py`** — Read target resolution. Parses `gotta read` invocations to
  resolve local files, artifacts, URLs, and provider-routed targets.
- **`routing.py`** — Canonical locator parsing. Converts locators to plugin argv
  for re-invocation.
- **`leads.py`** — Lead extraction. Detects URLs, Jira keys, canonical locators,
  and Slack permalinks in artifact text. Classifies leads and generates followup
  commands.
- **`source.py`** — Source metadata extraction. Normalizes timestamps from JSON,
  Markdown, and Slack formats into classified ISO-8601 timestamps.

State management:

- **`friction.py`** — Friction/oops channel. Structured friction events as
  append-only JSONL with Markdown projection.
- **`projection.py`** — Bidirectional JSONL-to-Markdown serialization. Append-only
  JSONL is canonical truth; Markdown projections are derived for readability.
- **`logs.py`**, **`notes.py`**, **`todo.py`** — Per-channel state surfaces using
  the same JSONL-plus-projection pattern.

Utilities:

- **`config.py`** — User config and state path helpers. OS-native directories
  via `platformdirs`, TOML config loading, provider environment resolution.
- **`helptext.py`** — Recursive help rendering. Traverses argparse parser trees
  and formats long-form help with boilerplate stripping.
- **`compat.py`** — Python version compatibility shims.

### Plugin Contract

A plugin is a `PluginSpec` (defined in `builtin.py`) with:

- **`runner`** — the callable that executes the command (`main(argv) -> int`)
- **`session_access`** — `"none"`, `"read"`, or `"write"` (controls whether a
  session is required and whether output is materialized)
- **`route_target`** — optional callback for URL-based routing from `gotta read`
- **`route_priority`** — numeric priority for route conflicts (lower wins)
- **`canonical_locator`** / **`preferred_name`** / **`infer_content_type`** —
  optional callbacks for materialization metadata

Plugins register through the `gotta.plugins` entry-point group in
`pyproject.toml`. Core plugins are defined in `builtin.py`; external plugins
in separate distributions shadow core plugins only if explicitly prioritized.

### Testing Patterns

Tests live in `tests/` and use pytest with three main fixtures:

- **`monkeypatch`** — mock functions, environment variables, and working
  directory. Provider tests mock all API calls; no real external service
  requests.
- **`tmp_path`** — isolated filesystem for session roots and content stores.
- **`capsys`** — capture and assert stdout/stderr output.

To test a new plugin:

1. Create `tests/test_yourplugin.py`.
2. Mock provider APIs with `monkeypatch.setattr`.
3. Set up session roots with `tmp_path` and bind context with
   `monkeypatch.setenv`.
4. Call the plugin runner directly and assert stdout and filesystem state.

No external test dependencies beyond pytest. No `conftest.py` needed for simple
cases; fixtures are inline.
