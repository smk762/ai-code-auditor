# Dragonhound shared design layer (dh-shared v1, currently 1.2.12)

The one source of truth for what the Dragonhound Labs sites share: dragonhound.dev (this repo,
the reference look), Studio, Lightbox, Atlas, Cull, Realms, Safelight, Assay, Sightline, Mask, Quarry, Proof and Iris. Each repo vendors a pinned copy, so there is no
runtime dependency and no CSP change. A checksum keeps the copies honest.

| File | What it is |
|---|---|
| `dh-tokens.css` | CSS custom properties, light and dark: colour, type, space, radius, elevation, focus, motion |
| `dh-badge.css` | The demo-state badge (`.dh-badge`), styled from the tokens |
| `dh-strip.mjs` | The family bar's source: links, labels and mark as an ES module (for React/Next), plus the HTML renderer |
| `dh-strip.d.mts` | TypeScript declarations for `dh-strip.mjs` (1.2.1), vendored next to it so a typed import needs no `// @ts-ignore` |
| `dh-strip.html` + `dh-strip.css` | The family bar for the top of each demo (HTML + CSS, works without JS); generated from `dh-strip.mjs` |
| `dh-theme.js` | Light/dark that follows you across `*.dragonhound.dev` (cookie), change event and `dhTheme` API, strip current-site marking; CSP-safe |
| `dh-mark.svg` | The logo mark, simplified for small sizes (1.2 KB; same shape as `src/data/logo.ts`) |
| `favicons/` | The lab's own icons (dragonhound.dev): `favicon.svg` (dark-tab aware), `favicon.ico` (16+32), `apple-touch-icon.png` (180), `icon-192/512.png`, `site.webmanifest`. Not vendored unless asked (see Favicons) |
| `manifest.json` | Version, per-file sha256 and the set checksum (written by `--stamp`) |

Published read-only at `https://dragonhound.dev/brand/v1/` (same files plus `sync-shared.mjs`),
so a target repo's CI can compare against `https://dragonhound.dev/brand/v1/manifest.json`.

## Tokens

All names start with `--dh-`. Use the tokens; don't copy their values.

