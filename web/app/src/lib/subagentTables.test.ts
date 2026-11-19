import assert from 'node:assert/strict';
import test from 'node:test';
import { createMarkdownProcessor } from '@astrojs/markdown-remark';
import { rehypeSubagentTables, subagentTableBody } from './subagentTables.ts';

test('page skips the duplicated standalone introduction but keeps section notes', () => {
  assert.equal(subagentTableBody('# Report\n\nIntro\n\n## Coverage\n\nKeep this note.'), '## Coverage\n\nKeep this note.');
  assert.equal(subagentTableBody('No sections here.'), 'No sections here.');
});

test('comparison tables keep three columns and accessible headers, including missing values', async () => {
  const processor = await createMarkdownProcessor({ rehypePlugins: [rehypeSubagentTables] });
  const { code } = await processor.render(
    '| Measure | Claude | Codex |\n| :-- | --: | --: |\n| Calls | 0 | N/A |\n| <code>a&#124;b&lt;c&gt;</code> | 1 | 2 |',
  );
  assert.equal((code.match(/scope="col"/g) ?? []).length, 3);
  assert.equal((code.match(/scope="row"/g) ?? []).length, 2);
  assert.match(code, /class="subagent-table-scroll" tabindex="0" role="region" aria-label="Claude \/ Codex"/);
  assert.match(code, /<th[^>]*scope="row"[^>]*>Calls<\/th>/);
  assert.match(code, />0<\/td>/);
  assert.match(code, />N\/A<\/td>/);
  assert.match(code, /<code>a\|b&#x3C;c><\/code>/);
  assert.match(code, /scope="col" data-provider="claude">Claude<\/th>/);
});

test('distribution tables retain separate statistics and provider row headers', async () => {
  const processor = await createMarkdownProcessor({ rehypePlugins: [rehypeSubagentTables] });
  const { code } = await processor.render(
    '### Rounds per session\n\n| Provider | Avg | P25 | P50 | P90 | P99 |\n| :-- | --: | --: | --: | --: | --: |\n| Claude | 2 | 1 | 2 | 3 | 4 |\n| Codex | N/A | N/A | N/A | N/A | N/A |',
  );
  assert.equal((code.match(/scope="col"/g) ?? []).length, 6);
  assert.equal((code.match(/scope="row"/g) ?? []).length, 2);
  assert.match(code, /scope="row" data-provider="claude">Claude<\/th>/);
  assert.match(code, /scope="row" data-provider="codex">Codex<\/th>/);
  assert.equal((code.match(/>N\/A<\/td>/g) ?? []).length, 5);
  assert.doesNotMatch(code, /<br|data-provider="[^"]+">Avg/);
});

const comparison = (rows: string) => '| Measure | Claude | Codex |\n| :-- | --: | --: |\n' + rows;
async function curated(slug: string, md: string) {
  const processor = await createMarkdownProcessor({ rehypePlugins: [[rehypeSubagentTables, { slug }]] });
  return (await processor.render(md)).code;
}

test('adoption keeps cohort, unknown, and zero coverage visible', async () => {
  const code = await curated('subagents/adoption', '## Sessions\n\n' + comparison(
    '| Main sessions | 4 | 8 |\n| Main sessions with confirmed roots | 4 | 7 |\n' +
    '| Subagents (linked or not) | 2 | 3 |\n| Subagents in these families | 2 | 3 |\n' +
    '| Sessions with unknown origin | 0 | 0 |\n| Sessions with unavailable family links | N/A | 0 |',
  ));
  assert.match(code, /Main sessions with confirmed roots/);
  assert.match(code, /Sessions with unavailable family links/);
  assert.match(code, /Subagents in these families/);
  assert.match(code, /Sessions with unknown origin/);
});

test('workload retains population coverage and zero-round comparisons', async () => {
  const code = await curated('subagents/workload', '## Sessions and coverage\n\n' + comparison(
    '| Observed sessions | 10 | 20 |\n| Sessions outside these groups | 2 | 3 |\n' +
    '| Linked sessions outside these families | 0 | 0 |\n| Sessions with unavailable family links | 0 | 0 |',
  ) + '\n\n## Subagents\n\n' + comparison(
    '| Sessions with no tool calls | 0 (0.0%) | 36 (18.6%) |\n| Sessions with one round | 0 (0.0%) | 0 (0.0%) |',
  ));
  assert.match(code, /Sessions and coverage/);
  assert.match(code, /Observed sessions/);
  assert.match(code, /Sessions outside these groups/);
  assert.match(code, /Sessions with one round/);
  assert.match(code, /Sessions with no tool calls/);
  assert.match(code, />0 \(0\.0%\)<\/td>/);
});

test('token coverage keeps exceptions and unavailable evidence, without the validation tail', async () => {
  const code = await curated('subagents/tokens_and_reports', [
    '## First and peak input per session\n### Main sessions\n#### Input coverage\n',
    comparison('| Sessions | 2 | 3 |\n| Sessions with incomplete input coverage | 0 | 0 |\n| Rounds with valid input | 4 | 5 |\n| Rounds | 4 | 5 |'),
    '\n### Subagents\n#### Input coverage\n',
    comparison('| Sessions | 2 | 3 |\n| Sessions with incomplete input coverage | 1 | 0 |\n| Rounds with valid input | 3 | 5 |\n| Rounds | 4 | 5 |'),
    '\n## Report evidence coverage\n### Main sessions\n',
    comparison('| Captured evidence | Evidence available | No report evidence |\n| Calls with valid report evidence | 10 | 0 |\n| Calls with valid timing | 8 | 0 |\n| Missing timing | 2 | 0 |\n| Missing call ID | 0 | 0 |'),
    '\n### Other recipients\nOutside the displayed families.\n',
    comparison('| Known errors (excluded) | 3 | 0 |'),
  ].join('\n'));
  assert.equal((code.match(/>Input coverage<\/h4>/g) ?? []).length, 1);
  assert.match(code, /Sessions with incomplete input coverage/);
  assert.match(code, /No report evidence/);
  assert.match(code, /Calls with valid timing/);
  assert.match(code, /Missing timing/);
  assert.doesNotMatch(code, /Missing call ID|Other recipients|Outside the displayed families/);
});

test('tool usage retains summaries and coverage while omitting the exhaustive tool tail', async () => {
  const code = await curated('subagents/tool_usage', [
    '## Subagents\n',
    comparison('| Tool calls | 10 | 20 |\n| Known successes | 9 | 10 |\n| Unknown outcomes | 0 | 8 |\n| Outcome coverage | 100% | 60% |\n| Calls with missing time | 0 | 2 |'),
    '\n## Tools outside the figure\n',
    comparison('| Calls | 2 | 3 |'),
    '\n## Every recorded tool\n### Subagents\n#### Bash\n',
    comparison('| Calls | 9 | N/A |'),
    '\n## End note\nKeep this note.',
  ].join('\n'));
  assert.equal((code.match(/<table>/g) ?? []).length, 2);
  assert.match(code, /Outcome coverage/);
  assert.match(code, /Calls with missing time/);
  assert.match(code, /Known successes/);
  assert.match(code, /Unknown outcomes/);
  assert.match(code, /Tools outside the figure/);
  assert.doesNotMatch(code, /Every recorded tool|Bash/);
  assert.match(code, /Keep this note/);
});
