/**
 * Memory Page - 记忆管理页面
 * 负责记忆列表展示、虚拟滚动、筛选和排序
 */

import { normalizeImportance, esc, statusPill, typeLabel, originLabel } from "./utils.js";
import { MEMORY_DELETE_LIMIT } from "./memory-deletion.js";
import { MemoryGroupPicker } from "./memory-group-picker.js";

export class MemoryPage {
  constructor(state, apiClient, peekPanel, deletion) {
    this.state = state;
    this.api = apiClient;
    this.peek = peekPanel;
    this.deletion = deletion;
    this.groupPicker = new MemoryGroupPicker(apiClient, id => this.changeFilter("group", id));
    this.selectedStatuses = new Map();
    if (!(this.state.memory.selectedIds instanceof Set)) {
      this.state.memory.selectedIds = new Set();
    }

    // 虚拟滚动配置
    this.ROW_HEIGHT = 56;
    this.SCROLL_BUFFER = 15;
    this._fetchGeneration = 0;
  }

  /**
   * 获取记忆列表
   */
  async fetch() {
    clearTimeout(this.filterTimer);
    this.loading = true;
    if (this.initialized) this.updateSelectionControls();
    const fetchGeneration = ++this._fetchGeneration;
    const params = {
      page: String(this.state.memory.page),
      page_size: String(this.state.memory.pageSize)
    };

    if (this.state.memory.group) params.group = this.state.memory.group;
    if (this.state.memory.session) params.session_id = this.state.memory.session;
    if (this.state.memory.keyword) params.keyword = this.state.memory.keyword;
    if (this.state.memory.status && this.state.memory.status !== "all") {
      params.status = this.state.memory.status;
    }
    if (this.state.memory.type && this.state.memory.type !== "all") {
      params.type = this.state.memory.type;
    }
    if (this.state.memory.sort) {
      params.sort = this.state.memory.sort;
    }

    try {
      const data = await this.api.get("memories", params);
      if (fetchGeneration !== this._fetchGeneration) return;

      // 删除/筛选后当前页可能超出总页数，回退到最后一页重取，避免显示假空表
      const total = data.total || 0;
      const totalPages = Math.max(1, Math.ceil(total / this.state.memory.pageSize));
      if (this.state.memory.page > totalPages && !(data.items || []).length) {
        this.state.memory.page = totalPages;
        return this.fetch();
      }

      this.state.memory.total = total;
      this.state.memory.hasMore = data.has_more || false;

      this.state.memory.items = (Array.isArray(data.items) ? data.items : []).map(item => ({
        memory_id: item.id,
        doc_id: item.doc_id,
        summary:
          (item.metadata && item.metadata.persona_summary) ||
          item.summary ||
          item.text ||
          item.content ||
          "",
        content: item.text || item.content,
        memory_type: (item.metadata && item.metadata.memory_type) || "GENERAL",
        importance: normalizeImportance(item.metadata && item.metadata.importance),
        status: (item.metadata && item.metadata.status) || "active",
        created_at: (item.metadata && item.metadata.create_time)
          ? new Date(item.metadata.create_time * 1000).toLocaleString()
          : item.created_at || "--",
        updated_at: (item.metadata && item.metadata.updated_at)
          ? new Date(item.metadata.updated_at * 1000).toLocaleString()
          : (item.metadata && item.metadata.create_time)
            ? new Date(item.metadata.create_time * 1000).toLocaleString()
            : item.updated_at || "--",
        last_access: (item.metadata && item.metadata.last_access_time)
          ? new Date(item.metadata.last_access_time * 1000).toLocaleString()
          : "--",
        consolidated_count: (item.metadata && Array.isArray(item.metadata.consolidated_from))
          ? item.metadata.consolidated_from.length
          : 0,
        raw: item,
      }));
      for (const item of this.state.memory.items) {
        if (this.state.memory.selectedIds.has(item.memory_id)) this.selectedStatuses.set(item.memory_id, item.status);
      }

      this.loading = false;
      this.renderVirtual({ resetScroll: true });
      this.updatePagination();
    } catch (e) {
      if (fetchGeneration !== this._fetchGeneration) return;
      this.loading = false;
      this.state.memory.items = [];
      this.state.memory.total = 0;
      this.state.memory.hasMore = false;
      this.showToast(e.message || window.t("misc.fetchMemoriesFail"), true);
      this.renderEmpty();
      this.updatePagination();
    }
  }

