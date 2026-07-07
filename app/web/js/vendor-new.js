// ── Vendor Management · Onboarding wizard (extract-first flow) ──
//
// Phase 1 (Step 1):  POST /vendors/extract-bulk
//   Operator drops every compliance doc they have. We upload + run Claude
//   extraction without creating a vendor row. Returns a `staging_id` plus
//   suggested vendor / banking / documents / contracts fields that we
//   pre-fill into steps 2-5.
//
// Phase 2 (Step 6):  POST /vendors/submit-staged
//   Operator reviews + edits the pre-filled form. On submit we send the
//   whole payload back; backend commits everything in one transaction and
//   promotes the staged S3 objects under the vendor's supplier_code.
//
// State management:
//   The /submit-staged endpoint writes a `create` row into
//   vendor_master_history (and per-child history) with source='extraction'
//   so the audit trail shows what came from the model vs. what the
//   operator manually changed.

const { ipcRenderer } = require('electron');

setupBackArrow();
setupLogout('logoutBtn');
if (!requireAuth('purchase')) throw new Error('auth');

// ── Nav wiring ──
document.getElementById('backToPurchase').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-purchase', { from: 'vendor-new' }));
document.getElementById('backToVendorManagement').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-vendor-management', { from: 'vendor-new' }));
document.getElementById('backToList').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-existing-vendors', { from: 'vendor-new' }));
document.getElementById('navList').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-existing-vendors', { from: 'vendor-new' }));
document.getElementById('navBackVendorManagement').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-vendor-management', { from: 'vendor-new' }));
document.getElementById('navBackModules').addEventListener('click', () =>
  ipcRenderer.send('navigate-to-modules', { from: 'vendor-new' }));

// ── State ──
let currentStep = 1;
const TOTAL_STEPS = 6;

// Files staged for the extract call. Pre-extraction: an array of
// `{ file, doc_type, doc_number? }`. After extraction the entries also
// gain `{ s3_url, extracted, status }` so we can resubmit on Step 6 without
// re-uploading.
const filesQueue = [];

// Phase-1 result. Populated by runExtract(); consumed by setStep(N) handlers
// to hydrate steps 2-5 and again by submit() for /submit-staged.
let extraction = null;       // { staging_id, vendor_fields, banking_fields, documents, contracts, conflicts, failures }
let stagingId  = null;

// Banking rows. After extract these come from extraction.banking_fields; the
// operator can edit / mark primary / add or remove rows.
const bankingRows = [];

// Contract rows. Mirrors bankingRows.
const contractRows = [];

// Tracks which form fields were auto-filled by extraction so the UI can
// badge them. Cleared per-field when the user types — once they touch a
// field it's no longer "auto".
const autoFilled = new Set();

// Result snapshot after submission (for review pane).
let createResult = null;

// ── DOM refs ──
const stepsEl    = document.getElementById('wizardSteps');
const prevBtn    = document.getElementById('prevBtn');
const nextBtn    = document.getElementById('nextBtn');
const cancelBtn  = document.getElementById('cancelBtn');
const bankList   = document.getElementById('bankList');
const contractList = document.getElementById('contractList');
const fileList   = document.getElementById('fileList');
const uploader   = document.getElementById('uploader');
const fileInput  = document.getElementById('fileInput');
const reviewEl   = document.getElementById('reviewSummary');
const reviewRes  = document.getElementById('reviewResult');
const extractActions = document.getElementById('extractActions');
const extractRunBtn  = document.getElementById('extractRunBtn');
const extractSkipBtn = document.getElementById('extractSkipBtn');
const extractProgress = document.getElementById('extractProgress');
const extractCancelBtn = document.getElementById('extractCancelBtn');
const extractSummary  = document.getElementById('extractSummary');

// Live AbortController while an extraction is in flight. Captured at the
// top of runExtract() and cleared in the finally — `extractCancelBtn`
// fires `.abort()` on whichever controller is currently held.
let _extractAbort = null;

// ── Step transitions ──
function setStep(n) {
  if (n < 1 || n > TOTAL_STEPS) return;
  currentStep = n;
  for (let i = 1; i <= TOTAL_STEPS; i++) {
    const pane = document.getElementById(`pane${i}`);
    if (pane) pane.hidden = i !== n;
  }
  Array.from(stepsEl.children).forEach((li, idx) => {
    const stepNum = idx + 1;
    li.classList.toggle('is-active', stepNum === n);
    li.classList.toggle('is-done', stepNum < n);
  });
  prevBtn.disabled = n === 1;
  nextBtn.textContent = n === TOTAL_STEPS ? 'Submit vendor' : 'Next';
  // Step 1 can only advance after extraction has run OR the user has
  // explicitly chosen "Skip — fill manually".
  if (n === 1) {
    nextBtn.disabled = !(extraction || autoFilled.has('__skipped__'));
  } else {
    nextBtn.disabled = false;
  }
  if (n === 4) renderBankingRows();
  if (n === 5) renderContractRows();
  if (n === 6) renderReview();
}

