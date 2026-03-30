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
- [x] Default backend selection landed.
  - `src/gotta/content/backend.py` now owns the default backend path for:
    - `default_blob_store(...)`
    - `default_ledger_store(...)`
    - `default_ledger_store_for_dirs(...)`
    - `scan_content_snapshots(...)`
    - `materialize_artifact_bytes(...)`
  - The derived views and resolution/materialization seam no longer import
    `FileSystemLedgerStore` directly:
    - `src/gotta/dispatch/materialize.py`
    - `src/gotta/resolve/locate.py`
    - `src/gotta/plugins/session/timeline/source.py`
    - `src/gotta/plugins/session/lead/payload.py`
    - `src/gotta/plugins/session/scan/payload.py`
    - `src/gotta/plugins/session/graph/payload.py`
    - `src/gotta/plugins/session/analyze/lineage.py`
  - Concrete filesystem ownership is now isolated to:
    - `src/gotta/content/filesystem.py`
    - `src/gotta/content/backend.py`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it removed direct concrete-backend ownership from the
  shared view seam.
  More retargeting of this exact species would not; the next work that pays
  rent is typing the exported payload families and continuing to erase
  filesystem-shaped assumptions inside those views.
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
  - The temporary generic `IndexStore` placeholder from this phase was later
    deleted in favor of explicit:
    - `GraphIndexBackend`
    - `LeadIndexBackend`
    - `SemanticIndexBackend`
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
- [x] Typed `session analyze` payload center landed.
  - `src/gotta/plugins/session/analyze/model.py` now owns the exported payload
    families for the analyze seam:
    - `LineagePayload`
    - `LineageFocusPayload`
    - `SemanticPayload`
    - `SemanticFocusPayload`
    - `AnalysisOverviewPayload`
    - `CombinedAnalysisPayload`
  - The analyze builders, renderers, and entrypoint now consume the named
    payload contracts instead of anonymous `dict[str, Any]` records:
    - `src/gotta/plugins/session/analyze/focus.py`
    - `src/gotta/plugins/session/analyze/lineage.py`
    - `src/gotta/plugins/session/analyze/semantic.py`
    - `src/gotta/plugins/session/analyze/overview.py`
    - `src/gotta/plugins/session/analyze/render.py`
    - `src/gotta/plugins/session/analyze/main.py`
  - `src/gotta/plugins/session/analyze` no longer exports `Any` or
    `dict[str, Any]` payload shapes.
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted a still-live anonymous payload seam in one
  of the heaviest derived views.
  More cleanup of this exact species inside `session analyze` would not; the
  next work that pays rent is pushing the same named-payload discipline into the
  remaining derived views and continuing to erase filesystem-shaped assumptions
  there.
- [x] Typed `session lead` payload center landed.
  - Shared cross-package lead records now live in:
    - `src/gotta/lead/model.py`
      - `LeadEdgeRecord`
      - `LeadSourceSummary`
      - `LeadSearchOrigin`
  - The `session leads` surface now has a truthful payload owner:
    - `src/gotta/plugins/session/lead/model.py`
      - `LeadArtifact`
      - `LeadsPayload`
      - `ProviderCountRecord`
      - `RelationCountRecord`
  - The lead builders, ranking helpers, and session renderer now consume the
    named payload contracts instead of anonymous `dict[str, object]` exports:
    - `src/gotta/lead/edge.py`
    - `src/gotta/lead/aggregate.py`
    - `src/gotta/lead/rank.py`
    - `src/gotta/plugins/session/core.py`
    - `src/gotta/plugins/session/lead/payload.py`
    - `src/gotta/plugins/session/lead/render.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/lead src/gotta/plugins/session/lead src/gotta/plugins/session/core.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted another still-live anonymous derived-view
  seam and forced the shared lead records to become first-class contracts.
  More cleanup of this exact species inside `session lead` would not; the next
  work that pays rent is the remaining derived-view payload families outside
  `analyze` and `lead`, especially `session scan` and `session timeline`.
- [x] Typed `session scan` payload center landed.
  - The `session scan` surface now has a truthful payload owner:
    - `src/gotta/plugins/session/scan/model.py`
      - `ScanVisibility`
      - `ScanEntry`
      - `ScanPayload`
  - The scan builder and renderer now consume named payload contracts instead
    of anonymous `dict[str, object]` exports:
    - `src/gotta/plugins/session/scan/payload.py`
    - `src/gotta/plugins/session/scan/render.py`
  - The existing typed snippet window remains the leaf owner:
    - `src/gotta/plugins/session/scan/snippet.py`
      - `SnippetLine`
      - `Snippet`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/plugins/session/scan`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted another live anonymous derived-view seam
  without inventing new topology or preserving old dict-shaped exports.
  More cleanup of this exact species inside `session scan` would not; the next
  work that pays rent is the remaining derived-view payload families outside
  `analyze`, `lead`, and `scan`, starting with `session timeline`.
