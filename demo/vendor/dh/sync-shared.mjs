#!/usr/bin/env node
// Dragonhound Labs shared design layer: stamp, publish, vendor and drift-check.
// No dependencies (Node 18+). The same file is vendored into each target repo so its CI can run --check.
//
// In dragonhound-site (the source of truth, shared/):
//   node scripts/sync-shared.mjs --stamp                 recompute shared/manifest.json + header checksums
//   node scripts/sync-shared.mjs --check                 fail if manifest or the landing's own copies are stale
//   node scripts/sync-shared.mjs --publish dist          copy shared/ to dist/brand/v<major>/ (run by npm run build)
//   node scripts/sync-shared.mjs <repo> [--dest vendor/dh] [--with-favicons] [--no-theme-script]
//                                                        vendor shared/ into <repo>/<dest>, write <repo>/.dh-shared.json.
//                                                        favicons/ (the lab's own icons) are left out unless asked for;
//                                                        --no-theme-script leaves out dh-theme.js (locked single-theme sites).
// --stamp also regenerates dh-strip.html and the current-site and label-suffix rules in dh-strip.css from dh-strip.mjs.
//
// In a target repo (after vendoring):
//   node vendor/dh/sync-shared.mjs --check .             fail if any vendored file was edited
//   node vendor/dh/sync-shared.mjs --check . --against https://dragonhound.dev/brand/v1/manifest.json
//                                                        ...or if it no longer matches the published source
//   add --allow-outdated to report a newer upstream as a warning instead of a failure.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync, copyFileSync, statSync, unlinkSync, rmdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (f) => args.includes(f);
const opt = (f, d) => { const i = args.indexOf(f); return i >= 0 ? args[i + 1] : d; };
const positional = args.filter((a, i) => !a.startsWith("--") && !(i > 0 && ["--dest", "--against", "--publish"].includes(args[i - 1])));

const sha = (buf) => createHash("sha256").update(buf).digest("hex");
// Hash text with LF line endings so a checkout with autocrlf does not count as drift.
const TEXT = /\.(css|js|mjs|ts|mts|html|svg|md|json|webmanifest)$/;
const fileHash = (p) => { const b = readFileSync(p); return sha(TEXT.test(p) ? b.toString("utf8").replace(/\r\n/g, "\n") : b); };
const setHash = (files) => sha(Object.keys(files).sort().map((k) => `${files[k]}  ${k}\n`).join(""));
const walk = (dir, base = dir) => readdirSync(dir).flatMap((n) => {
  const p = join(dir, n);
  return statSync(p).isDirectory() ? walk(p, base) : [relative(base, p).split("\\").join("/")];
});
const die = (msg) => { console.error(`sync-shared: ${msg}`); process.exit(1); };

// The source repo is the one with shared/manifest.json next to scripts/.
const SRC = resolve(HERE, "../shared");
const isSource = existsSync(join(SRC, "manifest.json")) && existsSync(join(SRC, "dh-tokens.css"));
const MANIFEST = "manifest.json";
const HELPERS = [MANIFEST, "sync-shared.mjs"]; // vendored alongside, hashed for local edits, outside the set checksum

function sourceFiles() {
  return walk(SRC).filter((f) => f !== MANIFEST).sort();
}

