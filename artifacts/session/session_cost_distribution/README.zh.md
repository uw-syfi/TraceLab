# session_cost_distribution

**一个编码会话 / 请求 / 步骤花费多少，钱又都花到哪里去了？**

计算 `tab:cost_distribution`（`src/04_SessionContext.tex`）背后的 USD 成本分布。对每种粒度（按会话、按请求、按步骤）以及每个计费类别，论文表格报告**成本**的 avg / p50 / p90 / p99 以及该类别在总支出中的占比（脚本还会把底层的 token 分布，含 p25，打印到 stdout）：

- **追加 token** —— `newly_append_tokens`，按新鲜输入费率计费。
- **前缀 token** —— `prefix_tokens`，按缓存读取费率计费。
- **输出 token** —— `output_tokens`（包含推理），按输出费率计费。
- **总计** —— 上述三者之和。

## 定义

- **成本**使用单一来源的价格表 `artifacts/utils/pricing.json`，经由
  `web_analytics/pricing.py`（`price_for` → 按模型做精确/family 解析；`round_cost` → 追加按输入/缓存写入费率、前缀按缓存读取费率、输出按输出费率——与网页 dashboard 所用的同一套计费）。模型没有价格的轮次为*未计价*并被排除；分析会打印所选 trace 的准确已计价与未计价覆盖率。
- **请求** —— 一个用户轮次，经由与
  `human_in_the_loop/user_turn_decomposition` 相同的轮次状态机。**步骤** —— 一个 LLM 轮次。**会话** —— 一个 `session_id`。

## 运行方式

```bash
uv run python artifacts/session/session_cost_distribution/analyze.py --db trace/syfi_coding_trace.duckdb
uv run python artifacts/session/session_cost_distribution/analyze.py            # default merged trace
```

## 输出

- `session_cost_distribution.tex` —— 合并后的单列成本表（Avg / P50 / P90 / P99
  + % cost），用于论文。
- `session_cost_distribution.md` —— 该表格的 GFM Markdown 镜像，渲染在网页详情页上。
- `headline.json` —— 用于 Overview gallery 卡片的几个 headline 数字。
- stdout —— 合并 + 按提供商（Claude / Codex）的 token 与成本分位数，以及
  追加 / 前缀 / 输出的成本构成。

## 关键数字（公开数据，列表价截至 2026-07）

- **成本构成：前缀/缓存 58.6%，追加/新鲜输入 30.5%，输出 11.0%。** 尽管有约 ~10× 的缓存读取折扣，缓存输入仍纯粹靠体量主导了支出。
- 平均成本：**$11.8 / 会话**、**$1.10 / 请求**、**$0.14 / 步骤**；中位数则低得多
  （$0.79 / $0.34 / $0.09），并带有沉重的会话尾部（p99 = $209）。

无图。

## SyFI result analysis

### session_cost_distribution.md

对一个编码智能体而言，账单是由重新读取上下文主导的，而不是由生成主导（论文的
`tab:cost_distribution`）。即便缓存**前缀 token 的计费费率大约只有新鲜输入费率的十分之一**，它们仍占总支出的 **58.6%**——纯属体量，因为不断累积的上下文在每个步骤上都被重放——相比之下追加/新鲜输入为 **30.5%**，而输出仅为 **11.0%**。尽管输出的每 token 价格很高，但它在总量上很便宜，因为每个步骤发出的 token 太少。绝对成本在中位数处是适度的（$0.79/会话，$0.34/请求，$0.09/步骤），但带有沉重的尾部：平均每个会话为 $11.8，p99 达到 **$209**，少数极长的会话驱动了大部分支出。这与「生成才是昂贵部分」的通常直觉恰好相反。

## Shared request ownership update

Request groups use `artifacts/utils/request_timing.py`. Native turn identity owns rows; legacy user-triggered groups remain countable for workload and attributed cost. Missing completion or nonpositive/contradictory timestamps no longer erase those counts. Only native completed lifecycles with consistent timestamps yield response duration. This can increase counts relative to historical tables that dropped groups solely for missing/nonpositive duration. Rebuild generated outputs for the intended capture.
