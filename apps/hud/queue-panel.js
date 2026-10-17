/* CortexOS · Queue panel for the JARVIS HUD.
 *
 * Pure helpers live at the top (unit-tested with node --test). The controller
 * below wires the real /api/tasks + /api/queue/summary endpoints into the DOM.
 * Loaded as a plain script before hud.js; in node it exports the helpers.
 * No fake data: every value rendered comes from the backend response.
 */
(function (global) {
  'use strict';

  var STATUS = {
    queued: { label: 'Queued', cls: 'st-queued' },
    running: { label: 'Running', cls: 'st-running' },
    awaiting_approval: { label: 'Awaiting approval', cls: 'st-awaiting' },
    completed: { label: 'Completed', cls: 'st-completed' },
    failed: { label: 'Failed', cls: 'st-failed' },
    cancelled: { label: 'Cancelled', cls: 'st-cancelled' }
  };
  var ACTIVE = ['queued', 'running', 'awaiting_approval'];

  function statusLabel(status) { return (STATUS[status] || {}).label || 'Unknown'; }
  function statusClass(status) { return (STATUS[status] || {}).cls || 'st-unknown'; }
  function canCancel(task) { return ACTIVE.indexOf(task.status) !== -1; }
  function canRetry(task) { return task.status === 'failed' || task.status === 'cancelled'; }
  function canFollow(task) { return task.status === 'queued' || task.status === 'running'; }

  function formatTimestamp(epoch, now) {
    if (epoch === null || epoch === undefined || epoch === '') return '—';
    var d = new Date(Number(epoch) * 1000);
    if (isNaN(d.getTime())) return '—';
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var ref = now ? new Date(now) : new Date();
    var hhmmss = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    if (d.toDateString() === ref.toDateString()) return hhmmss;
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hhmmss;
  }

  function applyFilters(tasks, filters) {
    filters = filters || {};
    var status = filters.status || 'all';
    var priority = filters.priority || 'any';
    var limit = filters.limit || 25;
    var out = (tasks || []).filter(function (task) {
      if (status !== 'all' && task.status !== status) return false;
      if (priority !== 'any' && String(task.priority) !== String(priority)) return false;
      return true;
    });
    return out.slice(0, limit);
  }

  function emptyStateFor(loading, tasks) {
    if (loading) return 'loading';
    return (tasks && tasks.length) ? 'rows' : 'empty';
  }

  function retryDelayFor(failures, base, max) {
    base = base || 5000; max = max || 30000;
    if (failures <= 1) return base;
    return Math.min(max, base * Math.pow(2, failures - 1));
  }

  function activeCount(summary) {
    if (!summary) return 0;
    return (summary.queued || 0) + (summary.running || 0) + (summary.awaiting_approval || 0);
  }

  function nextPollDelay(state) {
    if (state.failures > 0) return retryDelayFor(state.failures);
    return activeCount(state.summary) > 0 ? 5000 : 20000;
  }

  function staleness(nowMs, lastOkMs, failures, staleAfterSeconds) {
    staleAfterSeconds = staleAfterSeconds || 45;
    if (!lastOkMs) return { stale: true, label: 'no data yet' };
    var age = Math.max(0, Math.round((nowMs - lastOkMs) / 1000));
    if (failures > 0) return { stale: true, label: 'last ok ' + age + 's ago · reconnecting' };
    var label = age < 2 ? 'just updated' : 'updated ' + age + 's ago';
    return { stale: age > staleAfterSeconds, label: label };
  }

  function shouldBlockSubmit(state, nowMs, cooldownMs) {
    cooldownMs = cooldownMs === undefined ? 3000 : cooldownMs;
    if (state.submitting) return true;
    if (state.lastSubmitAt && (nowMs - state.lastSubmitAt) < cooldownMs) return true;
    return false;
  }

  function attemptLabel(task, nowSeconds) {
    var text = (task.attempts || 0) + '/' + (task.max_attempts || 0) + ' attempts';
    if (task.next_attempt_at) {
      var nowS = nowSeconds === undefined ? Date.now() / 1000 : nowSeconds;
      text = (task.attempts || 0) + '/' + (task.max_attempts || 0)
        + ' · retry in ' + Math.max(0, Math.round(task.next_attempt_at - nowS)) + 's';
    } else if (task.cycle) {
      text += ' · cycle ' + (task.cycle + 1);
    }
    return text;
  }

  function tierLabel(task) {
    var meta = task.meta || {};
    if (meta.tier) return 'T' + meta.tier + (meta.backend ? ' · ' + meta.backend : '');
    if (task.backend) return task.backend;
    return '—';
  }

  function taskLabel(task) {
    var prompt = String(task.prompt || '');
    return prompt.length > 120 ? prompt.slice(0, 117) + '…' : prompt;
  }

  function errorSummary(task) {
    var text = String(task.error || '');
    if (!text) return '';
    return text.length > 140 ? text.slice(0, 137) + '…' : text;
  }

  var api = {
    STATUS: STATUS,
    statusLabel: statusLabel,
    statusClass: statusClass,
    canCancel: canCancel,
    canRetry: canRetry,
    canFollow: canFollow,
    formatTimestamp: formatTimestamp,
    applyFilters: applyFilters,
    emptyStateFor: emptyStateFor,
    retryDelayFor: retryDelayFor,
    activeCount: activeCount,
    nextPollDelay: nextPollDelay,
    staleness: staleness,
    shouldBlockSubmit: shouldBlockSubmit,
    attemptLabel: attemptLabel,
    tierLabel: tierLabel,
    taskLabel: taskLabel,
    errorSummary: errorSummary
  };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onFollow = options.onFollow || function () {};
    var documentRef = options.documentRef || document;

    var state = {
      tasks: [], summary: null, workers: [], loading: false, failures: 0, lastOk: 0,
      submitting: false, lastSubmitAt: 0, openTaskId: null, detail: null,
      timer: null, bannerTimer: null
    };

    function setBanner(kind, text) {
      var banner = $('queue-banner');
      if (!banner) return;
      banner.hidden = false;
      banner.className = 'queue-banner ' + kind;
      banner.textContent = text;
      clearTimeout(state.bannerTimer);
      state.bannerTimer = setTimeout(function () { banner.hidden = true; }, 10000);
    }

    function workerText() {
      var workers = state.workers || [];
      var alive = workers.filter(function (w) { return w.alive; }).length;
      return alive + '/' + workers.length + ' workers';
    }

    function renderSummary() {
      var root = $('queue-summary');
      if (!root) return;
      root.replaceChildren();
      var summary = state.summary;
      var order = ['queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled'];
      for (var i = 0; i < order.length; i++) {
        var key = order[i];
        var chip = document.createElement('span');
        chip.className = 'q-summary-chip ' + statusClass(key);
        chip.textContent = statusLabel(key) + ' ' + ((summary && summary[key]) || 0);
        root.append(chip);
      }
      var workers = document.createElement('span');
      workers.className = 'q-summary-chip st-meta';
      workers.textContent = workerText();
      root.append(workers);
      if (summary && summary.queued_ready > 0 && summary.oldest_ready_seconds != null) {
        var oldest = document.createElement('span');
        oldest.className = 'q-summary-chip st-meta';
        oldest.textContent = 'oldest ready ' + Math.round(summary.oldest_ready_seconds) + 's';
        root.append(oldest);
      }
      var stale = staleness(Date.now(), state.lastOk, state.failures);
      var stamp = $('queue-updated');
      if (stamp) {
        stamp.textContent = stale.label;
        stamp.classList.toggle('stale', stale.stale);
      }
      var vital = $('queue-vital');
      if (vital && summary) {
        vital.textContent = activeCount(summary) + ' active';
        var detail = $('queue-vital-detail');
        if (detail) {
          detail.textContent = workerText()
            + (summary.failed_last_24h ? ' · ' + summary.failed_last_24h + ' failed 24h' : '');
        }
      }
    }

    function actionButton(task, action, label, confirmText) {
      var button = document.createElement('button');
      button.className = 'ghost q-action';
      button.textContent = label;
      button.dataset.queueAction = action;
      button.dataset.task = task.id;
      button.onclick = function (event) {
        event.stopPropagation();
        if (confirmText && !documentRef.defaultView.confirm(confirmText)) return;
        act(task.id, action);
      };
      return button;
    }

    function renderRow(task) {
      var row = document.createElement('div');
      row.className = 'queue-row';
      row.dataset.status = task.status;

      var main = document.createElement('button');
      main.className = 'queue-row-main';
      main.onclick = function () {
        if (state.openTaskId === task.id) {
          state.openTaskId = null;
          state.detail = null;
          renderDetail();
        } else {
          loadDetail(task.id);
        }
      };

      var chip = document.createElement('span');
      chip.className = 'q-chip ' + statusClass(task.status);
      chip.textContent = statusLabel(task.status);
      var priority = document.createElement('span');
      priority.className = 'q-priority';
      priority.textContent = 'P' + task.priority;
      var label = document.createElement('span');
      label.className = 'q-label';
      label.textContent = taskLabel(task);
      var meta = document.createElement('span');
      meta.className = 'q-meta';
      meta.textContent = task.id.slice(0, 8) + ' · ' + tierLabel(task) + ' · '
        + attemptLabel(task) + ' · ' + formatTimestamp(task.updated_at);
      main.append(chip, priority, label, meta);
      if (task.error) {
        var err = document.createElement('span');
        err.className = 'q-error';
        err.textContent = errorSummary(task);
        main.append(err);
      }

      var actions = document.createElement('div');
      actions.className = 'q-actions';
      if (canFollow(task)) {
        var follow = document.createElement('button');
        follow.className = 'ghost q-action';
        follow.textContent = 'Follow';
        follow.title = 'Watch live output for this task';
        follow.onclick = function (event) {
          event.stopPropagation();
          onFollow(task);
        };
        actions.append(follow);
      }
      actions.append(actionButton(task, 'detail', 'Details'));
      if (canCancel(task)) {
        actions.append(actionButton(task, 'cancel', task.status === 'running' ? 'Stop' : 'Cancel',
          task.status === 'running' ? 'Request cooperative cancellation of this running task?' : ''));
      }
      if (canRetry(task)) {
        actions.append(actionButton(task, 'retry', 'Retry', 'Retry this task? It will run again.'));
      }

      row.append(main, actions);
      return row;
    }

    function renderList() {
      var root = $('queue-list');
      if (!root) return;
      var status = $('queue-filter-status') ? $('queue-filter-status').value : 'all';
      var priority = $('queue-filter-priority') ? $('queue-filter-priority').value : 'any';
      var filtered = applyFilters(state.tasks, { status: status, priority: priority, limit: 50 });
      root.replaceChildren();
      var mode = emptyStateFor(state.loading && !state.tasks.length, filtered);
      if (mode === 'loading') {
        var loading = document.createElement('p');
        loading.className = 'dim';
        loading.textContent = 'Loading tasks…';
        root.append(loading);
        return;
      }
      if (mode === 'empty') {
        var empty = document.createElement('p');
        empty.className = 'dim';
        empty.textContent = state.tasks.length
          ? 'No tasks match the current filters.'
          : 'The queue is empty. Submissions from the HUD, voice, or the scheduler appear here.';
        root.append(empty);
      } else {
        for (var i = 0; i < filtered.length; i++) root.append(renderRow(filtered[i]));
      }
      var count = $('queue-count');
      if (count) count.textContent = filtered.length + ' shown · ' + state.tasks.length + ' loaded';
    }

    function field(term, value) {
      var wrap = document.createElement('div');
      wrap.className = 'queue-field';
      var label = document.createElement('span');
      label.textContent = term;
      var strong = document.createElement('strong');
      strong.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
      wrap.append(label, strong);
      return wrap;
    }

    function renderDetail() {
      var root = $('queue-detail');
      if (!root) return;
      root.replaceChildren();
      if (!state.openTaskId || !state.detail) {
        root.hidden = true;
        return;
      }
      root.hidden = false;
      var task = state.detail.task;
      var events = state.detail.events || [];
      var head = document.createElement('div');
      head.className = 'queue-detail-head';
      var title = document.createElement('strong');
      title.textContent = 'Task ' + task.id;
      var close = document.createElement('button');
      close.className = 'ghost';
      close.textContent = 'Close';
      close.onclick = function () { state.openTaskId = null; state.detail = null; renderDetail(); };
      var follow = document.createElement('button');
      follow.className = 'ghost';
      follow.textContent = 'Follow live';
      follow.disabled = !canFollow(task);
      follow.onclick = function () { onFollow(task); };
      var cancel = actionButton(task, 'cancel', 'Cancel task',
        task.status === 'running' ? 'Request cooperative cancellation of this running task?' : '');
      cancel.disabled = !canCancel(task);
      var retry = actionButton(task, 'retry', 'Retry', 'Retry this task? It will run again.');
      retry.disabled = !canRetry(task);
      head.append(title, follow, cancel, retry, close);
      root.append(head);

      var grid = document.createElement('div');
      grid.className = 'queue-fields';
      grid.append(
        field('status', statusLabel(task.status)),
        field('priority', 'P' + task.priority),
        field('origin', task.origin),
        field('tier / backend', tierLabel(task)),
        field('skill', task.skill_id),
        field('attempts', attemptLabel(task)),
        field('created', formatTimestamp(task.created_at)),
        field('started', formatTimestamp(task.started_at)),
        field('updated', formatTimestamp(task.updated_at)),
        field('finished', formatTimestamp(task.finished_at)),
        field('request id', task.request_id),
        field('dedupe key', task.dedupe_key),
        field('result', task.result_ref),
        field('error', task.error)
      );
      if (task.meta && task.meta.route_reason) grid.append(field('route reason', task.meta.route_reason));
      root.append(grid);

      var evHead = document.createElement('div');
      evHead.className = 'queue-events-title';
      evHead.textContent = 'Event history (' + events.length + ')';
      root.append(evHead);
      var list = document.createElement('div');
      list.className = 'queue-events';
      if (!events.length) {
        var empty = document.createElement('p');
        empty.className = 'dim';
        empty.textContent = 'No recorded events yet.';
        list.append(empty);
      }
      for (var i = 0; i < events.length; i++) {
        var event = events[i];
        var line = document.createElement('p');
        line.className = 'queue-event';
        var at = document.createElement('span');
        at.className = 'q-event-at';
        at.textContent = formatTimestamp(event.at);
        var kind = document.createElement('strong');
        kind.textContent = event.kind;
        var message = document.createElement('span');
        message.textContent = event.message ? ' — ' + event.message : '';
        line.append(at, kind, message);
        list.append(line);
      }
      root.append(list);
    }

    function renderAll() {
      renderSummary();
      renderList();
      renderDetail();
    }

    function schedule(delay) {
      clearTimeout(state.timer);
      var wait = delay !== undefined ? delay : nextPollDelay(state);
      if (documentRef.hidden) wait = Math.max(wait, 30000);
      state.timer = setTimeout(refresh, wait);
    }

    async function refresh() {
      if (state.loading) return;
      state.loading = true;
      renderList();
      try {
        var responses = await Promise.all([
          apiFetch(apiBase + '/tasks?limit=100'),
          apiFetch(apiBase + '/queue/summary')
        ]);
        var listRes = responses[0], sumRes = responses[1];
        var listData = await listRes.json().catch(function () { return {}; });
        var sumData = await sumRes.json().catch(function () { return {}; });
        if (!listRes.ok) throw new Error(listData.detail || listData.error || ('tasks HTTP ' + listRes.status));
        if (!sumRes.ok) throw new Error(sumData.detail || ('summary HTTP ' + sumRes.status));
        state.tasks = listData.tasks || [];
        state.summary = listData.summary || null;
        state.workers = sumData.workers || [];
        state.failures = 0;
        state.lastOk = Date.now();
        if (state.openTaskId) await loadDetail(state.openTaskId, true);
        renderAll();
      } catch (err) {
        state.failures += 1;
        setBanner('error', 'Queue refresh failed: ' + (err && err.message ? err.message : err));
        renderAll();
      } finally {
        state.loading = false;
        schedule();
      }
    }

    async function loadDetail(id, quiet) {
      try {
        var res = await apiFetch(apiBase + '/tasks/' + encodeURIComponent(id));
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        state.openTaskId = id;
        state.detail = data;
        renderDetail();
      } catch (err) {
        if (!quiet) setBanner('error', 'Could not load task ' + id.slice(0, 8) + ': ' + err.message);
      }
    }

    async function act(id, action) {
      var buttons = documentRef.querySelectorAll('[data-queue-action="' + action + '"][data-task="' + id + '"]');
      buttons.forEach(function (button) { button.disabled = true; });
      try {
        var res = await apiFetch(apiBase + '/tasks/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
        if (action === 'detail') { await loadDetail(id); return; }
        setBanner('ok', 'Task ' + id.slice(0, 8) + ': ' + (data.status || action)
          + (data.detail ? ' · ' + data.detail : ''));
      } catch (err) {
        setBanner('error', 'Task ' + id.slice(0, 8) + ' ' + action + ' failed: ' + err.message);
      } finally {
        schedule(800);
      }
    }

    async function submitTask() {
      var promptEl = $('queue-prompt');
      var prompt = promptEl ? promptEl.value.trim() : '';
      if (!prompt) return;
      if (shouldBlockSubmit(state, Date.now())) {
        setBanner('info', 'A submission was just sent — wait a moment before sending another.');
        return;
      }
      state.submitting = true;
      state.lastSubmitAt = Date.now();
      var submit = $('queue-submit');
      if (submit) submit.disabled = true;
      try {
        var body = { prompt: prompt, priority: Number($('queue-priority') ? $('queue-priority').value : 5) };
        var agent = $('queue-agent') ? $('queue-agent').value : 'auto';
        if (agent && agent !== 'auto') body.backend = agent;
        var res = await apiFetch(apiBase + '/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
        if (data.duplicate) {
          setBanner('info', 'Already queued as ' + data.task.id.slice(0, 8) + ' (duplicate suppressed).');
        } else {
          setBanner('ok', 'Queued ' + data.task.id.slice(0, 8) + ' · P' + data.task.priority);
        }
        if (promptEl) promptEl.value = '';
      } catch (err) {
        setBanner('error', 'Could not queue task: ' + err.message);
      } finally {
        state.submitting = false;
        if (submit) submit.disabled = false;
        schedule(800);
      }
    }

    documentRef.addEventListener('visibilitychange', function () {
      if (!documentRef.hidden && state.lastOk && (Date.now() - state.lastOk) > 8000) {
        refresh();
      }
    });

    var refreshButton = $('queue-refresh');
    if (refreshButton) refreshButton.onclick = function () { refresh(); };
    var submitButton = $('queue-submit');
    if (submitButton) submitButton.onclick = submitTask;
    var promptInput = $('queue-prompt');
    if (promptInput) {
      promptInput.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') submitTask();
      });
    }
    ['queue-filter-status', 'queue-filter-priority'].forEach(function (id) {
      var el = $(id);
      if (el) el.onchange = renderList;
    });

    renderAll();
    refresh();
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexQueuePanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
