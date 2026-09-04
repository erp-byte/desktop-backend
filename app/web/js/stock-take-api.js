// ── Stock Take · adjustment ledger API client ──
//
// Thin wrapper around authFetch for /api/v1/stock-take/*. The ledger is
// APPEND-ONLY — stocktake_transactions blocks UPDATE and DELETE by trigger — so
// this client deliberately exposes no update or delete call. A correction is a
// new row posted with reverses_txn_id, which is what reverseTransaction() does.
//
// Exposes globals (window.stockTakeApi.*) so page scripts loaded after this file
// can call without imports, matching the rest of app/web.

(function () {
  const BASE = '/api/v1/stock-take';

  /** Surface the API's own message rather than a bare status.
   *  The house envelope hoists error/message to the top level (see
   *  request_context), but a plain FastAPI HTTPException nests them under
   *  detail — both shapes are unwrapped here. */
  async function readError(res, fallback) {
    try {
      const d = await res.json();
      if (d) {
        if (typeof d.detail === 'string' && d.detail) return d.detail;
        if (d.detail && typeof d.detail === 'object') {
          if (d.detail.message) return String(d.detail.message);
          if (d.detail.error) return String(d.detail.error);
        }
        if (d.message) return String(d.message);
        if (d.error) return String(d.error);
      }
    } catch (_) { /* non-JSON body */ }
    return fallback;
  }

  async function get(path, params) {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    });
    const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ''}`;
    const res = await authFetch(url);
    if (!res.ok) throw new Error(await readError(res, `HTTP ${res.status}`));
    return res.json();
  }

  /** Warehouses and floors the signed-in user may post against, plus why not. */
  const getScope = () => get('/scope');

  const listTransactions = (q) => get('/transactions', {
    warehouse: q && q.warehouse,
    location: q && q.location,
    itemName: q && q.itemName,
    page: q && q.page,
    pageSize: q && q.pageSize,
  });

  /** Counted + netted balance for one article at the caller's scope. */
  const getBalance = (q) => get('/balance', {
    itemName: q.itemName,
    stockType: q.stockType,
    warehouse: q.warehouse,
    location: q.location,
  });

  async function createTransaction(body) {
    const res = await authFetch(`${BASE}/transactions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readError(res, `HTTP ${res.status}`));
    return res.json();
  }

  /** Post the balancing entry for an existing row.
   *
   *  Copies the original's article, classification and magnitude, flips the
   *  direction, and points at it with reverses_txn_id. The server derives
   *  is_reversal from that and refuses to reverse a reversal; a partial unique
   *  index makes a second reversal of the same row impossible. Quantities stay
   *  POSITIVE — direction lives in `operation`, never in the sign. */
  function reverseTransaction(original, reason) {
    return createTransaction({
      item_name: original.item_name,
      sku_id: original.sku_id,
      is_new_article: original.is_new_article,
      material_type: original.material_type,
      item_category: original.item_category,
      item_subcategory: original.item_subcategory,
      stock_type: original.stock_type,
      units: original.units,
      qty_kg: original.qty_kg,
      operation: original.operation === 'ADDITION' ? 'SUBTRACTION' : 'ADDITION',
      reason: reason,
      warehouse: original.warehouse,
      location: original.location,
      reverses_txn_id: original.txn_id,
    });
  }

  window.stockTakeApi = {
    getScope, listTransactions, getBalance, createTransaction, reverseTransaction,
  };
})();
