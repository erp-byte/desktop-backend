// ── Toast notifications ──
// Requires a toast element in the HTML:
//   <div class="toast" id="toast"><span id="toastIcon"></span><span id="toastMsg"></span></div>
// Exposes global showToast(message, type) function.
// Types: 'error' (red), 'success' (green), 'info' (blue), 'warning' (orange)

let _toastTimer;
function showToast(message, type = 'error') {
  const toast = document.getElementById('toast');
  if (!toast) return;
  clearTimeout(_toastTimer);
  toast.className = `toast ${type}`;
  document.getElementById('toastMsg').textContent = message;

  const icons = {
    error: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    success: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="8 12 11 15 16 9"/></svg>',
    info: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    warning: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  };

  document.getElementById('toastIcon').innerHTML = icons[type] || icons.error;
  requestAnimationFrame(() => toast.classList.add('show'));
  _toastTimer = setTimeout(() => toast.classList.remove('show'), 4000);
}
