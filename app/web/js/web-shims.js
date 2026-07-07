// ── Browser compatibility shim for the Electron renderer scripts ──
//
// The vendor-onboarding module (auth.js / navigation.js / titlebar.js /
// vendor-api.js / vendor-new.js) is copied VERBATIM from the Electron
// frontend. Those files begin with `const { ipcRenderer } = require('electron')`
// and drive auth / navigation / env through IPC to the Electron main process.
//
// In a plain browser there is no `require` and no main process. This shim:
//   • defines a global `require('electron')` that returns a fake ipcRenderer
//   • backs the IPC channels the shared scripts actually use with
//     localStorage (token + /me snapshot) and real fetches to this same
//     FastAPI backend (/api/v1/auth/*).
//
// Load this FIRST — before any script that calls require().

(function () {
  const ORIGIN = window.location.origin;

  // Session storage keys. Kept namespaced so they don't collide with the
  // legacy `candor_auth_*` mirror keys that auth.js also writes.
  const K_ACCESS  = 'cf_web_access';
  const K_REFRESH = 'cf_web_refresh';
  const K_ME      = 'cf_web_me';

  // ── Session helpers (also used by login.js) ──
  const CandorAuth = {
    save({ access_token, refresh_token, me }) {
      if (access_token != null)  localStorage.setItem(K_ACCESS, access_token);
      if (refresh_token != null) localStorage.setItem(K_REFRESH, refresh_token);
      if (me != null)            localStorage.setItem(K_ME, JSON.stringify(me));
    },
    clear() {
      localStorage.removeItem(K_ACCESS);
      localStorage.removeItem(K_REFRESH);
      localStorage.removeItem(K_ME);
    },
    accessToken() { return localStorage.getItem(K_ACCESS); },
    refreshToken() { return localStorage.getItem(K_REFRESH); },
    me() {
      try { return JSON.parse(localStorage.getItem(K_ME) || 'null'); }
      catch (_) { return null; }
    },
    origin: ORIGIN,
  };
  window.CandorAuth = CandorAuth;

  // ── Navigation mapping ──
  // Only the New Vendor wizard + login page exist in this standalone port.
  // Channels targeting sibling pages we didn't port degrade to a toast so
  // the operator isn't silently dropped onto a 404.
  function navigate(channel) {
    switch (channel) {
      case 'navigate-to-login':
      case 'navigate-to-force-change-password':
        window.location.href = 'login.html';
        return;
      case 'navigate-to-existing-vendors':
      case 'navigate-to-vendor-new':
        window.location.href = 'vendor-new.html';
        return;
      default:
        if (typeof showToast === 'function') {
          showToast('Only the New Vendor wizard is available in this standalone port.', 'info');
        }
    }
  }

  // ── Real backend calls behind the auth:* channels ──
  async function doRefresh() {
    const rt = CandorAuth.refreshToken();
    if (!rt) return { ok: false, error: 'invalid_refresh_token' };
    try {
      const res = await fetch(`${ORIGIN}/api/v1/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      });
      if (!res.ok) return { ok: false, error: 'invalid_refresh_token' };
      const d = await res.json();
      CandorAuth.save({ access_token: d.access_token, refresh_token: d.refresh_token });
      return { ok: true, access_token: d.access_token, expires_in: d.expires_in };
    } catch (_) {
      return { ok: false, error: 'refresh_failed' };
    }
  }

  async function doLogout() {
    const rt = CandorAuth.refreshToken();
    const at = CandorAuth.accessToken();
    if (rt && at) {
      try {
        await fetch(`${ORIGIN}/api/v1/auth/logout`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${at}` },
          body: JSON.stringify({ refresh_token: rt }),
        });
      } catch (_) { /* best-effort */ }
    }
    return { ok: true };
  }

  // ── Fake ipcRenderer ──
  const ipcRenderer = {
    // Synchronous hydration used by auth.js at module load.
    sendSync(channel) {
      if (channel === 'auth:get-state-sync') {
        return { access_token: CandorAuth.accessToken(), me: CandorAuth.me() };
      }
      return null;
    },

    async invoke(channel, arg) {
      switch (channel) {
        case 'get-env':       return { API_BASE_URL: ORIGIN };
        case 'get-nav-info':  return { previous: null };
        case 'auth:refresh':  return await doRefresh();
        case 'auth:logout':   return await doLogout();
        case 'auth:logout-all': return { ok: true };
        case 'auth:clear':    CandorAuth.clear(); return { ok: true };
        case 'open-external-url':
          if (typeof arg === 'string' && /^https?:\/\//i.test(arg)) window.open(arg, '_blank', 'noopener');
          return true;
        default:              return null;
      }
    },

    send(channel, _payload) {
      if (channel === 'navigate-back') {
        if (window.history.length > 1) window.history.back();
        else navigate('navigate-to-existing-vendors');
        return;
      }
      if (channel.startsWith('navigate-to-')) { navigate(channel); return; }
      // ws-*, window-minimize / -maximize / -close, ws-update-token → no-op
    },

    // No-op event subscription surface in case any shared script wires one.
    on() {}, once() {}, removeListener() {}, removeAllListeners() {},
  };

  // ── The require('electron') global ──
  const _electron = { ipcRenderer };
  window.require = function (moduleName) {
    if (moduleName === 'electron') return _electron;
    throw new Error(`web-shims: module '${moduleName}' is not available in the browser port`);
  };
})();
