# Quick Start

Anamnesis is a long-term memory plugin for AstrBot. It maintains a searchable memory store outside the immediate chat window so the bot can remember stable preferences, long-running projects, relationships, group context, and past agreements.

## Install

1. Put the plugin directory under AstrBot's `data/plugins/` directory.
2. Restart or reload AstrBot.
3. AstrBot will install Python dependencies from `requirements.txt`.
4. Open the AstrBot plugin configuration page and select `Anamnesis`.

## Migrating from LivingMemory

Skip this section for a fresh install.

Anamnesis stores data in `data/plugin_data/astrbot_plugin_anamnesis`, separate from the old `astrbot_plugin_livingmemory` directory, so old memories are not picked up automatically. Run the migration commands in this order:

| Step | Command | Notes |
| --- | --- | --- |
| 1 | — | Install and enable Anamnesis; leave the old plugin installed for now |
| 2 | `/anam migrate preview` | Lists the files and sizes that would be migrated; read-only, writes nothing |
| 3 | `/anam migrate exec` | Performs the migration; the old `backups/` directory is skipped to save disk space |
| 4 | `/anam migrate-verify` | Compares row counts for documents, memory_atoms, graph_entries, graph_edges, graph_nodes, and conversations against the old database |
| 5 | — | Uninstall the old plugin in AstrBot only after the reconciliation passes |

Migration reads the old plugin's data directory only and never modifies or deletes its files, so you can disable Anamnesis and fall back to the old plugin at any point before uninstalling it.

To reclaim disk space after migrating, run `/anam vacuum`; it trims the write-operation log and runs SQLite VACUUM on the database.

The old plugin used the `/lmem` command group, the `recall_long_term_memory` and `memorize_long_term_memory` agent tools, and the `<RAG-Faiss-Memory>` injection marker. Anamnesis renames these to `/anam`, `anamnesis_recall_memory` / `anamnesis_memorize_memory`, and `<Anamnesis-Memory>`; persona prompts written against the old names must be updated.

## Required configuration

| Key | Purpose | Recommendation |
| --- | --- | --- |
| `provider_settings.embedding_provider_id` | Generates vectors for semantic retrieval | Leave empty to use AstrBot's default embedding provider |
| `provider_settings.llm_provider_id` | Summarizes conversations and evaluates memory | Leave empty to use the default LLM; a stable reasoning model is recommended |
| `bot_language` | Language for command and status replies | `zh`, `en`, or `ru` |

## Recommended settings

| Scenario | Recommendation |
| --- | --- |
| Private assistant | Enable persona and session filtering to avoid cross-persona memories |
| Long-running group chat | Enable `enable_full_group_capture` to capture context that does not directly mention the bot |
| Agent / tool loop | Keep agent memory tools enabled so the model can recall or write memory when useful |
| Gemini provider | `fake_tool_call` automatically falls back to `extra_user_content` |
| DeepSeek V4 thinking | Use normal `fake_tool_call`; the legacy `fake_tool_call_deepseek_v4` option is only a compatibility alias |

## Open the dashboard

AstrBot `4.24.2` or later is recommended. Open:

`Plugins -> Anamnesis -> Pages -> dashboard`

The dashboard lets you inspect memories, debug recall, manage backups, and browse graph relationships.

## Verify the setup

After several turns of conversation, try:

```text
/anam status
/anam summarize
/anam search your keywords
```

If status and search results appear, the basic pipeline is working.
