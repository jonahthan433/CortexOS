/* CortexOS · Drafts panel for the JARVIS HUD.
 *
 * Drafts are local-only text until an explicit save: notes are written to the
 * vault, tasks are handed to the durable queue, and email/connector drafts
 * report honestly that execution is not wired yet. Regeneration uses the
 * configured fast model and surfaces its failure instead of inventing text.
 * Pure helpers are tested with `node --test`.
 */
(function (global) {
  'use strict';

  var VOICE_DRAFT_KEY = 'cortexos-voice-draft';
  var VOICE_DRAFT_KIND_KEY = 'cortexos-voice-draft-kind';
  var KINDS = ['note', 'task', 'email', 'connector'];
  var KIND_LABELS = {
    note: 'Note → vault',
    task: 'Task → queue',
    email: 'Email',
    connector: 'Connector action'
  };

  function kindLabel(kind) { return KIND_LABELS[kind] || String(kind || 'draft'); }

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

  function draftMeta(draft, nowSeconds) {
    return [
      kindLabel(draft.kind),
      'revision ' + (draft.revision || 1),
      draft.source ? 'from ' + draft.source : '',
      'updated ' + formatAge(draft.updated_at, nowSeconds)
    ].filter(Boolean).join(' · ');
  }

  function executionText(execution) {
    if (!execution) return '';
    return execution.available
      ? 'Can be saved: ' + (execution.reason || '')
      : 'Not wired yet: ' + (execution.reason || 'execution is unavailable in this build');
  }

  function canSave(draft) {
    return !!(draft && draft.execution && draft.execution.available && draft.status !== 'saved');
  }

  function savedLine(draft) {
    if (!draft || draft.status !== 'saved' || !draft.saved_ref) return '';
    return 'Saved · ' + draft.saved_ref;
  }

  function parseTarget(text) {
    var trimmed = String(text || '').trim();
    if (!trimmed) return { value: null };
    try {
      var parsed = JSON.parse(trimmed);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return { error: 'Target must be a JSON object, e.g. {"to": ["person@example.com"]}' };
      }
      return { value: parsed };
    } catch (error) {
      return { error: 'Target is not valid JSON: ' + error.message };
    }
  }

  function describeSave(data) {
    if (!data) return 'The save returned no result.';
    var reference = data.reference ? ' · ' + data.reference : '';
    if (data.status === 'already_saved') return 'Already saved' + reference;
    if (data.created === false) return 'A queue task for this draft already exists' + reference;
    return 'Saved' + reference;
  }

  function voiceDraftKind(stored) {
    return KINDS.indexOf(stored) !== -1 ? stored : 'note';
  }

  var api = {
    VOICE_DRAFT_KEY: VOICE_DRAFT_KEY,
    VOICE_DRAFT_KIND_KEY: VOICE_DRAFT_KIND_KEY,
    KINDS: KINDS,
    KIND_LABELS: KIND_LABELS,
    kindLabel: kindLabel,
    formatAge: formatAge,
    draftMeta: draftMeta,
    executionText: executionText,
    canSave: canSave,
    savedLine: savedLine,
    parseTarget: parseTarget,
    describeSave: describeSave,
    voiceDraftKind: voiceDraftKind,
    refresh: function () {}
  };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onQueued = options.onQueued || function () {};
    var root = $('drafts-list');
    if (!root) return;
    var state = { drafts: [], openId: null, note: '', error: '', loading: false };

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
    function setError(text) { state.error = text; render(); }

    function reload() {
      state.loading = true;
      request('GET', '/drafts').then(function (drafts) {
        state.drafts = drafts;
        state.loading = false;
        state.error = '';
        render();
        if (state.openId) refreshEditor(state.openId, true);
      }).catch(function (error) {
        state.loading = false;
        setError('Could not load drafts: ' + error.message);
      });
    }

    function refreshEditor(id, quiet) {
      request('GET', '/drafts/' + encodeURIComponent(id)).then(function (draft) {
        state.openId = id;
        renderEditor(draft);
      }).catch(function (error) {
        if (!quiet) setError('Could not open the draft: ' + error.message);
        else render();
      });
    }

    function create() {
      var kind = $('draft-kind').value;
      var title = $('draft-title').value.trim();
      var body = $('draft-body').value.trim();
      if (!body) { setNote('A draft needs some text before it can be created.'); return; }
      request('POST', '/drafts', { kind: kind, title: title || null, body: body }).then(function (draft) {
        $('draft-title').value = '';
        $('draft-body').value = '';
        setNote('Draft created. Nothing was sent or executed.');
        state.openId = draft.id;
        reload();
        renderEditor(draft);
      }).catch(function (error) { setNote('Could not create the draft: ' + error.message); });
    }

    function saveEdits(id, getValues) {
      var values = getValues();
      var parsed = parseTarget(values.target);
      if (parsed.error) { setNote(parsed.error); return; }
      var payload = { title: values.title || null, body: values.body || null };
      if (values.target.trim()) payload.target = parsed.value;
      request('PUT', '/drafts/' + encodeURIComponent(id), payload).then(function (draft) {
        setNote('Draft updated to revision ' + draft.revision + '.');
        renderEditor(draft);
        reload();
      }).catch(function (error) { setNote('Could not save the edits: ' + error.message); });
    }

    function regenerate(id) {
      setNote('Asking the fast model to rewrite this draft…');
      request('POST', '/drafts/' + encodeURIComponent(id) + '/regenerate').then(function (draft) {
        setNote('Regenerated (revision ' + draft.revision + '). Review before saving.');
        renderEditor(draft);
        reload();
      }).catch(function (error) { setNote('Regeneration failed honestly: ' + error.message); });
    }

    function save(id) {
      request('POST', '/drafts/' + encodeURIComponent(id) + '/save').then(function (data) {
        setNote(describeSave(data));
        if (data.task) onQueued(data.task);
        reload();
      }).catch(function (error) { setNote('Could not save: ' + error.message); });
    }

    function discard(id) {
      if (!window.confirm('Discard this draft? Its text will be deleted.')) return;
      request('DELETE', '/drafts/' + encodeURIComponent(id)).then(function () {
        if (state.openId === id) { state.openId = null; $('draft-editor').replaceChildren(); }
        setNote('Draft discarded.');
        reload();
      }).catch(function (error) { setNote('Could not discard the draft: ' + error.message); });
    }

    function renderEditor(draft) {
      var editor = $('draft-editor');
      editor.replaceChildren();
      state.openId = draft.id;

      var head = document.createElement('div');
      head.className = 'draft-editor-head';
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = kindLabel(draft.kind);
      var id = document.createElement('small');
      id.className = 'dim';
      id.textContent = draft.id;
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'ghost';
      close.textContent = 'Close';
      close.onclick = function () { state.openId = null; editor.replaceChildren(); };
      head.append(chip, id, close);
      editor.append(head);

      var titleInput = document.createElement('input');
      titleInput.type = 'text';
      titleInput.value = draft.title || '';
      titleInput.placeholder = 'Draft title';
      titleInput.maxLength = 200;
      var bodyArea = document.createElement('textarea');
      bodyArea.className = 'draft-body';
      bodyArea.rows = 8;
      bodyArea.value = draft.body || '';
      bodyArea.placeholder = 'Draft text';
      editor.append(titleInput, bodyArea);

      var targetInput = document.createElement('input');
      targetInput.type = 'text';
      targetInput.value = draft.target ? JSON.stringify(draft.target) : '';
      targetInput.placeholder = 'target (JSON, optional — email needs {"to": [...]})';
      editor.append(targetInput);

      if (draft.transcript) {
        var transcript = document.createElement('small');
        transcript.className = 'dim';
        transcript.textContent = 'Preserved transcript: ' + draft.transcript.slice(0, 300);
        editor.append(transcript);
      }
      var execution = document.createElement('small');
      execution.className = 'dim';
      execution.textContent = [executionText(draft.execution), savedLine(draft)].filter(Boolean).join(' · ');
      editor.append(execution);

      var actions = document.createElement('div');
      actions.className = 'draft-actions';
      var editButton = document.createElement('button');
      editButton.className = 'ghost';
      editButton.textContent = 'Save edits';
      editButton.onclick = function () {
        saveEdits(draft.id, function () {
          return { title: titleInput.value.trim(), body: bodyArea.value.trim(), target: targetInput.value };
        });
      };
      var regenButton = document.createElement('button');
      regenButton.className = 'ghost';
      regenButton.textContent = 'Regenerate';
      regenButton.title = 'Rewrite with the configured fast model; nothing is sent anywhere.';
      regenButton.onclick = function () { regenerate(draft.id); };
      var saveButton = document.createElement('button');
      saveButton.className = 'run';
      saveButton.textContent = draft.kind === 'note' ? 'Save to vault'
        : draft.kind === 'task' ? 'Create queue task' : 'Save (not wired)';
      saveButton.disabled = !canSave(draft);
      saveButton.title = saveButton.disabled
        ? (draft.status === 'saved' ? 'Already saved' : executionText(draft.execution))
        : 'Runs the existing guarded save path';
      saveButton.onclick = function () { save(draft.id); };
      var discardButton = document.createElement('button');
      discardButton.className = 'ghost';
      discardButton.textContent = 'Discard';
      discardButton.onclick = function () { discard(draft.id); };
      actions.append(editButton, regenButton, saveButton, discardButton);
      editor.append(actions);
    }

    function renderRow(draft) {
      var row = document.createElement('div');
      row.className = 'draft-row';
      row.dataset.draftId = draft.id;
      var main = document.createElement('button');
      main.className = 'draft-row-main';
      main.onclick = function () { refreshEditor(draft.id); };
      var title = document.createElement('strong');
      title.textContent = draft.title || '(untitled draft)';
      var meta = document.createElement('small');
      meta.className = 'dim';
      meta.textContent = draftMeta(draft, Date.now() / 1000)
        + (draft.status === 'saved' ? ' · saved' : '')
        + (draft.has_transcript ? ' · has transcript' : '');
      main.append(title, meta);
      var open = document.createElement('button');
      open.className = 'ghost';
      open.textContent = 'Open';
      open.onclick = function (event) { event.stopPropagation(); refreshEditor(draft.id); };
      var remove = document.createElement('button');
      remove.className = 'ghost';
      remove.textContent = 'Discard';
      remove.onclick = function (event) { event.stopPropagation(); discard(draft.id); };
      row.append(main, open, remove);
      return row;
    }

    function render() {
      var note = $('drafts-note');
      if (note) { note.hidden = !state.note; note.textContent = state.note; }
      root.replaceChildren();
      if (state.error) {
        var failure = document.createElement('p');
        failure.className = 'dim';
        failure.textContent = state.error;
        root.append(failure);
        return;
      }
      if (state.loading && !state.drafts.length) {
        var loading = document.createElement('p');
        loading.className = 'dim';
        loading.textContent = 'Loading drafts…';
        root.append(loading);
        return;
      }
      if (!state.drafts.length) {
        var empty = document.createElement('p');
        empty.className = 'dim';
        empty.textContent = 'No drafts. Drafts are local text only — nothing is sent or executed until you save one and confirm.';
        root.append(empty);
        return;
      }
      state.drafts.forEach(function (draft) { root.append(renderRow(draft)); });
    }

    var createButton = $('draft-create');
    if (createButton) createButton.onclick = create;
    var refreshButton = $('drafts-refresh');
    if (refreshButton) refreshButton.onclick = reload;

    var toggle = $('draft-voice-toggle');
    var kindSelect = $('draft-voice-kind');
    if (toggle) {
      toggle.checked = window.localStorage.getItem(VOICE_DRAFT_KEY) === '1';
      toggle.onchange = function () {
        window.localStorage.setItem(VOICE_DRAFT_KEY, toggle.checked ? '1' : '0');
      };
    }
    if (kindSelect) {
      kindSelect.value = voiceDraftKind(window.localStorage.getItem(VOICE_DRAFT_KIND_KEY));
      kindSelect.onchange = function () {
        window.localStorage.setItem(VOICE_DRAFT_KIND_KEY, voiceDraftKind(kindSelect.value));
      };
    }

    reload();
    api.refresh = reload;
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexDraftsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
