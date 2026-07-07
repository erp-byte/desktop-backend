// ── Auth session management (IAM v1) ──
//
// Renderer-side shim. Public surface preserved from the legacy version so
// the ~30 modules that call requireAuth() / requireAdmin() / authFetch() /
// getAuthUser() / hasModuleAccess() / hasPermission() / setupUserInfo()
// continue to work unchanged.
//
// Internals replaced:
// • Token storage: refresh token now lives in main process (safeStorage).
//   Access token + /me snapshot live in renderer RAM (closure-scoped).
// • 401 handling: silent refresh + retry once with concurrency dedupe;
//   token_reuse_detected + invalid_refresh_token wipe and redirect.
// • Hydration: on every page load, this module synchronously pulls
//   {access_token, me} from the main process via ipcRenderer.sendSync —
//   keeps requireAuth() synchronous so no module needs an async wrapper.
// • Legacy localStorage: 12 candor_auth_* keys are wiped on first load
//   post-migration.

const { ipcRenderer: _authIpc } = require('electron');

// ── Module state ──

let _accessToken = null;
let _me = null;             // /me shape: {user_id, full_name, phone, email, status, must_change_password,
                            //             roles[], entities[], warehouses[], floors[], is_admin, permissions[]}
let _refreshPromise = null; // dedupes concurrent refreshes; resolves to {access_token, expires_in}

// ── Sync bootstrap from main process ──

(function _hydrate() {
  try {
    const state = _authIpc.sendSync('auth:get-state-sync');
    if (state && typeof state === 'object') {
      _accessToken = state.access_token || null;
      _me = state.me || null;
    }
  } catch (err) {
    console.warn('[auth] hydration failed:', err.message);
  }
  if (_me) _populateLegacyKeys(_me);
  else     _clearLegacyAuthKeys();
})();

// ── Legacy localStorage compat ──
//
// 30+ existing modules read these keys directly (not via getAuthUser()).
// Rather than sweep all callsites, we mirror the synthesized values into
// the legacy keys on hydrate and clear them on logout. Keys that map to
// privileged credentials (token, session_id, expires_at, permissions
// JSON) are never written — only the display/role/audit-trail fields
// the callsites actually need. RAM remains the source of truth; these
// localStorage entries are advisory mirrors, refreshed on every page load.

const _LEGACY_KEYS = [
  'candor_auth_token', 'candor_auth_session_id', 'candor_auth_expires_at',
  'candor_auth_user_id', 'candor_auth_phone', 'candor_auth_full_name',
  'candor_auth_email', 'candor_auth_entity', 'candor_auth_role_id',
  'candor_auth_role_name', 'candor_auth_is_admin', 'candor_auth_permissions',
];
function _clearLegacyAuthKeys() {
  try { _LEGACY_KEYS.forEach(k => localStorage.removeItem(k)); } catch (_) {}
}

function _populateLegacyKeys(me) {
  try {
    const firstRole = (me.roles || [])[0] || null;
    const set = (k, v) => { if (v == null || v === '') localStorage.removeItem(k); else localStorage.setItem(k, String(v)); };
    set('candor_auth_user_id',   me.user_id);
    set('candor_auth_phone',     me.phone);
    set('candor_auth_full_name', me.full_name);
    set('candor_auth_email',     me.email);
    set('candor_auth_entity',    (me.entities || [])[0]);
    // Mirror user-level factory/floor scope arrays as JSON so non-renderer
    // modules (workers, legacy pages that can't import `getMe()`) can read
    // them. Empty array = "no restriction at the user level".
    set('candor_auth_warehouses', JSON.stringify(me.warehouses || []));
    set('candor_auth_floors',     JSON.stringify(me.floors     || []));
    set('candor_auth_role_id',   firstRole && firstRole.role_id);
    // role_name: prefer code (lowercase, used in checker-role gates) over label (display string)
    set('candor_auth_role_name', firstRole && (firstRole.code || firstRole.label));
    set('candor_auth_is_admin',  me.is_admin === true ? 'true' : 'false');
    // Privileged fields intentionally NOT mirrored: token, session_id, expires_at, permissions JSON
  } catch (_) {}
}

// ── Public: token + me getters ──

function getAccessToken() { return _accessToken; }
function getAuthToken()   { return _accessToken; } // legacy alias
function getMe()          { return _me; }
function isLoggedIn()     { return !!_accessToken && !!_me; }

/**
 * User-level factory/floor lock.
 *
 * Returns: { warehouses: string[], floors: string[], entities: string[] }
 *
 * An empty array means "no restriction at the user level" — pages should
 * treat that as "show all options". A non-empty array is the canonical
 * lock-list; pages should filter their dropdowns to that subset.
 * Admins still have empty arrays today (admin bypass is server-side), so
 * callers that want admin to see everything should `if (me.is_admin) return ALL`.
 */
