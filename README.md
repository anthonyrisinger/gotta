# Gotta

`gotta` is a session-rooted CLI for remote discovery, evidence acquisition,
durable operator memory, and actor-coordinated workflows.

The point is broader than retrieving one more document. `gotta` is for terminal
work that would otherwise die in scrollback, browser tabs, or compacted model
context. It externalizes enough evidence, state, provenance, and follow-up
structure that a session can be reopened and continued from grounded context
instead of reconstructed from memory.

At a glance:

- `gotta search` routes plain-text remote discovery into provider-native search
  surfaces
- `gotta read` materializes local or remote evidence into a durable session
  graph
- `gotta session` turns that graph into manifest, timeline, graph, leads,
  analyze, and scan views
- `gotta notes`, `gotta logs`, `gotta oops`, and `gotta todo` are live CLI
  surfaces over canonical JSONL state
- `gotta actor` coordinates sibling actors inside one shared session without
  flattening everything into one transcript

The public surface stays small:

```bash
gotta <plugin> ...
```

`gotta ...` is the canonical operator path. It binds or discovers the right
session for the current context, hydrates the runtime environment, and
dispatches the requested plugin with that session active. You do not need to
`cd` into the session root first.

## Why `gotta`

The name is a modal verb of necessity. Every subcommand reads as a natural
English sentence expressing what needs to happen:

- `gotta read` — I have to read this
- `gotta oops` — I have to inspect or record this friction
- `gotta session analyze` — I have to analyze the session
- `gotta want` — I have to express what I want
- `gotta todo append` — I have to track this work

That composability is not accidental. Alternative names work for acquisition
commands but break down everywhere else. `glean read` makes sense; `glean oops`
does not. `trace jira search` reads well; `trace want` does not. Only a modal
verb of necessity composes across the entire surface: evidence acquisition,
friction capture, intent declaration, actor coordination, and session synthesis.

The semantics are also exact. The tool's thesis is that evidence must be
externalized, context must survive compression, and friction must be recorded.
"Gotta" is that necessity made literal.

## Continuity Over Context Windows

Actors work in waves. They pull a few strong anchors, expand into adjacent
evidence, synthesize the resulting web, and then eventually hit compression
pressure. Context windows compact. Terminal history scrolls away. Thin
summaries preserve headlines but lose the path that made those headlines
trustworthy.

`gotta` is shaped around that boundary.

It externalizes the parts of the working context that should survive:

- the session itself as a durable working root
- canonical task, log, and friction state
- materialized evidence with native reopen handles
- synthesis surfaces such as manifest, timeline, graph, leads, analyze, and
  scan

That is why `gotta` is session-rooted rather than request-rooted. A request is
ephemeral. A session can be reopened, inspected, extended, handed off, or
compacted and rehydrated without losing the shape of the work.

The choice of medium is equally deliberate: native CLI working surfaces are
one of the strongest places for serious actor work. They provide room to
inspect, correlate, transform, act on, and resume real evidence. `gotta`
leans into that by treating retrieval, memory, and action as part of the same
working surface.

## Why It Is Shaped This Way

`gotta` is deliberately native-first:

- prefer provider-native retrieval and mutation surfaces over ad hoc shell
  fallbacks
- keep durable truth in session state, not in terminal scrollback
- make every material read reopenable through native locators
- preserve enough provenance that compression leads to rehydration, not loss of
  working context

The normal working loop expands and compresses repeatedly:

1. retrieve one or two strong anchors through provider-native search or read
2. expand outward into adjacent evidence and materialize it into the session
3. compress that evidence web through `session manifest`, `timeline`, `graph`,
   `leads`, and `analyze`
4. record friction and continuity gaps in `oops`
5. refine the next retrieval wave from durable state instead of memory

`state/oops.jsonl` is the canonical friction log, and `gotta oops` is the live
readable surface. It captures operator-visible seams: misleading contracts,
interrupted continuations, coverage gaps, and workflow friction worth fixing.

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

