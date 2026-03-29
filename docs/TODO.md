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
- [x] Plugin-shaped registry alias layer deleted.
  - Removed `PluginSpec`, `get_plugin()`, `available_plugins()`,
    `discovered_plugins()`, `iter_plugins()`, and the parallel plugin-named
    dispatch aliases from the live core.
  - Retargeted the live registry consumers to `SurfaceSpec`,
    `SurfaceBinding`, `get_surface()`, `available_surfaces()`,
    `load_surface_runner()`, and `run_surface()`.
  - Retargeted the federated surfaces and stored-display path that still
    depended on the old names:
    - `src/gotta/resolve/name.py`
    - `src/gotta/resolve/canon.py`
    - `src/gotta/resolve/intent.py`
    - `src/gotta/plugins/read.py`
    - `src/gotta/plugins/search.py`
    - `src/gotta/plugins/ask.py`
    - `src/gotta/stored.py`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted a still-load-bearing false center.
  More registry synonym cleanup of the same species would not; the next work
  that pays rent is pushing derived views toward ledger/state contracts and
  typing the exported payload families that still move as anonymous records.
- [x] Logical ledger record tranche landed.
  - `src/gotta/content/model.py` now centers:
    - `ArtifactMetadata`
    - `StoredArtifactLayout`
    - `ArtifactRecord`
    - `AliasRecord`
    - `MaterializationEvent`
    - `ManifestEntry`
  - `Materialization` and `ContentSnapshot` now center logical records and
    layout directly.
  - The filesystem storage implementation now produces logical ledger records
    instead of exporting raw path bundles as the semantic center.
  - First downstream consumers now read `artifact`, `layout`, `aliases`, and
    `events` directly:
    - `src/gotta/resolve/locate.py`
    - `src/gotta/lead/resolve.py`
    - `src/gotta/lead/cache.py`
    - `src/gotta/lead/edge.py`
    - `src/gotta/plugins/session/core.py`
    - `src/gotta/plugins/session/timeline/stamp.py`
    - `src/gotta/plugins/session/timeline/source.py`
    - `src/gotta/plugins/session/analyze/lineage.py`
    - `src/gotta/plugins/session/graph/payload.py`
    - `src/gotta/plugins/session/lead/payload.py`
    - `src/gotta/plugins/session/scan/payload.py`
- [x] Diminishing-returns judgment for this cycle:
  more registry or package churn would not pay rent.
  The next work that pays rent is explicit storage contracts and continued
  demotion of filesystem-shaped truth, not more naming or file-motion passes.
- [x] Dead storage-record legacy surface deleted from the live core.
  - Removed path-shaped and metadata/alias convenience accessors from:
    - `MaterializationEvent`
    - `Materialization`
    - `ContentSnapshot`
    in `src/gotta/content/model.py`
  - Retargeted the affected tests to the logical record center:
    - `artifact`
    - `layout`
    - `alias`
    - `event`
    - `aliases`
  - The removed surface is no longer present in `src/` or the test surface.
- [x] Diminishing-returns judgment for this cycle:
  deleting the dead legacy layer paid rent.
  More cleanup of the same species would not; the next work that pays rent is
  explicit storage contracts and splitting logical ledger operations from the
  filesystem implementation.
- [x] Storage-contract split landed.
  - `src/gotta/content/store.py` is now the storage-contract layer:
    - `BlobStore`
    - `LedgerStore`
    - `StateStore`
    - `IndexStore`
  - `src/gotta/content/filesystem.py` is now the filesystem implementation:
    - `FileSystemBlobStore`
    - `FileSystemLedgerStore`
  - Concrete call sites now import the filesystem implementation explicitly
    instead of treating `content/store.py` as both contract and implementation.
  - The touched derived views now consume logical records through the concrete
    filesystem ledger:
    - `src/gotta/dispatch/materialize.py`
    - `src/gotta/resolve/locate.py`
    - `src/gotta/plugins/session/timeline/source.py`
    - `src/gotta/plugins/session/scan/payload.py`
    - `src/gotta/plugins/session/graph/payload.py`
    - `src/gotta/plugins/session/analyze/lineage.py`
    - `src/gotta/plugins/session/lead/payload.py`
