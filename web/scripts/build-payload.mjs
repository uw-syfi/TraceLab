// Assemble derived web assets from the toolkit, with zero code/data drift.
//   Phase 1: copy pre-rendered figure PNGs from artifacts/ into app/public/figures/.
//   Phase 2: assemble the Pyodide payload (Python whitelist) into app/public/py/.
//
// SAFETY: only ever reads from artifacts/, web/payload/, and scripts/ (the canonical
// normalize/sanitize CLIs); never copies anything under trace/. Every copy is funnelled through
// copyTracked(), which asserts the source path.

import {
  mkdirSync,
  rmSync,
  existsSync,
  copyFileSync,
  readdirSync,
  writeFileSync,
} from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // web/scripts
const REPO = resolve(SCRIPT_DIR, '../..'); // repo root
const ARTIFACTS = join(REPO, 'artifacts');
const SCRIPTS = join(REPO, 'scripts'); // canonical normalize/sanitize CLIs (raw -> normalized -> sanitized)
const PAYLOAD_SRC = resolve(SCRIPT_DIR, '../payload'); // web/payload (hand-written driver.py / ingest.py)
const PUBLIC = resolve(SCRIPT_DIR, '../app/public');
const TRACE_PREFIX = join(REPO, 'trace') + '/';

function copyTracked(src, dest) {
  if (src.startsWith(TRACE_PREFIX)) {
    throw new Error(`[build-payload] refusing to copy trace-derived data: ${src}`);
  }
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(src, dest);
}

// ---------------------------------------------------------------------------
// Phase 1 — figure PNGs
// ---------------------------------------------------------------------------

