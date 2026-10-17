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
