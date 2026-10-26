/**
 * Recall Page - 召回测试页面
 * 负责测试记忆召回功能
 */

import { esc, statusPill, normalizeImportance } from "./utils.js";

const temporalLabels = {
  mode: ["检索模式", "Retrieval mode", "Режим поиска"],
  auto: ["自动判断", "Automatic", "Автоматически"],
  semantic: ["关键词与语义", "Keywords and semantic", "Ключевые слова и семантика"],
  time: ["按时间范围", "Time range", "Период времени"],
  hybrid: ["时间与语义融合", "Time and semantic", "Время и семантика"],
  persona: ["人格 ID（留空查询全部）", "Persona ID (blank: all)", "ID персоны (пусто: все)"],
  start: ["开始时间（包含）", "Start time (inclusive)", "Начало (включительно)"],
  end: ["结束时间（不包含）", "End time (exclusive)", "Конец (исключительно)"],
  hint: ["支持“昨天、上周”等日期。手动时间按插件或 AstrBot 时区解释；查整天时，结束时间填次日零点。", "Dates such as 昨天 or 上周 are supported. Manual times use the plugin/AstrBot timezone. End a full-day range at the next midnight.", "Поддерживаются даты вроде 昨天 и 上周. Время использует часовой пояс плагина/AstrBot; конец полного дня — следующая полночь."],
  enter: ["请填写查询关键词或时间范围", "Enter recall keywords or a time range", "Введите запрос или период времени"],
  basis: ["时间依据", "Time basis", "Основание времени"],
  event_time: ["事件时间", "Event time", "Время события"],
  atom_event_time: ["事实事件时间", "Fact event time", "Время события факта"],
  source_time: ["原消息窗口", "Source message window", "Период исходных сообщений"],
  create_time: ["记忆创建时间", "Memory creation time", "Время создания памяти"],
  graph: ["图检索证据", "Graph retrieval evidence", "Доказательства графа"],
  literal_source_support: ["原文档有字面支持", "Literal support in the source document", "Буквальное подтверждение в документе"],
  weak_literal_source_support: ["字面支持较弱，已降低图贡献", "Weak literal support; graph contribution discounted", "Слабое подтверждение; вклад графа снижен"],
  source_unavailable: ["来源不可用，已降低图贡献", "Source unavailable; graph contribution discounted", "Источник недоступен; вклад графа снижен"],
};
export function recallText(key) {
  const index = { zh: 0, en: 1, ru: 2 }[window.getLanguage?.() || "zh"] || 0;
  return temporalLabels[key]?.[index] || key;
}
function temporalDate(value, zone) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { timeZone: zone || "UTC", dateStyle: "short", timeStyle: "short" }).format(new Date(Number(value) * 1000));
  } catch { return new Date(Number(value) * 1000).toISOString(); }
}

export class RecallPage {
  constructor(state, apiClient, peekPanel) {
    this.state = state;
    this.api = apiClient;
    this.peek = peekPanel;
  }