- [x] Diminishing-returns judgment for this cycle:
  this still paid rent because it made the storage boundary real in code.
  More cleanup of this exact species would not; the next work that pays rent is
  typing the artifact-pipeline records above storage and driving derived views
  toward ledger/state contracts instead of concrete filesystem ownership.
- [x] Artifact-pipeline tranche landed.
  - `src/gotta/capture.py` now centers:
    - `Capture`
    - `ArtifactKind`
  - `src/gotta/projection.py` now centers:
    - `Projection`
    - `projection_bytes(...)`
    - `projection_for_capture(...)`
  - Provider project hooks in the touched seam now return `Projection` instead
    of raw bytes.
  - The touched providers now consume canonical capture fields:
    - `preferred_name`
    - `content_type`
    - `metadata`
    - `view_data`
  - The dispatch/materialization seam now treats `artifact_kind` as
    `ArtifactKind | None` instead of carrying the dead empty-string sentinel.
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it made the capture/project seam real in code and
  deleted dead sentinels instead of preserving them.
  More cleanup of this exact species would not; the next work that pays rent is
  pushing derived views toward ledger/state contracts and then typing the
  exported payload families that still move as anonymous records.

## Current Tranche

- [x] Freeze the registry vocabulary in executable code.
- [x] Push that vocabulary through top-level dispatch, help, route discovery,
  and ask-family binding dispatch.
- [x] Split the logical ledger from filesystem-shaped storage truth at the
  record layer.
- [x] Delete the dead storage-record legacy layer once `src/` no longer depends
  on it.
- [x] Introduce explicit storage contracts and separate the contract layer from
  the concrete filesystem implementation.
- [x] Type the artifact-pipeline records above storage.
- [ ] Next tranche: keep pushing derived views toward ledger/state contracts
  instead of concrete filesystem ownership, then type the exported payload
  families that still move as anonymous records.

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
- [x] Stop centering exported ledger truth on raw filesystem-shaped records in:
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

- [x] Introduce first-class storage contracts:
  - `LedgerStore`
  - `BlobStore`
  - `StateStore`
  - `IndexStore`
- [ ] Type the ledger records first:
  - [x] `ArtifactRecord`
  - [x] `ArtifactMetadata`
  - [x] `AliasRecord`
  - [x] `MaterializationEvent`
  - [x] `ManifestEntry`
  - `SessionBindingRecord`
  - `ActorStateRecord`
  - `StateChannelRecord`
- [ ] Type the artifact-pipeline records that sit above storage:
  - [x] `Capture`
  - [x] `Projection`
  - [x] `ArtifactKind`
- [ ] Demote filesystem-shaped exported truth in `src/gotta/content/model.py`.
  Stop treating fields like:
  - `canonical_path`
  - `data_path`
  - `names_dir`
  - `logs_dir`
  - `content_dir`
  as the essence of stored artifacts.
- [x] Split `src/gotta/content/store.py` into storage contracts versus concrete
  filesystem implementation.
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

- [x] Implement `SurfaceBinding` and command-path declaration in the registry.
- [x] Delete the plugin-shaped alias layer from the registry and dispatch
  surface.
- [x] Introduce the first real ledger record types.
- [x] Start extracting logical ledger operations from `content/store.py`.
- [x] Replace filesystem-shaped exported records in `content/model.py`.
- [x] Introduce `LedgerStore`, `BlobStore`, `StateStore`, and `IndexStore`.
- [x] Split `content/store.py` into storage contracts and concrete filesystem
  implementation.
- [x] Remove remaining internal dependence on dead legacy properties from the
  test surface, then delete those properties.
- [x] Type the artifact-pipeline records above storage:
  - `Capture`
  - `Projection`
  - `ArtifactKind`
- [ ] Keep pushing derived views toward ledger/state contracts instead of
  concrete filesystem ownership.
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
