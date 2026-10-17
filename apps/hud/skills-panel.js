/* CortexOS · Skill detail drawer for the JARVIS HUD.
 *
 * When the hub renders a skill card (hud.js tags it with data-skill-id),
 * clicking the card opens a server-fetched detail view: manifest fields,
 * stats, requirements, and the instruction text. Running still goes through
 * the exact same submit + approval gate as the existing Run button — this
 * module never executes skill content on its own. Pure helpers are tested
 * with `node --test`.
 */
(function (global) {
  'use strict';

  function riskText(skill) {
    if (!skill) return '';
    return (skill.risk_level || 'unknown') + (skill.approval_required ? ' · approval required each run' : ' · no external gate');
  }

  function promotionText(skill) {
    if (!skill) return '';
    var status = skill.promotion_status || 'not_started';
    var labels = {
      not_started: 'automation review not started',
      pending_review: 'awaiting human promotion review',
      approved: 'automation approved'
    };
    var text = labels[status] || status;
    var runs = skill.successful_runs || 0;
    if (status !== 'approved') text += ' · ' + runs + '/5 confirmed runs';
    return text;
  }

  function statsRows(skill) {
    if (!skill) return [];
    var rows = [];
    if (skill.successful_runs !== undefined) rows.push(['successful runs', String(skill.successful_runs)]);
    if (skill.error_runs !== undefined) rows.push(['runs needing improvement', String(skill.error_runs)]);
    if (skill.tier !== undefined) rows.push(['tier', String(skill.tier)]);
    return rows;
  }

  function detailSections(skill) {
    if (!skill) return [];
    var sections = [
      ['description', skill.description || '—'],
      ['risk', riskText(skill)],
      ['automation', promotionText(skill)]
    ];
    if (skill.triggers && skill.triggers.length) sections.push(['triggers', skill.triggers.join(', ')]);
    if (skill.domain) sections.push(['domain', skill.domain]);
    if (skill.files && skill.files.length) sections.push(['files', skill.files.join(', ')]);
    return sections;
  }

  var api = { riskText: riskText, promotionText: promotionText, statsRows: statsRows, detailSections: detailSections };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onRun = options.onRun || function () {};
    var hub = $('skills');
    if (!hub) return;

    var drawer = document.createElement('div');
    drawer.id = 'skill-detail';
    drawer.hidden = true;
    document.body.append(drawer);

    function close() { drawer.hidden = true; drawer.replaceChildren(); }

    function row(label, value) {
      var wrap = document.createElement('div');
      wrap.className = 'skill-row';
      var key = document.createElement('span');
      key.className = 'skill-key';
      key.textContent = label;
      var val = document.createElement('span');
      val.className = 'skill-value';
      val.textContent = value;
      wrap.append(key, val);
      return wrap;
    }

    function render(skill) {
      drawer.replaceChildren();
      var head = document.createElement('div');
      head.className = 'skill-detail-head';
      var title = document.createElement('strong');
      title.textContent = skill.name || skill.id;
      var id = document.createElement('small');
      id.className = 'dim';
      id.textContent = skill.id;
      var closeButton = document.createElement('button');
      closeButton.type = 'button';
      closeButton.className = 'ghost';
      closeButton.textContent = 'Close';
      closeButton.onclick = close;
      head.append(title, id, closeButton);
      drawer.append(head);

      detailSections(skill).forEach(function (pair) { drawer.append(row(pair[0], pair[1])); });
      statsRows(skill).forEach(function (pair) { drawer.append(row(pair[0], pair[1])); });

      if (skill.instructions) {
        var label = document.createElement('div');
        label.className = 'skill-key';
        label.textContent = 'instructions';
        var pre = document.createElement('pre');
        pre.className = 'skill-instructions';
        pre.textContent = skill.instructions;
        drawer.append(label, pre);
      }

      var actions = document.createElement('div');
      actions.className = 'skill-actions';
      var run = document.createElement('button');
      run.type = 'button';
      run.className = 'run';
      run.textContent = 'Run skill';
      run.title = 'Runs through the normal submit flow; approval rules still apply.';
      run.onclick = function () { close(); onRun(skill.id); };
      actions.append(run);
      var note = document.createElement('small');
      note.className = 'dim';
      note.textContent = skill.approval_required
        ? 'This skill requires fresh human approval before any external action.'
        : 'Read-only skills run directly; external actions still require approval.';
      actions.append(note);
      drawer.append(actions);
    }

    function open(skillId) {
      drawer.hidden = false;
      drawer.replaceChildren();
      var loading = document.createElement('p');
      loading.className = 'dim';
      loading.textContent = 'Loading skill ' + skillId + '…';
      drawer.append(loading);
      apiFetch(apiBase + '/skills/' + encodeURIComponent(skillId)).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
          return data;
        });
      }).then(render).catch(function (error) {
        drawer.replaceChildren();
        var failure = document.createElement('p');
        failure.className = 'dim';
        failure.textContent = 'Could not load this skill: ' + error.message;
        var closeButton = document.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'ghost';
        closeButton.textContent = 'Close';
        closeButton.onclick = close;
        drawer.append(failure, closeButton);
      });
    }

    hub.addEventListener('click', function (event) {
      if (event.target.closest('button')) return;  // Run/Promote keep their own behavior
      var card = event.target.closest('.card.skill[data-skill-id]');
      if (!card) return;
      open(card.dataset.skillId);
    });
    hub.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      var card = event.target.closest('.card.skill[data-skill-id]');
      if (card) open(card.dataset.skillId);
    });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') close(); });
  }

  api.init = init;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.CortexSkillsPanel = api;
  }
})(typeof self !== 'undefined' ? self : this);
