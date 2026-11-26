const ACTIVE = new Set(["queued", "running"]);
const TERMINAL = new Set(["completed", "partial", "failed", "interrupted", "restored", "purged"]);
const POLL_INTERVAL = 1000;
const MAX_POLL_FAILURES = 3;

export const MEMORY_DELETE_LIMIT = 2000;

export class MemoryDeletion {
  constructor(api, { storageKey, onBusyChange, onSettled, onHistory }) {
    this.api = api;
    this.storageKey = storageKey;
    this.onBusyChange = onBusyChange;
    this.onSettled = onSettled;
    this.onHistory = onHistory;
    this.failures = 0;
  }

  init() {
    this.dialog = document.getElementById("mem-delete-dialog");
    this.panel = document.getElementById("mem-delete-progress");
    this.status = document.getElementById("mem-delete-status");
    this.progress = document.getElementById("mem-delete-meter");
    this.undo = document.getElementById("mem-delete-undo");
    this.retry = document.getElementById("mem-delete-retry");
    this.dismiss = document.getElementById("mem-delete-dismiss");
    this.details = document.getElementById("mem-delete-details");
    this.undo.addEventListener("click", () => this.restore());
    this.retry.addEventListener("click", () => { this.failures = 0; this.poll(); });
    this.dismiss.addEventListener("click", () => this.reset());
    document.getElementById("mem-delete-history").addEventListener("click", this.onHistory);

    try { this.operationId = sessionStorage.getItem(this.storageKey); } catch { /* The host may restrict iframe storage. */ }
    if (this.operationId) this.poll();
  }

  get busy() {
    return Boolean(this.submitting || this.restoring ||
      (this.operationId && (!this.operation || ACTIVE.has(this.operation.status))));
  }

  notify() {
    this.render();
    if (this.busy !== this.lastBusy) {
      this.lastBusy = this.busy;
      this.onBusyChange?.();
    }
  }

  async request(ids, allowRecovery, onAccepted) {
    if (this.busy || this.dialog.open) {
      throw new Error(window.t("delete.jobBusy"));
    }
    const mode = await this.confirm(ids.length, allowRecovery);
    if (!mode) return false;
    await this.submit(ids, mode, { onAccepted });
    return true;
  }

  confirm(count, allowRecovery) {
    const checkbox = document.getElementById("mem-delete-permanent");
    const message = document.getElementById("mem-delete-confirm-msg");
    const confirmButton = document.getElementById("mem-delete-confirm");
    document.getElementById("mem-delete-mode").hidden = !allowRecovery;
    checkbox.checked = !allowRecovery;
    const render = () => {
      message.textContent = window.t(checkbox.checked ? "delete.permanentConfirm" : "delete.recoveryConfirm", count);
      confirmButton.textContent = window.t(checkbox.checked ? "delete.permanentAction" : "delete.recoveryAction");
    };
    checkbox.onchange = render;
    render();
    this.dialog.returnValue = "";
    return new Promise(resolve => {
      this.dialog.addEventListener("close", () => {
        const mode = checkbox.checked ? "permanent" : "recoverable";
        resolve(this.dialog.returnValue === "confirm" ? mode : null);
      }, { once: true });
      this.dialog.showModal();
    });
  }

  async submit(ids, mode, { reason, onAccepted } = {}) {
    if (this.busy) throw new Error(window.t("delete.jobBusy"));
    this.submitting = true;
    this.startError = "";
    this.notify();
    try {
      const payload = { memory_ids: ids, mode };
      if (reason) payload.reason = reason;
      const result = await this.api.post("operations/delete/start", payload, { retries: 0 });
      this.assertStatus(result);
      this.setOperation(result.operation_id, result);
    } catch (error) {
      this.startError = error.message || window.t("delete.error");
      throw error;
    } finally {
      this.submitting = false;
      this.notify();
    }
    this.advance();
    onAccepted?.();
  }

  assertStatus(result, id = result.operation_id) {
    if (!id || result.operation_id !== id || (!ACTIVE.has(result.status) && !TERMINAL.has(result.status))) {
      throw new Error(window.t("delete.statusError"));
    }
  }

  advance() {
    if (ACTIVE.has(this.operation.status)) this.schedule(POLL_INTERVAL);
    else this.onSettled?.(this.operation);
  }