  /**
   * 初始化召回页面事件监听
   */
  initEventListeners() {
    this.ensureTemporalControls();
    if (!this._temporalLanguageListener) {
      this._temporalLanguageListener = () => this.updateTemporalLabels();
      window.addEventListener?.("languagechange", this._temporalLanguageListener);
    }
    const queryInput = document.getElementById("recall-query");
    const searchBtn = document.getElementById("recall-search-btn");
    const kSlider = document.getElementById("recall-k");
    const kValue = document.getElementById("recall-k-value");

    // k 值滑块
    if (kSlider && kValue) {
      kSlider.addEventListener("input", () => {
        kValue.textContent = kSlider.value;
      });
    }

    // 搜索按钮
    if (searchBtn) {
      searchBtn.addEventListener("click", () => this.runRecall());
    }

    // 回车搜索（Ctrl+Enter 或 Cmd+Enter）
    if (queryInput) {
      queryInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          this.runRecall();
        }
      });
    }
  }

  ensureTemporalControls() {
    const form = document.getElementById("recall-query")?.closest?.(".recall-form-section");
    if (!form || document.getElementById("recall-mode")) return;
    const field = (id, label, placeholder = "") => `<div class="recall-form-field" style="flex:1;min-width:160px"><label class="recall-form-label" for="${id}" data-recall-label="${label}">${esc(recallText(label))}</label><input type="text" id="${id}" class="input input-sm" placeholder="${esc(placeholder)}" /></div>`;
    (document.getElementById("recall-advanced") || form).insertAdjacentHTML("beforeend", `<div class="recall-form-row" id="recall-temporal-controls"><div class="recall-form-field" style="flex:1;min-width:160px"><label class="recall-form-label" for="recall-mode" data-recall-label="mode">${esc(recallText("mode"))}</label><select id="recall-mode" class="input input-sm">${["auto", "semantic", "time", "hybrid"].map(mode => `<option value="${mode}">${esc(recallText(mode))}</option>`).join("")}</select></div>${field("recall-persona", "persona")}${field("recall-start-time", "start", "2026-10-01T00:00:00+08:00")}${field("recall-end-time", "end", "2026-10-02T00:00:00+08:00")}</div><p class="text-secondary" id="recall-temporal-hint" data-recall-label="hint">${esc(recallText("hint"))}</p>`);
  }

  updateTemporalLabels() {
    (document.querySelectorAll?.("[data-recall-label]") || []).forEach(element => { element.textContent = recallText(element.dataset.recallLabel); });
    const mode = document.getElementById("recall-mode");
    if (mode?.options) Array.from(mode.options).forEach(option => { option.textContent = recallText(option.value); });
    if (this.state._recallCache) this.renderResults(this.state._recallCache.data, this.state._recallCache.elapsed);
  }

  /**
   * 执行召回测试
   */
  async runRecall() {
    const searchBtn = document.getElementById("recall-search-btn");
    // 回车触发不经过按钮 disabled 守卫，需显式防并发（慢响应覆盖新结果）
    if (searchBtn && searchBtn.disabled) return;
    const query = document.getElementById("recall-query").value.trim();
    const k = parseInt(document.getElementById("recall-k").value) || 5;
    const sessionId = document.getElementById("recall-session").value.trim();
    const mode = document.getElementById("recall-mode")?.value || "auto";
    const personaId = document.getElementById("recall-persona")?.value.trim() || "";
    const start = document.getElementById("recall-start-time")?.value.trim() || "";
    const end = document.getElementById("recall-end-time")?.value.trim() || "";

    if (!query && (mode === "semantic" || (!start && !end))) {
      this.showToast(recallText("enter"), true);
      return;
    }

    if (searchBtn) searchBtn.disabled = true;

    const startTime = Date.now();

    try {
      const params = { query, k, mode };
      if (sessionId) params.session_id = sessionId;
      if (personaId) params.persona_id = personaId;
      if (start) params.start_time = start;
      if (end) params.end_time = end;

      const data = await this.api.post("recall/test", params);
      const elapsed = Date.now() - startTime;

      this.state._recallCache = { data, elapsed };
      this.renderResults(data, elapsed);
    } catch (e) {
      this.showToast(e.message || window.t("recall.fail"), true);
      document.getElementById("recall-results").innerHTML = '<div class="inline-feedback" role="alert">' + esc(e.message || window.t("recall.fail")) + '<button type="button" class="btn btn-sm" id="recall-retry">' + esc(window.t("common.retry")) + '</button></div>';
      document.getElementById("recall-retry")?.addEventListener("click", () => this.runRecall());
      document.getElementById("recall-stats").classList.add("hidden");
      this.state._recallCache = null;
    } finally {
      if (searchBtn) searchBtn.disabled = false;
    }
  }

  /**
   * 渲染召回结果
   * @param {Object} data - 召回结果数据
   * @param {number} elapsed - 耗时（毫秒）
   */
  renderResults(data, elapsed) {
    // 后端返回 results，不是 memories
    const memories = data.results || data.memories || [];
    const count = memories.length;

    // 更新统计信息
    const statsEl = document.getElementById("recall-stats");
    const countText = document.getElementById("recall-count-text");
    const timeText = document.getElementById("recall-time-text");

    if (statsEl) statsEl.classList.remove("hidden");
    if (countText) {
      countText.textContent = count === 0
        ? window.t("recall.noMatch")
        : window.t("recall.resultsCount", count);
    }
    if (timeText) {
      const time = data.elapsed_time_ms || elapsed;
      timeText.textContent = window.t("recall.timeElapsed", (time / 1000).toFixed(2));
      if (data.retrieval?.mode) {
        timeText.textContent += " · " + recallText(data.retrieval.mode);
        const range = data.retrieval.time_range;
        if (range) timeText.textContent += ` · [${temporalDate(range.start, range.timezone)}, ${temporalDate(range.end, range.timezone)}) ${range.timezone}`;
      }
    }

    const resultsEl = document.getElementById("recall-results");
    if (!resultsEl) return;

    if (count === 0) {
      resultsEl.innerHTML = '<div class="table-empty">' + window.t("recall.noMatch") + '</div>';
      return;
    }

    let html = '<div class="recall-results-list">';

    memories.forEach((mem, idx) => {
      // 后端返回 memory_id 和 content
      const memoryId = mem.memory_id ?? mem.id;
      const content = mem.content || mem.text || mem.summary || "";
      // 后端返回 similarity_score，不是 score
      const score = mem.similarity_score != null ? Number(mem.similarity_score).toFixed(3) :
                    (mem.score != null ? Number(mem.score).toFixed(3) : "--");
      const importance = normalizeImportance(mem.metadata?.importance ?? 0.5).toFixed(1);
      const type = mem.metadata?.memory_type || "GENERAL";
      const status = mem.metadata?.status || "active";

      html += '<div class="result-card recall-result-item" role="button" tabindex="0" aria-label="' + esc(window.t("detail.memoryTitle", memoryId)) + '" data-memory-id="' + esc(memoryId) + '">';
      html += '<div class="result-card-header recall-result-header">';
      html += '<span class="result-rank recall-result-rank">#' + (idx + 1) + '</span>';
      html += '<span class="cell-mono recall-result-id">#' + esc(memoryId) + '</span>';
      html += '<span class="result-score-badge recall-result-score">' + esc(window.t("recall.fusedScore")) + ': ' + esc(score) + '</span>';
      html += statusPill(status);
      html += '<span class="type-tag">' + esc(type) + '</span>';
      html += '</div>';
      html += '<div class="result-content recall-result-content">' + esc(content) + '</div>';
      html += '<div class="recall-result-meta text-secondary">';
      html += '<span>' + window.t("detail.importance") + ': ' + importance + '/10</span>';
      if (mem.metadata?.session_id) {
        html += '<span>' + esc(window.t("detail.sessionId")) + ': ' + esc(String(mem.metadata.session_id)) + '</span>';
      }
      if (mem.metadata?.persona_id) html += '<span>' + esc(recallText("persona")) + ': ' + esc(String(mem.metadata.persona_id)) + '</span>';
      if (mem.metadata?.time_basis) html += '<span>' + esc(recallText("basis")) + ': ' + esc(recallText(mem.metadata.time_basis)) + '</span>';
      html += '</div>';
      if (mem.score_breakdown && typeof mem.score_breakdown === "object") {
        html += '<div class="recall-route-scores" aria-label="' + esc(window.t("recall.scoreRoutes")) + '">';
        const routes = { document_keyword_score: "graph.scoreDocKW", document_vector_score: "graph.scoreDocVec", graph_keyword_score: "graph.scoreGraphKW", graph_vector_score: "graph.scoreGraphVec" };
        Object.entries(routes).forEach(([key, label]) => { const value = Number(mem.score_breakdown[key]); if (mem.score_breakdown[key] != null && Number.isFinite(value)) html += '<span class="recall-route-score">' + esc(window.t(label)) + ': ' + value.toFixed(3) + '</span>'; });
        html += '</div>';
        if (typeof mem.score_breakdown.graph_evidence_reason === "string") {
          const tokens = Array.isArray(mem.score_breakdown.graph_evidence_matched_tokens) ? mem.score_breakdown.graph_evidence_matched_tokens.join("、") : "";
          html += '<details class="recall-evidence"><summary>' + esc(recallText("graph")) + '</summary><p>' + esc(recallText(mem.score_breakdown.graph_evidence_reason)) + '</p>' + (tokens ? '<p>' + esc(tokens) + '</p>' : '') + '</details>';
        }
      }
      html += '</div>';
    });

    html += '</div>';
    resultsEl.innerHTML = html;

    // 绑定点击事件
    resultsEl.querySelectorAll(".recall-result-item").forEach(item => {
      const open = () => {
        const memoryId = item.dataset.memoryId;
        const memory = memories.find(m => String(m.memory_id || m.id) === memoryId);
        if (memory) {
          this.peek.renderMemory({
            memory_id: memory.memory_id || memory.id,
            summary: memory.content || memory.text || memory.summary,
            content: memory.content || memory.text,
            memory_type: memory.metadata?.memory_type,
            importance: memory.metadata?.importance,
            status: memory.metadata?.status,
            created_at: memory.metadata?.create_time
              ? new Date(memory.metadata.create_time * 1000).toLocaleString()
              : "--",
            raw: memory
          });
        }
      };
      item.addEventListener("click", event => { if (!event.target.closest?.("details")) open(); });
      item.addEventListener("keydown", event => { if (!event.target.closest?.("details") && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } });
    });
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
