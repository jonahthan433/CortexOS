/* CortexOS · Suggestions panel for the JARVIS HUD.
 *
 * Renders the bounded, evidence-backed suggestion list from /api/suggestions.
 * Evaluation itself never executes anything; every action here is an explicit
 * click that goes through the same guarded service paths (queue retry, review
 * request) and reports exactly what happened. Pure helpers are tested with
 * `node --test`; no fake data is ever rendered.
 */
(function (global) {
  'use strict';

  var KIND_LABELS = {
    failed_task_recovery: 'Failed task',
    scheduled_job: 'Scheduled job',
    automation_stale: 'Stale automation',
    automation_candidate: 'Automation candidate',
    repeated_workflow: 'Repeated workflow',
    reliability_review: 'Reliability review'
  };

  function kindLabel(kind) { return KIND_LABELS[kind] || String(kind || 'suggestion'); }

  function formatAge(epoch, nowSeconds) {
    if (epoch === null || epoch === undefined || epoch === '') return 'unknown';
    var now = nowSeconds === undefined ? Date.now() / 1000 : nowSeconds;
    var age = Math.max(0, Math.round(now - Number(epoch)));
    if (age < 10) return 'just now';
    if (age < 90) return age + 's ago';
    if (age < 5400) return Math.round(age / 60) + 'm ago';
    if (age < 36 * 3600) return Math.round(age / 3600) + 'h ago';
    return Math.round(age / 86400) + 'd ago';
  }

  function summaryLine(report, nowSeconds) {
    if (!report || !report.total) return '';
    var counts = report.counts || {};
    var parts = Object.keys(counts).sort().map(function (kind) {
      return counts[kind] + ' ' + kindLabel(kind).toLowerCase();
    });
    return report.total + ' active · ' + parts.join(', ') + ' · evaluated ' + formatAge(report.generated_at, nowSeconds);
  }

  function evidenceText(entry) {
    if (!entry) return '';
    var head = (entry.type || 'evidence') + (entry.id ? ' ' + entry.id : '');
    return entry.detail ? head + ' — ' + entry.detail : head;
  }

  function actionEnabled(action) { return !action || action.available === undefined ? true : !!action.available; }

  function actionTitle(action) {
    if (!action) return '';
    return action.reason || (actionEnabled(action) ? '' : 'Not available yet');
  }

  function describeAct(data) {
    if (!data) return 'The action returned no result.';
    if (data.executed) {
      var status = data.result && data.result.status ? ' · ' + data.result.status : '';
      return 'Executed: ' + data.action + status;
    }
    return 'Nothing was executed' + (data.reference ? ' · reference: ' + data.reference : '');
  }

  function describeDecision(data) {
    if (!data) return 'The decision returned no result.';
    var text = data.status === 'dismissed' ? 'Dismissed' : data.status === 'snoozed' ? 'Snoozed' : (data.status || 'saved');
    if (data.until) text += ' · hidden until ' + new Date(data.until * 1000).toLocaleString();
    return text;
  }

  var api = {
    KIND_LABELS: KIND_LABELS,
    kindLabel: kindLabel,
    formatAge: formatAge,
    summaryLine: summaryLine,
    evidenceText: evidenceText,
    actionEnabled: actionEnabled,
    actionTitle: actionTitle,
    describeAct: describeAct,
    describeDecision: describeDecision,
    refresh: function () {}
  };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var root = $('suggestions-list');
    if (!root) return;
    var state = { report: null, loading: false, error: '', note: '' };

    function post(path, body) {
      var init = { method: 'POST' };
      if (body) {
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
      state.error = '';
      render();
      apiFetch(apiBase + '/suggestions').then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      }).then(function (report) {
        state.report = report;
        state.loading = false;
        render();
      }).catch(function (error) {
        state.loading = false;
        state.error = error.message;
        render();
      });
    }

    function act(item, action) {
      setNote('Working on ' + kindLabel(item.kind) + '…');
      post('/suggestions/' + encodeURIComponent(item.id) + '/act', { action: action.action })
        .then(function (data) { setNote(describeAct(data)); reload(); })
        .catch(function (error) { setNote('Could not run this action: ' + error.message); });
    }

    function decide(item, decision, body) {
      setNote('Recording ' + decision + '…');
      post('/suggestions/' + encodeURIComponent(item.id) + '/' + decision, body)
        .then(function (data) { setNote(describeDecision(data)); reload(); })
        .catch(function (error) { setNote('Could not record the decision: ' + error.message); });
    }

    function actionButton(item, action) {
      var button = document.createElement('button');
      button.className = 'ghost';
      button.textContent = action.label || action.action;
      button.disabled = !actionEnabled(action);
      var title = actionTitle(action);
      if (title) button.title = title;
      button.onclick = function () { act(item, action); };
      return button;
    }

    function renderItem(item) {
      var card = document.createElement('article');
      card.className = 'card suggestion-card';
      card.dataset.suggestionId = item.id;

      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = kindLabel(item.kind);
      var title = document.createElement('strong');
      title.textContent = item.title || item.id;
      var why = document.createElement('p');
      why.className = 'suggestion-why';
      why.textContent = item.why || '';
      card.append(chip, title, why);

      (item.evidence || []).forEach(function (entry) {
        var line = document.createElement('small');
        line.className = 'dim suggestion-evidence';
        line.textContent = evidenceText(entry);
        card.append(line);
      });

      var actions = document.createElement('div');
      actions.className = 'suggestion-actions';
      (item.actions || []).forEach(function (action) { actions.append(actionButton(item, action)); });
      var dismiss = document.createElement('button');
      dismiss.className = 'ghost';
      dismiss.textContent = 'Dismiss';
      dismiss.title = 'Hide this suggestion (kept in state/suggestions.json; never deletes anything)';
      dismiss.onclick = function () { decide(item, 'dismiss'); };
      var snooze = document.createElement('button');
      snooze.className = 'ghost';
      snooze.textContent = 'Snooze 8h';
      snooze.onclick = function () { decide(item, 'snooze', { hours: 8 }); };
      actions.append(dismiss, snooze);
      card.append(actions);
      return card;
    }

    function render() {
      var summary = $('suggestions-summary');
      if (summary) {
        summary.textContent = state.loading && !state.report ? 'Evaluating current state…'
          : summaryLine(state.report, Date.now() / 1000) || 'No active suggestions.';
      }
      var note = $('suggestions-note');
      if (note) {
        note.hidden = !state.note;
        note.textContent = state.note;
      }
      root.replaceChildren();
      if (state.error) {
        var failure = document.createElement('p');
        failure.className = 'dim';
        failure.textContent = 'Could not load suggestions: ' + state.error;
        root.append(failure);
        return;
      }
      if (state.loading && !state.report) {
        var loading = document.createElement('p');
        loading.className = 'dim';
        loading.textContent = 'Evaluating queue, jobs, and skills…';
        root.append(loading);
        return;
      }
      var items = (state.report && state.report.items) || [];
      if (!items.length) {
        var empty = document.createElement('p');
        empty.className = 'dim';
        empty.textContent = 'Nothing needs attention right now. Suggestions appear here when evidence (failed tasks, stale automation, repeated work) justifies them.';
        root.append(empty);
        return;
      }
      items.forEach(function (item) { root.append(renderItem(item)); });
    }

    var refresh = $('suggestions-refresh');
    if (refresh) refresh.onclick = reload;
    var panel = $('suggestions');
    if (panel) {
      panel.addEventListener('cortexos:visible', reload);
    }
    reload();
    api.refresh = reload;
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexSuggestionsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
