/*! Lucide subset runtime. ISC license: LUCIDE_LICENSE. Icon data is injected at build time. */
(function(global) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const defaults = { xmlns: NS, width: 24, height: 24, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" };
  function createIcons(options = {}) {
    document.querySelectorAll("[data-lucide]").forEach(placeholder => {
      const name = placeholder.getAttribute("data-lucide"), icon = global.AnamIcons[name];
      if (!icon) return;
      const svg = document.createElementNS(NS, "svg");
      Object.entries({ ...defaults, ...options.attrs }).forEach(([key, value]) => svg.setAttribute(key, String(value)));
      for (const attr of placeholder.attributes) if (attr.name !== "data-lucide" && attr.name !== "class") svg.setAttribute(attr.name, attr.value);
      svg.setAttribute("class", `lucide lucide-${name} ${placeholder.getAttribute("class") || ""}`.trim());
      icon.forEach(([tag, attrs]) => { const child = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([key, value]) => child.setAttribute(key, value)); svg.appendChild(child); });
      placeholder.replaceWith(svg);
    });
  }
  global.lucide = { createIcons, icons: global.AnamIcons };
})(typeof window !== "undefined" ? window : globalThis);
