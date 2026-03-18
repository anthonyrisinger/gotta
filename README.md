# Gotta

`gotta` is a session-rooted CLI for evidence retrieval, durable investigation
state, and linked peer workflows.

The public surface stays small:

```bash
gotta <plugin> ...
```

`gotta ...` is the canonical operator path. It binds or discovers the right
session for the current context, hydrates the runtime environment, and dispatches
the requested plugin with that session active. You do not need to `cd` into the
session root first.

## Why It Is Shaped This Way

`gotta` is deliberately native-first:

- prefer provider-native retrieval and mutation surfaces over ad hoc shell
  workarounds
- keep durable truth in session state, not in terminal scrollback
- make every material read reopenable through native locators

The normal investigation loop expands and compresses repeatedly:

1. retrieve one or two strong anchors through provider-native search or read
2. expand outward into adjacent evidence
3. compress that evidence web through `session manifest`, `timeline`, `graph`,
   `leads`, and `analyze`
4. record friction and broken affordances in `oops`
5. refine the next retrieval wave from durable state instead of memory

`OOPS.md` is not incidental note-taking. `state/oops.jsonl` is the canonical
friction log, projected into `OOPS.md`, and is meant to capture operator-visible
seams: misleading contracts, broken continuations, coverage gaps, and workflow
friction worth fixing.

## Installation