  schedule(delay) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.poll(), delay);
  }

  track(id) {
    if (this.operationId !== id) {
      if (this.busy || this.polling) return;
      this.setOperation(id);
    }
    this.poll();
  }

  setOperation(id, operation = null) {
    this.operationId = id;
    this.operation = operation;
    this.failures = 0;
    this.startError = this.pollError = this.restoreError = "";
    this.restoreResult = null;
    this.details.open = false;
    try { sessionStorage.setItem(this.storageKey, id); } catch { /* Progress remains available in this page. */ }
  }

  async poll() {
    if (!this.operationId || this.polling) return;
    clearTimeout(this.timer);
    this.polling = true;
    this.notify();
    try {
      const result = await this.api.get("operations/delete/status", { operation_id: this.operationId });
      this.assertStatus(result, this.operationId);
      this.operation = result;
      this.pollError = "";
      this.failures = 0;
    } catch (error) {
      this.pollError = error.message || window.t("delete.statusError");
      ++this.failures;
    } finally {
      this.polling = false;
      this.notify();
    }
    if (!this.pollError) this.advance();
    else if (this.failures < MAX_POLL_FAILURES) this.schedule(POLL_INTERVAL * (1 + this.failures));
  }

  async restore() {
    if (this.busy || !this.operation?.can_restore) return;
    this.restoring = true;
    this.restoreError = "";
    this.notify();
    try {
      const result = await this.api.post("operations/restore", { operation_id: this.operationId }, { retries: 0 });
      this.restoreResult = result;
      if (!result.conflicts?.length) {
        this.operation = { ...this.operation, status: "restored", can_restore: false };
      }
      this.onSettled?.(this.operation);
    } catch (error) {
      this.restoreError = error.message || window.t("delete.restoreError");
    } finally {
      this.restoring = false;
      this.notify();
    }
  }

  reset() {
    clearTimeout(this.timer);
    this.operationId = this.operation = this.restoreResult = null;
    this.startError = this.pollError = this.restoreError = "";
    this.failures = 0;
    try { sessionStorage.removeItem(this.storageKey); } catch { /* The host may restrict iframe storage. */ }
    this.notify();
  }

  render() {
    if (!this.panel) return;
    const t = window.t;
    const operation = this.operation;
    const failure = this.startError || this.pollError || this.restoreError || operation?.error;
    this.panel.hidden = !this.submitting && !this.operationId && !failure;
    const active = operation && ACTIVE.has(operation.status);
    const total = Number(operation?.total) || 0;
    const processed = Number(operation?.processed_count) || 0;
    const deleted = Number(operation?.deleted_count) || 0;
    const failed = Number(operation?.failed_count) || 0;

    let message = "";
    if (this.submitting) message = t("delete.submitting");
    else if (this.restoring) message = t("delete.restoring");
    else if (this.startError) message = t("delete.submitFailed", this.startError);
    else if (this.pollError) message = t("delete.statusFailed", this.pollError);
    else if (this.restoreError) message = t("delete.restoreFailed", this.restoreError);
    else if (this.restoreResult) message = t("delete.restoreSummary", this.restoreResult.ids?.length || 0, this.restoreResult.conflicts?.length || 0);
    else if (operation?.status === "restored") message = t("delete.wasRestored");
    else if (operation?.status === "purged") message = t("delete.wasPurged");
    else if (operation) {
      const counts = t(operation.mode === "recoverable" ? "delete.recoveryCount" : "delete.permanentCount", deleted, failed);
      message = t(`delete.job.${operation.status}`, processed, total, counts);
      if (operation.error) message += ` · ${operation.error}`;
    } else if (this.operationId) message = t("common.loading");
    if (this.status.textContent !== message) this.status.textContent = message;

    this.progress.hidden = !this.submitting && !active;
    if (!this.submitting && total > 0) {
      this.progress.max = total;
      this.progress.value = processed;
    } else this.progress.removeAttribute("value");
    this.undo.hidden = !operation?.can_restore || active || Boolean(this.startError);
    this.undo.disabled = this.restoring;
    this.retry.hidden = !this.pollError;
    this.retry.disabled = this.polling;
    this.dismiss.hidden = this.polling || this.submitting || this.restoring ||
      (this.busy && this.failures < MAX_POLL_FAILURES);
    const failedIds = operation?.failed_ids || [];
    this.details.hidden = active || !failedIds.length;
    this.details.querySelector("p").textContent = failedIds.join(", ");
  }
}
