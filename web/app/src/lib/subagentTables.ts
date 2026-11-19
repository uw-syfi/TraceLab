import type { Root, Element, RootContent } from 'hast';

// The README already introduces the page; keep the standalone report's introduction in its file.
export const subagentTableBody = (md: string) => md.replace(/^[\s\S]*?(?=^## )/m, '');

const elements = (node: Root | Element) => node.children.filter((child): child is Element => child.type === 'element');
const textOf = (node: RootContent): string => 'value' in node ? String(node.value) : 'children' in node ? node.children.map(textOf).join('') : '';
const zero = (value: string) => /^0(?:\.0+)?%?(?:\s*\(0(?:\.0+)?%\))?$/.test(value.trim());
const quietRows = new Set([
  'Sessions with unknown origin', 'Linked sessions outside these families',
  'Sessions with unavailable family links', 'Sessions with no tool calls', 'Sessions with one round',
  'Families excluded for missing or invalid usage', 'Rounds with missing or invalid usage',
  'Calls with missing time', 'Calls with zero or negative time',
]);

// Curate the web view only. The generated reports retain every measurement and diagnostic.
function curateTable(table: Element, slug: string, headings: string[]): boolean {
  const body = elements(table).find((child) => child.tagName === 'tbody');
  if (!body) return true;
  if (['subagents/adoption', 'subagents/workload', 'subagents/tool_usage'].includes(slug)) return true;
  const rows = elements(body);
  const values = (row: Element) => elements(row).slice(1).map(textOf);
  const label = (row: Element) => textOf(elements(row)[0]);
  const named = (name: string) => rows.find((row) => label(row) === name);
  const same = (a: string, b: string) => {
    const left = named(a), right = named(b);
    return !!left && !!right && JSON.stringify(values(left)) === JSON.stringify(values(right));
  };
  const allZero = (row: Element) => values(row).length > 0 && values(row).every(zero);
  const section = headings.filter(Boolean).at(-1);
  const incompleteInput = named('Sessions with incomplete input coverage');
  if (slug === 'subagents/tokens_and_reports' && section === 'Input coverage' &&
      incompleteInput && allZero(incompleteInput) &&
      same('Rounds with valid input', 'Rounds')) return false;

  body.children = body.children.filter((node) => {
    if (node.type !== 'element' || node.tagName !== 'tr') return true;
    const name = label(node);
    if (quietRows.has(name) && allZero(node)) return false;
    if (slug === 'subagents/tokens_and_reports') {
      if (section === 'Coverage and subagent share') {
        if (name === 'Rounds') return false;
        if (name === 'Families with complete usage' && same(name, 'All recorded families')) return false;
        if (name === 'Complete families with a positive token total' && same(name, 'Families with complete usage')) return false;
      }
      if (headings.includes('Report evidence coverage')) {
        if (['Tool calls inspected', 'Calls without report evidence'].includes(name)) return false;
        if (['Calls with saved report fields', 'Calls with valid timing'].includes(name) && same(name, 'Calls with valid report evidence')) return false;
        if (!['Calls with valid report evidence', 'Calls with valid timing', 'Rounds receiving results'].includes(name) && allZero(node)) return false;
      }
    }
    return true;
  });
  return elements(body).length > 0;
}

function curateSections(tree: Root, slug: string) {
  const headings: string[] = [];
  const kept: RootContent[] = [];
  let skippedDepth = 0;
  // Heading blocks keep their explanatory notes; empty diagnostic blocks disappear together.
  for (let i = 0; i < tree.children.length;) {
    const start = i++;
    while (i < tree.children.length && !(tree.children[i].type === 'element' && /^h[1-6]$/.test((tree.children[i] as Element).tagName))) i++;
    const block = tree.children.slice(start, i);
    const first = block[0];
    if (first.type === 'element' && /^h[1-6]$/.test(first.tagName)) {
      const depth = Number(first.tagName[1]);
      headings.length = depth;
      headings[depth - 1] = textOf(first);
      if (skippedDepth && depth <= skippedDepth) skippedDepth = 0;
      if ((slug === 'subagents/tool_usage' && textOf(first) === 'Every recorded tool') ||
          (slug === 'subagents/tokens_and_reports' && headings.includes('Report evidence coverage') && textOf(first) === 'Other recipients')) skippedDepth = depth;
    }
    if (skippedDepth) continue;
    let hadTable = false, hasTable = false;
    const filtered = block.filter((node) => {
      if (node.type !== 'element' || node.tagName !== 'table') return true;
      hadTable = true;
      const keep = curateTable(node, slug, headings);
      hasTable ||= keep;
      return keep;
    });
    if (!hadTable || hasTable) kept.push(...filtered);
  }
  tree.children = kept;
}

/** Give the generated comparison tables real row and column headers. */
export function rehypeSubagentTables({ slug = '' }: { slug?: string } = {}) {
  return (tree: Root) => {
    if (slug) curateSections(tree, slug);
    function visit(node: Root | RootContent) {
      if (node.type === 'element' && node.tagName === 'tr') {
        const cells = node.children.filter((child): child is Element =>
          child.type === 'element' && (child.tagName === 'td' || child.tagName === 'th'));
        cells.forEach((cell, index) => {
          if (cell.tagName === 'th') cell.properties.scope = 'col';
          else if (index === 0) {
            cell.tagName = 'th';
            cell.properties.scope = 'row';
          }
          const text = cell.children[0];
          if (cell.tagName === 'th' && text?.type === 'text' && /^(Claude|Codex)$/.test(text.value)) {
            cell.properties['data-provider'] = text.value.toLowerCase();
          }
        });
      }
      if ('children' in node) {
        node.children.forEach((child, index) => {
          visit(child);
          if (child.type === 'element' && child.tagName === 'table') {
            node.children[index] = { type: 'element', tagName: 'div', properties: {
                className: ['subagent-table-scroll'], tabIndex: 0, role: 'region', ariaLabel: 'Claude / Codex',
              }, children: [child] };
          }
        });
      }
    }
    visit(tree);
  };
}
