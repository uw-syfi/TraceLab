#!/usr/bin/env node
// Self-host every webfont, so the site needs NO external CDN (works offline / behind a firewall).
// All fonts come from npm devDependencies (+ one source TTF cached from GitHub for the CJK subset);
// `npm ci` + this script is all a deploy needs. Outputs are gitignored and regenerated on prebuild.
//
// CJK strategy — two faces that cascade by family order in tokens.css
// (`… "LXGW WenKai", "LXGW WenKai Full", serif`):
//   1. "LXGW WenKai"      — a SUBSET (pyftsubset) holding only the ~1k characters that actually
//      appear in the site's static content (UI + README.zh.md). One small woff2 (~220KB) that we
//      PRELOAD, so the static pages' Chinese is there at first paint — no flash-of-unstyled-text.
//   2. "LXGW WenKai Full" — the full font as unicode-range shards (from lxgw-wenkai-webfont). Only
//      kicks in for characters the subset lacks — i.e. AI-assistant output, whose glyphs can't be
//      known at build time. Fetched on demand per shard; NOT preloaded.
//
// Latin — @fontsource-variable/fraunces (opsz, normal+italic) + @fontsource-variable/hanken-grotesk
// (wght). Small fixed glyph set; the basic-latin blocks are preloaded. 'Xxx Variable' family names
// are renamed back to plain 'Fraunces' / 'Hanken Grotesk' so tokens.css needs no change.
//
// Both CJK faces use font-weight: 400 900 so every weight maps to the single Regular face — Chinese
// never gets an ugly synthetic bold.