function getAuthScope() {
  return {
    entities:   Array.isArray(_me?.entities)   ? _me.entities.slice()   : [],
    warehouses: Array.isArray(_me?.warehouses) ? _me.warehouses.slice() : [],
    floors:     Array.isArray(_me?.floors)     ? _me.floors.slice()     : [],
  };
}

/**
 * Legacy-shape user object synthesized from the new /me. Preserves all
 * existing callsites that read user.entity / user.roleName / user.isAdmin /
 * user.userId / user.phone / user.fullName / user.email / user.roleId.
 *
 * Multi-valued fields (entities[], warehouses[], floors[], roles[]) collapse
 * to their first element for the legacy contract; consumers who want the
 * full set should call getMe() directly.
 */
function getAuthUser() {
  if (!_me) {
    return { userId: -1, phone: '', fullName: '', email: '', entity: '', roleId: -1, roleName: '', isAdmin: false };
  }
  const firstRole = (_me.roles || [])[0] || null;
  return {
    userId:   _me.user_id ?? -1,
    phone:    _me.phone || '',
    fullName: _me.full_name || '',
    email:    _me.email || '',
    entity:   (_me.entities || [])[0] || '',
    roleId:   firstRole ? (firstRole.role_id ?? -1) : -1,
    roleName: firstRole ? (firstRole.label || firstRole.code || '') : '',
    isAdmin:  _me.is_admin === true,
  };
}

function getAuthPermissions() {
  return (_me && Array.isArray(_me.permissions)) ? _me.permissions : [];
}

/**
 * Module-level access — admins bypass; anyone else needs at least one
 * permission row whose `module` matches.
 */
function hasModuleAccess(module) {
  if (!_me) return false;
  if (_me.is_admin === true) return true;
  return getAuthPermissions().some(p => p.module === module);
}

/**
 * Granular permission check with hierarchical fallback (kept identical to
 * legacy behavior; only the source-of-truth changed).
 *
 * Order: exact (module, sub, sub_sub, action)
 *      → (module, sub, null, action)
 *      → (module, null, null, action)
 */
function hasPermission(module, subModule, subSubModule, action) {
  if (!_me) return false;
  if (_me.is_admin === true) return true;

  const perms = getAuthPermissions();

  for (const p of perms) {
    if (p.module === module && p.action === action
        && (p.sub_module || null) === (subModule || null)
        && (p.sub_sub_module || null) === (subSubModule || null)) {
      return true;
    }
  }

  if (subSubModule) {
    for (const p of perms) {
      if (p.module === module && (p.sub_module || null) === (subModule || null)
          && !p.sub_sub_module && p.action === action) {
        return true;
      }
    }
  }

  if (subModule) {
    for (const p of perms) {
      if (p.module === module && !p.sub_module && !p.sub_sub_module && p.action === action) {
        return true;
      }
    }
  }

  return false;
}

// ── Public: setters / clearers ──

/**
 * Login flow: login.js calls auth:login IPC, gets {ok, access_token, me},
 * then calls setAuth({...}) to update local module state before navigating.
 *
 * Note: main process already has _me populated (auth:login sets it before
 * returning), so we don't fire a redundant auth:set-me IPC here — that
 * would race with the imminent navigation's sync hydration on the new page.
 */
function setAuth({ access_token, me }) {
  _accessToken = access_token || null;
  _me = me || null;
  if (_me) _populateLegacyKeys(_me);
}

function clearAuthSession() {
  _accessToken = null;
  _me = null;
  try { _authIpc.invoke('auth:clear'); } catch (_) {}
  _clearLegacyAuthKeys();
}

async function performAuthLogout() {
  try { await _authIpc.invoke('auth:logout'); } catch (_) {}
  _authIpc.send('ws-stop');
  clearAuthSession();
  if (typeof clearAllPageStates === 'function') clearAllPageStates();
  _authIpc.send('navigate-to-login');
}

async function performLogoutAll() {
  try { await _authIpc.invoke('auth:logout-all', _accessToken); } catch (_) {}
  _authIpc.send('ws-stop');
  clearAuthSession();
  if (typeof clearAllPageStates === 'function') clearAllPageStates();
  _authIpc.send('navigate-to-login');
}

// ── Public: route guards (signatures unchanged) ──

function requireAuth(module) {
  if (!isLoggedIn()) {
    _authIpc.send('navigate-to-login');
    return false;
  }
  if (_me && _me.must_change_password === true) {
    _authIpc.send('navigate-to-force-change-password');
    return false;
  }
  if (module && !hasModuleAccess(module)) {
    if (typeof showToast === 'function') {
      showToast('Access denied: you do not have permission for this module.');
    }
    setTimeout(() => _authIpc.send('navigate-to-modules', { from: 'access-denied' }), 500);
    return false;
  }
  return true;
}

function requireAdmin() {
  if (!requireAuth()) return false;
  if (!(_me && _me.is_admin === true)) {
    if (typeof showToast === 'function') {
      showToast('Admin access required.');
    }
    setTimeout(() => _authIpc.send('navigate-to-modules', { from: 'access-denied' }), 500);
    return false;
  }
  return true;
}

