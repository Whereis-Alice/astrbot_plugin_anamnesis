/** Identity groups presentation only; every mutation uses persisted scope/key. */
import { debounce, esc } from "./utils.js";
const CATEGORIES = ["identity", "preference", "status", "task_preference", "constraint"];
const KEY_LABELS = { preferred_nickname: "nickname", nickname: "nickname", preferred_name: "nickname", name: "name", real_name: "name", occupation: "occupation", profession: "occupation", age: "age", birthday: "birthday", location: "location", city: "location", favorite_food: "food", food_preference: "food", dietary_preference: "food", favorite_game: "game", favorite_games: "game", hobby: "hobby", hobbies: "hobby", language: "language", preferred_language: "language", communication_style: "style", response_style: "style", timezone: "timezone", current_status: "status" };

export class ProfilePage {
  constructor(state, apiClient, peekPanel) {
    this.state = state; this.api = apiClient; this.peek = peekPanel;
    state.profile = { users: [], items: [], total: 0, limit: 30, offset: 0, query: "", sessionId: "", identity: "", user: null, readerOpen: false, hasMore: false, detailTotal: 0, detailHasMore: false, category: "", lifecycle: "", profileEnabled: null, scopeMode: "", listError: "", detailError: "" };
    this._fetchGeneration = 0; this._detailGeneration = 0;
  }
  t(key, ...args) { return window.t?.(key, ...args) || key; }
  _hydrate() { window.lmHydrateIcons?.(); }
  _showToast(message, error = false) { window.lmShowToast?.(message, error); }

  async fetch() {
    const generation = ++this._fetchGeneration;
    const p = this.state.profile;
    const params = { limit: String(p.limit), offset: String(p.offset) };
    if (p.query) params.q = p.query;
    if (p.sessionId) params.session_id = p.sessionId;
    const refresh = document.getElementById("profile-refresh");
    if (refresh) refresh.disabled = true;
    try {
      const data = await this.api.get("profiles/users", params);
      if (generation !== this._fetchGeneration) return;
      p.users = Array.isArray(data.users) ? data.users : [];
      p.total = Number(data.total) || 0; p.limit = Number(data.limit) || p.limit;
      p.offset = Number(data.offset) || 0; p.hasMore = Boolean(data.has_more);
      p.profileEnabled = data.profile_enabled; p.scopeMode = data.scope_mode || ""; p.listError = "";
      if (!p.users.length && p.offset > 0 && p.total > 0) { p.offset = Math.max(0, Math.floor((p.total - 1) / p.limit) * p.limit); return this.fetch(); }
      const user = p.users.find(u => u.identity === p.identity);
      this.renderDirectory();
      if (user && p.readerOpen) await this.selectUser(user);
      else if (!user) { ++this._detailGeneration; p.identity = ""; p.user = null; p.items = []; p.readerOpen = false; this.renderDetail(); }
    } catch (error) { if (generation === this._fetchGeneration) { p.listError = error.message || this.t("profile.fetchFailed"); this.renderDirectory(); } }
    finally { if (generation === this._fetchGeneration && refresh) refresh.disabled = false; }
  }
  async selectUser(user, reveal = false) {
    if (!user?.identity) return;
    const p = this.state.profile;
    p.identity = user.identity; p.user = user; p.readerOpen = true; p.items = []; p.detailTotal = Number(user.fact_count) || 0;
    p.detailHasMore = false; p.detailError = "";
    this.renderDirectory(); await this.fetchDetail(false);
    if (reveal) document.getElementById("profile-reader")?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }
  closeUser() {
    const p = this.state.profile;
    ++this._detailGeneration; p.readerOpen = false; p.detailLoading = false;
    this.renderDirectory(); this.renderDetail();
    document.getElementById("profile-users")?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }
  async fetchDetail(append = false) {
    const p = this.state.profile;
    if (!p.identity) return;
    const generation = ++this._detailGeneration;
    const identity = p.identity;
    p.detailLoading = true; p.detailError = ""; this.renderDetail();
    try {
      const data = await this.api.get("profiles", { identity, limit: "500", offset: String(append ? p.items.length : 0) });
      if (generation !== this._detailGeneration || identity !== p.identity) return;
      const incoming = (Array.isArray(data.items) ? data.items : []).map(item => ({ ...item, profile_scope: item.profile_scope || item.scope || "", profile_key: item.profile_key || item.key || "", value: item.value == null ? "" : String(item.value) }));
      p.items = append ? [...p.items, ...incoming] : incoming;
      p.detailTotal = Number(data.total) || p.items.length;
      p.detailHasMore = Boolean(data.has_more) && incoming.length > 0;
    } catch (error) { if (generation === this._detailGeneration) p.detailError = error.message || this.t("profile.fetchFailed"); }
    finally { if (generation === this._detailGeneration) { p.detailLoading = false; this.renderDetail(); if (!append) { const reader = document.getElementById("profile-reader"); if (reader) reader.scrollTop = 0; } } }
  }
  lifecycle(item, now = Date.now() / 1000) {
    if (item.quarantined) return "quarantined";
    if (item.suppressed) return "suppressed";
    const expires = Number(item.expires_at);
    return expires > 0 ? expires <= now ? "expired" : "temporary" : "stable";
  }
  label(item) {
    if (item.display_label) return String(item.display_label);
    const key = item.profile_key || "";
    return KEY_LABELS[key] ? this.t("profile.keyLabel." + KEY_LABELS[key]) : key.replace(/[_:.-]+/g, " ").trim() || this.t("profile.fact");
  }
  _formatTime(value) { if (!value) return this.t("table.na"); const n = Number(value); return Number.isFinite(n) && n > 0 ? new Date(n < 1e11 ? n * 1000 : n).toLocaleString() : String(value); }
  _formatConfidence(value) { return value != null && value !== "" && Number.isFinite(Number(value)) ? Math.round(Math.min(1, Math.max(0, Number(value))) * 100) + "%" : this.t("table.na"); }
  _chatName(item) {
    const session = String(item.source_session_id || "");
    const source = session.match(/:(GroupMessage|FriendMessage):(.+)$/);
    if (source && ((source[1] === "FriendMessage" && (item.source_group_id || item.source_group_name)) ||
        (source[1] === "GroupMessage" && item.source_group_id && String(item.source_group_id) !== source[2]))) return session;
    return item.source_group_name || (item.source_group_id ? this.t("profile.groupWithId", item.source_group_id) : this.t("profile.privateOrUnknown"));
  }
  render() { this.renderDirectory(); this.renderDetail(); }

