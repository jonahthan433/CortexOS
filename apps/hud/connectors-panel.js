/* CortexOS · Connectors panel for the JARVIS HUD.
 *
 * Shows exactly what each connector can do (operations, side-effect class,
 * scopes, approval requirement, rate limits, timeouts) and its real credential
 * state — `not_configured` and `blocked_missing_credentials` are displayed as
 * such, never papered over. "Authorize" only fetches the OAuth URL for the user
 * to open deliberately; nothing here ever starts authorization on its own or
 * displays tokens (the API never returns them). Pure helpers are tested with
 * `node --test`.
 */
(function (global) {
  'use strict';

  var CREDENTIAL_BADGES = {
    ready: { label: 'authorized', cls: 'badge-ok' },
    not_configured: { label: 'setup needed', cls: 'badge-warn' },
    blocked_missing_credentials: { label: 'not authorized', cls: 'badge-warn' },
    disabled: { label: 'disabled', cls: 'badge-planned' }
  };

  var HEALTH_LABELS = {
    unchecked: 'not checked',
    ok: 'ok',
    error: 'error',
    expired: 'token expired',
    blocked_missing_credentials: 'not authorized',
    not_configured: 'setup needed',
    disabled: 'disabled',
    invalid_state: 'authorization state invalid',
    authorization_failed: 'authorization failed'
  };

  function credentialBadge(credential) {
    var status = (credential && credential.status) || 'not_configured';
    var known = CREDENTIAL_BADGES[status];
    return known ? { label: known.label, cls: known.cls } : { label: String(status), cls: 'badge-warn' };
  }

  function healthLabel(health) {
    var status = (health && health.status) || 'unchecked';
    return HEALTH_LABELS[status] || String(status);
  }

  function sideEffectText(operation) {
    if (operation.side_effect === 'read') return 'read-only · runs directly';
    return (operation.side_effect === 'write' ? 'writes data' : 'external effect') + ' · approval required each run';
  }

  function scopeShort(scope) {
    var parts = String(scope || '').split('/');
    return parts[parts.length - 1] || String(scope);
  }

  function operationMeta(operation) {
    return [
      'scopes: ' + (operation.scopes || []).map(scopeShort).join(', '),
      '≤' + operation.rate_limit_per_minute + '/min',
      'timeout ' + operation.timeout_seconds + 's',
      operation.retry_safe ? 'safe to retry' : 'no auto-retry',
      operation.idempotency === 'none' ? '' : 'idempotent: ' + operation.idempotency
    ].filter(Boolean).join(' · ');
  }

  function missingScopes(credential, operation) {
    var granted = (credential && credential.scopes) || [];
    return (operation.scopes || []).filter(function (scope) { return granted.indexOf(scope) === -1; });
  }

  function fieldsText(operation) {
    var names = Object.keys(operation.fields || {});
    if (!names.length) return '';
    return 'args: ' + names.map(function (name) {
      return name + (operation.fields[name].required ? '*' : '');
    }).join(', ');
  }

  function authorizeEnabled(connector) {
    var status = connector.credential && connector.credential.status;
    return status !== 'not_configured' && status !== 'disabled';
  }

  function summaryLine(status) {
    if (!status) return '';
    var connectors = status.connectors || [];
    var authorized = connectors.filter(function (item) {
      return item.credential && item.credential.status === 'ready';
    }).length;
    return connectors.length + ' connectors · ' + authorized + ' authorized'
      + (status.enabled ? '' : ' · connectors disabled in configuration');
  }

  var api = {
    credentialBadge: credentialBadge,
    healthLabel: healthLabel,
    sideEffectText: sideEffectText,
    scopeShort: scopeShort,
    operationMeta: operationMeta,
    missingScopes: missingScopes,
    fieldsText: fieldsText,
    authorizeEnabled: authorizeEnabled,
    summaryLine: summaryLine,
    refresh: function () {}
  };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var root = $('connectors-list');
    if (!root) return;
    var state = { status: null, loading: false, note: '', error: '', health: {} };

    function request(method, path, body) {
      var init = { method: method };
      if (body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(body);
      }
      return apiFetch(apiBase + path, init).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      });
    }

    function setNote(text) { state.note = text; render(); }

    function reload() {
      state.loading = true;
      request('GET', '/connectors').then(function (status) {
        state.status = status;
        state.loading = false;
        state.error = '';
        render();
      }).catch(function (error) {
        state.loading = false;
        state.error = 'Could not load connectors: ' + error.message;
        render();
      });
    }

    function checkHealth(id) {
      setNote('Checking ' + id + ' credentials against Google…');
      request('POST', '/connectors/' + encodeURIComponent(id) + '/health').then(function (health) {
        state.health[id] = health;
        setNote(id + ': ' + healthLabel(health) + (health.detail ? ' — ' + health.detail : ''));
        reload();
      }).catch(function (error) { setNote(id + ' health check failed: ' + error.message); });
    }

    function authorize(id) {
      setNote('Preparing the OAuth URL for ' + id + '…');
      request('POST', '/connectors/' + encodeURIComponent(id) + '/oauth/start').then(function (start) {
        setNote('Authorization URL ready — open it deliberately in a browser. State: ' + start.state);
        var card = root.querySelector('[data-connector="' + id + '"] .connector-auth');
        if (card) {
          card.hidden = false;
          card.replaceChildren();
          var link = document.createElement('a');
          link.href = start.url;
          link.target = '_blank';
          link.rel = 'noopener';
          link.className = 'connector-url';
          link.textContent = 'Open Google authorization page (new tab)';
          var urlText = document.createElement('code');
          urlText.className = 'connector-url-text';
          urlText.textContent = start.url;
          var note = document.createElement('small');
          note.className = 'dim';
          note.textContent = (start.note || '') + ' Redirect: ' + start.redirect_uri + ' · scopes: '
            + (start.scopes || []).map(scopeShort).join(', ');
          card.append(link, urlText, note);
        }
      }).catch(function (error) { setNote(id + ' cannot start authorization: ' + error.message); });
    }

    function renderOperation(connector, operation) {
      var row = document.createElement('div');
      row.className = 'connector-op';
      row.dataset.operation = operation.name;
      var name = document.createElement('strong');
      name.textContent = operation.name;
      var chips = document.createElement('span');
      chips.className = 'chip-row';
      var effect = document.createElement('span');
      effect.className = 'chip ' + (operation.side_effect === 'read' ? 'badge-ok' : 'badge-warn');
      effect.textContent = sideEffectText(operation);
      chips.append(effect);
      if (operation.approval) {
        var gate = document.createElement('span');
        gate.className = 'chip badge-warn';
        gate.textContent = 'approval each run';
        chips.append(gate);
      }
      var missing = missingScopes(connector.credential, operation);
      if (connector.credential.status === 'ready' && missing.length) {
        var scope = document.createElement('span');
        scope.className = 'chip badge-warn';
        scope.textContent = 'missing scope: ' + missing.map(scopeShort).join(', ');
        chips.append(scope);
      }
      var description = document.createElement('small');
      description.className = 'dim';
      description.textContent = operation.description;
      var meta = document.createElement('small');
      meta.className = 'dim';
      meta.textContent = [operationMeta(operation), fieldsText(operation)].filter(Boolean).join(' · ');
      row.append(name, chips, description, meta);
      return row;
    }

    function renderConnector(connector) {
      var card = document.createElement('article');
      card.className = 'card connector-card';
      card.dataset.connector = connector.id;

      var head = document.createElement('div');
      head.className = 'connector-head';
      var name = document.createElement('strong');
      name.textContent = connector.name;
      var version = document.createElement('small');
      version.className = 'dim';
      version.textContent = 'v' + connector.version + ' · ' + connector.auth;
      var badge = credentialBadge(connector.credential);
      var chip = document.createElement('span');
      chip.className = 'chip ' + badge.cls;
      chip.textContent = badge.label;
      var health = state.health[connector.id] || connector.health;
      var healthChip = document.createElement('span');
      healthChip.className = 'chip ' + (health && health.status === 'ok' ? 'badge-ok' : 'badge-planned');
      healthChip.textContent = 'health: ' + healthLabel(health);
      head.append(name, version, chip, healthChip);
      card.append(head);

      var description = document.createElement('small');
      description.className = 'dim';
      description.textContent = connector.description;
      card.append(description);

      if (connector.credential.detail) {
        var detail = document.createElement('small');
        detail.className = 'dim';
        detail.textContent = connector.credential.detail;
        card.append(detail);
      }
      if (connector.credential.status === 'not_configured') {
        var setup = document.createElement('small');
        setup.className = 'dim';
        setup.textContent = connector.setup;
        card.append(setup);
      }
      if (connector.credential.warning) {
        var warning = document.createElement('small');
        warning.className = 'dim';
        warning.textContent = connector.credential.warning;
        card.append(warning);
      }

      var actions = document.createElement('div');
      actions.className = 'connector-actions';
      var healthButton = document.createElement('button');
      healthButton.className = 'ghost';
      healthButton.textContent = 'Check health';
      healthButton.title = 'Validates the stored token with Google; never displays it.';
      healthButton.onclick = function () { checkHealth(connector.id); };
      var authButton = document.createElement('button');
      authButton.className = 'ghost';
      authButton.textContent = 'Authorize';
      authButton.disabled = !authorizeEnabled(connector);
      authButton.title = authorizeEnabled(connector)
        ? 'Fetches the OAuth URL for you to open deliberately; you approve access in your browser.'
        : (connector.credential.status === 'disabled'
          ? 'Connectors are disabled in configuration'
          : 'Create the OAuth client file first: ' + connector.setup);
      authButton.onclick = function () { authorize(connector.id); };
      actions.append(healthButton, authButton);
      card.append(actions);

      var authPanel = document.createElement('div');
      authPanel.className = 'connector-auth';
      authPanel.hidden = true;
      card.append(authPanel);

      var ops = document.createElement('div');
      ops.className = 'connector-ops';
      (connector.operations || []).forEach(function (operation) {
        ops.append(renderOperation(connector, operation));
      });
      card.append(ops);
      return card;
    }

    function render() {
      var summary = $('connectors-summary');
      if (summary) summary.textContent = state.loading && !state.status ? 'Loading connectors…'
        : summaryLine(state.status) || 'Connectors unavailable.';
      var note = $('connectors-note');
      if (note) { note.hidden = !state.note; note.textContent = state.note; }
      root.replaceChildren();
      if (state.error) {
        var failure = document.createElement('p');
        failure.className = 'dim';
        failure.textContent = state.error;
        root.append(failure);
        return;
      }
      if (state.loading && !state.status) {
        var loading = document.createElement('p');
        loading.className = 'dim';
        loading.textContent = 'Loading connector declarations…';
        root.append(loading);
        return;
      }
      (state.status.connectors || []).forEach(function (connector) { root.append(renderConnector(connector)); });
    }

    var refreshButton = $('connectors-refresh');
    if (refreshButton) refreshButton.onclick = reload;
    reload();
    api.refresh = reload;
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexConnectorsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
