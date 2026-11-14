// Shared helpers for audit-api pages: fetch/render, formatting, theme toggle.
// Loaded as a plain script (no build step); exposes window.UI.
(function () {
  const THEME_KEY = "ai-audit-theme";

  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (_) { return null; }
  }
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  applyTheme(storedTheme());

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const UI = {
    esc,
    css: (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim(),
    money: (x) => x == null ? "—" : (x >= 100 ? "$" + x.toFixed(0) : (x === 0 || x >= 0.01) ? "$" + x.toFixed(2) : "$" + x.toFixed(3)),
    int: (x) => (x || 0).toLocaleString(),
    plural: (n, word) => `${(n || 0).toLocaleString()} ${word}${n === 1 ? "" : "s"}`,
    ago(iso) {
      if (!iso) return "—";
      const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
      if (s < 60) return "just now";
      if (s < 3600) return `${Math.floor(s / 60)} min ago`;
      if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
      return `${Math.floor(s / 86400)} d ago`;
    },
    duration(ms) {
      if (ms == null) return "—";
      if (ms < 1000) return `${Math.round(ms)}ms`;
      const s = Math.round(ms / 1000);
      if (s < 60) return `${s}s`;
      if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
      return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    },
    async getJSON(url) {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
      return r.json();
    },
    // Fetch JSON and hand it to render(); on failure, show the error in errEl.
    async load(url, render, errEl) {
      try {
        render(await UI.getJSON(url));
      } catch (e) {
        if (errEl) errEl.innerHTML = `<span class="err">Could not load ${esc(url)}: ${esc(e.message)}</span>`;
        else console.error(e);
      }
    },
    // Called on OS or manual theme change, so canvas charts can re-read tokens.
    onThemeChange(fn) {
      matchMedia("(prefers-color-scheme: dark)").addEventListener("change", fn);
      document.addEventListener("ui:themechange", fn);
    },
  };

  // Theme button (rendered by the server-side nav partial): auto → light → dark → auto.
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("ui-theme");
    if (!btn) return;
    const label = () => { btn.textContent = `Theme: ${storedTheme() || "auto"}`; };
    label();
    btn.addEventListener("click", () => {
      const order = [null, "light", "dark"];
      const next = order[(order.indexOf(storedTheme()) + 1) % order.length];
      try { next ? localStorage.setItem(THEME_KEY, next) : localStorage.removeItem(THEME_KEY); } catch (_) {}
      applyTheme(next);
      label();
      document.dispatchEvent(new Event("ui:themechange"));
    });
  });

  window.UI = UI;
})();
