/* CortexOS · Vault browser panel for the JARVIS HUD.
 *
 * Read-only by design: it only calls the jailed /api/vault/* endpoints. Every
 * rendered value comes from the server response; failures show the server's
 * message instead of fabricated content. Pure helpers are unit-tested with
 * `node --test`; the controller wires them to the DOM.
 */
(function (global) {
  'use strict';

  function formatBytes(size) {
    var n = Number(size);
    if (size === null || size === undefined || size === '' || isNaN(n)) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function joinPath(base, name) {
    var clean = String(base || '').replace(/^\.\/?/, '').replace(/\/+$/, '');
    if (!clean || clean === '.') return name;
    return clean + '/' + name;
  }

  function parentPath(rel) {
    var clean = String(rel || '').replace(/\/+$/, '');
    if (!clean || clean === '.') return '';
    var index = clean.lastIndexOf('/');
    return index === -1 ? '' : clean.slice(0, index);
  }

  function breadcrumbs(rel) {
    var trail = [{ label: 'vault', path: '' }];
    var parts = String(rel || '').replace(/^\.\/?/, '').split('/').filter(Boolean);
    var current = '';
    for (var i = 0; i < parts.length; i++) {
      current = current ? current + '/' + parts[i] : parts[i];
      trail.push({ label: parts[i], path: current });
    }
    return trail;
  }

  function sortEntries(entries) {
    return (entries || []).slice().sort(function (a, b) {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
      return String(a.name).localeCompare(String(b.name));
    });
  }

  function previewMode(file) {
    if (!file) return 'none';
    if (file.binary) return 'binary';
    return 'text';
  }

  function fileMeta(file) {
    if (!file) return '';
    var parts = [formatBytes(file.size)];
    if (file.modified) parts.push(new Date(file.modified * 1000).toLocaleString());
    if (file.truncated) parts.push('preview truncated at 200 KB');
    if (file.binary) parts.push('binary');
    return parts.join(' · ');
  }

  var api = { formatBytes: formatBytes, joinPath: joinPath, parentPath: parentPath, breadcrumbs: breadcrumbs,
              sortEntries: sortEntries, previewMode: previewMode, fileMeta: fileMeta };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var state = { path: '', selected: null, searching: false };

    function setBanner(text) {
      var banner = $('vault-banner');
      if (!banner) return;
      banner.hidden = false;
      banner.className = 'queue-banner error';
      banner.textContent = text;
      clearTimeout(setBanner.timer);
      setBanner.timer = setTimeout(function () { banner.hidden = true; }, 12000);
    }

    function updated(text) {
      var el = $('vault-updated');
      if (el) el.textContent = text;
    }

    function message(text, className) {
      var p = document.createElement('p');
      p.className = className || 'dim';
      p.textContent = text;
      return p;
    }

    function get(path) {
      return apiFetch(apiBase + path).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      });
    }

    function renderBreadcrumbs() {
      var root = $('vault-breadcrumbs');
      if (!root) return;
      root.replaceChildren();
      var trail = breadcrumbs(state.path);
      trail.forEach(function (crumb, index) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'crumb' + (index === trail.length - 1 ? ' current' : '');
        button.textContent = crumb.label;
        button.onclick = function () { loadTree(crumb.path); };
        root.append(button);
      });
    }

    function renderEntries(payload) {
      var list = $('vault-list');
      if (!list) return;
      list.replaceChildren();
      var entries = sortEntries(payload.entries);
      if (!entries.length) {
        list.append(message('This folder is empty.'));
        return;
      }
      entries.forEach(function (entry) {
        var row = document.createElement('button');
        row.type = 'button';
        row.className = 'vault-row' + (entry.path === state.selected ? ' selected' : '');
        var name = document.createElement('span');
        name.className = 'vault-name';
        name.textContent = (entry.type === 'dir' ? '🗀 ' : '🗋 ') + entry.name;
        var meta = document.createElement('span');
        meta.className = 'vault-meta';
        meta.textContent = entry.type === 'dir' ? 'folder' : formatBytes(entry.size);
        row.append(name, meta);
        row.onclick = function () {
          if (entry.type === 'dir') loadTree(entry.path);
          else openFile(entry.path);
        };
        list.append(row);
      });
      if (payload.truncated) list.append(message('Listing truncated; open a subfolder to see the rest.'));
    }

    function loadTree(path) {
      state.path = path || '';
      state.searching = false;
      var clear = $('vault-search-clear');
      if (clear) clear.hidden = true;
      var results = $('vault-search-results');
      if (results) { results.hidden = true; results.replaceChildren(); }
      var list = $('vault-list');
      var viewer = $('vault-viewer');
      if (list) { list.hidden = false; list.replaceChildren(message('Loading folder…')); }
      if (viewer) viewer.hidden = true;
      renderBreadcrumbs();
      get('/vault/tree?path=' + encodeURIComponent(state.path)).then(function (payload) {
        renderEntries(payload);
        updated('updated ' + new Date().toLocaleTimeString());
      }).catch(function (error) {
        if (list) list.replaceChildren(message('Could not load this folder: ' + error.message));
        updated('load failed');
      });
    }

    function openFile(rel) {
      state.selected = rel;
      var viewer = $('vault-viewer');
      var title = $('vault-viewer-title');
      var meta = $('vault-viewer-meta');
      var body = $('vault-viewer-body');
      if (!viewer || !body) return;
      viewer.hidden = false;
      if (title) title.textContent = rel;
      if (meta) meta.textContent = '';
      body.textContent = 'Loading…';
      get('/vault/file?path=' + encodeURIComponent(rel)).then(function (file) {
        if (meta) meta.textContent = fileMeta(file);
        var mode = previewMode(file);
        if (mode === 'binary') body.textContent = 'Binary file — not previewed here.';
        else body.textContent = file.text || '';
        body.scrollTop = 0;
        var list = $('vault-list');
        if (list && !state.searching) {
          Array.prototype.forEach.call(list.querySelectorAll('.vault-row'), function (row) {
            var name = row.querySelector('.vault-name');
            row.classList.toggle('selected', !!name && name.textContent.endsWith(rel.split('/').pop()));
          });
        }
      }).catch(function (error) {
        body.textContent = 'Could not open this file: ' + error.message;
      });
    }

    function clearSearch() {
      var input = $('vault-search');
      if (input) input.value = '';
      loadTree(state.path);
    }

    function runSearch() {
      var input = $('vault-search');
      var query = input ? input.value.trim() : '';
      if (!query) { clearSearch(); return; }
      var output = $('vault-search-results');
      var list = $('vault-list');
      if (!output) return;
      state.searching = true;
      output.hidden = false;
      output.replaceChildren(message('Searching…'));
      if (list) list.hidden = true;
      var clear = $('vault-search-clear');
      if (clear) clear.hidden = false;
      get('/vault/search?q=' + encodeURIComponent(query)).then(function (payload) {
        output.replaceChildren();
        if (!payload.results.length) {
          output.append(message('No matches for “' + query + '”.'));
        } else {
          payload.results.forEach(function (result) {
            var row = document.createElement('button');
            row.type = 'button';
            row.className = 'vault-row search';
            var name = document.createElement('span');
            name.className = 'vault-name';
            name.textContent = result.path;
            var meta = document.createElement('span');
            meta.className = 'vault-meta';
            meta.textContent = result.where === 'name' ? 'name match' : ('line ' + result.line);
            row.append(name, meta);
            if (result.snippet) {
              var snippet = document.createElement('small');
              snippet.className = 'vault-snippet';
              snippet.textContent = result.snippet;
              row.append(snippet);
            }
            row.onclick = function () { openFile(result.path); };
            output.append(row);
          });
          output.append(message(payload.truncated
            ? 'Showing the first matches; refine the query to narrow results.'
            : 'All matches shown · ' + payload.scanned_files + ' files scanned.'));
        }
        updated('searched ' + new Date().toLocaleTimeString());
      }).catch(function (error) {
        output.replaceChildren(message('Search failed: ' + error.message));
      });
    }

    var refresh = $('vault-refresh');
    if (refresh) refresh.onclick = function () { loadTree(state.path); };
    var run = $('vault-search-run');
    if (run) run.onclick = runSearch;
    var clear = $('vault-search-clear');
    if (clear) clear.onclick = clearSearch;
    var input = $('vault-search');
    if (input) input.addEventListener('keydown', function (event) { if (event.key === 'Enter') runSearch(); });
    var close = $('vault-viewer-close');
    if (close) close.onclick = function () { var viewer = $('vault-viewer'); if (viewer) viewer.hidden = true; };

    loadTree('');
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexVaultPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
