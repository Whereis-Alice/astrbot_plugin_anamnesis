# 3.4.0: upstream adaptations and profiles

This release adapts the remaining reviewed capabilities from LivingMemory **2.7.1**, pinned to `b8aaaec2c1232ec2c86fce22cec066a0069018e0`, onto Anamnesis 3.3.0. Existing tool names, identities and session/persona boundaries are preserved.

## Readable profiles

Open Plugins → Anamnesis → Pages → dashboard → Profiles. Find a person by name, account, latest known group or source session, then read complete categorized facts with separate chat sources. Each fact supports full text, editing, deletion and source-memory navigation. Narrow windows and keyboard navigation are supported.

Directory grouping does not merge stored facts. Updates use the exact scope/key, and `user_profile.scope_mode=session` still isolates injected profiles by chat. Explicit aliases determine identity grouping. New extraction supplies readable labels; older facts have fallback titles.

Since 3.4.2, latest group labels are kept separately from each fact's original source chat. Missing account IDs do not create profiles, and invalid source memories do not displace valid facts from the injection quota.

## Retrieval and lifecycle

- Vector search expands when scope filtering leaves too few results, with a default cap of 8192. Document and graph routes share embeddings and checked IDs, including misses. Legacy hosts exposing only `retrieve` retain expansion; embeddings inside that API cannot be reused.
- `recall_engine.fusion_mode=rrf` preserves compatibility. Optional `score` uses normalized relevance, route weights and agreement; defaults are candidate multiplier 3, BM25 weight 0.5 and agreement bonus 0.25.
- Atom FTS reconciles IDs, content and state at startup. Atom matches contribute parent candidates. Expired facts leave every displayed summary field; cached results recheck current document state and expiry.
- Graph reads reuse a read-only connection. Access writes coalesce while preserving repeated counts and flush on unload. Failed vector deletion persistently requests rebuilding. FAISS startup runs outside the event loop; request hooks use a readiness snapshot.

## Summary integrity

Platform timestamps support milliseconds. `reflection_engine.split_by_day=true` uses the host timezone, and `idle_gap_minutes=180` splits idle windows; 0 disables the idle boundary. A user message and reply stay together. Sealed short windows can summarize while the new window stays open.

WebChat checkpoints reconcile edited/regenerated tails and retain compressed heads. Revision checks invalidate stale summaries before and after persistence and compensate memory/profile writes. Platforms without reliable checkpoints retain conservative behavior.

`reflection_engine.dedup_enabled=false` by default. Merging requires equivalent facts, matching scope/persona/source chat/identity and text similarity ≥0.97. Negation, changed numbers and new facts stay separate. Merges retain the original ID/summary and append provenance and retained sources.

Since 3.4.1, summaries that can be reverted also require the same source-message window. Separate turns stay separate so reverting new messages preserves memories supported by older messages. Summaries with message IDs require identical valid ID sets; legacy revision-based summaries require identical source windows.

## Tools and migration

Since 3.4.2, runtime migration requires a consistent, complete target chat origin in `source_session_id` or `source_window.session_id`. Bare numeric IDs and records with missing or conflicting provenance stay unchanged and require an administrator's migration preview. Automatic moves use database backups and synchronize atom and graph scopes. If a graph rebuild or selected document write is still in progress, wait for completion and run a new preview before retrying.

`agent_tools.agentic_recall_enabled` adds exact speaker/time filters, excluded IDs and cursors to `anamnesis_recall_memory`, and registers `anamnesis_read_memory_evidence`. Time uses event time, source-message time ranges, then creation time, and labels the basis used. Unix seconds and ISO-8601 are accepted; timezone-free values use UTC. Pagination uses a bounded pool, default 100. Retrieval and evidence share 5 calls, 20 seconds of execution and 16000 output characters per request. Evidence additionally checks source sessions for global or historically unfiltered recall. Sources can contain superseded claims; live facts and lifecycle information accompany them.

`anamnesis_forget_memory` requires exact ID, reason and ownership checks. `agent_tools.forget_mode=delete` is permanent; `archive` is recoverable through `action=restore`. Archive suppresses related profile injection and retains original atom expiry. Restore does not reset TTL or overwrite later profile corrections. Defaults: 3 operations per request, 2-second per-scope cooldown (0 disables), bounded audit records.

Administrators use `/anam migrate-scope <source> <target>` to preview, then `backup <fingerprint>` or `apply <fingerprint>`. An optional persona follows the arguments; for a persona-restricted preview use `preview - <persona>`. Apply rechecks the fingerprint and creates SQLite backups before updating documents, atoms, graph entries and graph-vector metadata. Source sessions/personas stay intact; personal profiles and sharing configuration do not change. Ordinary errors roll back. WAL across files cannot promise power-loss atomicity; backups remain in `scope-backups`.

## Settings and UI

The dashboard settings page offers schema-based categories, search and folding. Values validate and persist before a required plugin reload; the host configuration page remains available. Memory/graph navigation shows provenance, record reasons and route scores, with persistent retry feedback for failures. Local Lucide subsets and bundles build using `npm run dashboard:build`; verify committed assets with `npm run dashboard:check`.
