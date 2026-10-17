/* Pure helper tests for the vault browser panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./vault-panel.js');

test('formatBytes renders honest sizes', () => {
  assert.equal(panel.formatBytes(0), '0 B');
  assert.equal(panel.formatBytes(512), '512 B');
  assert.equal(panel.formatBytes(2048), '2.0 KB');
  assert.equal(panel.formatBytes(5 * 1024 * 1024), '5.0 MB');
  assert.equal(panel.formatBytes(undefined), '—');
  assert.equal(panel.formatBytes('nope'), '—');
});

test('joinPath and parentPath stay relative', () => {
  assert.equal(panel.joinPath('', 'raw'), 'raw');
  assert.equal(panel.joinPath('.', 'raw'), 'raw');
  assert.equal(panel.joinPath('wiki', 'note.md'), 'wiki/note.md');
  assert.equal(panel.joinPath('receipts/2026/', '10'), 'receipts/2026/10');
  assert.equal(panel.parentPath('wiki/note.md'), 'wiki');
  assert.equal(panel.parentPath('raw'), '');
  assert.equal(panel.parentPath(''), '');
});

test('breadcrumbs build the vault trail', () => {
  assert.deepEqual(panel.breadcrumbs(''), [{ label: 'vault', path: '' }]);
  assert.deepEqual(panel.breadcrumbs('wiki/notes'), [
    { label: 'vault', path: '' },
    { label: 'wiki', path: 'wiki' },
    { label: 'notes', path: 'wiki/notes' }
  ]);
});

test('sortEntries puts folders first, case-insensitively', () => {
  const sorted = panel.sortEntries([
    { name: 'b.md', type: 'file' },
    { name: 'Wine', type: 'dir' },
    { name: 'a.md', type: 'file' },
    { name: 'Alpha', type: 'dir' }
  ]);
  assert.deepEqual(sorted.map(e => e.name), ['Alpha', 'Wine', 'a.md', 'b.md']);
});

test('previewMode and fileMeta never invent content', () => {
  assert.equal(panel.previewMode(null), 'none');
  assert.equal(panel.previewMode({ binary: true }), 'binary');
  assert.equal(panel.previewMode({ binary: false }), 'text');
  assert.equal(panel.fileMeta(null), '');
  const meta = panel.fileMeta({ size: 100, modified: 0, truncated: true, binary: false });
  assert.ok(meta.includes('100 B'));
  assert.ok(meta.includes('truncated'));
  assert.ok(panel.fileMeta({ size: 1, modified: 0, binary: true }).includes('binary'));
});