  renderDirectory() {
    const p = this.state.profile;
    const status = document.getElementById("profile-status");
    if (status) status.textContent = p.profileEnabled === false ? this.t("profile.disabled") : p.profileEnabled === true ? this.t("profile.enabled") : "";
    const note = document.getElementById("profile-scope-note");
    if (note) { note.textContent = p.scopeMode ? ["session", "user"].includes(p.scopeMode) ? this.t("profile.scopeMode." + p.scopeMode) : this.t("profile.scopeMode.unknown", p.scopeMode) : ""; note.classList.toggle("hidden", !p.scopeMode); }
    const list = document.getElementById("profile-users");
    const total = document.getElementById("profile-user-total");
    if (total) total.textContent = this.t("profile.contact.people", p.total);
    if (list) list.innerHTML = p.listError ? '<div class="inline-feedback" role="alert">' + esc(p.listError) + '<button class="btn btn-sm" data-profile-retry="list">' + esc(this.t("common.retry")) + '</button></div>' : p.users.length ? p.users.map((user, index) => {
      const name = user.manual_alias || user.sender_name || user.sender_id || this.t("profile.unknownUser");
      const chat = user.last_group_name || (user.last_group_id ? this.t("profile.groupWithId", user.last_group_id) : this.t("profile.privateOrUnknown"));
      const selected = user.identity === p.identity && p.readerOpen;
      const preview = (Array.isArray(user.fact_preview) ? user.fact_preview : []).slice(0, 3);
      const facts = preview.length ? preview.map(item => `<span class="profile-preview-fact"><span class="profile-preview-label">${esc(this.label(item))}</span><span class="profile-preview-value">${esc(item.value)}</span></span>`).join("") : `<span class="profile-preview-empty">${esc(this.t("profile.contact.emptyPreview"))}</span>`;
      return `<button type="button" class="profile-user ${selected ? "is-selected" : ""}" style="--profile-card-hue:${(index * 67 + 245) % 360}" data-profile-user="${index}" aria-pressed="${selected}" aria-expanded="${selected}" aria-controls="profile-reader"><span class="profile-user-heading"><span class="profile-avatar" aria-hidden="true">${esc(Array.from(String(name))[0] || "?")}</span><span class="profile-user-copy"><strong>${esc(name)}</strong><span class="profile-user-id">${esc(user.platform || "")} · ${esc(user.sender_id || "--")}</span></span></span><span class="profile-user-chat"><i data-lucide="messages-square" aria-hidden="true"></i>${esc(chat)}</span><span class="profile-user-preview">${facts}</span><span class="profile-user-count">${esc(this.t("profile.groupFactCount", user.fact_count || 0))}${user.pending_count ? `<span class="profile-pending-badge">${esc(this.t("profile.ledger.pendingCount", user.pending_count))}</span>` : ""}</span><span class="profile-user-footer"><span>${esc(this.t("profile.contact.lastSeen"))}<time>${esc(this._formatTime(user.last_seen_at || user.updated_at))}</time></span><span class="profile-open-label">${esc(this.t("profile.contact.open"))}<i data-lucide="arrow-up-right" aria-hidden="true"></i></span></span></button>`;
    }).join("") : '<div class="table-empty">' + esc(this.t("common.noData")) + '</div>';
    const info = document.getElementById("profile-pagination-info");
    if (info) info.textContent = this.t("profile.userPage", Math.floor(p.offset / p.limit) + 1, Math.max(1, Math.ceil(p.total / p.limit)), p.total);
    const prev = document.getElementById("profile-prev"), next = document.getElementById("profile-next");
    if (prev) prev.disabled = p.offset <= 0;
    if (next) next.disabled = !p.hasMore;
    this._hydrate();
  }
  renderDetail() {
    const container = document.getElementById("profile-reader");
    if (!container) return;
    const p = this.state.profile, u = p.user;
    container.classList.toggle("hidden", !p.readerOpen);
    if (!u) { container.innerHTML = '<div class="profile-reader-empty">' + esc(this.t("profile.selectUser")) + '</div>'; return; }
    const now = Date.now() / 1000;
    const visible = p.items.map((item, index) => ({ item, index })).filter(({ item }) => (!p.category || item.category === p.category) && (!p.lifecycle || this.lifecycle(item, now) === p.lifecycle));
    const scopes = new Map();
    visible.forEach(row => {
      const source = JSON.stringify([row.item.profile_scope, row.item.source_session_id || ""]);
      if (!scopes.has(source)) scopes.set(source, []);
      scopes.get(source).push(row);
    });
    let html = `<button type="button" class="btn btn-ghost profile-return" id="profile-person-close"><i data-lucide="arrow-left" aria-hidden="true"></i>${esc(this.t("profile.contact.close"))}</button><header class="profile-reader-header"><div><span class="eyebrow">${esc(this.t("profile.detailBadge"))}</span><h2>${esc(u.manual_alias || u.sender_name || u.sender_id || this.t("profile.unknownUser"))}</h2><p>${esc(u.platform || "")} · ${esc(u.sender_id || "--")}<br>${esc(this.t("profile.recentGroup"))}：${esc(u.last_group_name || (u.last_group_id ? this.t("profile.groupWithId", u.last_group_id) : this.t("profile.privateOrUnknown")))}<br>${esc(this.t("profile.contact.lastSeen"))}：${esc(this._formatTime(u.last_seen_at || u.updated_at))}</p></div><span class="type-tag">${esc(this.t("profile.groupFactCount", p.detailTotal))}</span></header><div class="profile-reader-filters"><label>${esc(this.t("profile.category"))}<select class="select input" id="profile-category"><option value="">${esc(this.t("profile.allCategories"))}</option>${CATEGORIES.map(c => `<option value="${c}"${p.category === c ? " selected" : ""}>${esc(this.t("profile.category." + c))}</option>`).join("")}</select></label><label>${esc(this.t("profile.lifecycle"))}<select class="select input" id="profile-lifecycle"><option value="">${esc(this.t("profile.allLifecycle"))}</option>${["stable", "temporary", "expired", "suppressed", "quarantined"].map(c => `<option value="${c}"${p.lifecycle === c ? " selected" : ""}>${esc(this.t("profile.lifecycle." + c))}</option>`).join("")}</select></label></div>`;
    html += `<form class="profile-alias-form" id="profile-alias-form"><label for="profile-alias">${esc(this.t("profile.ledger.alias"))}</label><div><input class="input" id="profile-alias" maxlength="80" value="${esc(u.manual_alias || "")}" placeholder="${esc(this.t("profile.ledger.aliasHint"))}"><button type="submit" class="btn btn-secondary btn-sm" id="profile-alias-save">${esc(this.t("common.save"))}</button></div><small>${esc(this.t("profile.ledger.observedNames"))} ${esc((u.observed_names || [u.sender_name]).filter(Boolean).join(" · "))}</small></form>`;
    if (p.detailError) html += '<div class="inline-feedback" role="alert">' + esc(p.detailError) + '<button class="btn btn-sm" data-profile-retry="detail">' + esc(this.t("common.retry")) + '</button></div>';
    if (!visible.length) html += '<div class="table-empty">' + esc(this.t(p.detailLoading ? "common.loading" : "profile.noFacts")) + '</div>';
    for (const rows of scopes.values()) {
      const head = [...rows].sort((a, b) => Number(b.item.updated_at) - Number(a.item.updated_at))[0].item;
      html += `<section class="profile-source-section"><header class="profile-source-header"><i data-lucide="messages-square" aria-hidden="true"></i><div><h3>${esc(this._chatName(head))}</h3><span>${esc(this.t("profile.groupFactCount", rows.length))}</span><details class="profile-source-context"><summary>${esc(this.t("profile.sourceDetails"))}</summary><code>${esc(head.source_session_id || this.t("profile.sharedSource"))}</code><code>${esc(head.profile_scope)}</code></details></div></header>`;
      const categories = [...CATEGORIES, ...new Set(rows.map(r => r.item.category).filter(c => !CATEGORIES.includes(c)))];
      for (const category of categories) {
        const facts = rows.filter(r => r.item.category === category);
        if (!facts.length) continue;
        const translated = this.t("profile.category." + category);
        html += `<h4 class="profile-category-heading">${esc(translated.startsWith("profile.category.") ? category || this.t("profile.fact") : translated)}</h4><div class="profile-facts">`;
        facts.forEach(({ item, index }) => {
          const life = this.lifecycle(item, now);
          html += `<article class="profile-fact-card ${life}"><div class="profile-fact-heading"><h5>${esc(this.label(item))}</h5><span class="profile-lifecycle ${life}">${esc(this.t("profile.lifecycle." + life))}</span></div><div class="profile-authority"><span>${esc(this.t("profile.ledger.authority." + (item.authority || "legacy")))}</span>${item.manual_override ? `<span class="profile-manual-lock">${esc(this.t("profile.ledger.locked"))}</span>` : ""}</div><div class="profile-fact-value">${esc(item.value)}</div>${this._renderLedger(item, index)}<div class="profile-fact-footer"><span>${esc(this._formatTime(item.updated_at))} · ${esc(this.t("profile.confidence"))} ${esc(this._formatConfidence(item.confidence))}</span><button type="button" class="btn btn-secondary btn-sm" data-profile-edit="${index}"><i data-lucide="square-pen" aria-hidden="true"></i>${esc(this.t("profile.readEdit"))}</button></div><details class="profile-fact-source"><summary>${esc(this.t("profile.sourceDetails"))}</summary><dl><dt>${esc(this.t("profile.key"))}</dt><dd>${esc(item.profile_key)}</dd><dt>${esc(this.t("profile.scope"))}</dt><dd>${esc(item.profile_scope)}</dd><dt>${esc(this.t("profile.sourceUser"))}</dt><dd>${esc(item.source_sender_name || "--")}</dd><dt>${esc(this.t("profile.expires"))}</dt><dd>${esc(item.expires_at ? this._formatTime(item.expires_at) : this.t("profile.never"))}</dd></dl>${item.source_memory_id != null ? `<button type="button" class="btn btn-ghost btn-sm" data-source-memory="${esc(item.source_memory_id)}">${esc(this.t("profile.openSource", item.source_memory_id))}<i data-lucide="arrow-up-right" aria-hidden="true"></i></button>` : ""}</details></article>`;
        });
        html += '</div>';
      }
      html += '</section>';
    }
    if (p.detailHasMore) html += `<button type="button" class="btn btn-secondary profile-load-more" id="profile-load-more"${p.detailLoading ? " disabled" : ""}>${esc(this.t(p.detailLoading ? "common.loading" : "profile.loadMore", p.items.length, p.detailTotal))}</button>`;
    container.innerHTML = html; this._hydrate();
  }
  _renderEvidence(evidence = []) {
    if (!evidence.length) return `<p class="profile-ledger-note">${esc(this.t("profile.ledger.noQuote"))}</p>`;
    return evidence.map(source => `<div class="profile-evidence ${source.active === false ? "is-inactive" : ""}"><p>${esc(source.quote || this.t(source.source_memory_id == null ? "profile.ledger.manualEvidence" : "profile.ledger.noQuote"))}</p><small>${esc(source.source_group_name || source.source_session_id || this.t("profile.sharedSource"))} · ${esc(this._formatTime(source.created_at))}${source.active === false ? ` · ${esc(this.t("profile.ledger.inactiveEvidence"))}` : ""}</small>${source.source_memory_id != null ? `<button type="button" class="btn btn-ghost btn-sm" data-source-memory="${esc(source.source_memory_id)}">${esc(this.t("profile.openSource", source.source_memory_id))}</button>` : ""}</div>`).join("");
  }
  _renderAdoption(item, index) {
    const paused = Boolean(item.quarantined), action = paused ? "resume" : "pause";
    if (!Number.isInteger(item.current_claim_id) || !Number.isInteger(item.revision)) return "";
    const reason = paused ? `<p class="profile-quarantine-reason">${esc(item.quarantine_reason || this.t("profile.quarantine.noReason"))}</p>` : `<label class="form-label" for="profile-pause-reason-${index}">${esc(this.t("profile.quarantine.reason"))}</label><textarea class="input textarea" id="profile-pause-reason-${index}" data-profile-pause-reason rows="2" maxlength="500" placeholder="${esc(this.t("profile.quarantine.reasonHint"))}"></textarea>`;
    return `<details class="profile-adoption-control${paused ? " is-paused" : ""}"${paused ? " open" : ""}><summary>${esc(this.t(paused ? "profile.lifecycle.quarantined" : "profile.quarantine.manage"))}</summary><p class="profile-ledger-note">${esc(this.t("profile.quarantine.hint"))}</p>${reason}<button type="button" class="btn btn-secondary btn-sm" data-profile-quarantine="${action}" data-profile-index="${index}">${esc(this.t("profile.quarantine." + action))}</button></details>`;
  }
  _renderLedger(item, index) {
    const pending = Array.isArray(item.pending) ? item.pending : [], history = Array.isArray(item.history) ? item.history : [];
    let html = pending.length ? `<section class="profile-proposals"><h6>${esc(this.t("profile.ledger.pendingCount", pending.length))}</h6><p class="profile-ledger-note">${esc(this.t("profile.ledger.proposalHint"))}</p>${pending.map(claim => `<article><div class="profile-proposal-value">${esc(claim.value)}</div>${this._renderEvidence(claim.evidence)}<div class="profile-review-actions"><button type="button" class="btn btn-primary btn-sm" data-profile-review="accept" data-profile-index="${index}" data-claim-id="${esc(claim.claim_id)}"${claim.can_accept === false ? " disabled" : ""}>${esc(this.t("profile.ledger.accept"))}</button><button type="button" class="btn btn-secondary btn-sm" data-profile-review="reject" data-profile-index="${index}" data-claim-id="${esc(claim.claim_id)}">${esc(this.t("profile.ledger.reject"))}</button></div></article>`).join("")}</section>` : "";
    if (item.evidence || history.length || item.manual_override) html += `<details class="profile-ledger-details"><summary>${esc(this.t("profile.ledger.evidenceHistory", item.evidence?.length || 0, history.length))}</summary>${this._renderEvidence(item.evidence)}${history.length ? `<h6>${esc(this.t("profile.ledger.history"))}</h6>${history.map(claim => `<article class="profile-history-item"><span>${esc(this.t("profile.ledger.state." + claim.state))} · ${esc(this._formatTime(claim.updated_at))}</span><p>${esc(claim.value)}</p><details><summary>${esc(this.t("profile.sourceDetails"))}</summary>${this._renderEvidence(claim.evidence)}</details></article>`).join("")}` : ""}${item.manual_override ? `<button type="button" class="btn btn-ghost btn-sm" data-profile-review="unlock" data-profile-index="${index}">${esc(this.t("profile.ledger.unlock"))}</button>` : ""}</details>`;
    const adoptionHistory = Array.isArray(item.adoption_history) ? item.adoption_history : [];
    if (adoptionHistory.length) html += `<details class="profile-ledger-details profile-adoption-history"><summary>${esc(this.t("profile.quarantine.history"))}</summary>${adoptionHistory.map(entry => `<article class="profile-history-item"><span>${esc(this.t("profile.quarantine.action." + entry.action))} · ${esc(this._formatTime(entry.created_at))}</span><p>${esc(entry.reason || this.t("profile.quarantine.noReason"))}</p><small>${esc(entry.actor)}</small></article>`).join("")}</details>`;
    return this._renderAdoption(item, index) + html;
  }
  async quarantineItem(item, action, reason = "") {
    if (!item || !["pause", "resume"].includes(action) || this._quarantining || this._saving || this._reviewing) return;
    this._quarantining = true;
    const generation = ++this.peek._detailGeneration;
    this.peek.open(true);
    try {
      if (!await this.peek.showConfirmDialog(this.t("profile.quarantine.confirmTitle." + action), this.t("profile.quarantine.confirm." + action, this.label(item)), { destructive: false })) return;
      await this.api.post("profiles/quarantine", { profile_scope: item.profile_scope, profile_key: item.profile_key, current_claim_id: item.current_claim_id, expected_revision: item.revision, action, reason: action === "pause" ? reason.trim() : "" }, { retries: 0 });
      this._showToast(this.t("profile.quarantine.saved." + action));
      await this.fetch();
    } catch (error) { this._showToast(error.message || this.t("profile.updateFailed"), true); }
    finally { if (generation === this.peek._detailGeneration) this.peek.close(); this._quarantining = false; }
  }
  async reviewItem(item, action, claimId) {
    if (!item || this._reviewing) return;
    this._reviewing = true;
    const generation = ++this.peek._detailGeneration;
    this.peek.open(true);
    try {
      if (!await this.peek.showConfirmDialog(this.t("profile.ledger.confirmTitle"), this.t("profile.ledger.confirm." + action, this.label(item)), { destructive: false })) return;
      await this.api.post("profiles/review", { profile_scope: item.profile_scope, profile_key: item.profile_key, action, ...(claimId ? { claim_id: Number(claimId) } : {}), expected_revision: item.revision }, { retries: 0 });
      this._showToast(this.t("profile.ledger.reviewSaved"));
      await this.fetchDetail();
    } catch (error) { this._showToast(error.message || this.t("profile.updateFailed"), true); }
    finally { if (generation === this.peek._detailGeneration) this.peek.close(); this._reviewing = false; }
  }
  async saveAlias() {
    if (this._aliasSaving || !this.state.profile.user) return;
    const identity = this.state.profile.user.identity, alias = document.getElementById("profile-alias")?.value?.trim() || "";
    const button = document.getElementById("profile-alias-save");
    this._aliasSaving = true; if (button) button.disabled = true;
    try { await this.api.post("profiles/alias", { identity, alias }, { retries: 0 }); this._showToast(this.t("profile.updateSuccess")); await this.fetch(); }
    catch (error) { this._showToast(error.message || this.t("profile.updateFailed"), true); }
    finally { this._aliasSaving = false; if (button) button.disabled = false; }
  }
  _formatDateInput(value) {
    const n = Number(value); if (!n || !Number.isFinite(n)) return "";
    const d = new Date(n < 1e11 ? n * 1000 : n), pad = v => String(v).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  openItem(item) {
    if (!item || !this.peek) return;
    this.peek._detailGeneration++; this.state._detailCache = null; this.state._nodeDetailCache = null;
    document.getElementById("peek-badge").textContent = this.t("profile.detailBadge");
    document.getElementById("peek-title").textContent = this.label(item);
    const categories = [...new Set([...CATEGORIES, item.category])];
    const titleInput = `<label class="form-label" for="profile-edit-label">${esc(this.t("profile.title"))}</label><input class="input" id="profile-edit-label" type="text" maxlength="80" value="${esc(item.display_label || "")}" placeholder="${esc(this.t("profile.titleHint"))}">`;
    document.getElementById("peek-body").innerHTML = `<form class="profile-editor" id="profile-editor-form"><div class="profile-editor-section"><div class="peek-section-title">${esc(this.t("profile.editSection"))}</div>${titleInput}<label class="form-label" for="profile-edit-category">${esc(this.t("profile.category"))}</label><select class="select input" id="profile-edit-category">${categories.map(c => `<option value="${esc(c)}"${c === item.category ? " selected" : ""}>${esc(this.t("profile.category." + c))}</option>`).join("")}</select><label class="form-label" for="profile-edit-value">${esc(this.t("profile.value"))}</label><textarea class="input textarea" id="profile-edit-value" rows="8" maxlength="300">${esc(item.value)}</textarea><label class="form-label" for="profile-edit-confidence">${esc(this.t("profile.confidence"))}</label><input class="input" id="profile-edit-confidence" type="number" min="0" max="1" step="0.01" value="${esc(item.confidence ?? 1)}"><label class="form-label" for="profile-edit-expires">${esc(this.t("profile.expires"))}</label><input class="input" id="profile-edit-expires" type="datetime-local" value="${esc(this._formatDateInput(item.expires_at))}"><small class="profile-editor-hint">${esc(this.t("profile.expiresHint"))}</small><p class="profile-ledger-note">${esc(this.t("profile.ledger.manualSaveHint"))}</p></div><details class="profile-source-details"><summary>${esc(this.t("profile.sourceDetails"))}</summary><div class="profile-detail-row"><span>${esc(this.t("profile.key"))}</span><code>${esc(item.profile_key)}</code></div><div class="profile-detail-row"><span>${esc(this.t("profile.scope"))}</span><code>${esc(item.profile_scope)}</code></div><div class="profile-detail-row"><span>${esc(this.t("profile.sourceSession"))}</span><code>${esc(item.source_session_id || "--")}</code></div>${item.source_memory_id != null ? `<button type="button" class="btn btn-sm" id="profile-editor-source">${esc(this.t("profile.openSource", item.source_memory_id))}</button>` : ""}</details><div class="profile-editor-actions"><button type="button" class="btn btn-danger" id="profile-editor-delete"><i data-lucide="trash-2" aria-hidden="true"></i>${esc(this.t("profile.delete"))}</button><button type="submit" class="btn btn-primary" id="profile-editor-save"><i data-lucide="save" aria-hidden="true"></i>${esc(this.t("common.save"))}</button></div><div id="profile-editor-feedback" class="inline-feedback hidden" role="alert"></div></form>`;
    if (item.quarantined) document.getElementById("peek-body").innerHTML = `<aside class="profile-adoption-control is-paused"><strong>${esc(this.t("profile.lifecycle.quarantined"))}</strong><p class="profile-quarantine-reason">${esc(item.quarantine_reason || this.t("profile.quarantine.noReason"))}</p><p class="profile-ledger-note">${esc(this.t("profile.quarantine.hint"))}</p></aside>` + document.getElementById("peek-body").innerHTML;
    this.peek.open(true);
    document.getElementById("profile-editor-form")?.addEventListener("submit", event => { event.preventDefault(); this.saveItem(item); });
    document.getElementById("profile-editor-delete")?.addEventListener("click", () => this.deleteItem(item.profile_scope, item.profile_key));
    document.getElementById("profile-editor-source")?.addEventListener("click", () => this.peek.renderMemory({ memory_id: item.source_memory_id }));
    document.getElementById("profile-edit-value")?.focus(); this._hydrate();
  }
  async saveItem(item) {
    if (!item || this._saving) return;
    const value = document.getElementById("profile-edit-value")?.value?.trim() || "", category = document.getElementById("profile-edit-category")?.value || "";
    const text = document.getElementById("profile-edit-confidence")?.value || "", confidence = Number(text);
    const expiresText = document.getElementById("profile-edit-expires")?.value || "", expiry = expiresText ? Date.parse(expiresText) / 1000 : null;
    const label = document.getElementById("profile-edit-label")?.value;
    const feedback = document.getElementById("profile-editor-feedback");
    const fail = message => { if (feedback) { feedback.textContent = message; feedback.classList.remove("hidden"); } this._showToast(message, true); };
    if (!value || !category || !text || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) { fail(this.t("profile.invalidFields")); return; }
    if (expiresText && !Number.isFinite(expiry)) { fail(this.t("profile.invalidExpiry")); return; }
    this._saving = true;
    const generation = this.peek._detailGeneration;
    const button = document.getElementById("profile-editor-save"); if (button) button.disabled = true;
    try {
      await this.api.post("profiles/update", { profile_scope: item.profile_scope, profile_key: item.profile_key, value, category, confidence, expires_at: expiry, ...(typeof label === "string" ? { display_label: label.trim() } : {}), ...(Number.isInteger(item.revision) ? { expected_revision: item.revision } : {}) }, { retries: 0 });
      this._showToast(this.t("profile.updateSuccess"));
      if (generation === this.peek._detailGeneration) this.peek.close();
      await this.fetch();
    } catch (error) { fail(error.message || this.t("profile.updateFailed")); }
    finally { this._saving = false; if (button) button.disabled = false; }
  }
  async deleteItem(scope, key) {
    if (!scope || !key || this._deleting) return;
    this._deleting = true; this.peek.open(true);
    const generation = this.peek._detailGeneration;
    const editor = document.getElementById("profile-editor-form");
    let cancelled = false;
    try {
      if (!await this.peek.showConfirmDialog(this.t("profile.deleteConfirmTitle"), this.t("profile.deleteConfirmMessage", key, scope))) {
        cancelled = true;
        // Restore the original form node, including live inputs and listeners.
        if (editor && generation === this.peek._detailGeneration) document.getElementById("peek-body")?.replaceChildren(editor);
        return;
      }
      const result = await this.api.post("profiles/delete", { profile_scope: scope, profile_key: key }, { retries: 0 });
      this._showToast(this.t("profile.deleteSuccess", Number(result.count) || 1)); await this.fetch();
    } catch (error) { this._showToast(error.message || this.t("profile.deleteFailed"), true); }
    finally { if (!cancelled && generation === this.peek._detailGeneration) this.peek.close(); this._deleting = false; }
  }
  initEventListeners() {
    if (this._bound) return; this._bound = true;
    const p = this.state.profile, bind = (id, type, fn) => document.getElementById(id)?.addEventListener(type, fn);
    bind("profile-refresh", "click", () => this.fetch());
    bind("profile-query", "input", debounce(event => { p.query = event.target.value.trim(); p.offset = 0; this.fetch(); }, 300));
    bind("profile-session", "input", debounce(event => { p.sessionId = event.target.value.trim(); p.offset = 0; this.fetch(); }, 300));
    bind("profile-reset", "click", () => { p.query = ""; p.sessionId = ""; p.category = ""; p.lifecycle = ""; p.offset = 0; document.getElementById("profile-query").value = ""; document.getElementById("profile-session").value = ""; this.fetch(); });
    bind("profile-prev", "click", () => { if (p.offset > 0) { p.offset = Math.max(0, p.offset - p.limit); this.fetch(); } });
    bind("profile-next", "click", () => { if (p.hasMore) { p.offset += p.limit; this.fetch(); } });
    bind("profile-users", "click", event => { const user = event.target.closest("[data-profile-user]"); if (user) this.selectUser(p.users[Number(user.dataset.profileUser)], true); if (event.target.closest('[data-profile-retry="list"]')) this.fetch(); });
    bind("profile-reader", "click", event => {
      if (event.target.closest("#profile-person-close")) { this.closeUser(); return; }
      const review = event.target.closest("[data-profile-review]"); if (review) this.reviewItem(p.items[Number(review.dataset.profileIndex)], review.dataset.profileReview, review.dataset.claimId);
      const quarantine = event.target.closest("[data-profile-quarantine]"); if (quarantine) this.quarantineItem(p.items[Number(quarantine.dataset.profileIndex)], quarantine.dataset.profileQuarantine, quarantine.closest(".profile-adoption-control")?.querySelector("[data-profile-pause-reason]")?.value || "");
      const edit = event.target.closest("[data-profile-edit]"); if (edit) this.openItem(p.items[Number(edit.dataset.profileEdit)]);
      const source = event.target.closest("[data-source-memory]"); if (source) this.peek.renderMemory({ memory_id: source.dataset.sourceMemory });
      if (event.target.closest("#profile-load-more")) this.fetchDetail(true);
      if (event.target.closest('[data-profile-retry="detail"]')) this.fetchDetail(p.items.length > 0);
    });
    bind("profile-reader", "submit", event => { if (event.target.id === "profile-alias-form") { event.preventDefault(); this.saveAlias(); } });
    bind("profile-reader", "change", event => { if (event.target.id === "profile-category") p.category = event.target.value; else if (event.target.id === "profile-lifecycle") p.lifecycle = event.target.value; else return; this.renderDetail(); });
  }
}
