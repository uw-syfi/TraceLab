# session_internal_counts

**一个编码会话、以及一次请求，包含多少工作量？**

计算 `tab:session_internal_counts`（`src/04_SessionContext.tex`）背后的计数分布：每个
会话的请求数、用户触发 / 工具触发的步骤数以及工具调用数；每个请求的
工具触发步骤数和工具调用数；以及每个步骤的工具调用数——每一项都给出
avg / p25 / p50 / p90 / p99。

## 运行方式

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/session/session_internal_counts/analyze.py

# the pinned public trace
uv run python artifacts/session/session_internal_counts/analyze.py --db trace/syfi_coding_trace.duckdb

# a prebuilt DB, into a chosen dir
uv run python artifacts/session/session_internal_counts/analyze.py --db "$TMPDIR/trace.duckdb" -o "$TMPDIR/out"
```

## 输出

- `session_internal_counts.tex` —— 合并后的三行（booktabs）表格，可直接 `\input` 或
  粘贴进 `src/04_SessionContext.tex`。
- `session_internal_counts.md` —— 该表格的 GFM Markdown 镜像，渲染在网页详情页上。
- `headline.json` —— 用于 Overview gallery 卡片的几个 headline 数字。
- stdout —— 完整的合并 + 按提供商（Claude / Codex）的明细。

无图。

## SyFI result analysis

### session_internal_counts.md

编码会话是持久的，且压倒性地自主（论文的
`tab:session_internal_counts`）。一个会话平均有 10.1 个请求，但带有一条长尾（p99 = 161），所以人类会一次又一次地回到同一个会话。每个会话中工具触发步骤（avg 69.9）远多于用户触发步骤（avg 9.8），所以一旦一个请求落地，循环就自行运转起来：解决一个请求平均需要 6.7 个工具触发步骤和 8.9 次工具调用。在步骤这一层级，每一轮只发出略多于一次的工具调用（avg 1.1，p50 1，p90 2），所以并行工具调用确实会发生，但属于例外而非常态。

## Shared request ownership update

Request groups use `artifacts/utils/request_timing.py`. Native turn identity owns rows; legacy user-triggered groups remain countable for workload and attributed cost. Missing completion or nonpositive/contradictory timestamps no longer erase those counts. Only native completed lifecycles with consistent timestamps yield response duration. This can increase counts relative to historical tables that dropped groups solely for missing/nonpositive duration. Rebuild generated outputs for the intended capture.
