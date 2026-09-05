# Commands

Anamnesis commands use the `/anam` prefix (alias `/amem`).

| Command | Description |
| --- | --- |
| `/anam status` | Show memory statistics and, while maintenance is active or abnormal, its state, progress, and message |
| `/anam search <query> [k]` | Search long-term memories; `k` defaults to 5 |
| `/anam forget <id>` | Delete a specific memory |
| `/anam rebuild-index` | Rebuild document indexes |
| `/anam rebuild-graph` | Rebuild and compact graph indexes into memory-level vectors |
| `/anam webui` | Show WebUI entry information |
| `/anam summarize [message_count]` | Summarize now; with a count, re-summarize the most recent N messages |
| `/anam reset` | Reset current session memory context |
| `/anam cleanup [preview\|exec]` | Clean old memory injection fragments from message history |
| `/anam migrate [preview\|exec]` | Read-only migration of all data from the old `astrbot_plugin_livingmemory` data directory; `preview` only reports, `exec` performs the migration |
| `/anam migrate-verify` | Reconcile after migration by comparing per-table row counts with the old database |
| `/anam vacuum` | Trim the write-operation log and run SQLite VACUUM to reclaim disk space |
| `/anam help` | Show help |

## Migration and disk reclamation

The full migration procedure from the old LivingMemory plugin is described in the [quick start](/en/guide/getting-started).

| Command | Behavior |
| --- | --- |
| `/anam migrate preview` | Reports the files and sizes that would be migrated and writes nothing |
| `/anam migrate exec` | Performs the migration and skips the old `backups/` directory to save disk space |
| `/anam migrate-verify` | Compares row counts for documents / memory_atoms / graph_entries / graph_edges / graph_nodes / conversations |
| `/anam vacuum` | Trims the write-operation log and runs SQLite VACUUM to reclaim database file space |

Migration reads the old data directory only and never modifies or deletes the old plugin's data. Uninstall the old plugin after `/anam migrate-verify` passes.

## Index maintenance states

Startup consistency checks and automatic repairs run in the background. Use `/anam status` to inspect non-idle states:

| State | Meaning | Recommended action |
| --- | --- | --- |
| `checking` | Document, vector, BM25, and graph consistency is being checked | No action is required; the plugin remains available |
| `rebuilding` | Indexes are being rebuilt in batches and progress is available | Keep providers available and do not start duplicate rebuilds |
| `partial` | Rebuild finished with tolerated failures or the final check still found differences | Check provider limits and logs, then run `/anam rebuild-index` again if needed |
| `failed` | Background maintenance failed; the live index remains in use when a shadow generation did not switch | Fix the reported environment problem and rerun the rebuild |
| `cancelled` | Maintenance was cancelled during plugin stop or reload | The next startup checks again, or rebuild manually after the runtime is stable |

`idle` and `ready` do not add a maintenance section to the status reply.

## Troubleshooting

| Symptom | Try this |
| --- | --- |
| Recently discussed content is not searchable | Run `/anam summarize` to ensure it has been written into long-term memory |
| A summary is empty or misses details | Use `/anam summarize 20` for the latest 20 messages, or re-summarize retained source from memory details |
| Memories leak across personas | Check `filtering_settings.use_persona_filtering` |
| Group context is incomplete | Check `session_manager.enable_full_group_capture` |
| Search indexes look inconsistent | Run `/anam rebuild-index`; for graph issues, run `/anam rebuild-graph` |
| Old recall degrades after changing the embedding model | Wait for the provider-fingerprint check to trigger a full vector rebuild and monitor `/anam status` |
| An error names `faiss-cpu 1.14.2` or a core dependency conflict | AstrBot 4.27.1 and the plugin require `faiss-cpu>=1.14.3`; update or repair the Desktop embedded environment and lock instead of overriding a core dependency |
| FAISS reports `Illegal instruction` | The plugin first probes generic instruction mode; if that also fails, install a wheel compatible with the current CPU and Python or move to a compatible runtime |
| FAISS reports an undefined `SuperKMeans` | Python wrappers and the binary extension are mismatched; cleanly reinstall a compatible build in the exact Python environment used by AstrBot |