- [x] Typed `session timeline` payload center landed.
  - The `session timeline` surface now has a truthful payload owner:
    - `src/gotta/plugins/session/timeline/model.py`
      - `TimelineVisibility`
      - `TimelinePluginCountRecord`
      - `TimelineActorCountRecord`
      - `TimelineEvent`
      - `TimelinePayload`
  - The timeline producers, payload builder, and renderer now consume the named
    event and payload contracts instead of anonymous `dict[str, object]`
    records:
    - `src/gotta/plugins/session/timeline/local.py`
    - `src/gotta/plugins/session/timeline/acquired.py`
    - `src/gotta/plugins/session/timeline/source.py`
    - `src/gotta/plugins/session/timeline/payload.py`
    - `src/gotta/plugins/session/timeline/render.py`
  - Dead timeline-only export detritus that no longer paid rent was deleted:
    - the acquired timeline surface no longer exports the unused `fetch_link`
      field
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/plugins/session/timeline src/gotta/plugins/session/core.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted another live anonymous derived-view seam
  and replaced it with one shared event family across every timeline producer.
  More cleanup of this exact species inside `session timeline` would not; the
  next work that pays rent is the remaining exported session payload families,
  starting with `session manifest`, then the smaller typed-count cleanup still
  left in `session graph`.
- [x] Typed `session manifest` payload center landed.
  - The `session manifest` surface now has a truthful payload owner:
    - `src/gotta/plugins/session/manifest/model.py`
      - `ManifestVisibility`
      - `ManifestRecord`
      - `ManifestPayloadEntry`
      - `ManifestPluginCountRecord`
      - `ManifestActorCountRecord`
      - `ManifestPayload`
  - The manifest loader, aggregator, payload builder, and renderer now consume
    named record and payload contracts instead of anonymous `dict[str, object]`
    exports:
    - `src/gotta/plugins/session/manifest/record.py`
    - `src/gotta/plugins/session/manifest/payload.py`
    - `src/gotta/plugins/session/manifest/render.py`
  - Adjacent manifest consumers now depend on the typed manifest record center:
    - `src/gotta/plugins/session/graph/payload.py`
    - `src/gotta/plugins/session/scan/payload.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/plugins/session/manifest src/gotta/plugins/session/graph/payload.py src/gotta/plugins/session/scan/payload.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last fully anonymous exported session
  payload family and forced the shared manifest row shape into one named
  contract center. More cleanup of this exact species inside `session manifest`
  would not; the next work that pays rent is the smaller typed-count and
  payload cleanup still left in `session graph`.
- [x] Typed `session graph` count-record seam landed.
  - The `session graph` surface now names its exported count-record families:
    - `src/gotta/plugins/session/graph/model.py`
      - `GraphProviderCountRecord`
      - `GraphArtifactKindCountRecord`
  - The graph payload builder now emits typed count records and keeps the
    source-visibility cache on the typed graph visibility spine:
    - `src/gotta/plugins/session/graph/payload.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/plugins/session/graph`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it removed the last obvious anonymous exported
  count-record leak in the session-view cluster without reopening any settled
  boundaries. More cleanup of this exact species inside `session graph` would
  not; the next honest move is to reassess the remaining exported anonymous
  shapes outside the now-mostly-settled session-view cluster and then switch
  back to provider-local monolith and algorithmic pressure.
- [x] Removed runner/stdout fallback capture from federated `search`.
  - Top-level `gotta search` now routes materialization only through explicit
    provider search hooks instead of buffering provider stdout and pretending it
    is canonical capture:
    - `src/gotta/plugins/search.py`
  - The federated search surface now requires both halves of the provider
    contract:
    - explicit search capture hook
    - explicit search projection hook
  - Regression coverage now pins the dead fallback path and the explicit-hook
    requirement:
    - `tests/test_dispatch.py`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it removed a live contract violation from a federated
  core surface. More cleanup of this exact species inside `search` would not;
  the next honest move is to type the remaining actor/runtime status payload
  cluster and `session doctor`, then reassess whether core-collapse is actually
  done and provider-local pressure should take over.
- [x] Typed the actor/runtime status payload center and `session` status
  surfaces.
  - The actor/runtime status seam now has one truthful payload owner:
    - `src/gotta/session/status/payload/model.py`
      - `LifecycleEntry`
      - `EvidenceArtifact`
      - `EvidenceSummary`
      - `RecentActivityPayload`
      - `NoteSummary`
      - `NoteCheckSummary`
      - `ProgressSummary`
      - `RequestStatePayload`
      - `RuntimeStatePayload`
      - `RuntimeSignalPayload`
      - `ActorActivityPayload`
      - `ActorStatusPayload`
  - The top-level `gotta session` surfaces now have named payload owners:
    - `src/gotta/plugins/session/model.py`
      - `SessionEnvPayload`
      - `DoctorRuntimePayload`
      - `DoctorSessionPayload`
      - `DoctorCheck`
      - `DoctorChecks`
      - `DoctorPayload`
  - Durable binding rows now have a named topology contract instead of flowing
    as raw dicts:
    - `src/gotta/topology.py`
      - `BindingRecord`
  - The typed contract now runs through the touched seam:
    - `src/gotta/plugins/session/show.py`
    - `src/gotta/plugins/session/doctor.py`
    - `src/gotta/session/activity/summary.py`
    - `src/gotta/session/status/payload/activity.py`
    - `src/gotta/session/status/payload/request.py`
    - `src/gotta/session/status/payload/runtime.py`
    - `src/gotta/session/status/payload/main.py`
    - `src/gotta/session/status/todo.py`
    - `src/gotta/notes/render.py`
    - `src/gotta/plugins/actor.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/topology.py src/gotta/plugins/session/model.py src/gotta/plugins/session/show.py src/gotta/plugins/session/doctor.py src/gotta/session/status src/gotta/session/activity/summary.py src/gotta/notes/render.py src/gotta/plugins/actor.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last obvious anonymous status and
  doctor payload seam in the shared session core without reopening settled
  storage or registry boundaries. More cleanup of this exact species would not;
  the next honest move is to reassess the remaining shared-core anonymous
  exports and, unless one more real seam survives, switch from core collapse to
  provider-local and algorithmic pressure.
