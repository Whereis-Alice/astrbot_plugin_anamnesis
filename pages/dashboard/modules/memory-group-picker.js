import { esc } from "./utils.js";

const GROUP_PAGE_SIZE = 50;

export class MemoryGroupPicker {
  constructor(api, onSelect) {
    this.api = api;
    this.onSelect = onSelect;
    this.selected = null;
    this.groups = [];
    this.generation = 0;
  }

  init() {
    this.dialog = document.getElementById("mem-group-dialog");
    this.trigger = document.getElementById("mem-group-picker");
    this.query = document.getElementById("mem-group-query");
    this.list = document.getElementById("mem-group-options");
    this.more = document.getElementById("mem-group-more");
    this.feedback = document.getElementById("mem-group-feedback");

    this.trigger.addEventListener("click", () => {
      this.query.value = "";
      this.dialog.showModal();
      this.load();
    });
    this.query.addEventListener("input", () => {
      ++this.generation;
      this.groups = [];
      this.hasMore = false;
      this.loading = true;
      this.error = false;
      this.renderOptions();
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.load(), 200);
    });
    this.more.addEventListener("click", () => this.load(true));
    this.list.addEventListener("click", event => {
      const option = event.target.closest("[data-memory-group]");
      if (!option) return;
      const id = option.dataset.memoryGroup;
      this.selected = this.groups.find(group => group.id === id) || null;
      this.dialog.close();
      this.render();
      this.onSelect(id);
    });
    this.dialog.addEventListener("close", () => {
      ++this.generation;
      clearTimeout(this.timer);
    });
    this.render();
  }

  async load(append = false) {
    const generation = ++this.generation;
    if (!append) {
      this.groups = [];
      this.hasMore = false;
      this.offset = 0;
    }
    this.loading = true;
    this.error = false;
    this.renderOptions();
    try {
      const data = await this.api.get("memories/groups", {
        q: this.query.value.trim(), limit: GROUP_PAGE_SIZE, offset: this.offset,
      });
      if (generation !== this.generation) return;
      const items = Array.isArray(data.items) ? data.items : [];
      const existing = new Set(this.groups.map(group => group.id));
      this.groups.push(...items.filter(group => !existing.has(group.id)));
      this.offset += items.length;
      this.hasMore = Boolean(data.has_more);
    } catch {
      if (generation !== this.generation) return;
      this.error = true;
      if (!append) this.hasMore = false;
    }
    this.loading = false;
    this.renderOptions();
  }

  render() {
    if (!this.trigger) return;
    const group = this.selected;
    const label = group ? (group.group_name || group.group_id) : window.t("filter.groupsPick");
    this.trigger.querySelector("span").textContent = label;
    this.trigger.title = group ? `${group.group_id} · ${group.platform_id}` : label;
    this.trigger.setAttribute("aria-label", group ? window.t("filter.groupsSelected", label) : label);
    this.trigger.classList.toggle("has-group", Boolean(group));
    if (this.dialog.open) this.renderOptions();
  }

  renderOptions() {
    const selected = this.selected?.id || "";
    const all = `<button type="button" class="btn btn-ghost memory-group-option" data-memory-group="" aria-pressed="${!selected}">${esc(window.t("filter.groupsAll"))}</button>`;
    this.list.innerHTML = all + this.groups.map(group => {
      const identity = `${group.group_id} · ${group.platform_id}`;
      const label = group.group_name || group.group_id;
      const detail = group.group_name ? identity : group.platform_id;
      return `<button type="button" class="btn btn-ghost memory-group-option" data-memory-group="${esc(group.id)}" aria-pressed="${group.id === selected}">
        <span class="memory-group-identity"><strong>${esc(label)}</strong><small>${esc(detail)}</small></span>
        <span class="memory-group-count">${esc(window.t("filter.groupsCount", group.count))}</span>
      </button>`;
    }).join("");
    this.feedback.textContent = this.loading ? window.t("common.loading") :
      this.error ? window.t("filter.groupsError") :
        this.groups.length ? "" : window.t("filter.groupsEmpty");
    this.more.hidden = !this.hasMore;
    this.more.disabled = this.loading;
  }
}
