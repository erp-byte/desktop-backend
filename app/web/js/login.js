// ── Login page (browser port) ──
//
// Talks to the real server_replica auth API:
//   POST /api/v1/auth/login   { phone, password } → { access_token, refresh_token, user, ... }
//   GET  /api/v1/auth/me      (Bearer)            → full { permissions, roles, is_admin, ... }
//
// The /login response carries only a shallow `user`; the wizard's
// requireAuth('purchase') gate needs the full permission set, so we fetch
// /me right after and persist that as the `me` snapshot via CandorAuth
// (the same store web-shims.js reads on every page load).

(function () {
  const ORIGIN = window.CandorAuth.origin;
  const form = document.getElementById('loginForm');
  const btn = document.getElementById('loginBtn');

  // Already signed in? Skip straight to the wizard.
  if (window.CandorAuth.accessToken() && window.CandorAuth.me()) {
    window.location.replace('vendor-new.html');
    return;
  }

  function errMessage(body, status) {
    // server_replica surfaces auth failures via its structured envelope;
    // fall back to a generic message when the shape is unfamiliar.
    if (body && typeof body === 'object') {
      if (typeof body.message === 'string') return body.message;
      if (body.detail && typeof body.detail === 'object' && body.detail.msg) return body.detail.msg;
      if (typeof body.detail === 'string') return body.detail;
    }
    return `Sign in failed (HTTP ${status}).`;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const phone = document.getElementById('phone').value.trim();
    const password = document.getElementById('password').value;
    if (!phone || !password) { showToast('Enter your phone and password.', 'warning'); return; }

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Signing in…';

    try {
      const res = await fetch(`${ORIGIN}/api/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, password }),
      });
      let body = null;
      try { body = await res.json(); } catch (_) { /* non-JSON */ }
      if (!res.ok) { showToast(errMessage(body, res.status), 'error'); return; }

      const accessToken = body.access_token;
      const refreshToken = body.refresh_token;

      // Pull the full /me snapshot (permissions/roles) so requireAuth works.
      let me = null;
      try {
        const meRes = await fetch(`${ORIGIN}/api/v1/auth/me`, {
          headers: { 'Authorization': `Bearer ${accessToken}` },
        });
        if (meRes.ok) me = await meRes.json();
      } catch (_) { /* fall through — save shallow user below */ }

      // If /me was unavailable, synthesize a minimal snapshot from `user`
      // so isLoggedIn() is at least true (module gate may still deny).
      if (!me && body.user) {
        me = {
          user_id: body.user.user_id, phone: body.user.phone,
          full_name: body.user.full_name, email: body.user.email,
          is_admin: body.user.is_admin === true, status: 'active',
          must_change_password: body.must_change_password === true,
          roles: [], permissions: [], entities: [], warehouses: [], floors: [],
        };
      }

      window.CandorAuth.save({ access_token: accessToken, refresh_token: refreshToken, me });

      if (body.must_change_password === true) {
        showToast('Password change required — please use the desktop app to reset it.', 'warning');
      }
      window.location.replace('vendor-new.html');
    } catch (err) {
      showToast(`Network error — ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
})();
