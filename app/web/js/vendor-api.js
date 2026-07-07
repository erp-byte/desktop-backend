// ── Vendor Management · API client ──
//
// Thin wrapper around authFetch that knows the vendor service shape:
//   • single configured base URL pulled from main process via getEnv()
//   • all vendor endpoints under /api/v1/vendors
//   • lookup_value cache for the dropdown sources (SUPPLIER_TYPE, etc.)
//
// Backend error envelope:
//   { detail: { code, msg, ...context } }   — surfaced via VendorApiError.
//
// Exposes globals (window.vendorApi.*) so renderer scripts loaded after this
// file can call without imports (matches the rest of the codebase's idiom).

(function () {
  const VENDORS_PATH = '/api/v1/vendors';
  const LOOKUPS_PATH = '/api/v1/lookups';

  // ── Hardcoded enums (no API needed) ──
  const ENUMS = {
    VENDOR_STATUS: [
      { code: 'active',      label: 'Active' },
      { code: 'inactive',    label: 'Inactive' },
      { code: 'blacklisted', label: 'Blacklisted' },
    ],
    DOC_TYPE: [
      { code: 'FSSAI',     label: 'FSSAI' },
      { code: 'BRC',       label: 'BRC' },
      { code: 'PAN',       label: 'PAN' },
      { code: 'GST',       label: 'GST' },
      { code: 'MSME',      label: 'MSME' },
      { code: 'UDYAM',     label: 'UDYAM' },
      { code: 'IEC',       label: 'IEC' },
      { code: 'EPR',       label: 'EPR' },
      { code: 'CIN',       label: 'CIN' },
      { code: 'TIN',       label: 'TIN' },
      { code: 'TAN',       label: 'TAN' },
      { code: 'POLLUTION', label: 'Pollution' },
      { code: 'CONTRACT',  label: 'Contract' },
      { code: 'OTHER',     label: 'Other' },
    ],
    CONTRACT_TYPE: [
      { code: 'yearly',   label: 'Yearly' },
      { code: 'one-time', label: 'One-time' },
      { code: 'NDA',      label: 'NDA' },
      { code: 'MSA',      label: 'MSA' },
    ],
  };

  // Acceptable file types for document/contract upload.
  const UPLOAD_ACCEPT = 'application/pdf,image/jpeg,image/png';
  const UPLOAD_MAX_BYTES = 25 * 1024 * 1024;

  let _baseUrl = null;
  let _baseReady = null;

  async function ensureBase() {
    if (_baseUrl) return _baseUrl;
    if (_baseReady) return _baseReady;
    _baseReady = (async () => {
      try {
        const env = (typeof getEnv === 'function') ? await getEnv() : null;
        _baseUrl = (env && env.API_BASE_URL) ? env.API_BASE_URL.replace(/\/+$/, '') : 'http://127.0.0.1:8000';
      } catch (_) {
        _baseUrl = 'http://127.0.0.1:8000';
      }
      return _baseUrl;
    })();
    return _baseReady;
  }

  // ── Error type ──
  // Surfaced for both transport failures and 4xx/5xx responses with the
  // backend's { detail: {code, msg, ...} } envelope. Callers can `instanceof
  // VendorApiError` and key i18n / inline messages off `.code`.
  class VendorApiError extends Error {
    constructor({ status, code, msg, context }) {
      super(msg || code || `HTTP ${status}`);
      this.name = 'VendorApiError';
      this.status = status;
      this.code = code || null;
      this.context = context || {};
    }
  }

  async function _parseError(res) {
    let body = null;
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    const detail = body && body.detail;
    if (detail && typeof detail === 'object') {
      const { code, msg, ...rest } = detail;
      return new VendorApiError({ status: res.status, code, msg, context: rest });
    }
    if (typeof detail === 'string') {
      return new VendorApiError({ status: res.status, code: null, msg: detail });
    }
    return new VendorApiError({ status: res.status, code: null, msg: `HTTP ${res.status}` });
  }

  async function _request(path, opts = {}) {
    const base = await ensureBase();
    const res = await authFetch(`${base}${path}`, opts);
    if (!res.ok) throw await _parseError(res);
    if (res.status === 204) return null;
    return res.json();
  }

  // ── Lookups (with module-scoped cache) ──
  // The brief states the endpoint shape `GET /api/v1/lookups?type=<TYPE>`
  // returning `[{ lookup_id, code, label, display_order, is_active }]`. If
  // the backend hasn't shipped this yet, we surface an empty array so the
  // form still renders — operators see "(no options)" rather than a crash.
  const _lookupCache = new Map();
  const _lookupInflight = new Map();

  async function getLookups(type) {
    if (!type) return [];
    if (_lookupCache.has(type)) return _lookupCache.get(type);
    if (_lookupInflight.has(type)) return _lookupInflight.get(type);
    const p = (async () => {
      try {
        const data = await _request(`${LOOKUPS_PATH}?type=${encodeURIComponent(type)}`);
        const list = Array.isArray(data) ? data : (data && data.items) || [];
        const filtered = list
          .filter(r => r.is_active !== false)
          .sort((a, b) => (a.display_order ?? 999) - (b.display_order ?? 999));
        _lookupCache.set(type, filtered);
        return filtered;
      } catch (err) {
        // Endpoint may not exist yet — degrade gracefully.
        console.warn(`[vendor-api] lookup ${type} failed:`, err.message);
        _lookupCache.set(type, []);
        return [];
      } finally {
        _lookupInflight.delete(type);
      }
    })();
    _lookupInflight.set(type, p);
    return p;
  }

  function getLookupLabel(list, id) {
    if (id == null) return '';
    const row = (list || []).find(r => r.lookup_id === id);
    return row ? (row.label || row.code || '') : '';
  }

  // ── Vendor master ──
  // `approval` is 'approved' | 'pending' | undefined (omit = both).
  // Backend already sorts approved-first regardless of the filter, so
  // the UI can render one mixed list without re-grouping.
  function listVendors({ status, category_code_id, search, approval,
                         page = 1, page_size = 50 } = {}) {
    const qs = new URLSearchParams();
    if (status)            qs.set('status', status);
    if (category_code_id)  qs.set('category_code_id', category_code_id);
    if (search)            qs.set('search', search);
    if (approval)          qs.set('approval', approval);
    qs.set('page', String(page));
    qs.set('page_size', String(page_size));
    return _request(`${VENDORS_PATH}/?${qs.toString()}`);
  }

  // GET /paged — same shape as listVendors, server pins page_size to 200.
  // Use this for denser desktop tables that don't need a page-size knob.
  function listVendorsPaged({ status, category_code_id, search, approval, page = 1 } = {}) {
    const qs = new URLSearchParams();
    if (status)            qs.set('status', status);
    if (category_code_id)  qs.set('category_code_id', category_code_id);
    if (search)            qs.set('search', search);
    if (approval)          qs.set('approval', approval);
    qs.set('page', String(page));
    return _request(`${VENDORS_PATH}/paged?${qs.toString()}`);
  }

  // GET /search — direct DB hit, no pagination. Returns up to 1000 matches
  // in one round-trip. `truncated=true` in the response means the query
  // matched more than 1000 vendors — prompt the user to narrow the term.
  // `q` is required and 2-char minimum (backend enforces; we early-return
  // here so the UI doesn't fire empty searches on every keystroke).
  function searchVendors({ q, status, category_code_id, approval } = {}) {
    if (!q || q.trim().length < 2) {
      return Promise.resolve({ vendors: [], total_returned: 0, truncated: false });
    }
    const qs = new URLSearchParams();
    qs.set('q', q.trim());
    if (status)            qs.set('status', status);
    if (category_code_id)  qs.set('category_code_id', category_code_id);
    if (approval)          qs.set('approval', approval);
    return _request(`${VENDORS_PATH}/search?${qs.toString()}`);
  }

  function createVendor(body) {
    return _request(`${VENDORS_PATH}/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // POST /with-documents — vendor JSON + N files in one multipart payload.
  // `files` is an array of File objects; `docsMeta` is parallel array of
  // { doc_type, doc_number? }. The vendor commits even if docs fail.
  async function createVendorWithDocuments(vendor, files, docsMeta) {
    const fd = new FormData();
    fd.append('vendor', JSON.stringify(vendor));
    if (Array.isArray(files) && files.length) {
      fd.append('docs_meta', JSON.stringify(docsMeta || []));
      files.forEach(f => fd.append('files', f, f.name));
    } else {
      fd.append('docs_meta', JSON.stringify([]));
    }
    return _request(`${VENDORS_PATH}/with-documents`, { method: 'POST', body: fd });
  }

  // POST /extract-bulk — phase-1 of the new onboarding flow.
  // Uploads N files BEFORE a vendor row exists; Claude reads every doc and
  // returns a consolidated suggestion across all tabs (vendor / banking /
  // documents / contracts) keyed by a `staging_id`. Nothing is persisted
  // to vendor_master until the user submits via /vendors/submit-staged.
  //
  // `docsMeta` (optional) is a parallel array of `{ doc_type }` hints; pass
  // empty to let the model infer doc_type from content.
  //
  // `opts.signal` (optional AbortSignal) lets callers cancel an in-flight
  // extraction — the underlying fetch aborts and a DOMException with
  // name="AbortError" propagates so the wizard can roll the UI back.
  async function extractBulk(files, docsMeta, { signal } = {}) {
    const fd = new FormData();
    fd.append('docs_meta', JSON.stringify(docsMeta || []));
    files.forEach(f => fd.append('files', f, f.name));
    return _request(`${VENDORS_PATH}/extract-bulk`, {
      method: 'POST',
      body: fd,
      signal,
    });
  }

  // POST /submit-staged — phase-2. Commits the (potentially-edited) staged
  // payload to vendor_master + child tables in one transaction. Server
  // promotes the S3 staging objects to `vendors/{supplier_code}/...`.
  //
  // `body` shape:
  //   { staging_id, vendor: {...}, banking: [...],
  //     documents: [...], contracts: [...] }
  function submitStagedVendor(body) {
    return _request(`${VENDORS_PATH}/submit-staged`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // ── History (state management) ──
  //
  // The server snapshots the previous + new row state for every mutation
  // on vendor_master / banking / document / contract. These endpoints
  // expose the timeline and let an admin revert a single field-set.
  function listVendorHistory(vendorId, { operation, fromDate, toDate, page = 1, pageSize = 50 } = {}) {
    const qs = new URLSearchParams();
    if (operation) qs.set('operation', operation);
    if (fromDate)  qs.set('from', fromDate);
    if (toDate)    qs.set('to', toDate);
    qs.set('page', String(page));
    qs.set('page_size', String(pageSize));
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/history?${qs.toString()}`);
  }
  function getHistoryEntry(vendorId, historyId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/history/${encodeURIComponent(historyId)}`);
  }
  function revertHistory(vendorId, historyId, reason) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/history/${encodeURIComponent(historyId)}/revert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason || null }),
    });
  }
  function listBankingHistory(vendorId, bankId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking/${encodeURIComponent(bankId)}/history`);
  }
  function listDocumentHistory(vendorId, docId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/${encodeURIComponent(docId)}/history`);
  }
  function listContractHistory(vendorId, contractId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/${encodeURIComponent(contractId)}/history`);
  }

  function getVendor(vendorId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}`);
  }

  // PATCH /vendors/{id} — when `reason` is provided we attach it as `_reason`,
  // which the backend strips off the column patch and persists on the history
  // row. This is what surfaces in the timeline as the "why" of each edit.
  function patchVendor(vendorId, body, { reason } = {}) {
    const payload = reason ? { ...body, _reason: reason } : body;
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  function deleteVendor(vendorId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}`, { method: 'DELETE' });
  }

  function approveVendor(vendorId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/approve`, { method: 'POST' });
  }

  // ── Banking ──
  function addBanking(vendorId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function listBanking(vendorId, { activeOnly = false } = {}) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking?active_only=${activeOnly ? 'true' : 'false'}`);
  }
  function patchBanking(vendorId, bankId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking/${encodeURIComponent(bankId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function deleteBanking(vendorId, bankId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking/${encodeURIComponent(bankId)}`, { method: 'DELETE' });
  }
  function setPrimaryBanking(vendorId, bankId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/banking/${encodeURIComponent(bankId)}/set-primary`, { method: 'POST' });
  }

  // ── Documents ──
  function addDocument(vendorId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function extractDocument(vendorId, file, docType) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    fd.append('doc_type', docType);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/extract`, { method: 'POST', body: fd });
  }
  function uploadAndSaveDocument(vendorId, file, docType, { docNumber, statusId } = {}) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    fd.append('doc_type', docType);
    if (docNumber) fd.append('doc_number', docNumber);
    if (statusId)  fd.append('status_id', statusId);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/upload-and-save`, { method: 'POST', body: fd });
  }
  function appendDocumentFile(vendorId, docId, file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/${encodeURIComponent(docId)}/append-file`, { method: 'POST', body: fd });
  }
  function listDocuments(vendorId, { docType, expiringWithinDays } = {}) {
    const qs = new URLSearchParams();
    if (docType)             qs.set('doc_type', docType);
    if (expiringWithinDays)  qs.set('expiring_within_days', String(expiringWithinDays));
    const tail = qs.toString() ? `?${qs.toString()}` : '';
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents${tail}`);
  }
  function patchDocument(vendorId, docId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/${encodeURIComponent(docId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function deleteDocument(vendorId, docId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' });
  }

  // ── Contracts ──
  function addContract(vendorId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function extractContract(vendorId, file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/extract`, { method: 'POST', body: fd });
  }
  function uploadAndSaveContract(vendorId, file, { contractType } = {}) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (contractType) fd.append('contract_type', contractType);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/upload-and-save`, { method: 'POST', body: fd });
  }
  function appendContractFile(vendorId, contractId, file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/${encodeURIComponent(contractId)}/append-file`, { method: 'POST', body: fd });
  }
  function listContracts(vendorId, { contractType } = {}) {
    const qs = contractType ? `?contract_type=${encodeURIComponent(contractType)}` : '';
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts${qs}`);
  }
  function patchContract(vendorId, contractId, body) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/${encodeURIComponent(contractId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function deleteContract(vendorId, contractId) {
    return _request(`${VENDORS_PATH}/${encodeURIComponent(vendorId)}/contracts/${encodeURIComponent(contractId)}`, { method: 'DELETE' });
  }

  // ── Field-level helpers used by the UI ──

  // s3_urls is a CSV string on the wire. Split→array for read paths, join for
  // write paths. Empty values collapse to '' (not 'null') so the backend's
  // whitespace normaliser sees a clean string.
  function s3UrlsToArray(s) {
    return String(s || '').split(',').map(u => u.trim()).filter(Boolean);
  }
  function s3UrlsToCsv(arr) {
    return (arr || []).filter(Boolean).join(',');
  }

  // Validate IFSC client-side: 4 letters + '0' + 6 alphanumeric (uppercase).
  function isValidIfsc(s) {
    return /^[A-Z]{4}0[A-Z0-9]{6}$/.test(s || '');
  }

  // File-type / size guard mirroring the backend 25 MB allowlist.
  function validateUploadFile(file) {
    if (!file) return 'No file chosen.';
    const okType = ['application/pdf', 'image/jpeg', 'image/png'].includes(file.type);
    if (!okType) return 'Only PDF / JPEG / PNG are accepted.';
    if (file.size > UPLOAD_MAX_BYTES) return `File too large (${(file.size / 1048576).toFixed(1)} MB). Max is 25 MB.`;
    return null;
  }

  // HTML escape for innerHTML interpolation. The vendor surface previously
  // duplicated this in 3 files; consolidating here so a future fix lands once.
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Derive a friendly chip label from an S3 URL — last path segment minus
  // querystring, truncated. Fallback to "file N" if extraction yields empty.
  function s3UrlBasename(url, fallback = 'file') {
    try {
      const path = String(url || '').split('?')[0];
      const tail = path.split('/').filter(Boolean).pop() || '';
      const decoded = decodeURIComponent(tail);
      if (!decoded) return fallback;
      return decoded.length > 40 ? decoded.slice(0, 37) + '…' : decoded;
    } catch (_) {
      return fallback;
    }
  }

  // Open an http(s) URL in the OS default browser via main process. Avoids
  // window.open inside a nodeIntegration renderer (which would inherit
  // nodeIntegration in the popup window — a real security hole).
  function openExternalUrl(url) {
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return Promise.resolve(false);
    try {
      const { ipcRenderer } = require('electron');
      return ipcRenderer.invoke('open-external-url', url);
    } catch (_) {
      return Promise.resolve(false);
    }
  }

  // ── Public surface ──
  window.vendorApi = {
    ENUMS, UPLOAD_ACCEPT, UPLOAD_MAX_BYTES, VendorApiError,

    // helpers
    getLookups, getLookupLabel,
    s3UrlsToArray, s3UrlsToCsv, s3UrlBasename,
    isValidIfsc, validateUploadFile,
    esc, openExternalUrl,

    // vendor
    listVendors, listVendorsPaged, searchVendors,
    createVendor, createVendorWithDocuments,
    extractBulk, submitStagedVendor,
    getVendor, patchVendor, deleteVendor, approveVendor,

    // history
    listVendorHistory, getHistoryEntry, revertHistory,
    listBankingHistory, listDocumentHistory, listContractHistory,

    // banking
    addBanking, listBanking, patchBanking, deleteBanking, setPrimaryBanking,

    // documents
    addDocument, extractDocument, uploadAndSaveDocument, appendDocumentFile,
    listDocuments, patchDocument, deleteDocument,

    // contracts
    addContract, extractContract, uploadAndSaveContract, appendContractFile,
    listContracts, patchContract, deleteContract,
  };
})();
