/**
 * User Profile Page - 长期用户档案管理
 *
 * Profiles are deliberately kept separate from the memory table.  A profile
 * row is scoped by the exact profile_scope returned by the server; deleting a
 * row therefore cannot accidentally clear another user's facts.
 */

import { debounce, esc } from "./utils.js";

export class ProfilePage {
  constructor(state, apiClient, peekPanel) {
    this.state = state;
    this.api = apiClient;
    this.peek = peekPanel;
    if (!this.state.profile) {
      this.state.profile = {
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        scope: "",
        sessionId: "",
        key: "",
        scopes: [],
        sessionIds: [],
        profileEnabled: null,
        scopeMode: "",
        hasMore: false,
      };
    }
    this._fetchGeneration = 0;
    this._bound = false;
    this._deleting = false;
  }

  t(key, ...args) {
    return typeof window !== "undefined" && typeof window.t === "function"
      ? window.t(key, ...args)
      : key;
  }

  /** Load one page of profile facts using the server's offset contract. */
  async fetch() {
    const generation = ++this._fetchGeneration;
    const profile = this.state.profile;
    const params = {
      limit: String(profile.limit),
      offset: String(profile.offset),
    };
    if (profile.scope) params.scope = profile.scope;
    if (profile.sessionId) params.session_id = profile.sessionId;
    if (profile.key) params.key = profile.key;

    const refresh = document.getElementById("profile-refresh");
    if (refresh) refresh.disabled = true;
    try {
      const data = await this.api.get("profiles", params);
      if (generation !== this._fetchGeneration) return;

      const items = Array.isArray(data && data.items) ? data.items : [];
      profile.items = items.map((item) => ({
        profile_scope: item.profile_scope || item.scope || "",
        profile_key: item.profile_key || item.key || "",
        category: item.category || "--",
        value: item.value == null ? "" : String(item.value),
        confidence: item.confidence,
        source_memory_id: item.source_memory_id,
        source_session_id: item.source_session_id || "",
        source_group_id: item.source_group_id || "",
        source_group_name: item.source_group_name || "",
        source_sender_name: item.source_sender_name || "",
        updated_at: item.updated_at,
        expires_at: item.expires_at,
      }));
      profile.total = Number.isFinite(Number(data && data.total))
        ? Number(data.total)
        : profile.items.length;
      const responseLimit = Number(data && data.limit);
      const responseOffset = Number(data && data.offset);
      if (Number.isFinite(responseLimit) && responseLimit >= 1) profile.limit = responseLimit;
      if (Number.isFinite(responseOffset) && responseOffset >= 0) profile.offset = responseOffset;
      profile.hasMore = profile.offset + profile.items.length < profile.total;
      profile.scopes = this._uniqueStrings(data && data.scopes);
      profile.sessionIds = this._uniqueStrings(data && data.session_ids);
      profile.profileEnabled = typeof (data && data.profile_enabled) === "boolean"
        ? data.profile_enabled
        : null;
      profile.scopeMode = data && data.scope_mode ? String(data.scope_mode) : "";

      this.render();
    } catch (error) {
      if (generation !== this._fetchGeneration) return;
      profile.items = [];
      profile.total = 0;
      profile.hasMore = false;
      this._renderStatus();
      this._renderEmpty(this.t("profile.fetchFailed"));
      this._showToast(error && error.message ? error.message : this.t("profile.fetchFailed"), true);
    } finally {
      if (generation === this._fetchGeneration && refresh) refresh.disabled = false;
    }
  }

  _uniqueStrings(values) {
    if (!Array.isArray(values)) return [];
    return [...new Set(values
      .filter((value) => value != null && String(value) !== "")
      .map((value) => String(value)))].sort((a, b) => a.localeCompare(b));
  }

