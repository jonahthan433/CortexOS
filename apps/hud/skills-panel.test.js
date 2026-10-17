/* Pure helper tests for the skill detail drawer. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./skills-panel.js');

test('riskText states the gate plainly', () => {
  assert.equal(panel.riskText({ risk_level: 'external_action', approval_required: true }),
    'external_action · approval required each run');
  assert.equal(panel.riskText({ risk_level: 'read_only', approval_required: false }),
    'read_only · no external gate');
});

test('promotionText reports real promotion state and progress', () => {
  assert.equal(panel.promotionText({ promotion_status: 'approved' }), 'automation approved');
  assert.equal(panel.promotionText({ promotion_status: 'pending_review', successful_runs: 6 }),
    'awaiting human promotion review · 6/5 confirmed runs');
  assert.equal(panel.promotionText({ successful_runs: 2 }),
    'automation review not started · 2/5 confirmed runs');
  assert.equal(panel.promotionText(null), '');
});

test('statsRows only includes measured values', () => {
  const rows = panel.statsRows({ successful_runs: 5, error_runs: 1, tier: 2 });
  assert.deepEqual(rows, [
    ['successful runs', '5'],
    ['runs needing improvement', '1'],
    ['tier', '2']
  ]);
  assert.deepEqual(panel.statsRows({}), []);
  assert.deepEqual(panel.statsRows(null), []);
});

test('detailSections builds the drawer table without inventing fields', () => {
  const sections = panel.detailSections({
    description: 'does things', risk_level: 'read_only', approval_required: false,
    triggers: ['a', 'b'], domain: 'ops', files: ['skill.json', 'SKILL.md']
  });
  assert.deepEqual(sections.map(pair => pair[0]), ['description', 'risk', 'automation', 'triggers', 'domain', 'files']);
  const bare = panel.detailSections({});
  assert.deepEqual(bare.map(pair => pair[0]), ['description', 'risk', 'automation']);
  assert.deepEqual(panel.detailSections(null), []);
});

test('versionLine states the pinned version, source, and hash', () => {
  assert.equal(panel.versionLine({ version: '1.2.0', version_source: 'manifest', content_hash: 'abcdef1234567890' }),
    'v1.2.0 · manifest · abcdef123456…');
  assert.equal(panel.versionLine({ version: '1.0.0' }), 'v1.0.0 · unknown source · no hash');
  assert.equal(panel.versionLine(null), '');
});

test('problemLines surfaces each validation problem verbatim', () => {
  assert.deepEqual(panel.problemLines(['bad tier', 'missing field']), ['bad tier', 'missing field']);
  assert.deepEqual(panel.problemLines(null), []);
  assert.deepEqual(panel.problemLines([]), []);
});

test('historyRows marks the current version and rollback eligibility', () => {
  const rows = panel.historyRows({ entries: [
    { hash: 'aaaaaaaa1111', version: '2.0.0', source: 'change', at: 5, current: true },
    { hash: 'bbbbbbbb2222', version: '1.0.0', source: 'baseline', at: 1 }
  ] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].hash8, 'aaaaaaaa');
  assert.equal(rows[0].current, true);
  assert.equal(rows[0].rollbackAllowed, false);
  assert.equal(rows[1].rollbackAllowed, true);
  assert.equal(rows[1].source, 'baseline');
  assert.deepEqual(panel.historyRows(null), []);
});

test('test result helpers report pass counts and mismatches', () => {
  const result = {
    passed: 1, total: 2, status: 'failed', receipt: 'x',
    cases: [
      { name: 'route', passed: true, mismatches: [] },
      { name: 'gate', passed: false, mismatches: ['expected tier 3, routed to tier 2'] }
    ]
  };
  assert.equal(panel.testSummary(result), '1/2 passed · failed · receipt recorded');
  const lines = panel.testCaseLines(result);
  assert.equal(lines[0], '✓ route');
  assert.ok(lines[1].startsWith('✗ gate — expected tier 3'));
  assert.equal(panel.testSummary(null), '');
  assert.deepEqual(panel.testCaseLines(null), []);
});
