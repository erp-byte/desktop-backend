"""
Symbol Builder — downloads Fyers symbol master, identifies F&O stocks,
maps option chain symbols, and saves to JSON + Excel.

No database connection needed. Run locally.

pip install requests pandas openpyxl
"""
import requests
import pandas as pd
from io import StringIO
import json
import os
import sys
import logging
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Output directory
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# FYERS SYMBOL MASTER URLs
# ============================================================
URLS = {
    "NSE_CM": "https://public.fyers.in/sym_details/NSE_CM.csv",
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
}

COLUMNS = [
    "instrument_type",        # 0: 0=equity, 10=index, 11=futstk, 13=optstk, 14=futidx, 15=optidx
    "lot_size",               # 1: 1 for equity, actual lot for F&O
    "tick_size",              # 2
    "col3",                   # 3: unknown
    "col4",                   # 4: unknown
    "last_update",            # 5: date string
    "expiry_epoch",           # 6: nan for equity
    "symbol",                 # 7: NSE:RELIANCE-EQ / NSE:RELIANCE26APRFUT
    "exchange_code",          # 8: 10=NSE
    "segment_code",           # 9: 10=CM, 11=FO
    "scrip_code",             # 10
    "col11",                  # 11: unknown
    "underlying_scrip_code",  # 12
    "strike_price",           # 13: -1 for non-derivatives
    "col14",                  # 14: unknown
    "fytoken",                # 15
    "option_type",            # 16: CE/PE for options, nan otherwise
    "underlying_fytoken",     # 17
    "col18",                  # 18: unknown
]

# Regex patterns for extracting underlying ticker from F&O symbols
import re
_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
_FUT_RE = re.compile(rf"^NSE:([A-Z0-9&]+)\d{{2}}(?:{_MONTHS})FUT$")
_OPT_RE = re.compile(rf"^NSE:([A-Z0-9&]+)\d{{2}}(?:{_MONTHS})\d+\.?\d*[CP]E$")


def _fo_underlying(sym: str) -> str | None:
    """Extract underlying NSE ticker from a F&O fyers symbol."""
    if not isinstance(sym, str):
        return None
    m = _FUT_RE.match(sym) or _OPT_RE.match(sym)
    return m.group(1) if m else None


def download_symbol_master(url: str) -> pd.DataFrame:
    """Download and parse a Fyers symbol master CSV."""
    logger.info(f"Downloading {url}...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    df = pd.read_csv(
        StringIO(resp.text),
        header=None,
        names=COLUMNS[:19],
        on_bad_lines="skip",
        dtype=str
    )
    # Convert numeric columns back to appropriate types
    for col in ["instrument_type", "lot_size", "tick_size", "expiry_epoch",
                "exchange_code", "scrip_code", "underlying_scrip_code", "strike_price"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    logger.info(f"Downloaded {len(df)} rows")
    return df


def get_equity_symbols(cm_df: pd.DataFrame) -> pd.DataFrame:
    """Extract all equity (EQ series) stocks from NSE_CM."""
    equity = cm_df[
        (cm_df["instrument_type"] == 0) &
        (cm_df["symbol"].str.endswith("-EQ", na=False))
    ].copy()

    equity["fyers_symbol"] = equity["symbol"]
    # Derive NSE ticker from fyers symbol: NSE:RELIANCE-EQ → RELIANCE
    equity["nse_symbol"] = (
        equity["symbol"]
        .str.removeprefix("NSE:")
        .str.removesuffix("-EQ")
    )
    equity["clean_name"] = equity["nse_symbol"]  # full name not in CSV
    equity["isin_clean"] = ""                     # isin not in current CSV format

    logger.info(f"Found {len(equity)} equity stocks")
    return equity


def get_fno_stocks(fo_df: pd.DataFrame) -> dict:
    """Extract F&O stock names and lot sizes using regex on fyers symbols."""
    futures = fo_df[fo_df["instrument_type"] == 11].copy()  # FUTSTK
    options = fo_df[fo_df["instrument_type"] == 13].copy()  # OPTSTK

    futures["underlying"] = futures["symbol"].apply(_fo_underlying)
    options["underlying"] = options["symbol"].apply(_fo_underlying)

    fno_map = {}

    # One row per underlying (first contract seen has the lot size)
    for _, row in futures.dropna(subset=["underlying"]).drop_duplicates("underlying").iterrows():
        name = row["underlying"]
        fno_map[name] = {
            "lot_size": int(row["lot_size"]) if pd.notna(row["lot_size"]) else 0,
            "has_futures": True,
            "has_options": False,
        }

    for name in options.dropna(subset=["underlying"])["underlying"].unique():
        if name in fno_map:
            fno_map[name]["has_options"] = True
        else:
            lot = options[options["underlying"] == name]["lot_size"].iloc[0]
            fno_map[name] = {
                "lot_size": int(lot) if pd.notna(lot) else 0,
                "has_futures": False,
                "has_options": True,
            }

    logger.info(f"Found {len(fno_map)} F&O stocks")
    return fno_map


def get_index_symbols(cm_df: pd.DataFrame) -> list:
    """Extract index symbols."""
    indices = cm_df[cm_df["instrument_type"] == 10].copy()
    index_list = []
    for _, row in indices.iterrows():
        fyers_sym = row["symbol"] if pd.notna(row["symbol"]) else ""
        # NSE:NIFTY50-INDEX → NIFTY50
        nse_sym = fyers_sym.removeprefix("NSE:").removesuffix("-INDEX")
        index_list.append({
            "symbol": nse_sym,
            "fyers_symbol": fyers_sym,
            "name": nse_sym,
        })
    logger.info(f"Found {len(index_list)} indices")
    return index_list


def build_futures_symbol(nse_symbol: str, year: int = None, month: int = None) -> str:
    """Build current month futures symbol."""
    if year is None or month is None:
        today = date.today()
        year = today.year
        month = today.month

    month_map = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"
    }

    yy = str(year)[2:]
    mon = month_map[month]
    return f"NSE:{nse_symbol}{yy}{mon}FUT"


