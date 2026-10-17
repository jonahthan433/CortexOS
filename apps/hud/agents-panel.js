/* CortexOS · Agents directory panel for the JARVIS HUD.
 *
 * Renders /api/agents/directory: CLI execution adapters, model-gateway
 * agents, and skill-backed role agents. Implemented versus planned is shown
 * exactly as the backend labels it — nothing is dressed up. Pure helpers are
 * unit-tested with `node --test`.
 */
(function (global) {
  'use strict';

  var KIND_LABELS = { cli: 'CLI adapter', llm: 'model gateway', role: 'role agent' };

  function kindLabel(kind) { return KIND_LABELS[kind] || String(kind || 'unknown'); }

  function agentBadge(entry) {
    if (!entry || !entry.implemented) return { text: 'Planned', cls: 'badge-planned' };
    if (entry.issues && entry.issues.length) return { text: 'Needs attention', cls: 'badge-warn' };
    return { text: 'Ready', cls: 'badge-ok' };
  }

  function capabilityChips(entry) {
    return (entry && entry.capabilities) ? entry.capabilities.slice(0, 8) : [];
  }

  function assignmentText(entry) {
    if (!entry || !entry.assignment) return '';
    return entry.assignment.provider + ' · ' + (entry.assignment.model || 'default model');
  }

  function laneText(entry) {
    if (!entry) return '';
    var parts = [kindLabel(entry.kind)];
    if (entry.role) parts.push(entry.role);
    if (entry.lane) parts.push(entry.lane);
    if (entry.autonomous === true) parts.push('autonomous');
    else if (entry.autonomous === false) parts.push('human-gated');
    return parts.join(' · ');
  }

  function filterEntries(entries, kind) {
    if (!kind || kind === 'all') return (entries || []).slice();
    return (entries || []).filter(function (entry) { return entry.kind === kind; });
  }

  var api = { kindLabel: kindLabel, agentBadge: agentBadge, capabilityChips: capabilityChips,
              assignmentText: assignmentText, laneText: laneText, filterEntries: filterEntries };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onOpenTask = options.onOpenTask || function () {};
    var state = { entries: [], lastOk: 0, failures: 0 };

    function setBanner(text) {
      var banner = $('agents-banner');
      if (!banner) return;
      banner.hidden = false;
      banner.className = 'queue-banner error';
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

    function load() {
      apiFetch(apiBase + '/agents/directory').then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      }).then(function (payload) {
        state.failures = 0;
        state.lastOk = Date.now();
        state.entries = payload.agents || [];
        render();
      }).catch(function (error) {
        state.failures += 1;
        state.entries = [];
        render();
        var list = $('agents-list');
        if (list) list.replaceChildren(message('Agents could not load: ' + error.message));
        setBanner('Agents request failed: ' + error.message);
      });
    }

    function render() {
      var list = $('agents-list');
      if (!list) return;
      list.replaceChildren();
      var filter = $('agents-filter');
      var entries = filterEntries(state.entries, filter ? filter.value : 'all');
      if (!entries.length) {
        list.append(message('No agents in this filter.'));
        return;
      }
      entries.forEach(function (entry) {
        var card = document.createElement('article');
        card.className = 'card agent-card' + (entry.implemented ? '' : ' planned');

        var head = document.createElement('div');
        head.className = 'agent-head';
        var name = document.createElement('strong');
        name.textContent = entry.name || entry.id;
        var id = document.createElement('small');
        id.className = 'dim';
        id.textContent = entry.id;
        var badge = document.createElement('span');
        var badgeInfo = agentBadge(entry);
        badge.className = 'chip ' + badgeInfo.cls;
        badge.textContent = badgeInfo.text;
        head.append(name, id, badge);
        card.append(head);

        var lane = document.createElement('small');
        lane.className = 'agent-lane';
        lane.textContent = laneText(entry);
        card.append(lane);

        var detail = document.createElement('p');
        detail.className = 'agent-detail';
        detail.textContent = entry.detail || '';
        card.append(detail);

        var assignment = assignmentText(entry);
        if (assignment) {
          var assigned = document.createElement('small');
          assigned.className = 'agent-assign';
          assigned.textContent = 'provider: ' + assignment;
          card.append(assigned);
        }

        var chips = capabilityChips(entry);
        if (chips.length) {
          var chipRow = document.createElement('div');
          chipRow.className = 'chip-row';
          chips.forEach(function (name) {
            var chip = document.createElement('span');
            chip.className = 'chip cap';
            chip.textContent = name;
            chipRow.append(chip);
          });
          card.append(chipRow);
        }

        if (entry.issues && entry.issues.length) {
          var issues = document.createElement('ul');
          issues.className = 'agent-issues';
          entry.issues.forEach(function (issue) {
            var item = document.createElement('li');
            item.textContent = issue;
            issues.append(item);
          });
          card.append(issues);
        }

        if (entry.recent && entry.recent.length) {
          var recent = document.createElement('div');
          recent.className = 'agent-recent';
          var label = document.createElement('small');
          label.className = 'dim';
          label.textContent = 'recent runs';
          recent.append(label);
          entry.recent.forEach(function (run) {
            var row = document.createElement('button');
            row.type = 'button';
            row.className = 'agent-run';
            var title = document.createElement('span');
            title.textContent = run.title || run.id;
            var status = document.createElement('span');
            status.className = 'chip ' + ('st-' + String(run.status || 'unknown').replace(/_/g, '-'));
            status.textContent = run.status || 'unknown';
            row.append(title, status);
            row.onclick = function () { onOpenTask({ id: run.id }); };
            recent.append(row);
          });
          card.append(recent);
        } else if (entry.implemented) {
          var none = document.createElement('small');
          none.className = 'dim';
          none.textContent = 'no recent runs recorded';
          card.append(none);
        }

        list.append(card);
      });
    }

    var filter = $('agents-filter');
    if (filter) filter.onchange = render;
    var refresh = $('agents-refresh');
    if (refresh) refresh.onclick = load;

    load();
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexAgentsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
