# Vendor Sourcing & RFQ Agent — Design

Date: 2026-11-02
Status: DRAFT — awaiting approval. Nothing is built yet.
Owner: Procurement / AI team (ai.1@candorfoods.in)

## 1. Intent

Procurement keeps two files on Google Drive: the list of vendors Candor already buys from, and the list of SKUs (raw materials / packaging) with their specifications. Today, finding an alternative supplier for a SKU and collecting quotes is manual: search, call, chase on WhatsApp, wait for email.

This agent does that loop automatically. For a SKU it finds 3–5 alternative vendors who supply the same item (preferring Navi Mumbai, then Mumbai, then the rest of Maharashtra, then the rest of India), gets their email, WhatsApp number and calling number, contacts them on all three channels with the specification, and collects the quotes into one comparison table that lands back on Drive.

Sarvam AI is the AI layer: its Voice Agents platform makes and holds the phone calls in English/Hindi/Marathi, and its chat model (Sarvam-105B) ranks vendor candidates and extracts structured quotes from email, WhatsApp and call transcripts.

## 2. Goals, non-goals, anti-goals

Goals
- Per SKU: 3–5 *new* vendors (not already in the existing-vendor list), ranked by location tier, each with at least one working contact channel.
- Every vendor gets the full spec on email + WhatsApp + a phone call, with sensible timing and follow-ups.
- Every reply (any channel, any language) becomes a structured quote row: price, unit, MOQ, lead time, GST, payment/delivery terms, validity.
- Output: one comparison sheet per run on Google Drive, plus a short summary to procurement.
- Full audit trail: what was sent, when, to whom, what came back, raw transcript/email kept.

Non-goals (this version)
- No negotiation. The agent asks for a quote; it does not haggle or accept.
- No PO creation, no vendor onboarding into `vendor_master`. Winning vendors are onboarded by a human through the existing vendor flow.
- No inbound "vendor calls us back" handling beyond logging a callback request.
- No UI in the ERP frontend in v1. Drive sheet + WhatsApp/email summary is the interface.

Anti-goals (would make this a failure even if it "works")
- Spamming: contacting vendors who clearly do not supply the item, or contacting the same vendor repeatedly for the same SKU.
- Harming the shared WhatsApp Business number the ERP uses for approvals/OTPs (quality-rating drop or ban from cold outreach).
- Sending a wrong or incomplete spec (vendors quote the wrong thing; the whole run is wasted).
- Misrepresenting the caller: the voice agent must say it is an automated assistant calling on behalf of Candor Foods.

## 3. Constraints (from the codebase and the business)

- Runs inside the existing ERP backend (FastAPI + asyncpg + PostgreSQL on RDS). It needs a public webhook URL for WhatsApp replies and Sarvam call results, so it cannot be a fire-and-forget script; it is a backend module plus a CLI kick-off script.
- Reuses what already exists: SMTP (Gmail app password) for mail, the Meta WhatsApp Cloud API client pattern (`customer_returns/services/wa_notify.py`, `sample/services/whatsapp_service.py`) and the shared inbound WhatsApp webhook, `fpdf2` for PDF, `rapidfuzz` for name matching, `openpyxl`/`pandas` for sheets.
- New dependencies: `sarvamai` (Python SDK), `google-api-python-client` + `google-auth` (Drive/Sheets via service account). `httpx` (already present) for Sarvam Voice Agents REST and Google Places.
- DB migrations are applied on RDS by the user, never by Claude.
- Calling hours: 10:00–18:00 IST, Mon–Sat. No calls on Sundays/holidays.
- Everything the agent sends must be reviewable: templates and prompts live in config files, not hard-coded.

## 4. End-to-end flow

```
Drive (vendors.xlsx, skus.xlsx)
        |  1. Intake
        v
 rfq_run / rfq_item  --> 2. Discovery (Places + listings + web) --> Sarvam-105B relevance rank
        |                                        |
        |                              dedupe vs existing vendors (rapidfuzz)
        v                                        v
 3. Shortlist tab on Drive  <-- tiering, contact enrichment
        |  (procurement approves — recommended gate)
        v
 4. Outreach: email (SMTP) + WhatsApp template (Cloud API) + Sarvam voice call
        |
        v
 5. Replies: IMAP poll | WhatsApp webhook | Sarvam call webhook --> Sarvam-105B JSON extraction --> rfq_quote
        |
        v
 6. Follow-ups (day 2 WhatsApp, day 1 call retry) --> 7. Quotes tab on Drive + summary to procurement
```

