# LivingMemory upstream review (2026-10-25)

This review compares Anamnesis 3.2.4 with LivingMemory **2.7.1**, pinned to [`b8aaaec2c1232ec2c86fce22cec066a0069018e0`](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/b8aaaec2c1232ec2c86fce22cec066a0069018e0). The adaptations ship in Anamnesis **3.3.0**. Identity anchors, user profiles, retained sources, and write recovery differ between the projects and require individual adaptations.

## Adopted in this update

| Upstream direction | Anamnesis adaptation |
| --- | --- |
| [Chinese character bigrams](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/91d2634) | Adjacent pairs within continuous Chinese segments, without crossing punctuation, Latin letters, or digits; identical insert, edit, query, and rebuild rules, retaining nickname expansion |
| [Token cache](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9d8c9fa) | Short-text LRU capped at 128 entries; dictionary and stopword changes invalidate it; callers receive copies |
| [Consolidation isolation and rollback](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/a0ce689) | Both consolidation modes split by `(session_id, persona_id)` and enforce it for direct calls. Results retain scope. Partial source deletion preserves the merged copy to avoid losing the only remaining copy |
| [History ordering](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/a9eff4d) | Message ID breaks timestamp ties consistently in ascending and descending queries |
| [Decay and task completion](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/709a236) | Field-level SQL preserves concurrent metadata. Failed batches roll back without marking completion. Cancellation waits for transaction completion and records successful decay. Background failures are logged; storage tasks are cancelled after a bounded wait |
| [Migration backups](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/31a6249) | Required backup failure aborts migration; failed migration attempts SQLite backup restoration and stops initialization. The existing opt-out setting is respected |
| [Streaming LLM](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/db19cb7) | Configurable `llm_stream_mode`; only a fully ended stream with a valid final response succeeds. Automatic mode remembers successful streaming providers and does not switch on unrelated errors |
| [Injection budget](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/de8f31f) | Configurable total and per-entry budgets, preserving framing and fake-tool JSON. Stored text and the separate profile budget remain intact |
| [FAISS non-ASCII paths](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9801a3b) | Lazy loading and a shared bridge for initialization, persistence, and rebuilding. ASCII temporary serialization is copied to staging in the destination directory and atomically replaced, preserving a healthy index on cross-volume failure |
| [Documentation dependencies](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/de4578f) | source-map-js and Vue patch updates; lockfile uses the official npm registry |
| [Embedding configuration](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9c77431) | Recommend explicit stable provider selection; explain that a blank ID uses the first available model, preserving existing configurations |

## Worth adapting separately

| Direction | Remaining work |
| --- | --- |
| [Near-duplicate merging at write time](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/079026a) | Could reduce repeated event summaries. Must preserve original sources, profile ownership, failure compensation, and profile cleanup after explicit deletion |
| Wider candidates and score fusion | Evaluate with nickname expansion, recent-memory slots, graph retrieval, and Rerank to avoid noisy recall or ranking regressions |
| Shared Embedding and FAISS read/write locks | Cover the main index, graph index, asynchronous persistence, and rebuild paths together |
| Final expiry checks for memory atoms | Also sanitize `persona_summary` and `key_facts`, which take priority in display; replacing retrieval content alone can still inject expired facts |
| [History editing and regenerate reconciliation](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/3afdd2d) | Requires revisions, source tracking, and cancellation compensation across the lifecycle; implement and validate separately |
| [Dedicated settings page](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/dd2ecfb) | Categories, search, and folding are useful. Migration needs consistent validation, translations, persistence, and host-page access. This update keeps the AstrBot configuration page |

## Retained branch features

Identity anchors, nickname history, independent user profiles and WebUI editing, explicit-deletion profile cleanup, write journals and recovery, retained sources, existing SQLite lock retries, and asynchronous index persistence remain available. Removing the write journal does not fit this branch's recovery chain. This update does not change profile scope or grouping.

## Upgrade and validation

No manual database migration is required. New settings have defaults: streaming `auto`, injection total 6000 characters, and per-entry 1200 characters. `0` disables the corresponding length limit. See [configuration](/en/configuration).

Old BM25 indexes are not rewritten automatically. Run `/anam rebuild-index` to apply the Chinese bigram rules immediately; this calls the Embedding Provider, so consider library size and API costs first.

Regression coverage includes all Chinese retrieval write paths, cache invalidation, scoped consolidation, partial-delete compensation, real SQLite transaction failure and cancellation, migration restoration, stream completeness, JSON/text budgets, and real FAISS non-ASCII read/write operations. Validation also includes the full Python suite, existing frontend tests, and documentation build. Exact results are recorded in the commit message.
