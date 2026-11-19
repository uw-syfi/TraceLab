import assert from 'node:assert/strict';
import test from 'node:test';
import { subagentHeadlineRows } from './headlines.ts';

test('headline comparisons pair providers and preserve full values behind compact text', () => {
  const rows = subagentHeadlineRows([
    { label: 'Codex average rounds per main session', value: '841.61' },
    { label: 'Claude average rounds per main session', value: '1,694.50' },
    { label: 'Claude unrelated measure', value: '99' },
  ]);
  assert.deepEqual(rows, [{
    label: 'Avg rounds / delegating main',
    measure: 'average rounds per main session',
    values: [
      { value: '1,694.50', display: '1.7K' },
      { value: '841.61', display: '841.61' },
    ],
  }]);
});

test('headline comparisons preserve zero and missing provider measurements', () => {
  assert.deepEqual(subagentHeadlineRows([
    { label: 'Claude main sessions with subagents', value: '0.0%' },
  ])[0].values, [
    { value: '0.0%', display: '0.0%' },
    { value: 'N/A', display: 'N/A' },
  ]);
  assert.deepEqual(subagentHeadlineRows([]), []);
});

test('workload previews label medians separately from legacy mean values', () => {
  const rows = subagentHeadlineRows([
    { label: 'Claude median rounds per main session', value: '146.50' },
    { label: 'Codex median rounds per main session', value: '156.00' },
    { label: 'Claude median rounds per subagent', value: '23.00' },
  ]);
  assert.equal(rows[0].label, 'Median rounds / delegating main');
  assert.deepEqual(rows[0].values.map((row) => row.value), ['146.50', '156.00']);
  assert.equal(rows[1].label, 'Median rounds / subagent');
  assert.equal(rows[1].values[1].value, 'N/A');
});
