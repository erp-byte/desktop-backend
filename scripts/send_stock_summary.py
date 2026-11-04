"""Email stock summary images to every inventory_manager and admin, plus EXTRA_RECIPIENTS.

COLD STORAGE: one image per category (Dates, Other than Dates), every cold store
in it except A-185 Cold (an A185 floor, shown in the A185 warehouse image), in
the layout of the cold storage team's summary sheet. Built from the
Savla/Rishi report that was loaded into Stock Take, because only the report
carries the group / sub group and the purchase rates behind Avg Rate and Value.

WAREHOUSES: one image per warehouse, from Stock Take's current stock (the same
figures as the Stock Take page), by floor > category. Kg only: Stock Take holds
no rates, so there is no value to show.

Run from the repo root. --dry-run renders the images and lists the recipients
without sending; --to sends only to the addresses given (a test to yourself).

    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\send_stock_summary.py --dry-run
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\send_stock_summary.py --to you@candorfoods.in
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\send_stock_summary.py
"""
from __future__ import annotations

import argparse
import asyncio
import re
import smtplib
import ssl
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path

import asyncpg
import openpyxl
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.core.mail_identity import Module, SubjectPolicy, stamp  # noqa: E402
from app.modules.production.services.wa_notify import role_holders  # noqa: E402
from app.modules.stock_take import floors as F  # noqa: E402
from app.modules.stock_take.services.latest_stock_service import fetch_stock_by_place  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ROLES = ("inventory_manager", "admin")
#: Always sent the summary as well, whatever their ERP roles (added 2026-10-26).
EXTRA_RECIPIENTS = (
    "rakesh@candorfoods.in",
    "prashant.pal@candorfoods.in",
    "rmpatil@candorfoods.in",
    "satyendra@candorfoods.in",
)
#: The newest Savla/Rishi report dropped in backend/, unless --report names one.
DEFAULT_REPORT = max(ROOT.glob("Savla_Rishi_Inventory_*.xlsx"), key=lambda p: p.stat().st_mtime, default=None)
#: The report's stores in the order its own summary sheet lists them.
COLD_ORDER = ["Rishi", "Savla", "Savla Bond", "Eskimo"]
#: In the report but not part of the cold storage summary: A-185 Cold is a floor
#: of A185, counted on the floor app, and already in the A185 warehouse image.
COLD_SKIP = {"A-185 Cold"}
CATEGORIES = [("Dates", "Dates"), ("Other than dates", "Other than Dates")]


# ── numbers ──────────────────────────────────────────────────────────────────

def inr(v: float, dec: int = 0) -> str:
    """Indian grouping, as the summary sheet shows it: 30536890 -> 3,05,36,890; 0 -> '-'."""
    if round(v, dec) == 0:
        return "-"
    whole, _, frac = f"{abs(v):.{dec}f}".partition(".")
    if len(whole) > 3:
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", whole[:-3])
        whole = f"{head},{whole[-3:]}"
    return ("-" if v < 0 else "") + whole + (f".{frac}" if frac else "")


# ── drawing ──────────────────────────────────────────────────────────────────

SCALE = 2                       # rendered at 2x so the text stays sharp on a phone
ROW_H = 24
PAD = 6
WIDTHS = [190, 215, 260, 140, 130, 175]
COLD_ALIGN = ["l", "l", "l", "r", "c", "r"]       # rates centred, as the sheet has them
WAREHOUSE_ALIGN = ["l", "l", "c", "r", "r", "r"]
PEACH, GREEN, BLUE = (248, 203, 173), (226, 239, 218), (217, 225, 242)
WHITE, INK, GRID = (255, 255, 255), (0, 0, 0), (110, 110, 110)
_fonts: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(bold: bool, size: int = 15):
    key = (bold, size)
    if key not in _fonts:
        for name in (("calibrib.ttf", "arialbd.ttf") if bold else ("calibri.ttf", "arial.ttf")):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                _fonts[key] = ImageFont.truetype(str(path), size * SCALE)
                break
        else:
            _fonts[key] = ImageFont.load_default(size=size * SCALE)
    return _fonts[key]


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def render(rows: list[tuple[str, list[str]]], path: Path, align: list[str]) -> None:
    """rows: (kind, six cells). kind is title | summary | head | data | total."""
    W = sum(WIDTHS) * SCALE
    img = Image.new("RGB", (W + SCALE, len(rows) * ROW_H * SCALE + SCALE), WHITE)
    d = ImageDraw.Draw(img)
    xs = [sum(WIDTHS[:i]) * SCALE for i in range(len(WIDTHS) + 1)]
    for r, (kind, cells) in enumerate(rows):
        y0, y1 = r * ROW_H * SCALE, (r + 1) * ROW_H * SCALE
        for c, text in enumerate(cells):
            fill = {"summary": GREEN, "head": BLUE, "total": BLUE}.get(kind, WHITE)
            if kind == "title" and 2 <= c <= 4:
                fill = PEACH
            d.rectangle([xs[c], y0, xs[c + 1], y1], fill=fill)
            bold = kind != "data" or c < 2
            font = _font(bold, 18 if kind == "title" and c == 2 else 15)
            text = _fit(d, str(text or ""), font, xs[c + 1] - xs[c] - 2 * PAD * SCALE)
            side = "c" if kind in ("title", "head") or (kind == "summary" and c < 3) else align[c]
            tw = d.textlength(text, font=font)
            x = {"l": xs[c] + PAD * SCALE, "r": xs[c + 1] - PAD * SCALE - tw,
                 "c": (xs[c] + xs[c + 1] - tw) / 2}[side]
            d.text((x, (y0 + y1) / 2), text, font=font, fill=INK, anchor="lm")
        d.line([0, y1, W, y1], fill=GRID, width=1)
    d.line([0, 0, W, 0], fill=GRID, width=1)
    for x in xs:
        d.line([x, 0, x, len(rows) * ROW_H * SCALE], fill=GRID, width=1)
    img.save(path, optimize=True)


