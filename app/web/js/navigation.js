// ── Navigation helpers ──
// Provides back-arrow setup, logout handler, and state persistence utilities.
// Exposes globals: PAGE_LABELS, setupBackArrow(), setupLogout(), clearAllPageStates(), getEnv()

const { ipcRenderer: _navIpc } = require('electron');

const PAGE_LABELS = {
  login: 'Login',
  modules: 'Modules',
  production: 'Production',
  'so-creation': 'SO Creation',
  'production-gst': 'GST Summary',
  'production-wo': 'Work Orders',
  'fulfillment': 'SO Fulfillment',
  'planning': 'Planning',
  'plan-builder': 'Plan Builder',
  'plan-list': 'Plan Dashboard',
  'plan-detail': 'Plan Detail',
  'indents': 'Purchase Indents',
  'alerts': 'Alerts',
  'orders': 'Production Orders',
  'job-cards': 'Job Cards',
  'job-card-detail': 'Job Card Detail',
  'team-dashboard': 'Team Dashboard',
  'floor-dashboard': 'Floor Dashboard',
  'floor-inventory': 'Floor Inventory',
  'movements': 'Movement History',
  'offgrade': 'Off-Grade',
  'loss-analytics': 'Loss & Yield',
  'day-end': 'Day-End',
  'balance-scan': 'Balance Scan',
  'discrepancy': 'Discrepancies',
  'purchase': 'Purchase',
  'po-creation': 'Purchase Order Upload',
  'po-receiving': 'PO Receiving',
  'po-manual-entry': 'PO Manual Entry',
  'material-receipt':   'Material Receipt',
  'send-intimation':    'Send Intimation',
  'create-inward':      'Create Inward',
  'vendor-management':  'Vendor Management',
  'existing-vendors':   'Existing Vendors',
  'vendor-new':         'New Vendor',
  'vendor-detail':      'Vendor Detail',
  'vendor-evaluation':  'Vendor Evaluation',
  'quality-control':    'Quality Control',
  'inward-check':       'Inward Check',
  'admin': 'Admin',
  'webhooks': 'Webhooks',
};

/**
 * Sets up the back-arrow button in the titlebar.
 * Call with a beforeNavigate callback to save state before going back.
 */
async function setupBackArrow(beforeNavigate) {
  const navInfo = await _navIpc.invoke('get-nav-info');
  if (navInfo.previous) {
    const backArrow = document.getElementById('backArrow');
    const backSep = document.getElementById('backSep');
    if (!backArrow) return navInfo;
    document.getElementById('backLabel').textContent = PAGE_LABELS[navInfo.previous] || 'Back';
    backArrow.classList.add('show');
    if (backSep) backSep.classList.add('show');
    backArrow.addEventListener('click', () => {
      if (beforeNavigate) beforeNavigate();
      _navIpc.send('navigate-back');
    });
  }
  return navInfo;
}

/**
 * Wires up one or more logout buttons by ID.
 * Calls auth logout API, clears session + page states, navigates to login.
 */
function setupLogout(...buttonIds) {
  buttonIds.forEach(id => {
    const btn = document.getElementById(id);
    if (btn) {
      btn.addEventListener('click', () => {
        if (typeof performAuthLogout === 'function') {
          performAuthLogout();
        } else {
          clearAllPageStates();
          _navIpc.send('navigate-to-login');
        }
      });
    }
  });
}

function clearAllPageStates() {
  Object.keys(localStorage).forEach(k => {
    if (k.startsWith('candor_state_')) localStorage.removeItem(k);
  });
}

/**
 * Fetches environment variables from the main process.
 * Returns an object with allowed env vars (API_BASE_URL, MOCK_AUTH_EMAIL, etc.)
 */
let _envCache = null;
async function getEnv() {
  if (!_envCache) {
    _envCache = await _navIpc.invoke('get-env');
  }
  return _envCache;
}

/**
 * Wires every standard production sidebar nav-item to its IPC channel.
 * Runs automatically on DOMContentLoaded. Idempotent via dataset.navWired.
 * Uses `onclick =` (overwrite-safe) so per-module handlers that also bind
 * via addEventListener won't cause navStack double-pushes from this wiring.
 */
const SIDEBAR_NAV_MAP = {
  navDashboard:       'navigate-to-dashboard',
  navBackModules:     'navigate-to-modules',
  navBackProduction:  'navigate-to-production',
  navSoCreation:      'navigate-to-so-creation',
  navOrders:          'navigate-to-orders',
  navFulfillment:     'navigate-to-fulfillment',
  navPlanning:        'navigate-to-planning',
  navPlanList:        'navigate-to-plan-list',
  navPlanBuilder:     'navigate-to-plan-builder',
  navJobCards:        'navigate-to-job-cards',
  navTeamDash:        'navigate-to-team-dashboard',
  navFloorDash:       'navigate-to-floor-dashboard',
  navStoreDash:       'navigate-to-store-dashboard',
  navIndents:         'navigate-to-indents',
  navProdIndents:     'navigate-to-production-indent',
  navQcDash:          'navigate-to-qc-dashboard',
  navAlerts:          'navigate-to-alerts',
};

function wireStandardNav(sourcePage) {
  Object.entries(SIDEBAR_NAV_MAP).forEach(([id, channel]) => {
    const btn = document.getElementById(id);
    if (!btn || btn.dataset.navWired) return;
    btn.dataset.navWired = '1';
    btn.onclick = () => _navIpc.send(channel, { from: sourcePage || undefined });
  });
}

/**
 * Populates the sidebar user-card from the live login (auth.js · getAuthUser()).
 * Updates any element with class .user-name / .user-email / .user-avatar found
 * inside .user-card containers — works on every page that uses the standard
 * sidebar footer template, no per-page wiring required.
 *
 * Falls back gracefully if auth.js hasn't loaded yet (no-op) or the user
 * fields are blank.
 */
function setupSidebarUser() {
  if (typeof getAuthUser !== 'function') return;
  const u = getAuthUser() || {};
  const name    = u.fullName || u.phone || 'User';
  // Prefer email; fall back to humanised role; finally phone or '—'.
  const subline = u.email
    || (u.roleName ? String(u.roleName).replace(/_/g, ' ').toLowerCase() : '')
    || u.phone
    || '';
  const initial = (u.fullName || u.phone || 'U').trim().charAt(0).toUpperCase() || 'U';

  document.querySelectorAll('.user-card').forEach(card => {
    const nameEl   = card.querySelector('.user-name');
    const emailEl  = card.querySelector('.user-email');
    const avatarEl = card.querySelector('.user-avatar');
    if (nameEl)   nameEl.textContent = name;
    if (emailEl)  emailEl.textContent = subline;
    if (avatarEl) avatarEl.textContent = initial;
  });
}

// Auto-wire on DOMContentLoaded so every page inherits nav + user info for free.
function _initOnReady() {
  wireStandardNav();
  setupSidebarUser();
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initOnReady);
} else {
  _initOnReady();
}

/**
 * Fetches unread alert count and updates the sidebar badge.
 * Call on page load for any page that has #alertBadge in the sidebar.
 */
async function updateAlertBadge() {
  try {
    const env = await getEnv();
    const base = env?.API_BASE_URL || 'http://127.0.0.1:8000';
    const fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    const res = await fetcher(`${base}/api/v1/production/alerts?read=false&page_size=1`);
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById('alertBadge');
    if (!badge) return;
    const count = data.pagination?.total || 0;
    if (count > 0) {
      badge.textContent = count > 99 ? '99+' : count;
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
  } catch (_) {}
}
