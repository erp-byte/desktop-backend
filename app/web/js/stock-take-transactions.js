// ── Stock Take · adjustment ledger view ──
//
// Read-only listing of stocktake_transactions, plus the one write the ledger
// permits: posting a balancing entry against an existing row. There is no edit
// and no delete because the table blocks both by trigger — a mistake is
// corrected by adding an opposite row that points at the original.
//
// Reversal is the reason this screen exists: the POST endpoint accepts
// reverses_txn_id, but until now nothing could pick WHICH row is being
// corrected, so linked corrections were unreachable from the UI.

(function () {
  const PAGE_SIZE = 50;

  const state = {
    page: 1,
    warehouse: '',
    location: '',
    itemName: '',
    rows: [],
    pagination: null,
    /** txn_ids already reversed, so the button is not offered twice — the
     *  server enforces this with a unique index, but a disabled control is
     *  better than a 400. */
    reversed: new Set(),
    pendingReversal: null,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (n, dp = 2) => Number(n || 0).toLocaleString('en-IN',
    { minimumFractionDigits: dp, maximumFractionDigits: dp });

  /** ISO timestamp -> "02 Sep 2026, 14:32". Built from the parsed parts in the
   *  browser's own zone; the server sends timestamptz so this is a real moment. */
  function fmtWhen(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getDate())} ${M[d.getMonth()]} ${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // ── Scope: fills the filter dropdowns with what this user can see ─────────
  async function loadScope() {
    try {
      const s = await stockTakeApi.getScope();
      fillSelect($('filterWarehouse'), s.warehouses, 'All warehouses');
      fillSelect($('filterLocation'), s.floors, 'All floors');
      if (!s.can_post) {
        $('scopeNote').textContent = s.blocked_reason === 'no_floor_access'
          ? 'You have no floor assigned, so you cannot post or reverse entries. You can still read the ledger.'
          : 'You have no warehouse assigned, so you cannot post or reverse entries. You can still read the ledger.';
        $('scopeNote').hidden = false;
      }
      state.canPost = s.can_post;
    } catch (e) {
      // A failed scope read is not fatal — the ledger still lists, the filters
      // just fall back to free-form. Say so rather than leaving empty selects.
      $('scopeNote').textContent = `Couldn't load your access scope: ${e.message}`;
      $('scopeNote').hidden = false;
      state.canPost = false;
    }
  }

  function fillSelect(el, values, allLabel) {
    if (!el) return;
    el.innerHTML = `<option value="">${esc(allLabel)}</option>`
      + (values || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  }

  // ── Listing ───────────────────────────────────────────────────────────────
  let reqId = 0;
  async function load() {
    const id = ++reqId;
    $('tableBody').innerHTML = `<tr><td colspan="9" class="state-empty">Loading…</td></tr>`;
    try {
      const r = await stockTakeApi.listTransactions({
        warehouse: state.warehouse, location: state.location,
        itemName: state.itemName, page: state.page, pageSize: PAGE_SIZE,
      });
      if (id !== reqId) return;   // a newer request is already in flight
      state.rows = r.transactions || [];
      state.pagination = r.pagination;
      state.rows.forEach((t) => { if (t.reverses_txn_id) state.reversed.add(t.reverses_txn_id); });
      render();
    } catch (e) {
      if (id !== reqId) return;
      $('tableBody').innerHTML =
        `<tr><td colspan="9" class="state-empty">Couldn't load the ledger: ${esc(e.message)}</td></tr>`;
      $('pager').hidden = true;
    }
  }

  function render() {
    const body = $('tableBody');
    if (!state.rows.length) {
      body.innerHTML = `<tr><td colspan="9" class="state-empty">No transactions recorded${
        state.warehouse || state.location || state.itemName ? ' for these filters' : ' yet'}.</td></tr>`;
      $('pager').hidden = true;
      return;
    }

    body.innerHTML = state.rows.map((t) => {
      const isAdd = t.operation === 'ADDITION';
      const signed = `${isAdd ? '+' : '−'}${num(t.qty_kg)}`;
      const alreadyReversed = state.reversed.has(t.txn_id);
      // A reversal cannot itself be reversed (the server refuses), and a row
      // already reversed cannot be reversed twice (unique index).
      const canReverse = state.canPost && !t.is_reversal && !alreadyReversed;
      return `
        <tr>
          <td class="mono">#${t.txn_code}</td>
          <td>
            <div>${esc(t.item_name)}</div>
            <div class="cell-sub">${esc([t.material_type, t.item_category, t.item_subcategory].filter(Boolean).join(' · '))}</div>
          </td>
          <td>${esc(t.stock_type)}</td>
          <td class="num">${num(t.units, 0)}</td>
          <td class="num ${isAdd ? 'pos' : 'neg'}">${signed}</td>
          <td>${esc(t.warehouse)} · ${esc(t.location)}</td>
          <td class="cell-reason" title="${esc(t.reason)}">${esc(t.reason)}</td>
          <td>
            <div>${esc(t.created_by)}</div>
            <div class="cell-sub">${fmtWhen(t.created_at)}</div>
          </td>
          <td>
            ${t.is_reversal
              ? `<span class="badge badge-muted">reverses #${t.reverses_txn_code}</span>`
              : alreadyReversed
                ? `<span class="badge badge-muted">reversed</span>`
                : canReverse
                  ? `<button class="btn btn-ghost btn-sm" data-reverse="${t.txn_id}">Reverse</button>`
                  : ''}
          </td>
        </tr>`;
    }).join('');

    body.querySelectorAll('[data-reverse]').forEach((b) => {
      b.addEventListener('click', () => openReversal(Number(b.dataset.reverse)));
    });

    const p = state.pagination;
    if (p && p.total_pages > 1) {
      $('pager').hidden = false;
      $('pagerInfo').textContent =
        `Page ${p.page} of ${p.total_pages} · ${p.total.toLocaleString('en-IN')} transactions`;
      $('prevPage').disabled = p.page <= 1;
      $('nextPage').disabled = p.page >= p.total_pages;
    } else {
      $('pager').hidden = true;
    }
  }

  // ── Reversal ──────────────────────────────────────────────────────────────
  function openReversal(txnId) {
    const t = state.rows.find((r) => r.txn_id === txnId);
    if (!t) return;
    state.pendingReversal = t;
    const flipped = t.operation === 'ADDITION' ? 'subtraction' : 'addition';
    $('reversalSummary').innerHTML =
      `Reversing <strong>#${t.txn_code}</strong> — ${esc(t.item_name)}, `
      + `${t.operation === 'ADDITION' ? '+' : '−'}${num(t.qty_kg)} kg at ${esc(t.warehouse)} · ${esc(t.location)}.`
      + `<br>This posts a new <strong>${flipped}</strong> of ${num(t.qty_kg)} kg pointing at it. `
      + `Neither row is edited — both stay in the ledger.`;
    $('reversalReason').value = '';
    $('reversalError').hidden = true;
    $('reversalModal').hidden = false;
    $('reversalReason').focus();
  }

  function closeReversal() {
    $('reversalModal').hidden = true;
    state.pendingReversal = null;
  }

  async function confirmReversal() {
    const t = state.pendingReversal;
    const reason = $('reversalReason').value.trim();
    if (!t) return;
    if (!reason) {
      $('reversalError').textContent = 'A reason is required — it is the audit trail for the correction.';
      $('reversalError').hidden = false;
      return;
    }
    $('confirmReversal').disabled = true;
    try {
      const r = await stockTakeApi.reverseTransaction(t, reason);
      showToast(`Reversal #${r.transaction.txn_code} posted against #${t.txn_code}.`, 'success');
      closeReversal();
      state.page = 1;
      await load();
    } catch (e) {
      $('reversalError').textContent = e.message;
      $('reversalError').hidden = false;
    } finally {
      $('confirmReversal').disabled = false;
    }
  }

  // ── Wiring ────────────────────────────────────────────────────────────────
  function init() {
    // No module argument on purpose. requireAuth(module) denies unless the user
    // holds a matching module permission, and the stock-take endpoints are gated
    // on get_current_user alone — there is no `stock_take` permission row to
    // hold. Passing one here would bounce every user off a page the API happily
    // serves them. Access control for WRITES is the floor/warehouse scope the
    // server derives from the token, surfaced by /scope.
    if (typeof requireAuth === 'function') requireAuth();
    if (typeof setupUserInfo === 'function') setupUserInfo();

    // Filters reset paging: page 4 of the old result set means nothing against
    // a new one.
    const onFilter = (fn) => (e) => { fn(e.target.value); state.page = 1; load(); };
    $('filterWarehouse').addEventListener('change', onFilter((v) => { state.warehouse = v; }));
    $('filterLocation').addEventListener('change', onFilter((v) => { state.location = v; }));

    let debounce;
    $('filterItem').addEventListener('input', (e) => {
      clearTimeout(debounce);
      const v = e.target.value.trim();
      debounce = setTimeout(() => { state.itemName = v; state.page = 1; load(); }, 300);
    });

    $('prevPage').addEventListener('click', () => { state.page = Math.max(1, state.page - 1); load(); });
    $('nextPage').addEventListener('click', () => { state.page += 1; load(); });
    $('refresh').addEventListener('click', () => load());

    $('cancelReversal').addEventListener('click', closeReversal);
    $('confirmReversal').addEventListener('click', confirmReversal);
    $('reversalModal').addEventListener('click', (e) => {
      if (e.target === $('reversalModal')) closeReversal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('reversalModal').hidden) closeReversal();
    });

    loadScope().then(load);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