// The tokens header carries the sha256 of everything after its first comment.
function stampTokens(version) {
  const p = join(SRC, "dh-tokens.css");
  const css = readFileSync(p, "utf8").replace(/\r\n/g, "\n");
  const end = css.indexOf("*/") + 2;
  const body = css.slice(end);
  const head = css.slice(0, end)
    .replace(/dh-tokens v[\d.]+/, `dh-tokens v${version}`)
    .replace(/sha256:(?:[0-9a-f]{64}|PENDING)/, `sha256:${sha(body)}`);
  writeFileSync(p, head + body);
}
function stampVersions(version) {
  for (const f of sourceFiles().filter((f) => /\.(css|m?js|m?ts|html)$/.test(f))) {
    const p = join(SRC, f);
    const s = readFileSync(p, "utf8");
    const t = s.replace(/^(\/\*! dh-[a-z]+ v|<!-- dh-[a-z]+ v)[\d.]+/, `$1${version}`);
    if (t !== s) writeFileSync(p, t);
  }
}
// dh-strip.mjs is the source of dh-strip.html and of the current-site selectors in dh-strip.css.
const STRIP_CSS_BLOCK = /(\/\* @generated from dh-strip\.mjs[^*]*\*\/\n)[\s\S]*?(\/\* @end \*\/)/;
const LABEL_CSS_BLOCK = /(\/\* @generated label suffixes from dh-strip\.mjs[^*]*\*\/\n)[\s\S]*?(\/\* @end \*\/)/;
async function stripOutputs(version) {
  const mod = await import(pathToFileURL(join(SRC, "dh-strip.mjs")).href);
  const html = `<!-- dh-strip v${version}: Dragonhound Labs family bar. Generated from dh-strip.mjs by --stamp: do not edit.
     Paste everything from the nav element below as the first element after the skip link. Mark the current site
     with a data-dh-site attribute (${mod.strip.links.map((l) => l.site).join(", ").replace(/, (?=[^,]*$)/, " or ")}) on the html element or on the nav element:
     dh-strip.css shows it, dh-theme.js adds aria-current="page". The label suffix defaults to "${mod.strip.labelSuffixes[mod.strip.defaultLabel].replace(/^ · /, "")}";
     pick another with data-dh-label (${Object.keys(mod.strip.labelSuffixes).filter((k) => k !== mod.strip.defaultLabel).map((k) => `${k}: "${mod.strip.labelSuffixes[k].replace(/^ · /, "")}"`).join(", ")}) on the html or nav element.
     Links are same-tab: every *.dragonhound.dev host is internal. Needs dh-tokens.css + dh-strip.css. -->
${mod.stripHtml()}
`;
  const cssNow = readFileSync(join(SRC, "dh-strip.css"), "utf8").replace(/\r\n/g, "\n");
  if (!STRIP_CSS_BLOCK.test(cssNow) || !LABEL_CSS_BLOCK.test(cssNow)) die("dh-strip.css lost its @generated/@end markers");
  const css = cssNow.replace(STRIP_CSS_BLOCK, (_, open, close) => `${open}${mod.currentSiteSelector()}\n${close}`)
    .replace(LABEL_CSS_BLOCK, (_, open, close) => `${open}${mod.labelSuffixCss()}\n${close}`);
  const svgPath = (readFileSync(join(SRC, "dh-mark.svg"), "utf8").match(/ d="([^"]+)"/) || [])[1];
  if (svgPath !== mod.strip.markPath) die("dh-strip.mjs markPath differs from dh-mark.svg");
  return { "dh-strip.html": html, "dh-strip.css": css };
}

function buildManifest(version) {
  const files = Object.fromEntries(sourceFiles().map((f) => [f, fileHash(join(SRC, f))]));
  return { name: "dh-shared", version, checksum: setHash(files), files };
}
const readJSON = (p) => JSON.parse(readFileSync(p, "utf8"));

// Copies the landing page serves from public/ must equal their shared/ originals.
const LANDING_COPIES = {
  "dh-theme.js": "public/dh-theme.js",
  "favicons/favicon.svg": "public/favicon.svg",
  "favicons/favicon.ico": "public/favicon.ico",
  "favicons/apple-touch-icon.png": "public/apple-touch-icon.png",
  "favicons/icon-192.png": "public/icon-192.png",
  "favicons/icon-512.png": "public/icon-512.png",
  "favicons/site.webmanifest": "public/site.webmanifest",
};

async function loadReference(ref) {
  if (/^https?:\/\//.test(ref)) {
    const r = await fetch(ref, { headers: { accept: "application/json" } });
    if (!r.ok) die(`could not fetch ${ref}: HTTP ${r.status}`);
    return r.json();
  }
  return readJSON(ref);
}