/** experiment folder under artifacts/, the emitted PNG (literal or regex glob), destination. */
const FIGURES = [
  { exp: 'tool_calls/tool_latency_distribution', file: 'tool_latency_by_tool.png', dest: 'tool_calls/tool_latency_by_tool.png' },
  { exp: 'tool_calls/tool_latency_distribution', file: 'tool_latency_weighted_bins.png', dest: 'tool_calls/tool_latency_weighted_bins.png' },
  { exp: 'tool_calls/tool_latency_distribution', file: 'tool_latency_count_cdf_by_provider.png', dest: 'tool_calls/tool_latency_count_cdf_by_provider.png' },
  { exp: 'tool_calls/tool_latency_distribution', file: 'tool_total_latency_cdf_by_provider.png', dest: 'tool_calls/tool_total_latency_cdf_by_provider.png' },
  { exp: 'tool_calls/tool_call_counts', file: 'tool_call_counts.png', dest: 'tool_calls/tool_call_counts.png' },
  { exp: 'tool_calls/tool_category_distribution', file: 'tool_category_count_ring.png', dest: 'tool_calls/tool_category_count_ring.png' },
  { exp: 'tool_calls/tool_category_distribution', file: 'tool_category_latency_bar.png', dest: 'tool_calls/tool_category_latency_bar.png' },
  { exp: 'tool_calls/tool_category_distribution', file: 'tool_category_dashboard.png', dest: 'tool_calls/tool_category_dashboard.png' },
  { exp: 'tool_calls/tool_category_distribution', file: 'tool_latency_long_tail_imbalance.png', dest: 'tool_calls/tool_latency_long_tail_imbalance.png' },
  { exp: 'tool_calls/tool_time_by_kind', file: 'tool_total_time_by_kind.png', dest: 'tool_calls/tool_total_time_by_kind.png' },
  { exp: 'llm_generation/prefix_append_distribution', file: 'prefix_append_distribution.png', dest: 'llm_generation/prefix_append_distribution.png' },
  { exp: 'llm_generation/prefix_append_distribution', file: 'prefix_append_cdf.png', dest: 'llm_generation/prefix_append_cdf.png' },
  { exp: 'llm_generation/prefix_append_distribution', file: 'prefix_vs_append_sample.png', dest: 'llm_generation/prefix_vs_append_sample.png' },
  { exp: 'llm_generation/prefix_append_distribution', file: 'append_tokens_weighted_bins.png', dest: 'llm_generation/append_tokens_weighted_bins.png' },
  { exp: 'llm_generation/adjusted_prefix_append', file: 'prefix_vs_adjusted_append_sample.png', dest: 'llm_generation/prefix_vs_adjusted_append_sample.png' },
  { exp: 'llm_generation/append_vs_prefix_latency', file: 'append_vs_prefix_bucket_effects.png', dest: 'llm_generation/append_vs_prefix_bucket_effects.png' },
  { exp: 'llm_generation/append_vs_prefix_latency', file: 'append_vs_prefix_normalized_overlap.png', dest: 'llm_generation/append_vs_prefix_normalized_overlap.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'output_vs_next_append_scatter_min2000.png', dest: 'llm_generation/output_vs_next_append_scatter_min2000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'ranked_output_vs_next_append_min2000.png', dest: 'llm_generation/ranked_output_vs_next_append_min2000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'output_vs_prefix_gain_scatter_min2000.png', dest: 'llm_generation/output_vs_prefix_gain_scatter_min2000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'ranked_output_vs_prefix_gain_min2000.png', dest: 'llm_generation/ranked_output_vs_prefix_gain_min2000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'output_vs_next_append_scatter_min4000.png', dest: 'llm_generation/output_vs_next_append_scatter_min4000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'ranked_output_vs_next_append_min4000.png', dest: 'llm_generation/ranked_output_vs_next_append_min4000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'output_vs_prefix_gain_scatter_min4000.png', dest: 'llm_generation/output_vs_prefix_gain_scatter_min4000.png' },
  { exp: 'llm_generation/output_append_assignment', file: 'ranked_output_vs_prefix_gain_min4000.png', dest: 'llm_generation/ranked_output_vs_prefix_gain_min4000.png' },
  { exp: 'llm_generation/token_spindles', file: 'token_spindles_transparent.png', dest: 'llm_generation/token_spindles_transparent.png' },
  { exp: 'llm_generation/output_tokens', file: 'output_tokens_distribution.png', dest: 'llm_generation/output_tokens_distribution.png' },
  { exp: 'llm_generation/generation_time_cdf', file: 'llm_generation_time_count_cdf_by_provider.png', dest: 'llm_generation/llm_generation_time_count_cdf_by_provider.png' },
  { exp: 'llm_generation/generation_time_cdf', file: 'llm_generation_time_total_cdf_by_provider.png', dest: 'llm_generation/llm_generation_time_total_cdf_by_provider.png' },
  { exp: 'prefix_cache/cache_hit_ratio', file: 'cache_hit_ratio_histogram.png', dest: 'prefix_cache/cache_hit_ratio_histogram.png' },
  { exp: 'prefix_cache/cache_hit_ratio', file: 'cache_hit_ratio_append_weighted_histogram.png', dest: 'prefix_cache/cache_hit_ratio_append_weighted_histogram.png' },
  { exp: 'prefix_cache/kv_cache_active_ratio', file: 'kv_cache_active_ratio_by_provider.png', dest: 'prefix_cache/kv_cache_active_ratio_by_provider.png' },
  { exp: 'prefix_cache/cache_hit_idle_relationship', file: 'user_wait_time_vs_hit_rate_scatter.png', dest: 'prefix_cache/user_wait_time_vs_hit_rate_scatter.png' },
  { exp: 'prefix_cache/cache_hit_idle_relationship', file: 'tool_result_wait_time_vs_hit_rate_scatter.png', dest: 'prefix_cache/tool_result_wait_time_vs_hit_rate_scatter.png' },
  { exp: 'human_in_the_loop/human_input_wait', file: 'human_input_wait_cdf.png', dest: 'human_in_the_loop/human_input_wait_cdf.png' },
  { exp: 'human_in_the_loop/human_input_wait', file: 'human_input_wait_count_cdf_by_provider.png', dest: 'human_in_the_loop/human_input_wait_count_cdf_by_provider.png' },
  { exp: 'human_in_the_loop/human_input_wait', file: 'human_input_wait_total_cdf_by_provider.png', dest: 'human_in_the_loop/human_input_wait_total_cdf_by_provider.png' },
  { exp: 'session/session_token_steps', glob: /_token_steps\.png$/, dest: 'session/session_token_steps.png' },
];

function resolveFigure(f) {
  const dir = join(ARTIFACTS, f.exp);
  if (!existsSync(dir)) return null;
  let name = f.file;
  if (f.glob) {
    name = readdirSync(dir).find((n) => f.glob.test(n));
    if (!name) return null;
  }
  const src = join(dir, name);
  return existsSync(src) ? src : null;
}

// The summary the site reads (KPIs, donut, comparison). It MUST come from the same render
// as the figures, so prefer overview_summary's freshly written file; fall back to the
// committed repo-root summary.json only when nothing has been rendered yet.
function copySummary() {
  const rendered = join(ARTIFACTS, 'trace_facts/overview_summary/summary.json');
  const fallback = join(REPO, 'summary.json');
  const src = existsSync(rendered) ? rendered : existsSync(fallback) ? fallback : null;
  if (!src) {
    console.log('[build-payload] summary.json: none found — run a render first');
    return;
  }
  copyTracked(src, join(PUBLIC, 'data', 'summary.json'));
  const which = src === rendered ? 'rendered (overview_summary)' : 'repo-root fallback';
  console.log(`[build-payload] summary.json <- ${which}`);
}

function copyFigures() {
  let copied = 0;
  const pending = [];
  for (const f of FIGURES) {
    const src = resolveFigure(f);
    if (!src) {
      pending.push(f.dest);
      continue;
    }
    copyTracked(src, join(PUBLIC, 'figures', f.dest));
    copied += 1;
  }
  console.log(`[build-payload] figures copied: ${copied}/${FIGURES.length} -> ${join(PUBLIC, 'figures')}`);
  if (pending.length) {
    console.log(`[build-payload] figures pending (run the toolkit render first): ${pending.length}`);
    for (const p of pending) console.log(`  - ${p}`);
  }
}

// ---------------------------------------------------------------------------
// Phase 2 — Pyodide payload (must mirror driver.py's V1 + OVERVIEW set)
// ---------------------------------------------------------------------------

// duckdb 1.1.2 ships in the Pyodide 0.27.2 lock; experiments query the materialized trace DB
// (artifacts/utils/trace_db.py). Only duckdb is added — pandas/pyarrow/polars stay out of the
// boot payload unless an experiment genuinely needs them (keeps first-boot weight down).
const PY_PACKAGES = ['numpy', 'matplotlib', 'Pillow', 'duckdb'];

// Experiment scripts (each paired with its README.md so png_sidecar can self-contain).
const PY_SCRIPTS = [
  'tool_calls/tool_latency_distribution/plot.py',
  'tool_calls/tool_call_counts/plot.py',
  'llm_generation/prefix_append_distribution/plot.py',
  'llm_generation/output_tokens/plot.py',
  'llm_generation/generation_time_cdf/plot.py',
  'prefix_cache/cache_hit_ratio/analyze.py',
  'prefix_cache/kv_cache_active_ratio/plot.py',
  'human_in_the_loop/human_input_wait/plot.py',
  'session/session_token_steps/plot.py',
  'trace_facts/overview_summary/analyze.py',
];

function assemblePython() {
  const PY_OUT = join(PUBLIC, 'py');
  rmSync(PY_OUT, { recursive: true, force: true }); // start clean so no stale files linger
  const files = []; // relative paths the worker mounts under its MEMFS root

  const add = (srcAbs, rel) => {
    copyTracked(srcAbs, join(PY_OUT, rel));
    files.push(rel);
  };

  // The in-process dispatcher + the format-aware ingest orchestrator (hand-written, outside artifacts/).
  add(join(PAYLOAD_SRC, 'driver.py'), 'driver.py');
  add(join(PAYLOAD_SRC, 'ingest.py'), 'ingest.py');

  // Canonical normalize/sanitize CLIs, mounted flat under normalize/ so their bare cross-imports
  // (extract_codex_rounds -> extract_claude_rounds, sanitize_round_trace -> trace_privacy) resolve
  // once ingest.py puts that dir on sys.path. Same copy-from-canonical model as artifacts/utils/*.
  for (const name of [
    'extract_claude_rounds.py',
    'extract_codex_rounds.py',
    'sanitize_round_trace.py',
    'trace_privacy.py',
  ]) {
    add(join(SCRIPTS, name), `normalize/${name}`);
  }

  // Shared util library — every .py under artifacts/utils (png_sidecar globs them all).
  const utilsDir = join(ARTIFACTS, 'utils');
  for (const name of readdirSync(utilsDir)) {
    if (name.endsWith('.py')) add(join(utilsDir, name), `artifacts/utils/${name}`);
  }

  // Single-source price table: web_analytics/pricing.py reads it at runtime to bill cost in-browser.
  // It's data (not .py), so the utils loop above skips it — copy it explicitly. Same file the web
  // mock imports via lib/analytics/cost.ts, so native/wasm/mock prices stay in lockstep.
  add(join(utilsDir, 'pricing.json'), 'artifacts/utils/pricing.json');

  // Web-analytics aggregator — turns one trace DuckDB into the entire AnalyticsPayload JSON (+ the
  // on-demand session-detail / round-raw queries) over the QA run-python RPC. Pure stdlib + duckdb;
  // every .py in the folder ships so `import analyze` and its sibling builders resolve in MEMFS.
  const webAnalyticsDir = join(ARTIFACTS, 'web_analytics');
  for (const name of readdirSync(webAnalyticsDir)) {
    if (name.endsWith('.py')) add(join(webAnalyticsDir, name), `artifacts/web_analytics/${name}`);
  }

  // Experiment scripts + their READMEs.
  for (const rel of PY_SCRIPTS) {
    add(join(ARTIFACTS, rel), `artifacts/${rel}`);
    const readme = join(dirname(join(ARTIFACTS, rel)), 'README.md');
    if (existsSync(readme)) {
      const relReadme = `artifacts/${dirname(rel)}/README.md`;
      add(readme, relReadme);
    }
  }

  writeFileSync(
    join(PY_OUT, 'manifest.json'),
    JSON.stringify({ packages: PY_PACKAGES, files }, null, 2),
  );
  console.log(`[build-payload] python payload: ${files.length} file(s) -> ${PY_OUT}`);
}

copySummary();
copyFigures();
assemblePython();