`gotta` supports Python `>=3.10`.

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
uv run python -m vulture src tests --min-confidence 80
```

Structural pressure tools are part of the maintenance discipline:

```bash
uv run python -m radon cc src tests -s
uv run lizard src tests
uv build --python 3.10 --clear
uvx twine check dist/*
```

`pytest`, `ruff`, and `vulture` are the correctness and hygiene gate. `radon`
and `lizard` are not bug finders; they expose responsibility concentration and
complexity hotspots so cleanup removes residue instead of burying it.

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

[providers.grafana.env]
GOTTA_GRAFANA_BASE_URL = "https://grafana.example.com"
GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN = "glsa_your_service_account_token"
# Optional when the token must be pinned to one org explicitly.
GOTTA_GRAFANA_ORG_ID = "1"
```

Durable OAuth state lands under gotta's OS-native state directory:

- macOS: `~/Library/Application Support/gotta/auth/`
- Linux: `~/.local/state/gotta/auth/`
- Windows: `%LocalAppData%\\gotta\\auth\\`

## Canonical Session Model

`gotta` has two first-class session layouts:

- shared-topology sessions under gotta's OS-native data directory
- exact-root sessions scaffolded intentionally at one concrete path

Shared-topology sessions are the ambient default for stable interactive
fingerprints such as Codex threads and terminal sessions:

```text
<gotta data dir>/sessions/<session-id>/
  session.json
  content/
  actors/<actor-id>/
```

Examples:

- macOS: `~/Library/Application Support/gotta/sessions/<session-id>/`
- Linux: `~/.local/share/gotta/sessions/<session-id>/`

Each shared-topology session owns:

- `session.json` for shared session membership and actor metadata
- `content/` for the shared evidence web and append-only manifest
- `actors/<actor-id>/` for actor-local writable surfaces and state

Each actor root then owns its local working surfaces:

```text
actors/<actor-id>/
  WANT.md
  GOAL.md
  state/
  content -> ../../content
```

Top-level fingerprint bindings live separately:

```text
<gotta data dir>/bindings/<fingerprint> -> ../sessions/<session-id>/actors/<actor-id>
```

The default private session for an unbound fingerprint is:

```text
sessions/<fingerprint>/actors/<fingerprint>/
```

Exact-root sessions are the manual path when you intentionally want one
workspace-local root with session metadata, actor surfaces, and content
co-located:

```bash
gotta session init --session "$WS"
gotta session bind "$WS"
```

```text
<exact-root>/
  WANT.md
  GOAL.md
  state/
  content/
  actors/<actor-id>/
  session.json          # appears once sibling actors are bound
```

The topology choice is intentional:

- shared-topology sessions are the canonical ambient/default path
- exact-root sessions are the explicit "this directory is the session" path
- `gotta session bind` accepts either a shared session id, an exact session
  root, or an explicit `<session>/<actor>` reference

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

This is the continuity boundary in concrete form: the session keeps enough of
the working set externalized that later synthesis, follow-up research, or
downstream actions can be rebuilt from durable state instead of a lossy memory
of prior interactions.

## Retrieval And Materialization

Remote discovery and evidence acquisition are the canonical front door:

```bash
gotta jira status
gotta search jira:retry budget
gotta search github:service ownership --type code --repo org/repo
gotta github https://github.com/org/repo/commits/HEAD
gotta github https://github.com/org/repo/actions/runs/1234567890
gotta grafana status
gotta grafana datasources
gotta grafana search --type dash-db
gotta grafana search --type dash-db production-overview
gotta grafana search "production overview"
gotta grafana get demo-dashboard-uid
gotta grafana query --dashboard demo-dashboard-uid 'sum(app_up)'
gotta grafana query --dashboard 'https://grafana.example.com/d/demo-dashboard-uid/view?orgId=1&from=now-6h&to=now' 'sum(app_up)'
gotta grafana query --datasource prom-main 'rate(http_requests_total[5m])'
gotta confluence search "queue architecture"
gotta gdocs search "incident response"
gotta granola list --time-range last_30_days --limit 5
gotta granola transcript 11111111-1111-1111-1111-111111111111 --query latency
gotta granola search-transcript latency --time-range last_30_days
gotta slack workspaces
gotta slack auth
gotta slack search "handoff failure"
gotta read https://github.com/org/repo/blob/main/README.md
gotta read https://slack.com/docs/T12345678/D12345678
```

Stable interactive fingerprints like Codex threads and terminal sessions
automatically adopt their deterministic default session on the first
session-aware command that actually targets session state or acquires
remote/provider evidence. Provider `search`, provider `get`, remote/provider
`read`, and read-only session surfaces all auto-bootstrap there. Plain local
reads stay sessionless, and explicit `--actor` targeting still requires an
existing session because actor selection is session topology, not session
creation. Fallback synthetic fingerprints remain conservative and can still
use provider surfaces sessionlessly until a session is explicitly bound or
created.

`gotta read` is the canonical acquisition entrypoint. It supports:

- local files and directories
- bounded local rereads through `--head`, `--tail`, and `--section`
- bounded remote/provider reads whose limits only trim what is shown while the
  full canonical payload still lands underneath in the evidence web
- provider URLs routed to the correct plugin
- canonical provider locators emitted by session surfaces
- stored content rereads by artifact or digest

This surface is broader than search. It is the acquisition layer for the
session's evidence web. Slack threads and docs, GitHub pages and Actions runs,
Jira issues, Google Docs and Drive files, shared-drive documents, Grafana
dashboards, Confluence pages, and Granola notes all become reopenable session
artifacts rather than transient terminal output.

Granola extends that same model to personal notes and transcripts through the
local desktop session and Granola's APIs, so meeting notes and transcripts
participate in the same durable evidence graph as repository, ticket, and chat
artifacts.

Once that context is grounded, the same native surfaces support follow-on
action. The goal is not only to recover information, but also to preserve
enough working state that research, planning, and provider-native actions can
happen against the same session context.

## Remote Discovery

`gotta search` is the canonical plain-text remote discovery surface:

```bash
gotta search jira:Architecture
gotta search slack:ABC reboot --workspace demo --limit 10
gotta search github:SomeFunction --type code --repo acme/widgets
gotta search confluence:Architecture --title-only
```

The top-level verb already means search, so the routed target omits the
subcommand by default. Explicit routed aliases like `github:search foo` still
work, but the canonical form is `github:foo`.

## Session Synthesis Surfaces

`gotta session` is how the raw evidence web becomes navigable:

- `session manifest`
- `session timeline`
- `session graph`
- `session leads`
- `session analyze`
- `session scan`

Examples:

```bash
gotta session show
gotta session manifest --plugin jira
gotta session timeline --filter retry --limit 20
gotta session graph --filter jira --output mermaid
gotta session leads artifact:ticket.md@0123deadbeef
gotta session analyze --mode lineage --output mermaid
gotta session analyze --output markdown
gotta session scan "retry ownership"
```

These surfaces intentionally compress the evidence web without severing it:

- `manifest` summarizes what has been materialized
- `timeline` reconstructs chronology
- `graph` renders lineage and continuity
- `leads` extracts explicit next reads from existing artifacts
- `analyze` renders focused lineage and semantic synthesis directly from
  durable state
- `scan` searches projected artifact text across the materialized corpus

Where helpful, manifest, timeline, graph, and leads also surface top-N
hotspots in both text and JSON so the densest actors, providers, plugins, and
artifact kinds are visible without reading the entire payload.

`session analyze` always renders the requested `--output` format to stdout and
no longer writes graph bundles or summary artifacts as a side effect. Redirect
stdout when you want durable editor-visible output. Textual stdout is budgeted
by default; pass `--full-output` to disable terminal budgeting. Successful
operator surfaces emit a compact JSON receipt on stderr only when gotta has
non-obvious side effects to report, such as truncation or artifact locators;
pass `--quiet` to suppress informational stderr output. Raw Mermaid output
requires `--mode lineage` or `--mode semantic`; use `--output markdown` for
the combined two-graph bundle. Text and Markdown analysis outputs now start
with a compact anchor shortlist plus lineage and lead previews before the
deeper sections.

Empty `manifest`, `graph`, `leads`, and `analyze` output means the session has
not materialized enough evidence yet.

After compaction pressure, these are the surfaces that make rehydration cheap.
They preserve the shape of the work so the next pass starts from durable
structure rather than a thin summary.

## Session Coordination

When you want one explicit shared session, bind it and then rewrite the
operator-owned charter surfaces inside the active actor root:

```bash
gotta session bind retry-review
gotta actor bind Claude
gotta want --stdin <<'EOF'
Queue retry context review.
EOF
gotta goal --stdin <<'EOF'
Build the execution charter for the current session from live context.
EOF
```

That yields one shared session plus one actor-local working root:

```text
sessions/retry-review/
  session.json
  content/
  actors/<actor-id>/
    WANT.md
    GOAL.md
    state/
    content -> ../../content
```

Inside this topology:

- `WANT.md` and `GOAL.md` are actor-local intentional rewrites
- `state/todo.jsonl` is actor-local checklist truth and `gotta todo` is the
  live readable/mutable surface
- `state/logs.jsonl` is actor-local procedural/system trace and `gotta logs`
  is the live readable trace surface
- `state/oops.jsonl` is actor-local friction truth and `gotta oops` is the
  live readable friction surface
- `state/notes.jsonl` is actor-authored narration truth and `gotta notes` is
  the live readable narration surface
- `sessions/<session-id>/content/` is shared across all actors bound into that
  session

Read-only session-rooted surfaces such as `gotta oops`, `gotta logs`,
`gotta todo`, `gotta want`, `gotta goal`, `gotta actor status`, and
`gotta session show` auto-bootstrap the deterministic default session for
stable interactive fingerprints like Codex threads and terminal sessions.
Fallback synthetic fingerprints still require either an existing bound session
or an explicit `--session <session-id>`.

This split is deliberate:

- narrative framing stays editable
- operational truth stays append-only and durable
- the CLI renders readable output from canonical state on demand

In stable interactive contexts, the first session-aware `gotta` command
implicitly scaffolds and adopts the deterministic session for that context.
Future commands in that same context resolve there ambiently. `gotta session
init` remains the manual exact-root path when you want to scaffold one specific
root intentionally. For shared-topology sessions, other contexts should reuse
the shared session id with `--session <shared-session-id>` or `gotta session
bind <shared-session-id>`. Exact-root reuse with `--session <session-root>` or
`gotta session bind '<session-root>'` is the low-level path for non-shared
roots or intentionally reusing one concrete actor root.

Examples:

```bash
gotta want --stdin <<'EOF'
Queue retry context review.
EOF
gotta goal --stdin <<'EOF'
Trace retry handling from the first strong source anchor and keep the charter
current as evidence lands.
EOF
gotta todo append <<'EOF'
Inspect duplicate materializations in `gotta session analyze`.
EOF
gotta logs append <<'EOF'
Captured the first GitHub, Jira, and Confluence anchor set.
EOF
gotta oops append <<'EOF'
Direct fetch coverage should preserve the same continuity guarantees as search.
EOF
```

## Shared Sessions

Each shared session owns its evidence web directly and carries nested
actor-local session areas beneath it.

- shared session roots live at `sessions/<session-id>/`
- actor-local session areas live at `sessions/<session-id>/actors/<actor-id>/`
- the active fingerprint points at one actor-local root through
  `bindings/<fingerprint>`
- shared evidence lives under `sessions/<session-id>/content/`
- actor-local planning and lifecycle state stay local to each actor root
- actor targeting is explicit actor selection inside the current session, not
  path traversal under `actors/`

Examples:

```bash
gotta session bind retry-review
gotta actor bind Claude
ACTOR=<bound-actor-id>
gotta want --actor "$ACTOR" --stdin <<'EOF'
Trace retry ownership from the first strong source anchor.
EOF
gotta goal --actor "$ACTOR" --stdin <<'EOF'
Materialize the actor-local evidence contract before launch.
EOF
gotta actor launch "$ACTOR"
gotta notes show --actor "$ACTOR"
gotta todo extend --actor "$ACTOR" <<'EOF'
- Compare retry behavior across the relevant design docs.
EOF
```

Important invariants:

- `gotta session bind ...` switches the active fingerprint binding to one
  actor root, whether you name it by shared session id, exact session root, or
  explicit `<session>/<actor>` reference
- binding a shared session id joins that shared session through the caller's own
  actor root; binding an exact actor-root path reuses that exact actor root
- `gotta actor bind ...` binds sibling actor sessions inside that shared
  session
  but does not launch them
- actor-local `WANT.md` and `GOAL.md` are seeded placeholders that must be
  rewritten before launch with `gotta want --actor <actor> ...` and
  `gotta goal --actor <actor> ...`
- actor-local checklist state is seeded automatically and surfaced through
  `gotta todo`
- actor notes are the canonical actor-authored narration surface
- short one-line notes are valid; polished synthesis notes are optional
- actor evidence often lands in the shared manifest, timeline, and graph before
  notes fully catch up

The important property is continuity under delegation. Actors can branch, gather
evidence, and rejoin the shared working set without flattening everything into
a single chat transcript.

## `oops` As Canonical Alignment

`gotta oops` is a first-class operator surface.

Bare `gotta oops` shows the friction ledger. `gotta oops show` is the explicit
read form. `gotta oops append ...` and `gotta oops extend ...` write new
entries. Bare prose, real piped stdin, `--stdin`, and `--from-file` still imply
`append` when no read action is named.

Use it to record:

- incomplete or misleading contracts
- native surfaces that should have been followable but were not
- provider coverage limits that materially shaped the working path
- workflow friction that forced avoidable detours

The point is not merely bug tracking. The point is preserving operator-visible
misalignment in canonical shared state so the tool can be refined from observed
behavior rather than taste or memory.

## Key Concepts

These terms appear throughout `gotta` and its documentation:

- **session** — a durable working root that survives context loss. Sessions own
  a content store, actor roots, and synthesis surfaces. They can be reopened,
  extended, handed off, or compacted and rehydrated.

- **actor** — a named participant in a session. Each actor has its own charter
  files (`WANT.md`, `GOAL.md`), canonical state, and lifecycle. `gotta notes`
  is actor-authored narration, `gotta logs` is procedural/system trace,
  `gotta oops` is friction, and `gotta todo` is the checklist surface. Actors
  can be human operators, AI agents, or automated workflows.

- **fingerprint** — the context identity that binds the current terminal,
  thread, or environment to a session and actor root. Fingerprints are derived
  from terminal session IDs, Codex thread IDs, or explicit bindings.

- **materialization** — the act of capturing command output as a durable,
  content-addressed artifact in the session's content store. Materialized
  evidence has a SHA-256 digest and can be reopened by locator.

- **canonical locator** — a provider-normalized reference for a materialized
  artifact. Examples: `github:org/repo/blob/main/README.md`,
  `jira:PROJ-123`, `slack:C01234/p1234567890`. Locators are emitted when
  evidence lands and can be followed with `gotta read`.

- **artifact locator** — a session-relative reference to stored content.
  Format: `artifact:<preferred-name>@<digest12>`. Resolves through
  `gotta read` without requiring manifest spelunking.

- **content locator** — a digest-based reference to stored bytes. Format:
  `content:<sha256>`. Two identical fetches share the same content object.

- **evidence web** — the accumulated set of materialized artifacts, their
  metadata, and the relationships between them. The web grows through retrieval
  waves and can be navigated through synthesis surfaces.

- **synthesis surface** — a compressed, navigable view over the evidence web.
  `manifest` summarizes what has been materialized. `timeline` reconstructs
  chronology. `graph` renders lineage. `leads` extracts followable references.
  `analyze` renders focused synthesis directly from durable state. `scan`
  searches projected artifact text across the corpus.

- **friction** — operator-visible misalignment captured in `oops`. Not bug
  tracking. Friction records seams: misleading contracts, continuity gaps,
  workflow detours. The canonical log is `state/oops.jsonl`; `gotta oops` is
  the readable surface.

- **projection** — an on-demand terminal render from canonical state or a
  provider/content transform. Projections are no longer seeded as live root
  files; the CLI is the live readable surface.

- **rehydration** — recovering prior working state from durable session
  artifacts after context has been compressed. The synthesis surfaces make
  rehydration cheap: they preserve the shape of the work so the next pass
  starts from structure rather than a thin summary.

## Plugin Architecture

`gotta` uses two entry-point groups:

- `gotta.plugins` for top-level plugins
- `gotta.ask` for optional ask-family extensions

Core is a PEP 420 namespace package. Plugins can live in separate distributions
and still contribute modules under the shared `gotta` namespace.

Core ships these top-level plugins:

- `ask`
- `actor`
- `confluence`
- `gdocs`
- `gdrive`
- `goal`
- `grafana`
- `granola`
- `github`
- `gsheets`
- `jira`
- `logs`
- `notes`
- `oops`
- `read`
- `search`
- `session`
- `slack`
- `todo`
- `want`

`read` is the URL-shaped dispatcher. It routes recognized targets through
installed provider plugins by asking those plugins whether they own the target.

## Ask Extensions

`gotta ask` is a generic host for separately installed ask-family extensions.
Ask surfaces are provided by extensions that register the `gotta.ask`
entry-point group.

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
- preserve session continuity and reopenable evidence paths

If you contribute:

- run the full quality gate before proposing changes
- keep examples and fixtures generic and public
- preserve the native evidence-first workflow
- prefer behavior-level cleanup over compatibility ballast

See [CONTRIBUTING.md](https://github.com/anthonyrisinger/gotta/blob/main/CONTRIBUTING.md)
for the repository baseline.

## Release

Build and validate artifacts with `uv`:

```bash
./scripts/release patch
./scripts/release minor
```

The script is the canonical release path. It bumps the version with `uv`,
runs the release gate, builds the wheel and sdist, validates them with
`twine check`, smoke-installs the wheel on Python 3.10, commits the release
metadata, pushes `main`, publishes to PyPI, and waits for public propagation.

It reads the PyPI token from `~/.pypirc` under `[pypi].password`.
