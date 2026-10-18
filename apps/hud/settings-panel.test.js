/* Pure helper tests for the settings panel. */
const test = require('node:test');
const assert = require('node:assert');
const panel = require('./settings-panel.js');

test('providerTone flags disabled, degraded, and keyless cloud providers', () => {
  assert.equal(panel.providerTone({ enabled: false }), 'badge-planned');
  assert.equal(panel.providerTone({ enabled: true, health: { status: 'error' } }), 'badge-warn');
  assert.equal(panel.providerTone({ enabled: true, has_key: false, local: false }), 'badge-warn');
  assert.equal(panel.providerTone({ enabled: true, has_key: false, local: true }), 'badge-ok');
  assert.equal(panel.providerTone({ enabled: true, has_key: true, local: false }), 'badge-ok');
});

test('providerSummary never mentions key values, only sources', () => {
  const text = panel.providerSummary({
    kind: 'openai_compatible', local: false, has_key: true, key_source: 'env:OPENROUTER_KEY',
    default_model: 'qwen'
  });
  assert.equal(text, 'openai_compatible · cloud · key: env:OPENROUTER_KEY · default: qwen');
  assert.ok(!text.includes('sk-'));
  assert.ok(panel.providerSummary({ kind: 'ollama', local: true, has_key: false }).includes('key: none'));
});

test('enabledCapabilities lists only capabilities that are on', () => {
  const caps = panel.enabledCapabilities({ capabilities: { chat: true, streaming: false, json_mode: true } });
  assert.deepEqual(caps.sort(), ['chat', 'json_mode']);
  assert.deepEqual(panel.enabledCapabilities({}), []);
});

test('modelsFor prefers the model list and falls back to the default', () => {
  assert.deepEqual(panel.modelsFor({ models: ['a', 'b'], default_model: 'a' }), ['a', 'b']);
  assert.deepEqual(panel.modelsFor({ models: [], default_model: 'a' }), ['a']);
  assert.deepEqual(panel.modelsFor({}), []);
});

test('buildAssignmentBody mirrors the validated API contract', () => {
  const body = panel.buildAssignmentBody({
    agent: 'gemini-flash', name: 'gemini-flash', provider: 'acme', model: 'm1',
    fallbackProvider: 'backup', fallbackModel: 'b1', allowCloudFallback: true
  });
  assert.deepEqual(body, {
    provider: 'acme', model: 'm1', name: 'gemini-flash',
    fallback: [{ provider: 'backup', model: 'b1' }],
    allow_cloud_fallback: true, requirements: ['chat']
  });
  const bare = panel.buildAssignmentBody({ agent: 'x', provider: 'acme' });
  assert.deepEqual(bare.fallback, []);
  assert.equal(bare.allow_cloud_fallback, false);
});

test('assignmentSummary is explicit about fallback policy', () => {
  const text = panel.assignmentSummary({
    provider: 'local-llm', model: 'm', fallback: [{ provider: 'openrouter' }], allow_cloud_fallback: false
  });
  assert.equal(text, 'local-llm · m · fallback: openrouter · no silent cloud fallback');
  assert.ok(panel.assignmentSummary({ provider: 'a', model: 'b', fallback: [], allow_cloud_fallback: true })
    .includes('cloud fallback allowed'));
});

test('inference tests require confirmation; validate does not', () => {
  assert.equal(panel.testModeRequiresConfirmation('inference'), true);
  assert.equal(panel.testModeRequiresConfirmation('connectivity'), false);
  assert.equal(panel.testModeRequiresConfirmation('validate'), false);
});

test('healthText is never dressed up', () => {
  assert.equal(panel.healthText({ health: {} }), 'not tested yet');
  assert.equal(panel.healthText(null), 'not tested yet');
  const text = panel.healthText({ health: { status: 'error', detail: 'HTTP 402', checked_at: 1700000000 } }, 1700000060000);
  assert.ok(text.startsWith('error: HTTP 402'));
  assert.ok(text.includes('1m ago'));
});

test('slugifyBot produces safe agent ids', () => {
  assert.equal(panel.slugifyBot('Researcher'), 'researcher');
  assert.equal(panel.slugifyBot('  Market  Scout! '), 'market-scout');
  assert.equal(panel.slugifyBot('Sales/Bot (v2)'), 'sales-bot-v2');
  assert.equal(panel.slugifyBot('--edge--'), 'edge');
  assert.equal(panel.slugifyBot('!!!'), '');
  assert.equal(panel.slugifyBot('a'.repeat(80)).length, 48);
  assert.equal(panel.slugifyBot(''), '');
});

test('buildCreateBotBody mirrors the create endpoint contract', () => {
  const body = panel.buildCreateBotBody({
    name: '  Researcher ', agentId: 'Researcher', provider: 'openrouter',
    model: ' nvidia/nemotron-3-super-120b-a12b:free ', persona: '  You are meticulous. '
  });
  assert.deepEqual(body, {
    agent_id: 'researcher', name: 'Researcher', provider: 'openrouter',
    model: 'nvidia/nemotron-3-super-120b-a12b:free', persona: 'You are meticulous.',
    fallback: [], allow_cloud_fallback: false, requirements: ['chat']
  });
  const derived = panel.buildCreateBotBody({ name: 'Market Scout', provider: 'openrouter' });
  assert.equal(derived.agent_id, 'market-scout');
  assert.equal(derived.persona, '');
  assert.equal(derived.model, '');
});