- [x] Typed the authored state-channel records and top-level `logs`, `notes`,
  and `oops` payload seams.
  - Canonical authored-state records now have truthful owners:
    - `src/gotta/logs.py`
      - `LogRecord`
      - `LogsPayload`
    - `src/gotta/notes/file.py`
      - `ActorNoteRecord`
    - `src/gotta/notes/model.py`
      - `ActorNotesPayload`
      - `SessionNoteRecord`
      - `SessionNotesPayload`
    - `src/gotta/friction.py`
      - `FrictionRecord`
      - `FrictionSummary`
  - Top-level surface payload owners now exist for the authored-state read
    surfaces:
    - `src/gotta/plugins/logs/model.py`
      - `SessionLogRecord`
      - `SessionLogsPayload`
      - `AggregateLogsPayload`
    - `src/gotta/plugins/notes/model.py`
      - session-wide note payload exports
    - `src/gotta/plugins/oops.py`
      - `OopsDisplayRecord`
      - `OopsPayload`
  - The canonical state owners and read surfaces now consume named records
    instead of anonymous `dict[str, object]` traffic:
    - `src/gotta/logs.py`
    - `src/gotta/notes/file.py`
    - `src/gotta/notes/render.py`
    - `src/gotta/notes/voice.py`
    - `src/gotta/friction.py`
    - `src/gotta/plugins/logs/render.py`
    - `src/gotta/plugins/logs/show.py`
    - `src/gotta/plugins/notes/render.py`
    - `src/gotta/plugins/notes/show.py`
    - `src/gotta/plugins/oops.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/logs.py src/gotta/notes src/gotta/friction.py src/gotta/plugins/logs src/gotta/plugins/notes src/gotta/plugins/oops.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it removed one more still-live anonymous core seam in
  canonical authored state, not because it shuffled topology. More cleanup of
  this exact species across `logs`, `notes`, and `oops` would not; the next
  honest move is the event-sourced `todo` surface, which is the remaining
  authored-state core seam that still exports anonymous records.
- [x] Typed the event-sourced `todo` core and payload seam.
  - `src/gotta/todo.py` now centers:
    - `TodoItem`
    - `TodoCreateEvent`
    - `TodoCheckEvent`
    - `TodoEvent`
    - `TodoPayload`
  - The `todo` core no longer exports anonymous `dict[str, object]` records
    for:
    - canonical event playback
    - resolved item state
    - top-level show payloads
  - `create_todo_item(...)` now returns the resolved `TodoItem` record rather
    than a raw event-shaped dict.
  - The top-level surface and actor-managed status seam now consume the named
    `todo` contracts instead of anonymous dict traffic:
    - `src/gotta/plugins/todo.py`
    - `src/gotta/session/status/todo.py`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/todo.py src/gotta/plugins/todo.py src/gotta/session/status/todo.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last authored-state core seam of this
  species instead of preserving it. More shared-core contract cleanup of this
  exact species would not; the next honest move is to treat this phase as
  settled and switch to hot-kernel reduction, starting with the session lead
  and analyze hotspots that are now the real pressure centers.
- [x] Lead-kernel hot-kernel tranche landed.
  - `src/gotta/lead/model.py` now centers:
    - `LeadMention`
    - `LeadEdge`
    - `LeadSourceSummary`
    - `LeadResolution`
  - Deleted the stale `LeadEdgeRecord` noun from the live lead core.
  - `src/gotta/lead/resolve.py` now owns the shared lead-kernel synthesis path:
    - `resolve_lead_resolution(...)`
  - `src/gotta/plugins/session/lead/payload.py` no longer synthesizes target
    selection, edge building, and source aggregation inline; it consumes the
    named lead-kernel resolution and only performs session-surface assembly.
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/lead src/gotta/plugins/session/lead`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it replaced one more overloaded hot kernel with a
  named executable center and deleted stale lead-kernel residue. More
  lead-kernel taxonomy cleanup of this exact species would not; the next honest
  move is the remaining provider-knowledge leak in the core, not another lead
  rename pass.