prevBtn.addEventListener('click', () => setStep(currentStep - 1));
nextBtn.addEventListener('click', async () => {
  if (currentStep < TOTAL_STEPS) {
    if (!validateCurrentStep()) return;
    setStep(currentStep + 1);
  } else {
    await submit();
  }
});
cancelBtn.addEventListener('click', () => {
  if (createResult) {
    ipcRenderer.send('navigate-to-vendor-detail', { from: 'vendor-new', vendorId: createResult.vendor.vendor_id });
  } else if (confirm('Discard this vendor draft? Nothing has been saved yet.')) {
    ipcRenderer.send('navigate-to-existing-vendors', { from: 'vendor-new' });
  }
});

// ── Step validation ──
function validateCurrentStep() {
  if (currentStep === 2) {
    const name = document.getElementById('f-name').value.trim();
    if (!name || name.length < 2) {
      showToast('Vendor name is required (≥2 characters).', 'error');
      document.getElementById('f-name').focus();
      return false;
    }
    if (name.length > 512) {
      showToast('Name is too long (max 512 chars).', 'error');
      return false;
    }
  }
  if (currentStep === 3) {
    const fssai = document.getElementById('f-fssai_no').value.trim();
    if (fssai && !/^\d{14}$/.test(fssai)) { showToast('FSSAI must be 14 digits.', 'error'); return false; }
    const pan = document.getElementById('f-pan_no').value.trim();
    if (pan && pan.length !== 10) { showToast('PAN must be 10 characters.', 'error'); return false; }
    const gst = document.getElementById('f-gstn').value.trim();
    if (gst && gst.length !== 15) { showToast('GSTIN must be 15 characters.', 'error'); return false; }
    const cin = document.getElementById('f-cin_no').value.trim();
    if (cin && cin.length !== 21) { showToast('CIN must be 21 characters.', 'error'); return false; }
    const iec = document.getElementById('f-iec_no').value.trim();
    if (iec && !/^\d{10}$/.test(iec)) { showToast('IEC must be 10 digits.', 'error'); return false; }
  }
  if (currentStep === 4) {
    for (let i = 0; i < bankingRows.length; i++) {
      const r = bankingRows[i];
      if (!r.bank_name || !r.account_no || !r.account_name) {
        showToast(`Banking row ${i + 1}: name / account no / account name are required.`, 'error');
        return false;
      }
      if (r.account_no.length < 4) {
        showToast(`Banking row ${i + 1}: account number must be ≥4 chars.`, 'error');
        return false;
      }
      if (r.ifsc && !vendorApi.isValidIfsc(r.ifsc)) {
        showToast(`Banking row ${i + 1}: invalid IFSC.`, 'error');
        return false;
      }
    }
  }
  return true;
}

// ── Files queue (Step 1) ──
uploader.addEventListener('click', () => fileInput.click());
uploader.addEventListener('dragover', (e) => { e.preventDefault(); uploader.classList.add('drag-over'); });
uploader.addEventListener('dragleave', () => uploader.classList.remove('drag-over'));
uploader.addEventListener('drop', (e) => {
  e.preventDefault();
  uploader.classList.remove('drag-over');
  Array.from(e.dataTransfer.files).forEach(addFileToQueue);
});
fileInput.addEventListener('change', () => {
  Array.from(fileInput.files).forEach(addFileToQueue);
  fileInput.value = '';
});

function addFileToQueue(file) {
  if (extraction) {
    // After extraction has already run, the staged session is locked. The
    // operator would need to start over to add more files. Surface this
    // clearly rather than silently dropping the file.
    showToast('Extraction already ran. Reset to add more files.', 'warning');
    return;
  }
  if (filesQueue.length >= 20) {
    showToast('Up to 20 files per batch — submit this batch first.', 'warning');
    return;
  }
  const err = vendorApi.validateUploadFile(file);
  if (err) { showToast(err, 'error'); return; }
  filesQueue.push({ file, doc_type: '__auto__' });
  renderFileQueue();
}

