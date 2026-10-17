/* Pure helper tests for the suggestions panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./suggestions-panel.js');

test('kindLabel names every suggestion kind', () => {
  assert.equal(panel.kindLabel('failed_task_recovery'), 'Failed task');
  assert.equal(panel.kindLabel('automation_stale'), 'Stale automation');
  assert.equal(panel.kindLabel('something_else'), 'something_else');
});

test('formatAge is deterministic and honest about missing timestamps', () => {
  const now = 1000000;
  assert.equal(panel.formatAge(now - 5, now), 'just now');
  assert.equal(panel.formatAge(now - 30, now), '30s ago');
  assert.equal(panel.formatAge(now - 600, now), '10m ago');
  assert.equal(panel.formatAge(now - 7200, now), '2h ago');
  assert.equal(panel.formatAge(null, now), 'unknown');
});

test('summaryLine counts kinds and the evaluation age', () => {
  const report = {
    total: 2, generated_at: 1000000 - 120,
    counts: { failed_task_recovery: 1, scheduled_job: 1 }
  };
  assert.equal(panel.summaryLine(report, 1000000),
    '2 active · 1 failed task, 1 scheduled job · evaluated 2m ago');
  assert.equal(panel.summaryLine({ total: 0 }, 1000000), '');
  assert.equal(panel.summaryLine(null, 1000000), '');
});

test('evidenceText cites type, id, and detail', () => {
  assert.equal(panel.evidenceText({ type: 'task', id: 't1', detail: 'status=failed' }),
    'task t1 — status=failed');
  assert.equal(panel.evidenceText({ type: 'skill', id: 's1' }), 'skill s1');
  assert.equal(panel.evidenceText(null), '');
});

test('actions are only enabled when the server says so', () => {
  assert.equal(panel.actionEnabled({}), true);
  assert.equal(panel.actionEnabled({ available: true }), true);
  assert.equal(panel.actionEnabled({ available: false }), false);
  assert.equal(panel.actionTitle({ available: false, reason: 'runs needed' }), 'runs needed');
  assert.equal(panel.actionTitle({ available: false }), 'Not available yet');
  assert.equal(panel.actionTitle({ available: true }), '');
});

test('describeAct and describeDecision never overstate what happened', () => {
  assert.equal(panel.describeAct({ executed: true, action: 'retry_task', result: { status: 'queued' } }),
    'Executed: retry_task · queued');
  assert.equal(panel.describeAct({ executed: false, action: 'inspect', reference: 'task:t1' }),
    'Nothing was executed · reference: task:t1');
  assert.equal(panel.describeDecision({ status: 'dismissed' }), 'Dismissed');
  assert.ok(panel.describeDecision({ status: 'snoozed', until: 1000000 }).startsWith('Snoozed · hidden until'));
});