- [x] Surface-declared provider intent tranche landed.
  - `src/gotta/builtin.py` now treats artifact intent as an explicit surface
    contract:
    - `SurfaceArtifactIntent`
    - `ArtifactIntentHook`
    - `SurfaceSpec.artifact_intent`
    - `SurfaceBinding.artifact_intent`
  - `src/gotta/resolve/intent.py` no longer hard-codes provider names,
    subcommand families, or provider-specific discovery/evidence/control maps.
    Installed surfaces now teach the core their artifact-bearing semantics.
  - The built-in provider surfaces now declare their own artifact intent in:
    - `src/gotta/plugins/confluence.py`
    - `src/gotta/plugins/gdocs.py`
    - `src/gotta/plugins/gdrive.py`
    - `src/gotta/plugins/grafana.py`
    - `src/gotta/plugins/github/main.py`
    - `src/gotta/plugins/granola.py`
    - `src/gotta/plugins/gsheets.py`
    - `src/gotta/plugins/jira.py`
    - `src/gotta/plugins/slack.py`
  - The new contract is pinned directly in the test surface:
    - `tests/test_dispatch.py`
      - `test_artifact_intent_follows_surface_contract(...)`
  - Targeted contract-center pyright is clean:
    - `uvx pyright src/gotta/builtin.py src/gotta/resolve/intent.py src/gotta/plugins/github/main.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted a still-load-bearing provider-knowledge
  seam from the core instead of just rearranging hot files. More shared-core
  payload typing of the old species would be churn now; the next honest move is
  the remaining provider-knowledge leak in canonicalization and dispatch, then
  the provider-local and algorithmic phase.
- [x] Generic canonicalization tranche landed.
  - `src/gotta/resolve/canon.py` no longer hard-codes provider-specific
    canonicalizer helpers for:
    - GitHub
    - Slack
    - Confluence
    - Jira
    - Google Docs
    - Google Drive
    - Google Sheets
  - The core canonicalization center is now:
    - installed surface `canonical_locator(...)`, or
    - generic `plugin:invocation_locator(...)` fallback
  - The provider-specific canonical shapes are now fully owned by installed
    surface bindings rather than core fallback code.
  - The generic fallback is pinned directly in:
    - `tests/test_dispatch.py`
      - `test_canonical_locator_falls_back_to_generic_surface_free_shape(...)`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted another still-live provider-knowledge seam
  from the core. More core canonicalization cleanup of the same species would
  be churn now; the next honest move is the remaining provider-name branch in
  `src/gotta/dispatch/main.py`, then reassess whether the core-collapse phase is
  honestly over.
- [x] Shared actor-option dispatch tranche landed.
  - `src/gotta/builtin.py` now models whether a surface wants shared dispatch
    actor handling with:
    - `SurfaceSpec.shared_actor_option`
    - `SurfaceBinding.shared_actor_option`
  - `src/gotta/dispatch/main.py` no longer hard-codes provider names to decide
    whether `--actor` is stripped into shared runtime options before dispatch.
    Dispatch now asks the installed surface binding directly.
  - The built-in shared-runtime acquisition surfaces now declare the contract
    explicitly:
    - `read`
    - `search`
    - provider surfaces that materialize through the shared runtime path
  - The contract is pinned directly in:
    - `tests/test_dispatch.py`
      - `test_search_plugin_spec_exposes_unary_should_materialize_contract(...)`
      - `test_provider_binding_declares_shared_actor_option(...)`
  - Targeted pyright on the touched seam is now clean:
    - `uvx pyright src/gotta/builtin.py src/gotta/dispatch/main.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last obvious provider-name branch from
  shared dispatch and replaced it with an installable contract. More shared-core
  decoupling of this exact species would likely be churn now; the next honest
  move is to reassess whether any real provider-knowledge seam remains in the
  core at all before continuing the collapse.
- [x] Provider-owned top-level `search` routing tranche landed.
  - `src/gotta/builtin.py` now carries:
    - `SurfaceSpec.search_route`
    - `SurfaceBinding.search_route`
  - `src/gotta/resolve/search.py` no longer owns provider-specific command
    families or provider-specific read/specialized redirect logic. It now does
    only:
    - generic `<provider>:<tail>` parsing
    - installed-surface `search_route(...)` dispatch
    - generic top-level plain-text validation
  - Installed provider surfaces now own their top-level `search` semantics in:
    - `src/gotta/plugins/confluence.py`
    - `src/gotta/plugins/gdocs.py`
    - `src/gotta/plugins/gdrive.py`
    - `src/gotta/plugins/grafana.py`
    - `src/gotta/plugins/github/route.py`
    - `src/gotta/plugins/granola.py`
    - `src/gotta/plugins/gsheets.py`
    - `src/gotta/plugins/jira.py`
    - `src/gotta/plugins/slack.py`
  - The new contract is pinned directly in:
    - `tests/test_dispatch.py`
      - `test_search_resolve_route_redirects_github_get_targets_to_read(...)`
      - `test_search_resolve_route_redirects_slack_workspace_locators_to_read(...)`
  - Focused validation is clean:
    - `uv run pytest -q tests/test_dispatch.py tests/test_cli.py tests/test_read.py -k 'search or canonical_locator or session_access_mode or should_materialize'`
    - `36 passed`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last load-bearing provider-command
  branch from shared `search` routing instead of renaming one more center.
  More shared-core provider-routing cleanup of the same species would now be
  churn; the next honest move is to reassess whether any remaining provider
  knowledge in shared core is still architectural, or whether the phase has
  genuinely crossed into provider-local and algorithmic pressure.
- [x] Surface-owned default source metadata tranche landed.
  - `src/gotta/builtin.py` now carries:
    - `DefaultSourceMetadata`
    - `SurfaceSpec.default_source_metadata`
    - `SurfaceBinding.default_source_metadata`
  - `src/gotta/dispatch/metadata.py` no longer hard-codes Slack-specific
    source timestamp derivation from:
    - canonical thread locators
    - `firstTs`
    - `lastTs`
  - Installed Slack now owns that seam in:
    - `src/gotta/plugins/slack.py`
      - `default_source_metadata(...)`
  - The contract is pinned directly in:
    - `tests/test_dispatch.py`
      - `test_slack_binding_declares_default_source_metadata(...)`
  - Focused validation is clean:
    - `uv run pytest -q tests/test_dispatch.py -k 'slack_binding_declares_default_source_metadata or materialize_invocation_carries_slack_thread_source_timestamps or materialize_invocation_carries_slack_channel_source_window or materialize_invocation_extracts_slack_markdown_source_times or materialize_invocation_extracts_visibility_from_markdown or search_resolve_route_redirects_slack_workspace_locators_to_read'`
    - `6 passed`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the remaining provider-specific metadata
  branch from shared dispatch without inventing a larger framework. More
  dispatch-side cleanup of the same species would now be churn; the next honest
  shared-core seam is provider-specific visibility classification in
  `src/gotta/source/visibility.py`, not dispatch or routing.
