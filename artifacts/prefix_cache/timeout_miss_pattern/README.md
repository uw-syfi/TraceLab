# timeout_miss_pattern

**Why is one user round an expensive full cache miss when nothing was forgotten?**
Because a long idle wait expires the prefix cache while the context keeps growing.

## Experiment overview

This is a **synthetic schematic**, not a measurement — it takes no trace input. It hand-shapes a
single session's `prefix` / `append` token counts and draws them with the exact stacked-bar idiom of
the real per-session plot (`session/session_token_steps`): one x position per LLM step, a light bar
for **Prefix Tokens** served from cache (a cache **hit**), and a dark bar stacked on top for the
**Append Tokens** (the **miss** portion). A fully-dark bar is therefore a total miss at a glance.

It exists to correct a common wrong mental model — that the context "resets" after an idle gap — and
to isolate the one real effect of the gap.

Accounting used (the honest cache-replay model, see `../cache_replay`):

- **On a cache hit**, the cached prefix is the *entire* previous step's input, and the append is only
  what is genuinely new since then (the model's prior reply + the new tool result or user message).
  So a normal tool-loop step appends little and its bar is almost all light with a thin dark tip.
- **On a timeout miss**, the cache was evicted during the idle wait, so `prefix = 0` and the whole
  accumulated context is re-appended at full price — one tall, fully-dark bar.
- **Either way the total (`prefix + append`) only ever increases.** The idle gap expires the
  *cache*, never the *context*; nothing is forgotten.

The shaped session has three phases, separated by two idle gaps drawn the way the reference figure
draws them — a **horizontal break in the bar run with an analog-clock icon** sitting in it:

1. **User round 0 + tool loop** — warm hits (thin dark tips).
2. **User round 1 + tool loop**, entered after a **small (< 1 min) gap** — cache still valid, still a
   hit (clock caption in green).
3. A **larger (> 5 min) gap** expires the cache, so the next user round is a **full re-prefill** (the
   whole, now-large context is a miss; clock caption in red), after which the tool loop warms back up.

The short idle gap is drawn smaller than the >5-minute timeout gap to match the reference schematic,
while still avoiding a fully time-scaled x-axis. The clock icons use fixed-pixel `DrawingArea`
objects, so they stay true circles regardless of the token-vs-step data aspect ratio.

The teaching point sits at that one bar: it carries the *same* total context a hit would have, but a
totally different cost — only the cache went cold.

## Code structure

`plot.py` is fully self-contained:

- `build_steps()` — deterministically (seeded) generates the `Step` sequence with the hit/miss
  accounting above; returns the steps and the index of the timeout-miss bar.
- `plot()` — inserts the two horizontal idle-gap breaks into the x-layout, then renders the stacked
  bars, the user-initiated step verticals, the clock icons, and the group/callout annotations.
- `clock_icon()` / `place_clock()` — the aspect-correct analog clock (a fixed-pixel `DrawingArea`)
  and its placement.
- `group_bracket()` — the flat top brackets labelling each phase.
- `main()` — writes the PNG and embeds the self-contained sidecar (this README + the script).

No `trace_db` / `--db` / `-i` surface: the only flag is `-o/--output-dir`.

## Running it

```bash
# render next to this README
uv run python artifacts/prefix_cache/timeout_miss_pattern/plot.py

# render elsewhere
uv run python artifacts/prefix_cache/timeout_miss_pattern/plot.py -o /tmp/out
```

## Outputs

Written to `-o` (default this folder):

- `timeout_miss_pattern.png` — the schematic. Self-contained: it embeds this README and the script.
  Unpack with `python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### timeout_miss_pattern.png

Read it left to right. Nearly every bar is light with only a thin dark cap — those are cache hits,
where the dark cap is the few thousand genuinely-new tokens and the entire prior context was served
from cache. The bar tops only ever rise: context accumulates monotonically, and crucially it does
**not** dip across the idle gap. The single fully-dark bar (`U2`) is the timeout miss: after a
>5-minute idle wait the cache was evicted, so the next user round re-prefilled the whole accumulated
context at full price. It is tall for the same reason its neighbours are tall — the context kept
growing — but it is *dark* because none of it was cached. The contrast against the thin-tipped hit
bars on either side is the whole story: idle time costs you a re-prefill of everything you have
accumulated, not because anything was forgotten, but because the cache went cold.
