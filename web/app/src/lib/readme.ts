// Parse the toolkit's structured experiment READMEs into the pieces the detail page lays out:
//   README preamble             -> shown ABOVE the figures (usually the driving question)
//   ## SyFI result analysis      -> ### <figure>.png subsections, shown AFTER each matching image
//   ## Code structure / Running it / Outputs / … -> reference block below
//
// The README standard is locked in artifacts/README.md; this mirrors it. Matching of analysis to a
// figure is by basename (### tool_call_counts.png ↔ /figures/tool_call_counts.png). Experiments
// whose figures are named dynamically (e.g. session_token_steps' per-session PNGs) carry a single
// generic ### subsection — when there is exactly one, it applies to every figure.

export interface ParsedReadme {
  /** Intro/question preamble, markdown. */
  overviewMd: string;
  /** Remaining sections (Experiment overview, Code structure, Running it, Outputs, …), markdown. */
  detailsMd: string;
  /** Remaining sections split by their ## heading, for expandable reference rows. */
  detailsSections: ReadmeSection[];
  /** Per-figure analysis keyed by figure basename (the ### heading text). */
  analysisByFigure: Record<string, string>;
  /** ### headings in document order (for the single-generic fallback). */
  analysisHeadings: string[];
}

export interface ReadmeSection {
  title: string;
  body: string;
}

/** Split markdown into sections at a given ATX heading marker (e.g. "## " or "### "). */
function splitByHeading(md: string, marker: string): { preamble: string; sections: ReadmeSection[] } {
  const lines = md.split('\n');
  const sections: ReadmeSection[] = [];
  const preamble: string[] = [];
  let title: string | null = null;
  let buf: string[] = [];
  const flush = () => {
    if (title !== null) sections.push({ title: title.trim(), body: buf.join('\n').trim() });
  };
  for (const line of lines) {
    if (line.startsWith(marker)) {
      flush();
      title = line.slice(marker.length);
      buf = [];
    } else if (title !== null) {
      buf.push(line);
    } else {
      preamble.push(line);
    }
  }
  flush();
  return { preamble: preamble.join('\n').trim(), sections };
}

const isOverview = (t: string) => /experiment overview/i.test(t);
const isAnalysis = (t: string) => /result analysis/i.test(t);

export function parseExperimentReadme(md: string): ParsedReadme {
  // Drop the leading H1 (the page hero already shows the title).
  const body = md.replace(/^#\s+[^\n]*\n+/, '');
  const { preamble, sections } = splitByHeading(body, '## ');

  const overviewParts: string[] = [];
  if (preamble) overviewParts.push(preamble);
  const detailParts: string[] = [];
  const detailsSections: ReadmeSection[] = [];
  const analysisByFigure: Record<string, string> = {};
  const analysisHeadings: string[] = [];

  for (const sec of sections) {
    if (isOverview(sec.title)) {
      detailParts.push(`## ${sec.title}\n\n${sec.body}`);
      detailsSections.push(sec);
    } else if (isAnalysis(sec.title)) {
      const { sections: figs } = splitByHeading(sec.body, '### ');
      for (const fig of figs) {
        analysisByFigure[fig.title] = fig.body;
        analysisHeadings.push(fig.title);
      }
    } else {
      detailParts.push(`## ${sec.title}\n\n${sec.body}`);
      detailsSections.push(sec);
    }
  }

  return {
    overviewMd: overviewParts.join('\n\n').trim(),
    detailsMd: detailParts.join('\n\n').trim(),
    detailsSections,
    analysisByFigure,
    analysisHeadings,
  };
}

/** The basename of a figure src, e.g. "/figures/tool_call_counts.png" -> "tool_call_counts.png". */
export function figureBasename(src: string): string {
  const clean = src.split('?')[0].split('#')[0];
  return clean.slice(clean.lastIndexOf('/') + 1);
}

/** Analysis markdown for a figure: exact basename match, else the single generic subsection. */
export function analysisForFigure(parsed: ParsedReadme, src: string): string {
  const base = figureBasename(src);
  if (parsed.analysisByFigure[base]) return parsed.analysisByFigure[base];
  for (const heading of parsed.analysisHeadings) {
    if (!heading.includes('{N}')) continue;
    const pattern = heading
      .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      .replace('\\{N\\}', '\\d+');
    if (new RegExp(`^${pattern}$`).test(base)) return parsed.analysisByFigure[heading];
  }
  if (parsed.analysisHeadings.length === 1) {
    return parsed.analysisByFigure[parsed.analysisHeadings[0]];
  }
  return '';
}