- **Colour** (light / dark, switched by `prefers-color-scheme` and overridden by `<html data-theme>`):
  `bg`, `surface`, `surface-2`, `text`, `muted`, `border`, `accent`, `on-accent`, `highlight`, `focus`,
  `success|warn|danger` each with `-ink` and `-tint`, and `demo-ink`, `demo-tint`, `demo-border`.
  - `--dh-stage-data|training|apps|foundation` (1.1.0): pipeline stage colours (data teal, training red, apps ochre,
    foundation blue). In Argus package marks they colour the single eye; any UI can use them to mark a package's stage.
    Marks and rules live in `brand/` in argus-cortex. Not for UI chrome such as links, buttons or focus.
  - **Text on a tint uses the `-ink` token**, never the fill colour. The status inks are checked against two tints: the
    `-tint` token, and a 20% mix of the fill over the background or surface (`color-mix(in srgb, var(--dh-warn) 20%, var(--dh-bg))`,
    which some sites use instead). Lowest ratios (1.2.0):

    | Ink | on `-tint` | on 20% fill over bg / surface | on bg / surface |
    |---|---|---|---|
    | light success `#14532d` | 8.0 | 6.7 / 6.9 | 8.8 / 9.1 |
    | light warn `#7a4f00` (was `#8a5a00`: 4.3 on the 20% mix) | 6.5 | 5.2 / 5.4 | 6.9 / 7.1 |
    | light danger `#7f1d1d` | 8.8 | 6.8 / 7.1 | 9.7 / 10.0 |
    | dark success `#a7e8bd` | 10.9 | 9.1 / 8.2 | 13.1 / 12.0 |
    | dark warn `#f0c36a` | 9.0 | 7.6 / 6.9 | 11.2 / 10.3 |
    | dark danger `#fbb4b4` | 9.9 | 8.0 / 7.1 | 10.8 / 9.9 |

    A stronger tint than 20% is not covered: check it before you use one.
  - `--dh-cat-1..8` (1.2.0), each with `-ink` and `-tint`: a categorical palette for facets, tags and chart series
    (blue, orange, green, red, magenta, olive, sky, slate, in that order; use them in order). Light and dark values are
    picked for separation under deuteranopia, protanopia and tritanopia (minimum OKLab distance 0.07 light, 0.08 dark,
    across all three simulations). Each fill is at least 3:1 on `bg` and `surface` (non-text: dots, bars, borders; lowest
    3.3 light, 4.5 dark). `-tint` is 20% of the fill over `--dh-surface` (a `color-mix()`); text on it uses `-ink`
    (at least 5.5:1 light, 7.0:1 dark, also on a 20% mix over bg, and at least 6.8:1 on bg/surface). Colour is never the
    only cue: keep the facet's label next to it. For a status meaning, use success/warn/danger instead.

    ```css
    .facet[data-group="1"] { background: var(--dh-cat-1-tint); color: var(--dh-cat-1-ink); border-color: var(--dh-cat-1); }
    ```
  - `--dh-accent` is for links, buttons and icons. A product may override **only** `--dh-accent` and
    `--dh-on-accent` (Studio purple, Lightbox indigo, Atlas amber; Cull keeps the family accent), at `:root`, in both schemes, and must keep 4.5:1 on `--dh-surface`.
  - `--dh-highlight` (#003366 light, #1a4d80 dark) is the card hover border and glow, and the landing logo's outline trace.
    In dark mode it is 2.1:1 on the background, so it is decorative only.
  - `--dh-focus` is the focus-ring colour: the highlight in light mode (12.2:1), a lighter tint #4a86c4 in dark mode (4.8:1 on bg, 4.4:1 on surface).
- **Type:** `--dh-font` (system-first sans, no webfont), `--dh-mono`; sizes `--dh-text-2xs` 12 px,
  `xs` 13, `sm` 14, `md` 15, `base` 16, `lg` 17, `xl` 18, `2xl` 20, `3xl` 24, `display` (clamp 1.6 to 2.4 rem);
  `--dh-leading` 1.6, `--dh-leading-tight` 1.2, `--dh-weight-semibold|bold`.
  - **Never set `font` (or `font-size`) on `html`.** The sizes are in rem, so a smaller root size shrinks every token
    (a `font: 14px/1.4 …` on `html` turns `--dh-text-2xs` into 10.5 px). Set the family and size on `body` instead:
    `body { font: var(--dh-text-base)/var(--dh-leading) var(--dh-font); }`.
  - **Text-size floor for dense tool UIs** (tables, inspectors, facet lists): nothing below `--dh-text-2xs` (12 px) on
    phones, and keep running text at `--dh-text-sm` or larger. If a desktop layout uses 11 px labels, raise them to
    `--dh-text-2xs` below 560 px.
  - **Map labels:** the floor applies to UI text (controls, tables, legends, popups). Labels drawn on a map canvas
    follow cartographic hierarchy instead and may go down to 11 px for minor places, provided they have a halo
    (a contrasting outline or glow) so they stay legible over any terrain.
- **Space:** `--dh-space-1|2|3|4|5|6|8|10|12|16` = 4 to 64 px on a 4 px base.
- **Radius:** `--dh-radius-sm` 6, `md` 8 (buttons), `lg` 10 (cards), `xl` 14, `pill`.
- **Elevation:** `--dh-shadow-1`, `--dh-shadow-hover` (the card hover: drop shadow plus a 1 px highlight ring in light mode, a highlight glow in dark mode), `--dh-shadow-color`, `--dh-glow`.
- **Focus:** `outline: var(--dh-focus-ring); outline-offset: var(--dh-focus-offset);` (2 px, 3 px offset).
- **Motion:** `--dh-dur-fast` .15 s (colour), `--dh-dur` .2 s (border, shadow, lift), `--dh-dur-slow` .35 s (image zoom),
  `--dh-dur-draw` 1.3 s (one-off reveals); `--dh-ease`, `--dh-ease-out` (entering), `--dh-ease-in` (leaving).
- **Layout:** `--dh-max` 1080 px, `--dh-gutter` 16 px, `--dh-strip-h` 30 px.

**Card hover, the house style:**

```css
.card { transition: border-color var(--dh-dur), box-shadow var(--dh-dur), transform var(--dh-dur); }
.card:is(:hover, :focus-within) { border-color: var(--dh-highlight); box-shadow: var(--dh-shadow-hover); transform: translateY(var(--dh-lift)); }
.card :focus-visible { outline: var(--dh-focus-ring); outline-offset: var(--dh-focus-offset); }
```

**Reduced motion:** movement goes through `--dh-lift`, `--dh-zoom` and `--dh-dur-draw`, which the tokens set
to `0px`, `1` and `0s` under `prefers-reduced-motion: reduce`. Colour and opacity fades keep their
durations. So write `transform: translateY(var(--dh-lift))` and never a literal pixel lift, and you don't
need your own reduced-motion block for hover effects.

**Tailwind 4:** map in `@theme inline`, e.g. `--color-bg: var(--dh-bg); --color-accent: var(--dh-accent); --radius-lg: var(--dh-radius-lg); --font-sans: var(--dh-font);`.

## Demo-state vocabulary

Four labels replace the seven found in the audit ("RECORDED", "DEMO", "DEMO SAMPLE", "NOT IN DEMO",
"COMING SOON", "Recorded pipeline run", "demo on recorded data"). Sentence case, one badge component, a
`title` that says what it means.

| Label | Class | Use when | Example `title` |
|---|---|---|---|
| **Recorded** | `.dh-badge` | Real output from a real run, replayed. The default for every demo page. | "Recorded output from a real run; no model runs while you browse" |
| **Sample data** | `.dh-badge dh-badge--sample` | Illustrative or synthetic input or output, not from a real run. | "Synthetic sample to show the layout; not real output" |
| **In-browser** | `.dh-badge dh-badge--browser` | Computed live on the client, from your input, right now. | "Runs in your browser; nothing is sent anywhere" |
| **Not in this demo** | `.dh-badge dh-badge--off` | The feature needs the real backend (GPU, model, writes). Show the control disabled with this badge. | "Needs the self-hosted backend; see the README" |

```html
<span class="dh-badge" title="Recorded output from a real run; no model runs while you browse">Recorded</span>
```

Retired: "Demo", "Demo sample", "Coming soon" (inside a demo), "Recorded pipeline run", "demo on recorded data".
The strip's own label is "Live demo · recorded output" ("Live demo" on phones). A demo that computes in the visitor's
browser instead (Realms) sets `data-dh-label="browser"` for "Live demo · runs in your browser" (1.2.4; see Demo strip).

## Demo strip

One source, two forms: `dh-strip.mjs` exports the links, labels and mark (`strip`), plus `stripHtml({ current, label })`.
`dh-strip.html` is generated from it by `npm run shared:stamp`, and the build fails if the two drift.

- **Static HTML:** paste `dh-strip.html` as the first element in `<body>` after the skip link, include `dh-strip.css`
  after the tokens, and say which site this is with **`data-dh-site="studio"`** (or `lightbox`, `atlas`, `cull`, `realms`, `safelight`, `assay`, `sightline`, `mask`, `quarry`, `proof`, `iris`, `retina`) on `<html>`
  or on the `<nav class="dh-strip">`. Don't edit the markup: `dh-strip.css` shows the current link from that
  attribute (no JS needed), and `dh-theme.js` adds `aria-current="page"` to it, also for strips mounted later.
  A hand-set `aria-current` still works.
- **Label suffix (1.2.4):** the default is "· recorded output". A site whose demo runs in the visitor's browser adds
  **`data-dh-label="browser"`** ("· runs in your browser") next to `data-dh-site`, on `<html>` or on the nav; a nav's own
  attribute wins over `<html>`. The pasted markup carries every suffix (`<span class="dh-strip-more" data-dh-label="…">`,
  all but the default `hidden`) and `dh-strip.css` shows the chosen one, so there is still nothing to edit and no JS.
  The keys and texts live in `strip.labelSuffixes` (`strip.labelMore` is the default); a new suffix is a dh-shared
  change, not a local patch. Natively, render `strip.labelSuffixes.browser` in place of `strip.labelMore`, or call
  `stripHtml({ label: "browser" })` for a string with only that suffix.
- **React / Next:** `import { strip } from "./vendor/dh/dh-strip.mjs"` and render it natively (the file header
  has a ready JSX snippet); set `aria-current="page"` on your own link. No HTML string replacement.
  TypeScript picks up the types from `dh-strip.d.mts` next to it (1.2.1; for a `.mjs` import TypeScript looks for `.d.mts`, not `.d.ts`).

It is a `<nav aria-label="Dragonhound Labs demos">` landmark, 30 px tall. Links are same-tab (all `*.dragonhound.dev` hosts are internal: make sure your
external-link rewriter treats them that way). Below 1168 px (1.2.12; 1112 px in 1.2.11, 1072 px in 1.2.10, 1024 px in 1.2.8, 968 px in 1.2.7, 880 px in 1.2.5, 720 px in 1.2.4, 640 px in 1.2.2 and 1.2.3, 560 px before) the "Dragonhound Labs" text and
the label suffix are visually hidden (still read out). Below 856 px (1.2.12; 800 px in 1.2.11, 768 px in 1.2.10, 720 px in 1.2.8, 664 px in 1.2.7, 600 px in 1.2.5) the thirteen links no longer fit, so the link list scrolls
sideways on its own (1.2.5) while the mark and "Live demo" stay put; its edges fade while there is more to scroll that way
(scroll-driven animations; browsers without them clip the list at the edge), and `dh-theme.js` scrolls the current
site's link into view. Spacing tightens below 420 px, and below 360 px the strip text drops to 12 px. The bar never wraps.
If your header is sticky, offset it by `var(--dh-strip-h)` or put the strip inside it.

## Theme

```html
<meta name="color-scheme" content="light dark">
<script src="/dh-theme.js"></script>            <!-- blocking, in <head>: sets data-theme before first paint -->
...
<button type="button" data-dh-theme-toggle aria-label="Dark theme" aria-pressed="false">…</button>
```

- External file, no inline code: covered by `script-src 'self'` under a hash or nonce CSP, and it creates no HTML (Trusted Types safe).
  With a nonce policy that has `'strict-dynamic'`, give the tag your nonce.
- Two states, system default: toggling stores an override in a `dh-theme` cookie (`Domain=.dragonhound.dev; Path=/; SameSite=Lax; Secure`, one year); toggling back to the system scheme deletes it. On other hosts (localhost, `*.pages.dev`) the cookie is host-only.
- It re-reads the cookie when a tab becomes visible again, so a change on another subdomain carries over.
- The landing page's old `localStorage.theme` is migrated once. A site with its own 3-state toggle should drop it for this one.
- Where the cookie can't be stored (cookies blocked, a sandboxed or opaque-origin frame, a rejected `Domain`), the toggle still works: the choice holds in the page, is kept in `localStorage.theme` across loads, and a refocus never resets it to the system scheme (1.2.9).
- Dark-only site (Lightbox): `<html data-theme="dark" data-dh-theme-lock>`, no toggle (and see "locked sites" below).
- Next.js: put the tag in the root layout's `<head>` as a plain `<script src>` (not `next/script` with `afterInteractive`), and add `suppressHydrationWarning` to `<html>`.
- **Change notification:** `document` receives a `dh-theme-change` CustomEvent with
  `detail = { theme, stored, source }`: `theme` is the effective `"light"`/`"dark"`, `stored` the override or `null`,
  `source` one of `"toggle"`, `"api"`, `"system"` (OS scheme changed), `"sync"` (cookie changed on another subdomain or tab).
  It fires only when something changed, and not for the initial state (read that with `effective()`).
- **API:** `window.dhTheme.get()` (the override or `null`), `.effective()` (`"light"`/`"dark"`), `.set("light" | "dark" | null)`,
  `.subscribe(fn)` returning an unsubscribe function. React: `useSyncExternalStore(dhTheme.subscribe, dhTheme.effective, () => "light")`.
- **Toggles mounted later** (client-side navigation, hydration) get `aria-pressed` kept in sync automatically (a
  `MutationObserver` in `dh-theme.js`; clicks are delegated), so no per-site workaround is needed.
- **Single-theme (locked) sites can skip `dh-theme.js`.** It only matters for the toggle and the cookie; set
  `data-theme` on `<html>` yourself. Vendor with `--no-theme-script` (1.2.1) to leave the file out: the lock records
  it under `omitted` (with `"noThemeScript": true`), so `--check` and `--against` still pass. Pass the flag on every re-sync. Load it anyway only if you use the static `dh-strip.html` and want `aria-current`
  (the visual state works without it). With `data-dh-theme-lock` the script leaves the theme alone, `set()` does
  nothing and `subscribe()` never fires.
- The cookie must be mentioned anywhere a site says "no cookies".

## Favicons

**These are the lab's own icons, for dragonhound.dev.** The **Argus apps (Studio, Lightbox, Atlas, Cull) use the official
favicon sets from argus-cortex `brand/`**, copied unchanged (all rights reserved), and must not use the Dragonhound
lab favicons. So `sync-shared.mjs` leaves `favicons/` out of a vendored copy unless you pass `--with-favicons`
(the lock records the left-out files, so `--check` and `--against` still work).

A site that does use the lab set copies `favicons/*` to its root and adds:

```html
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
```

`favicon.svg` is the mark in ink #1c1f22 with a `prefers-color-scheme: dark` rule inside (light ink #e6e8ea).
The PNG and ICO sizes sit on the brand tile #1d5f74 so they read on any tab bar. Regenerate with
`npm i --no-save playwright && node scripts/build-favicons.mjs` (writes `shared/favicons/` and the landing's `public/`).
For another monochrome mark: `node scripts/build-favicons.mjs --mark mark.svg --out dir [--tile "#hex"] [--name "Name"] [--short-name "Short"]`
writes the same six files to `dir` only.

## Versioning, checksum and sync

- `manifest.json` holds the version (semver: patch = fix, minor = new token or class, major = rename or removal; a major bump also moves the published path to `/brand/v2/`), each file's sha256 (text hashed with LF line endings) and `checksum`, the sha256 of the sorted `"<sha>  <path>"` lines. `dh-tokens.css` also carries its own body checksum in its header.
- After any change here: `npm run shared:stamp` (add `-- --version 1.3.0` to bump). It also regenerates `dh-strip.html` and the current-site selectors in `dh-strip.css` from `dh-strip.mjs`. `npm run build` runs `shared:check` first and fails if the manifest, the header checksum or the landing's copies in `public/` are stale.
- Vendor into another repo (from this repo): `node scripts/sync-shared.mjs ../argus-studio --dest frontend/vendor/dh` (default dest `vendor/dh`). It copies the files (without `favicons/` unless `--with-favicons`, and without `dh-theme.js` if `--no-theme-script`) plus `manifest.json` and `sync-shared.mjs`, writes `.dh-shared.json` (version, checksum, dest, file hashes, left-out files) at the repo root, and deletes files an earlier sync vendored that are no longer in the set.
- Check in the target repo: `node vendor/dh/sync-shared.mjs --check .` fails if a vendored file was edited; add `--against https://dragonhound.dev/brand/v1/manifest.json` to also fail when it no longer matches the published source (`--allow-outdated` turns a newer upstream version into a warning).

- Test page (strip marking, `dhTheme` events and API, categorical swatches) in this repo: `node tests/shared/serve.mjs`, then open `http://localhost:4330/tests/shared/` (served with the site's CSP; it reports pass/fail on the page).

CI snippet for a target repo (GitHub Actions):

```yaml
  dh-shared:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22 }
      - run: node vendor/dh/sync-shared.mjs --check . --against https://dragonhound.dev/brand/v1/manifest.json --allow-outdated
```

## Adoption checklist (WP6 Studio, WP7 Lightbox and Realms)

1. Vendor: `node scripts/sync-shared.mjs <repo> [--dest …]`, commit `vendor/dh/` and `.dh-shared.json`, add the CI job above.
2. Tokens: load `dh-tokens.css` before the site's CSS (Tailwind: `@import` it, then map `@theme inline` to `--dh-*`). Replace local neutrals, radius, focus and status colours with tokens; keep only the product `--dh-accent`/`--dh-on-accent` override. Text on tints uses `-ink`.
3. Type: drop webfonts for `--dh-font` (Studio may keep Geist Mono for code via `--dh-mono` override, documented).
4. Theme: `dh-theme.js` in `<head>`, `data-dh-theme-toggle` on the toggle; remove the old theme code and any inline theme bootstrap (and its CSP hash). Lightbox: `data-theme="dark" data-dh-theme-lock`.
5. Strip: replace DemoBanner / the demo pill with the strip (`dh-strip.html`, or `dh-strip.mjs` rendered natively) + `dh-strip.css`, `data-dh-site="<site>"` on `<html>` (or `aria-current="page"` on your link in JSX), after the skip link, same-tab links.
6. Badges: `dh-badge.css` and the four labels above; remove the retired ones.
7. Favicons: Argus apps use the official set from argus-cortex `brand/` (not `favicons/` here); delete the old icons (Studio: the Next.js default).
8. Cards (where a site has them): the card-hover snippet above.
9. Verify: 390 px and 1440 px in both themes, toggle on one subdomain and reload another (cookie `Domain=.dragonhound.dev`), zero CSP errors, Lighthouse unchanged or better.

## Changelog

- **1.2.12**
  - `dh-strip.mjs`: Retina (`retina`, https://retina.dragonhound.dev/, argus-retina) joins the strip after Iris.
    `dh-strip.html`, the current-site rules in `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: the phone layout applies below 1168 px (was 1112 px) and the link list scrolls below 856 px (was
    800 px). Measured in Chromium with DejaVu Sans, as 1.2.11 was: the new link adds 54 px, so thirteen links need
    1158 px (1104 px for twelve), and 846 px without the name and suffix (792 px for twelve).
  - `dh-theme.js` and the landing's external-link rule: comments name the new site. The test page has the new link
    and the new phone width.

- **1.2.11**
  - `dh-strip.mjs`: Iris (`iris`, https://iris.dragonhound.dev/, argus-iris) joins the strip after Proof. `dh-strip.html`,
    the current-site rules in `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: the phone layout applies below 1112 px (was 1072 px) and the link list scrolls below 800 px (was
    768 px). Measured in Chromium with DejaVu Sans, a wider system font than 1.2.10's: twelve links need 1104 px
    (1072 px for eleven), and 792 px without the name and suffix (761 px for eleven).
    Below 360 px links are at least 24 px wide ("Iris" was 22 px). Measured at every width from 320 to 1160 px and at
    1280 and 1440 px, both suffixes, each site current (20,232 cases): no page or bar overflow, links at least 24 px wide.
  - `dh-theme.js` and the landing's external-link rule: comments name the new site. The test page has the new link
    and the new phone width.

- **1.2.10**
  - `dh-strip.mjs`: Proof (`proof`, https://proof.dragonhound.dev/, argus-proof) joins the strip after Quarry:
    Lightbox, Studio, Atlas, Cull, Realms, Safelight, Assay, Sightline, Mask, Quarry, Proof. `dh-strip.html`, the
    current-site rules in `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: eleven links, the current one semibold, and the longer suffix need 1000 px in Chromium with Linux
    system fonts (955 px for ten), so the phone layout (mark only, short label) applies below 1072 px (was 1024 px),
    keeping the same font headroom as 1.2.8. Without the name and suffix the eleven links need 724 px (at 720 px the
    bar overflowed by up to 4 px), so the link list scrolls sideways below 768 px (was 720 px). The 420 px and 360 px
    tightening is unchanged. Measured in Chromium at every width from 320 to 1100 px and at 1280 and 1440 px, both
    suffixes, each site current: no page or bar overflow, "Live demo" always on screen, the current link always in
    view, links at least 25 px wide.
  - `dh-theme.js` and the landing's external-link rule: comments name the new site. The test page has the new link
    and the new phone width. The README's opening list names Proof.

- **1.2.9**
  - `dh-theme.js`: reading or writing `document.cookie` no longer throws out of the script. In an opaque-origin
    document (a sandboxed iframe without `allow-same-origin`) the getter throws `SecurityError`, which used to stop
    start-up before the toggle handling, `aria-pressed` and strip marking; it now reads as "nothing stored".
  - `dh-theme.js`: a choice whose cookie was dropped (cookies blocked, a rejected `Domain`) is no longer undone when
    the tab becomes visible again or comes back from bfcache, and no spurious `"sync"` event fires. Every write is read
    back. While the cookie doesn't hold the choice, it is kept in `localStorage.theme` (the old landing key) across
    loads, and that key is only removed once the cookie holds it, so the one-time migration no longer loses the value
    when the cookie is refused. A page with working cookies behaves as before.
  - Test page: the dropped-cookie and throwing-cookie cases, and an opaque-origin frame (`tests/shared/frame.html`;
    `serve.mjs` lets the test page frame it).

- **1.2.8**
  - `dh-strip.mjs`: Quarry (`quarry`, https://quarry.dragonhound.dev/, argus-quarry) joins the strip after Mask:
    Lightbox, Studio, Atlas, Cull, Realms, Safelight, Assay, Sightline, Mask, Quarry. `dh-strip.html`, the current-site
    rules in `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: ten links, the current one semibold, and the longer suffix need 955 px in Chromium with Linux system
    fonts. The nine-link bar of 1.2.7 measured about 60 px wider elsewhere (961 px against 900 px here), so the phone
    layout (mark only, short label) applies below 1024 px (was 968 px), leaving room for wider fonts. Without the name
    and suffix the ten links need 679 px, so the link list scrolls sideways below 720 px (was 664 px). The 420 px and
    360 px tightening is unchanged. Measured in Chromium at every width from 320 to 1030 px and at 1280 and 1440 px,
    both suffixes, each site current: no page or bar overflow, "Live demo" always on screen, the current link always
    in view, links at least 25 px wide.
  - `dh-theme.js` and the landing's external-link rule: comments name the new site. The test page has the new link
    and the new phone width. The README's opening list names Mask and Quarry, and its strip section gives the current
    breakpoints (it still gave 1.2.5's).

- **1.2.7**
  - `dh-strip.mjs`: Mask (`mask`, https://mask.dragonhound.dev/, argus-mask) joins the strip after Sightline: Lightbox,
    Studio, Atlas, Cull, Realms, Safelight, Assay, Sightline, Mask. `dh-strip.html`, the current-site rules in
    `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: nine links, the current one semibold, and the longer suffix need about 961 px, so the phone layout
    (mark only, short label) applies below 968 px (was 880 px), and the link list scrolls sideways below 664 px (was
    600 px), where nine links stop fitting. The 420 px and 360 px tightening is unchanged. Measured in Chromium at every
    width from 320 to 999 px and at 1280 and 1440 px, both suffixes, each site current: no page or bar overflow,
    "Live demo" always on screen, the current link always in view, links at least 25 px wide.
  - `dh-theme.js` and the landing's external-link rule: comments name the new site. The test page has the new link
    and the new phone width. The README's header names the current version again (it still said 1.2.5 in 1.2.6).

- **1.2.6**
  - `dh-strip.css`: `.dh-strip` is `position: relative`. Below 880 px the name and the label suffix are visually
    hidden with `position: absolute`. In an unpositioned bar they escaped its scroll box, sitting where the bar's
    content ends, so the whole page scrolled sideways whenever that content was a few px wider than the screen
    (between 600 and 879 px, depending on the browser's fonts: 6 px at 604 px in CI's Chrome). The test page checks
    the bar is positioned.

- **1.2.5**
  - `dh-strip.mjs`: Safelight (`safelight`, https://safelight.dragonhound.dev/, argus-darkroom), Assay (`assay`,
    https://assay.dragonhound.dev/, argus-mint) and Sightline (`sightline`, https://sightline.dragonhound.dev/,
    argus-survey) join the strip after Realms: Lightbox, Studio, Atlas, Cull, Realms, Safelight, Assay, Sightline.
    `dh-strip.html`, the current-site rules in `dh-strip.css` and the `DhSite` type follow.
  - `dh-strip.css`: eight links and the longer suffix need about 860 px, so the phone layout (mark only, short label)
    applies below 880 px (was 720 px). Below 600 px the link list scrolls sideways inside the strip instead of the
    whole bar, with edge fades driven by `animation-timeline: scroll()` that show only while there is more to scroll
    that way (none when the list fits; no fade, just a clip, without scroll-driven animation support). The 420 px and
    360 px tightening is unchanged. Measured in Chromium at every width from 320 to 1440 px, both suffixes, each site
    current: no page or bar overflow, "Live demo" always on screen, the current link always in view, links at least
    25 px wide.
  - `dh-theme.js`: when it marks the current site's link and the link list scrolls, it scrolls that link into view
    (the list only, never the page). Its comments and this README name the new sites.

- **1.2.4**
  - `dh-strip.mjs`: Realms (`realms`, https://realms.dragonhound.dev/), the argus-atlas minigame, joins the strip after
    Cull: Lightbox, Studio, Atlas, Cull, Realms. `dh-strip.html`, the current-site rules in `dh-strip.css` and the
    `DhSite` type follow.
  - Label suffix option: `data-dh-label="browser"` on `<html>` or the nav shows "Live demo · runs in your browser"
    instead of the default "· recorded output", without editing the pasted strip. `dh-strip.mjs` gains
    `labelSuffixes`, `defaultLabel`, `stripHtml({ label })` and `labelSuffixCss()`; `dh-strip.html` now carries both
    suffix spans (the non-default one `hidden`), and `--stamp` generates the matching rules into a second
    `@generated` block in `dh-strip.css`. Types: `DhLabel`. The default output is unchanged.
  - `dh-strip.css`: five links and the longer suffix need about 680 px, so the phone layout applies below 720 px
    (was 640 px), the tight spacing below 420 px (was 380 px) with 3 px link padding, and below 360 px the strip
    text is 12 px with 2 px link padding. Measured in Chromium at every width from 320 to 1440 px with both suffixes:
    no sideways scroll; the closest fit is about 10 px of spare room at 320 px. Links stay at least 24 px wide.
  - Comments in `dh-theme.js` and `sync-shared.mjs` and this README name Realms.

- **1.2.3**
  - `dh-strip.mjs`: the Realms link becomes **Atlas** (`atlas`, https://atlas.dragonhound.dev/). The painted-map demo
    moved to atlas.dragonhound.dev; realms.dragonhound.dev is kept for the Realms minigame and redirects to Atlas until
    it ships. Order is unchanged: Lightbox, Studio, Atlas, Cull. `dh-strip.html`, the current-site rules in
    `dh-strip.css` and the `DhSite` type in `dh-strip.d.mts` follow (`"realms"` is replaced by `"atlas"`), so the
    static demo now sets `data-dh-site="atlas"`. "Atlas" is one character shorter than "Realms", so the 1.2.2 breakpoints still fit.
  - Comments in `dh-theme.js` and `sync-shared.mjs` and this README name Atlas.

- **1.2.2**
  - `dh-strip.mjs`: Cull (`cull`, https://cull.dragonhound.dev/) joins the strip after Realms, so the Cull demo marks
    itself current; `dh-strip.html`, the current-site rules in `dh-strip.css` and the `DhSite` type in `dh-strip.d.mts` follow.
  - `dh-strip.css`: with four demo links the full bar ("Dragonhound Labs" … "Live demo · recorded output") needs about
    625 px, so the phone layout (name and "· recorded output" visually hidden) now applies below 640 px instead of 560 px,
    and below 380 px the spacing tightens. The bar fits from 320 px to desktop without the sideways scroll.
  - Comments in `dh-theme.js` and `sync-shared.mjs` and this README name Cull alongside the other demos.

- **1.2.1**
  - `dh-strip.html`: the header comment no longer contains tag-like text (it said `<nav>` and `<html>`, which led to a
    bad partial paste); it names "the nav element" instead.
  - `sync-shared.mjs`: `--no-theme-script` leaves `dh-theme.js` out of a vendored copy for locked single-theme sites,
    recorded in `.dh-shared.json` so `--check`/`--against` still pass; `.mts` files are hashed as text.
  - `dh-strip.d.mts` (new): TypeScript declarations for `dh-strip.mjs`.
  - README: the 12 px floor is for UI text; map canvas labels may go to 11 px for minor places with a halo.

- **1.2.0**
  - `--dh-warn-ink` light `#8a5a00` → `#7a4f00` (4.3 → 5.2:1 on a 20% warn mix, 6.5:1 on `--dh-warn-tint`); status ink ratios documented per tint.
  - `--dh-cat-1..8` with `-ink` and `-tint`: colour-blind-aware categorical palette for facets and series.
  - `dh-theme.js`: `dh-theme-change` event, `dhTheme.effective()` and `.subscribe()`, `aria-pressed` kept in sync on toggles mounted later, strip current-site marking from `data-dh-site`; no unused `catch` binding.
  - `dh-strip.mjs` (new): the strip as an ES module; `dh-strip.html` and the strip's current-site CSS are generated from it. Links carry `data-dh-site`; `aria-current` is now `"page"`.
  - `sync-shared.mjs`: favicons left out of vendored copies unless `--with-favicons`; stale vendored files removed.
  - `build-favicons.mjs`: `--mark`, `--out`, `--tile`, `--name`, `--short-name`.
  - README: locked sites may skip `dh-theme.js`, never set `font` on `html`, text-size floor for dense UIs, Argus favicon rule.
- **1.1.0** `--dh-stage-data|training|apps|foundation` stage colours.
- **1.0.0** Tokens, badge, strip, theme, favicons.