# ── cold storage, from the report ────────────────────────────────────────────

def cold_tables(report: Path) -> tuple[str, list[tuple[str, list]]]:
    wb = openpyxl.load_workbook(report, read_only=True, data_only=True)
    ws = next(w for w in wb.worksheets
              if any(r and r[0] == "Inward Dt" for r in w.iter_rows(max_row=10, values_only=True)))
    rows = list(ws.iter_rows(values_only=True))
    h = next(i for i, r in enumerate(rows) if r and r[0] == "Inward Dt")
    hdr = [str(x).strip() if x else "" for x in rows[h]]
    col = {name: hdr.index(name) for name in (
        "Total Inventory Kgs", "Value", "Group Name*", "Sub Group Name*", "Storage Location", "Dates/OTD")}
    report_day = rows[1][1]     # "Report Date :" -- the day this stock opens
    day = report_day.strftime("%d-%b-%y") if hasattr(report_day, "strftime") else str(report_day)

    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))))
    for r in rows[h + 1:]:
        if not r or r[col["Storage Location"]] is None or str(r[col["Storage Location"]]).strip() in COLD_SKIP:
            continue
        cell = agg[r[col["Dates/OTD"]]][str(r[col["Storage Location"]]).strip()][
            str(r[col["Group Name*"]]).strip()][str(r[col["Sub Group Name*"]]).strip()]
        cell[0] += float(r[col["Total Inventory Kgs"]] or 0)
        cell[1] += float(r[col["Value"]] or 0)

    tables = []
    for key, title in CATEGORIES:
        locs = agg.get(key, {})
        body, kg_all, val_all = [], 0.0, 0.0
        for loc in [*[l for l in COLD_ORDER if l in locs], *sorted(set(locs) - set(COLD_ORDER))]:
            first, kg_loc, val_loc = True, 0.0, 0.0
            for group in sorted(locs[loc], key=str.lower):
                first_g = True
                for sub in sorted(locs[loc][group], key=str.lower):
                    kg, val = locs[loc][group][sub]
                    if round(kg) == 0:
                        continue
                    body.append(("data", [loc if first else "", group if first_g else "", sub,
                                          inr(kg), inr(val / kg), inr(val)]))
                    first = first_g = False
                    kg_loc += kg
                    val_loc += val
            if kg_loc:
                body.append(("total", [f"{loc} Total", "", "", inr(kg_loc), inr(val_loc / kg_loc), inr(val_loc)]))
                kg_all += kg_loc
                val_all += val_loc
        tables.append((f"cold_{title.lower().replace(' ', '_')}", COLD_ALIGN, [
            ("title", ["All Cold Warehouses", "Category --- >", title, "In Kgs", "Value/KG", ""]),
            ("summary", [day, "CDPL+CFPL", "Summary", inr(kg_all), inr(val_all / kg_all, 2), inr(val_all)]),
            ("head", ["Storage Location", "Group Name*", "Sub Group Name*", "Total in Kgs",
                      "Avg Rate/Kg", "Approximate Value"]),
            *body,
        ], (kg_all, val_all)))
    return day, tables


# ── warehouses, from Stock Take ──────────────────────────────────────────────

def _group_label(category: str | None) -> str:
    """'MISCELLANEOUS - RM' / 'seeds' -> 'Miscellaneous - RM' / 'Seeds'. The
    catalogue spells one category in both cases, so the key is upper-cased and
    the label is built from it; two-letter words (RM, PM, FG) stay capitals."""
    words = (category or "Uncategorised").strip().upper().split(" ")
    return " ".join(w if len(w) <= 2 else w.capitalize() for w in words)


