/* Pure helper tests for the dashboard login panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./auth-panel.js');

test('modeFor picks setup until a password exists', () => {
  assert.equal(panel.modeFor({ has_password: false }), 'setup');
  assert.equal(panel.modeFor({ has_password: true }), 'login');
  assert.equal(panel.modeFor(null), 'setup');
});

test('validate enforces length and setup confirmation', () => {
  assert.equal(panel.validate('short', 'short', 'setup'), 'Password must be at least 8 characters.');
  assert.equal(panel.validate('longenough', 'mismatch1', 'setup'), 'Passwords do not match.');
  assert.equal(panel.validate('longenough', 'longenough', 'setup'), null);
  assert.equal(panel.validate('longenough', '', 'login'), null);
  assert.equal(panel.validate('', '', 'login'), 'Enter a password.');
});

test('modeFor never asks login before a password is set', () => {
  const state = { has_password: false };
  const mode = panel.modeFor(state);
  assert.equal(panel.validate('', '', mode), 'Enter a password.');
  assert.equal(panel.validate('abcdefgh', 'different9', mode), 'Passwords do not match.');
  assert.equal(panel.validate('abcdefgh', 'abcdefgh', mode), null);
});

test('errorText maps server responses to plain guidance', () => {
  assert.equal(panel.errorText(401, 'Wrong password'), 'Wrong password.');
  assert.equal(panel.errorText(400, 'Password must be at least 8 characters long'),
    'Password must be at least 8 characters long');
  assert.equal(panel.errorText(400, undefined), 'Password must be at least 8 characters.');
  assert.equal(panel.errorText(409, undefined), 'A password is already set — reload the page to sign in.');
  assert.match(panel.errorText(429, undefined), /Too many failed attempts/);
  assert.equal(panel.errorText(500, 'Internal error'), 'Internal error');
  assert.equal(panel.errorText(503, undefined), 'Sign-in failed.');
});
