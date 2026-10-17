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

  function versionLine(skill) {
    if (!skill) return '';
    var hash = skill.content_hash ? String(skill.content_hash).slice(0, 12) + '…' : 'no hash';
    return 'v' + (skill.version || '?') + ' · ' + (skill.version_source || 'unknown source') + ' · ' + hash;
  }

  function problemLines(problems) {
    return (problems || []).map(function (problem) { return String(problem); });
  }

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

  function historyRows(history) {
    if (!history || !history.entries) return [];
    return history.entries.map(function (entry) {
      return {
        hash: entry.hash,
        hash8: String(entry.hash || '').slice(0, 8),
        version: entry.version || '?',
        source: entry.source || 'unknown',
        at: entry.at,
        current: !!entry.current,
        rolled_back_from: entry.rolled_back_from || null,
        rollbackAllowed: !entry.current
      };
    });
  }

  function testSummary(result) {
    if (!result) return '';
    return result.passed + '/' + result.total + ' passed · ' + result.status
      + (result.receipt ? ' · receipt recorded' : '');
  }

  function testCaseLines(result) {
    if (!result || !result.cases) return [];
    return result.cases.map(function (case_) {
      var line = (case_.passed ? '✓ ' : '✗ ') + case_.name;
      if (case_.mismatches && case_.mismatches.length) line += ' — ' + case_.mismatches.join('; ');
      return line;
    });
  }

  var api = {
    riskText: riskText, promotionText: promotionText, statsRows: statsRows, detailSections: detailSections,
    versionLine: versionLine, problemLines: problemLines, historyRows: historyRows,
    testSummary: testSummary, testCaseLines: testCaseLines, formatAge: formatAge
  };

  /* ------------------------------------------------------------------ DOM */

  function $(id) { return document.getElementById(id); }

  function init(options) {
    var apiBase = options.api;
    var apiFetch = options.apiFetch;
    var onRun = options.onRun || function () {};
    var onChanged = options.onChanged || function () {};
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
      drawer.append(row('version', versionLine(skill)));

      if (skill.problems && skill.problems.length) {
        var problemLabel = document.createElement('div');
        problemLabel.className = 'skill-key';
        problemLabel.textContent = 'validation problems';
        drawer.append(problemLabel);
        problemLines(skill.problems).forEach(function (problem) {
          var line = document.createElement('small');
          line.className = 'dim skill-problem';
          line.textContent = problem;
          drawer.append(line);
        });
      }

      if (skill.tests && skill.tests.length) {
        var testsLabel = document.createElement('div');
        testsLabel.className = 'skill-key';
        testsLabel.textContent = 'declared tests';
        drawer.append(testsLabel);
        skill.tests.forEach(function (case_) {
          var line = document.createElement('small');
          line.className = 'dim';
          line.textContent = case_.name + ' — ' + String(case_.prompt || '').slice(0, 120);
          drawer.append(line);
        });
        var testButton = document.createElement('button');
        testButton.type = 'button';
        testButton.className = 'ghost';
        testButton.textContent = 'Run tests';
        testButton.title = 'Deterministic routing and gate checks only; records a skill_test receipt. No providers, no side effects.';
        var testResult = document.createElement('div');
        testResult.className = 'skill-test-result';
        testButton.onclick = function () { runSkillTests(skill.id, testResult, testButton); };
        drawer.append(testButton, testResult);
      } else {
        var noTests = document.createElement('small');
        noTests.className = 'dim';
        noTests.textContent = 'No tests declared — add a tests section to the manifest to make this skill testable.';
        drawer.append(noTests);
      }

      var historyLabel = document.createElement('div');
      historyLabel.className = 'skill-key';
      historyLabel.textContent = 'history';
      var historyButton = document.createElement('button');
      historyButton.type = 'button';
      historyButton.className = 'ghost';
      historyButton.textContent = 'Show history';
      var historyList = document.createElement('div');
      historyList.className = 'skill-history';
      historyList.hidden = true;
      historyButton.onclick = function () { toggleHistory(skill.id, historyList, historyButton); };
      drawer.append(historyLabel, historyButton, historyList);

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

    function runSkillTests(skillId, container, button) {
      button.disabled = true;
      container.replaceChildren();
      var pending = document.createElement('small');
      pending.className = 'dim';
      pending.textContent = 'Running declared tests (routing and gate checks only)…';
      container.append(pending);
      apiFetch(apiBase + '/skills/' + encodeURIComponent(skillId) + '/test', { method: 'POST' })
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            return data;
          });
        })
        .then(function (result) {
          container.replaceChildren();
          var summary = document.createElement('small');
          summary.className = 'dim';
          summary.textContent = testSummary(result);
          container.append(summary);
          testCaseLines(result).forEach(function (line) {
            var item = document.createElement('small');
            item.className = 'dim';
            item.textContent = line;
            container.append(item);
          });
          onChanged();
        })
        .catch(function (error) {
          container.replaceChildren();
          var failure = document.createElement('small');
          failure.className = 'dim';
          failure.textContent = 'Tests could not run: ' + error.message;
          container.append(failure);
        })
        .finally(function () { button.disabled = false; });
    }

    function historyRow(skillId, entry) {
      var wrap = document.createElement('div');
      wrap.className = 'skill-history-row';
      var label = document.createElement('span');
      label.className = 'skill-history-label';
      label.textContent = 'v' + entry.version + ' · ' + entry.hash8 + '… · ' + entry.source
        + ' · ' + formatAge(entry.at, Date.now() / 1000);
      wrap.append(label);
      if (entry.current) {
        var current = document.createElement('span');
        current.className = 'chip badge-ok';
        current.textContent = 'current';
        wrap.append(current);
      } else {
        var rollback = document.createElement('button');
        rollback.type = 'button';
        rollback.className = 'ghost';
        rollback.textContent = 'Rollback';
        rollback.onclick = function () {
          if (!window.confirm('Roll back to v' + entry.version + ' (' + entry.hash8 + '…)? '
            + 'The current version stays in history.')) return;
          apiFetch(apiBase + '/skills/' + encodeURIComponent(skillId) + '/rollback', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hash: entry.hash, confirm: true })
          }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
              if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
              return data;
            });
          }).then(function (data) {
            onChanged();
            open(skillId);
          }).catch(function (error) {
            label.textContent += ' — rollback failed: ' + error.message;
          });
        };
        wrap.append(rollback);
      }
      return wrap;
    }

    function toggleHistory(skillId, container, button) {
      if (container.dataset.loaded === '1') {
        container.hidden = !container.hidden;
        button.textContent = container.hidden ? 'Show history' : 'Hide history';
        return;
      }
      button.disabled = true;
      container.replaceChildren();
      var pending = document.createElement('small');
      pending.className = 'dim';
      pending.textContent = 'Loading history…';
      container.append(pending);
      apiFetch(apiBase + '/skills/' + encodeURIComponent(skillId) + '/history')
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            return data;
          });
        })
        .then(function (history) {
          container.replaceChildren();
          var rows = historyRows(history);
          if (!rows.length) {
            var empty = document.createElement('small');
            empty.className = 'dim';
            empty.textContent = 'No recorded versions yet; the current version becomes the baseline on next sync.';
            container.append(empty);
          }
          rows.forEach(function (entry) { container.append(historyRow(skillId, entry)); });
          container.dataset.loaded = '1';
          container.hidden = false;
          button.textContent = 'Hide history';
        })
        .catch(function (error) {
          container.replaceChildren();
          var failure = document.createElement('small');
          failure.className = 'dim';
          failure.textContent = 'Could not load history: ' + error.message;
          container.append(failure);
        })
        .finally(function () { button.disabled = false; });
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