def warehouse_tables(stock: list[dict], day: str) -> list[tuple[str, list, list, tuple]]:
    """One table per warehouse: floor > category, Fresh / Off Grade / Total kg.

    By category, not sub category: floor x sub category runs to ~250 rows for
    W202, too tall to read as one image on a phone."""
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, set()])))
    labels: dict[tuple[str, str], str] = {}
    for r in stock:
        if r["warehouse"] in F.COLD_WAREHOUSES:
            continue
        fl = r["floor_key"] or ""
        labels.setdefault((r["warehouse"], fl), (r["floor"] or "").strip() or "(no floor)")
        cell = agg[r["warehouse"]][fl][_group_label(r["item_category"])]
        cell[0 if r["stock_type"] == "Fresh Stock" else 1] += r["total_weight"]
        if round(r["total_weight"]):
            cell[2].add(r["item_key"])

    tables = []
    for wh in sorted(agg):
        declared = [f.upper() for f in F.FLOORS_BY_WAREHOUSE.get(wh, [])]
        floors = sorted(agg[wh], key=lambda k: (declared.index(k) if k in declared else len(declared), k))
        body, tot, items_all = [], [0.0, 0.0], set()
        for fl in floors:
            first, fl_tot, items_fl = True, [0.0, 0.0], set()
            for group in sorted(agg[wh][fl]):
                fresh, off, items = agg[wh][fl][group]
                if round(fresh) == 0 and round(off) == 0:
                    continue
                body.append(("data", [labels[(wh, fl)] if first else "", group, str(len(items)),
                                      inr(fresh), inr(off), inr(fresh + off)]))
                first = False
                fl_tot[0] += fresh
                fl_tot[1] += off
                items_fl |= items
            if not first:
                body.append(("total", [f"{labels[(wh, fl)]} Total", "", str(len(items_fl)),
                                       inr(fl_tot[0]), inr(fl_tot[1]), inr(sum(fl_tot))]))
                tot[0] += fl_tot[0]
                tot[1] += fl_tot[1]
                items_all |= items_fl
        if not body:
            continue
        tables.append((f"warehouse_{wh.lower()}", WAREHOUSE_ALIGN, [
            ("title", [f"Warehouse {F.warehouse_label(wh)}", "Source --- >", "Stock Take", "In Kgs", "In Kgs", "In Kgs"]),
            ("summary", [day, "All floors", f"{len(items_all)} articles", inr(tot[0]), inr(tot[1]), inr(sum(tot))]),
            ("head", ["Storage Location", "Group Name*", "Articles", "Fresh Stock", "Off Grade", "Total in Kgs"]),
            *body,
        ], (sum(tot), None)))
    return tables


# ── who, and the mail ────────────────────────────────────────────────────────

async def recipients(conn) -> list[dict]:
    people: dict[int, dict] = {}
    for role in ROLES:
        for h in await role_holders(conn, [role]):
            people.setdefault(h["user_id"], {**h, "roles": []})["roles"].append(role)
    return list(people.values())


