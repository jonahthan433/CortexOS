/* Login page for the dashboard: password sign-in with a cookie session.
   Pure helpers are exported for node --test; the controller wires the DOM. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CortexAuthPanel = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function modeFor(status) {
    return status && status.has_password ? 'login' : 'setup';
  }

  function validate(password, confirm, mode) {
    if (!password) return 'Enter a password.';
    if (password.length < 8) return 'Password must be at least 8 characters.';
    if (mode === 'setup' && password !== confirm) return 'Passwords do not match.';
    return null;
  }

  function errorText(status, detail) {
    if (status === 401) return 'Wrong password.';
    if (status === 400) return detail || 'Password must be at least 8 characters.';
    if (status === 409) return 'A password is already set — reload the page to sign in.';
    if (status === 429) return detail || 'Too many failed attempts. Wait a few minutes and try again.';
    return detail || 'Sign-in failed.';
  }

  function controller(opts) {
    var doc = opts.document || document;
    var view = doc.querySelector(opts.view);
    if (!view) return null;
    var title = view.querySelector(opts.title);
    var hint = view.querySelector(opts.hint);
    var form = view.querySelector(opts.form);
    var password = view.querySelector(opts.password);
    var confirmRow = view.querySelector(opts.confirmRow);
    var confirm = view.querySelector(opts.confirm);
    var error = view.querySelector(opts.error);
    var submit = view.querySelector(opts.submit);
    var mode = 'login';

    function showError(text) {
      error.textContent = text;
      error.hidden = false;
    }

    function open(hasPassword) {
      mode = modeFor({ has_password: hasPassword });
      title.textContent = mode === 'setup' ? 'Create a password' : 'Sign in';
      hint.textContent = mode === 'setup'
        ? 'No password is set yet. The first sign-in claims this dashboard; choose one only you know.'
        : 'Enter your CortexOS password to open the dashboard.';
      confirmRow.hidden = mode !== 'setup';
      password.setAttribute('autocomplete', mode === 'setup' ? 'new-password' : 'current-password');
      submit.textContent = mode === 'setup' ? 'Create password' : 'Sign in';
      error.hidden = true;
      password.value = '';
      confirm.value = '';
      view.hidden = false;
      try { password.focus(); } catch (e) {}
    }

    function close() {
      view.hidden = true;
    }

    async function onSubmit(event) {
      event.preventDefault();
      var problem = validate(password.value, confirm.value, mode);
      if (problem) { showError(problem); return; }
      submit.disabled = true;
      try {
        var res = await (opts.fetch || window.fetch)(opts.api + (mode === 'setup' ? '/auth/setup' : '/auth/login'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: password.value })
        });
        var data = {};
        try { data = await res.json(); } catch (e) {}
        if (!res.ok) { showError(errorText(res.status, data && data.detail)); return; }
        password.value = '';
        confirm.value = '';
        close();
        if (opts.onAuthenticated) opts.onAuthenticated();
      } catch (e) {
        showError('Could not reach the Bridge. Check that the service is running.');
      } finally {
        submit.disabled = false;
      }
    }

    form.addEventListener('submit', onSubmit);
    return { open: open, close: close };
  }

  return { modeFor: modeFor, validate: validate, errorText: errorText, controller: controller };
});
