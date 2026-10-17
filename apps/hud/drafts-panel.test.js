/* Pure helper tests for the drafts panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./drafts-panel.js');

test('kindLabel explains what saving each kind does', () => {
  assert.equal(panel.kindLabel('note'), 'Note → vault');
  assert.equal(panel.kindLabel('task'), 'Task → queue');
  assert.equal(panel.kindLabel('email'), 'Email');
  assert.equal(panel.kindLabel('connector'), 'Connector action');
});

test('draftMeta shows revision, source, and age', () => {
  const now = 1000000;
  assert.equal(
    panel.draftMeta({ kind: 'note', revision: 2, source: 'voice', updated_at: now - 30 }, now),
    'Note → vault · revision 2 · from voice · updated 30s ago');
  assert.equal(
    panel.draftMeta({ kind: 'task', updated_at: now - 30 }, now),
    'Task → queue · revision 1 · updated 30s ago');
});

test('executionText states availability before any save attempt', () => {
  assert.equal(panel.executionText({ available: true, reason: 'Saves to the vault.' }),
    'Can be saved: Saves to the vault.');
  assert.equal(panel.executionText({ available: false, reason: 'not wired' }),
    'Not wired yet: not wired');
  assert.equal(panel.executionText(null), '');
});

test('canSave respects execution availability and saved status', () => {
  assert.equal(panel.canSave({ status: 'draft', execution: { available: true } }), true);
  assert.equal(panel.canSave({ status: 'saved', execution: { available: true } }), false);
  assert.equal(panel.canSave({ status: 'draft', execution: { available: false } }), false);
  assert.equal(panel.canSave(null), false);
});

test('savedLine only shows a real reference', () => {
  assert.equal(panel.savedLine({ status: 'saved', saved_ref: '/vault/output/x.md' }),
    'Saved · /vault/output/x.md');
  assert.equal(panel.savedLine({ status: 'draft', saved_ref: null }), '');
  assert.equal(panel.savedLine(null), '');
});

test('parseTarget accepts JSON objects and rejects junk honestly', () => {
  assert.deepEqual(panel.parseTarget(''), { value: null });
  assert.deepEqual(panel.parseTarget('  '), { value: null });
  assert.deepEqual(panel.parseTarget('{"to": ["a@b.c"]}'), { value: { to: ['a@b.c'] } });
  assert.ok(panel.parseTarget('not json').error.includes('not valid JSON'));
  assert.ok(panel.parseTarget('[1]').error.includes('JSON object'));
});

test('describeSave reports the real outcome including duplicates', () => {
  assert.equal(panel.describeSave({ status: 'saved', reference: 'task-1' }), 'Saved · task-1');
  assert.equal(panel.describeSave({ status: 'saved', reference: 'task-1', created: false }),
    'A queue task for this draft already exists · task-1');
  assert.equal(panel.describeSave({ status: 'already_saved', reference: 'x' }), 'Already saved · x');
  assert.equal(panel.describeSave(null), 'The save returned no result.');
});

test('voiceDraftKind falls back to note', () => {
  assert.equal(panel.voiceDraftKind('task'), 'task');
  assert.equal(panel.voiceDraftKind('bogus'), 'note');
  assert.equal(panel.voiceDraftKind(null), 'note');
});