def build_message(sender: str, to: list[str], subject: str, plain: str, html: str,
                  images: list[tuple[str, Path]]) -> EmailMessage:
    """Plain + HTML, each image inline in the HTML (cid) in the order given.

    `subject` is "<what> - <detail>"; stamp() composes the final line from
    those two parts (an EVENT subject ignores the Subject header it is given)."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="candorfoods.in")
    msg.set_content(plain)
    cids = {name: make_msgid(domain="candorfoods.in")[1:-1] for name, _ in images}
    for name, cid in cids.items():
        html = html.replace(f"__CID_{name}__", cid)
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[1]
    for name, path in images:
        html_part.add_related(path.read_bytes(), "image", "png", cid=f"<{cids[name]}>", filename=path.name)
    what, _, detail = subject.partition(" - ")
    stamp(msg, module=Module.REPORTS, policy=SubjectPolicy.EVENT, entity_type="StockSummary",
          entity_id=what, event_label=detail or None, sender=sender)
    return msg


def send(settings: Settings, to: list[str], subject: str, plain: str, html: str,
         images: list[tuple[str, Path]]) -> None:
    sender = settings.SMTP_EMAIL.strip()
    msg = build_message(sender, to, subject, plain, html, images)
    with smtplib.SMTP(settings.SMTP_HOST.strip(), settings.SMTP_PORT, timeout=60) as srv:
        srv.starttls(context=ssl.create_default_context())
        srv.login(sender, settings.SMTP_APP_PASSWORD.strip())
        srv.send_message(msg, from_addr=sender, to_addrs=to)


def rds_dsn() -> str:
    """Whichever DATABASE_URL line in backend/.env (active or commented out) points
    at RDS, where the live stock is. The active line gets switched to Supabase for
    test work; the daily report must not follow it there."""
    env = (ROOT / ".env").read_text()
    for dsn in re.findall(r"^\s*#?\s*DATABASE_URL=(postgresql://\S+)", env, re.M):
        if "rds.amazonaws.com" in dsn.split("@", 1)[-1]:
            return dsn
    sys.exit("no DATABASE_URL line in backend/.env points at RDS")


async def main(a) -> None:
    settings = Settings()
    dsn = rds_dsn()
    print(f"database: {dsn.split('@', 1)[-1].split('/', 1)[0]}")

    if not a.report:
        sys.exit("no Savla_Rishi_Inventory_*.xlsx in backend/ - pass --report")
    print(f"cold report: {Path(a.report).name}")
    cold_day, cold = cold_tables(Path(a.report))
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            stock, _ = await fetch_stock_by_place(conn)
            people = await recipients(conn)
    finally:
        await conn.close()
    now = datetime.now(IST)
    wh_day = now.strftime("%d-%b-%y")
    tables = [*cold, *warehouse_tables(stock, wh_day)]

    out = Path(a.out or tempfile.gettempdir()) / f"stock_summary_{now:%Y%m%d_%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    images = []
    for name, align, rows, _ in tables:
        render(rows, out / f"{name}.png", align)
        images.append((name, out / f"{name}.png"))
    print(f"\nimages ({len(images)}) in {out}:")
    for (name, _, rows, (kg, val)), (_, path) in zip(tables, images):
        print(f"  {path.name:<34} {len(rows) - 3:>3} rows  {inr(kg):>12} kg" + (f"  Rs {inr(val)}" if val else ""))

    # Role holders first, then the standing extras; one copy per address.
    everyone = list({e.lower(): e for e in [*(p["email"] for p in people if p["email"]), *EXTRA_RECIPIENTS]}.values())
    to = a.to or everyone
    print("\nrecipients:" if not a.to else "\nrecipients (--to):")
    for p in people:
        mark = "" if p["email"] else "  <- no email, skipped"
        print(f"  {p['name']:<28} {', '.join(p['roles']):<26} {p['email'] or '-'}{mark}")
    role_emails = {p["email"].lower() for p in people if p["email"]}
    for e in EXTRA_RECIPIENTS:
        if e.lower() not in role_emails:
            print(f"  {'':<28} {'(always included)':<26} {e}")
    if a.to:
        print("  sending only to: " + ", ".join(a.to))
    if not to:
        sys.exit("no recipient has an email address")

    cold_kg = sum(t[3][0] for t in cold)
    cold_val = sum(t[3][1] for t in cold)
    subject = f"Stock summary - cold storage {cold_day}, warehouses {wh_day}"
    names = [n for n, _ in images]
    stores = "Savla, Savla Bond, Rishi, Eskimo; A-185 Cold is in the A185 warehouse image"
    plain = (f"Stock Take updated with the cold storage opening stock of {cold_day}.\n"
             f"Cold storage ({stores}): {inr(cold_kg)} kg, approx. Rs {inr(cold_val)}.\n"
             f"Warehouses: current Stock Take figures as on {now:%d-%b-%y %H:%M} IST, kg only.\n"
             f"Images attached: {', '.join(p.name for _, p in images)}\n")
    blocks = "".join(
        f'<p style="margin:18px 0 6px"><b>{escape(n.replace("_", " ").title())}</b></p>'
        f'<img src="cid:__CID_{n}__" style="max-width:100%;border:1px solid #ccc">' for n in names)
    html = ("<div style=\"font-family:Calibri,Arial,sans-serif;font-size:14px\">"
            f"<p>Stock Take updated with the cold storage opening stock of {escape(cold_day)}.</p>"
            f"<p><b>Cold storage</b> ({escape(stores)}): "
            f"{inr(cold_kg)} kg, approx. Rs {inr(cold_val)}.<br>"
            f"<b>Warehouses</b>: current Stock Take figures as on {now:%d-%b-%y %H:%M} IST, in kg "
            "(Stock Take holds no rates, so no value).</p>" + blocks + "</div>")

    if a.dry_run:
        print(f"\nDRY RUN - nothing sent. Subject would be: {subject}")
        return
    if not settings.SMTP_HOST.strip():
        sys.exit("SMTP_HOST is not set in backend/.env")
    send(settings, to, subject, plain, html, images)
    print(f"\nSENT to {len(to)}: {', '.join(to)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="render and list recipients, send nothing")
    ap.add_argument("--to", nargs="+", help="send only to these addresses")
    ap.add_argument("--report", default=str(DEFAULT_REPORT) if DEFAULT_REPORT else None,
                    help="the Savla/Rishi report xlsx (default: the newest one in backend/)")
    ap.add_argument("--out", help="folder for the images (default: your temp folder)")
    asyncio.run(main(ap.parse_args()))