def build_symbols_list(equity_df: pd.DataFrame, fno_map: dict) -> list:
    """Build the final symbols list."""
    symbols = []

    for _, row in equity_df.iterrows():
        nse_sym = row["nse_symbol"]
        fyers_sym = row["fyers_symbol"]

        if not isinstance(nse_sym, str) or not nse_sym.strip():
            continue

        is_fno = nse_sym in fno_map
        lot_size = fno_map[nse_sym]["lot_size"] if is_fno else None
        has_futures = fno_map[nse_sym]["has_futures"] if is_fno else False
        has_options = fno_map[nse_sym]["has_options"] if is_fno else False
        futures_symbol = build_futures_symbol(nse_sym) if is_fno else None

        symbols.append({
            "symbol": nse_sym,
            "name": row["clean_name"],
            "isin": row["isin_clean"] if pd.notna(row["isin_clean"]) else "",
            "sector": "",
            "market_cap_type": "",
            "is_fno": is_fno,
            "has_futures": has_futures,
            "has_options": has_options,
            "fyers_symbol": fyers_sym,
            "nse_symbol": nse_sym,
            "option_chain_symbol": fyers_sym if is_fno else None,
            "futures_symbol": futures_symbol,
            "lot_size": lot_size,
            "is_active": True,
        })

    logger.info(f"Built {len(symbols)} symbols ({sum(1 for s in symbols if s['is_fno'])} F&O)")
    return symbols


