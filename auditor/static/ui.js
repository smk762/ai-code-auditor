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

  const TOKEN_KEY = "ai-audit-token";
  let memToken = null;  // fallback when sessionStorage is blocked
  function authToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || memToken; } catch (_) { return memToken; }
  }
  function forgetToken() {
    memToken = null;
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) {}
  }
  // Small modal asking for the token; resolves to the string, or null when cancelled.
  function askToken(message) {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.className = "ui-auth";
      dlg.innerHTML = `<form method="dialog">
        <h2>Sign in for actions</h2><p></p>
        <input type="password" name="token" autocomplete="current-password" aria-label="API token or auth code" required>
        <div class="row"><button value="cancel" type="button" class="secondary">Cancel</button><button value="ok" class="primary">Continue</button></div>
        <p class="hint">Kept for this browser tab only.</p></form>`;
      dlg.querySelector("p").textContent = message;
      const input = dlg.querySelector("input");
      const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
      dlg.querySelector("button.secondary").addEventListener("click", () => done(null));
      dlg.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); done(input.value.trim() || null); });
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
      document.body.appendChild(dlg);
      dlg.showModal();
      input.focus();
    });
  }

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
    SEVERITIES: ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"],
    sevBadge: (sev) => `<span class="sev sev-${esc(String(sev || "INFO").toLowerCase())}">${esc(String(sev || "INFO").toLowerCase())}</span>`,
    runStatus(st) {
      const cls = { success: "ok", completed: "ok", failed: "critical", partial_success: "warn", running: "warn", pending: "warn" }[st] || "";
      return `<span class="status ${cls}">${esc(String(st || "").replace("_", " "))}</span>`;
    },
    // Query-string state, so filtered views are deep-linkable.
    params: () => Object.fromEntries(new URLSearchParams(location.search)),
    setParams(obj) {
      const qs = new URLSearchParams(Object.entries(obj).filter(([, v]) => v !== "" && v != null)).toString();
      history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
      return qs;
    },
    // Mutating request (POST etc.). Read-only pages never need this. On 401 it asks once for
    // the API token or auth code, keeps it in sessionStorage (this tab only) and retries.
    // The secret goes in both X-API-Token and X-Auth-Code; the server accepts whichever matches.
    async send(method, url, body) {
      const attempt = async () => {
        const headers = { Accept: "application/json", "Content-Type": "application/json" };
        const tok = authToken();
        if (tok) { headers["X-API-Token"] = tok; headers["X-Auth-Code"] = tok; }
        return fetch(url, { method, headers, body: body == null ? undefined : JSON.stringify(body) });
      };
      let r = await attempt();
      if (r.status === 401) {
        const given = await askToken(authToken() ? "That token was not accepted. Try again:" : "This action needs the API token or auth code.");
        if (!given) throw new Error("cancelled: no token given");
        try { sessionStorage.setItem(TOKEN_KEY, given); } catch (_) { memToken = given; }
        r = await attempt();
        if (r.status === 401) { forgetToken(); }
      }
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 300)}`);
      return r.json();
    },
    forgetToken: () => forgetToken(),
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