  _renderStatus() {
    const status = document.getElementById("profile-status");
    if (!status) return;
    const profile = this.state.profile;
    status.className = "profile-status";
    if (profile.profileEnabled === false) {
      status.classList.add("is-disabled");
      status.textContent = this.t("profile.disabled");
    } else if (profile.profileEnabled === true) {
      status.classList.add("is-enabled");
      status.textContent = this.t("profile.enabled");
    } else {
      status.textContent = "";
    }

    const note = document.getElementById("profile-scope-note");
    if (note) {
      const knownMode = profile.scopeMode === "session" || profile.scopeMode === "user";
      note.textContent = !profile.scopeMode ? "" : knownMode
        ? this.t("profile.scopeMode." + profile.scopeMode)
        : this.t("profile.scopeMode.unknown", profile.scopeMode);
      note.classList.toggle("hidden", !profile.scopeMode);
    }
  }

  _renderFilters() {
    const profile = this.state.profile;
    const scopeSelect = document.getElementById("profile-scope");
    const sessionSelect = document.getElementById("profile-session");
    if (scopeSelect) {
      const current = profile.scope;
      const choices = current && !profile.scopes.includes(current)
        ? [current, ...profile.scopes]
        : profile.scopes;
      scopeSelect.innerHTML = '<option value="">' + esc(this.t("profile.allScopes")) + "</option>" +
        choices.map((scope) => '<option value="' + esc(scope) + '">' + esc(scope) + "</option>").join("");
      scopeSelect.value = current;
    }
    if (sessionSelect) {
      const current = profile.sessionId;
      const choices = current && !profile.sessionIds.includes(current)
        ? [current, ...profile.sessionIds]
        : profile.sessionIds;
      sessionSelect.innerHTML = '<option value="">' + esc(this.t("profile.allSessions")) + "</option>" +
        choices.map((session) => '<option value="' + esc(session) + '">' + esc(session) + "</option>").join("");
      sessionSelect.value = current;
    }
    const keyInput = document.getElementById("profile-key");
    if (keyInput && keyInput.value !== profile.key) keyInput.value = profile.key;
  }