If you do not already have `uv`, install it first from Astral's official
[installation guide](https://docs.astral.sh/uv/getting-started/installation/).

Install with `uv`:

```bash
uv tool install gotta
```

That installs the `gotta` entrypoint.

If you want to try the CLI without installing it permanently:

```bash
uvx --from gotta gotta --help
```

`gotta` currently supports Python `>=3.10`.

If you are developing on the repo, sync a local uv-managed environment:

```bash
uv sync --python 3.10 --extra dev
```

Installed entrypoints:

```bash
gotta ...
```

## Development And Quality Gate

The core local gate is:

```bash
uv run pytest -q
uv run ruff check src tests
uv run python -m py_compile $(find src tests -name '*.py' -print)
uv run python -m vulture src tests --min-confidence 80
```

Structural pressure tools are part of the maintenance discipline:

```bash
uv run python -m radon cc src tests -s
uv run lizard src tests
uv build --python 3.10 --clear
uvx twine check dist/*
```

`pytest`, `ruff`, `py_compile`, and `vulture` are the correctness and hygiene
gate. `radon` and `lizard` are not bug finders; they expose responsibility
concentration and complexity hotspots so cleanup removes residue instead of
burying it.

## Configuration And Durable State

Durable provider config lives in `GOTTA_CONFIG_FILE` or, by default, under
gotta's OS-native config directory:

- macOS: `~/Library/Application Support/gotta/gotta.toml`
- Linux: `~/.config/gotta/gotta.toml`
- Windows: `%AppData%\\gotta\\gotta.toml`

Example:

```toml
[providers.atlassian.env]
GOTTA_ATLASSIAN_OAUTH_CLIENT_ID = "your-client-id"
GOTTA_ATLASSIAN_OAUTH_CLIENT_SECRET = "your-client-secret"
GOTTA_ATLASSIAN_OAUTH_REDIRECT_URI = "http://localhost:8080/callback"
GOTTA_ATLASSIAN_TOOLSETS = "all"
GOTTA_JIRA_BASE_URL = "https://example.atlassian.net"
GOTTA_CONFLUENCE_BASE_URL = "https://example.atlassian.net/wiki"

[providers.google.env]
GOTTA_GOOGLE_OAUTH_CLIENT_ID = "your-client-id"
GOTTA_GOOGLE_OAUTH_CLIENT_SECRET = "your-client-secret"
GOTTA_GOOGLE_OAUTH_REDIRECT_URI = "http://localhost:8091/callback"

[providers.slack.env]
GOTTA_SLACK_WORKSPACE = "your-workspace"
```

Durable OAuth state lands under gotta's OS-native state directory:

- macOS: `~/Library/Application Support/gotta/auth/`
- Linux: `~/.local/state/gotta/auth/`
- Windows: `%LocalAppData%\\gotta\\auth\\`

## Canonical Session Model

The canonical top-level root is a session root under gotta's OS-native data
directory:

```text
<gotta data dir>/sessions/<session-id>/
```

Examples:

- macOS: `~/Library/Application Support/gotta/sessions/<session-id>/`
- Linux: `~/.local/share/gotta/sessions/<session-id>/`

Session roots are context-derived, not mission-name-derived.

Each session root owns:

```text
$WS/
  state/
    env
  bin/
  content/
  graph.mmd
  graph.json
  semantic-graph.mmd
  semantic-graph.json
  summary.json
```

Stored artifacts have two native reopen handles:

- `artifact:<preferred-name>@<digest12>`
- `content:<sha256>`

Both resolve through `gotta read ...`, so emitted evidence does not require
manual manifest spelunking just to continue.

The content store is first-class:

- the filesystem under `$WS/content` is the durable evidence graph
- `manifest.jsonl` is the append-only invocation index
- identical bytes land in the same content object
- repeated fetches create additional timestamped evidence links

## Retrieval And Materialization

Provider-first usage without `work` is first-class:

```bash
gotta jira status
gotta jira search "retry budget"
gotta github search "service ownership"
gotta github https://github.com/org/repo/commits/HEAD
gotta confluence search "queue architecture"
gotta gdocs search "incident response"
gotta slack workspaces
gotta slack auth
gotta slack search "handoff failure"
gotta read https://github.com/org/repo/blob/main/README.md
```

Those commands bind or create a session automatically and materialize durable
evidence under that session.

`gotta read` is the canonical retrieval entrypoint. It supports:

- local files and directories
- bounded local rereads through `--head`, `--tail`, and `--section`
- provider URLs routed to the correct plugin
- canonical provider locators emitted by session surfaces
- stored content rereads by artifact or digest

## Session Synthesis Surfaces

`gotta session` is how the raw evidence web becomes navigable:

- `session manifest`
- `session timeline`
- `session graph`
- `session leads`
- `session analyze`

Examples:

```bash
gotta session show
gotta session manifest --plugin jira
gotta session timeline --limit 20
gotta session graph --output mermaid
gotta session leads artifact:ticket.md@0123deadbeef
gotta session analyze
```

These surfaces intentionally compress the evidence web:

- `manifest` summarizes what has been materialized
- `timeline` reconstructs chronology
- `graph` renders lineage and continuity
- `leads` extracts explicit next reads from existing artifacts
- `analyze` rebuilds cached session synthesis outputs from durable state

Before the first strong anchor, empty `manifest`, `graph`, `leads`, and
`analyze` output is normal.

## Session Coordination

Scaffold the active session in place, then rewrite the operator-owned charter
surfaces explicitly:

```bash
gotta session init
gotta want --stdin <<'EOF'
Queue retry investigation.
EOF
gotta goal --stdin <<'EOF'
Build the execution charter for the current session from live context.
EOF
```

That scaffolds the canonical session files plus readable projections:

```text
$WS/
  WANT.md
  TODO.md
  LOGS.md
  GOAL.md
  OOPS.md
  peers/
  bin/
```

Inside the session surface:

- `WANT.md` and `GOAL.md` are intentional rewrites
- `state/todo.jsonl` is canonical and projects into `TODO.md`
- `state/logs.jsonl` is canonical and projects into `LOGS.md`
- `state/oops.jsonl` is canonical and projects into `OOPS.md`
- peer lifecycle state, peer notes state, and the session evidence web carry shared coordination

This split is deliberate:

- narrative framing stays editable
- operational truth stays append-only and durable
- readable projections refresh from canonical state

Examples:

```bash
gotta session init
gotta want --stdin <<'EOF'
Queue retry investigation.
EOF
gotta goal --stdin <<'EOF'
Investigate retry handling from the first strong source anchor and keep the
charter current as evidence lands.
EOF
gotta todo append <<'EOF'
Inspect duplicate materializations in `gotta session analyze`.
EOF
gotta logs append <<'EOF'
Captured the first GitHub, Jira, and Confluence anchor set.
EOF
gotta oops append <<'EOF'
Search results were followable, but one direct fetch still had a continuity gap.
EOF
```

## Flat Linked Peer Sessions

Peers are linked sessions, not hierarchical sub-objects.

- `peers/<actor>/` is a symlink to that linked peer session root
- `state/peers/<actor>/` is a symlink to that peer session's `state/`
- peer-local planning and lifecycle state stay peer-local
- shared coordination surfaces stay shared by explicit links

Examples:

```bash
gotta peer with Claude
gotta want --session peers/claude --stdin <<'EOF'
Trace retry ownership from the first strong source anchor.
EOF
gotta goal --session peers/claude --stdin <<'EOF'
Materialize the peer-local evidence contract before launch.
EOF
gotta peer launch Claude
gotta notes show claude
gotta todo extend --session peers/claude <<'EOF'
- Compare retry behavior across the earliest design docs.
EOF
```

Important invariants:

- `gotta peer with ...` configures linked peer sessions but does not launch
  them
- peer-local `WANT.md` and `GOAL.md` are seeded placeholders that must be
  rewritten before launch with `gotta want --session peers/<peer> ...` and
  `gotta goal --session peers/<peer> ...`
- peer-local `TODO.md` is seeded automatically and can be extended
- peer notes are supervisory visibility, not a separate truth store
- peer evidence often lands in the shared manifest, timeline, and graph before
  notes fully catch up

## `oops` As Canonical Alignment

`gotta oops` is a first-class operator surface, not an afterthought.

Use it to record:

- broken or misleading contracts
- native surfaces that should have been followable but were not
- provider coverage limits that materially shaped the investigation
- workflow friction that forced unnatural detours

The point is not merely bug tracking. The point is preserving operator-visible
misalignment in canonical shared state so the tool can be refined from observed
breakage rather than taste or memory.

## Plugin Architecture

`gotta` uses two entry-point groups:

- `gotta.plugins` for top-level plugins
- `gotta.ask` for optional ask-family extensions

Core is a PEP 420 namespace package. Plugins can live in separate distributions
and still contribute modules under the shared `gotta` namespace.

Core currently ships these top-level plugins:

- `ask`
- `confluence`
- `gdocs`
- `gdrive`
- `github`
- `gsheets`
- `jira`
- `logs`
- `notes`
- `oops`
- `peer`
- `read`
- `session`
- `slack`
- `todo`
- `want`
- `goal`

`read` is the URL-shaped dispatcher. It routes recognized targets through
installed provider plugins by asking those plugins whether they own the target.

## Ask Extensions

`gotta ask` is a generic host for separately installed ask-family extensions.
Core does not ship any built-in ask surfaces.

An ask extension registers the `gotta.ask` entry-point group and then becomes
available as:

```bash
gotta ask <surface> ...
```

Use `gotta ask --help-all` to inspect installed ask surfaces recursively.

## Contributing And Release Discipline

The project is maintained with a few deliberate rules:

- remove residue instead of layering over it
- prefer tool-observable truth to conversational assumption
- keep friction canonical in `oops`
- treat complexity as measurable pressure, not just aesthetic discomfort

If you contribute:

- run the full quality gate before proposing changes
- keep examples and fixtures generic and public
- preserve the native evidence-first workflow
- prefer behavior-level cleanup over compatibility ballast

See [CONTRIBUTING.md](CONTRIBUTING.md)
for the repository baseline.

## Release

Build and validate artifacts with `uv`:

```bash
uv build --python 3.10 --clear
uvx twine check dist/*
uv publish --dry-run
uv publish
```

`uv publish` needs PyPI credentials in the environment. The simplest local path
is `UV_PUBLISH_TOKEN`.