- [x] Surface-owned visibility classification tranche landed.
  - `src/gotta/builtin.py` now carries:
    - `VisibilityClassifier`
    - `SurfaceSpec.classify_visibility`
    - `SurfaceBinding.classify_visibility`
  - `src/gotta/source/visibility.py` no longer hard-codes provider-specific
    visibility branches for:
    - Slack
    - GitHub
    - Jira
  - Installed surfaces now own those semantics in:
    - `src/gotta/plugins/slack.py`
      - `classify_visibility(...)`
    - `src/gotta/plugins/github/main.py`
      - `classify_visibility(...)`
    - `src/gotta/plugins/jira.py`
      - `classify_visibility(...)`
  - Focused validation is clean:
    - `uv run pytest -q tests/test_source.py tests/test_dispatch.py tests/test_slack.py tests/test_github.py tests/test_atlassian.py -k 'visibility or slack_binding_declares_default_source_metadata'`
    - `9 passed`
  - Shared-core residue sweep now shows no remaining provider-specific branches
    in `src/gotta/dispatch`, `src/gotta/resolve`, or `src/gotta/source`
    beyond provider names inside example error strings.
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it deleted the last real provider-aware shared-core
  classification center instead of just moving code around. More shared-core
  provider-decoupling of this exact species would now be churn. The shared-core
  collapse phase is effectively across the line; the next honest move is
  derived-backend contracts, `exec`, or provider-local / algorithmic pressure.
- [x] Explicit `exec` surface tranche landed.
  - `src/gotta/plugins/exec.py` now owns the explicit command-evidence surface:
    - `gotta exec -- <command> [args...]`
    - canonical execution capture
    - canonical execution projection
  - `src/gotta/builtin.py` now registers `exec` as a first-class surface with:
    - explicit capture/project hooks
    - explicit locator and naming hooks
    - explicit evidence intent
  - `src/gotta/dispatch/main.py` now returns materialized capture exit status
    instead of collapsing every captured execution to success.
  - `src/gotta/source/visibility.py` now classifies `exec` as local `gotta`
    evidence rather than unknown provider output.
  - Focused validation is clean:
    - `uv run pytest -q tests/test_dispatch.py tests/test_source.py -k 'exec or visibility or should_materialize'`
    - `11 passed`
    - `uvx pyright src/gotta/plugins/exec.py src/gotta/builtin.py src/gotta/dispatch/main.py src/gotta/source/visibility.py src/gotta/capture.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because `exec` was still a missing architectural center, not
  just a hot file. More shared-core provider-decoupling of the old species
  would be churn now. The next honest move is derived-backend contracts or a
  shift into provider-local / algorithmic pressure.
- [x] Derived-backend contract tranche landed.
  - `src/gotta/plugins/session/backend.py` now centers:
    - `GraphIndexBackend`
    - `LeadIndexBackend`
    - `SemanticIndexBackend`
    - `default_graph_index_backend(...)`
    - `default_lead_index_backend(...)`
    - `default_semantic_index_backend(...)`
  - `src/gotta/content/store.py` no longer carries the stale generic
    `IndexStore` placeholder.
  - `gotta session graph`, `gotta session leads`, and `gotta session analyze`
    now query explicit derived backends instead of reaching directly into the
    in-process builders.
  - Focused validation is clean:
    - `uv run pytest -q tests/test_session.py -k 'default_graph_index_backend or default_lead_index_backend or default_semantic_index_backend or session_analyze_extracts_explicit_leads_and_surfaces_gaps or session_leads_can_focus_one_artifact_by_artifact_locator or session_analyze_treats_multiple_renderings_as_variants_not_collisions'`
    - `6 passed`
    - `uvx pyright src/gotta/plugins/session/backend.py src/gotta/plugins/session/graph/main.py src/gotta/plugins/session/lead/main.py src/gotta/plugins/session/analyze/main.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because explicit graph/lead/semantic backends were still a
  missing executable center and the generic `IndexStore` had become stale
  architectural residue. More shared-core collapse of this exact species would
  now be churn. The next honest move is provider-local / algorithmic pressure
  unless runtime-selected alternate backends become real code instead of prose.
- [x] Analyze focus-kernel tranche landed.
  - `src/gotta/plugins/session/analyze/focus.py` now owns the focus-selection
    kernel instead of leaving focus synthesis split across both payload
    builders.
  - The named focus owner now centers:
    - `LineageFocusSelection`
    - `SemanticFocusSelection`
    - `select_lineage_focus(...)`
    - `select_semantic_focus(...)`
  - `src/gotta/plugins/session/analyze/lineage.py` now assembles
    `LineageFocusPayload` around the named selection instead of owning:
    - candidate scoring
    - seed selection
    - neighborhood expansion
    - neighbor pruning
  - `src/gotta/plugins/session/analyze/semantic.py` now assembles
    `SemanticFocusPayload` around the named selection instead of owning:
    - node scoring
    - seed selection
    - incident-edge collection
    - neighbor ranking
  - Focused validation is clean:
    - `uv run pytest -q tests/test_session.py -k 'session_analyze_focus or session_analyze_lineage_focus or session_analyze_all_mode_focus_returns_combined_outputs'`
    - `5 passed`
    - `uvx pyright src/gotta/plugins/session/analyze/focus.py`
    - `0 errors, 0 warnings, 0 informations`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it replaced two overloaded focus payload builders with
  one truthful focus-selection owner. More focus-kernel reshuffling of this
  exact species would likely become helper confetti. The next honest move was
  the still-hot analyze render seam, not more registry or storage churn.
