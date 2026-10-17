const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('./mission-panel.js');

test('ageLabel buckets seconds honestly', () => {
  assert.equal(api.ageLabel(0), 'now');
  assert.equal(api.ageLabel(44), 'now');
  assert.equal(api.ageLabel(45), '0m');
  assert.equal(api.ageLabel(90), '1m');
  assert.equal(api.ageLabel(3599), '59m');
  assert.equal(api.ageLabel(3600), '1h');
  assert.equal(api.ageLabel(90000), '1d');
  assert.equal(api.ageLabel(-5), 'now');
  assert.equal(api.ageLabel('bad'), 'now');
});

test('trim collapses whitespace and truncates with ellipsis', () => {
  assert.equal(api.trim('  a   b  ', 10), 'a b');
  assert.equal(api.trim('abcdefghij', 5), 'abcd…');
  assert.equal(api.trim('abcd efgh', 4), 'abc…');
  assert.equal(api.trim(null, 5), '');
});

test('stageOf maps every real queue status', () => {
  assert.equal(api.stageOf('queued'), 'capture');
  assert.equal(api.stageOf('awaiting_approval'), 'gate');
  assert.equal(api.stageOf('running'), 'execute');
  assert.equal(api.stageOf('completed'), 'shipped');
  assert.equal(api.stageOf('failed'), 'unshipped');
  assert.equal(api.stageOf('cancelled'), 'unshipped');
  assert.equal(api.stageOf('something-new'), 'other');
  assert.equal(api.stageOf(undefined), 'other');
});

test('taskAgent prefers the worker, then the backend, then auto routes', () => {
  assert.equal(api.taskAgent({ worker: 'codex', backend: 'hermes' }), 'codex');
  assert.equal(api.taskAgent({ worker: null, backend: 'Hermes' }), 'hermes');
  assert.equal(api.taskAgent({}), 'auto');
});

test('bucketTasks sorts each stage by its own clock', () => {
  const tasks = [
    { id: 'q-later', status: 'queued', priority: 7, created_at: 100 },
    { id: 'q-first', status: 'queued', priority: 1, created_at: 900 },
    { id: 'q-same-pri', status: 'queued', priority: 1, created_at: 500 },
    { id: 'run-old', status: 'running', started_at: 10 },
    { id: 'run-new', status: 'running', started_at: 40 },
    { id: 'done-old', status: 'completed', finished_at: 100 },
    { id: 'done-new', status: 'completed', finished_at: 300 },
    { id: 'fail-old', status: 'failed', finished_at: 50 },
    { id: 'cancel-new', status: 'cancelled', finished_at: 60 },
    { id: 'weird', status: 'mystery' }
  ];
  const buckets = api.bucketTasks(tasks);
  assert.deepEqual(buckets.capture.map(t => t.id), ['q-same-pri', 'q-first', 'q-later']);
  assert.deepEqual(buckets.execute.map(t => t.id), ['run-old', 'run-new']);
  assert.deepEqual(buckets.shipped.map(t => t.id), ['done-new', 'done-old']);
  assert.deepEqual(buckets.unshipped.map(t => t.id), ['cancel-new', 'fail-old']);
  assert.deepEqual(buckets.other.map(t => t.id), ['weird']);
  assert.deepEqual(buckets.gate, []);
});

test('agentActiveCount counts only active work for that agent', () => {
  const tasks = [
    { status: 'queued', worker: 'codex' },
    { status: 'running', worker: 'codex' },
    { status: 'awaiting_approval', backend: 'Codex' },
    { status: 'completed', worker: 'codex' },
    { status: 'failed', worker: 'codex' },
    { status: 'running', backend: 'hermes' },
    { status: 'queued' }
  ];
  assert.equal(api.agentActiveCount(tasks, { id: 'codex', name: 'Codex' }), 3);
  assert.equal(api.agentActiveCount(tasks, { id: 'hermes', name: 'Hermes' }), 1);
  assert.equal(api.agentActiveCount(tasks, { id: 'claude', name: 'Claude' }), 0);
  assert.equal(api.agentActiveCount(tasks, null), 0);
  // auto-routed tasks (no worker/backend) must not be attributed to any agent
  assert.equal(api.agentActiveCount([{ status: 'queued' }], { id: 'codex', name: 'Codex' }), 0);
});

test('agentState precedence: planned, issues, working, ready', () => {
  assert.deepEqual(api.agentState({ implemented: false, issues: ['x'] }, 5).text, 'Planned');
  assert.deepEqual(api.agentState({ implemented: true, issues: ['bad'] }, 5), { text: 'Needs attention', cls: 'badge-warn' });
  assert.deepEqual(api.agentState({ implemented: true, issues: [] }, 2), { text: 'Working', cls: 'badge-live' });
  assert.deepEqual(api.agentState({ implemented: true, issues: [] }, 0), { text: 'Ready', cls: 'badge-ok' });
  assert.deepEqual(api.agentState(null, 0).text, 'Planned');
});

test('taskMeta quotes real fields only', () => {
  const meta = api.taskMeta({ priority: 3, worker: 'codex', skill_id: 'research-brief', created_at: 1000 }, 1060);
  assert.equal(meta, 'P3 · codex · research-brief · 1m ago');
  assert.equal(api.taskMeta({ priority: 5 }, 1000), 'P5 · auto routing');
  assert.equal(api.taskMeta({}, 1000), 'P? · auto routing');
});
