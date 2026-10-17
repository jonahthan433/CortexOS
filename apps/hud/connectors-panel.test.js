/* Pure helper tests for the connectors panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./connectors-panel.js');

test('credentialBadge never hides a missing credential state', () => {
  assert.deepEqual(panel.credentialBadge({ status: 'ready' }), { label: 'authorized', cls: 'badge-ok' });
  assert.deepEqual(panel.credentialBadge({ status: 'not_configured' }), { label: 'setup needed', cls: 'badge-warn' });
  assert.deepEqual(panel.credentialBadge({ status: 'blocked_missing_credentials' }),
    { label: 'not authorized', cls: 'badge-warn' });
  assert.deepEqual(panel.credentialBadge({ status: 'disabled' }), { label: 'disabled', cls: 'badge-planned' });
  assert.deepEqual(panel.credentialBadge(null), { label: 'setup needed', cls: 'badge-warn' });
  assert.deepEqual(panel.credentialBadge({ status: 'weird' }), { label: 'weird', cls: 'badge-warn' });
});

test('healthLabel maps server states to plain words', () => {
  assert.equal(panel.healthLabel({ status: 'ok' }), 'ok');
  assert.equal(panel.healthLabel({ status: 'blocked_missing_credentials' }), 'not authorized');
  assert.equal(panel.healthLabel(null), 'not checked');
  assert.equal(panel.healthLabel({ status: 'custom_state' }), 'custom_state');
});

test('sideEffectText makes the gate explicit', () => {
  assert.equal(panel.sideEffectText({ side_effect: 'read' }), 'read-only · runs directly');
  assert.equal(panel.sideEffectText({ side_effect: 'external' }), 'external effect · approval required each run');
  assert.equal(panel.sideEffectText({ side_effect: 'write' }), 'writes data · approval required each run');
});

test('scopeShort trims scope URLs for display only', () => {
  assert.equal(panel.scopeShort('https://www.googleapis.com/auth/gmail.send'), 'gmail.send');
  assert.equal(panel.scopeShort('gmail.readonly'), 'gmail.readonly');
  assert.equal(panel.scopeShort(''), '');
});

test('operationMeta shows limits, retry, and idempotency', () => {
  const read = panel.operationMeta({
    scopes: ['https://www.googleapis.com/auth/gmail.readonly'], rate_limit_per_minute: 60,
    timeout_seconds: 20, retry_safe: true, idempotency: 'none'
  });
  assert.equal(read, 'scopes: gmail.readonly · ≤60/min · timeout 20s · safe to retry');
  const send = panel.operationMeta({
    scopes: ['https://www.googleapis.com/auth/gmail.send'], rate_limit_per_minute: 20,
    timeout_seconds: 30, retry_safe: false, idempotency: 'content_hash'
  });
  assert.equal(send, 'scopes: gmail.send · ≤20/min · timeout 30s · no auto-retry · idempotent: content_hash');
});

test('missingScopes compares granted scopes against the operation', () => {
  const credential = { scopes: ['https://www.googleapis.com/auth/drive.file'] };
  const operation = { scopes: ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.readonly'] };
  assert.deepEqual(panel.missingScopes(credential, operation),
    ['https://www.googleapis.com/auth/drive.readonly']);
  assert.deepEqual(panel.missingScopes(credential, { scopes: [] }), []);
  assert.deepEqual(panel.missingScopes(null, operation),
    ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.readonly']);
});

test('fieldsText lists argument names with required markers', () => {
  assert.equal(panel.fieldsText({ fields: { to: { required: true }, subject: { required: true }, note: {} } }),
    'args: to*, subject*, note');
  assert.equal(panel.fieldsText({ fields: {} }), '');
});

test('authorizeEnabled requires a client file and enabled connectors', () => {
  assert.equal(panel.authorizeEnabled({ credential: { status: 'ready' } }), true);
  assert.equal(panel.authorizeEnabled({ credential: { status: 'blocked_missing_credentials' } }), true);
  assert.equal(panel.authorizeEnabled({ credential: { status: 'not_configured' } }), false);
  assert.equal(panel.authorizeEnabled({ credential: { status: 'disabled' } }), false);
});

test('summaryLine counts authorized connectors truthfully', () => {
  const status = {
    enabled: true,
    connectors: [{ credential: { status: 'ready' } }, { credential: { status: 'not_configured' } }]
  };
  assert.equal(panel.summaryLine(status), '2 connectors · 1 authorized');
  assert.equal(panel.summaryLine({ ...status, enabled: false }),
    '2 connectors · 1 authorized · connectors disabled in configuration');
  assert.equal(panel.summaryLine(null), '');
});