- [x] Analyze render-access tranche landed.
  - `src/gotta/plugins/session/analyze/render.py` no longer spelunks optional
    TypedDict payloads directly across mermaid, text, and markdown rendering.
  - The render seam now goes through one local render-access layer:
    - `_mapping(...)`
    - `_mapping_list_field(...)`
    - `_text_field(...)`
    - `_int_field(...)`
    - `_bool_field(...)`
    - `_string_list_field(...)`
  - This tranche deleted the dominant type-pressure center in the analyze
    render surface without reopening payload contracts.
  - Focused validation is clean:
    - `uvx pyright src/gotta/plugins/session/analyze/render.py`
    - `0 errors, 0 warnings, 0 informations`
    - `uv run pytest -q tests/test_session.py -k 'analyze'`
    - `22 passed`
  - Fresh discover after the cut shows the pressure map moved materially:
    - `src/gotta/plugins/session/analyze/render.py` dropped off the type board
    - overall type pressure dropped from `316 diagnostics` to `106 diagnostics`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it removed the dominant render/type-pressure seam from
  the current analyze surface and made the next pressure centers clearer. More
  render access cleanup of this exact species would likely be churn. Fresh
  study says the next honest pressure centers are:
  - `src/gotta/plugins/session/analyze/focus.py:select_lineage_focus`
  - `src/gotta/plugins/session/analyze/lineage.py:lineage_payload`
  - then provider-local kernels like GitHub search and Jira field coercion
- [x] Analyze lineage-builder tranche landed.
  - `src/gotta/plugins/session/analyze/lineage.py` now centers typed local
    build records instead of mutating anonymous dict soup across the whole
    builder path:
    - `_RevisionTrackEvent`
    - `_SourceState`
    - `_ContentDetailState`
    - `_LineageBuildState`
  - Revision edges now track typed revision events instead of abusing
    `LineageRevisionEdge` as temporary working state.
  - The lineage seam now converts cross-package lead records explicitly into:
    - `LeadSourceSummary`
    - `LeadEdgeSummary`
    instead of leaking the shared lead payload types through the analyze
    boundary.
  - Focused validation is clean:
    - `uvx pyright src/gotta/plugins/session/analyze/lineage.py`
    - `0 errors, 0 warnings, 0 informations`
    - `uv run pytest -q tests/test_session.py -k 'analyze or lineage'`
    - `22 passed`
  - Fresh discover after the cut shows the pressure map moved materially:
    - `src/gotta/plugins/session/analyze/lineage.py` dropped off the type board
    - `lineage_payload(...)` dropped off the hotspot board
    - remaining analyze pressure is now centered in:
      - `src/gotta/plugins/session/analyze/focus.py:select_lineage_focus`
      - `src/gotta/plugins/session/analyze/overview.py:analysis_overview_payload`
      - `src/gotta/plugins/session/analyze/semantic.py`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because it replaced one overloaded analyze builder with a
  truthful typed owner and deleted the last obvious local dict-soup state in
  that seam. More lineage-builder cleanup of this exact species would likely be
  churn. The next honest move is the still-hot analyze focus/overview pressure,
  starting with `select_lineage_focus(...)`, before shifting fully into
  provider-local kernels.
- [x] Analyze focus-state tranche landed.
  - `src/gotta/plugins/session/analyze/focus.py` now centers one mutable
    lineage selection owner:
    - `_LineageFocusState`
  - `select_lineage_focus(...)` no longer owns all of:
    - seed-state unpacking
    - selection expansion
    - lead-edge absorption
    - neighbor pruning
    - focused payload projection
    inline inside one function
  - The focus seam now drives those transitions through one state owner
    instead of ad hoc local set choreography.
  - Focused validation is clean:
    - `uvx pyright src/gotta/plugins/session/analyze/focus.py`
    - `0 errors, 0 warnings, 0 informations`
    - `uv run pytest -q tests/test_session.py -k 'focus or lineage_focus or semantic_focus or all_mode_focus'`
    - `6 passed`
  - Fresh discover after the cut shows the pressure map moved materially:
    - `src/gotta/plugins/session/analyze/focus.py:select_lineage_focus`
      dropped off the hotspot board
    - remaining analyze pressure is now centered in:
      - `src/gotta/plugins/session/analyze/overview.py:analysis_overview_payload`
      - `src/gotta/plugins/session/analyze/semantic.py`
      - then shared render pressure in `src/gotta/plugins/session/lead/render.py`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because the focus selector was still one overloaded
  algorithmic center, not just a hot file. More focus-kernel cleanup of this
  exact species would likely be churn now. The next honest move is the analyze
  overview/semantic seam, and only after that should the pressure shift fully
  into provider-local kernels.
- [x] Analyze semantic-graph tranche landed.
  - `src/gotta/plugins/session/analyze/semantic.py` now centers explicit graph
    build owners instead of mutating node and edge TypedDict state inline:
    - `_SemanticNodeState`
    - `_SemanticGraphState`
  - The semantic seam now reads lineage payload families through explicit
    helper readers instead of direct required-key spelunking across optional
    TypedDicts.
  - `semantic_payload(...)` no longer owns all of:
    - provider/source graph node creation
    - resource/query expansion
    - content node materialization
    - lead-source discovery merges
    - edge assembly
    inline in one dict-and-set builder.
  - Focused validation is clean:
    - `uvx pyright src/gotta/plugins/session/analyze/semantic.py`
    - `0 errors, 0 warnings, 0 informations`
    - `uv run pytest -q tests/test_session.py -k 'semantic or analyze or graph or all_mode'`
    - `27 passed`
  - Fresh discover after the cut shows the pressure map moved materially:
    - `src/gotta/plugins/session/analyze/semantic.py` dropped off the type board
    - overall type pressure dropped from `52 diagnostics` to `24 diagnostics`
    - remaining shared analyze pressure is now centered in:
      - `src/gotta/plugins/session/analyze/overview.py:analysis_overview_payload`
      - then shared render pressure in `src/gotta/plugins/session/lead/render.py`
      - with the larger remaining heat otherwise now provider-local
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because `semantic_payload(...)` was still one overloaded
  mutable graph builder and the main remaining analyze type-pressure seam.
  More semantic cleanup of this exact species would likely be churn now. The
  next honest move is `analysis_overview_payload(...)`, and after that shared
  analyze pressure is probably across its natural boundary.
