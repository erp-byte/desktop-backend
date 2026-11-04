# Transfer request notifications (WhatsApp + email) and holds

Decided 2026-10-17. Inter-unit transfer requests tell the next person by email and
WhatsApp, and the store and the inventory managers can answer from the message. The
store and the inventory managers can also put a request on hold, with a reason.

Code: `app/modules/transfer/services/request_notify.py` (who and what),
`request_wa_reply.py` (WhatsApp taps and the reason for a hold or reject),
`request_action.py` (making the decision as the person who acted),
`request_action_token.py` (signed email links),
`app/modules/transfer/request_email_router.py` (the page the email links open),
`request_decision_service.py` (accept / hold / reject, approve / hold / reject).
Migrations: `app/db/126_transfer_request_notifications.sql`,
`app/db/127_transfer_request_holds.sql`.

## The request flow

Pending -> Accepted (the supplying store) -> Approved (an inventory manager) ->
Transferred (Scan & send). Either step may reject instead (with a reason), or hold:

- **Store hold** (on a Pending request, by the supplying store): the request stays
  **Pending**, marked as held by the store, with the reason. The store can still Accept
  or Reject it, but not hold it again. The person who raised it can still edit or delete
  it; an edit takes the hold off and the store is told about the request again.
- **Manager hold** (on an Accepted request, by an inventory manager): the request becomes
  **On Hold**, with the reason. An inventory manager can still Approve or Reject it, but
  not hold it again. The usual rule applies: nobody approves, holds or rejects a request
  they raised or accepted. An On Hold request cannot be edited or deleted, and it is not
  Ready to Send.
- Once a held request is accepted, approved or rejected, the hold's reason stays on the
  request as history.

ERP endpoints: `POST /api/v1/transfer/requests/{id}/store-hold {reason}` (accept
permission) and `POST /api/v1/transfer/requests/{id}/hold {reason}` (approve permission).

## Who gets what

