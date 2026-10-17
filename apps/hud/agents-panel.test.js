/* Pure helper tests for the agents directory panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./agents-panel.js');

test('agentBadge distinguishes ready, attention, and planned', () => {
  assert.equal(panel.agentBadge({ implemented: true, issues: [] }).text, 'Ready');
  assert.equal(panel.agentBadge({ implemented: true, issues: ['unknown provider'] }).text, 'Needs attention');
  assert.equal(panel.agentBadge({ implemented: false }).text, 'Planned');
  assert.equal(panel.agentBadge({ implemented: true }).cls, 'badge-ok');
});

test('kindLabel names the three real kinds', () => {
  assert.equal(panel.kindLabel('cli'), 'CLI adapter');
  assert.equal(panel.kindLabel('llm'), 'model gateway');
  assert.equal(panel.kindLabel('role'), 'role agent');
  assert.equal(panel.kindLabel('other'), 'other');
});

test('capabilityChips caps the list for layout', () => {
  const entry = { capabilities: ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'] };
  assert.equal(panel.capabilityChips(entry).length, 8);
  assert.deepEqual(panel.capabilityChips({}), []);
});

test('assignmentText and laneText describe real state only', () => {
  assert.equal(panel.assignmentText({ assignment: { provider: 'acme', model: 'm1' } }), 'acme · m1');
  assert.equal(panel.assignmentText({ assignment: { provider: 'acme', model: '' } }), 'acme · default model');
  assert.equal(panel.assignmentText({}), '');
  assert.equal(panel.laneText({ kind: 'role', role: 'ops', lane: 'local', autonomous: true }),
    'role agent · ops · local · autonomous');
  assert.ok(panel.laneText({ kind: 'cli' }).includes('CLI adapter'));
});

test('filterEntries slices by kind and keeps all for all', () => {
  const entries = [{ kind: 'cli' }, { kind: 'llm' }, { kind: 'role' }];
  assert.equal(panel.filterEntries(entries, 'all').length, 3);
  assert.deepEqual(panel.filterEntries(entries, 'llm'), [{ kind: 'llm' }]);
  assert.deepEqual(panel.filterEntries(entries, 'none'), []);
});