- [x] Analyze overview-state tranche landed.
  - `src/gotta/plugins/session/analyze/overview.py` now centers one truthful
    summary owner:
    - `_OverviewState`
  - `analysis_overview_payload(...)` no longer owns all of:
    - semantic graph signal extraction
    - provider/kind/relation ranking
    - anchor and query-seed selection
    - structural/source thresholding
    - final overview payload assembly
    inline inside one function.
  - The overview seam now drives those transitions through one summary-state
    owner instead of local counter/list choreography.
  - Focused validation is clean:
    - `uvx pyright src/gotta/plugins/session/analyze/overview.py`
    - `0 errors, 0 warnings, 0 informations`
    - `uv run pytest -q tests/test_session.py -k 'overview or all_mode or analyze'`
    - `22 passed`
    - `uv run ruff check src/gotta/plugins/session/analyze/overview.py`
    - `All checks passed!`
  - Fresh discover after the cut shows the pressure map moved materially:
    - shared analyze pressure is effectively across its natural boundary
    - the remaining hotspot board is now led by:
      - `src/gotta/plugins/github/search.py:markdown_search`
      - `src/gotta/plugins/github/parse.py:parse_args`
      - `src/gotta/plugins/jira.py:coerce_field_value`
      - `src/gotta/plugins/session/lead/render.py:render_leads_text`
      - `src/gotta/plugins/session/graph/payload.py:graph_payload`
    - overall type pressure remains low at `24 diagnostics`
- [x] Diminishing-returns judgment for this cycle:
  this paid rent because `analysis_overview_payload(...)` was still the last
  overloaded shared analyze center. More shared analyze cleanup of this exact
  species would likely be churn now. The next honest move is the remaining
  shared typed-boundary pressure in `session/lead/render.py` and
  `session/graph/payload.py`, or a deliberate shift into provider-local
  kernels.

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
- [x] Type the exported `session analyze` payload family and center it in one
  truthful owner.
- [x] Type the exported `session lead` payload family and promote shared lead
  edge/summary records into a cross-package model center.
- [x] Type the exported `session scan` payload family and center it in one
  truthful owner.
- [x] Type the exported `session timeline` payload family and center it in one
  truthful owner.
- [x] Type the exported `session manifest` payload family and center it in one
  truthful owner.
- [x] Finish the smaller typed-count and payload cleanup still left in
  `session graph`.
- [x] Remove runner/stdout fallback capture from federated `search`.
- [x] Type the remaining actor/runtime status payload cluster and `session
  doctor`.
- [x] Type the canonical authored-state `logs`, `notes`, and `oops` records and
  payload surfaces.
- [x] Type the event-sourced `todo` core and payload seam.
- [x] Start hot-kernel reduction in the lead kernel by landing:
  - `LeadEdge`
  - `LeadResolution`
  - `resolve_lead_resolution(...)`
- [x] Move provider artifact-intent classification out of the core and into
  installed surface contracts.
- [x] Delete provider-specific canonicalization fallback from the core and keep
  only surface-owned canonical locators plus a generic fallback.
- [x] Replace the provider-name dispatch actor branch with a surface-declared
  shared actor-option contract.
- [x] Move top-level `search` provider routing and read/specialized redirects
  out of shared core and into installed surface contracts.
- [x] Move provider-specific source timestamp defaults out of shared dispatch
  and into installed surface contracts.
- [x] Move provider-specific visibility classification out of shared core and
  onto installed surface contracts.
- [x] Land the explicit `exec` surface as canonical evidence instead of
  implicit local-command fallback.
- [x] Land explicit graph/lead/semantic derived-backend contracts and delete the
  stale generic `IndexStore` placeholder.
- [x] Collapse the `session analyze` focus seam into one truthful focus-kernel
  owner.
- [x] Collapse the `session analyze` render seam onto one render-access layer
  and remove its direct optional-payload spelunking.
- [x] Collapse the `session analyze` lineage seam onto typed local build
  records and explicit analyze-owned lead summaries.
- [x] Collapse the `session analyze` lineage focus selector onto one mutable
  selection-state owner.
- [x] Collapse the `session analyze` semantic graph builder onto explicit node
  and graph state owners.
- [x] Collapse the `session analyze` overview builder onto one summary-state
  owner.
- [ ] Next tranche: cut the remaining shared typed-boundary pressure in
  `src/gotta/plugins/session/lead/render.py` or
  `src/gotta/plugins/session/graph/payload.py`, then decide whether further
  rent still exists outside provider-local kernels.

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
- [x] Remove runner/stdout fallback capture from federated `search` in
  `src/gotta/plugins/search.py`.
- [x] Eliminate anonymous `dict`-shaped exported payloads in the settled
  session-view cluster:
  - manifest
  - lead
  - timeline
  - graph
  - analyze
