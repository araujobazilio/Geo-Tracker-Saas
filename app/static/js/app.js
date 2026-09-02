/*
 * GEO Tracker — Application JavaScript
 *
 * Configures HTMX to send the CSRF token on every state-changing request.
 * The CSRF token is read from the <meta name="csrf-token"> tag in the
 * base template, which is populated server-side from the session.
 */

(function () {
  'use strict';

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  // HTMX: attach CSRF token to every request that needs it.
  document.addEventListener('htmx:configRequest', function (event) {
    var method = event.detail.verb.toUpperCase();
    if (method === 'POST' || method === 'PUT' || method === 'PATCH' || method === 'DELETE') {
      event.detail.headers['X-CSRF-Token'] = getCsrfToken();
    }
  });

  // Also configure fetch for any non-HTMX mutations.
  var originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || 'GET').toUpperCase();
    if (method === 'POST' || method === 'PUT' || method === 'PATCH' || method === 'DELETE') {
      init.headers = init.headers || {};
      if (!init.headers['X-CSRF-Token']) {
        init.headers['X-CSRF-Token'] = getCsrfToken();
      }
    }
    return originalFetch.call(this, input, init);
  };
})();
