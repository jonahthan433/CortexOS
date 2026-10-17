/* CortexOS · Activity search panel for the JARVIS HUD.
 *
 * Searches the merged timeline (queue tasks, task events, vault request
 * records) through /api/activity with a cursor. Rows render only what the
 * backend returned; empty and error states say exactly that. Pure helpers are
 * unit-tested with `node --test`.
 */
(function (global) {
  'use strict';

  var KIND_LABELS = { task: 'task', event: 'event', request: 'request' };

  function kindLabel(kind) { return KIND_LABELS[kind] || String(kind || 'unknown'); }

  function relativeTime(epoch, now) {
    if (epoch === null || epoch === undefined || epoch === '') return '—';
    var seconds = Math.max(0, Math.round((Number(now || Date.now()) / 1000) - Number(epoch)));
    if (isNaN(seconds)) return '—';
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.round(seconds / 3600) + 'h ago';
    return Math.round(seconds / 86400) + 'd ago';
  }

  function dedupeAppend(existing, incoming) {
    var seen = {};
    var out = [];
    (existing || []).concat(incoming || []).forEach(function (item) {
      var key = kindLabel(item.kind) + ':' + String(item.id);
      if (seen[key]) return;
      seen[key] = true;
      out.push(item);
    });
    return out;
  }

  function statusTone(status) {
    if (!status) return 'st-unknown';
    var key = String(status).replace(/_/g, '-');
    if (key === 'awaiting-approval') key = 'awaiting';
    return 'st-' + key;
  }

  function describeItem(item) {
    var parts = [];
    if (item.priority !== undefined && item.priority !== null) parts.push('P' + item.priority);
    if (item.backend) parts.push(item.backend);
    if (item.skill) parts.push(item.skill);
    if (item.detail) parts.push(item.detail);
    return parts.join(' · ');
  }

  function summarizeSources(sources) {
    if (!sources) return '';
    return 'tasks ' + (sources.task || 0) + ' · events ' + (sources.event || 0) + ' · requests ' + (sources.request || 0);
  }

  var api = { kindLabel: kindLabel, relativeTime: relativeTime, dedupeAppend: dedupeAppend,
              statusTone: statusTone, describeItem: describeItem, summarizeSources: summarizeSources };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onOpenTask = options.onOpenTask || function () {};
    var state = { items: [], cursor: null, hasMore: false, loading: false, lastOk: 0, failures: 0, debounce: null };

    function setBanner(text) {
      var banner = $('activity-banner');
      if (!banner) return;
      banner.hidden = false;
      banner.className = 'queue-banner error';
      banner.textContent = text;
      clearTimeout(setBanner.timer);
      setBanner.timer = setTimeout(function () { banner.hidden = true; }, 12000);
    }

    function updated() {
      var el = $('activity-updated');
      if (!el) return;
      if (state.failures > 0) { el.textContent = 'reconnecting · ' + state.failures + ' failed'; return; }
      if (!state.lastOk) { el.textContent = 'no data yet'; return; }
      el.textContent = 'updated ' + new Date(state.lastOk).toLocaleTimeString();
    }

    function message(text) {
      var p = document.createElement('p');
      p.className = 'dim';
      p.textContent = text;
      return p;
    }

    function fetchPage(before) {
      if (state.loading) return;
      state.loading = true;
      var query = $('activity-search');
      var kind = $('activity-kind');
      var status = $('activity-status');
      var params = '?limit=20';
      if (query && query.value.trim()) params += '&q=' + encodeURIComponent(query.value.trim());
      if (kind && kind.value) params += '&kind=' + encodeURIComponent(kind.value);
      if (status && status.value) params += '&status=' + encodeURIComponent(status.value);
      if (before) params += '&before=' + encodeURIComponent(before);
      apiFetch(apiBase + '/activity' + params).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      }).then(function (payload) {
        state.failures = 0;
        state.lastOk = Date.now();
        state.items = before ? dedupeAppend(state.items, payload.items) : (payload.items || []);
        state.cursor = payload.cursor;
        state.hasMore = !!payload.has_more;
        state.lastSources = payload.sources || {};
        render();
      }).catch(function (error) {
        state.failures += 1;
        if (!before) {
          state.items = [];
          render();
          var list = $('activity-list');
          if (list) list.replaceChildren(message('Activity could not load: ' + error.message));
        }
        setBanner('Activity request failed: ' + error.message);
      }).finally(function () {
        state.loading = false;
        updated();
      });
    }

    function render() {
      var list = $('activity-list');
      if (!list) return;
      list.replaceChildren();
      var more = $('activity-more');
      if (!state.items.length) {
        list.append(message('No activity matches this search yet.'));
        if (more) more.hidden = true;
        return;
      }
      state.items.forEach(function (item) {
        var row = document.createElement('button');
        row.type = 'button';
        row.className = 'activity-row';
        var head = document.createElement('div');
        head.className = 'activity-head';
        var when = document.createElement('span');
        when.className = 'activity-time';
        when.textContent = relativeTime(item.at, Date.now());
        when.title = item.at ? new Date(item.at * 1000).toLocaleString() : '';
        var kind = document.createElement('span');
        kind.className = 'activity-kind k-' + kindLabel(item.kind);
        kind.textContent = kindLabel(item.kind);
        var title = document.createElement('span');
        title.className = 'activity-title';
        title.textContent = item.title || item.id;
        head.append(when, kind, title);
        if (item.status) {
          var badge = document.createElement('span');
          badge.className = 'chip ' + statusTone(item.status);
          badge.textContent = item.status.replace(/_/g, ' ');
          head.append(badge);
        }
        row.append(head);
        var detail = describeItem(item);
        if (detail) {
          var sub = document.createElement('small');
          sub.className = 'activity-detail';
          sub.textContent = detail;
          row.append(sub);
        }
        row.onclick = function () {
          if (item.kind === 'task') onOpenTask({ id: item.id });
          else if (item.kind === 'event' && item.task_id) onOpenTask({ id: item.task_id });
          else if (item.kind === 'request' && window.CortexVaultPanel) { /* request records live in the vault panel */ }
        };
        list.append(row);
      });
      if (more) {
        more.hidden = !state.hasMore;
        more.textContent = state.hasMore ? 'Load more' : '';
      }
      var hint = $('activity-updated');
      if (hint) hint.title = summarizeSources(state.lastSources);
    }

    function scheduleSearch() {
      clearTimeout(state.debounce);
      state.debounce = setTimeout(function () { fetchPage(null); }, 300);
    }

    var search = $('activity-search');
    if (search) {
      search.addEventListener('input', scheduleSearch);
      search.addEventListener('keydown', function (event) { if (event.key === 'Enter') { clearTimeout(state.debounce); fetchPage(null); } });
    }
    var kind = $('activity-kind');
    if (kind) kind.onchange = function () { fetchPage(null); };
    var status = $('activity-status');
    if (status) status.onchange = function () { fetchPage(null); };
    var refresh = $('activity-refresh');
    if (refresh) refresh.onclick = function () { fetchPage(null); };
    var more = $('activity-more');
    if (more) more.onclick = function () { fetchPage(state.cursor); };

    fetchPage(null);
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexActivityPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
