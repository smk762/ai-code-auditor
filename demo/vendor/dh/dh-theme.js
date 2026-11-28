/*! dh-theme v1.2.12 | Dragonhound Labs light/dark theme, shared across *.dragonhound.dev.
 *
 * Load it as a plain, blocking script in <head>, before the stylesheet if you can:
 *     <meta name="color-scheme" content="light dark">
 *     <script src="/dh-theme.js"></script>
 * It is an external file with no inline code, no eval and no HTML creation, so 'self' in script-src
 * covers it under a hash- or nonce-based CSP and Trusted Types is unaffected. Running in <head>
 * sets data-theme before first paint, so there is no flash of the wrong theme.
 *
 * Model (two-state, system default): no stored value means "follow prefers-color-scheme".
 * Toggling stores an explicit override; toggling back to the system's own scheme clears it.
 * Storage: a "dh-theme" cookie on Domain=.dragonhound.dev (so the choice follows you between
 * dragonhound.dev, studio., lightbox., atlas., cull., realms., safelight., assay., sightline., mask., quarry., proof., iris., retina.), host-only on any other host (localhost, previews).
 * The cookie is set only when someone uses the toggle. A legacy localStorage "theme" value
 * (the landing page's old key) is read once and migrated. Where the cookie can't be stored (cookies
 * blocked, an opaque-origin frame, a rejected Domain), that key holds the choice instead, and a
 * missing cookie never undoes the choice made in this page.
 *
 * Toggle: any <button data-dh-theme-toggle> gets click handling (delegated) and aria-pressed
 * (pressed = dark), including toggles mounted later by client-side rendering.
 * Change event: document receives "dh-theme-change" with detail { theme, stored, source }:
 *   theme = the effective "light" | "dark"; stored = the override or null;
 *   source = "toggle" | "api" | "system" (OS scheme changed) | "sync" (cookie changed in another tab or subdomain).
 * API: window.dhTheme = { get(), effective(), set("light" | "dark" | null), subscribe(fn) -> unsubscribe }.
 * Locked sites (a single-theme demo): <html data-theme="dark" data-dh-theme-lock> and the script leaves the
 * theme alone (set() does nothing, subscribe() never fires). Such a site may skip this file entirely.
 *
 * Demo strip: data-dh-site="<site>" on <html> or on a .dh-strip puts aria-current="page" on that site's
 * strip link (dh-strip.css shows the visual state from the same attribute, also without JS) and, where the
 * link list scrolls (phones, 1.2.5), scrolls that link into view.
 */