def save_json(data: list, filename: str):
    """Save to JSON."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(data)} records to {filepath}")


def save_excel(all_symbols: list, fno_symbols: list, indices: list, filename: str):
    """Save to Excel with multiple sheets."""
    filepath = os.path.join(OUTPUT_DIR, filename)

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:

        # Sheet 1: All equity stocks
        df_all = pd.DataFrame(all_symbols) if all_symbols else pd.DataFrame(columns=["symbol"])
        if "symbol" in df_all.columns:
            df_all = df_all.sort_values("symbol")
        df_all.to_excel(writer, sheet_name="All Stocks", index=False)

        # Sheet 2: F&O stocks only
        df_fno = pd.DataFrame(fno_symbols) if fno_symbols else pd.DataFrame(columns=["symbol"])
        if "symbol" in df_fno.columns and not df_fno.empty:
            df_fno = df_fno.sort_values("symbol")
        df_fno.to_excel(writer, sheet_name="FnO Stocks", index=False)

        # Sheet 3: Indices
        df_idx = pd.DataFrame(indices)
        df_idx.to_excel(writer, sheet_name="Indices", index=False)

        # Sheet 4: Symbol mapping examples
        mapping_data = []
        for s in fno_symbols[:20]:
            mapping_data.append({
                "Stock": s["symbol"],
                "WebSocket / Quotes": s["fyers_symbol"],
                "Option Chain API": s["option_chain_symbol"],
                "Futures (Current Month)": s["futures_symbol"],
                "NSE Delivery Data": s["nse_symbol"],
                "Lot Size": s["lot_size"],
            })
        df_map = pd.DataFrame(mapping_data)
        df_map.to_excel(writer, sheet_name="Symbol Mapping", index=False)

        # Sheet 5: Summary
        stats = pd.DataFrame([
            {"Metric": "Total Equity Stocks", "Value": len(all_symbols)},
            {"Metric": "F&O Enabled Stocks", "Value": len(fno_symbols)},
            {"Metric": "Non-F&O Stocks", "Value": len(all_symbols) - len(fno_symbols)},
            {"Metric": "Indices", "Value": len(indices)},
            {"Metric": "Download Date", "Value": date.today().isoformat()},
            {"Metric": "Source: Equity", "Value": URLS["NSE_CM"]},
            {"Metric": "Source: F&O", "Value": URLS["NSE_FO"]},
        ])
        stats.to_excel(writer, sheet_name="Summary", index=False)

    logger.info(f"Saved Excel to {filepath}")


def print_stats(all_symbols: list, fno_symbols: list, indices: list):
    """Print statistics."""
    print(f"\n{'='*60}")
    print(f"  SYMBOL MASTER — {date.today()}")
    print(f"{'='*60}")
    print(f"  Total equity stocks:   {len(all_symbols)}")
    print(f"  F&O enabled:           {len(fno_symbols)}")
    print(f"  Non-F&O:               {len(all_symbols) - len(fno_symbols)}")
    print(f"  Indices:               {len(indices)}")
    print(f"{'='*60}")

    print(f"\n  F&O STOCKS:")
    print(f"  {'Symbol':<15} {'Lot':<6} {'Fyers':<25} {'Futures':<28} {'Opt Chain'}")
    print(f"  {'-'*100}")
    for s in sorted(fno_symbols, key=lambda x: x["symbol"]):
        print(f"  {s['symbol']:<15} {str(s['lot_size'] or ''):<6} {s['fyers_symbol']:<25} {s['futures_symbol'] or '':<28} {s['option_chain_symbol'] or ''}")

    print(f"\n  KEY INDICES:")
    for idx in indices:
        name_upper = idx["name"].upper()
        if any(k in name_upper for k in ["NIFTY 50", "NIFTY BANK", "NIFTY FIN", "VIX", "MIDCAP"]):
            print(f"  {idx['fyers_symbol']:<35} {idx['name']}")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("\n  Downloading Fyers symbol masters...\n")

    # Step 1: Download
    cm_df = download_symbol_master(URLS["NSE_CM"])
    fo_df = download_symbol_master(URLS["NSE_FO"])

    # Step 2: Extract
    equity_df = get_equity_symbols(cm_df)
    fno_map = get_fno_stocks(fo_df)
    indices = get_index_symbols(cm_df)

    # Step 3: Build
    all_symbols = build_symbols_list(equity_df, fno_map)
    fno_symbols = [s for s in all_symbols if s["is_fno"]]

    # Step 4: Stats
    print_stats(all_symbols, fno_symbols, indices)

    # Step 5: Save JSON
    save_json(all_symbols, "symbols_all.json")
    save_json(fno_symbols, "symbols_fno.json")
    save_json(indices, "symbols_indices.json")

    # Step 6: Save Excel
    save_excel(all_symbols, fno_symbols, indices, "symbols_master.xlsx")

    # Step 7: Print output info
    print(f"\n{'='*60}")
    print(f"  FILES SAVED IN: {OUTPUT_DIR}")
    print(f"{'='*60}")
    print(f"  symbols_all.json        — {len(all_symbols)} equity stocks")
    print(f"  symbols_fno.json        — {len(fno_symbols)} F&O stocks")
    print(f"  symbols_indices.json    — {len(indices)} indices")
    print(f"  symbols_master.xlsx     — Excel (5 sheets)")
    print(f"{'='*60}")
    print(f"\n  NEXT STEPS:")
    print(f"  1. Open symbols_master.xlsx")
    print(f"  2. Go to 'All Stocks' sheet")
    print(f"  3. Fill 'market_cap_type' column → LARGE or SMALL")
    print(f"  4. Fill 'sector' column → IT, Banking, Pharma, etc.")
    print(f"  5. Save the Excel — you'll import this into Supabase later")
    print()