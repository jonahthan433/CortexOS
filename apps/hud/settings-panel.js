/* CortexOS · Settings panel for the JARVIS HUD.
 *
 * Two real jobs: inspect the LLM provider registry (masked, with honest
 * validate/connectivity/inference tests) and manage agent→provider
 * assignments through the validated /api/llm endpoints. It never sees or
 * shows credential values, and it cannot bypass server-side validation:
 * errors from the API are displayed verbatim. Pure helpers are unit-tested
 * with `node --test`.
 */
(function (global) {
  'use strict';

  var TEST_MODES = [
    { mode: 'validate', label: 'Validate' },
    { mode: 'connectivity', label: 'Connectivity' },
    { mode: 'inference', label: 'Inference (real call)' }
  ];

  function providerTone(provider) {
    if (!provider) return 'badge-planned';
    if (!provider.enabled) return 'badge-planned';
    var health = provider.health || {};
    if (health.status === 'error') return 'badge-warn';
    if (!provider.has_key && !provider.local) return 'badge-warn';
    return 'badge-ok';
  }

  function providerSummary(provider) {
    if (!provider) return '';
    var parts = [provider.kind, provider.local ? 'local' : 'cloud'];
    parts.push('key: ' + (provider.has_key ? (provider.key_source || 'configured') : 'none'));
    if (provider.default_model) parts.push('default: ' + provider.default_model);
    return parts.join(' · ');
  }

  function enabledCapabilities(provider) {
    if (!provider || !provider.capabilities) return [];
    return Object.keys(provider.capabilities).filter(function (name) { return provider.capabilities[name]; });
  }

  function modelsFor(provider) {
    if (!provider) return [];
    if (provider.models && provider.models.length) return provider.models.slice();
    return provider.default_model ? [provider.default_model] : [];
  }

  function testModeRequiresConfirmation(mode) { return mode === 'inference'; }

  function healthText(provider, nowMs) {
    var health = provider && provider.health;
    if (!health || !health.status || health.status === 'unknown') return 'not tested yet';
    var age = health.checked_at ? Math.max(0, Math.round(((nowMs || Date.now()) / 1000) - health.checked_at)) : null;
    var when = age === null ? '' : (age < 60 ? ' · ' + age + 's ago' : ' · ' + Math.round(age / 60) + 'm ago');
    var text = health.status + (health.mode ? ' (' + health.mode + ')' : '');
    if (health.detail) text += ': ' + health.detail;
    return text + when;
  }

  function buildAssignmentBody(draft) {
    var body = {
      provider: draft.provider,
      model: draft.model || '',
      name: draft.name || draft.agent || '',
      fallback: [],
      allow_cloud_fallback: !!draft.allowCloudFallback,
      requirements: ['chat']
    };
    if (draft.fallbackProvider) {
      body.fallback.push({ provider: draft.fallbackProvider, model: draft.fallbackModel || '' });
    }
    return body;
  }

  function assignmentSummary(entry) {
    if (!entry) return '';
    var parts = [entry.provider + ' · ' + (entry.model || 'default model')];
    if (entry.fallback && entry.fallback.length) {
      parts.push('fallback: ' + entry.fallback.map(function (item) { return item.provider; }).join(' → '));
    }
    parts.push(entry.allow_cloud_fallback ? 'cloud fallback allowed' : 'no silent cloud fallback');
    return parts.join(' · ');
  }

  var api = { TEST_MODES: TEST_MODES, providerTone: providerTone, providerSummary: providerSummary,
              enabledCapabilities: enabledCapabilities, modelsFor: modelsFor,
              testModeRequiresConfirmation: testModeRequiresConfirmation, healthText: healthText,
              buildAssignmentBody: buildAssignmentBody, assignmentSummary: assignmentSummary };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var state = { providers: [], assignments: [], agentIds: [] };

    function setBanner(text, kind) {
      var banner = $('settings-banner');
      if (!banner) return;
      banner.hidden = false;
      banner.className = 'queue-banner ' + (kind || 'error');
      banner.textContent = text;
      clearTimeout(setBanner.timer);
      setBanner.timer = setTimeout(function () { banner.hidden = true; }, 12000);
    }

    function message(text) {
      var p = document.createElement('p');
      p.className = 'dim';
      p.textContent = text;
      return p;
    }

    function call(method, path, body) {
      var init = { method: method };
      if (body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(body);
      }
      return apiFetch(apiBase + path, init).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
      });
    }

    function updated() {
      var el = $('settings-updated');
      if (el) el.textContent = 'updated ' + new Date().toLocaleTimeString();
    }

    /* ------------------------------------------------------------ providers */

    function renderProviders() {
      var root = $('settings-providers');
      if (!root) return;
      root.replaceChildren();
      if (!state.providers.length) {
        root.append(message('No providers configured. Add [llm.providers.<id>] in config/cortexos.toml and restart the Bridge.'));
        return;
      }
      state.providers.forEach(function (provider) {
        var card = document.createElement('article');
        card.className = 'card provider-card';
        var head = document.createElement('div');
        head.className = 'agent-head';
        var name = document.createElement('strong');
        name.textContent = provider.id;
        name.title = provider.base_url;
        var badge = document.createElement('span');
        badge.className = 'chip ' + providerTone(provider);
        badge.textContent = provider.enabled ? (provider.local ? 'local' : 'cloud') : 'disabled';
        head.append(name, badge);
        if (provider.builtin) {
          var builtin = document.createElement('small');
          builtin.className = 'dim';
          builtin.textContent = 'builtin';
          head.append(builtin);
        }
        card.append(head);

        var summary = document.createElement('small');
        summary.className = 'agent-lane';
        summary.textContent = providerSummary(provider);
        card.append(summary);

        var caps = enabledCapabilities(provider);
        if (caps.length) {
          var chipRow = document.createElement('div');
          chipRow.className = 'chip-row';
          caps.forEach(function (capability) {
            var chip = document.createElement('span');
            chip.className = 'chip cap';
            chip.textContent = capability;
            chipRow.append(chip);
          });
          card.append(chipRow);
        }

        var health = document.createElement('small');
        health.className = 'provider-health';
        health.textContent = 'health: ' + healthText(provider, Date.now());
        card.append(health);

        var controls = document.createElement('div');
        controls.className = 'provider-controls';
        TEST_MODES.forEach(function (mode) {
          var button = document.createElement('button');
          button.type = 'button';
          button.className = 'ghost';
          button.textContent = mode.label;
          button.onclick = function () { runTest(provider.id, mode.mode, button); };
          controls.append(button);
        });
        card.append(controls);

        var result = document.createElement('small');
        result.className = 'provider-result';
        result.id = 'provider-result-' + provider.id;
        result.textContent = '';
        card.append(result);

        root.append(card);
      });
    }

    function runTest(providerId, mode, button) {
      if (testModeRequiresConfirmation(mode)
          && !global.confirm('This makes a real inference call to provider “' + providerId + '”. It may consume provider credits or quota. Continue?')) {
        return;
      }
      var result = $('provider-result-' + providerId);
      button.disabled = true;
      if (result) result.textContent = mode + ': running…';
      call('POST', '/llm/providers/' + encodeURIComponent(providerId) + '/test', { mode: mode }).then(function (outcome) {
        if (!outcome.ok) {
          if (result) result.textContent = mode + ': request failed (HTTP ' + outcome.status + ') ' + (outcome.data.detail || '');
          return;
        }
        var data = outcome.data;
        var text = data.status + (data.detail ? ': ' + data.detail : '');
        if (data.elapsed_ms) text += ' (' + data.elapsed_ms + ' ms)';
        if (data.models && data.models.length) text += ' · ' + data.models.length + ' models';
        if (result) result.textContent = mode + ': ' + text;
        if (data.status !== 'ok') setBanner('Provider test for “' + providerId + '” (' + mode + '): ' + text, data.status === 'error' ? 'error' : 'warn');
      }).catch(function (error) {
        if (result) result.textContent = mode + ': failed to reach the Bridge (' + error.message + ')';
      }).finally(function () {
        button.disabled = false;
        refreshProviders();
      });
    }

    /* ---------------------------------------------------------- assignments */

    function renderAssignments() {
      var root = $('settings-assign-list');
      if (!root) return;
      root.replaceChildren();
      if (!state.assignments.length) {
        root.append(message('No assignments yet. Agents without an assignment keep their existing CLI execution path.'));
        return;
      }
      state.assignments.forEach(function (entry) {
        var row = document.createElement('article');
        row.className = 'card assign-card';
        var head = document.createElement('div');
        head.className = 'agent-head';
        var name = document.createElement('strong');
        name.textContent = entry.name || entry.agent_id;
        var id = document.createElement('small');
        id.className = 'dim';
        id.textContent = entry.agent_id;
        head.append(name, id);
        if (entry.issues && entry.issues.length) {
          var badge = document.createElement('span');
          badge.className = 'chip badge-warn';
          badge.textContent = 'invalid';
          head.append(badge);
        }
        row.append(head);

        var summary = document.createElement('small');
        summary.className = 'agent-lane';
        summary.textContent = assignmentSummary(entry);
        row.append(summary);

        if (entry.issues && entry.issues.length) {
          var issues = document.createElement('ul');
          issues.className = 'agent-issues';
          entry.issues.forEach(function (issue) {
            var item = document.createElement('li');
            item.textContent = issue;
            issues.append(item);
          });
          row.append(issues);
        }

        var controls = document.createElement('div');
        controls.className = 'provider-controls';
        var edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'ghost';
        edit.textContent = 'Edit';
        edit.onclick = function () { fillForm(entry); };
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'ghost';
        remove.textContent = 'Remove';
        remove.onclick = function () {
          if (!global.confirm('Remove the provider assignment for “' + entry.agent_id + '”? The agent returns to its previous execution path.')) return;
          call('DELETE', '/llm/agents/' + encodeURIComponent(entry.agent_id)).then(function (outcome) {
            setBanner(outcome.ok ? 'Assignment removed for ' + entry.agent_id + '.'
                                 : 'Could not remove assignment: ' + (outcome.data.detail || outcome.status),
                      outcome.ok ? 'ok' : 'error');
            refreshAssignments();
          });
        };
        controls.append(edit, remove);
        row.append(controls);
        root.append(row);
      });
    }

    function fillForm(entry) {
      var agent = $('assign-agent');
      var provider = $('assign-provider');
      var model = $('assign-model');
      var fallbackProvider = $('assign-fallback-provider');
      var fallbackModel = $('assign-fallback-model');
      var cloud = $('assign-cloud-fallback');
      if (agent) agent.value = entry.agent_id;
      if (provider) provider.value = entry.provider;
      if (model) model.value = entry.model || '';
      var fallback = (entry.fallback && entry.fallback[0]) || null;
      if (fallbackProvider) fallbackProvider.value = fallback ? fallback.provider : '';
      if (fallbackModel) fallbackModel.value = fallback ? (fallback.model || '') : '';
      if (cloud) cloud.checked = !!entry.allow_cloud_fallback;
      syncModelOptions();
    }

    function syncModelOptions() {
      var providerSelect = $('assign-provider');
      var provider = providerSelect && providerSelect.value
        ? state.providers.filter(function (item) { return item.id === providerSelect.value; })[0]
        : null;
      var datalist = $('assign-model-options');
      if (datalist) {
        datalist.replaceChildren();
        modelsFor(provider).forEach(function (model) {
          var option = document.createElement('option');
          option.value = model;
          datalist.append(option);
        });
      }
    }

    function submitAssignment(event) {
      if (event) event.preventDefault();
      var agent = ($('assign-agent') || {}).value || '';
      var provider = ($('assign-provider') || {}).value || '';
      if (!(agent.trim())) { setBanner('Enter an agent id (for example hermes, or a new id like gemini-flash).', 'warn'); return; }
      if (!provider) { setBanner('Choose a provider.', 'warn'); return; }
      var body = buildAssignmentBody({
        agent: agent.trim(), name: agent.trim(), provider: provider,
        model: (($('assign-model') || {}).value || '').trim(),
        fallbackProvider: ($('assign-fallback-provider') || {}).value || '',
        fallbackModel: (($('assign-fallback-model') || {}).value || '').trim(),
        allowCloudFallback: !!($('assign-cloud-fallback') || {}).checked
      });
      var save = $('assign-save');
      if (save) save.disabled = true;
      call('PUT', '/llm/agents/' + encodeURIComponent(agent.trim()), body).then(function (outcome) {
        if (!outcome.ok) {
          setBanner('Assignment rejected: ' + (outcome.data.detail || ('HTTP ' + outcome.status)), 'error');
          var hint = $('assign-hint');
          if (hint) hint.textContent = 'Rejected: ' + (outcome.data.detail || ('HTTP ' + outcome.status));
          return;
        }
        setBanner('Assignment saved for ' + agent.trim() + ' → ' + provider + '.', 'ok');
        var hint = $('assign-hint');
        if (hint) hint.textContent = 'Saved. New tasks for this agent run through the model gateway.';
        refreshAssignments();
        refreshAgents();
      }).catch(function (error) {
        setBanner('Assignment failed: ' + error.message, 'error');
      }).finally(function () {
        if (save) save.disabled = false;
      });
    }

    /* --------------------------------------------------------------- system */

    function renderSystem(status, health) {
      var root = $('settings-system');
      if (!root) return;
      root.replaceChildren();
      var rows = [];
      if (status) {
        rows.push(['bridge', status.name + ' · ' + status.state]);
        rows.push(['default agent', status.backend]);
        rows.push(['vault', status.vault]);
        rows.push(['skills', String(status.skills)]);
        if (status.llm) rows.push(['provider registry', status.llm.providers + ' providers · ' + (status.llm.assigned_agents || 0) + ' assigned agents']);
      }
      if (health && health.checks) {
        Object.keys(health.checks).forEach(function (name) {
          var check = health.checks[name] || {};
          var detail = check.status;
          if (name === 'queue' && check.workers_alive !== undefined) detail += ' · ' + check.workers_alive + '/' + check.workers_total + ' workers';
          if (name === 'llm' && check.providers !== undefined) detail += ' · ' + check.providers + ' providers, ' + (check.assigned_agents || 0) + ' assigned';
          rows.push([name, detail]);
        });
      }
      if (!rows.length) {
        root.append(message('System state unavailable.'));
        return;
      }
      rows.forEach(function (pair) {
        var row = document.createElement('div');
        row.className = 'system-row';
        var key = document.createElement('span');
        key.className = 'system-key';
        key.textContent = pair[0];
        var value = document.createElement('span');
        value.className = 'system-value';
        value.textContent = pair[1];
        row.append(key, value);
        root.append(row);
      });
    }

    /* -------------------------------------------------------------- refresh */

    function refreshProviders() {
      return call('GET', '/llm/providers').then(function (outcome) {
        if (!outcome.ok) throw new Error(outcome.data.detail || ('HTTP ' + outcome.status));
        state.providers = outcome.data.providers || [];
        var select = $('assign-provider');
        if (select) {
          var current = select.value;
          select.replaceChildren();
          var empty = document.createElement('option');
          empty.value = '';
          empty.textContent = 'choose provider…';
          select.append(empty);
          state.providers.forEach(function (provider) {
            var option = document.createElement('option');
            option.value = provider.id;
            option.textContent = provider.id + (provider.enabled ? '' : ' (disabled)');
            select.append(option);
          });
          select.value = state.providers.some(function (p) { return p.id === current; }) ? current : '';
        }
        var fallback = $('assign-fallback-provider');
        if (fallback) {
          var previous = fallback.value;
          fallback.replaceChildren();
          var none = document.createElement('option');
          none.value = '';
          none.textContent = 'none';
          fallback.append(none);
          state.providers.forEach(function (provider) {
            var option = document.createElement('option');
            option.value = provider.id;
            option.textContent = provider.id;
            fallback.append(option);
          });
          fallback.value = state.providers.some(function (p) { return p.id === previous; }) ? previous : '';
        }
        renderProviders();
      });
    }

    function refreshAssignments() {
      return call('GET', '/llm/agents').then(function (outcome) {
        if (!outcome.ok) throw new Error(outcome.data.detail || ('HTTP ' + outcome.status));
        state.assignments = outcome.data.agents || [];
        renderAssignments();
      });
    }

    function refreshAgents() {
      return call('GET', '/agents').then(function (outcome) {
        if (!outcome.ok) throw new Error(outcome.data.detail || ('HTTP ' + outcome.status));
        var datalist = $('assign-agent-options');
        if (!datalist) return;
        datalist.replaceChildren();
        (outcome.data.agents || []).forEach(function (agent) {
          var option = document.createElement('option');
          option.value = agent.id;
          option.label = agent.name || agent.id;
          datalist.append(option);
        });
      });
    }

    function refreshSystem() {
      return Promise.all([call('GET', '/status'), call('GET', '/health')]).then(function (results) {
        renderSystem(results[0].ok ? results[0].data : null, results[1].ok ? results[1].data : null);
      });
    }

    function refreshAll() {
      refreshProviders().then(function () { return refreshAssignments(); })
        .then(function () { return refreshAgents(); })
        .then(function () { return refreshSystem(); })
        .then(updated)
        .catch(function (error) { setBanner('Settings could not load: ' + error.message, 'error'); });
    }

    var refresh = $('settings-refresh');
    if (refresh) refresh.onclick = refreshAll;
    var form = $('settings-assign-form');
    if (form) form.addEventListener('submit', submitAssignment);
    var clear = $('assign-clear');
    if (clear) clear.onclick = function () {
      if (form) form.reset();
      syncModelOptions();
    };
    var providerSelect = $('assign-provider');
    if (providerSelect) providerSelect.onchange = syncModelOptions;

    refreshAll();
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexSettingsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