async function main() {
  if (flag("--stamp")) {
    if (!isSource) die("--stamp runs in dragonhound-site only");
    const version = opt("--version", readJSON(join(SRC, MANIFEST)).version);
    stampVersions(version);
    for (const [f, text] of Object.entries(await stripOutputs(version))) writeFileSync(join(SRC, f), text);
    stampTokens(version);
    const m = buildManifest(version);
    writeFileSync(join(SRC, MANIFEST), JSON.stringify(m, null, 2) + "\n");
    // Refresh the landing's public/ copies too: stampVersions() rewrites version headers (e.g. dh-theme.js),
    // so without this the build's --check fails right after a version bump.
    const root = resolve(SRC, "..");
    let copied = 0;
    for (const [from, to] of Object.entries(LANDING_COPIES)) {
      if (existsSync(join(root, to)) && fileHash(join(root, to)) === fileHash(join(SRC, from))) continue;
      mkdirSync(dirname(join(root, to)), { recursive: true });
      copyFileSync(join(SRC, from), join(root, to));
      copied++;
    }
    console.log(`stamped dh-shared v${m.version} ${m.checksum.slice(0, 12)} (${Object.keys(m.files).length} files${copied ? `, ${copied} landing cop${copied === 1 ? "y" : "ies"} refreshed` : ""})`);
    return;
  }

  if (flag("--publish")) {
    if (!isSource) die("--publish runs in dragonhound-site only");
    const m = readJSON(join(SRC, MANIFEST));
    const out = join(resolve(opt("--publish")), "brand", `v${m.version.split(".")[0]}`);
    for (const f of [...sourceFiles(), MANIFEST]) {
      mkdirSync(dirname(join(out, f)), { recursive: true });
      copyFileSync(join(SRC, f), join(out, f));
    }
    copyFileSync(fileURLToPath(import.meta.url), join(out, "sync-shared.mjs"));
    console.log(`published dh-shared v${m.version} to ${relative(process.cwd(), out)}/`);
    return;
  }

  if (flag("--check")) {
    const target = positional[0];
    let failed = false;
    const fail = (msg) => { console.error(`DRIFT: ${msg}`); failed = true; };

    if (!target) {
      // Source repo self-check: manifest current, landing copies identical.
      if (!isSource) die("--check without a target runs in dragonhound-site; in a target repo pass its path, e.g. --check .");
      const m = readJSON(join(SRC, MANIFEST));
      const fresh = buildManifest(m.version);
      if (fresh.checksum !== m.checksum) fail("shared/ changed since the last stamp: run node scripts/sync-shared.mjs --stamp (and bump --version if the change is not a fix)");
      const css = readFileSync(join(SRC, "dh-tokens.css"), "utf8").replace(/\r\n/g, "\n");
      const end = css.indexOf("*/") + 2;
      if (!css.slice(0, end).includes(`sha256:${sha(css.slice(end))}`)) fail("dh-tokens.css header checksum is stale: run --stamp");
      for (const [f, text] of Object.entries(await stripOutputs(m.version))) {
        if (readFileSync(join(SRC, f), "utf8").replace(/\r\n/g, "\n") !== text) fail(`${f} is out of date with dh-strip.mjs: run --stamp`);
      }
      const root = resolve(SRC, "..");
      for (const [from, to] of Object.entries(LANDING_COPIES)) {
        if (!existsSync(join(root, to)) || fileHash(join(root, to)) !== fileHash(join(SRC, from))) fail(`${to} differs from shared/${from}`);
      }
      if (failed) process.exit(1);
      console.log(`dh-shared v${m.version} ${m.checksum.slice(0, 12)}: source is stamped and the landing copies match`);
      return;
    }

    const repo = resolve(target);
    const lockPath = join(repo, ".dh-shared.json");
    if (!existsSync(lockPath)) die(`${lockPath} not found: vendor the shared layer first`);
    const lock = readJSON(lockPath);
    for (const [f, h] of Object.entries(lock.files)) {
      const p = join(repo, lock.dest, f);
      if (!existsSync(p)) fail(`${lock.dest}/${f} is missing`);
      else if (fileHash(p) !== h) fail(`${lock.dest}/${f} was edited locally (change it in dragonhound-site/shared/ instead)`);
    }
    // Files left out at vendoring time (favicons/ unless --with-favicons, dh-theme.js with --no-theme-script) still count towards the upstream checksum.
    const core = { ...lock.omitted, ...Object.fromEntries(Object.entries(lock.files).filter(([k]) => !HELPERS.includes(k))) };
    if (setHash(core) !== lock.checksum) fail(".dh-shared.json checksum does not match its file list");

    const ref = opt("--against") ?? (isSource && resolve(HERE, "..") !== repo ? join(SRC, MANIFEST) : null);
    if (ref) {
      const up = await loadReference(ref);
      if (up.checksum !== lock.checksum) {
        const msg = `vendored v${lock.version} (${lock.checksum.slice(0, 12)}) differs from upstream v${up.version} (${up.checksum.slice(0, 12)}): re-run the sync`;
        if (flag("--allow-outdated") && up.version !== lock.version) console.warn(`WARN: ${msg}`);
        else fail(msg);
      }
    }
    if (failed) process.exit(1);
    console.log(`dh-shared v${lock.version} ${lock.checksum.slice(0, 12)}: ${Object.keys(lock.files).length} vendored files intact${ref ? ", matches upstream" : ""}`);
    return;
  }

  // Vendor into a target repo.
  const target = positional[0];
  if (!target) die("usage: sync-shared.mjs <target-repo> [--dest vendor/dh] [--with-favicons] [--no-theme-script] | --check [repo] | --stamp | --publish <dir>");
  if (!isSource) die("vendoring runs from dragonhound-site (the source of truth)");
  const m = readJSON(join(SRC, MANIFEST));
  const fresh = buildManifest(m.version);
  if (fresh.checksum !== m.checksum) die("shared/ is not stamped: run --stamp first");
  const repo = resolve(target);
  if (repo === resolve(SRC, "..")) die("refusing to vendor into dragonhound-site itself");
  const dest = opt("--dest", "vendor/dh");
  const out = join(repo, dest);
  // The lab's favicons are Dragonhound's own icons: product sites (the Argus apps use argus-cortex brand/) must
  // not ship them by accident, so they are vendored only with --with-favicons.
  // Locked single-theme sites may leave out dh-theme.js with --no-theme-script (it only drives the toggle and cookie).
  const omit = (f) => (f.startsWith("favicons/") && !flag("--with-favicons")) || (f === "dh-theme.js" && flag("--no-theme-script"));
  const files = {}, omitted = {};
  for (const [f, h] of Object.entries(m.files)) {
    if (omit(f)) { omitted[f] = h; continue; }
    files[f] = h;
    mkdirSync(dirname(join(out, f)), { recursive: true });
    copyFileSync(join(SRC, f), join(out, f));
  }
  // Remove files a previous sync vendored that are no longer in the set (e.g. favicons/ from v1.1.0).
  const oldLockPath = join(repo, ".dh-shared.json");
  const removed = [];
  if (existsSync(oldLockPath)) {
    const old = readJSON(oldLockPath);
    for (const f of Object.keys(old.files || {})) {
      if (HELPERS.includes(f) || f in files || !existsSync(join(repo, old.dest, f))) continue;
      unlinkSync(join(repo, old.dest, f));
      removed.push(f);
      const dir = dirname(join(repo, old.dest, f));
      if (dir !== join(repo, old.dest) && readdirSync(dir).length === 0) rmdirSync(dir);
    }
  }
  copyFileSync(join(SRC, MANIFEST), join(out, MANIFEST));
  files[MANIFEST] = fileHash(join(out, MANIFEST));
  copyFileSync(fileURLToPath(import.meta.url), join(out, "sync-shared.mjs"));
  files["sync-shared.mjs"] = fileHash(join(out, "sync-shared.mjs"));
  // The lock's checksum is over the upstream file set, so --against compares like with like;
  // the two helper files are hashed too, for the local-edit check.
  const lock = { name: "dh-shared", version: m.version, checksum: m.checksum, dest, files };
  // Left-out files keep their hashes, so --check and --against still verify against the full upstream set.
  if (Object.keys(omitted).length) lock.omitted = omitted;
  if (flag("--no-theme-script")) lock.noThemeScript = true;
  writeFileSync(join(repo, ".dh-shared.json"), JSON.stringify(lock, null, 2) + "\n");
  console.log(`vendored dh-shared v${m.version} ${m.checksum.slice(0, 12)} into ${join(target, dest)} (${Object.keys(files).length} files` +
    `${Object.keys(omitted).length ? `; left out ${Object.keys(omitted).length}: ${Object.keys(omitted).some((f) => f.startsWith("favicons/")) ? "favicons/ (add --with-favicons to include)" : ""}${"dh-theme.js" in omitted ? `${Object.keys(omitted).length > 1 ? ", " : ""}dh-theme.js (--no-theme-script)` : ""}` : ""}` +
    `${removed.length ? `; removed ${removed.length} no longer vendored: ${removed.join(", ")}` : ""})`);
}

main().catch((e) => die(e.message));