// ── Sidebar user-info hookup (kept identical to legacy) ──

function setupUserInfo() {
  const u = getAuthUser();
  const nameEl = document.getElementById('userName');
  const roleEl = document.getElementById('userRole');
  const avatarEl = document.getElementById('userAvatar');
  if (nameEl) nameEl.textContent = u.fullName || 'User';
  if (roleEl) roleEl.textContent = u.roleName || (_me && _me.is_admin ? 'Admin' : 'Role');
  if (avatarEl) avatarEl.textContent = (u.fullName || 'U').charAt(0).toUpperCase();
}

// ── Authenticated fetch wrapper ──

/**
 * Adds Authorization. On 401, parses the IAM v1 envelope, branches on
 * error code, and (for invalid_access_token / generic 401) silently
 * refreshes via main and retries the original request once.
 *
 * Concurrency: when N tabs hit 401 simultaneously, only ONE auth:refresh
 * IPC fires — the rest await the same shared promise.
 */
async function authFetch(url, options = {}) {
  if (!_accessToken) {
    _redirectToLogin('expired');
    throw new Error('No auth token');
  }

  const firstRes = await _doFetch(url, options, _accessToken);
  if (firstRes.status !== 401) return _decorate403(firstRes);

  // Peek the envelope on a clone so the caller can still consume the body
  // if we end up returning this response (which we don't, since we're
  // refreshing — but defensive cloning costs nothing).
  const env = await _peekEnvelope(firstRes.clone());

  if (env.error === 'token_reuse_detected') {
    _redirectToLogin('security', 'Your session was revoked for security reasons. Please log in again.');
    throw new Error('Session security');
  }
  if (env.error === 'invalid_refresh_token') {
    _redirectToLogin('expired');
    throw new Error('Session expired');
  }

  // Generic 401 → silent refresh + retry once, with concurrency dedupe.
  // The dedupe holds the promise for ~1 second AFTER it resolves so
  // straggler 401s arriving on the heels of the first refresh consume the
  // cached result instead of firing a second auth:refresh (which would
  // arm backend reuse-detection if rotation already occurred server-side).
  if (!_refreshPromise) {
    _refreshPromise = _authIpc.invoke('auth:refresh')
      .then(r => {
        if (!r || !r.ok) throw new Error((r && r.error) || 'refresh_failed');
        _accessToken = r.access_token;
        window.dispatchEvent(new CustomEvent('cf:auth:tokens-changed', { detail: r }));
        try { _authIpc.send('ws-update-token', { token: _accessToken }); } catch (_) {}
        return r;
      })
      .catch(err => { _redirectToLogin('expired'); throw err; });
    // Hold the resolved promise reachable for 1s so concurrent 401s share it.
    _refreshPromise.finally(() => {
      const p = _refreshPromise;
      setTimeout(() => { if (_refreshPromise === p) _refreshPromise = null; }, 1000);
    });
  }

  try { await _refreshPromise; }
  catch (_) { throw new Error('Refresh failed'); }

  // Retry once with new token
  return _decorate403(await _doFetch(url, options, _accessToken));
}

async function _doFetch(url, options, token) {
  const headers = { ...(options.headers || {}) };
  headers['Authorization'] = `Bearer ${token}`;
  return fetch(url, { ...options, headers });
}

async function _peekEnvelope(res) {
  try {
    const data = await res.json();
    return {
      error:   data && data.error,
      message: data && data.message,
      request_id: data && data.request_id,
      details: (data && data.details) || {},
    };
  } catch (_) {
    return { error: null, message: null, request_id: null, details: {} };
  }
}

function _decorate403(res) {
  if (res.status !== 403) return res;
  // Branch on the structured envelope so admin_required redirects to
  // modules instead of just toasting (per spec).
  res.clone().json().then(env => {
    const code = env && env.error;
    const msg  = (env && env.message) || "Access denied. You don't have permission for this action.";
    if (typeof showToast === 'function') showToast(msg);
    if (code === 'admin_required') {
      setTimeout(() => _authIpc.send('navigate-to-modules', { from: 'access-denied' }), 600);
    }
  }).catch(() => {
    if (typeof showToast === 'function') showToast("Access denied. You don't have permission for this action.");
  });
  return res;
}

let _redirecting = false;
function _redirectToLogin(reason, toastMsg) {
  if (_redirecting) return;       // coalesce concurrent failures into one redirect
  _redirecting = true;
  if (toastMsg && typeof showToast === 'function') showToast(toastMsg);
  clearAuthSession();
  if (typeof clearAllPageStates === 'function') clearAllPageStates();
  _authIpc.send('ws-stop');
  setTimeout(() => {
    _authIpc.send('navigate-to-login');
    // Released after navigation fires — new page resets module scope anyway.
  }, 600);
}
