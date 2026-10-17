/* Pure helper tests for the activity search panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./activity-panel.js');

test('kindLabel covers the three real sources', () => {
  assert.equal(panel.kindLabel('task'), 'task');
  assert.equal(panel.kindLabel('event'), 'event');
  assert.equal(panel.kindLabel('request'), 'request');
  assert.equal(panel.kindLabel(undefined), 'unknown');
});

test('relativeTime is honest about ages', () => {
  const now = 1_700_000_000_000;
  assert.equal(panel.relativeTime(now / 1000 - 5, now), '5s ago');
  assert.equal(panel.relativeTime(now / 1000 - 90, now), '2m ago');
  assert.equal(panel.relativeTime(now / 1000 - 7200, now), '2h ago');
  assert.equal(panel.relativeTime(now / 1000 - 200000, now), '2d ago');
  assert.equal(panel.relativeTime(null, now), '—');
});

test('dedupeAppend keeps first occurrence across cursor overlap', () => {
  const page1 = [{ kind: 'task', id: 't1' }, { kind: 'event', id: 'e1' }];
  const page2 = [{ kind: 'event', id: 'e1' }, { kind: 'task', id: 't2' }];
  assert.deepEqual(panel.dedupeAppend(page1, page2).map(i => i.id), ['t1', 'e1', 't2']);
});

test('statusTone maps awaiting_approval to the queue class', () => {
  assert.equal(panel.statusTone('awaiting_approval'), 'st-awaiting');
  assert.equal(panel.statusTone('completed'), 'st-completed');
  assert.equal(panel.statusTone(''), 'st-unknown');
});

test('describeItem joins only real fields', () => {
  assert.equal(panel.describeItem({ priority: 3, backend: 'hermes', skill: 's1', detail: 'x' }), 'P3 · hermes · s1 · x');
  assert.equal(panel.describeItem({}), '');
  assert.equal(panel.describeItem({ backend: 'codex' }), 'codex');
});

test('summarizeSources reports per-source counts', () => {
  assert.equal(panel.summarizeSources({ task: 2, event: 1, request: 0 }), 'tasks 2 · events 1 · requests 0');
  assert.equal(panel.summarizeSources(null), '');
});
