import { esc } from "./utils.js";

const labels = {
  title: ["管理工作台", "Administration", "Управление"],
  refresh: ["刷新", "Refresh", "Обновить"],
  confirm: ["确认", "Confirm", "Подтвердить"], cancelConfirm: ["取消", "Cancel", "Отмена"],
  recovery: ["删除与恢复", "Delete and recover", "Удаление и восстановление"],
  correction: ["纠正预览", "Correction preview", "Предпросмотр исправления"],
  imports: ["文件与文本导入", "File and text import", "Импорт файлов и текста"],
  evaluation: ["固定查询评测", "Fixed-query evaluation", "Оценка запросов"],
  events: ["事件记忆", "Event memory", "Память событий"],
  operations: ["操作与任务记录", "Operations and jobs", "Операции и задачи"],
  ids: ["记忆 ID（逗号分隔）", "Memory IDs (comma separated)", "ID памяти (через запятую)"],
  id: ["记忆 ID", "Memory ID", "ID памяти"],
  scope: ["记忆作用域", "Memory scope", "Область памяти"],
  persona: ["人格 ID（可选）", "Persona ID (optional)", "ID персоны (необязательно)"],
  reason: ["操作原因", "Reason", "Причина"],
  delete: ["移入恢复中心", "Move to recovery", "Переместить в восстановление"],
  restore: ["整次恢复", "Restore operation", "Восстановить операцию"],
  purge: ["永久销毁", "Permanently purge", "Удалить навсегда"],
  instruction: ["描述要纠正的内容", "Describe the correction", "Опишите исправление"],
  preview: ["生成修改预览", "Generate preview", "Создать предпросмотр"],
  draft: ["直接填写新正文（可选，跳过模型）", "Direct replacement text (optional)", "Новый текст (необязательно)"],
  execute: ["确认并执行此计划", "Confirm and execute", "Подтвердить и выполнить"],
  rollback: ["回滚修改", "Roll back correction", "Откатить исправление"],
  before: ["修改前", "Before", "До"], after: ["修改后", "After", "После"],
  import: ["提交导入任务", "Submit import job", "Создать задачу импорта"],
  files: ["选择文件", "Choose files", "Выбрать файлы"], directory: ["选择文件夹", "Choose folder", "Выбрать папку"],
  content: ["粘贴文本或导出数据", "Paste text or exported data", "Вставьте текст или данные"],
  evaluate: ["运行评测", "Run evaluation", "Запустить оценку"],
  cases: ["查询与期望 ID（JSON）", "Queries and expected IDs (JSON)", "Запросы и ожидаемые ID (JSON)"],
  createEvent: ["生成事件", "Generate event", "Создать событие"],
  llm: ["使用模型总结（关闭则保留证据摘录）", "Summarize with model (off: evidence excerpts)", "Сводка моделью (выкл.: выдержки)"],
  eventTitle: ["事件标题（规则模式可选）", "Event title (optional for excerpts)", "Название события (необязательно)"],
  rebuild: ["重新生成", "Rebuild", "Пересоздать"],
  rebuildMembers: ["核对事件来源", "Review event sources", "Проверить источники события"],
  sourceIds: ["原事件使用的记忆 ID", "Original memory IDs", "Исходные ID памяти"],
  selectedMembers: ["本次使用的记忆 ID（逗号分隔）", "Memory IDs to use (comma separated)", "ID памяти для использования (через запятую)"],
  rebuildNote: ["请使用同一会话、人格和来源会话中的有效记忆。修改、删除或过期的记忆需要替换或移除。新摘要生成成功前保留原事件。", "Use active memories from the same scope, persona and source session. Replace or remove changed, deleted or expired memories. The original event remains until the new summary is ready.", "Используйте действующие воспоминания из той же области, персоны и исходной сессии. Замените или уберите изменённые, удалённые или истёкшие записи. Исходное событие сохранится до готовности новой сводки."],
  rebuildStale: ["部分来源已变化，请先调整记忆 ID，再重新生成。", "Some sources have changed. Update the memory IDs before rebuilding.", "Некоторые источники изменились. Обновите ID памяти перед пересозданием."],
  sourceSession: ["来源会话", "Source session", "Исходная сессия"],
  eventPersona: ["人格", "Persona", "Персона"],
  notSet: ["未指定", "Not specified", "Не указано"],
  eventMissing: ["事件已变化，请刷新后重试。", "The event has changed. Refresh and try again.", "Событие изменилось. Обновите страницу и повторите."],
  detail: ["查看详情", "Details", "Подробности"],
  cancel: ["取消任务", "Cancel job", "Отменить задачу"],
  retry: ["重试未完成部分", "Retry unfinished work", "Повторить незавершённое"],
  remove: ["清理记录", "Remove record", "Очистить запись"],
  empty: ["暂无记录", "No records", "Нет записей"],
  "kind.delete": ["删除恢复", "Deletion recovery", "Восстановление удалённого"],
  "kind.correction": ["记忆纠正", "Memory correction", "Исправление памяти"],
  "kind.import": ["资料导入", "Import", "Импорт"],
  "kind.evaluation": ["检索评测", "Retrieval evaluation", "Оценка поиска"],
  "kind.episode": ["事件整理", "Event summary", "Сводка события"],
  "kind.projection": ["索引同步", "Index synchronization", "Синхронизация индекса"],
  "status.preparing": ["准备中", "Preparing", "Подготовка"],
  "status.queued": ["排队中", "Queued", "В очереди"],
  "status.running": ["进行中", "Running", "Выполняется"],
  "status.executing": ["提交中", "Committing", "Применяется"],
  "status.needs_review": ["中断待核验", "Interrupted; needs review", "Прервано; нужна проверка"],
  "status.completed": ["已完成", "Completed", "Завершено"],
  "status.partial": ["部分完成", "Partially completed", "Частично завершено"],
  "status.failed": ["失败", "Failed", "Ошибка"],
  "status.interrupted": ["已中断", "Interrupted", "Прервано"],
  "status.cancelled": ["已取消", "Cancelled", "Отменено"],
  "status.preview": ["等待确认", "Awaiting confirmation", "Ожидает подтверждения"],
  "status.stale": ["来源已变化", "Source changed", "Источник изменился"],
  "status.prepared_commit": ["提交后待核验", "Commit needs verification", "Нужна проверка применения"],
  "status.rollback_prepared": ["回滚后待核验", "Rollback needs verification", "Нужна проверка отката"],
  "status.restored": ["已恢复", "Restored", "Восстановлено"],
  "status.purged": ["已永久销毁", "Permanently purged", "Удалено навсегда"],
  "status.rolled_back": ["已回滚", "Rolled back", "Откачено"],
  projection: ["待同步索引", "Pending index synchronization", "Ожидающая синхронизация"],
  repair: ["同步一批索引", "Synchronize a batch", "Синхронизировать пакет"],
  confirmPurge: ["将永久删除本次操作仍持有的记忆及恢复快照。", "Permanently delete owned memories and recovery snapshots.", "Навсегда удалить воспоминания и снимки этой операции."],
  confirmExecute: ["请核对修改前后正文与作用域，确认后执行。", "Review old/new text and scope before executing.", "Проверьте старый/новый текст и область перед выполнением."],
  note: ["事件按需生成；过期或来源变化后停止展示。导入和生成任务逐个执行，重启后可手动重试。", "Events are generated on demand and withdrawn when evidence changes. Jobs run sequentially; interrupted jobs can be retried.", "События создаются по запросу и скрываются при изменении доказательств. Прерванные задачи можно повторить."],
};
export function adminText(key) {
  const index = { zh: 0, en: 1, ru: 2 }[window.getLanguage?.() || "zh"] || 0;
  return labels[key]?.[index] || key;
}
export function parseMemoryIds(text) {
  const values = String(text).split(/[,，\s]+/).filter(Boolean).map(Number);
  if (!values.length || values.some(value => !Number.isSafeInteger(value) || value <= 0) || values.length > 200) throw new Error(adminText("ids"));
  return [...new Set(values)];
}
const date = value => new Date(value * 1000).toLocaleString();