  /**
   * 虚拟滚动渲染
   * @param {Object} options - 渲染选项
   * @param {boolean} options.resetScroll - 是否重置滚动位置
   */
  renderVirtual(options = {}) {
    const scrollEl = document.getElementById("memories-scroll");
    if (scrollEl && options.resetScroll) scrollEl.scrollTop = 0;

    if (!this.state.memory.items.length) {
      this.renderEmpty();
      return;
    }

    // 绑定滚动事件（仅绑定一次）
    if (scrollEl && !scrollEl._virtualScrollBound) {
      scrollEl._virtualScrollBound = true;
      scrollEl.addEventListener("scroll", () => {
        window.requestAnimationFrame(() => this.renderVirtualSlice());
      }, { passive: true });
    }

    this.renderVirtualSlice();
  }

  renderVirtualSlice() {
    const tbody = document.getElementById("memories-body");
    const scrollEl = document.getElementById("memories-scroll");
    if (!this.state.memory.items.length) {
      this.renderEmpty();
      return;
    }

    // 行高用实测值：CSS 的 td height 只是最小值，双行摘要单元格实际 ~63px，
    // 硬编码 56px 会让 spacer 逐行漂移、末尾行不可达
    const rowHeight = this._measuredRowHeight || this.ROW_HEIGHT;
    const totalHeight = this.state.memory.items.length * rowHeight;
    const scrollTop = scrollEl ? scrollEl.scrollTop : 0;
    const viewHeight = scrollEl ? scrollEl.clientHeight : 600;
    const start = Math.max(0, Math.floor(scrollTop / rowHeight) - this.SCROLL_BUFFER);
    const end = Math.min(
      this.state.memory.items.length,
      Math.ceil((scrollTop + viewHeight) / rowHeight) + this.SCROLL_BUFFER
    );
    const padTop = start * rowHeight;
    const padBottom = totalHeight - end * rowHeight;
    const spacerRow = (height) => height > 0
      ? '<tr class="virtual-spacer" aria-hidden="true" style="height:' + height + 'px"><td colspan="7" style="height:' + height + 'px;padding:0;border:0"></td></tr>'
      : "";

    let html = spacerRow(padTop);
    for (let i = start; i < end; i++) {
      const item = this.state.memory.items[i];
      const key = "m:" + item.memory_id;
      const imp = item.importance != null ? Number(item.importance).toFixed(1) : "5.0";
      const impNum = Math.min(10, Math.max(0, parseFloat(imp) || 0));
      const impCls = impNum >= 7 ? "high" : impNum >= 4 ? "medium" : "low";

      const selected = this.state.memory.selectedIds.has(item.memory_id);
      html += '<tr data-key="' + key + '" class="' + (selected ? 'is-selected' : '') + '" style="height:' + this.ROW_HEIGHT + 'px">';
      html += '<td class="cell-select"><input type="checkbox" class="memory-select" data-memory-id="' + item.memory_id + '" ' + (selected ? 'checked' : '') + ' aria-label="' + esc(window.t("delete.selectOne", item.memory_id)) + '" /></td>';
      html += '<td class="cell-mono cell-id">' + item.memory_id + '</td>';
      const consBadge = item.consolidated_count > 0
        ? '<span class="type-tag cons-badge" title="' + esc(window.t("table.consolidatedTitle")) + '">' + window.t("table.consolidated", item.consolidated_count) + '</span> '
        : "";
      html += '<td class="cell-summary">' + consBadge + '<div class="memory-summary-text">' + esc(item.summary || "") + '</div><div class="memory-summary-meta"><span class="origin-tag">' + esc(originLabel(item.raw?.metadata?.memory_origin)) + '</span>' + esc(window.t("table.updated", item.updated_at || "--")) + '</div></td>';
      html += '<td class="cell-type"><span class="type-tag">' + esc(typeLabel(item.memory_type)) + '</span></td>';
      html += '<td class="cell-importance"><div class="importance-bar"><div class="importance-bar-track">';
      html += '<div class="importance-bar-fill ' + impCls + '" style="width:' + (impNum * 10) + '%"></div></div>';
      html += '<span style="font-size:12px;color:var(--text-secondary)">' + imp + '</span></div></td>';
      html += '<td class="cell-status">' + statusPill(item.status) + '</td>';
      html += '<td class="cell-created text-secondary" style="font-size:12px">' + esc(item.created_at) + '</td>';
      html += '</tr>';
    }

    tbody.innerHTML = html + spacerRow(padBottom);
    tbody.style.paddingTop = "0";
    tbody.style.paddingBottom = "0";
    this._measureRowHeight(tbody, rowHeight);
    this.updateSelectionControls();
  }

