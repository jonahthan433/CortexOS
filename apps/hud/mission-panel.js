/* CortexOS Mission Control panels: agent roster, task pipeline, activity strip.
 * Pure helpers are exported for tests; the controllers only read live API data
 * and render it — no fabricated metrics, no local state beyond the response. */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------- helpers */

  function text(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function trim(value, max) {
    var clean = text(value).replace(/\s+/g, ' ').trim();
    if (clean.length <= max) return clean;
    return clean.slice(0, Math.max(1, max - 1)).replace(/\s+$/, '') + '…';
  }

  function ageLabel(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds)));
    if (!isFinite(s)) return 'now';
    if (s < 45) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }

  var STAGE_BY_STATUS = {
    queued: 'capture',
    awaiting_approval: 'gate',
    running: 'execute',
    completed: 'shipped',
    failed: 'unshipped',
    cancelled: 'unshipped'
  };

  function stageOf(status) {
    return STAGE_BY_STATUS[text(status)] || 'other';
  }

  function agentKey(task) {
    return text(task && (task.worker || task.backend)).toLowerCase();
  }

  function taskAgent(task) {
    return agentKey(task) || 'auto';
  }

  function taskTime(task) {
    var t = task || {};
    return Number(t.finished_at || t.updated_at || t.started_at || t.created_at || 0);
  }

  function bucketTasks(tasks) {
    var buckets = { capture: [], gate: [], execute: [], shipped: [], unshipped: [], other: [] };
    for (var i = 0; i < (tasks || []).length; i++) {
      var task = tasks[i] || {};
      (buckets[stageOf(task.status)] || buckets.other).push(task);
    }
    buckets.capture.sort(function (a, b) {
      return (Number(a.priority) || 9) - (Number(b.priority) || 9) || (Number(a.created_at) || 0) - (Number(b.created_at) || 0);
    });
    buckets.gate.sort(function (a, b) { return (Number(a.updated_at) || 0) - (Number(b.updated_at) || 0); });
    buckets.execute.sort(function (a, b) { return (Number(a.started_at) || 0) - (Number(b.started_at) || 0); });
    var byRecency = function (a, b) { return taskTime(b) - taskTime(a); };
    buckets.shipped.sort(byRecency);
    buckets.unshipped.sort(byRecency);
    return buckets;
  }

  function agentActiveCount(tasks, agent) {
    if (!agent) return 0;
    var wanted = [text(agent.id).toLowerCase(), text(agent.name).toLowerCase()].filter(Boolean);
    if (!wanted.length) return 0;
    var count = 0;
    for (var i = 0; i < (tasks || []).length; i++) {
      var task = tasks[i] || {};
      var status = text(task.status);
      if (status !== 'queued' && status !== 'running' && status !== 'awaiting_approval') continue;
      if (wanted.indexOf(agentKey(task)) !== -1) count++;
    }
    return count;
  }

  function agentState(entry, active) {
    if (!entry || !entry.implemented) return { text: 'Planned', cls: 'badge-planned' };
    if (entry.issues && entry.issues.length) return { text: 'Needs attention', cls: 'badge-warn' };
    if (active > 0) return { text: 'Working', cls: 'badge-live' };
    return { text: 'Ready', cls: 'badge-ok' };
  }

  function taskMeta(task, now) {
    var parts = [];
    parts.push('P' + (task.priority === undefined || task.priority === null ? '?' : task.priority));
    parts.push(taskAgent(task) === 'auto' ? 'auto routing' : taskAgent(task));
    if (task.skill_id) parts.push(task.skill_id);
    var at = taskTime(task);
    if (at) parts.push(ageLabel(now - at) + ' ago');
    return parts.join(' · ');
  }

  /* ------------------------------------------------------------- DOM util */

  function el(tag, cls, value) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (value !== undefined && value !== null) node.textContent = value;
    return node;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  /* ------------------------------------------------------------ controllers */

  function initRoster(options) {
    options = options || {};
    var root = byId('mission-roster');
    var stats = byId('mission-agents-stat');
    var state = { agents: null, tasks: [], error: null };

    function updateStat() {
      if (!stats || !state.agents) return;
      var implemented = 0;
      var active = 0;
      for (var i = 0; i < state.agents.length; i++) {
        if (state.agents[i] && state.agents[i].implemented) implemented++;
        active += agentActiveCount(state.tasks, state.agents[i]);
      }
      stats.textContent = implemented + ' agents · ' + active + ' active';
      stats.classList.toggle('alert', active > 0);
      if (options.onAgents) options.onAgents(state.agents.length);
    }

    function render() {
      if (!root) return;
      root.replaceChildren();
      if (state.agents === null) {
        root.append(el('p', 'dim', 'Loading agents…'));
        return;
      }
      if (state.error && !state.agents.length) {
        root.append(el('p', 'dim', 'Agent directory unavailable — ' + state.error));
        return;
      }
      root.classList.toggle('stale', !!state.error);
      var sorted = state.agents.slice().sort(function (a, b) {
        if (!!b.implemented !== !!a.implemented) return a.implemented ? -1 : 1;
        return text(a.name).localeCompare(text(b.name));
      });
      if (!sorted.length) {
        root.append(el('p', 'dim', 'No agents registered yet.'));
        updateStat();
        return;
      }
      sorted.forEach(function (agent) {
        var active = agentActiveCount(state.tasks, agent);
        var chip = agentState(agent, active);
        var card = el('div', 'roster-card');
        var head = el('div', 'roster-head');
        head.append(el('strong', null, agent.name || agent.id), el('span', 'chip ' + chip.cls, chip.text));
        card.append(head);
        card.append(el('small', 'roster-meta', [agent.role, agent.lane].filter(Boolean).join(' · ')));
        card.append(el('small', 'roster-work', active ? active + ' active task' + (active === 1 ? '' : 's') : 'no active tasks'));
        if (agent.implemented && agent.recent && agent.recent.length) {
          var runs = el('div', 'roster-runs');
          agent.recent.slice(0, 3).forEach(function (run) {
            var row = el('button', 'agent-run');
            row.type = 'button';
            row.append(el('span', null, run.title || run.id));
            row.append(el('span', 'chip st-' + text(run.status || 'unknown').replace(/_/g, '-')));
            row.onclick = function (event) {
              event.stopPropagation();
              if (options.onOpenTask) options.onOpenTask({ id: run.id });
            };
            runs.append(row);
          });
          card.append(runs);
        }
        card.onclick = function () { if (options.onOpenAgents) options.onOpenAgents(); };
        root.append(card);
      });
      updateStat();
    }

    function refresh() {
      return Promise.all([
        options.apiFetch(options.api + '/agents/directory'),
        options.apiFetch(options.api + '/tasks?limit=100')
      ]).then(function (responses) {
        return Promise.all([responses[0].json(), responses[1].json()]);
      }).then(function (payloads) {
        var directory = payloads[0];
        var tasks = payloads[1];
        state.agents = (directory && directory.agents) || [];
        state.tasks = Array.isArray(tasks) ? tasks : ((tasks && tasks.tasks) || []);
        state.error = null;
        render();
      }).catch(function (error) {
        state.error = error && error.message ? error.message : 'request failed';
        if (state.agents === null) state.agents = [];
        render();
      });
    }

    return { refresh: refresh, render: render };
  }

  function initPipeline(options) {
    options = options || {};
    var root = byId('mission-pipeline');
    var stats = byId('mission-tasks-stat');
    var gateStat = byId('mission-gate-stat');
    var state = { tasks: [], approvals: [], error: null };
    var STAGES = [
      { key: 'capture', title: 'Capture' },
      { key: 'gate', title: 'Human gate' },
      { key: 'execute', title: 'Execute' },
      { key: 'shipped', title: 'Shipped' }
    ];

    function approvalCard(item) {
      var card = el('div', 'pipe-card gate-card');
      card.append(el('strong', 'pipe-label', (item.skill || 'Request') + (item.risk_level ? ' · ' + item.risk_level : '')));
      if (item.reason) card.append(el('small', 'pipe-sub', trim(item.reason, 120)));
      card.append(el('small', 'pipe-sub', trim(item.prompt || '', 140)));
      var actions = el('div', 'pipe-actions');
      var approveBtn = el('button', 'ghost primary-outline', 'Approve');
      var rejectBtn = el('button', 'ghost', 'Reject');
      approveBtn.onclick = function () { if (options.onApprove) options.onApprove(item.id); };
      rejectBtn.onclick = function () { if (options.onReject) options.onReject(item.id); };
      actions.append(approveBtn, rejectBtn);
      card.append(actions);
      return card;
    }

    function taskCard(task, tone) {
      var now = Math.floor(Date.now() / 1000);
      var card = el('button', 'pipe-card ' + (tone || ''));
      card.type = 'button';
      card.append(el('span', 'q-chip st-' + text(task.status || 'unknown').replace(/_/g, '-'), text(task.status || 'unknown').replace(/_/g, ' ')));
      card.append(el('span', 'pipe-label', trim(task.prompt || task.id, 150)));
      card.append(el('small', 'pipe-sub', taskMeta(task, now)));
      if (task.error && (task.status === 'failed' || task.status === 'cancelled')) {
        card.append(el('small', 'pipe-error', trim(task.error, 120)));
      }
      card.onclick = function () { if (options.onOpenTask) options.onOpenTask(task); };
      return card;
    }

    function render() {
      if (!root) return;
      root.replaceChildren();
      if (state.error && !state.tasks.length) {
        root.append(el('p', 'dim', 'Pipeline unavailable — ' + state.error));
        return;
      }
      var buckets = bucketTasks(state.tasks);
      var gateItems = state.approvals.length + buckets.gate.length;
      var counts = {
        capture: buckets.capture.length,
        gate: gateItems,
        execute: buckets.execute.length,
        shipped: buckets.shipped.length
      };
      STAGES.forEach(function (stage) {
        var col = el('section', 'pipe-col pipe-' + stage.key);
        var head = el('header', 'pipe-col-head');
        head.append(el('span', 'pipe-title', stage.title), el('span', 'pipe-count', String(counts[stage.key] || 0)));
        col.append(head);
        var body = el('div', 'pipe-col-body');
        if (stage.key === 'capture') {
          if (!buckets.capture.length) body.append(el('p', 'dim pipe-empty', 'Nothing queued.'));
          buckets.capture.forEach(function (task) { body.append(taskCard(task, 'queued')); });
        } else if (stage.key === 'gate') {
          if (!gateItems) body.append(el('p', 'dim pipe-empty', 'Gate is clear.'));
          state.approvals.forEach(function (item) { body.append(approvalCard(item)); });
          buckets.gate.forEach(function (task) { body.append(taskCard(task, 'gate')); });
        } else if (stage.key === 'execute') {
          if (!buckets.execute.length) body.append(el('p', 'dim pipe-empty', 'Workers idle.'));
          buckets.execute.forEach(function (task) { body.append(taskCard(task, 'running')); });
        } else {
          if (!buckets.shipped.length) body.append(el('p', 'dim pipe-empty', 'Nothing shipped yet.'));
          buckets.shipped.slice(0, 6).forEach(function (task) { body.append(taskCard(task, 'shipped')); });
          if (buckets.unshipped.length) {
            body.append(el('small', 'pipe-divider', buckets.unshipped.length + ' not shipped'));
            buckets.unshipped.slice(0, 5).forEach(function (task) { body.append(taskCard(task, 'unshipped')); });
          }
        }
        col.append(body);
        root.append(col);
      });
      if (stats) {
        stats.textContent = state.tasks.length + ' tasks · ' + buckets.capture.length + ' queued';
        stats.classList.toggle('alert', buckets.capture.length > 0);
      }
      if (gateStat) {
        gateStat.textContent = gateItems ? gateItems + ' awaiting you' : 'gate clear';
        gateStat.classList.toggle('alert', gateItems > 0);
      }
    }

    function refresh() {
      return Promise.all([
        options.apiFetch(options.api + '/tasks?limit=60'),
        options.apiFetch(options.api + '/approvals')
      ]).then(function (responses) {
        return Promise.all([responses[0].json(), responses[1].json()]);
      }).then(function (payloads) {
        var tasks = payloads[0];
        var approvals = payloads[1];
        state.tasks = Array.isArray(tasks) ? tasks : ((tasks && tasks.tasks) || []);
        state.approvals = Array.isArray(approvals) ? approvals : ((approvals && approvals.items) || []);
        state.error = null;
        render();
      }).catch(function (error) {
        state.error = error && error.message ? error.message : 'request failed';
        render();
      });
    }

    return { refresh: refresh, render: render };
  }

  function initStrip(options) {
    options = options || {};
    var root = byId('mission-activity');

    function refresh() {
      if (!root) return Promise.resolve();
      return options.apiFetch(options.api + '/activity?limit=8').then(function (response) {
        return response.json();
      }).then(function (data) {
        var items = (data && data.items) || [];
        root.replaceChildren();
        if (!items.length) {
          root.append(el('p', 'dim', 'No activity recorded yet.'));
          return;
        }
        var now = Math.floor(Date.now() / 1000);
        items.forEach(function (item) {
          var card = el('button', 'strip-card');
          card.type = 'button';
          var head = el('span', 'strip-head');
          head.append(el('span', 'activity-kind k-' + text(item.kind || 'event'), text(item.kind || 'event')));
          head.append(el('span', 'strip-time', ageLabel(now - Number(item.at || 0))));
          card.append(head);
          card.append(el('strong', 'strip-title', trim(item.title || item.id, 90)));
          if (item.detail) card.append(el('small', 'strip-detail', trim(item.detail, 110)));
          card.onclick = function () { if (options.onOpen) options.onOpen(item); };
          root.append(card);
        });
      }).catch(function (error) {
        root.replaceChildren(el('p', 'dim', 'Activity unavailable — ' + (error && error.message ? error.message : 'request failed')));
      });
    }

    return { refresh: refresh };
  }

  function init(options) {
    options = options || {};
    var roster = initRoster(options);
    var pipeline = initPipeline(options);
    var strip = initStrip(options);
    roster.refresh();
    pipeline.refresh();
    strip.refresh();
    return {
      roster: roster,
      pipeline: pipeline,
      strip: strip,
      refreshAll: function () {
        roster.refresh();
        pipeline.refresh();
        strip.refresh();
      }
    };
  }

  var api = {
    text: text,
    trim: trim,
    ageLabel: ageLabel,
    stageOf: stageOf,
    agentKey: agentKey,
    taskAgent: taskAgent,
    taskTime: taskTime,
    bucketTasks: bucketTasks,
    agentActiveCount: agentActiveCount,
    agentState: agentState,
    taskMeta: taskMeta,
    init: init
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexMissionPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
