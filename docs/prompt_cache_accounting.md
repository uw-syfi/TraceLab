# Prompt Cache Accounting

## Claude

Claude exposes separate input-side usage counters:

```text
input_tokens_total = input_tokens
                   + cache_creation_input_tokens
                   + cache_read_input_tokens
```

In the normalized trace:

```text
prefix_tokens = cache_read_input_tokens
newly_append_tokens = input_tokens + cache_creation_input_tokens
```

Adjacent-round evidence from readable raw Claude sessions:

```text
next.cache_read_input_tokens ~= last input_tokens_total
```

The previous assistant output is not part of the same round's input/cache write,
but when it is replayed as conversation history, it usually appears in the next
round's cache write:

```text
next.cache_creation_input_tokens >= last.output_tokens
  overall adjacent pairs: 98.0%
  next round starts from tool_result: 98.39%
  next round starts from user_message: 94.65%
```

Long current user/tool input also inflates the same round's cache write:

```text
tool_result_chars >= 50k:
  median cache_creation_input_tokens = 22,518

user_input_chars >= 10k:
  median cache_creation_input_tokens = 25,727
```

Practical model:

```text
this_round.cache_read_input_tokens
  ~= cached prefix from previous request

this_round.cache_creation_input_tokens
  ~= previous assistant response replayed into prompt
   + this round's new user/tool-result content
   + message/tool framing and cache-boundary overhead
```

Caveats:

- `output_tokens` can include hidden/thinking or max-token output that may not be
  replayed as visible assistant message content.
- Raw Claude Code tool logs are not always equal to what is sent to the model;
  some large raw tool outputs appear clipped, compacted, or shifted across cache
  boundaries.
- Therefore, subtracting `last.output_tokens` from
  `input_tokens + cache_creation_input_tokens` is useful as an approximation for
  external context growth, but it is not a strict identity.

## Codex

Codex traces do not expose a separate `cache_creation_input_tokens` field. The
observable split is:

```text
prefix_tokens = cached_input_tokens
newly_append_tokens = input_tokens_total - cached_input_tokens
```

So the closest observable to Claude's cache write is `newly_append_tokens`, but
it is cache-miss/new-append accounting rather than an explicit cache-write
counter.

Adjacent-round evidence from `trace/llm_round_trace.merged.all_users.jsonl`:

```text
next.prefix_tokens ~= last.input_tokens_total
  median error: -183 tokens

next.newly_append_tokens >= last.visible_output_tokens
  95.49% of adjacent pairs

next.newly_append_tokens >= last.output_tokens
  91.30% of adjacent pairs
```

For Codex, `output_tokens` includes reasoning tokens. `visible_output_tokens` is
estimated as:

```text
max(0, output_tokens - reasoning_output_tokens)
```

This makes the previous-output relationship cleaner, because reasoning tokens
are generally not replayed as visible conversation history.

Long current user input strongly increases Codex `newly_append_tokens`:

```text
user_chars >= 10k:
  median newly_append_tokens = 7,206
  newly_append_tokens >= raw_chars / 4 in 3,376 / 3,713 rows

user_chars >= 50k:
  median newly_append_tokens = 26,005
  newly_append_tokens >= raw_chars / 4 in 84 / 90 rows
```

Long current tool output also increases append tokens, but less reliably:

```text
tool_chars >= 10k:
  median newly_append_tokens = 6,167

tool_chars >= 50k:
  median newly_append_tokens = 15,891

tool_chars >= 100k:
  median newly_append_tokens = 10,057
```

The weak relationship for extremely large raw tool outputs suggests Codex, like
Claude Code, may truncate, summarize, or otherwise avoid sending the full raw
tool output to the model. The raw trace `result_chars` value is therefore not
always a direct prompt-size measurement.

Practical Codex model:

```text
this_round.prefix_tokens
  ~= cached prefix from previous request

this_round.newly_append_tokens
  ~= previous visible assistant response
   + this round's new user/tool-result content actually sent to the model
   + message/tool framing and cache-boundary effects
```

Compared with Claude, Codex shows the same qualitative effects but with more
jitter, and without a direct public/raw counter for cache writes.

## Current Codex Trace Caveat

The Codex extractor currently has subagent-aware session assignment in
`scripts/extract_codex_rounds.py`: subagent files keep live child turns under
the child session while replayed parent turns are assigned back to the parent
session id for deduplication. The checked `trace/llm_round_trace.merged.all_users.jsonl`
was written before that extractor update, so large positive prefix jumps found
in that trace may include subagent replay artifacts. Recollect before treating
the current Codex jump counts as final.