| When | Who | Email | WhatsApp template (buttons) |
|---|---|---|---|
| A request is raised (or a store-held request is edited, or its supplying site changes) | The store of the supplying (From) site: active users who hold `transfer/requests/accept` and whose warehouse access covers the From site (a user with no warehouse lock covers every site) | One mail each, with **Accept** / **Hold** / **Reject** links | `interunit_transfer_request_store` (**Accept**, **Hold**) |
| The store accepts (also a store-held request) | The inventory managers: active users who hold `transfer/requests/approve` | One mail each, with **Approve** / **Hold** / **Reject** links | `interunit_request_approval` (**Approve**, **Hold**, **Reject**) |
| The store holds | The requester **and** the inventory managers (never the store user who held it) | Yes, no buttons | `interunit_transfer_request_held` |
| An inventory manager approves | The requester (the request's `created_by`) | Yes, no buttons | `interunit_request_decision`: "approved" |
| The store or a manager rejects | The requester, with who and the reason | Yes, no buttons | `interunit_request_decision`: "rejected" |
| An inventory manager holds | The requester, with who and the reason | Yes, no buttons | `interunit_request_decision`: "put on hold" |
| Scan & send saves the first box (the transfer is created) | The requester, with the challan and vehicle numbers | Yes, no buttons | `interunit_request_decision`: "scanned and sent" |

- Nobody gets a message asking them to decide a request they raised, and the managers'
  messages skip the store user who accepted or held it.
- Admins are not messaged for being admins. Only people who hold the permission through
  a role are (the same rule as floor requisitions and job cards).
- People are named by their full name in the ERP when it is known, otherwise by their
  email. Phone numbers and emails come from the ERP user (Admin screen).
- The store's WhatsApp message has no Reject button (its template has none): the store
  rejects in the ERP or from the email.

## The WhatsApp templates (approved in Meta, names fixed in the code)

All four are Utility templates in English (`en`) with positional variables. Each has a
text header whose `{{1}}` is the request number. The template names and the language are
written in the code (`request_notify.py`); there are no settings for them.

| Template | Body variables, in order | Quick replies (index: text, payload) |
|---|---|---|
| `interunit_transfer_request_store` | 1 request no, 2 from, 3 to, 4 items, 5 requested by, 6 requested at, 7 reason | 0: Accept `trq:accept:<id>`, 1: Hold `trq:store_hold:<id>` |
| `interunit_request_approval` | 1 request no, 2 from, 3 to, 4 items, 5 requested by, 6 accepted by, 7 accepted at | 0: Approve `trq:approve:<id>`, 1: Hold `trq:hold:<id>`, 2: Reject `trq:reject:<id>` |
| `interunit_transfer_request_held` | 1 request no, 2 from, 3 to, 4 items, 5 requested by, 6 held by, 7 held at, 8 reason | none |
| `interunit_request_decision` | 1 request no, 2 from, 3 to, 4 what happened, 5 by whom, 6 when, 7 remarks | none |

- Items read like `Cashew W320 (10), Almond 25kg (4) +2 more` (the quantity in brackets;
  items that do not fit are counted).
- Times read like `01 Oct 2026, 11:20`, as the ERP shows them.
- In the decision template, {{4}} is `approved`, `rejected`, `put on hold` or
  `scanned and sent`. {{7}} is the reason for a reject or a hold, `-` when there is
  none, and `Challan <no>, vehicle <no>` for a dispatch.
- The ERP sets each quick-reply button's payload when it sends, so a tap names its
  decision and request.
- Meta refuses a blank variable, or one with a new line, a tab or 4 or more spaces in a
  row, so the ERP collapses spaces and sends `-` for anything empty. Each variable is
  also cut to a safe length.

## Answering from WhatsApp

- **Accept / Approve**: the decision is made at once, as the ERP user whose phone number
  tapped. The reply says what happened.
- **Hold / Reject**: the bot replies asking for the reason. The next text message from
  that number **within 30 minutes** is taken as the reason, and the hold or reject is
  made with it. A reply that quotes the bot's question, or the request's message, counts
  too. A message after 30 minutes is not taken as the reason (tap again). Tapping another
  button in between cancels the waiting hold or reject.
- The ERP's checks apply exactly as in the ERP: the role permission, the store's
  warehouse lock (only the supplying site's store), nobody deciding their own request,
  and the request still being at that step (a held request cannot be held again).
- If the request has already moved on (decided in the ERP, or by someone else), nothing
  is done and the reply says what state it is in.
- A number that is not an active ERP user gets: "Sorry, this WhatsApp number is not
  registered to an ERP user, so nothing was done."

## Answering from email

- **Accept / Approve** opens a page showing the request with a **Confirm** button.
- **Hold / Reject** open a page with a box for the reason.
- Nothing changes until the button on that page is pressed, so a mail scanner that opens
  links cannot decide a request.
- Each link is signed for the person it was sent to and works for 14 days. Once the
  request has moved on, the link only shows its state and does nothing.

## Settings

There are no new settings: the template names and language are fixed in the code. The
notices use the existing ones:

- Email: `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, `SMTP_APP_PASSWORD` (no `SMTP_HOST`
  means no email), `PUBLIC_BACKEND_URL` (base of the email links, as for customer
  returns), `WEB_APP_URL` (the "Open it in the ERP" link), `JWT_SECRET` (signs the links).
- WhatsApp: `WHATSAPP_ENABLED`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
  `WHATSAPP_GRAPH_BASE`.

**Set `WHATSAPP_APP_SECRET`** if it is not set already. These buttons change requests,
and without it the webhook accepts a forged message (the server warns about this at
startup).

## What to configure

1. Apply migrations 126 and 127 (`scripts/migrate.py` runs them after 125). 127 must run
   before this backend is deployed: every request read selects its two columns. Without
   126, everything still works except Hold and Reject on WhatsApp, which then answer
   "please do it in the ERP".
2. Check that store users, inventory managers and requesters have their phone numbers
   and emails in the ERP, and that store users have their warehouse set. A store user
   with no warehouse set can answer for every site, so they are told about every request.

## Notes

- Notices are sent in the background after the action has been saved, and never fail or
  undo it. Every send and skip is logged with `[trq-notify]` (WhatsApp replies with
  `[trq-wa]`).
- A notice asking the store or the managers to act (or the store-hold notice) is not sent
  if the request has already moved past that step by the time it goes out.
- A transfer created from a request through the dispatch form
  (`POST /api/v1/transfer/transfers`) does not send the "scanned and sent" notice. Only
  Scan & send does.
- If every box is taken off a request's transfer, or the transfer is deleted, the request
  goes back to Approved; scanning it again creates a new transfer and sends a new
  "scanned and sent" notice.
