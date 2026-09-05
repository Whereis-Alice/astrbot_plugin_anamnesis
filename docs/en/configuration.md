# Configuration

Anamnesis defaults work for most users. The settings you usually need to touch are model providers, recall size, memory isolation, graph retrieval, backup, and cleanup.

> [!NOTE]
> Every numeric config item has a hard allowed range, shown on the AstrBot config page as a slider plus number field and in the tables below. Out-of-range values are clamped to the nearest bound on plugin load (invalid values fall back to that item's default), each fix is logged as a warning and written back to the config file — a single bad value no longer resets the whole config to defaults.

## Recommended profiles

| Scenario | Recommendation |
| --- | --- |
| First-time setup | Configure only `provider_settings.llm_provider_id` and `provider_settings.embedding_provider_id`; keep the rest at defaults |
| Private long-term assistant | Keep persona and session filtering enabled; keep `summary_trigger_rounds` around 8-12 |
| Group companion | Enable `session_manager.enable_full_group_capture` and consider a larger `context_window_size` |
| Low-resource server | Reduce `index_rebuild_settings.embedding_batch_size`, keep `tasks_limit = 1`, and increase request delays |
| Higher recall quality | Keep graph memory and atomization enabled; set `recall_engine.top_k` to 5-8 |
| Cost-sensitive setup | Lower `top_k`, disable recent-context expansion, and increase summary trigger rounds |

## Model providers

| Key | Default | Description |
| --- | --- | --- |
| `provider_settings.embedding_provider_id` | empty | Generates memory vectors. Empty means AstrBot's default embedding provider |
| `provider_settings.llm_provider_id` | empty | Summarizes conversations and evaluates memory importance. Empty means AstrBot's default LLM |

Try to keep the embedding model stable. If you change it and old memories recall poorly, run `/anam rebuild-index`.

## Session management

| Key | Default | Description |
| --- | --- | --- |
| `session_manager.enable_full_group_capture` | `true` | Captures group messages that do not directly mention the bot |
| `session_manager.context_window_size` | `50` | Historical message window used for summarization and context analysis |
| `session_manager.max_messages_per_session` | `1000` | Maximum stored messages for one session |
| `session_manager.cleanup_batch_size` | `50` | Number of old summarized messages cleaned per batch |

For very busy group chats, lower `context_window_size` or disable full group capture.

## Recall and injection

| Key | Default | Description |
| --- | --- | --- |
| `recall_engine.top_k` | `5` | Number of memories automatically recalled each turn |
| `recall_engine.max_k` | `10` | Maximum results returned by active agent recall |
| `recall_engine.importance_weight` | `1.0` | Importance weight in final ranking |
| `recall_engine.min_importance_for_retrieval` | `0.0` | Minimum importance; `0` disables the filter |
| `recall_engine.min_similarity_for_retrieval` | `0.0` | Minimum vector similarity; keyword-only hits remain eligible |
| `recall_engine.recent_memory_count` | `2` | Recall slots reserved for recent memories |
| `recall_engine.recent_memory_max_age_hours` | `72` | Time window for recent-memory slots |
| `recall_engine.memory_type_filter` | `all` | Use `event_only` to exclude known preference-only or relationship-only memories |
| `recall_engine.fallback_to_vector` | `true` | Falls back to vector search if hybrid retrieval fails |
| `recall_engine.rerank_enabled` | `false` | Reranks fused candidates by query relevance with a Rerank model |
| `recall_engine.rerank_provider_id` | `""` | ID of a Rerank-type provider in AstrBot; leave empty to skip |
| `recall_engine.rerank_candidates` | `20` | Fused candidates sent to the Rerank model (2-100); top_k kept after reranking |
| `recall_engine.injection_method` | `extra_user_content` | Where or how recalled memories are injected |
| `recall_engine.inject_with_recent_context` | `false` | Expands the query with recent conversation |
| `recall_engine.search_cache_enabled` | `true` | Enables short-term retrieval caching |

`extra_user_content` is the safest default. Gemini providers automatically fall back from `fake_tool_call` to `extra_user_content`. DeepSeek V4 thinking mode can now use normal `fake_tool_call` on recent AstrBot versions; the legacy `fake_tool_call_deepseek_v4` option is kept only as a compatibility alias and automatically falls back to `fake_tool_call`.

## Memory isolation

| Key | Default | Description |
| --- | --- | --- |
| `filtering_settings.use_persona_filtering` | `true` | Only recall memories for the current persona |
| `filtering_settings.memory_scope_mode` | `legacy` | Preserve legacy behavior, or scope by session, user, or globally |
| `filtering_settings.use_session_filtering` | `true` | Controls session filtering only in `legacy` mode |
| `filtering_settings.isolated_sessions` | Empty | Full session IDs that must always remain isolated, one per line |
| `access_control.whitelist_enabled` | `false` | Allow only listed identities to use long-term memory |
| `access_control.allowed_ids` | Empty | User ID, `platform:user ID`, group ID, or full session ID |
| `access_control.identity_aliases` | Empty | One `source identity=canonical name` mapping per line |

`user` shares memory across private and group chats for the same platform user. `global` shares memory across every non-isolated session. `isolated_sessions` always takes precedence. In `legacy` mode with session filtering disabled, configuring any isolated session moves non-isolated writes into the dedicated global scope so they cannot read isolated data. Enabling the allowlist with an empty list denies automatic capture, summarization, recall, and Agent memory tools.

Aliases are matched in this order: `platform:user ID`, user ID, then current username. The mapped display name is applied before summarization. Scope changes affect newly written memories only; existing memories are not migrated or re-embedded automatically.

## Person identity and nicknames

| Key | Default | Description |
| --- | --- | --- |
| `identity_settings.anchor_enabled` | `true` | Rewrite names inside stored facts as `nickname#account-tail` |
| `identity_settings.anchor_format` | `{name}#{tail}` | Anchor template; must contain both `{name}` and `{tail}` |
| `identity_settings.anchor_tail_length` | `4` | How many trailing account-id digits to use; `0` uses the full id |
| `identity_settings.alias_max_per_identity` | `40` | Maximum historical nicknames kept per account |
| `identity_settings.alias_cache_max` | `2000` | Row cap for the in-memory alias snapshot; `0` disables caching |
| `identity_settings.alias_cache_ttl_seconds` | `300` | Lazy refresh interval for the alias snapshot |
| `identity_settings.alias_query_expansion` | `true` | Expand queries with the other nicknames of the same account |
| `identity_settings.alias_backfill_on_start` | `true` | Backfill nicknames from existing memory metadata when the table is empty |
| `identity_settings.identity_guard_enabled` | `true` | Drop identity records that would poison memory and normalize the Bot's own names |
| `identity_settings.identity_guard_max_tracked` | `512` | Maximum accounts the guard tracks in memory |
| `identity_settings.max_distinct_names_per_identity` | `12` | Distinct nicknames within the window before the name is treated as unreliable |
| `identity_settings.name_stability_window_hours` | `24` | Observation window for counting renames |

Nicknames are neither unique nor stable, yet fact nodes are keyed by their text. Without anchoring, two different accounts sharing one nickname collapse into a single person node and one member's actions get attributed to the other; after a rename, older memories degrade into a stranger. Anchoring welds the account tail into the fact text, and the alias store remembers every nickname an account ever used so pre-rename memories stay searchable.

Anchoring affects the fact text written to long-term memory only; the summary injected into the model and the raw conversation are untouched. Changing `anchor_format` or `anchor_tail_length` does not rewrite existing memories, so both spellings coexist until the older ones decay away.

`alias_cache_max` and `identity_guard_max_tracked` are the only two keys here that directly determine resident memory: the first is the alias snapshot row cap, the second is how many accounts the guard keeps recent nicknames for. On a 1 GB host, lower them to `500` and `128`; setting `alias_cache_max` to `0` disables the snapshot entirely and queries SQLite every time.

For attributions that older builds already wrote incorrectly, use `/anam identity` to inspect and `/anam fix-identity` to repair; see [Commands](/en/commands).

## Reflection and lifecycle

| Key | Default | Description |
| --- | --- | --- |
| `reflection_engine.summary_trigger_rounds` | `10` | Number of conversation rounds before summarization |
| `reflection_engine.include_source_time_tags` | `true` | Derives source date tags from original message timestamps |
| `reflection_engine.source_retention_importance_threshold` | `0.8` | Retains original messages separately at or above the threshold |
| `importance_decay.decay_rate` | `0.01` | Daily importance decay |
| `importance_decay.access_decay_window_days` | `30.0` | Time window for access reinforcement |
| `importance_decay.access_decay_max_count` | `10` | Maximum access reinforcement count |
| `importance_decay.protected_importance_threshold` | `1.0` | Memories at or above this importance do not decay |

Lower `summary_trigger_rounds` if you want the bot to remember faster. Raise it if you want fewer LLM calls.

Retained source is written only to the SQLite `memory_sources` table, not to vector, BM25, graph, or atom indexes, so it does not increase vector count. It does increase database disk usage. Re-summarizing from Dashboard calls the LLM once, replaces the old memory, and regenerates its embedding and all derived indexes.

## Agent tools

| Key | Default | Description |
| --- | --- | --- |
| `agent_tools.enable_recall_tool` | `true` | Registers `anamnesis_recall_memory` for active recall |
| `agent_tools.enable_memorize_tool` | `false` | Registers `anamnesis_memorize_memory` for active writes |

The write tool is powerful and depends on model discipline. Start with active recall, then enable active writes after observing stable behavior.

## Graph memory and atomization

| Key | Default | Description |
| --- | --- | --- |
| `graph_memory.enabled` | `true` | Enables graph-route retrieval |
| `graph_memory.document_route_weight` | `0.65` | Document-route weight |
| `graph_memory.graph_route_weight` | `0.35` | Graph-route weight |
| `graph_memory.cross_route_bonus` | `0.08` | Bonus when both routes hit the same memory |
| `graph_memory.expansion_hops` | `1` | Graph neighbor expansion hops |
| `graph_memory.dynamic_route_weighting` | `true` | Adjusts route weights based on query intent |
| `graph_memory.atom_enabled` | `true` | Enables memory atomization |
| `graph_memory.max_edge_entries_per_memory` | `0` | Maximum relation (edge) index entries written per memory; `0` means unlimited. Graph entries dominate database size, so long-running large stores usually want `12`–`24` |

For relationship-heavy use, increase graph-route weight or set `expansion_hops` to `2`. If the database is large, second-hop expansion adds query cost, so use the WebUI recall debugger to inspect results first.

## Backup, migration, and cleanup

| Key | Default | Description |
| --- | --- | --- |
| `migration_settings.auto_migrate` | `true` | Migrates old databases at startup |
| `migration_settings.create_backup` | `true` | Creates a backup before migration |
| `migration_settings.auto_import_legacy` | `true` | On first start with an empty data directory, imports data from the old plugin automatically (equivalent to one `/anam migrate exec`) |
| `migration_settings.legacy_plugin_name` | `astrbot_plugin_livingmemory` | Data directory name of the old plugin. Change it only if you renamed that directory |
| `backup_settings.max_keep` | `2` | Maximum number of automatic backups to keep; oldest are deleted first |
| `backup_settings.max_total_size_mb` | `1024` | Total size cap for the backup directory in MB; oldest backups keep being deleted above it |
| `backup_settings.skip_if_larger_than_mb` | `512` | Skip automatic backup when the database exceeds this size in MB; `0` disables the skip |
| `backup_settings.enabled` | `true` | Daily database backup |
| `backup_settings.keep_days` | `7` | Backup retention days |
| `forgetting_agent.auto_cleanup_enabled` | `true` | Daily cleanup for old low-importance memories |
| `forgetting_agent.auto_archived_enabled` | `false` | Archives cleanup candidates outside retrieval indexes instead of deleting them |
| `forgetting_agent.cleanup_days_threshold` | `30` | Age threshold for cleanup candidates |
| `forgetting_agent.cleanup_importance_threshold` | `0.3` | Importance threshold for cleanup candidates |

With automatic archiving enabled, source documents remain visible and restorable in the Dashboard. Restoring regenerates the embedding and rebuilds BM25, graph, and memory-atom indexes.

`migration_settings` only covers in-place schema upgrades of this plugin's own database at startup. It is unrelated to `/anam migrate`, which copies data over from the old LivingMemory plugin; see [Commands](/en/commands) for that.

## Storage maintenance

| Option | Default | Description |
| --- | --- | --- |
| `storage_maintenance.auto_prune_write_ops` | `true` | Prunes the write-operation log during daily maintenance |
| `storage_maintenance.write_ops_keep_days` | `7.0` | Retention in days for completed write-operation log rows |
| `storage_maintenance.write_ops_keep_max` | `2000` | Maximum completed write-operation log rows to keep; the oldest are trimmed first |
| `storage_maintenance.write_ops_failed_keep_days` | `30.0` | Retention in days for failed write-operation log rows, kept longer for debugging |
| `storage_maintenance.daily_vacuum` | `false` | Runs SQLite `VACUUM` during daily maintenance. Off by default |
| `storage_maintenance.graph_prune_orphans` | `false` | Prunes orphaned graph nodes, edges, and entries during daily maintenance |

The write-operation log (`memory_write_ops`) stores the full payload of every memory write so that crashes can be recovered and replays stay idempotent. It is only useful while a write is in flight; on a store that had been running for 70 days this table measured 24MB.

`daily_vacuum` is off on purpose: `VACUUM` needs temporary space as large as the database and holds a write lock while it runs. Keep it off on low-memory or disk-constrained hosts and reclaim space on demand with `/anam vacuum` instead.

`graph_prune_orphans` is off because normal operation should not create orphans. If you have deleted memories by hand or interrupted a rebuild, inspect the result of `/anam vacuum` first before leaving it on.
## Memory store consolidation

| Key | Default | Description |
| --- | --- | --- |
| `memory_consolidation.enabled` | `false` | Enables periodic memory-store consolidation |
| `memory_consolidation.trigger` | `daily` | Trigger mode: `daily` = daily schedule, `reflection` = piggyback on each reflection |
| `memory_consolidation.granularity` | `session` | Aggregation granularity: `session` = same session, `semantic` = cross-session semantic clustering |
| `memory_consolidation.keep_original` | `archive` | Handling of originals after merge: `archive` = keep archived, `delete` = remove permanently |
| `memory_consolidation.min_memories_per_group` | `3` | Minimum memories per group to trigger consolidation |
| `memory_consolidation.min_age_days` | `7` | Only consolidate memories older than this many days |
| `memory_consolidation.max_importance` | `0.5` | Only consolidate memories below this importance |
| `memory_consolidation.max_groups_per_run` | `5` | Maximum groups consolidated per run |
| `memory_consolidation.semantic_similarity_threshold` | `0.7` | Minimum similarity in semantic clustering mode |
| `memory_consolidation.max_candidates` | `500` | Maximum candidate memories read per run, so large stores do not load everything at once |
| `memory_consolidation.max_group_size` | `10` | Maximum memories per group; larger groups are split evenly |
| `memory_consolidation.min_run_interval_hours` | `6.0` | Minimum hours between consolidation runs; also the cooldown for `trigger=reflection` |
| `memory_consolidation.merge_input_char_budget` | `12000` | Total character budget for the merge prompt; individual memories are truncated proportionally above it |

Memory consolidation controls the memory-store size at the source: scattered low-value memories are aggregated, organized, and summarized into a single concise memory, avoiding information loss from hard truncation at injection time. The merged result is written as a new memory and the originals are archived or deleted according to `keep_original`. With `trigger=reflection`, a 6-hour cooldown prevents per-message triggering.

## Index rebuild tuning

| Key | Default | Description |
| --- | --- | --- |
| `index_rebuild_settings.batch_size` | `50` | Memories read per batch |
| `index_rebuild_settings.embedding_batch_size` | `8` | Texts per embedding request |
| `index_rebuild_settings.tasks_limit` | `1` | Embedding concurrency limit |
| `index_rebuild_settings.max_retries` | `5` | Retry count for a failed batch |
| `index_rebuild_settings.request_delay` | `5.0` | Delay between embedding requests |
| `index_rebuild_settings.max_failure_ratio` | `0.02` | Allowed failure ratio |

If you hit API rate limits, increase `request_delay` first, then lower `embedding_batch_size`. Avoid raising concurrency blindly; index rebuilds are more about finishing reliably than finishing aggressively.