- [x] Type the remaining actor/runtime status payload cluster and `session
  doctor`.
- [x] Type the canonical authored-state `logs`, `notes`, and `oops` records and
  payload surfaces.
- [x] Type the event-sourced `todo` core and payload seam.
- [x] Move provider artifact-intent classification out of the core and into
  installed surface contracts.
- [x] Delete provider-specific canonicalization fallback from the core and keep
  only surface-owned canonical locators plus a generic fallback.
- [x] Replace the provider-name dispatch actor branch with a surface-declared
  shared actor-option contract.

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
- [ ] Type the ledger records first:
  - [x] `ArtifactRecord`
  - [x] `ArtifactMetadata`
  - [x] `AliasRecord`
  - [x] `MaterializationEvent`
  - [x] `ManifestEntry`
  - `SessionBindingRecord`
  - `ActorStateRecord`
  - [ ] `StateChannelRecord`
    - concrete authored-state records now exist for:
      - `LogRecord`
      - `ActorNoteRecord`
      - `FrictionRecord`
      - `TodoItem`
      - `TodoCreateEvent`
      - `TodoCheckEvent`
    - this authored-state record family is now concrete-complete; more work of
      this exact species would not pay rent
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

- [x] Type the lead kernel:
  - [x] `LeadMention`
  - [x] `LeadEdge`
  - [x] `LeadSourceSummary`
  - [x] `LeadResolution`
- [ ] Type the derived view payload families:
  - `GraphPayload`
  - [x] `TimelineEvent`
  - [x] `AnalyzeOverview`
  - [x] `LineagePayload`
  - [x] `SemanticPayload`
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
- [x] Make installed provider surfaces declare artifact-bearing intent instead
  of letting the core infer it from provider-name tables.
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
- [x] Remove runner/stdout fallback capture from federated `search`.
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

- [x] Introduce explicit backend interfaces for optional derived indexes:
  - `GraphIndexBackend`
  - `LeadIndexBackend`
  - `SemanticIndexBackend`
- [x] Make backend contracts typed around:
  - ingest
  - rebuild
  - query
  - health
  - staleness
- [x] Make derived views backend-aware without surrendering ledger truth.
- [x] Keep Memgraph, Kuzu, DuckDB, and similar systems strictly optional,
  rebuildable accelerators.

## Phase 6: Land The Explicit `exec` Surface

- [x] Land `gotta exec -- <command> [args...]`.
- [x] Capture canonical execution evidence including:
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
- [x] Materialize `exec` results as canonical `evidence`.
- [x] Route `exec` artifacts into:
  - manifest
  - timeline
  - graph
  - leads
  - analyze

## Phase 7: Algorithmic Hardening Inside Settled, Typed Boundaries

- [x] Reduce algorithmic density in the `session analyze` kernels:
  - `src/gotta/plugins/session/analyze/focus.py`
  - `src/gotta/plugins/session/analyze/lineage.py`
  - `src/gotta/plugins/session/analyze/semantic.py`
  - `src/gotta/plugins/session/analyze/overview.py`
- [ ] Reduce remaining shared typed-boundary density in:
  - `src/gotta/plugins/session/lead/render.py`
  - `src/gotta/plugins/session/graph/payload.py`
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
- [x] Introduce `LedgerStore`, `BlobStore`, and `StateStore`.
- [x] Split `content/store.py` into storage contracts and concrete filesystem
  implementation.
- [x] Remove remaining internal dependence on dead legacy properties from the
  test surface, then delete those properties.
- [x] Type the artifact-pipeline records above storage:
  - `Capture`
  - `Projection`
  - `ArtifactKind`
- [x] Introduce one truthful default backend owner and retarget the shared
  views to it.
- [ ] Keep pushing derived views toward ledger/state contracts instead of
  concrete filesystem ownership.
- [x] Type the lead kernel.
- [x] Type the first analyze/graph/timeline/session payload families.
- [x] Replace anonymous manifest and lead aggregate records with named types.
- [ ] Fix `gsheets` capture semantics.
- [x] Remove federated `search` runner-capture fallback.
- [x] Type the canonical authored-state `logs`, `notes`, and `oops` records and
  read payload surfaces.
- [x] Type the event-sourced `todo` core and payload seam.
- [x] Move provider artifact-intent classification out of the core and into
  installed surface contracts.
- [x] Delete provider-specific canonicalization fallback from the core and keep
  only surface-owned canonical locators plus a generic fallback.
- [x] Replace the provider-name dispatch actor branch with a surface-declared
  shared actor-option contract.
- [x] Move top-level `search` provider routing and read/specialized redirects
  out of shared core and into installed surface contracts.
- [x] Move provider-specific source timestamp defaults out of shared dispatch
  and into installed surface contracts.
- [x] Move provider-specific visibility classification out of shared core and
  onto installed surface contracts.
- [x] Switch from shared-core contract collapse to hot-kernel reduction once
  the last authored-state seam is typed.
- [x] Reassess whether any remaining provider-specific dispatch or routing
  fallback in the shared core is still load-bearing.
- [x] Reassess whether provider-specific visibility classification in shared
  core is still architectural or should move onto installed surface contracts.
- [x] Add explicit backend interfaces before any Memgraph/Kuzu integration.

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
- [x] Derived backend interfaces exist without becoming storage truth.
- [x] `gotta exec` exists as explicit evidence rather than implicit fallback.
- [ ] The lead kernel and major derived payloads are typed.
- [ ] Remaining work is primarily provider-local or algorithm-local, not
  architectural blur.
