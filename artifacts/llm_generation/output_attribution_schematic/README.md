# output_attribution_schematic

**A conceptual illustration (not a data plot) of how a prior agent step's *output* tokens are
accounted in the *next* step's prompt: folded into the cached prefix, or re-sent as new billed
input.**

## What it shows

Each step is one horizontal stacked bar `[prefix | new input | output]`. Two cases are drawn with
fixed, representative segment lengths:

- **(a) Output cached as prefix (ideal reuse).** The next step's cached prefix equals the prior
  step's *entire* composition: `10 + 2 + 4 = 16`. The next step still has its own new input
  and output: `16 | 1 | 2`.
- **(b) Output re-sent as new input (observed).** The next step's cached prefix stops *before* the
  prior output (dashed line aligns with the prior `prefix + new input` boundary, `10 + 2 = 12`).
  The prior output is therefore re-sent as part of the next step's new, billed input: `4 + 1 = 5`,
  followed by the next step's output: `12 | 5 | 2`.

This figure motivates the data-driven companion analysis in `../output_append_assignment`, which
measures how often each case actually occurs across adjacent steps in the trace.

## Reproduce

```bash
uv run python plot.py            # one-LaTeX-column (compact) PNG + PDF
uv run python plot.py --full     # larger size for slides/inspection
```

Outputs `output_attribution_schematic.{png,pdf}` in this directory. The paper embeds the PDF
(copied to `paper/figures/output_attribution.pdf`, wired in via
`paper/figure-tex/fig_output_attribution.tex`).

## Notes / assumptions

- **No trace data is read.** Segment lengths are illustrative units chosen for clarity, not measured
  token counts. The only requirement the drawing encodes is the qualitative relationship: in (b) the
  re-sent output makes the next step's new input longer than the prior output.
- Palette uses the shared paper colors: dark blue = cached prefix, light blue = newly billed
  input, orange = output.