export class AdministrationPage {
  constructor(state, api, peek) { this.state = state; this.api = api; this.peek = peek; this.records = []; this.episodes = []; this.sequence = 0; this.busy = false; }
  input(id, key, type = "text") { return `<label>${esc(adminText(key))}<input class="input" id="admin-${id}" type="${type}" /></label>`; }
  button(id, key) { return `<button type="button" class="btn btn-secondary btn-sm" id="admin-${id}">${esc(adminText(key))}</button>`; }
  initEventListeners() {
    const root = document.getElementById("administration-content"); if (!root) return;
    root.innerHTML = `<div class="admin-grid">
      <section class="admin-card"><h2>${esc(adminText("recovery"))}</h2>${this.input("delete-ids", "ids")}${this.input("reason", "reason")}${this.button("delete", "delete")}</section>
      <section class="admin-card"><h2>${esc(adminText("correction"))}</h2>${this.input("memory-id", "id", "number")}<label>${esc(adminText("instruction"))}<textarea class="input" id="admin-instruction"></textarea></label><label>${esc(adminText("draft"))}<textarea class="input" id="admin-draft"></textarea></label>${this.button("preview", "preview")}</section>
      <section class="admin-card"><h2>${esc(adminText("imports"))}</h2>${this.input("scope", "scope")}${this.input("persona", "persona")}<label>TXT / Markdown / JSON / CSV<select class="input" id="admin-format"><option>txt</option><option>md</option><option>json</option><option>csv</option></select></label><label><select class="input" id="admin-knowledge-type"><option value="reference">Reference</option><option value="conversation">Conversation</option><option value="notes">Notes</option></select></label><label>${esc(adminText("content"))}<textarea class="input" id="admin-import-content"></textarea></label><label>${esc(adminText("files"))}<input id="admin-files" type="file" multiple accept=".txt,.md,.json,.csv" /></label><label>${esc(adminText("directory"))}<input id="admin-directory" type="file" multiple webkitdirectory /></label>${this.button("import", "import")}</section>
      <section class="admin-card"><h2>${esc(adminText("evaluation"))}</h2>${this.input("eval-scope", "scope")}${this.input("eval-persona", "persona")}<label>${esc(adminText("cases"))}<textarea class="input" id="admin-cases" placeholder='[{"query":"上周讨论了什么","expected_ids":[123]}]'></textarea></label>${this.button("evaluate", "evaluate")}</section>
      <section class="admin-card"><h2>${esc(adminText("events"))}</h2>${this.input("event-ids", "ids")}${this.input("event-title", "eventTitle")}<label><input type="checkbox" id="admin-event-llm" checked /> ${esc(adminText("llm"))}</label>${this.button("event", "createEvent")}<div id="admin-episodes"></div></section>
      <section class="admin-card"><h2>${esc(adminText("projection"))}</h2><div id="admin-projection-count"></div>${this.button("repair", "repair")}</section></div>
      <p>${esc(adminText("note"))}</p><div id="admin-feedback" role="status"></div><section class="admin-card"><h2>${esc(adminText("operations"))}</h2><div id="admin-records"></div></section><section id="admin-detail" class="admin-card" hidden></section>`;
    const on = (id, fn) => document.getElementById(id)?.addEventListener("click", () => this.run(fn));
    on("administration-refresh", () => this.fetch());
    on("admin-delete", async () => {
      const ids = parseMemoryIds(this.value("delete-ids"));
      if (!await this.confirm(adminText("delete"), `${ids.join(", ")}\n${this.value("reason")}`)) return;
      await this.api.post("operations/delete", { memory_ids: ids, reason: this.value("reason") }); await this.fetch();
    });
    on("admin-preview", async () => this.showPreview(await this.api.post("corrections/preview", { memory_id: Number(this.value("memory-id")), instruction: this.value("instruction"), new_content: this.value("draft") || undefined })));
    on("admin-import", () => this.submitImports());
    on("admin-evaluate", async () => { await this.api.post("jobs/create", { kind: "evaluation", payload: { cases: JSON.parse(this.value("cases")), session_id: this.value("eval-scope") || null, persona_id: this.value("eval-persona") || null } }); await this.fetch(); });
    on("admin-event", async () => { await this.api.post("jobs/create", { kind: "episode", payload: { memory_ids: parseMemoryIds(this.value("event-ids")), title: this.value("event-title"), use_llm: document.getElementById("admin-event-llm").checked } }); await this.fetch(); });
    on("admin-repair", async () => { await this.api.post("projection/repair", {}); await this.fetch(); });
    root.addEventListener("click", event => {
      const button = event.target.closest("[data-admin-action]"); if (!button) return;
      this.run(() => this.action(button.dataset.adminAction, button.dataset.operationId));
    });
  }
  value(id) { return document.getElementById("admin-" + id)?.value.trim() || ""; }
  async confirm(title, message) {
    return new Promise(resolve => {
      const dialog = document.createElement("dialog"); dialog.className = "admin-confirm-dialog";
      dialog.innerHTML = `<h2>${esc(title)}</h2><p class="admin-prose">${esc(message)}</p><div class="admin-actions"><button id="admin-confirm-cancel-btn" class="btn btn-secondary" type="button">${esc(adminText("cancelConfirm"))}</button><button id="admin-confirm-ok-btn" class="btn btn-primary" type="button">${esc(adminText("confirm"))}</button></div>`;
      document.body.appendChild(dialog);
      let finished = false;
      const finish = result => { if (finished) return; finished = true; dialog.close(); dialog.remove(); resolve(result); };
      dialog.querySelector("#admin-confirm-cancel-btn").addEventListener("click", () => finish(false));
      dialog.querySelector("#admin-confirm-ok-btn").addEventListener("click", () => finish(true));
      dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
      dialog.addEventListener("close", () => finish(false));
      dialog.showModal(); dialog.querySelector("#admin-confirm-cancel-btn").focus();
    });
  }
  async editEpisodeMembers(episode) {
    return new Promise(resolve => {
      const dialog = document.createElement("dialog"); dialog.className = "admin-confirm-dialog admin-card";
      dialog.innerHTML = `<form>
        <h2>${esc(adminText("rebuildMembers"))}</h2>
        ${episode.stale ? `<p class="admin-prose">${esc(adminText("rebuildStale"))}</p>` : ""}
        <p class="admin-prose">${esc(adminText("rebuildNote"))}</p>
        <p class="admin-prose">${esc(adminText("scope"))}: ${esc(episode.session_id || adminText("notSet"))}<br>${esc(adminText("eventPersona"))}: ${esc(episode.persona_id || adminText("notSet"))}<br>${esc(adminText("sourceSession"))}: ${esc(episode.source_session_id || adminText("notSet"))}</p>
        <p class="admin-prose">${esc(adminText("sourceIds"))}: ${esc(episode.memory_ids.join(", "))}</p>
        <label>${esc(adminText("selectedMembers"))}<input class="input" id="admin-rebuild-ids" value="${esc(episode.memory_ids.join(", "))}" /></label>
        <label>${esc(adminText("eventTitle"))}<input class="input" id="admin-rebuild-title" value="${esc(episode.stale ? "" : episode.title)}" /></label>
        <label><input type="checkbox" id="admin-rebuild-llm" checked /> ${esc(adminText("llm"))}</label>
        <p id="admin-rebuild-error" role="alert" class="admin-prose"></p>
        <div class="admin-actions"><button id="admin-rebuild-cancel-btn" class="btn btn-secondary" type="button">${esc(adminText("cancelConfirm"))}</button><button id="admin-rebuild-submit-btn" class="btn btn-primary" type="submit">${esc(adminText("rebuild"))}</button></div>
      </form>`;
      document.body.appendChild(dialog);
      let finished = false;
      const finish = result => { if (finished) return; finished = true; dialog.close(); dialog.remove(); resolve(result); };
      const ids = dialog.querySelector("#admin-rebuild-ids");
      dialog.querySelector("#admin-rebuild-cancel-btn").addEventListener("click", () => finish(null));
      dialog.querySelector("form").addEventListener("submit", event => {
        event.preventDefault();
        try {
          finish({ memory_ids: parseMemoryIds(ids.value), title: dialog.querySelector("#admin-rebuild-title").value.trim(), use_llm: dialog.querySelector("#admin-rebuild-llm").checked });
        } catch (error) { dialog.querySelector("#admin-rebuild-error").textContent = error.message; ids.focus(); }
      });
      dialog.addEventListener("cancel", event => { event.preventDefault(); finish(null); });
      dialog.addEventListener("close", () => finish(null));
      dialog.showModal(); ids.focus();
    });
  }
  async run(fn) {
    if (this.busy) return; this.busy = true;
    try { await fn(); document.getElementById("admin-feedback").textContent = ""; }
    catch (error) { document.getElementById("admin-feedback").textContent = error.message; }
    finally { this.busy = false; }
  }
  async fetch() {
    const seq = ++this.sequence;
    const [records, episodes, projection] = await Promise.all([this.api.get("operations"), this.api.get("episodes"), this.api.get("projection")]);
    if (seq !== this.sequence) return;
    this.records = records; this.episodes = episodes; this.render();
    document.getElementById("admin-projection-count").textContent = `${projection.length}${projection.length >= 100 ? "+" : ""}`;
    clearTimeout(this.refreshTimer);
    if (this.records.some(record => ["preparing", "queued", "running", "executing"].includes(record.status))) this.refreshTimer = setTimeout(() => {
      if (this.state.page === "administration" && !this.busy) this.fetch().catch(error => { document.getElementById("admin-feedback").textContent = error.message; });
    }, 5000);
  }
  render() {
    const action = (key, id) => `<button type="button" class="btn btn-secondary btn-sm" data-admin-action="${key}" data-operation-id="${esc(id)}">${esc(adminText(key))}</button>`;
    document.getElementById("admin-records").innerHTML = this.records.map(record => {
      const job = ["import", "evaluation", "episode", "projection"].includes(record.kind);
      let buttons = action("detail", record.id);
      if (record.kind === "delete" && !record.expired && ["completed", "partial", "running"].includes(record.status)) buttons += action("restore", record.id) + action("purge", record.id);
      if (record.kind === "delete" && record.expired && !["purged", "restored"].includes(record.status)) buttons += action("purge", record.id);
      if (record.kind === "correction" && record.status === "preview" && !record.expired) buttons += action("preview", record.id);
      if (record.kind === "correction" && ["completed", "prepared_commit", "rollback_prepared", "needs_review"].includes(record.status) && !record.expired) buttons += action("rollback", record.id);
      if (job && ["queued", "running"].includes(record.status)) buttons += action("cancel", record.id);
      if (job && ["partial", "failed", "interrupted", "cancelled"].includes(record.status) && !record.expired) buttons += action("retry", record.id);
      if (job && !["preparing", "queued", "running"].includes(record.status) || ["purged", "restored", "rolled_back", "failed", "stale", "preview"].includes(record.status) || record.kind === "correction" && record.status === "completed" && record.expired) buttons += action("remove", record.id);
      return `<article class="admin-record"><div><strong>${esc(adminText("kind." + record.kind))}</strong> · ${esc(adminText("status." + record.status))} · ${esc(date(record.created_at))}<small>${esc(record.id)}</small></div><div class="admin-actions">${buttons}</div></article>`;
    }).join("") || `<p>${esc(adminText("empty"))}</p>`;
    document.getElementById("admin-episodes").innerHTML = this.episodes.map(episode => `<details><summary>${esc(episode.title)}</summary><p class="admin-prose">${esc(episode.summary)}</p><small>${esc(episode.source_session_id || episode.session_id || "")} · ${esc(episode.memory_ids.join(", "))}</small>${action("rebuild", episode.id)}</details>`).join("") || `<p>${esc(adminText("empty"))}</p>`;
  }
  async submitImports() {
    const scope = this.value("scope"), persona = this.value("persona") || null;
    const files = [...(document.getElementById("admin-files").files || []), ...(document.getElementById("admin-directory").files || [])];
    const inputs = [];
    if (this.value("import-content")) inputs.push({ content: this.value("import-content"), format: this.value("format"), filename: "pasted-text" });
    if (files.length > 100) throw new Error("At most 100 files per submission");
    for (const file of files) {
      const format = file.name.split(".").pop().toLowerCase(); if (!["txt", "md", "json", "csv"].includes(format)) continue;
      if (file.size > 5 * 1024 * 1024) throw new Error(`${file.name}: 5 MiB`);
      inputs.push({ content: await file.text(), format, filename: file.webkitRelativePath || file.name });
    }
    if (!inputs.length) throw new Error(adminText("content"));
    for (const input of inputs) await this.api.post("jobs/create", { kind: "import", payload: { ...input, session_id: scope || null, persona_id: persona, knowledge_type: this.value("knowledge-type") } });
    await this.fetch();
  }
  async showPreview(plan) {
    const element = document.getElementById("admin-detail"); element.hidden = false;
    element.innerHTML = `<h2>${esc(adminText("correction"))}</h2><p>${esc(plan.session_id || plan.payload?.before?.metadata?.session_id || "")} · ${esc(plan.persona_id || plan.payload?.before?.metadata?.persona_id || "")}</p><div class="admin-compare"><div><h3>${esc(adminText("before"))}</h3><p class="admin-prose">${esc(plan.old_content)}</p></div><div><h3>${esc(adminText("after"))}</h3><p class="admin-prose">${esc(plan.new_content)}</p></div></div>${this.button("execute-plan", "execute")}`;
    const id = plan.operation_id || plan.id;
    document.getElementById("admin-execute-plan").addEventListener("click", () => this.run(async () => {
      if (!await this.confirm(adminText("execute"), adminText("confirmExecute"))) return;
      const confirmation = await this.api.post("corrections/confirm", { operation_id: id });
      await this.api.post("corrections/execute", confirmation); element.hidden = true; await this.fetch();
    }));
    element.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }
  async action(action, id) {
    if (action === "preview") return this.showPreview(await this.api.get("operations/detail", { operation_id: id }));
    if (action === "detail") {
      const data = await this.api.get("operations/detail", { operation_id: id });
      const element = document.getElementById("admin-detail"); element.hidden = false;
      let body;
      if (data.kind === "evaluation" && data.result.summary) {
        body = `<dl class="admin-metrics">${Object.entries(data.result.summary).map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value === null ? "—" : value)}</dd></div>`).join("")}</dl>` + data.result.cases.map(item => `<details><summary>${esc(item.query)}</summary><p>Expected: ${esc(item.expected_ids.join(", "))}<br>Actual: ${esc(item.actual_ids.join(", "))}</p>${item.error ? `<p>${esc(item.error)}</p>` : ""}</details>`).join("");
      } else if (data.kind === "import") {
        body = `<p>${esc(data.payload.filename || "pasted-text")}</p>` + (data.chunks || []).map(item => `<details><summary>${esc(item.index)} · ${esc(adminText("status." + item.status))}</summary><pre class="admin-prose">${esc(JSON.stringify(item.result, null, 2))}</pre></details>`).join("");
      } else {
        body = `<pre class="admin-prose">${esc(JSON.stringify(data.result, null, 2))}</pre>`;
      }
      element.innerHTML = `<h2>${esc(adminText("detail"))}</h2><p>${esc(adminText("kind." + data.kind))} · ${esc(adminText("status." + data.status))} · ${esc(date(data.created_at))}</p>${body}`; return;
    }
    if (action === "rebuild") {
      const episode = this.episodes.find(item => item.id === id);
      if (!episode) throw new Error(adminText("eventMissing"));
      const expectedRevision = episode.revision;
      const draft = await this.editEpisodeMembers(episode);
      if (!draft) return;
      await this.api.post("jobs/create", { kind: "episode", payload: { ...draft, episode_id: id, expected_revision: expectedRevision } });
    } else if (["cancel", "retry"].includes(action) || action === "remove" && ["import", "evaluation", "episode", "projection"].includes(this.records.find(item => item.id === id)?.kind)) {
      await this.api.post("jobs/action", { operation_id: id, action });
    } else {
      if (action === "purge" && !await this.confirm(adminText("purge"), adminText("confirmPurge"))) return;
      if (action === "rollback" && !await this.confirm(adminText("rollback"), adminText("confirmExecute"))) return;
      await this.api.post(action === "rollback" ? "corrections/rollback" : "operations/" + action, { operation_id: id });
    }
    await this.fetch();
  }
}