  /**
   * 首个可见行渲染后实测行高，与假设不符时重算一次虚拟窗口
   * @param {HTMLElement} tbody - 表格主体
   * @param {number} assumed - 本次渲染假设的行高
   */
  _measureRowHeight(tbody, assumed) {
    if (this._remeasuring) return;
    if (!tbody || typeof tbody.querySelector !== "function") return;
    const row = tbody.querySelector("tr[data-key]");
    if (!row) return;
    const h = row.offsetHeight;
    if (h > 0 && Math.abs(h - assumed) > 0.5) {
      this._measuredRowHeight = h;
      this._remeasuring = true;
      try {
        this.renderVirtualSlice();
      } finally {
        this._remeasuring = false;
      }
    }
  }

  /**
   * 渲染空表格
   */
  renderEmpty() {
    const tbody = document.getElementById("memories-body");
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">' + window.t("table.noData") + '</td></tr>';
    tbody.style.paddingTop = "0";
    tbody.style.paddingBottom = "0";
    this.updateSelectionControls();
  }

  updateSelectionControls() {
    const selectedIds = this.state.memory.selectedIds;
    const pageIds = this.state.memory.items.map(item => item.memory_id);
    const selectedOnPage = pageIds.filter(id => selectedIds.has(id)).length;
    if (this.tableBody) {
      this.tableBody.setAttribute("aria-busy", String(Boolean(this.loading)));
      this.tableBody.querySelectorAll(".memory-select").forEach(input => { input.disabled = Boolean(this.loading); });
    }
    const selectAll = document.getElementById("mem-select-all");
    if (selectAll) {
      selectAll.checked = pageIds.length > 0 && selectedOnPage === pageIds.length;
      selectAll.indeterminate = selectedOnPage > 0 && selectedOnPage < pageIds.length;
      selectAll.disabled = this.loading || pageIds.length === 0;
    }

    const selection = document.getElementById("mem-selection");
    if (selection) selection.hidden = selectedIds.size === 0;
    const deleteButton = document.getElementById("mem-delete-selected");
    if (deleteButton) deleteButton.disabled = this.loading || selectedIds.size === 0 || Boolean(this.deletion?.busy);
    const deleteLabel = document.getElementById("mem-delete-selected-label");
    if (deleteLabel) deleteLabel.textContent = window.t("delete.selected", selectedIds.size);

    const batchEditButton = document.getElementById("mem-batch-edit");
    if (batchEditButton) batchEditButton.disabled = this.loading || selectedIds.size === 0 || Boolean(this.deletion?.busy);
    const batchEditLabel = document.getElementById("mem-batch-edit-label");
    if (batchEditLabel) batchEditLabel.textContent = window.t("batchEdit.button", selectedIds.size);

    const exportLabel = document.getElementById("mem-export-label");
    if (exportLabel) {
      const byGroup = !selectedIds.size && this.state.memory.group;
      const key = selectedIds.size ? "transfer.exportSelected" : byGroup ? "transfer.exportGroup" : "transfer.exportAll";
      exportLabel.textContent = window.t(key, selectedIds.size);
      document.getElementById("mem-export").title = byGroup
        ? window.t("transfer.exportGroupTitle") : exportLabel.textContent;
    }
  }

  setSelected(item, checked) {
    const { memory_id: id, status } = item;
    if (!checked) {
      this.state.memory.selectedIds.delete(id);
      this.selectedStatuses.delete(id);
      return true;
    }
    if (!this.state.memory.selectedIds.has(id) && this.state.memory.selectedIds.size >= MEMORY_DELETE_LIMIT) return false;
    this.state.memory.selectedIds.add(id);
    this.selectedStatuses.set(id, status);
    return true;
  }

  clearSelection() {
    this.state.memory.selectedIds.clear();
    this.selectedStatuses.clear();
  }

  changeFilter(key, value, delay = 0) {
    if (this.state.memory[key] === value) return;
    this.state.memory[key] = value;
    this.state.memory.page = 1;
    this.clearSelection();
    this.loading = true;
    ++this._fetchGeneration;
    this.renderVirtual();
    clearTimeout(this.filterTimer);
    if (delay) this.filterTimer = setTimeout(() => this.fetch(), delay);
    else this.fetch();
  }