import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  readdirSync,
  copyFileSync,
  rmSync,
  statSync,
} from 'node:fs';
import { resolve, join, dirname, basename, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const here = dirname(fileURLToPath(import.meta.url)); // web/scripts
const appRoot = resolve(here, '../app'); // web/app
const repoRoot = resolve(here, '../..'); // repo root
const nodeModules = resolve(appRoot, 'node_modules');

const FONT_VERSION = 'v1.522';
const SOURCE_TTF_URL = `https://github.com/lxgw/LxgwWenKai/releases/download/${FONT_VERSION}/LXGWWenKai-Regular.ttf`;

// CJK is rendered a touch larger than Latin so 中英混排 looks balanced: Han glyphs read smaller than
// Latin at the same font-size, and LXGW's brush (楷体) style is particularly small-faced. `size-adjust`
// scales ONLY the CJK faces (subset + Full); Latin (Fraunces/Hanken) is untouched, so English keeps
// its size. Tune this single number if Chinese looks too big/small relative to English.
const CJK_SIZE_ADJUST = '112%';
const CWEIGHT = (s) => s.replace(/font-weight:\s*400;/g, 'font-weight: 400 900;');
// For the full shards: widen weight AND add size-adjust to every @font-face.
const CJK_TUNE = (s) => CWEIGHT(s).replace(/font-display:\s*swap/g, `font-display:swap;size-adjust:${CJK_SIZE_ADJUST}`);

const log = (m) => console.log(`[fonts] ${m}`);
const warn = (m) => console.warn(`[fonts] ${m}`);

function pkgVersion(pkg) {
  try {
    return JSON.parse(readFileSync(join(nodeModules, pkg, 'package.json'), 'utf-8')).version || '';
  } catch {
    return '';
  }
}

// Copy the woff2 a @fontsource-style CSS references into the group's files/ dir, returning the CSS
// rewritten with absolute /fonts/<group>/files/ urls (+ optional family rename / transform).
function copyAndRewrite(pkg, cssFile, group, { rename, transform } = {}) {
  const pkgDir = join(nodeModules, pkg);
  const filesDir = resolve(appRoot, `public/fonts/${group}/files`);
  const raw = readFileSync(join(pkgDir, cssFile), 'utf-8');
  let bytes = 0;
  let count = 0;
  for (const m of raw.matchAll(/\.\/files\/([^)'"\s]+\.woff2)/g)) {
    copyFileSync(join(pkgDir, 'files', m[1]), join(filesDir, m[1]));
    bytes += statSync(join(filesDir, m[1])).size;
    count += 1;
  }
  let css = raw.replace(/\.\/files\//g, `/fonts/${group}/files/`);
  for (const [f, t] of Object.entries(rename ?? {})) css = css.split(`'${f}'`).join(`'${t}'`);
  if (transform) css = transform(css);
  return { css, bytes, count };
}

// --- CJK character collection + unicode-range -------------------------------------------------

function collectCjk() {
  const set = new Set();
  const isCjk = (cp) =>
    (cp >= 0x3000 && cp <= 0x30ff) || // CJK punct + kana
    (cp >= 0x3400 && cp <= 0x9fff) || // ext A + unified
    (cp >= 0xf900 && cp <= 0xfaff) || // compat
    (cp >= 0xff00 && cp <= 0xffef); //  full/half-width
  const exts = new Set(['.astro', '.ts', '.tsx', '.js', '.mjs', '.json', '.md']);
  const eat = (file) => {
    for (const ch of readFileSync(file, 'utf-8')) {
      const cp = ch.codePointAt(0);
      if (isCjk(cp)) set.add(cp);
    }
  };
  const walk = (dir, pick) => {
    if (!existsSync(dir)) return;
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
      if (ent.name === 'node_modules' || ent.name.startsWith('.') || ent.name.includes('.generated.')) continue;
      const p = join(dir, ent.name);
      if (ent.isDirectory()) walk(p, pick);
      else if (pick(ent.name)) eat(p);
    }
  };
  walk(resolve(appRoot, 'src'), (n) => exts.has(extname(n)));
  walk(resolve(repoRoot, 'artifacts'), (n) => n === 'README.zh.md' || n === 'README.md');
  return set;
}

function toUnicodeRange(cps) {
  const a = [...cps].sort((x, y) => x - y);
  const parts = [];
  let lo = a[0];
  let prev = a[0];
  for (let i = 1; i <= a.length; i++) {
    if (a[i] === prev + 1) {
      prev = a[i];
      continue;
    }
    const h = (n) => 'U+' + n.toString(16).toUpperCase();
    parts.push(lo === prev ? h(lo) : `${h(lo)}-${prev.toString(16).toUpperCase()}`);
    lo = a[i];
    prev = a[i];
  }
  return parts.join(',');
}

function findPyftsubset() {
  const venv = resolve(repoRoot, '.venv/bin/pyftsubset');
  return existsSync(venv) ? venv : 'pyftsubset';
}

async function downloadIfMissing(url, dest) {
  if (existsSync(dest) && statSync(dest).size > 1_000_000) return;
  mkdirSync(dirname(dest), { recursive: true });
  log(`downloading source ${FONT_VERSION} …`);
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
}

// --- LXGW: subset (preloaded) + full shards (on-demand) ----------------------------------------

async function buildLxgw() {
  const pkg = 'lxgw-wenkai-webfont';
  if (!existsSync(join(nodeModules, pkg))) {
    warn(`${pkg} not installed — Chinese falls back to system fonts`);
    const cssOut = resolve(appRoot, 'src/styles/lxgw.generated.css');
    if (!existsSync(cssOut)) writeFileSync(cssOut, '/* lxgw not installed — system fallback. */\n');
    return { subsetHref: null };
  }

  const publicDir = resolve(appRoot, 'public/fonts/lxgw');
  const filesDir = join(publicDir, 'files');
  const cssOut = resolve(appRoot, 'src/styles/lxgw.generated.css');
  const subsetWoff2 = join(publicDir, 'LXGWWenKai-subset.woff2');
  const subsetHref = '/fonts/lxgw/LXGWWenKai-subset.woff2';
  const metaFile = join(publicDir, '.meta.json');

  const cps = collectCjk();
  const charsHash = createHash('sha1').update([...cps].sort((a, b) => a - b).join(',')).digest('hex');
  const pv = pkgVersion(pkg);
  const shardSrc = join(nodeModules, pkg, 'files');
  const shards = readdirSync(shardSrc).filter((f) => /^lxgwwenkai-regular-subset-\d+\.woff2$/.test(f));

  if (existsSync(metaFile) && existsSync(cssOut) && existsSync(subsetWoff2)) {
    try {
      const m = JSON.parse(readFileSync(metaFile, 'utf-8'));
      if (m.charsHash === charsHash && m.pkgVersion === pv && m.fontVersion === FONT_VERSION && m.shards === shards.length) {
        log(`lxgw: up to date (subset ${cps.size} glyphs + ${shards.length} full shards)`);
        return { subsetHref };
      }
    } catch {}
  }

  // Source TTF for the subset (cached under node_modules/.cache, gitignored).
  const srcTtf = resolve(appRoot, `node_modules/.cache/lxgw/LXGWWenKai-Regular-${FONT_VERSION}.ttf`);
  await downloadIfMissing(SOURCE_TTF_URL, srcTtf);

  rmSync(publicDir, { recursive: true, force: true });
  mkdirSync(filesDir, { recursive: true });

  // 1. Subset → one woff2 covering exactly the static-content characters.
  const charsFile = resolve(appRoot, 'node_modules/.cache/lxgw/chars.txt');
  writeFileSync(charsFile, String.fromCodePoint(...[...cps].sort((a, b) => a - b)), 'utf-8');
  log(`lxgw: subsetting ${cps.size} static glyphs …`);
  execFileSync(
    findPyftsubset(),
    [srcTtf, `--text-file=${charsFile}`, `--output-file=${subsetWoff2}`, '--flavor=woff2', '--no-hinting', '--desubroutinize'],
    { stdio: ['ignore', 'ignore', 'inherit'] },
  );
  const subsetCss =
    `/* LXGW WenKai — static subset (${cps.size} glyphs), preloaded; covers all build-time site Chinese. */\n` +
    `@font-face{font-family:'LXGW WenKai';font-style:normal;font-weight:400 900;font-display:swap;size-adjust:${CJK_SIZE_ADJUST};` +
    `src:url('${subsetHref}') format('woff2');unicode-range:${toUnicodeRange(cps)}}`;

  // 2. Full shards → "LXGW WenKai Full" (on-demand; covers anything the subset lacks, e.g. AI output).
  const full = copyAndRewrite(pkg, 'lxgwwenkai-regular.css', 'lxgw', {
    rename: { 'LXGW WenKai': 'LXGW WenKai Full' },
    transform: CJK_TUNE,
  });

  writeFileSync(
    cssOut,
    `/* GENERATED by web/scripts/build-fonts.mjs — do not edit; gitignored. */\n${subsetCss}\n\n` +
      `/* LXGW WenKai Full — full-coverage shards (on-demand), e.g. for AI-generated Chinese. */\n${full.css}\n`,
  );

  const ofl = join(nodeModules, pkg, 'OFL.txt');
  if (existsSync(ofl)) copyFileSync(ofl, join(publicDir, 'OFL.txt'));

  writeFileSync(
    metaFile,
    JSON.stringify({ charsHash, pkgVersion: pv, fontVersion: FONT_VERSION, shards: shards.length, subsetGlyphs: cps.size }, null, 2),
  );
  log(
    `lxgw: subset ${(statSync(subsetWoff2).size / 1024).toFixed(0)}KB (${cps.size} glyphs, preloaded) + ${shards.length} full shards (${(full.bytes / 1024 / 1024).toFixed(1)}MB, on-demand)`,
  );
  return { subsetHref };
}

// --- Latin -------------------------------------------------------------------------------------

function buildLatin() {
  const cssOut = resolve(appRoot, 'src/styles/latin.generated.css');
  const publicDir = resolve(appRoot, 'public/fonts/latin');
  const metaFile = join(publicDir, '.meta.json');
  const want = [
    { pkg: '@fontsource-variable/fraunces', css: ['opsz.css', 'opsz-italic.css'], license: 'LICENSE', rename: { 'Fraunces Variable': 'Fraunces' } },
    { pkg: '@fontsource-variable/hanken-grotesk', css: ['index.css'], license: 'LICENSE', rename: { 'Hanken Grotesk Variable': 'Hanken Grotesk' } },
  ];
  const present = want.filter((w) => existsSync(join(nodeModules, w.pkg)));
  if (present.length === 0) {
    warn('latin fonts not installed — using fallback fonts');
    if (!existsSync(cssOut)) writeFileSync(cssOut, '/* latin not installed — system fallback. */\n');
    return;
  }
  const versions = Object.fromEntries(present.map((w) => [w.pkg, pkgVersion(w.pkg)]));
  if (existsSync(metaFile) && existsSync(cssOut)) {
    try {
      if (JSON.stringify(JSON.parse(readFileSync(metaFile, 'utf-8')).versions) === JSON.stringify(versions)) {
        log(`latin: up to date (${Object.entries(versions).map(([p, v]) => `${basename(p)}@${v}`).join(', ')})`);
        return;
      }
    } catch {}
  }
  rmSync(publicDir, { recursive: true, force: true });
  mkdirSync(join(publicDir, 'files'), { recursive: true });
  const parts = ['/* GENERATED by web/scripts/build-fonts.mjs — do not edit; gitignored. */'];
  // These are variable fonts (opsz/wght axes) — the browser must parse + instantiate them, which is
  // slower than a static subset, so `swap` shows a fallback then visibly swaps (a flash). They're
  // preloaded, so `block` instead waits the (short) load and paints the real face directly — no flash.
  const swapToBlock = (s) => s.replace(/font-display:\s*swap/g, 'font-display: block');
  for (const w of present) {
    parts.push(`\n/* ${w.pkg}@${versions[w.pkg]} */`);
    for (const c of w.css) parts.push(copyAndRewrite(w.pkg, c, 'latin', { rename: w.rename, transform: swapToBlock }).css);
    const lic = join(nodeModules, w.pkg, w.license);
    if (existsSync(lic)) copyFileSync(lic, join(publicDir, `${basename(w.pkg)}.${w.license}`));
  }
  writeFileSync(cssOut, parts.join('\n') + '\n');
  writeFileSync(metaFile, JSON.stringify({ versions }, null, 2));
  log('latin: Fraunces + Hanken Grotesk copied');
}

// --- Preload list (FOUT-killer): latin core blocks + the CJK subset ----------------------------

function buildPreload(subsetHref) {
  const preload = [];
  const latinFiles = resolve(appRoot, 'public/fonts/latin/files');
  if (existsSync(latinFiles)) {
    for (const f of readdirSync(latinFiles).sort()) {
      if (/-latin-(opsz|wght)-normal\.woff2$/.test(f)) preload.push(`/fonts/latin/files/${f}`);
    }
  }
  if (subsetHref) preload.push(subsetHref); // one file covering all static Chinese
  writeFileSync(resolve(appRoot, 'src/styles/font-preload.json'), JSON.stringify(preload) + '\n');
  log(`preload: ${preload.length} files (latin core + CJK subset)`);
}

async function main() {
  buildLatin();
  const { subsetHref } = await buildLxgw();
  buildPreload(subsetHref);
}

main().catch((e) => {
  warn(`skipped (${e.message}) — fonts fall back to system until this succeeds`);
  // Ensure imports never break the build even on failure.
  for (const f of ['lxgw.generated.css', 'latin.generated.css']) {
    const p = resolve(appRoot, 'src/styles', f);
    if (!existsSync(p)) writeFileSync(p, `/* ${f}: generation failed — system fallback. */\n`);
  }
  if (!existsSync(resolve(appRoot, 'src/styles/font-preload.json')))
    writeFileSync(resolve(appRoot, 'src/styles/font-preload.json'), '[]\n');
  process.exit(0);
});