  _formatTime(value) {
    if (value == null || value === "") return this.t("table.na");
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      const millis = numeric < 100000000000 ? numeric * 1000 : numeric;
      const date = new Date(millis);
      if (!Number.isNaN(date.getTime())) return date.toLocaleString();
    }
    return String(value);
  }

  _formatConfidence(value) {
    if (value == null || value === "") return this.t("table.na");
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return this.t("table.na");
    const percent = numeric <= 1 ? numeric * 100 : numeric;
    return Math.max(0, Math.min(100, percent)).toFixed(0) + "%";
  }

  render() {
    this._renderStatus();
    this._renderFilters();
    this._renderTable();
    this._updatePagination();
    if (typeof window !== "undefined" && typeof window.lmHydrateIcons === "function") {
      window.lmHydrateIcons();
    }
  }

  _renderEmpty(message = this.t("common.noData")) {
    const body = document.getElementById("profiles-body");
    if (body) body.innerHTML = '<tr><td colspan="4" class="table-empty">' + esc(message) + "</td></tr>";
  }

  _renderTable() {
    const body = document.getElementById("profiles-body");
    if (!body) return;
    const items = this.state.profile.items;
    if (!items.length) {
      this._renderEmpty();
      return;
    }

    // Group facts by profile_scope so each user's identity is shown once as a
    // group header instead of being repeated on every fact row.  Map preserves
    // the server ordering (first occurrence wins); fact rows keep their original
    // index into state.profile.items so edit/delete behaviour is unchanged.
    const groups = new Map();
    items.forEach((item, index) => {
      const scope = item.profile_scope || "";
      let group = groups.get(scope);
      if (!group) {
        group = { head: item, rows: [] };
        groups.set(scope, group);
      }
      group.rows.push({ item, index });
    });

    const html = [];
    groups.forEach((group) => {
      html.push(this._renderGroupHeader(group));
      group.rows.forEach(({ item, index }) => html.push(this._renderFactRow(item, index)));
    });
    body.innerHTML = html.join("");
  }

  _renderGroupHeader(group) {
    const head = group.head || {};
    const senderName = head.source_sender_name || this.t("table.na");
    const groupName = head.source_group_name || (head.source_group_id
      ? this.t("profile.groupWithId", head.source_group_id)
      : this.t("profile.privateOrUnknown"));
    return '<tr class="profile-group-header"><td class="profile-group-cell" colspan="4" title="' + esc(senderName + " / " + groupName) + '">' +
      '<div class="profile-group-identity"><strong class="profile-group-user">' + esc(senderName) + "</strong>" +
      '<span class="profile-group-chat">' + esc(groupName) + "</span>" +
      '<span class="profile-group-count">' + esc(this.t("profile.groupFactCount", group.rows.length)) + "</span></div></td></tr>";
  }

  _renderFactRow(item, index) {
    return '<tr class="profile-row" tabindex="0" data-profile-index="' + index + '" aria-label="' + esc(item.profile_key) + '">' +
      '<td class="profile-fact-cell" title="' + esc(item.profile_key) + '"><div class="profile-fact-content"><strong class="profile-key-cell cell-mono">' + esc(item.profile_key) + '</strong><span class="type-tag">' + esc(item.category) + "</span></div></td>" +
      '<td class="profile-value-cell" title="' + esc(item.value) + '"><div class="profile-value-content">' + esc(item.value) + "</div></td>" +
      '<td class="cell-mono profile-updated-cell">' + esc(this._formatTime(item.updated_at)) + "</td>" +
      '<td class="profile-action-cell"><button type="button" class="btn btn-danger btn-sm profile-delete" data-profile-scope="' + esc(item.profile_scope) + '" data-profile-key="' + esc(item.profile_key) + '" data-i18n-title="profile.deleteTitle" title="' + esc(this.t("profile.deleteTitle")) + '" aria-label="' + esc(this.t("profile.deleteTitle")) + '"><i data-lucide="trash-2" aria-hidden="true"></i><span>' + esc(this.t("profile.delete")) + "</span></button></td>" +
      "</tr>";
  }

  _updatePagination() {
    const profile = this.state.profile;
    const page = Math.floor(profile.offset / profile.limit) + 1;
    const pages = Math.max(1, Math.ceil(profile.total / profile.limit));
    const info = document.getElementById("profile-pagination-info");
    if (info) info.textContent = this.t("common.page", page, pages, profile.total);
    const previous = document.getElementById("profile-prev");
    const next = document.getElementById("profile-next");
    if (previous) previous.disabled = profile.offset <= 0;
    if (next) next.disabled = !profile.hasMore;
  }

  _formatDateInput(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return "";
    const date = new Date((numeric < 100000000000 ? numeric * 1000 : numeric));
    if (Number.isNaN(date.getTime())) return "";
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  openItem(item) {
    if (!item || !this.peek) return;
    this._activeItem = item;
    const badge = document.getElementById("peek-badge");
    const title = document.getElementById("peek-title");
    const body = document.getElementById("peek-body");
    if (!body) return;
    if (badge) badge.textContent = this.t("profile.detailBadge");
    if (title) title.textContent = item.profile_key || this.t("profile.editTitle");
    const groupName = item.source_group_name || (item.source_group_id
      ? this.t("profile.groupWithId", item.source_group_id)
      : this.t("profile.privateOrUnknown"));
    body.innerHTML = `
      <form class="profile-editor" id="profile-editor-form">
        <div class="profile-editor-section">
          <div class="peek-section-title">${esc(this.t("profile.editSection"))}</div>
          <label class="form-label" for="profile-edit-key">${esc(this.t("profile.key"))}</label>
          <input class="input" id="profile-edit-key" value="${esc(item.profile_key)}" readonly>
          <label class="form-label" for="profile-edit-category">${esc(this.t("profile.category"))}</label>
          <select class="select input" id="profile-edit-category">
            ${["identity", "preference", "status", "task_preference", "constraint"].map((category) =>
              `<option value="${category}"${category === item.category ? " selected" : ""}>${esc(this.t("profile.category." + category))}</option>`).join("")}
          </select>
          <label class="form-label" for="profile-edit-value">${esc(this.t("profile.value"))}</label>
          <textarea class="input textarea" id="profile-edit-value" rows="5" maxlength="300">${esc(item.value)}</textarea>
          <label class="form-label" for="profile-edit-confidence">${esc(this.t("profile.confidence"))}</label>
          <input class="input" id="profile-edit-confidence" type="number" min="0" max="1" step="0.01" value="${esc(item.confidence == null ? "" : item.confidence)}">
          <label class="form-label" for="profile-edit-expires">${esc(this.t("profile.expires"))}</label>
          <input class="input" id="profile-edit-expires" type="datetime-local" value="${esc(this._formatDateInput(item.expires_at))}">
          <small class="profile-editor-hint">${esc(this.t("profile.expiresHint"))}</small>
        </div>
        <div class="profile-editor-section profile-source-details">
          <div class="peek-section-title">${esc(this.t("profile.sourceDetails"))}</div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.recentChat"))}</span><strong>${esc(groupName)}</strong></div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.sourceUser"))}</span><strong>${esc(item.source_sender_name || this.t("table.na"))}</strong></div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.scope"))}</span><code>${esc(item.profile_scope || this.t("table.na"))}</code></div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.sourceSession"))}</span><code>${esc(item.source_session_id || this.t("table.na"))}</code></div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.sourceMemory"))}</span><code>${esc(item.source_memory_id == null ? this.t("table.na") : item.source_memory_id)}</code></div>
          <div class="profile-detail-row"><span>${esc(this.t("profile.updated"))}</span><strong>${esc(this._formatTime(item.updated_at))}</strong></div>
        </div>
        <div class="profile-editor-actions">
          <button type="button" class="btn btn-danger" id="profile-editor-delete"><i data-lucide="trash-2" aria-hidden="true"></i><span>${esc(this.t("profile.delete"))}</span></button>
          <button type="submit" class="btn btn-primary" id="profile-editor-save"><i data-lucide="save" aria-hidden="true"></i><span>${esc(this.t("common.save"))}</span></button>
        </div>
      </form>`;
    this.peek.open(true);
    const form = document.getElementById("profile-editor-form");
    if (form) form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.saveItem(item);
    });
    const deleteButton = document.getElementById("profile-editor-delete");
    if (deleteButton) deleteButton.addEventListener("click", () => this.deleteItem(item.profile_scope, item.profile_key));
    if (typeof window !== "undefined" && typeof window.lmHydrateIcons === "function") {
      window.lmHydrateIcons();
    }
  }

  async saveItem(item) {
    if (!item || this._saving) return;
    const value = document.getElementById("profile-edit-value")?.value?.trim() || "";
    const category = document.getElementById("profile-edit-category")?.value || "";
    const confidenceText = document.getElementById("profile-edit-confidence")?.value || "";
    const expiresText = document.getElementById("profile-edit-expires")?.value || "";
    const confidence = Number(confidenceText);
    if (!value || !category || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      this._showToast(this.t("profile.invalidFields"), true);
      return;
    }
    let expiresAt = null;
    if (expiresText) {
      const millis = Date.parse(expiresText);
      if (!Number.isFinite(millis)) {
        this._showToast(this.t("profile.invalidExpiry"), true);
        return;
      }
      expiresAt = millis / 1000;
    }
    this._saving = true;
    const saveButton = document.getElementById("profile-editor-save");
    if (saveButton) saveButton.disabled = true;
    try {
      await this.api.post("profiles/update", {
        profile_scope: item.profile_scope,
        profile_key: item.profile_key,
        value,
        category,
        confidence,
        expires_at: expiresAt,
      }, { retries: 0 });
      this._showToast(this.t("profile.updateSuccess"));
      this.peek.close();
      await this.fetch();
    } catch (error) {
      this._showToast(error && error.message ? error.message : this.t("profile.updateFailed"), true);
    } finally {
      this._saving = false;
      if (saveButton) saveButton.disabled = false;
    }
  }

  async deleteItem(scope, key) {
    if (!scope || !key || this._deleting) return;
    this._deleting = true;
    const badge = document.getElementById("peek-badge");
    const title = document.getElementById("peek-title");
    if (badge) badge.textContent = "";
    if (title) title.textContent = this.t("profile.deleteConfirmTitle");
    this.peek.open();
    try {
      const confirmed = await this.peek.showConfirmDialog(
        this.t("profile.deleteConfirmTitle"),
        this.t("profile.deleteConfirmMessage", key, scope),
      );
      if (!confirmed) return;
      const result = await this.api.post("profiles/delete", {
        profile_scope: scope,
        profile_key: key,
      }, { retries: 0 });
      this._showToast(this.t("profile.deleteSuccess", Number(result && result.count) || 1));
      if (this.state.profile.offset > 0 && this.state.profile.items.length === 1) {
        this.state.profile.offset = Math.max(0, this.state.profile.offset - this.state.profile.limit);
      }
      await this.fetch();
    } catch (error) {
      this._showToast(error && error.message ? error.message : this.t("profile.deleteFailed"), true);
    } finally {
      this.peek.close();
      this._deleting = false;
    }
  }

  _showToast(message, isError = false) {
    if (typeof window !== "undefined" && typeof window.lmShowToast === "function") {
      window.lmShowToast(message, isError);
    }
  }

  _resetAndFetch() {
    this.state.profile.offset = 0;
    this.fetch();
  }

  initEventListeners() {
    if (this._bound) return;
    this._bound = true;
    const scope = document.getElementById("profile-scope");
    const session = document.getElementById("profile-session");
    const key = document.getElementById("profile-key");
    const refresh = document.getElementById("profile-refresh");
    const reset = document.getElementById("profile-reset");
    const previous = document.getElementById("profile-prev");
    const next = document.getElementById("profile-next");
    const body = document.getElementById("profiles-body");

    if (scope) scope.addEventListener("change", () => {
      this.state.profile.scope = scope.value;
      this._resetAndFetch();
    });
    if (session) session.addEventListener("change", () => {
      this.state.profile.sessionId = session.value;
      this._resetAndFetch();
    });
    if (key) key.addEventListener("input", debounce(() => {
      this.state.profile.key = key.value.trim();
      this._resetAndFetch();
    }, 300));
    if (refresh) refresh.addEventListener("click", () => this.fetch());
    if (reset) reset.addEventListener("click", () => {
      this.state.profile.scope = "";
      this.state.profile.sessionId = "";
      this.state.profile.key = "";
      this._resetAndFetch();
    });
    if (previous) previous.addEventListener("click", () => {
      if (this.state.profile.offset <= 0) return;
      this.state.profile.offset = Math.max(0, this.state.profile.offset - this.state.profile.limit);
      this.fetch();
    });
    if (next) next.addEventListener("click", () => {
      if (!this.state.profile.hasMore) return;
      this.state.profile.offset += this.state.profile.limit;
      this.fetch();
    });
    if (body) body.addEventListener("click", (event) => {
      const button = event.target.closest(".profile-delete");
      if (button) {
        event.stopPropagation();
        this.deleteItem(button.dataset.profileScope, button.dataset.profileKey);
        return;
      }
      const row = event.target.closest(".profile-row");
      if (row) this.openItem(this.state.profile.items[Number(row.dataset.profileIndex)]);
    });
    if (body) body.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest(".profile-row");
      if (!row || event.target.closest("button")) return;
      event.preventDefault();
      this.openItem(this.state.profile.items[Number(row.dataset.profileIndex)]);
    });
  }
}