### 4.1 Intake
- Input files are Google Sheets (preferred) or `.xlsx` in a Drive folder shared read-only with a Google service account. `.xlsx` inputs are read via openpyxl; they are never written back.
- All output goes into ONE pre-created, user-owned Google Sheet ("RFQ Runs") shared with the service account as editor. The agent adds tabs per run; it never creates files. (My call: service accounts have no My Drive storage of their own, so creating files in a personal folder needs a Shared Drive or domain-wide delegation. One user-owned sheet avoids both.)
- Expected columns (builder adapts to the real files once shared):
  - Vendors: `vendor_name, city, state, phone, whatsapp, email, items_supplied` (free text).
  - SKUs: `sku_code, sku_name, category, specification (free text), unit, indicative_qty, target_vendor_count (default 5)`.
- A run is started by (my call, pending confirmation) a CLI command `python scripts/run_rfq.py --sku <code> [--all-pending]`; v2 can add a "Run" checkbox column in the sheet polled every 15 min.
- Spec text is normalised by Sarvam-105B into a structured spec (material, grade, size/dimensions, packaging, certifications, quantity) **and shown back to procurement in the shortlist sheet** so a wrong spec is caught before anyone is contacted.

### 4.2 Discovery (find 3–5 alternative vendors)
Sources, in order; stop when 8–10 candidates are found so 3–5 survive filtering:
1. Google Places API (Text Search, location-biased): query = `"<item> manufacturer OR supplier"` with bias circles for Navi Mumbai, Mumbai, Maharashtra. Gives name, address, phone, website, rating.
2. IndiaMART / JustDial / TradeIndia listings via an Apify actor or direct fetch (ToS risk, see section 8). Gives name, city, phone (often masked), product list.
3. Web search fallback for niche items.

Processing per candidate:
- Location tier: 1 = Navi Mumbai (incl. Taloja, Turbhe, Rabale MIDC, Panvel), 2 = Mumbai + Thane, 3 = rest of Maharashtra, 4 = rest of India. Derived from address / pincode.
- Relevance score 0–100 from Sarvam-105B given the structured spec and the candidate's listing text / website snippet ("does this vendor plausibly supply this exact item?"). Drop below 50.
- Dedupe against the existing-vendor sheet and `vendor_master` by fuzzy name + exact phone/email (rapidfuzz, threshold 90).
- Contact enrichment: fetch the vendor website / listing page and extract `email, phone(s)` with Sarvam-105B JSON output. Mobile (`+91 [6-9]xxxxxxxxx`) is the WhatsApp candidate; landline is the calling number; if only one mobile exists it is both.
- WhatsApp check: there is no API to test a number. The first (consented) WhatsApp send is the test: a Cloud API delivery status `failed` with error 131026 ("not a WhatsApp user") marks `whatsapp_verified = false`. Delivery `statuses` callbacks are ignored by the ERP today, so this is new webhook code.
- DND scrub (flag `RFQ_DND_SCRUB`): mobile numbers are checked against the NDNC/DND registry where a lookup is available through the telephony provider; DND-listed mobiles are not auto-called and get email only (plus WhatsApp after consent). Policy choice is open (section 10).
- Pick the top `target_vendor_count` by (tier, relevance, has_contact).

### 4.3 Approval gate (my recommendation; user may switch it off)
The shortlist is written to a `Shortlist` tab in the "RFQ Runs" sheet and a WhatsApp/email note goes to procurement. Outreach starts when the row's `approve` checkbox is ticked (Sheets API poll every 5 min) or immediately if `RFQ_AUTO_OUTREACH=true`. Reason: this is outbound contact to strangers in Candor's name; one bad run damages reputation and the WhatsApp number.

### 4.4 Outreach (per approved vendor)
WhatsApp is consent-first (my call, see section 9): Meta requires opt-in for business-initiated messages, and a cold "send us a quote" template to a stranger is likely to be re-classified as marketing at review and to hurt the number's quality rating. So the first touches are email and the call; the voice agent asks "may we WhatsApp you the spec?", and a yes (or any WhatsApp/email reply from the vendor) unlocks the WhatsApp channel. If the business explicitly wants WhatsApp as a cold first touch, that is a config flip (`RFQ_WA_COLD_FIRST=true`) with the risk accepted.

