# 3.5.0: profiles, time retrieval and administration

This release adapts mechanisms from Memorix 1.4.0 within Anamnesis's existing identity, scope, persona and lifecycle boundaries.

Use **3.5.6** for the earlier lock, permission and shutdown fixes plus recovery of failed import stages, recall after legacy-text correction rollback, evidence-based profile scope migration, complete proposal support and session filtering for pending review. The features and defaults below still apply.

Profiles remain disabled by default. Enabled profiles retain explicit self-report and verbatim-evidence validation. Manual edits have independent priority; changed automatic values enter review by default. The directory shows current values, proposals, history and multiple original evidence sources. Version checks protect edits and review actions. Administrator display aliases never merge accounts.

Natural-language date retrieval is enabled by default. Time queries read the database range directly; hybrid queries merge time and semantic candidates. Boundaries are `[start,end)`, using an explicit plugin timezone or AstrBot's session configuration. Candidate, range and overall timeout budgets remain enforced. Graph evidence scoring is optional and disabled by default.

The new Administration page provides:

- Recoverable deletion operations, source snapshots, whole-operation restore and explicit purge. Existing permanent deletion semantics remain intact. The disabled-by-default forget tool can use `forget_mode=recoverable`.
- Natural-language or direct-text correction previews, actor-bound single-use confirmation, source-version checks and rollback. The optional administrator planning tool only proposes; it cannot confirm or execute. Rollback preserves original atom lifetimes.
- TXT/Markdown/JSON/CSV import jobs from pasted text, multiple files or browser-selected folders. Chunk states persist; retries retain completed chunks. Interrupted jobs require explicit retry.
- Fixed-query evaluation with correctly named Hit@K, Precision@K, Recall@K, MRR, expected-empty accuracy, errors and latency. Reports include query/configuration fingerprints. Evaluation does not reinforce memories or apply configuration automatically.
- Optional on-demand events, disabled by default. Events retain member evidence, withdraw stale summaries and rebuild with version checks. Retrieval uses lexical matching and document-hit projection without an independent event embedding pool.
- Versioned index synchronization intents committed with document changes. Conditional completion protects newer revisions; synchronization uses current primary data and retains existing repair/compensation mechanisms.

`user_profile`, `recall_engine`, `episodic_memory` and `memory_administration` expose bounded switches and budgets. Native JSON/CSV imports preserve validated scope/lifecycle data; foreign history and operation identifiers remain import provenance only. `/new` continues to preserve long-term memory.

Source: [Memorix 1.4.0](https://github.com/exynos967/astrbot_plugin_memorix/tree/0ecb96e2db0f1edacece6638e3c26da3dc332e60). Anamnesis remains AGPL-3.0; see NOTICE.
