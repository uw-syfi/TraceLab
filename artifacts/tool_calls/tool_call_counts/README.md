# tool_call_counts

**Which tools do coding agents actually call, how often, and how often do those calls fail —
separately for Claude Code and Codex?**

## Experiment overview

Every agent step in the trace carries a `tools[]` list of the tool calls the model made in that step.
This experiment counts those calls per `(provider, tool)` and renders one horizontal-bar panel
per provider, tools ordered by call volume, with a red overlay marking the share that returned an
error.

Method and assumptions:

- **One row per call.** We count entries in `tool_calls` (the UNNESTed `tools[]`), not agent steps —
  a step that calls `Bash` three times contributes three.
- **MCP tools are merged.** Any tool whose name starts with `mcp_` is aliased to a single `mcp`
  bucket, since the long opaque server-qualified names are individually rare and uninformative in
  aggregate.
- **Rare tools collapse.** For the *figure only*, tools with fewer than
  `--min-tool-calls-for-plot` provider-local calls (default 20) are summed into one
  `Other (<N calls/tool)` bar. The CSV keeps full per-tool detail — nothing is dropped from the
  data, only from the plot.
- **Linear, clipped axis.** Tool usage is heavily skewed (one or two tools dominate), so each panel
  clips its x-axis at ~1.05× the *second*-largest bar and annotates the clipped leader with its
  true count. This keeps the long tail readable instead of being crushed against a single giant bar.
- **Errors** are counted as calls where `is_error` is true, drawn as a shorter bar inside the call
  bar.

## Code structure

`plot.py` is a thin query→shape→plot pipeline over the shared trace DuckDB:

- `load_tool_counts_by_provider(con, *, min_calls)` — one `GROUP BY provider, tool_name` query
  (with the `mcp_*` → `mcp` alias done in SQL), then the rare-tool collapse in Python (summing is
  order-independent). Returns `{provider: {tool_name: ToolCounts(calls, error_calls)}}`.
- `plot_tool_counts(...)` — builds the per-provider panels and the clipped-axis figure.
- `tool_count_panel_cap(...)` — the shared clip/annotation rule, used by both the plot and the CSV
  so the table's `panel_cap` / `*_plot_width` columns match the rendered bars exactly.
- `write_tool_call_counts_by_provider(...)` — the full-detail CSV.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) to the
  above and embeds the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/tool_calls/tool_call_counts/plot.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/tool_calls/tool_call_counts/plot.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/tool_calls/tool_call_counts/plot.py --db /tmp/trace.duckdb -o /tmp/out
```

Useful flags: `--top-tools` (max bars per panel, default 30), `--min-tool-calls-for-plot`
(rare-tool collapse threshold, default 20).

## Outputs (written to `-o`, default this folder)

- `tool_call_counts.png` — provider-paneled tool call counts with error overlay.
- `tool_call_counts_by_provider.csv` — full per-tool counts: `calls`, `error_calls`, `error_rate`,
  plus the plot-geometry columns (`panel_cap`, `call_plot_width`, `call_is_clipped`, …).

The PNG is self-contained — it embeds this README, the CSV, and the plotting code. Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### tool_call_counts.png

A small number of tools account for the overwhelming majority of calls on both providers — the
clipped leader bar (annotated with its true count) towers over everything else, which is exactly
why the axis is clipped at the second-largest bar. Reading the panels:

- The shape is **heavy-headed**: file/shell primitives dominate, and the long tail of
  specialized or MCP tools is comparatively thin — visible as the collapsed `Other` bar.
- The **error overlay** (red) is where to look for reliability differences: tools with a visible
  red segment are the ones whose calls fail often enough to matter at this volume. Per-tool
  `error_rate` is in the CSV for exact figures.
- Comparing the two provider panels shows how Claude Code and Codex differ in *tool vocabulary* —
  which named tools each exposes and leans on — independent of how many sessions each contributed.
