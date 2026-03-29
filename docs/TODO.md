# TODO

This is the top-level execution queue for the run to `gotta` 1.0.

The canonical architecture document is:

- [ARCHITECTURE.md](https://github.com/anthonyrisinger/gotta/blob/main/docs/ARCHITECTURE.md)

This file is the execution queue.

## Reading Rule

- Use this file for sequencing and execution.
- Use `ARCHITECTURE.md` for rationale and canonical structure.
- If this file drifts from `ARCHITECTURE.md`, update one or both deliberately.
  Do not let them diverge silently.

## Running Record

- [x] Surface registry tranche landed.
  - `src/gotta/builtin.py` now centers:
    - `CommandPath`
    - `CapabilitySpec`
    - `ProviderBundle`
    - `PackageSpec`
    - `SurfaceSpec`
    - `SurfaceBinding`
  - Top-level discovery is now binding- and surface-shaped.
  - Legacy `PluginSpec` / `get_plugin()` / `available_plugins()` remain only as
    compatibility shims for deeper seams not yet collapsed.
- [x] Adjacent command seam retarget landed.
  - `src/gotta/dispatch/main.py`
  - `src/gotta/dispatch/runtime.py`
  - `src/gotta/cli/argv.py`
  - `src/gotta/resolve/route.py`
  - `src/gotta/resolve/read.py`
  - `src/gotta/resolve/search.py`
  - `src/gotta/plugins/ask.py`
  now consume the registry in surface/binding terms.
- [x] Diminishing-returns judgment for this cycle:
  additional topology churn is still past diminishing returns.
  The work that is paying rent now is contract-first collapse, not more file
  motion.

## Current Tranche

- [x] Freeze the registry vocabulary in executable code.
- [x] Push that vocabulary through top-level dispatch, help, route discovery,
  and ask-family binding dispatch.
- [ ] Next tranche: split the logical ledger from filesystem-shaped storage
  truth.

## Non-Negotiable Invariants

- [ ] Keep the logical artifact ledger authoritative.
- [ ] Keep capture prior to projection.
- [ ] Keep `Capture`, `Projection`, and `ArtifactKind` as first-class contracts.
- [ ] Eradicate semantically meaningful anonymous payloads.
- [ ] Name every persisted, exported, cross-package, and derived-view shape.
- [ ] Keep context at write time.
- [ ] Keep authored session state distinct from derived views.
- [ ] Keep external graph/query systems as derived indexes only.
- [ ] Keep `exec` explicit; no silent local-command fallback from provider work.
- [ ] Keep `read` and `search` as first-class federated intent surfaces.
- [ ] Keep CLI names modeled as installed `SurfaceBinding`s, not package names.

## Execution Rule

- [ ] Do not begin heavy provider-local or algorithmic surgery until:
  - registry vocabulary is explicit in code
  - ledger records exist above filesystem shape
  - derived payload families and shared semantic aggregates in the target area
    are named
  - semantically meaningful anonymous shapes are gone from the shared cores you
    plan to cut
- [ ] Treat current architecture drift as a prerequisite queue, not background
  cleanup.

## Current Live Drifts To Eliminate First

- [x] Replace the flat `PluginSpec` registry center with explicit code-level
  registry and binding kinds.
- [ ] Stop exporting filesystem-shaped ledger truth from:
  - `src/gotta/content/model.py`
  - `src/gotta/content/store.py`
- [ ] Remove runner/stdout fallback capture from federated `search` in
  `src/gotta/plugins/search.py`.
- [ ] Eliminate anonymous `dict`-shaped exported payloads in manifest, lead,
  timeline, graph, analyze, and session-status surfaces.

## Phase 0: Freeze The Vocabulary In Code

- [x] Make `SurfaceBinding` real in code.
  Define install-time bindings that declare:
  - binding name
  - command path
  - implementation surface
  - provider bundle
  - auth profile
  - defaults
- [x] Split the flat registry contract into explicit code-level kinds:
  - `PackageSpec`
  - `SurfaceSpec`
  - `SurfaceBinding`
  - `ProviderBundle`
  - `CapabilitySpec`
- [x] Make provider capability families explicit instead of implicit command
  grammar:
  - route
  - read
  - search
  - capture
  - project
  - mutate
  - auth
  - status
- [x] Make `ask` binding-driven so one implementation can back:
  - `gotta ask sre`
  - `gotta ask it`
  - `gotta ask product`

## Phase 1: Make The Ledger Logical Instead Of Filesystem-Shaped

- [ ] Introduce first-class storage contracts:
  - `LedgerStore`
  - `BlobStore`
  - `StateStore`
  - `IndexStore`
- [ ] Type the ledger records first:
  - `ArtifactRecord`
  - `ArtifactMetadata`
  - `AliasRecord`
  - `MaterializationEvent`
  - `ManifestEntry`
  - `SessionBindingRecord`
  - `ActorStateRecord`
  - `StateChannelRecord`
- [ ] Type the artifact-pipeline records that sit above storage:
  - `Capture`
  - `Projection`
  - `ArtifactKind`
- [ ] Demote filesystem-shaped exported truth in `src/gotta/content/model.py`.
  Stop treating fields like:
  - `canonical_path`
  - `data_path`
  - `names_dir`
  - `logs_dir`
  - `content_dir`
  as the essence of stored artifacts.
- [ ] Split `src/gotta/content/store.py` into logical ledger operations versus
  concrete filesystem implementation.
- [ ] Make the derived views consume logical ledger/state contracts rather than
  raw filesystem-shaped records.

## Phase 2: Type The Exported Payload Families

- [ ] Type the lead kernel:
  - `LeadMention`
  - `LeadEdge`
  - `LeadSourceSummary`
  - `LeadResolution`
- [ ] Type the derived view payload families:
  - `GraphPayload`
  - `TimelineEvent`
  - `AnalyzeOverview`
  - `LineagePayload`
  - `SemanticPayload`
- [ ] Replace anonymous exported manifest and session payload records with named
  types.
- [ ] Type the extension-boundary protocols:
  - `RouteClaimant`
  - `ArtifactProvider`
  - `SearchProvider`
  - `Projector`
  - `Mutator`
  - `LeadExtractor`
  - `LeadRanker`
- [ ] Type registry/dispatch glue only after the contracts it points to are
  typed.

## Phase 3: Finish The Surface / Package Model

- [ ] Introduce vendor-family packaging as an explicit concept.
  Distinguish:
  - distribution package
  - vendor family
  - provider bundle
  - surface binding
- [ ] Model Google as one vendor family with multiple operator surfaces:
  - `gdrive`
  - `gdocs`
  - `gsheets`
- [ ] Split Slack into cleaner species:
  - live provider capabilities
  - local archive/index behavior
  - workspace/admin/control surfaces
- [ ] Promote `actor` to an explicit workflow-surface status instead of treating
  it like a plain authored state channel.
- [ ] Keep session-derived views honest as views:
  - `manifest`
  - `timeline`
  - `graph`
  - `leads`
  - `analyze`
  - `scan`

## Phase 4: Fix The Known Contract Violations

- [ ] Fix `gsheets` canonical capture so display-shaping flags affect projection
  only, not stored canonical bytes.
- [ ] Remove runner/stdout fallback capture from federated `search`.
  `search` should resolve into explicit provider search/capture/project hooks.
- [ ] Externalize GitHub capability contracts further and treat GitHub as the
  exemplar bundle for:
  - route
  - parse
  - capture
  - project
  - read
  - search
  - render
- [ ] Tighten Atlassian internals without changing its taxonomy:
  - keep shared provider substrate
  - reduce monolithic internal surfaces
  - preserve bundle-level semantics

## Phase 5: Derived Backend Contracts

- [ ] Introduce explicit backend interfaces for optional derived indexes:
  - `GraphIndexBackend`
  - `LeadIndexBackend`
  - `SemanticIndexBackend`
- [ ] Make backend contracts typed around:
  - ingest
  - rebuild
  - query
  - health
  - staleness
- [ ] Make derived views backend-aware without surrendering ledger truth.
- [ ] Keep Memgraph, Kuzu, DuckDB, and similar systems strictly optional,
  rebuildable accelerators.

## Phase 6: Land The Explicit `exec` Surface

- [ ] Land `gotta exec -- <command> [args...]`.
- [ ] Capture canonical execution evidence including:
  - argv
  - cwd
  - exit status
  - started_at
  - finished_at
  - duration
  - stdout
  - stderr
  - stdin provenance
  - environment policy
- [ ] Materialize `exec` results as canonical `evidence`.
- [ ] Route `exec` artifacts into:
  - manifest
  - timeline
  - graph
  - leads
  - analyze

## Phase 7: Algorithmic Hardening Inside Settled, Typed Boundaries

- [ ] Reduce algorithmic density in the `session analyze` kernels:
  - `src/gotta/plugins/session/analyze/lineage.py`
  - `src/gotta/plugins/session/analyze/semantic.py`
  - `src/gotta/plugins/session/analyze/overview.py`
- [ ] Reduce algorithmic density in the GitHub surface internals:
  - `src/gotta/plugins/github/search.py`
  - `src/gotta/plugins/github/parse.py`
  - `src/gotta/plugins/github/render.py`
- [ ] Reduce density and typing pressure in the lead kernel:
  - `src/gotta/plugins/session/lead/payload.py`
  - `src/gotta/lead/aggregate.py`
  - `src/gotta/lead/rank.py`
  - `src/gotta/lead/edge.py`
- [ ] Reduce provider-local contract density in:
  - `src/gotta/plugins/jira.py`
  - `src/gotta/plugins/oops.py`
  - `src/gotta/plugins/slack.py`
  - `src/gotta/plugins/gdrive.py`
- [ ] Reduce remaining type pressure in the shared cores:
  - `src/gotta/plugins/session/core.py`
  - `src/gotta/plugins/actor.py`
  - `src/gotta/plugins/grafana.py`
  - `src/gotta/builtin.py`
  - `src/gotta/dispatch/main.py`

## Concrete Near-Term Queue

- [ ] Implement `SurfaceBinding` and command-path declaration in the registry.
- [ ] Introduce the first real ledger record types.
- [ ] Start extracting logical ledger operations from `content/store.py`.
- [ ] Replace filesystem-shaped exported records in `content/model.py`.
- [ ] Type the lead kernel.
- [ ] Type the first analyze/graph/timeline/session payload families.
- [ ] Replace anonymous manifest and lead aggregate records with named types.
- [ ] Fix `gsheets` capture semantics.
- [ ] Remove federated `search` runner-capture fallback.
- [ ] Add explicit backend interfaces before any Memgraph/Kuzu integration.

## What Not To Do

- [ ] Do not begin provider-local or algorithm-local surgery while shared
  semantic record work is still pending.
- [ ] Do not resume topology churn inside `session/analyze`.
- [ ] Do not split files just because they are hot.
- [ ] Do not externalize provider packages before the binding and ledger
  contracts exist.
- [ ] Do not treat filesystem layout as architecture.
- [ ] Do not flatten `read`, `search`, or `ask` into provider-native commands.

## Done Means

- [ ] The registry knows about package, surface, binding, provider, and
  capability separately.
- [ ] The ledger contracts exist independently of the filesystem layout.
- [ ] Derived views read logical records, not path-shaped internals.
- [ ] Anonymous semantically meaningful payloads are gone from exported records,
  cross-package traffic, persisted records, major derived views, and in-package
  semantic aggregates.
- [ ] Provider packaging can be externalized cleanly.
- [ ] Multi-binding installs like `gotta ask sre` are first-class.
- [ ] Derived backend interfaces exist without becoming storage truth.
- [ ] `gotta exec` exists as explicit evidence rather than implicit fallback.
- [ ] The lead kernel and major derived payloads are typed.
- [ ] Remaining work is primarily provider-local or algorithm-local, not
  architectural blur.