(() => {
  const root = document.documentElement;
  const TOGGLE = "[data-dh-theme-toggle]", STRIP = ".dh-strip";
  const valid = (t) => (t === "light" || t === "dark" ? t : null);
  const locked = root.hasAttribute("data-dh-theme-lock");
  const system = matchMedia("(prefers-color-scheme: dark)");
  const effective = () => valid(root.dataset.theme) || (system.matches ? "dark" : "light");

  // ---- Demo strip: current-site marking ----
  // On phones the link list scrolls sideways (1.2.5): bring the current site's link into view, without moving the page.
  const reveal = (a) => {
    const ul = a.closest(".dh-strip-links");
    if (!ul || ul.scrollWidth <= ul.clientWidth) return;
    const u = ul.getBoundingClientRect(), r = a.getBoundingClientRect(), pad = 24;
    if (r.right > u.right - pad) ul.scrollLeft += r.right - u.right + pad;
    else if (r.left < u.left + pad) ul.scrollLeft -= u.left - r.left + pad;
  };
  const markStrip = (nav) => {
    const site = nav.getAttribute("data-dh-site") || root.getAttribute("data-dh-site");
    if (!site) return;
    nav.querySelectorAll("a[data-dh-site]").forEach((a) => {
      if (a.getAttribute("data-dh-site") === site) { a.setAttribute("aria-current", "page"); reveal(a); }
      else if (a.getAttribute("aria-current") === "page") a.removeAttribute("aria-current");
    });
  };

  // ---- Theme ----
  const KEY = "dh-theme", FAMILY = "dragonhound.dev", YEAR = 31536000, EVENT = "dh-theme-change";
  const host = location.hostname;
  const domain = host === FAMILY || host.endsWith("." + FAMILY) ? "; Domain=." + FAMILY : "";
  const secure = location.protocol === "https:" ? "; Secure" : "";
  const LEGACY = "theme"; // the landing page's old localStorage key; the fallback store when the cookie can't be kept
  // document.cookie throws SecurityError in an opaque-origin document (a sandboxed iframe): read that as "nothing stored".
  const readCookie = () => {
    try { return valid((document.cookie.match(/(?:^|;\s*)dh-theme=(light|dark)/) || [])[1]); } catch { return null; }
  };
  // True when the cookie now holds t: a blocked or rejected cookie is dropped silently, so read it back.
  const writeCookie = (t) => {
    try {
      document.cookie = KEY + "=" + (t || "") + "; Path=/; SameSite=Lax" + domain + secure + "; Max-Age=" + (t ? YEAR : 0);
    } catch { return false; }
    return readCookie() === t;
  };
  let saved = true; // false while the last choice isn't in the cookie: then the cookie is no source of truth
  const store = (t) => {
    saved = writeCookie(t);
    try { // the old key goes once the cookie holds the choice; until then it keeps the choice across loads
      if (saved || !t) localStorage.removeItem(LEGACY); else localStorage.setItem(LEGACY, t);
    } catch { /* storage blocked */ }
  };
  const read = () => {
    const c = readCookie();
    if (c) return c;
    let raw = null;
    try { raw = localStorage.getItem(LEGACY); } catch { /* storage blocked */ }
    if (raw === null) return null;
    const old = valid(raw);
    if (old) store(old); // to the cookie, or kept in the old key while the cookie can't be stored
    else try { localStorage.removeItem(LEGACY); } catch { /* storage blocked */ }
    return old;
  };
  const press = (el) => el.setAttribute("aria-pressed", String(effective() === "dark"));
  const sync = () => {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = root.dataset.theme || "light dark";
    document.querySelectorAll(TOGGLE).forEach(press);
  };
  const state = () => effective() + "|" + valid(root.dataset.theme);
  let last = "";
  const apply = (t, source) => {
    t = valid(t);
    if (t) root.dataset.theme = t; else delete root.dataset.theme;
    sync();
    if (state() === last) return;
    last = state();
    if (source) document.dispatchEvent(new CustomEvent(EVENT, { detail: { theme: effective(), stored: t, source } }));
  };
  const set = (t, source = "api") => {
    if (locked) return;
    t = valid(t);
    store(t);
    apply(t, source);
  };
  const subscribe = (fn) => {
    const h = (e) => fn(e.detail);
    document.addEventListener(EVENT, h);
    return () => document.removeEventListener(EVENT, h);
  };
  window.dhTheme = { get: () => valid(root.dataset.theme), effective, set: (t) => set(t), subscribe };

  if (!locked) {
    apply(read()); // before first paint; no event (nothing can be listening yet)
    document.addEventListener("click", (e) => {
      const btn = e.target instanceof Element && e.target.closest(TOGGLE);
      if (!btn) return;
      const sys = system.matches ? "dark" : "light";
      const target = effective() === "dark" ? "light" : "dark";
      set(target === sys ? null : target, "toggle");
    });
    system.addEventListener("change", () => apply(root.dataset.theme, "system"));
    // Another subdomain may have changed the cookie while this tab was in the background or bfcache.
    // Not when this page's own choice never reached the cookie: its absence would undo that choice.
    const recheck = () => { if (saved) apply(readCookie(), "sync"); };
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") recheck(); });
    addEventListener("pageshow", (e) => { if (e.persisted) recheck(); });
  }

  // ---- Toggles and strips mounted later (client-side navigation, hydration) ----
  const ready = () => {
    if (!locked) sync();
    document.querySelectorAll(STRIP).forEach(markStrip);
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "attributes") { // data-dh-site set or changed on <html> or on a strip
          if (r.target === root) document.querySelectorAll(STRIP).forEach(markStrip);
          else if (r.target.matches(STRIP)) markStrip(r.target);
          continue;
        }
        for (const n of r.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (!locked) { if (n.matches(TOGGLE)) press(n); n.querySelectorAll(TOGGLE).forEach(press); }
          const strip = n.closest(STRIP); // a strip, or links re-rendered inside one
          if (strip) markStrip(strip);
          n.querySelectorAll(STRIP).forEach(markStrip);
        }
      }
    }).observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-dh-site"] });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready, { once: true });
  else ready();
})();
