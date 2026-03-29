# Contributing

Thanks for contributing to `gotta`.

## Development Setup

If you do not already have `uv`, install it first from Astral's official
[installation guide](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --python 3.10 --extra dev
./scripts/install-hooks
```

`./scripts/install-hooks` sets `core.hooksPath` to the repo-owned
`.githooks/` directory. The default pre-commit hook formats staged `*.py`
files with `uv run ruff format`, re-stages them, and then runs
`uv run ruff check` on those same files. Partially staged Python files are
rejected so the hook never pulls unstaged edits into the commit.

## Before You Open A Change

Run the blocking local gate:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run python -m vulture src tests --min-confidence 80
```

Then run the structural pressure and packaging checks:

```bash
uv run python -m radon cc src tests -s
uv run lizard src tests
uv build --python 3.10 --clear
uvx twine check dist/*
```

For regular study and maintenance work, use the repo wrapper:

```bash
./scripts/study
./scripts/study --quick
./scripts/study --discover
./scripts/study --full
./scripts/study --deep
./scripts/study --types
```

`./scripts/study` defaults to the quick working-tree loop: namespace
policy, python-residue hygiene, full-tree `ruff check`, full-tree `ruff format
--check`, and a deterministic targeted pytest slice derived from the current
working tree. Treat that quick pass as local edit-loop signal, not branch
health. `--quick` is the explicit alias for that same mode.

`./scripts/study --discover` is the discovery surface. It is designed for
exploratory diagnosis rather than validating one local edit: it
summarizes slow tests, live complexity hotspots, pyright file pressure,
import-linter contract status, and semgrep rule counts in one pass.

`./scripts/study --full` preserves the branch-grade pass and remains the
release-validation contract. It keeps the full pytest run, vulture, formatter
gate, durations pass, advisory pressure tools, and local study binaries such
as `cloc`, `ctags`, and `ast-grep` when they are installed. `--deep` and
`--types` both imply `--full`; `--deep` adds `import-linter` and `semgrep`,
and `--types` adds a source-only `pyright` pass as a pressure map.

Quick and full modes both treat generated `__pycache__/` directories and
`*.pyc` files under `src/` as residue. The study runner scrubs existing
residue before the gate starts and suppresses new bytecode emission
in its subprocesses so the gate itself does not leave new source-tree exhaust
behind.

If you are preparing a PyPI upload, rebuild first and then validate the fresh
artifacts through the canonical release wrapper:

```bash
./scripts/release prepare patch
./scripts/release prepare minor
./scripts/release publish
./scripts/release publish --notes-file /tmp/gotta-release.md
./scripts/release github
./scripts/release github 0.41.0
./scripts/release patch
./scripts/release minor
```

`./scripts/release` is the canonical path for shipping. It runs
`./scripts/study --full` on the unbumped tree, then bumps the version with `uv`,
validates fresh artifacts, smoke-installs the wheel, commits the release
metadata, and either stops for review or pushes, publishes, tags, and creates
the GitHub Release. `prepare`
stops with a committed release bump so the exact release candidate can be
reviewed. `publish` validates the current prepared version and then pushes
`main`, publishes to PyPI, waits for public propagation, pushes the annotated
`vX.Y.Z` tag, and creates the GitHub Release. `github [version]` is the retry
and retrofit path for the GitHub tag/release layer without republishing to
PyPI. Set `GOTTA_RELEASE_NOTES_FILE=/path/to/release.md` or pass
`--notes-file /path/to/release.md` to publish a hand-written GitHub Release
body verbatim instead of the generated fallback. It reads the PyPI token from
`~/.pypirc` under `[pypi].password` and expects an authenticated `gh` CLI for
the GitHub release layer.

## Project Expectations

- Prefer native `gotta` surfaces over shell-side workarounds when testing or
  extending behavior.
- Preserve the canonical split between rewrite-on-purpose files and append-only
  canonical state.
- Avoid adding new core-to-plugin import edges. Keep stored rendering and lead
  extraction in core or neutral projection layers rather than routing through
  presentation plugins.
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

For the explicit 1.0 architecture direction, read
[ARCHITECTURE.md](https://github.com/anthonyrisinger/gotta/blob/main/docs/ARCHITECTURE.md)
alongside
[TODO.md](https://github.com/anthonyrisinger/gotta/blob/main/docs/TODO.md).
Together they capture the settled judgment
about the irreducible core, surface model, artifact ledger, session model,
backend contracts, type-system direction, and the explicit `exec` surface.

Before deep provider or algorithm work, freeze the semantic shapes. The current
live drifts to eliminate first are:

- the flat `PluginSpec` registry center in `builtin.py`
- filesystem-shaped ledger exports in `content/model.py` and `content/store.py`
- federated `search` fallback capture in `plugins/search.py`
- anonymous `dict`-shaped derived payloads where the architecture already
  assigns stable names

`gotta` is a surface-oriented CLI where the core orchestrates session
lifecycle, content materialization, and actor coordination. The live
implementation routes many surfaces through a flat `PluginSpec` registry. The
canonical 1.0 split is:

- surface binding
- distribution package
- provider bundle
- capability
- backend

### How A Command Flows

```
gotta jira search "retry budget"
  │
  ├─ cli/             normalize argv, bind/discover session, hydrate context
  ├─ builtin.py        registry shim for surfaces via entry points
  ├─ dispatch/main.py   orchestrate surface runtime over dispatch phases
  ├─ resolve/           resolve read/search targets and invocation metadata
  ├─ content/           materialize captured output to content store
  └─ stdout             emit receipt with artifact and content locators
```

### Module Tour

Core infrastructure:

- **`cli/`** — CLI kernel package. `entry.py` owns top-level orchestration,
  `argv.py` owns help/version and surface extraction, `bind.py` owns session
  root creation/binding, `select.py` owns root-selection policy, `env.py`
  owns session environment hydration, and `notice.py` owns operator-facing
  receipts and warnings.
- **`builtin.py`** — Extension registry shim (`PluginSpec`), discovery
  via setuptools entry points, and core surface registrations.
- **`dispatch/`** — Dispatch kernel package. `main.py` orchestrates plugin
  runtime, `option.py` owns shared flag stripping, `stream.py` owns captured
  stdout/stderr, `budget.py` owns interactive output truncation, `metadata.py`
  derives source metadata, `materialize.py` writes artifacts, `receipt.py`
  emits receipts, and `runtime.py` owns session/runtime scoping.
- **`content/`** — Evidence-store kernel package. `model.py` owns content
  dataclasses and errors, `path.py` owns locators and path normalization,
  `file.py` owns atomic/private file operations, `context.py` owns context and
  stdin probes, `env.py` owns session environment export/import, `scope.py`
  owns shared content/session resolution, `activity.py` owns append-only
  activity logging, `store.py` owns artifact materialization and content-store
  scans, and `stamp.py` owns UTC timestamp helpers.
- **`resolve/`** — Resolution kernel package. `read.py` owns `gotta read`
  target parsing and routed-target detection, `search.py` owns the top-level
  plain-text search contract, `route.py` owns provider locator tokenization,
  `canon.py` derives canonical locators, `name.py` derives preferred names and
  content types, `intent.py` owns artifact/session-access policy, and
  `invoke.py` resolves a command into one canonical invocation shape.
- **`session/`** — Session kernel package. `scope.py` resolves exact/shared/actor
  roots, `registry.py` owns actor identity and metadata, `bootstrap.py`
  scaffolds session and actor surfaces, `activity/` owns actor/session
  activity (`file.py`, `record.py`, `summary.py`, and `note.py`), `status/`
  owns actor lifecycle state (`marker.py`, `blocker.py`, `progress.py`,
  `payload.py`, `todo.py`, and `bind.py`), and `charter.py` owns want/goal
  text surfaces. The package itself is the `gotta.session` import boundary;
  there are no `__init__.py` files under `src/`.

### Study Tooling

The repo now carries two explicit study configs:

- `.importlinter` for executable architecture contracts
- `.semgrep/study.yml` for lightweight invariant probes

Recommended optional binaries for deeper study are `cloc`, `universal-ctags`,
`ast-grep`, `tree-sitter`, `pyan3`, `scip-python`, `repomix`, and `CodeQL`.
They are not all release gates, but they are valuable for understanding and
refactoring the codebase safely.

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

- **`lead/`** — Lead kernel package. `model.py` owns `LeadMention` and cache
  identity, `canon.py` canonicalizes external targets, `query.py` derives
  search-seed queries, `extract.py` mines explicit and semantic leads,
  `cache.py` owns `leads.json`, `edge.py` builds source-to-target edges,
  `aggregate.py` reduces edges into lead sources, `resolve.py` resolves lead
  targets back to stored snapshots, and `snapshot.py` owns content-snapshot
  labeling.
- **`source/`** — Source metadata package. `stamp.py` owns timestamp
  normalization and metadata derivation, `visibility.py` owns visibility
  classification and selection, and `render.py` owns metadata rendering.

State management:

- **`friction.py`** — Friction/oops channel. Structured friction events as
  append-only JSONL with Markdown projection.
- **`projection.py`** — Bidirectional JSONL-to-Markdown serialization. Append-only
  JSONL is canonical truth; Markdown projections are derived for readability.
- **`logs.py`**, **`notes/`**, **`todo.py`** — Per-channel state surfaces using
  the same JSONL-plus-projection pattern. `notes/file.py` owns canonical actor
  note state, `notes/voice.py` owns voice/readiness inference, and
  `notes/render.py` owns payload and Markdown projection.

Utilities:

- **`config.py`** — User config and state path helpers. OS-native directories
  via `platformdirs`, TOML config loading, provider environment resolution.
- **`helptext.py`** — Recursive help rendering. Traverses argparse parser trees
  and formats long-form help with boilerplate stripping.
- **`compat.py`** — Python version compatibility shims.

### Live Extension Contract

An installable extension is registered as a `PluginSpec` in `builtin.py`. That
is the live registration shim, not the final 1.0 doctrine.

In the live codebase, a `PluginSpec` has:

- **`runner`** — the callable that executes the command (`main(argv) -> int`)
- **`session_access`** — `"none"`, `"read"`, or `"write"` (controls whether a
  session is required and whether output is materialized)
- **`route_target`** — optional callback for URL-based routing from `gotta read`
- **`route_priority`** — numeric priority for route conflicts (lower wins)
- **`canonical_locator`** / **`preferred_name`** / **`infer_content_type`** —
  optional callbacks for materialization metadata

Extensions register through the `gotta.plugins` entry-point group in
`pyproject.toml`. Core surface registrations are defined in `builtin.py`;
external extensions in separate distributions shadow core registrations only if
explicitly prioritized.

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
