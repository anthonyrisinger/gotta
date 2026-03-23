# Gotta

`gotta` is a session-rooted CLI for evidence acquisition, operational memory,
durable continuity, and linked actor workflows.

The point is broader than retrieving one more document. `gotta` is designed to
keep actor work coherent when terminal history, model context windows, or human
working memory come under pressure. It pushes enough state, provenance, and
materialized evidence into durable external form. This lets an actor rehydrate
prior work reliably and continue from grounded context.

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
- `gotta oops` — I have to record this friction
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
- synthesis surfaces such as manifest, timeline, graph, leads, and analyze

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

`state/oops.jsonl` is the canonical friction log, projected into `OOPS.md`. It
captures operator-visible seams: misleading contracts, interrupted
continuations, coverage gaps, and workflow friction worth fixing.

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

The canonical top-level root is a shared session root under gotta's OS-native
data directory:

```text
<gotta data dir>/sessions/<session-id>/
  session.json
  content/
  actors/<actor-id>/
```

Examples:

- macOS: `~/Library/Application Support/gotta/sessions/<session-id>/`
- Linux: `~/.local/share/gotta/sessions/<session-id>/`

Each shared session owns:

- `session.json` for shared session membership and actor metadata
- `content/` for the shared evidence web and append-only manifest
- `actors/<actor-id>/` for actor-local writable surfaces and state

Each actor root then owns its local working surfaces:

```text
actors/<actor-id>/
  WANT.md
  GOAL.md
  TODO.md
  LOGS.md
  OOPS.md
  NOTES.md
  state/
  bin/
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

Provider-first usage without session scaffolding is a core workflow:

```bash
gotta jira status
gotta jira search "retry budget"
gotta github search "service ownership"
gotta github https://github.com/org/repo/commits/HEAD
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
```

Direct provider `status`, `search`, and `get` surfaces operate without session
scaffolding unless they intentionally materialize durable evidence.

`gotta read` is the canonical retrieval entrypoint. It supports:

- local files and directories
- bounded local rereads through `--head`, `--tail`, and `--section`
- bounded remote/provider reads whose limits only trim what is shown while the
  full canonical payload still lands underneath in the evidence web
- provider URLs routed to the correct plugin
- canonical provider locators emitted by session surfaces
- stored content rereads by artifact or digest

This surface is broader than search. It is the acquisition layer for the
session's evidence web. Slack threads, GitHub pages, Jira issues, Google docs,
Grafana dashboards, Confluence pages, and Granola notes all become reopenable
session artifacts rather than transient terminal output.

Granola extends that same model to personal notes and transcripts through the
local desktop session and Granola's APIs, so meeting notes and transcripts
participate in the same durable evidence graph as repository, ticket, and chat
artifacts.

Once that context is grounded, the same native surfaces support follow-on
action. The goal is not only to recover information, but also to preserve
enough working state that research, planning, and provider-native actions can
happen against the same session context.

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

These surfaces intentionally compress the evidence web without severing it:

- `manifest` summarizes what has been materialized
- `timeline` reconstructs chronology
- `graph` renders lineage and continuity
- `leads` extracts explicit next reads from existing artifacts
- `analyze` rebuilds cached session synthesis outputs from durable state

Empty `manifest`, `graph`, `leads`, and `analyze` output means the session has
not materialized enough evidence yet.

After compaction pressure, these are the surfaces that make rehydration cheap.
They preserve the shape of the work so the next pass starts from durable
structure rather than a thin summary.

## Session Coordination

Bind one shared session explicitly, then rewrite the operator-owned charter
surfaces inside the active actor root:

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
    TODO.md
    LOGS.md
    OOPS.md
    NOTES.md
    state/
    bin/
    content -> ../../content
```

Inside this topology:

- `WANT.md` and `GOAL.md` are actor-local intentional rewrites
- `state/todo.jsonl` is actor-local truth and projects into `TODO.md`
- `state/logs.jsonl` is actor-local truth and projects into `LOGS.md`
- `state/oops.jsonl` is actor-local truth and projects into `OOPS.md`
- `state/notes.jsonl` is actor-local truth and projects into `NOTES.md`
- `sessions/<session-id>/content/` is shared across all actors bound into that
  session

Read-only session-rooted surfaces such as `gotta oops`, `gotta logs`,
`gotta todo`, `gotta want`, `gotta goal`, `gotta actor status`, and
`gotta session show` require either an existing bound session or an explicit
`--session <session-id>`.

This split is deliberate:

- narrative framing stays editable
- operational truth stays append-only and durable
- readable projections refresh from canonical state

Examples:

```bash
gotta session init
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
  session-local actor root
- `gotta actor bind ...` binds sibling actor sessions inside that shared
  session
  but does not launch them
- actor-local `WANT.md` and `GOAL.md` are seeded placeholders that must be
  rewritten before launch with `gotta want --actor <actor> ...` and
  `gotta goal --actor <actor> ...`
- actor-local `TODO.md` is seeded automatically and can be extended
- actor notes are supervisory visibility, not a separate truth store
- actor evidence often lands in the shared manifest, timeline, and graph before
  notes fully catch up

The important property is continuity under delegation. Actors can branch, gather
evidence, and rejoin the shared working set without flattening everything into
a single chat transcript.

## `oops` As Canonical Alignment

`gotta oops` is a first-class operator surface.

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
  surfaces (`WANT.md`, `GOAL.md`), state logs (`TODO.md`, `LOGS.md`, `OOPS.md`,
  `NOTES.md`), and lifecycle. Actors can be human operators, AI agents, or
  automated workflows.

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
  `analyze` rebuilds cached session synthesis.

- **friction** — operator-visible misalignment captured in `oops`. Not bug
  tracking. Friction records seams: misleading contracts, continuity gaps,
  workflow detours. The canonical log is `state/oops.jsonl`; `OOPS.md` is the
  readable projection.

- **projection** — a readable Markdown file derived from canonical JSONL state.
  `TODO.md`, `LOGS.md`, `OOPS.md`, and `NOTES.md` are projections.
  Append-only JSONL is truth; Markdown is the human surface.

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
- `confluence`
- `gdocs`
- `gdrive`
- `grafana`
- `granola`
- `github`
- `gsheets`
- `jira`
- `logs`
- `notes`
- `oops`
- `actor`
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the repository baseline.

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