Sequence (IST):

| Day | Action |
|-----|--------|
| 0, on approval | Email RFQ |
| 0, +1 h, within calling hours | Sarvam voice call (attempt 1); asks for WhatsApp consent |
| 0, on consent | WhatsApp template with spec PDF |
| 1 | Call attempt 2 if no answer / busy |
| 2 | Follow-up on the best unlocked channel (WhatsApp if open, else email) |
| 5 | Run closes; vendor marked `no_response` |

All steps are rows in `rfq_job` (due_at, kind, vendor_id, lease_until, attempts). A ticker loop in the app lifespan (same pattern as `promote_reminder_loop`) claims due rows with `SELECT ... FOR UPDATE SKIP LOCKED`, so a second instance or a restart never double-sends. The ticker can also be driven by an internal `POST /sourcing/tick` endpoint from an external cron if the backend is ever moved to a non-persistent host.

Email: from a dedicated sourcing mailbox (e.g. `sourcing@candorfoods.in`, app password like the existing SMTP account), subject `RFQ <run_id>-<vendor_no>: <sku_name>`, body with spec table, quantity, required fields (price/unit, MOQ, lead time, GST, payment terms, validity), and a one-page PDF spec attachment (fpdf2). The reference in the subject is how replies are matched.

WhatsApp: one approved **utility** template (e.g. `rfq_request_v1`) with variables {sku_name, spec_summary, qty, rfq_ref, reply instructions}. Body is short; the full spec PDF goes as a document header. A reply opens the 24-hour session; the agent may send one free-form clarification asking for missing fields, then stops.

