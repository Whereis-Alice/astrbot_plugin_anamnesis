/** Navigation and reading controls stay available while the host bridge connects. */
export class WorkspaceShell {
  constructor(storageKey) {
    this.storageKey = storageKey;
    this.expanded = false;
    this.mobileOpen = false;
    this.reading = false;
    this.page = "graph";
    this.mainWasInert = false;
  }

  init() {
    this.root = document.getElementById("app");
    this.sidebar = document.getElementById("workspace-sidebar");
    this.main = document.getElementById("workspace-main");
    this.toggle = document.getElementById("sidebar-toggle");
    this.close = document.getElementById("sidebar-close");
    this.backdrop = document.getElementById("sidebar-backdrop");
    this.focusToggle = document.getElementById("focus-toggle");
    this.focusLabel = document.getElementById("focus-toggle-label");
    this.pageTitle = document.getElementById("workbench-page-title");
    if (!this.root || !this.sidebar || !this.main || !this.toggle) return;
    this.mainWasInert = this.main.hasAttribute("inert");

    try { this.expanded = localStorage.getItem(this.storageKey) === "true"; } catch { /* Opaque host frames may deny storage. */ }
    this.media = window.matchMedia?.("(max-width: 760px)");
    this.mobile = this.media ? this.media.matches : window.innerWidth <= 760;
    this.toggle.addEventListener("click", () => this.toggleNavigation());
    this.close?.addEventListener("click", () => this.setMobileOpen(false));
    this.backdrop?.addEventListener("click", () => this.setMobileOpen(false));
    this.focusToggle?.addEventListener("click", () => this.setReading(!this.reading));
    document.addEventListener("keydown", event => this.handleKeydown(event), true);
    const onMediaChange = event => {
      const focusWasInSidebar = this.sidebar.contains(document.activeElement);
      this.setMobileOpen(false, false);
      this.mobile = event.matches;
      this.render();
      if (focusWasInSidebar && (this.mobile || this.reading)) this.toggle.focus();
      this.requestLayout();
    };
    if (this.media?.addEventListener) this.media.addEventListener("change", onMediaChange);
    else this.media?.addListener?.(onMediaChange);
    this.sidebar.addEventListener("transitionend", event => {
      if (event.target === this.sidebar) this.requestLayout();
    });
    window.addEventListener("languagechange", () => this.refreshLabels());
    this.render();
  }

  toggleNavigation() {
    if (this.mobile) {
      this.setMobileOpen(!this.mobileOpen);
      return;
    }
    this.expanded = !this.expanded;
    try { localStorage.setItem(this.storageKey, String(this.expanded)); } catch { /* Keep this visit usable without persistence. */ }
    this.render();
    this.requestLayout();
  }

  setMobileOpen(open, restoreFocus = true) {
    open = Boolean(open && this.mobile && !this.reading);
    if (open === this.mobileOpen) return;
    if (open) {
      this.returnFocus = document.activeElement;
      this.mainWasInert = this.main.hasAttribute("inert");
    }
    this.mobileOpen = open;
    this.render();
    if (open) {
      (this.close || this.sidebar.querySelector(".nav-item[data-page]"))?.focus();
    } else if (restoreFocus) {
      const target = this.returnFocus?.isConnected ? this.returnFocus : this.toggle;
      target?.focus({ preventScroll: true });
    }
  }

  setReading(reading) {
    this.setMobileOpen(false);
    this.reading = Boolean(reading);
    this.render();
    this.requestLayout();
  }

  setPage(page) {
    this.page = page;
    this.setMobileOpen(false);
    this.sidebar?.querySelectorAll(".nav-item[data-page]").forEach(item => {
      if (item.dataset.page === page) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    this.refreshLabels();
  }

  render() {
    if (!this.root) return;
    this.root.classList.toggle("sidebar-expanded", this.expanded);
    this.root.classList.toggle("mobile-nav-open", this.mobileOpen);
    this.root.classList.toggle("reading-mode", this.reading);
    const sidebarHidden = this.reading || (this.mobile && !this.mobileOpen);
    this.sidebar.toggleAttribute("inert", sidebarHidden);
    this.sidebar.setAttribute("aria-hidden", String(sidebarHidden));
    this.sidebar.toggleAttribute("aria-modal", this.mobileOpen);
    if (this.mobileOpen) {
      this.sidebar.setAttribute("role", "dialog");
      this.sidebar.setAttribute("aria-modal", "true");
    } else {
      this.sidebar.removeAttribute("role");
      this.sidebar.removeAttribute("aria-modal");
    }
    this.main.toggleAttribute("inert", this.mobileOpen || this.mainWasInert);
    if (this.backdrop) this.backdrop.hidden = !this.mobileOpen;
    this.toggle.setAttribute("aria-expanded", String(!this.reading && (this.mobile ? this.mobileOpen : this.expanded)));
    this.focusToggle?.setAttribute("aria-pressed", String(this.reading));
    this.refreshLabels();
  }

  text(key, fallback) {
    const value = window.t?.(key);
    return value && value !== key ? value : fallback;
  }

  refreshLabels() {
    if (!this.sidebar) return;
    const navKey = this.mobile
      ? (this.mobileOpen ? "shell.closeNavigation" : "shell.openNavigation")
      : (this.expanded ? "shell.collapseSidebar" : "shell.expandSidebar");
    const navFallback = this.mobile
      ? (this.mobileOpen ? "Close navigation" : "Open navigation")
      : (this.expanded ? "Collapse sidebar" : "Expand sidebar");
    this.toggle.dataset.i18nAria = navKey;
    this.toggle.setAttribute("aria-label", this.text(navKey, navFallback));
    this.toggle.title = this.text(navKey, navFallback);
    const focusKey = this.reading ? "shell.exitFocus" : "shell.focus";
    const focusText = this.text(focusKey, this.reading ? "Exit focus" : "Focus");
    if (this.focusLabel) {
      this.focusLabel.dataset.i18n = focusKey;
      this.focusLabel.textContent = focusText;
    }
    if (this.focusToggle) {
      this.focusToggle.dataset.i18nTitle = focusKey;
      this.focusToggle.title = focusText;
      this.focusToggle.setAttribute("aria-label", focusText);
    }
    this.sidebar.querySelectorAll(".nav-item[data-page]").forEach(item => {
      const key = "nav." + item.dataset.page;
      const label = this.text(key, item.querySelector("span")?.textContent || item.dataset.page);
      item.title = label;
      item.setAttribute("aria-label", label);
      if (item.dataset.page === this.page && this.pageTitle) {
        this.pageTitle.dataset.i18n = key;
        this.pageTitle.textContent = label;
      }
    });
  }

  handleKeydown(event) {
    if (!this.mobileOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      this.setMobileOpen(false);
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...this.sidebar.querySelectorAll('button:not([disabled]), summary, a[href], input:not([disabled]), select:not([disabled]), [tabindex="0"]')]
      .filter(item => item.getClientRects().length && !item.closest("[inert]"));
    const first = items[0], last = items[items.length - 1];
    if (!first) return;
    if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last) || !this.sidebar.contains(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  }

  requestLayout() {
    if (this.layoutFrame) return;
    this.layoutFrame = window.requestAnimationFrame(() => {
      this.layoutFrame = 0;
      window.dispatchEvent(new Event("resize"));
    });
  }
}
