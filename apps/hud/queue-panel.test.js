/* Node tests for the queue panel's pure logic: `node --test queue-panel.test.js` */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const qp = require('./queue-panel.js');

test('status labels and classes cover every backend status', () => {
  const cases = {
    queued: ['Queued', 'st-queued'],
    running: ['Running', 'st-running'],
    awaiting_approval: ['Awaiting approval', 'st-awaiting'],
    completed: ['Completed', 'st-completed'],
    failed: ['Failed', 'st-failed'],
    cancelled: ['Cancelled', 'st-cancelled']
  };
  for (const [status, [label, cls]] of Object.entries(cases)) {
    assert.equal(qp.statusLabel(status), label);
    assert.equal(qp.statusClass(status), cls);
  }
  assert.equal(qp.statusLabel('mystery'), 'Unknown');
  assert.equal(qp.statusClass('mystery'), 'st-unknown');
});

test('cancel/retry/follow availability matches the real lifecycle', () => {
  const mk = status => ({ status });
  assert.ok(qp.canCancel(mk('queued')) && qp.canCancel(mk('running')) && qp.canCancel(mk('awaiting_approval')));
  assert.ok(!qp.canCancel(mk('completed')) && !qp.canCancel(mk('failed')) && !qp.canCancel(mk('cancelled')));
  assert.ok(qp.canRetry(mk('failed')) && qp.canRetry(mk('cancelled')));
  assert.ok(!qp.canRetry(mk('queued')) && !qp.canRetry(mk('running')) && !qp.canRetry(mk('completed')));
  assert.ok(qp.canFollow(mk('queued')) && qp.canFollow(mk('running')));
  assert.ok(!qp.canFollow(mk('completed')));
});

test('filters apply status, priority and limit without inventing rows', () => {
  const tasks = [
    { id: 'a', status: 'queued', priority: 5 },
    { id: 'b', status: 'running', priority: 1 },
    { id: 'c', status: 'failed', priority: 5 },
    { id: 'd', status: 'queued', priority: 9 }
  ];
  assert.deepEqual(qp.applyFilters(tasks, { status: 'queued' }).map(t => t.id), ['a', 'd']);
  assert.deepEqual(qp.applyFilters(tasks, { priority: 5 }).map(t => t.id), ['a', 'c']);
  assert.deepEqual(qp.applyFilters(tasks, { status: 'queued', priority: 5 }).map(t => t.id), ['a']);
  assert.deepEqual(qp.applyFilters(tasks, { limit: 2 }).map(t => t.id), ['a', 'b']);
  assert.deepEqual(qp.applyFilters([], { status: 'all' }), []);
});

test('empty vs loading vs rows state selection', () => {
  assert.equal(qp.emptyStateFor(true, []), 'loading');
  assert.equal(qp.emptyStateFor(false, []), 'empty');
  assert.equal(qp.emptyStateFor(false, [{ id: 'x' }]), 'rows');
});

test('poll backoff grows and caps; idle polls slower than active', () => {
  assert.equal(qp.retryDelayFor(0), 5000);
  assert.equal(qp.retryDelayFor(1), 5000);
  assert.equal(qp.retryDelayFor(2), 10000);
  assert.equal(qp.retryDelayFor(3), 20000);
  assert.equal(qp.retryDelayFor(4), 30000);
  assert.equal(qp.retryDelayFor(9), 30000);
  const busy = { failures: 0, summary: { queued: 1, running: 0, awaiting_approval: 0 } };
  const idle = { failures: 0, summary: { queued: 0, running: 0, awaiting_approval: 0 } };
  assert.equal(qp.nextPollDelay(busy), 5000);
  assert.equal(qp.nextPollDelay(idle), 20000);
  assert.equal(qp.nextPollDelay({ failures: 3, summary: null }), 20000);
});

test('staleness labels distinguish live, idle and reconnecting data', () => {
  const now = 1_000_000_000_000;
  assert.equal(qp.staleness(now, 0, 0).stale, true);
  assert.equal(qp.staleness(now, 0, 0).label, 'no data yet');
  const fresh = qp.staleness(now, now - 4000, 0);
  assert.equal(fresh.stale, false);
  assert.equal(fresh.label, 'updated 4s ago');
  const aged = qp.staleness(now, now - 60_000, 0);
  assert.equal(aged.stale, true);
  const failing = qp.staleness(now, now - 9000, 2);
  assert.equal(failing.stale, true);
  assert.match(failing.label, /reconnecting/);
});

test('duplicate and rapid submissions are blocked by the guard', () => {
  const now = 2_000_000_000_000;
  assert.ok(qp.shouldBlockSubmit({ submitting: true, lastSubmitAt: 0 }, now));
  assert.ok(qp.shouldBlockSubmit({ submitting: false, lastSubmitAt: now - 500 }, now));
  assert.ok(!qp.shouldBlockSubmit({ submitting: false, lastSubmitAt: now - 4000 }, now));
  assert.ok(!qp.shouldBlockSubmit({ submitting: false, lastSubmitAt: 0 }, now));
});

test('attempt labels surface retry scheduling honestly', () => {
  assert.equal(qp.attemptLabel({ attempts: 2, max_attempts: 3 }), '2/3 attempts');
  assert.equal(
    qp.attemptLabel({ attempts: 1, max_attempts: 3, next_attempt_at: 1060 }, 1000),
    '1/3 · retry in 60s');
  assert.equal(
    qp.attemptLabel({ attempts: 1, max_attempts: 3, next_attempt_at: 900 }, 1000),
    '1/3 · retry in 0s');
  assert.equal(qp.attemptLabel({ attempts: 1, max_attempts: 3, cycle: 1 }), '1/3 attempts · cycle 2');
});

test('tier labels prefer real meta, fall back to backend, never invent', () => {
  assert.equal(qp.tierLabel({ meta: { tier: 1, backend: 'local' } }), 'T1 · local');
  assert.equal(qp.tierLabel({ meta: {}, backend: 'hermes' }), 'hermes');
  assert.equal(qp.tierLabel({ meta: {} }), '—');
});

test('timestamps render dashes for missing values and clock for real ones', () => {
  assert.equal(qp.formatTimestamp(null), '—');
  assert.equal(qp.formatTimestamp(undefined), '—');
  assert.equal(qp.formatTimestamp('nonsense'), '—');
  const value = qp.formatTimestamp(1_700_000_000, Date.now());
  assert.match(value, /\d{2}:\d{2}:\d{2}/);
});

test('task label truncation keeps payloads short', () => {
  const long = 'x'.repeat(300);
  const label = qp.taskLabel({ prompt: long });
  assert.equal(label.length, 118);
  assert.ok(label.endsWith('…'));
  assert.equal(qp.taskLabel({ prompt: 'short' }), 'short');
  assert.equal(qp.errorSummary({}), '');
  assert.equal(qp.errorSummary({ error: 'boom' }), 'boom');
});

test('active count only counts live lifecycle states', () => {
  assert.equal(qp.activeCount({ queued: 2, running: 1, awaiting_approval: 3, completed: 9 }), 6);
  assert.equal(qp.activeCount(null), 0);
});