Voice call (Sarvam Voice Agents):
- One agent ("Candor Sourcing Assistant") built in the Sarvam dashboard, versioned, with a telephony connection (Sarvam's Exotel integration) and an Indian number.
- Input variables per call: `vendor_name, sku_name, spec_summary, qty, unit, rfq_ref, callback_email, callback_whatsapp`.
- Script: identify as an automated assistant from Candor Foods, confirm they supply the item, read the spec summary, ask price per unit, MOQ, lead time, GST, delivery terms, ask them to send a written quote to the email/WhatsApp, thank and end. Starts in English; dynamic switch to Hindi/Marathi allowed.
- Output variables (extracted by Sarvam post-call): `supplies_item (yes/no/unsure), price, price_unit, moq, lead_time_days, gst_pct, payment_terms, wants_callback, best_contact_channel, do_not_call (yes/no)`.
- Placed via `POST https://apps.sarvam.ai/api/outbounds/v1/orgs/{org}/workspaces/{ws}/outbounds` with `agent_variables` and `webhook_config.url = <PUBLIC_BACKEND_URL>/webhooks/sarvam/rfq-call`, `metadata = {rfq_vendor_id}`.
- The result arrives on the webhook: status (connected / no_answer / busy / failed), duration, `final_agent_variables`, `interaction_transcript` (English text per turn). Stored raw, then passed through the same quote extractor as the other channels.

### 4.5 Reply capture and quote extraction
- Email: IMAP poll of the sourcing mailbox every 5 min (new code; nothing in `app/` does IMAP today; the sourcing mailbox gets its own `RFQ_SMTP_*` / `RFQ_IMAP_*` settings separate from `settings.SMTP_*`); match by `RFQ <ref>` in subject or by sender address against outreach rows; attachments (PDF/XLSX/images) are text-extracted (PDF to text; images/scans via Sarvam Vision OCR) and included.
- WhatsApp: the sourcing number is a second phone number on the SAME WABA / Meta app, so it shares the existing `/whatsapp/webhook`, verify token and app secret. The webhook gets a first-stage branch on `metadata.phone_number_id`: events for the sourcing number go to `sourcing.handle_inbound` and are NOT relayed to the maintenance bot (today `sample/router.py` relays every inbound to `MAINTENANCE_FORWARD_URL` before routing; vendor replies must be excluded or they become maintenance tickets). Image/document replies need new parsing + media download (the current `extract_messages` sets text to empty for media). The send helpers take the sender `phone_number_id` as a parameter instead of reading one global env var.
- Call: Sarvam webhook handler with a shared-secret check (token in the URL or a header), idempotent on `attempt_id`.
- Extraction: Sarvam-105B `chat.completions` with `response_format = json_schema` for the `Quote` schema; non-English text is translated first with `sarvam-translate:v1`. Each quote row carries `confidence` and the source reply id. Low-confidence or incomplete quotes trigger one automated clarification on the same channel (only once).

### 4.6 Output
- "RFQ Runs" sheet: per run, tabs `Spec` (normalised spec shown back), `Shortlist`, `Outreach log`, `Quotes` (comparison: vendor, tier, city, price, unit, MOQ, lead time, GST, payment terms, validity, channel, confidence, link to raw).
- Summary message to procurement (WhatsApp + email) when the run closes or when 3 quotes are in, whichever comes first.
- Everything also lives in Postgres so the ERP can use it later (a sourcing page in the frontend is a natural v2).

## 5. Data model (new tables, prefix `rfq_`)

- `rfq_run` — id, started_by, source_file_ids, status (intake / discovery / awaiting_approval / outreach / closed), created_at, closed_at.
- `rfq_item` — id, run_id, sku_code, sku_name, spec_raw, spec_json, qty, unit, target_vendor_count.
- `rfq_vendor` — id, item_id, name, source (places / indiamart / justdial / web), city, state, pincode, tier (1–4), address, website, email, phone_call, phone_whatsapp, whatsapp_verified (null / true / false), relevance, existing_vendor_id (nullable), approved (bool), status (shortlisted / contacted / quoted / no_response / declined / do_not_contact).
- `rfq_outreach` — id, vendor_id, channel (email / whatsapp / call), attempt, scheduled_at, sent_at, external_id (SMTP message-id / wamid / Sarvam attempt_id), status, error, payload_json.
- `rfq_reply` — id, vendor_id, channel, received_at, external_id, raw_text, raw_json (transcript / email headers), attachments (S3 keys), language.
- `rfq_quote` — id, vendor_id, reply_id, price numeric, currency, price_unit, moq, lead_time_days, gst_pct, payment_terms, delivery_terms, validity_until, notes, confidence, extracted_json.
- `rfq_do_not_contact` — phone / email, reason, created_at (honoured forever across runs).
- `rfq_job` — id, kind (email / call / wa / followup / close), vendor_id, due_at, lease_until, attempts, status, last_error (the scheduler queue).

## 6. Code layout (backend)

```
app/modules/sourcing/
  router.py            # /sourcing/runs, /sourcing/runs/{id}/approve, status
  webhooks.py          # /webhooks/sarvam/rfq-call ; hook into the shared WA inbound
  schemas.py
  services/
    drive.py           # service-account Drive/Sheets read + write-back
    intake.py          # parse sheets -> rfq_run/rfq_item; spec normalisation
    discovery.py       # Places / listings / web -> candidates
    enrich.py          # contact extraction, tiering, dedupe
    outreach_email.py  # SMTP + PDF
    outreach_wa.py     # WABA template + free-form (same pattern as wa_notify.py)
    outreach_call.py   # Sarvam instant-outbound client
    inbound_email.py   # IMAP poller
    inbound_wa.py      # reply handling
    extract.py         # Sarvam-105B JSON extraction + translate
    scheduler.py       # follow-up timeline (asyncio loop like promote_reminder_loop)
    report.py          # Quotes tab + summary
  prompts/             # agent script, extraction prompts, message templates (editable)
app/db/1xx_sourcing_rfq.sql
scripts/run_rfq.py     # CLI kick-off
```

Config (env): `SARVAM_API_KEY, SARVAM_ORG_ID, SARVAM_WORKSPACE_ID, SARVAM_RFQ_APP_ID, SARVAM_RFQ_APP_VERSION, SARVAM_CONNECTION_ID, SARVAM_AGENT_PHONE, SARVAM_WEBHOOK_SECRET, GOOGLE_SA_JSON, RFQ_DRIVE_FOLDER_ID, GOOGLE_PLACES_API_KEY, APIFY_TOKEN (optional), RFQ_SMTP_* / RFQ_IMAP_*, RFQ_WABA_PHONE_NUMBER_ID (separate number recommended), RFQ_AUTO_OUTREACH, RFQ_CALL_WINDOW`.

## 7. Phases / milestones

0. **Accounts (user-side, blocks everything):** Sarvam org/workspace + API key, with the AI team given dashboard access to the workspace (the voice agent, its output variables and the telephony connection are edited there; `prompts/agent_script.md` in the repo is a mirror of the dashboard script, kept in sync by hand); Google service account + the user-owned "RFQ Runs" sheet shared with it; Google Places API key; a second phone number on the existing WABA + utility template submitted for approval; sourcing mailbox with app password + IMAP enabled; the HTTPS host that already serves the Meta webhook also registered as the Sarvam webhook URL.
1. **Intake + discovery, no outreach.** Run on 2–3 real SKUs, review the shortlist with procurement, tune relevance/tiering. Exit: shortlist quality accepted.
2. **Email + WhatsApp outreach + reply capture + extraction + Quotes tab.** Exit: a real quote round-trips on both channels.
3. **Voice calls via Sarvam** + webhook + extraction from transcripts. Exit: a real call yields a quote row.
4. **Scheduler, follow-ups, do-not-contact, summary notifications, hardening** (retries, idempotency, cost caps per run).

## 8. Risks and how the design handles them

- **WhatsApp number health:** cold outreach from the ERP's shared WABA number risks its quality rating. Use a separate phone number on the same WABA for sourcing (`RFQ_WABA_PHONE_NUMBER_ID`); utility template only; hard cap of N first-contacts per day.
- **TRAI / DND:** most listing numbers are personal mobiles and some are DND-registered. The agent discloses it is automated; calling hours are enforced; any "don't call" goes to `rfq_do_not_contact`; an optional DND scrub skips auto-calls to DND-listed mobiles. The policy choice (scrub, landline-only, or accept) is open in section 10.
- **Meta opt-in policy:** WhatsApp is consent-first by default (section 4.4).
- **Single-instance assumptions:** scheduler work is leased from `rfq_job` rows, so restarts and a second instance cannot double-send.
- **Listing-site scraping (IndiaMART / JustDial) ToS:** phase 1 starts with Google Places (licensed API) + web; listing scrapers are optional and behind a flag.
- **Wrong spec sent:** the normalised spec is shown back before outreach (gate).
- **Cost control:** per-run caps on Places queries, LLM calls and call minutes; a dry-run mode that logs what would be sent.
- **Duplicate contact:** unique (vendor phone/email, sku) per 30 days.

## 9. Calls I made (check these while reading)

- Backend module + CLI, not a standalone script (webhooks need a running server).
- Approval gate before outreach is ON by default.
- Sarvam Voice Agents platform handles telephony (via its Exotel connection) rather than building our own Twilio/Exotel + Saaras/Bulbul pipeline. Far less to build and maintain.
- Sarvam-105B for ranking/extraction; Claude stays available for web-search-style discovery if Sarvam alone is weak there.
- Google Places as the primary discovery source; listing sites optional.
- Dedicated sourcing mailbox; a second number on the existing WABA (same webhook, same Meta app).
- WhatsApp is consent-first: email + call open, WhatsApp after a yes. Cold WhatsApp is a config flip.
- Day 0 / 2 / 5 follow-up timeline; two call attempts; all steps are leased `rfq_job` rows.
- Output lives in one user-owned "RFQ Runs" Google Sheet; the agent never creates Drive files.
- DND scrub exists behind a flag; default policy is "skip auto-calls to DND mobiles" until told otherwise.

## 10. Open questions for procurement / the user

Most important first. Each answer changes what gets built.

1. **WhatsApp as first touch or after consent?** Default here is consent-first (email + call open, WhatsApp after a yes). If WhatsApp must be a cold first touch, say so and accept the number-quality risk.
2. **DND policy for calls:** skip auto-calls to DND-registered mobiles (default), landline-only for them, or call everyone and accept the risk?
3. **Approve the shortlist before outreach, or fully automatic?** Default: approve.
4. **The two Drive files:** links, who owns the folder, and are they native Google Sheets or `.xlsx`? Also, who will own the "RFQ Runs" output sheet?
5. **Where does the backend run in production** (Render, EC2, App Runner)? Needed for the Sarvam webhook URL; the design works on any of them.
6. **Sarvam dashboard access** for the AI team, or every agent-script change goes through you? (Changes Phase 3 effort.)
7. **How should a run start:** CLI, a checkbox in the sheet, or an ERP button? Default: CLI first.
8. **Call-agent languages** (English / Hindi / Marathi?) and voice / persona name.
9. **Quotes to the sheet only, or also into ERP vendor tables?** Default: sheet + `rfq_*` tables, not `vendor_master`.
10. **One SKU per run or the whole sheet?** What quantity to quote against?
11. **IndiaMART buyer account** we may use for "Get Quotes" instead of scraping?