  toggleAllOnPage(checked) {
    if (this.loading) return;
    for (const item of this.state.memory.items) {
      if (this.setSelected(item, checked)) continue;
      this.showToast(window.t("delete.selectionLimit", MEMORY_DELETE_LIMIT), true);
      break;
    }
    this.renderVirtual();
  }

  async deleteSelected() {
    const ids = Array.from(this.state.memory.selectedIds);
    if (!ids.length || this.loading || this.deletion.busy) return;
    const allowRecovery = ids.every(id => this.selectedStatuses.get(id) === "active");
    try {
      await this.deletion.request(ids, allowRecovery, () => {
        for (const id of ids) this.setSelected({ memory_id: id }, false);
        this.renderVirtual();
      });
    } catch (error) {
      this.showToast(error.message || window.t("delete.error"), true);
    }
  }

  async batchEdit() {
    const ids = Array.from(this.state.memory.selectedIds);
    if (!ids.length || this.loading || this.deletion?.busy) return;

    this.peek.open();
    const edit = await this.peek.showBatchEditDialog(ids.length);
    if (!edit) {
      this.peek.close();
      return;
    }

    const button = document.getElementById("mem-batch-edit");
    if (button) button.disabled = true;
    try {
      const payload = { memory_ids: ids, field: edit.field, value: edit.value };
      if (edit.value_scale) payload.value_scale = edit.value_scale;
      const result = await this.api.post(
        "memories/batch-update",
        payload,
        { retries: 0 }
      );
      const updated = Number(result.updated_count || 0);
      const failed = Number(result.failed_count || 0);
      if (failed > 0) {
        this.showToast(window.t("batchEdit.partialFailed", updated, failed), true);
      } else {
        this.showToast(window.t("batchEdit.success", updated));
      }
      this.clearSelection();
      this.peek.close();
      await this.fetch();
    } catch (error) {
      this.peek.close();
      this.showToast(error.message || window.t("batchEdit.error"), true);
      this.updateSelectionControls();
    }
  }

