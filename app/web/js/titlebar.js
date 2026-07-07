// ── Window control buttons ──
// Binds minimize/maximize/close to titlebar buttons via IPC.
// Loaded via <script src> — runs after DOM is ready (placed at end of <body>).

const { ipcRenderer: _tbIpc } = require('electron');

document.querySelector('.titlebar-btn.minimize')?.addEventListener('click', () => _tbIpc.send('window-minimize'));
document.querySelector('.titlebar-btn.maximize')?.addEventListener('click', () => _tbIpc.send('window-maximize'));
document.querySelector('.titlebar-btn.close')?.addEventListener('click', () => _tbIpc.send('window-close'));