function renderFileQueue() {
  if (!filesQueue.length) {
    fileList.innerHTML = '';
    extractActions.hidden = true;
    return;
  }
  fileList.innerHTML = filesQueue.map((f, i) => {
    const statusBadge = f.status === 'ok'
      ? `<span class="badge badge--success">extracted</span>`
      : f.status === 'failed'
        ? `<span class="badge badge--danger" title="${esc(f.error || '')}">failed</span>`
        : '';
    const docTypeSelect = extraction
      ? `<span class="file-row__type">${esc(f.doc_type === '__auto__' ? 'auto' : f.doc_type)}</span>`
      : `<select class="form-input form-input--sm doc-type-edit" data-idx="${i}">
          <option value="__auto__" ${f.doc_type === '__auto__' ? 'selected' : ''}>Auto-detect</option>
          ${vendorApi.ENUMS.DOC_TYPE.map(t =>
            `<option value="${t.code}" ${t.code === f.doc_type ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}
        </select>`;
    return `
      <div class="wizard-file-row" data-idx="${i}">
        <div class="file-meta" title="${esc(f.file.name)}">
          ${esc(f.file.name)}
          <small>${(f.file.size / 1024).toFixed(0)} KB</small>
        </div>
        ${docTypeSelect}
        ${statusBadge}
        ${extraction ? '' : `<button class="remove-btn" data-idx="${i}" type="button" title="Remove">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>`}
      </div>`;
  }).join('');

  fileList.querySelectorAll('.doc-type-edit').forEach(sel => {
    sel.addEventListener('change', () => { filesQueue[+sel.dataset.idx].doc_type = sel.value; });
  });
  fileList.querySelectorAll('.remove-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      filesQueue.splice(+btn.dataset.idx, 1);
      renderFileQueue();
    });
  });

  extractActions.hidden = !!extraction;   // hide once extraction has been run
}

// ── Phase 1: extract ──
extractRunBtn.addEventListener('click', runExtract);
extractSkipBtn.addEventListener('click', () => {
  // Reaching this means the user prefers to type. Mark the skip so Step 1
  // is "done" enough to advance, then jump straight to Basics.
  autoFilled.add('__skipped__');
  setStep(2);
});

async function runExtract() {
  if (!filesQueue.length) {
    showToast('Drop at least one file first.', 'warning');
    return;
  }
  extractActions.hidden = true;
  extractProgress.hidden = false;
  uploader.style.pointerEvents = 'none';
  uploader.style.opacity = '0.6';

  // New controller per run — lets the Cancel button abort the fetch.
  // Stash on a module-level var so the click handler can reach it.
  const controller = new AbortController();
  _extractAbort = controller;
  extractCancelBtn.disabled = false;
  extractCancelBtn.textContent = 'Cancel';

  try {
    const docsMeta = filesQueue.map(f => f.doc_type === '__auto__' ? {} : { doc_type: f.doc_type });
    const files    = filesQueue.map(f => f.file);
    const result   = await vendorApi.extractBulk(files, docsMeta, { signal: controller.signal });

    extraction = result;
    stagingId  = result.staging_id;

    // Wire per-file extraction status back into the queue so the badges
    // update in place.
    (result.documents || []).forEach((d, idx) => {
      if (!filesQueue[idx]) return;
      filesQueue[idx].status   = d.extraction_status || 'ok';
      filesQueue[idx].error    = d.extraction_error || null;
      filesQueue[idx].s3_url   = d.s3_url;
      filesQueue[idx].doc_type = d.doc_type || filesQueue[idx].doc_type;
      filesQueue[idx].extracted = d;
    });
    (result.failures || []).forEach(f => {
      // Failures are positionally aligned with `prepared` — they extend
      // beyond .documents only when an upload failed entirely.
    });

    // Lookups must be loaded before we can label-match dropdown values.
    // In practice they finish during the 15-30s Claude wait, but await
    // defensively so a slow cold start doesn't drop firm_status / etc.
    await _lookupsReady;

    hydrateFromExtraction(result);
    renderExtractSummary(result);
    renderFileQueue();

    showToast(`Extracted ${result.documents?.length || 0} document(s) — review the auto-filled form.`, 'success');
    setStep(2);
  } catch (err) {
    extractActions.hidden = false;
    // AbortError surfaces from a user-driven Cancel — clean message,
    // don't toast as a failure. The fetch was DOMException("AbortError")
    // (modern browsers) or the SDK's wrapper around it.
    const aborted = err?.name === 'AbortError' || /aborted/i.test(err?.message || '');
    if (aborted) {
      showToast('Extraction cancelled.', 'info');
    } else if (err.code === 'too_many_files')      showToast('Too many files — max 20 per batch.', 'error');
    else if (err.code === 'file_too_large')        showToast('A file exceeded 25 MB.', 'error');
    else if (err.code === 'mime_mismatch')         showToast('A file was rejected by MIME sniff.', 'error');
    else if (err.code === 'storage_unavailable')   showToast('Server S3 is offline — try again later.', 'error');
    else                                           showToast(`Extraction failed — ${err.message}`, 'error');
  } finally {
    _extractAbort = null;
    extractProgress.hidden = true;
    uploader.style.pointerEvents = '';
    uploader.style.opacity = '';
  }
}

// Wire the Cancel button — aborts whichever extraction is currently in
// flight. Disables itself immediately so a double-click doesn't fire
// the abort twice (no-op but spammy).
extractCancelBtn.addEventListener('click', () => {
  if (!_extractAbort) return;
  extractCancelBtn.disabled = true;
  extractCancelBtn.textContent = 'Cancelling…';
  _extractAbort.abort();
});

function renderExtractSummary(r) {
  const docs = r.documents || [];
  const okCount   = docs.filter(d => d.extraction_status === 'ok').length;
  const failCount = docs.filter(d => d.extraction_status !== 'ok').length + (r.failures?.length || 0);
  const conflicts = r.conflicts || [];

  extractSummary.hidden = false;
  extractSummary.innerHTML = `
    <div class="extract-summary__hdr">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
      Extracted ${okCount} of ${docs.length} document${docs.length === 1 ? '' : 's'}${failCount ? ` · ${failCount} failed` : ''}
    </div>
    <ul class="extract-summary__list">
      ${docs.map(d => `<li>
        <strong>${esc(d.doc_type || '—')}</strong>
        ${d.doc_number ? `<span class="mono">${esc(d.doc_number)}</span>` : ''}
        ${d.extraction_status !== 'ok' ? `<span class="badge badge--danger">${esc(d.extraction_status)}</span>` : ''}
      </li>`).join('')}
      ${(r.failures || []).map(f => `<li>
        <strong>${esc(f.doc_type || '—')}</strong> ${esc(f.filename || '')}
        <span class="badge badge--danger" title="${esc(f.error)}">upload failed</span>
      </li>`).join('')}
    </ul>
    ${conflicts.length ? `
      <div class="extract-conflicts">
        <h4>${conflicts.length} field${conflicts.length === 1 ? '' : 's'} need your choice</h4>
        <ul>
          ${conflicts.map((c, ci) => `<li data-conflict-idx="${ci}">
            <strong>${esc(c.field)}</strong> —
            ${(c.values_seen || []).map((v, vi) =>
              `<button class="btn btn--ghost btn--sm conflict-pick" data-field="${esc(c.field)}" data-value="${esc(v)}">
                ${esc(v)}${c.sources && c.sources[vi] ? ` <small>(${esc(c.sources[vi])})</small>` : ''}
              </button>`).join(' ')}
          </li>`).join('')}
        </ul>
      </div>
    ` : ''}
  `;

  extractSummary.querySelectorAll('.conflict-pick').forEach(btn => {
    btn.addEventListener('click', () => {
      const field = btn.dataset.field;
      const value = btn.dataset.value;
      const el = document.getElementById(`f-${field}`);
      if (el) {
        el.value = value;
        autoFilled.add(field);
        markAutoBadge(el);
      }
      btn.classList.add('is-picked');
      btn.closest('li').querySelectorAll('.conflict-pick').forEach(b => b.disabled = (b !== btn));
    });
  });
}

// ── Smart hydration ─────────────────────────────────────────────────────
//
// The vendor form has four input types we need to populate from the
// extraction payload:
//   1. <input>          — plain text / numeric. set .value.
//   2. <input type="date"> — needs YYYY-MM-DD. Claude already returns
//                            ISO per the system prompt; we normalise
//                            defensively for DD/MM/YYYY and other common
//                            Indian formats just in case.
//   3. <input type="checkbox"> — set .checked.
//   4. <select data-lookup="X"> — backed by lookup_value. The backend
//                            can't write lookup_id directly, so it sends
//                            us label hints under `<column>_label` keys.
//                            We resolve label → option.value (the lookup_id)
//                            by matching the option's text.
//
// `_label`-suffixed keys are routed to the matching lookup select.
// All other keys are routed to the element with id="f-{key}".


/** Find an option whose visible text matches `label` (case-insensitive,
 *  prefers exact match, falls back to substring) and return its value.
 *  Returns null if no option matches.
 */
function _resolveLookupLabel(selectEl, label) {
  if (!selectEl || !label) return null;
  const needle = String(label).trim().toLowerCase();
  if (!needle) return null;
  let exact = null;
  let contains = null;
  for (const opt of selectEl.options) {
    const text = (opt.textContent || '').trim().toLowerCase();
    if (!text) continue;
    if (text === needle) { exact = opt; break; }
    if (!contains && (text.includes(needle) || needle.includes(text))) {
      contains = opt;
    }
  }
  return (exact || contains)?.value || null;
}

/** Coerce common date formats to YYYY-MM-DD so <input type="date"> accepts
 *  the value. Empty / unparseable inputs return ''.
 */
function _normaliseISODate(v) {
  if (!v) return '';
  const s = String(v).trim();
  // Already ISO?
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  // DD/MM/YYYY or DD-MM-YYYY
  const dmy = s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})$/);
  if (dmy) {
    const [, d, m, y] = dmy;
    return `${y}-${m.padStart(2, '0')}-${d.padStart(2, '0')}`;
  }
  // Fallback: let JS try.
  const dt = new Date(s);
  if (!isNaN(dt.getTime())) {
    return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
  }
  return '';
}

/** Set a form field's value with type-appropriate coercion. Returns the
 *  element on success, null on miss (so caller can skip badge-marking).
 */
function _setFieldValue(elementId, value) {
  const el = document.getElementById(elementId);
  if (!el) return null;
  if (el.tagName === 'SELECT' && el.dataset.lookup) {
    // Label-match. If options haven't hydrated yet, defer once.
    const resolved = _resolveLookupLabel(el, value);
    if (resolved) {
      el.value = resolved;
      return el;
    }
    return null;   // no matching option; surface as no-op
  }
  if (el.type === 'checkbox') {
    el.checked = !!value;
    return el;
  }
  if (el.type === 'date') {
    const iso = _normaliseISODate(value);
    if (!iso) return null;
    el.value = iso;
    return el;
  }
  // Plain text / numeric / etc.
  el.value = value;
  return el;
}


// Apply the extracted suggestions to the form. Banking + contract rows are
// seeded into their own arrays so steps 4-5 can render multi-row UIs.
function hydrateFromExtraction(r) {
  const vendorFields = r.vendor_fields || {};

  // First pass: typed fields (text / checkbox / date). Keys without
  // a `_label` suffix go to id="f-{key}".
  Object.entries(vendorFields).forEach(([k, v]) => {
    if (v == null || v === '') return;
    if (k.endsWith('_label')) return;            // handled in the second pass

    const el = _setFieldValue(`f-${k}`, v);
    if (!el) return;

    autoFilled.add(k);
    markAutoBadge(el);
    // Once the operator types, the "auto" badge drops — signals they've
    // taken responsibility for that field. For SELECT, listen on 'change'
    // since 'input' doesn't fire on option pick in some browsers.
    const evt = (el.tagName === 'SELECT' || el.type === 'checkbox') ? 'change' : 'input';
    const drop = () => {
      autoFilled.delete(k);
      el.parentElement?.classList.remove('field--auto');
      el.removeEventListener(evt, drop);
    };
    el.addEventListener(evt, drop);
  });

  // Second pass: label hints for lookup-backed dropdowns. The key
  // `firm_status_id_label` resolves to the SELECT with id="f-firm_status_id"
  // (drop the `_label` suffix to recover the target element id).
  Object.entries(vendorFields).forEach(([k, v]) => {
    if (!k.endsWith('_label') || !v) return;
    const targetField = k.slice(0, -'_label'.length);    // e.g. "firm_status_id"
    const el = _setFieldValue(`f-${targetField}`, v);
    if (!el) return;

    autoFilled.add(targetField);
    markAutoBadge(el);
    const drop = () => {
      autoFilled.delete(targetField);
      el.parentElement?.classList.remove('field--auto');
      el.removeEventListener('change', drop);
    };
    el.addEventListener('change', drop);
  });

  (r.banking_fields || []).forEach(b => bankingRows.push({
    bank_name: b.bank_name || '',
    account_no: b.account_no || '',
    account_name: b.account_name || '',
    branch: b.branch || '',
    ifsc: b.ifsc || '',
    swift: b.swift || '',
    // Backend sends `account_type_label` (free text like "Current");
    // we resolve to the lookup_id after ACCOUNT_TYPE dropdown loads
    // — see renderBankingRows's hydrateLookups callback.
    account_type_id: b.account_type_id || '',
    account_type_label: b.account_type_label || '',
    is_primary: !!b.is_primary,
    is_active: true,
    valid_from: '',
    valid_to: '',
    _source: 'extraction',
  }));
  if (bankingRows.length && !bankingRows.some(r => r.is_primary)) {
    bankingRows[0].is_primary = true;
  }

  (r.contracts || []).forEach(c => contractRows.push({
    contract_type: c.contract_type || '',
    signed_date: c.signed_date || '',
    effective_from: c.effective_from || '',
    effective_to: c.effective_to || '',
    value_inr: c.value_inr ?? '',
    scoc_signed: false,
    auto_renew: false,
    s3_url: c.s3_url || '',
    _source: 'extraction',
  }));
}

function markAutoBadge(el) {
  const wrap = el.closest('.form-field');
  if (!wrap || wrap.classList.contains('field--auto')) return;
  wrap.classList.add('field--auto');
  const lbl = wrap.querySelector('.form-label');
  if (lbl && !lbl.querySelector('.extract-pill')) {
    lbl.insertAdjacentHTML('beforeend', ' <span class="extract-pill">auto</span>');
  }
}

// ── Banking rows ──
function blankBank() {
  return {
    bank_name: '', account_no: '', account_name: '',
    branch: '', ifsc: '', swift: '',
    account_type_id: '', is_primary: bankingRows.length === 0, is_active: true,
    valid_from: '', valid_to: '',
  };
}

function renderBankingRows() {
  if (!bankingRows.length) {
    bankList.innerHTML = `<div class="empty-state show" role="status" style="padding:18px">
      <div class="empty-state__title">No banking rows yet</div>
      <div class="empty-state__message">Optional, but required before SCM-Head approval.</div>
    </div>`;
    return;
  }
  bankList.innerHTML = bankingRows.map((r, i) => `
    <div class="wizard-bank-row${r._source === 'extraction' ? ' bank-row--auto' : ''}" data-idx="${i}">
      ${r._source === 'extraction' ? '<span class="extract-pill extract-pill--row">auto</span>' : ''}
      <div class="wizard-grid" style="width:100%;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))">
        <div class="form-field">
          <label class="form-label">Bank name *</label>
          <input class="form-input bank-edit" data-field="bank_name" value="${esc(r.bank_name)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Account no *</label>
          <input class="form-input mono bank-edit" data-field="account_no" value="${esc(r.account_no)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Account holder *</label>
          <input class="form-input bank-edit" data-field="account_name" value="${esc(r.account_name)}" />
        </div>
        <div class="form-field">
          <label class="form-label">IFSC</label>
          <input class="form-input mono bank-edit" data-field="ifsc" maxlength="11" value="${esc(r.ifsc)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Branch</label>
          <input class="form-input bank-edit" data-field="branch" value="${esc(r.branch)}" />
        </div>
        <div class="form-field">
          <label class="form-label">SWIFT</label>
          <input class="form-input mono bank-edit" data-field="swift" value="${esc(r.swift)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Account type</label>
          <select class="form-input bank-edit" data-field="account_type_id" data-lookup="ACCOUNT_TYPE"><option value="">—</option></select>
        </div>
        <div class="form-field" style="display:flex;align-items:center;gap:12px;padding-top:18px">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12.5px">
            <input type="radio" name="primaryBank" class="bank-primary" data-idx="${i}" ${r.is_primary ? 'checked' : ''} />
            Primary
          </label>
          <button class="btn btn--ghost btn--sm vendor-action-danger bank-remove" data-idx="${i}" type="button">Remove</button>
        </div>
      </div>
    </div>
  `).join('');

  bankList.querySelectorAll('.bank-edit').forEach(inp => {
    inp.addEventListener('input', () => {
      const row = inp.closest('.wizard-bank-row');
      const idx = +row.dataset.idx;
      bankingRows[idx][inp.dataset.field] = inp.value;
      // Operator touched an auto-filled row — drop the badge.
      if (bankingRows[idx]._source === 'extraction') {
        delete bankingRows[idx]._source;
        row.classList.remove('bank-row--auto');
        row.querySelector('.extract-pill--row')?.remove();
      }
    });
  });
  bankList.querySelectorAll('.bank-primary').forEach(rb => {
    rb.addEventListener('change', () => {
      const idx = +rb.dataset.idx;
      bankingRows.forEach((r, i) => { r.is_primary = i === idx; });
    });
  });
  bankList.querySelectorAll('.bank-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = +btn.dataset.idx;
      bankingRows.splice(idx, 1);
      if (!bankingRows.some(r => r.is_primary) && bankingRows.length) bankingRows[0].is_primary = true;
      renderBankingRows();
      hydrateLookups(bankList);
    });
  });

  // Apply previously-selected account_type_id after lookup hydration.
  // For extraction-seeded rows we get a free-text label ("Current",
  // "Savings", …) and need to label-match it against the loaded options
  // to recover the lookup_id.
  hydrateLookups(bankList).then(() => {
    bankList.querySelectorAll('.wizard-bank-row').forEach(row => {
      const idx = +row.dataset.idx;
      const sel = row.querySelector('select[data-field="account_type_id"]');
      if (!sel) return;
      const row_ = bankingRows[idx];
      if (row_.account_type_id) {
        sel.value = row_.account_type_id;
      } else if (row_.account_type_label) {
        const resolved = _resolveLookupLabel(sel, row_.account_type_label);
        if (resolved) {
          sel.value = resolved;
          row_.account_type_id = resolved;        // persist so submit picks it up
        }
      }
    });
  });
}

document.getElementById('bankAddBtn').addEventListener('click', () => {
  bankingRows.push(blankBank());
  renderBankingRows();
});

// ── Contract rows ──
function blankContract() {
  return {
    contract_type: '', signed_date: '',
    effective_from: '', effective_to: '',
    value_inr: '', scoc_signed: false, auto_renew: false,
    s3_url: '',
  };
}

function renderContractRows() {
  if (!contractRows.length) {
    contractList.innerHTML = `<div class="empty-state show" role="status" style="padding:18px">
      <div class="empty-state__title">No contracts attached</div>
      <div class="empty-state__message">Add rows if you have signed agreements, MSAs or NDAs to record.</div>
    </div>`;
    return;
  }
  const typeOpts = (val) => vendorApi.ENUMS.CONTRACT_TYPE.map(t =>
    `<option value="${t.code}" ${t.code === val ? 'selected' : ''}>${esc(t.label)}</option>`).join('');

  contractList.innerHTML = contractRows.map((c, i) => `
    <div class="wizard-bank-row${c._source === 'extraction' ? ' bank-row--auto' : ''}" data-idx="${i}">
      ${c._source === 'extraction' ? '<span class="extract-pill extract-pill--row">auto</span>' : ''}
      <div class="wizard-grid" style="width:100%;grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
        <div class="form-field">
          <label class="form-label">Type</label>
          <select class="form-input contract-edit" data-field="contract_type">
            <option value="">—</option>
            ${typeOpts(c.contract_type)}
          </select>
        </div>
        <div class="form-field">
          <label class="form-label">Signed date</label>
          <input class="form-input contract-edit" data-field="signed_date" type="date" value="${esc(c.signed_date)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Effective from</label>
          <input class="form-input contract-edit" data-field="effective_from" type="date" value="${esc(c.effective_from)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Effective to</label>
          <input class="form-input contract-edit" data-field="effective_to" type="date" value="${esc(c.effective_to)}" />
        </div>
        <div class="form-field">
          <label class="form-label">Value (₹)</label>
          <input class="form-input mono contract-edit" data-field="value_inr" type="number" step="any" value="${esc(c.value_inr)}" />
        </div>
        <div class="form-field" style="display:flex;align-items:center;gap:14px;padding-top:18px">
          <label style="display:flex;gap:6px;align-items:center;font-size:12.5px">
            <input type="checkbox" class="contract-edit" data-field="scoc_signed" ${c.scoc_signed ? 'checked' : ''} /> SCOC signed
          </label>
          <label style="display:flex;gap:6px;align-items:center;font-size:12.5px">
            <input type="checkbox" class="contract-edit" data-field="auto_renew" ${c.auto_renew ? 'checked' : ''} /> Auto-renew
          </label>
          <button class="btn btn--ghost btn--sm vendor-action-danger contract-remove" data-idx="${i}" type="button">Remove</button>
        </div>
      </div>
    </div>
  `).join('');

  contractList.querySelectorAll('.contract-edit').forEach(inp => {
    inp.addEventListener('change', () => {
      const row = inp.closest('.wizard-bank-row');
      const idx = +row.dataset.idx;
      if (inp.type === 'checkbox') contractRows[idx][inp.dataset.field] = inp.checked;
      else contractRows[idx][inp.dataset.field] = inp.value;
    });
    inp.addEventListener('input', () => {
      const row = inp.closest('.wizard-bank-row');
      if (row.classList.contains('bank-row--auto')) {
        const idx = +row.dataset.idx;
        delete contractRows[idx]._source;
        row.classList.remove('bank-row--auto');
        row.querySelector('.extract-pill--row')?.remove();
      }
    });
  });
  contractList.querySelectorAll('.contract-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      contractRows.splice(+btn.dataset.idx, 1);
      renderContractRows();
    });
  });
}

document.getElementById('contractAddBtn').addEventListener('click', () => {
  contractRows.push(blankContract());
  renderContractRows();
});

// ── Review summary ──
function renderReview() {
  const v = collectVendorBody();
  const compliance = ['fssai_no','pan_no','gstn','cin_no','iec_no','tin_tan','pollution_epr','brc_other']
    .filter(k => v[k]).map(k => `<div class="detail-row"><span class="lbl">${k.toUpperCase()}</span><span class="val mono">${esc(v[k])}</span></div>`).join('');

  const contacts = ['contact_person','designation','mobile','phone_company','email','website']
    .filter(k => v[k]).map(k => `<div class="detail-row"><span class="lbl">${k.replace(/_/g,' ')}</span><span class="val">${esc(v[k])}</span></div>`).join('');

  const addr = [v.address_line, v.city, v.state, v.pin_code].filter(Boolean).join(', ');
  const docCount = filesQueue.filter(f => f.status === 'ok').length;

  reviewEl.innerHTML = `
    <div class="detail-card">
      <h4 class="detail-card__title">Basics</h4>
      <div class="detail-row"><span class="lbl">Name</span><span class="val">${esc(v.name)}</span></div>
      <div class="detail-row"><span class="lbl">Status</span><span class="val">${esc(v.status)}</span></div>
      <div class="detail-row"><span class="lbl">Reg year</span><span class="val">${esc(v.supplier_reg_year || '—')}</span></div>
      <div class="detail-row"><span class="lbl">Address</span><span class="val">${esc(addr || '—')}</span></div>
    </div>
    <div class="detail-card">
      <h4 class="detail-card__title">Contact</h4>
      ${contacts || `<div class="detail-row"><span class="lbl">—</span><span class="val">No contact info</span></div>`}
    </div>
    <div class="detail-card">
      <h4 class="detail-card__title">Compliance</h4>
      ${compliance || `<div class="detail-row"><span class="lbl">—</span><span class="val">No identifiers</span></div>`}
      <div class="detail-row"><span class="lbl">MSME</span><span class="val">${v.is_msme ? 'Yes' : 'No'}</span></div>
    </div>
    <div class="detail-card">
      <h4 class="detail-card__title">Banking</h4>
      <div class="detail-row"><span class="lbl">Rows</span><span class="val">${bankingRows.length} (${bankingRows.filter(r => r.is_primary).length} primary)</span></div>
    </div>
    <div class="detail-card">
      <h4 class="detail-card__title">Documents</h4>
      <div class="detail-row"><span class="lbl">Extracted</span><span class="val">${docCount} / ${filesQueue.length}</span></div>
      ${filesQueue.length ? `<div class="detail-row"><span class="lbl">Types</span><span class="val">${esc(filesQueue.filter(f => f.status === 'ok').map(f => f.doc_type).join(', ') || '—')}</span></div>` : ''}
    </div>
    <div class="detail-card">
      <h4 class="detail-card__title">Contracts</h4>
      <div class="detail-row"><span class="lbl">Rows</span><span class="val">${contractRows.length}</span></div>
    </div>
  `;
}

// ── Submit ──
async function submit() {
  if (!validateCurrentStep()) return;
  if (createResult) {
    showToast('Vendor already created. Open detail to continue.', 'info');
    ipcRenderer.send('navigate-to-vendor-detail', { from: 'vendor-new', vendorId: createResult.vendor.vendor_id });
    return;
  }
  nextBtn.disabled = true;
  nextBtn.textContent = 'Submitting…';
  prevBtn.disabled = true;
  cancelBtn.disabled = true;

  const vendorBody = collectVendorBody();
  const docsForSubmit = filesQueue
    .filter(f => f.status === 'ok' && f.s3_url)
    .map(f => ({
      s3_url:     f.s3_url,
      doc_type:   f.doc_type,
      doc_number: f.extracted?.doc_number || null,
      issued_on:  f.extracted?.issued_on  || null,
      valid_from: f.extracted?.valid_from || null,
      valid_to:   f.extracted?.valid_to   || null,
    }));
  const contractsForSubmit = contractRows.map(c => ({
    s3_url:         c.s3_url || null,
    contract_type:  c.contract_type || null,
    signed_date:    c.signed_date || null,
    effective_from: c.effective_from || null,
    effective_to:   c.effective_to || null,
    value_inr:      c.value_inr === '' ? null : Number(c.value_inr),
    scoc_signed:    !!c.scoc_signed,
    auto_renew:     !!c.auto_renew,
  }));
  const bankingForSubmit = bankingRows.map(sanitizeBankBody);

  try {
    let result;
    if (stagingId) {
      // Phase-2 happy path — server promotes staged S3 objects.
      result = await vendorApi.submitStagedVendor({
        staging_id: stagingId,
        vendor:     vendorBody,
        banking:    bankingForSubmit,
        documents:  docsForSubmit,
        contracts:  contractsForSubmit,
      });
    } else {
      // Operator chose "Skip — fill manually" on Step 1, so we never staged
      // anything. Fall back to the legacy combined create path; banking is
      // posted per-row after the vendor exists.
      const created = await vendorApi.createVendorWithDocuments(vendorBody, [], []);
      result = { ...created, banking_results: [] };
      if (bankingForSubmit.length) {
        const promises = bankingForSubmit.map(r => vendorApi.addBanking(created.vendor.vendor_id, r));
        await Promise.allSettled(promises);
      }
    }
    createResult = result;
    showToast('Vendor created.', 'success');
    renderResultPanel(result);
  } catch (err) {
    showToast(`Couldn't create vendor — ${err.message}`, 'error');
    nextBtn.disabled = false;
    nextBtn.textContent = 'Submit vendor';
    prevBtn.disabled = false;
    cancelBtn.disabled = false;
  }
}

function sanitizeBankBody(r) {
  const body = { ...r };
  delete body._source;
  ['branch','ifsc','swift','account_type_id','valid_from','valid_to'].forEach(k => {
    if (body[k] === '' || body[k] == null) delete body[k];
  });
  return body;
}

function renderResultPanel(result) {
  reviewRes.hidden = false;
  reviewRes.innerHTML = `
    <div class="detail-card" style="margin-top:18px">
      <h4 class="detail-card__title">Created</h4>
      <div class="detail-row"><span class="lbl">Vendor ID</span><span class="val mono">${esc(result.vendor.vendor_id)}</span></div>
      <div class="detail-row"><span class="lbl">Supplier code</span><span class="val mono">${esc(result.vendor.supplier_code || '—')}</span></div>
      <div class="detail-row"><span class="lbl">Documents committed</span><span class="val">${(result.documents || []).length}</span></div>
      <div class="detail-row"><span class="lbl">Contracts committed</span><span class="val">${(result.contracts || []).length}</span></div>
      <div class="detail-row"><span class="lbl">Banking rows</span><span class="val">${(result.banking || []).length}</span></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn--secondary" id="resOpenList">Back to list</button>
      <button class="btn btn--primary" id="resOpenDetail">Open vendor</button>
    </div>
  `;

  document.getElementById('resOpenList').addEventListener('click', () =>
    ipcRenderer.send('navigate-to-existing-vendors', { from: 'vendor-new' }));
  document.getElementById('resOpenDetail').addEventListener('click', () =>
    ipcRenderer.send('navigate-to-vendor-detail', { from: 'vendor-new', vendorId: result.vendor.vendor_id }));

  nextBtn.disabled = true;
  nextBtn.textContent = 'Created';
  prevBtn.disabled = true;
  cancelBtn.disabled = false;
  cancelBtn.textContent = 'Open vendor';
}

// ── Body collection ──
function collectVendorBody() {
  const v = (id) => {
    const el = document.getElementById(`f-${id}`);
    if (!el) return null;
    if (el.type === 'checkbox') return el.checked;
    const val = el.value.trim();
    return val === '' ? null : val;
  };

  return {
    supplier_code:        v('supplier_code') || null,
    supplier_reg_year:    v('supplier_reg_year'),
    name:                 v('name'),
    status:               v('status') || 'active',

    supplier_type_id:     v('supplier_type_id'),
    firm_status_id:       v('firm_status_id'),
    business_type_id:     v('business_type_id'),
    category_code_id:     v('category_code_id'),
    sub_category:         v('sub_category'),
    local_os_id:          v('local_os_id'),
    msme_type_id:         v('msme_type_id'),
    scoc_status_id:       v('scoc_status_id'),
    kyc_status_id:        v('kyc_status_id'),
    doc_status_id:        v('doc_status_id'),

    core_business:        v('core_business'),
    contact_person:       v('contact_person'),
    designation:          v('designation'),
    phone_company:        v('phone_company'),
    mobile:               v('mobile'),
    email:                v('email'),
    website:              v('website'),
    address_line:         v('address_line'),
    state:                v('state'),
    city:                 v('city'),
    pin_code:             v('pin_code'),

    fssai_no:             v('fssai_no'),
    brc_other:            v('brc_other'),
    cin_no:               v('cin_no'),
    pan_no:               v('pan_no'),
    gstn:                 v('gstn'),
    iec_no:               v('iec_no'),
    pollution_epr:        v('pollution_epr'),
    tin_tan:              v('tin_tan'),

    is_msme:              !!v('is_msme'),
    msme_registration_date: v('msme_registration_date'),
    uam_udyam_no:         v('uam_udyam_no'),
    business_turnover_3y: v('business_turnover_3y'),
    capabilities:         v('capabilities'),
    remarks:              v('remarks'),
    reference:            v('reference'),
  };
}

// ── Lookup hydration ──
async function hydrateLookups(scope = document) {
  const selects = Array.from(scope.querySelectorAll('select[data-lookup]'));
  const types = [...new Set(selects.map(s => s.dataset.lookup))];
  const cache = await Promise.all(types.map(t => vendorApi.getLookups(t).then(rows => [t, rows])));
  const byType = Object.fromEntries(cache);

  selects.forEach(sel => {
    const rows = byType[sel.dataset.lookup] || [];
    if (sel.dataset.populated === '1') return;
    const current = sel.value;
    rows.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.lookup_id;
      opt.textContent = r.label || r.code;
      sel.appendChild(opt);
    });
    if (current) sel.value = current;
    sel.dataset.populated = '1';
  });
}

const esc = vendorApi.esc;

// ── Boot ──
// Capture the lookup-hydration promise at boot so the extraction step can
// await it before label-matching against the now-populated dropdowns.
// Without this, extraction that completes before lookups finish loading
// would silently fail to set firm_status / business_type / msme_type.
const _lookupsReady = hydrateLookups();
setStep(1);