  async exportMemories() {
    const button = document.getElementById("mem-export");
    const format = document.getElementById("mem-transfer-format").value || "json";
    const selectedIds = Array.from(this.state.memory.selectedIds);
    if (button) button.disabled = true;
    try {
      const payload = { format };
      if (selectedIds.length) payload.memory_ids = selectedIds;
      else if (this.state.memory.group) payload.group = this.state.memory.group;
      const result = await this.api.post("memories/export", payload, { retries: 0 });
      const blob = new Blob([result.content || ""], {
        type: result.mime_type || "application/octet-stream"
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename || ("anamnesis-export." + format);
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      this.showToast(window.t("transfer.exportSuccess", Number(result.memory_count || 0)));
    } catch (error) {
      this.showToast(error.message || window.t("transfer.failed"), true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async importFile(file) {
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) {
      this.showToast(window.t("transfer.fileTooLarge"), true);
      return;
    }
    const format = file.name.toLowerCase().endsWith(".csv") ? "csv" : "json";
    const duplicateStrategy = document.getElementById("mem-import-duplicates").value || "skip";
    const content = await file.text();
    const requestPayload = {
      format,
      content,
      duplicate_strategy: duplicateStrategy
    };
    const button = document.getElementById("mem-import");
    if (button) button.disabled = true;
    try {
      const preview = await this.api.post(
        "memories/import",
        { ...requestPayload, dry_run: true },
        { retries: 0 }
      );
      this.peek.open();
      const confirmed = await this.peek.showConfirmDialog(
        window.t("transfer.importPreviewTitle"),
        window.t(
          "transfer.importPreview",
          preview.valid_count || 0,
          preview.planned_import_count || 0,
          preview.duplicate_count || 0,
          preview.invalid_count || 0,
          preview.summary_required_count || 0
        ),
        { destructive: false }
      );
      if (!confirmed) {
        this.peek.close();
        return;
      }
      const result = await this.api.post(
        "memories/import",
        { ...requestPayload, dry_run: false },
        { retries: 0 }
      );
      this.peek.close();
      const failed = Number(result.failed_count || 0);
      this.showToast(
        window.t(
          "transfer.importSuccess",
          Number(result.imported_count || 0),
          Number(result.skipped_duplicate_count || 0),
          failed
        ),
        failed > 0
      );
      await this.fetch();
    } catch (error) {
      this.peek.close();
      this.showToast(error.message || window.t("transfer.failed"), true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  /**
   * 根据 key 获取记忆项
   * @param {string} key - 记忆键（格式：m:id）
   * @returns {Object|undefined} 记忆对象
   */
  getItemByKey(key) {
    return this.state.memory.items.find(i => ("m:" + i.memory_id) === key);
  }

  /**
   * 更新分页信息
   */
  updatePagination() {
    const p = this.state.memory.page;
    const ps = this.state.memory.pageSize;
    const t = this.state.memory.total;
    const tp = Math.max(1, Math.ceil(t / ps));

    document.getElementById("mem-pagination-info").textContent = window.t("common.page", p, tp, t);
    document.getElementById("mem-prev").disabled = p <= 1;
    document.getElementById("mem-next").disabled = !this.state.memory.hasMore;
  }

  /**
   * 初始化事件监听
   */
  initEventListeners() {
    // 表格行点击事件
    const tbody = document.getElementById("memories-body");
    this.tableBody = tbody;
    if (tbody) {
      tbody.addEventListener("click", (e) => {
        if (this.loading) return;
        const checkbox = e.target.closest(".memory-select");
        if (checkbox) {
          const row = checkbox.closest("tr");
          const item = this.getItemByKey(row?.dataset.key);
          if (!item) return;
          if (!this.setSelected(item, checkbox.checked)) {
            checkbox.checked = false;
            this.showToast(window.t("delete.selectionLimit", MEMORY_DELETE_LIMIT), true);
          }
          row.classList.toggle("is-selected", checkbox.checked);
          this.updateSelectionControls();
          return;
        }
        const tr = e.target.closest("tr");
        if (!tr || !tr.dataset.key) return;

        const item = this.getItemByKey(tr.dataset.key);
        if (item) this.peek.renderMemory(item);
      });
    }

    document.getElementById("mem-select-all").addEventListener("change", (e) => {
      this.toggleAllOnPage(e.target.checked);
    });

    document.getElementById("mem-delete-selected").addEventListener("click", () => {
      this.deleteSelected();
    });
    document.getElementById("mem-clear-selection").addEventListener("click", () => {
      this.clearSelection();
      this.renderVirtual();
    });

    document.getElementById("mem-batch-edit").addEventListener("click", () => {
      this.batchEdit();
    });

    document.getElementById("mem-export").addEventListener("click", () => {
      this.exportMemories();
    });

    const importInput = document.getElementById("mem-import-file");
    document.getElementById("mem-import").addEventListener("click", () => {
      importInput.value = "";
      importInput.click();
    });
    importInput.addEventListener("change", () => {
      this.importFile(importInput.files && importInput.files[0]);
    });

    this.groupPicker.init();
    for (const key of ["keyword", "session"]) {
      document.getElementById(`mem-${key}`).addEventListener("input", event => {
        this.changeFilter(key, event.target.value.trim(), 300);
      });
    }
    for (const key of ["status", "type"]) {
      document.getElementById(`mem-${key}`).addEventListener("change", event => {
        this.changeFilter(key, event.target.value);
      });
    }

    // 排序
    document.getElementById("mem-sort").addEventListener("change", () => {
      this.state.memory.sort = document.getElementById("mem-sort").value;
      this.state.memory.page = 1;
      this.fetch();
    });

    // 筛选：每页数量
    document.getElementById("mem-page-size").addEventListener("change", () => {
      this.state.memory.pageSize = parseInt(document.getElementById("mem-page-size").value) || 20;
      this.state.memory.page = 1;
      this.fetch();
    });

    // 分页：上一页
    document.getElementById("mem-prev").addEventListener("click", () => {
      if (!this.loading && this.state.memory.page > 1) {
        this.state.memory.page--;
        this.fetch();
      }
    });

    // 分页：下一页
    document.getElementById("mem-next").addEventListener("click", () => {
      if (!this.loading && this.state.memory.hasMore) {
        this.state.memory.page++;
        this.fetch();
      }
    });
    this.initialized = true;
    this.updateSelectionControls();
  }

  /**
   * 显示 Toast 提示
   * @param {string} message - 提示消息
   * @param {boolean} isError - 是否为错误
   */
  showToast(message, isError = false) {
    if (window.lmShowToast) {
      window.lmShowToast(message, isError);
    }
  }
}
