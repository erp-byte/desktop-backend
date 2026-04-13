"""
Enriches symbols_all.json with sector and market_cap_type,
then regenerates symbols_fno.json and symbols_master.xlsx.

market_cap_type:
  LARGE  = Nifty 100 constituents (Nifty 50 + Nifty Next 50)
  MID    = Nifty 101-250
  SMALL  = Nifty 251-500 and other F&O stocks not in Nifty 100
  ""     = non-F&O stocks not individually classified (fill manually)
"""

import json
import os
import pandas as pd
from datetime import date

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# ─────────────────────────────────────────────
# SECTOR MAP  (NSE ticker -> sector label)
# ─────────────────────────────────────────────
SECTOR = {
    # IT / Technology
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "MPHASIS": "IT", "COFORGE": "IT", "PERSISTENT": "IT",
    "LTM": "IT", "OFSS": "IT", "KPITTECH": "IT",

    # Banking — Private
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "AXISBANK": "Banking", "INDUSINDBK": "Banking", "BANDHANBNK": "Banking",
    "FEDERALBNK": "Banking", "AUBANK": "Banking", "RBLBANK": "Banking",
    "IDFCFIRSTB": "Banking",

    # Banking — PSU
    "SBIN": "Banking", "INDIANB": "Banking", "CANBK": "Banking",
    "BANKBARODA": "Banking", "PNB": "Banking", "UNIONBANK": "Banking",
    "BANKINDIA": "Banking",

    # NBFC / Financial Services
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "CHOLAFIN": "NBFC",
    "MUTHOOTFIN": "NBFC", "MANAPPURAM": "NBFC", "LICHSGFIN": "NBFC",
    "PNBHOUSING": "NBFC", "LTF": "NBFC", "SHRIRAMFIN": "NBFC",
    "JIOFIN": "NBFC", "SAMMAANCAP": "NBFC",

    # Insurance
    "HDFCLIFE": "Insurance", "SBILIFE": "Insurance", "ICICIPRULI": "Insurance",
    "ICICIGI": "Insurance", "LICI": "Insurance", "MFSL": "Insurance",
    "MAXHEALTH": "Healthcare",  # Max Health is hospitals, not insurance

    # Capital Markets / Exchanges
    "BSE": "Capital Markets", "MCX": "Capital Markets", "CDSL": "Capital Markets",
    "CAMS": "Capital Markets", "KFINTECH": "Capital Markets",
    "ANGELONE": "Capital Markets", "MOTILALOFS": "Capital Markets",
    "NUVAMA": "Capital Markets", "360ONE": "Capital Markets",
    "HDFCAMC": "Capital Markets", "IEX": "Capital Markets",
    "POLICYBZR": "Capital Markets", "NAUKRI": "Capital Markets",

    # Auto / Auto Ancillary
    "MARUTI": "Auto", "M&M": "Auto", "EICHERMOT": "Auto",
    "HEROMOTOCO": "Auto", "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    "EXIDEIND": "Auto Ancillary", "BOSCHLTD": "Auto Ancillary",
    "MOTHERSON": "Auto Ancillary", "TIINDIA": "Auto Ancillary",
    "UNOMINDA": "Auto Ancillary", "SONACOMS": "Auto Ancillary",
    "AMARAJABAT": "Auto Ancillary", "BALKRISIND": "Auto Ancillary",
    "TMPV": "Auto",          # Tata Motors Passenger Vehicles (DVR)
    "HYUNDAI": "Auto",
    "FORCEMOT": "Auto",
    "AMBER": "Consumer Electronics",

    # Pharma / Healthcare
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "AUROPHARMA": "Pharma", "LUPIN": "Pharma",
    "ALKEM": "Pharma", "LAURUSLABS": "Pharma", "BIOCON": "Pharma",
    "GLENMARK": "Pharma", "MANKIND": "Pharma", "ZYDUSLIFE": "Pharma",
    "PPLPHARMA": "Pharma", "TORNTPHARM": "Pharma",
    "APOLLOHOSP": "Healthcare", "FORTIS": "Healthcare",

    # Energy — Oil & Gas
    "RELIANCE": "Oil & Gas", "ONGC": "Oil & Gas", "BPCL": "Oil & Gas",
    "IOC": "Oil & Gas", "HINDPETRO": "Oil & Gas", "GAIL": "Oil & Gas",
    "OIL": "Oil & Gas", "PETRONET": "Oil & Gas",

    # Power / Renewables
    "NTPC": "Power", "POWERGRID": "Power", "TATAPOWER": "Power",
    "ADANIPOWER": "Power", "ADANIGREEN": "Power", "JSWENERGY": "Power",
    "NHPC": "Power", "INOXWIND": "Power", "WAAREEENER": "Power",
    "PREMIERENE": "Power", "TORNTPOWER": "Power", "ADANIENSOL": "Power",

    # Metals & Mining
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "SAIL": "Metals", "NATIONALUM": "Metals",
    "NMDC": "Metals", "HINDZINC": "Metals", "COALINDIA": "Metals",
    "JINDALSTEL": "Metals",

    # Cement
    "ULTRACEMCO": "Cement", "GRASIM": "Cement", "AMBUJACEM": "Cement",
    "SHREECEM": "Cement", "DALBHARAT": "Cement",

    # FMCG / Consumer
    "ITC": "FMCG", "HINDUNILVR": "FMCG", "BRITANNIA": "FMCG",
    "NESTLEIND": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "COLPAL": "FMCG", "GODREJCP": "FMCG", "TATACONSUM": "FMCG",
    "PATANJALI": "FMCG", "VBL": "FMCG", "UNITDSPR": "FMCG",
    "GODFRYPHLP": "FMCG",

    # Telecom
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUSTOWER": "Telecom",

    # Realty
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty",
    "LODHA": "Realty", "PRESTIGE": "Realty", "PHOENIXLTD": "Realty",

    # Infrastructure / Capital Goods
    "LT": "Infrastructure", "BHEL": "Capital Goods", "SIEMENS": "Capital Goods",
    "ABB": "Capital Goods", "CUMMINSIND": "Capital Goods", "CGPOWER": "Capital Goods",
    "HAVELLS": "Capital Goods", "POLYCAB": "Capital Goods", "KEI": "Capital Goods",
    "POWERINDIA": "Capital Goods", "KAYNES": "Capital Goods",
    "APLAPOLLO": "Capital Goods",

    # Defence / PSU
    "BEL": "Defence", "HAL": "Defence", "BDL": "Defence",
    "MAZDOCK": "Defence", "COCHINSHIP": "Defence",

    # PSU Infrastructure / Finance
    "NBCC": "PSU", "RVNL": "PSU", "IRFC": "PSU", "IREDA": "PSU",
    "PFC": "PSU", "RECLTD": "PSU", "HUDCO": "PSU",
    "ADANIPORTS": "Infrastructure",

    # Chemicals
    "PIDILITIND": "Chemicals", "SRF": "Chemicals", "PIIND": "Chemicals",
    "SOLARINDS": "Chemicals", "DEEPAKNTR": "Chemicals",

    # Retail / Consumer Discretionary
    "DMART": "Retail", "TRENT": "Retail", "KALYANKJIL": "Retail",
    "NYKAA": "Retail", "JUBLFOOD": "QSR",

    # Hospitality / Hotels
    "INDHOTEL": "Hospitality",

    # Aviation
    "INDIGO": "Aviation",

    # Logistics
    "DELHIVERY": "Logistics", "CONCOR": "Logistics",

    # Diversified / Conglomerate
    "ADANIENT": "Conglomerate", "BAJAJHLDNG": "Conglomerate",

    # Tech / New-age
    "PAYTM": "Fintech", "SWIGGY": "Consumer Tech", "ETERNAL": "Consumer Tech",
    "GMRAIRPORT": "Infrastructure",

    # Textiles
    "PAGEIND": "Textiles",

    # Misc
    "CROMPTON": "Consumer Electricals", "ASTRAL": "Building Materials",
    "SUPREMEIND": "Plastics", "DIXON": "Consumer Electronics",
    "BLUESTARCO": "Consumer Electricals", "VMM": "PSU",
    "PGEL": "Capital Goods",
}

# ─────────────────────────────────────────────
# MARKET CAP MAP
# ─────────────────────────────────────────────

# Nifty 50 (Large Cap)
NIFTY50 = {
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL",
    "SBIN", "ITC", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    "HCLTECH", "WIPRO", "MARUTI", "ULTRACEMCO", "SUNPHARMA", "M&M",
    "NTPC", "TATASTEEL", "TITAN", "BAJAJFINSV", "POWERGRID", "NESTLEIND",
    "TECHM", "COALINDIA", "JSWSTEEL", "ONGC", "HINDALCO", "GRASIM",
    "DRREDDY", "ADANIPORTS", "CIPLA", "BRITANNIA", "DIVISLAB", "TRENT",
    "INDUSINDBK", "HINDUNILVR", "EICHERMOT", "BPCL", "SHRIRAMFIN",
    "BEL", "TATACONSUM", "HEROMOTOCO", "ADANIENT", "HDFCLIFE",
    "APOLLOHOSP", "JIOFIN", "ETERNAL",
}

# Nifty Next 50 (also Large Cap)
NIFTY_NEXT50 = {
    "BAJAJHLDNG", "DMART", "PIDILITIND", "SIEMENS", "GODREJCP",
    "HAVELLS", "DLF", "DABUR", "MARICO", "COLPAL", "ADANIGREEN",
    "ADANIPOWER", "TATAPOWER", "OFSS", "HDFCAMC", "ICICIPRULI",
    "ICICIGI", "SBILIFE", "LICI", "HDFCLIFE",
    "LUPIN", "BIOCON", "GAIL", "IOC", "HINDPETRO",
    "NHPC", "NMDC", "COALINDIA", "SAIL", "VEDL",
    "BANKBARODA", "CANBK", "PNB", "UNIONBANK",
    "HAL", "BDL", "MAZDOCK", "COCHINSHIP",
    "IRFC", "PFC", "RECLTD", "RVNL",
    "LTF", "CHOLAFIN", "MUTHOOTFIN",
    "INDUSTOWER", "LODHA", "GODREJPROP",
    "NAUKRI", "PAYTM",
}

# Nifty Midcap 150 (Mid Cap)
NIFTY_MID = {
    "AUBANK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "RBLBANK",
    "INDIANB", "BANKINDIA",
    "AUROPHARMA", "ALKEM", "LAURUSLABS", "GLENMARK", "MANKIND",
    "ZYDUSLIFE", "PPLPHARMA", "TORNTPHARM",
    "ASHOKLEY", "EXIDEIND", "BOSCHLTD", "MOTHERSON", "TIINDIA",
    "UNOMINDA", "SONACOMS", "TVSMOTOR", "EICHERMOT", "FORCEMOT",
    "CUMMINSIND", "ABB", "CGPOWER", "POLYCAB", "KEI", "POWERINDIA",
    "KAYNES", "APLAPOLLO",
    "DALBHARAT", "AMBUJACEM", "SHREECEM",
    "JINDALSTEL", "NATIONALUM", "HINDZINC",
    "PETRONET", "OIL",
    "TORNTPOWER", "JSWENERGY", "INOXWIND", "WAAREEENER",
    "BSE", "MCX", "CDSL", "CAMS", "KFINTECH", "ANGELONE",
    "MOTILALOFS", "NUVAMA", "360ONE", "IEX",
    "LICHSGFIN", "PNBHOUSING", "MANAPPURAM",
    "PRESTIGE", "OBEROIRLTY", "PHOENIXLTD",
    "DELHIVERY", "CONCOR", "ADANIPORTS",
    "INDIGO", "INDHOTEL",
    "COFORGE", "PERSISTENT", "MPHASIS", "KPITTECH",
    "JUBLFOOD", "NYKAA", "KALYANKJIL",
    "SRF", "PIIND", "SOLARINDS",
    "DIXON", "AMBER", "BLUESTARCO", "CROMPTON", "HAVELLS",
    "PAGEIND", "TRENT", "VBL",
    "HUDCO", "IREDA", "NBCC",
    "ASTRAL", "SUPREMEIND",
    "HYUNDAI", "TMPV",
    "SWIGGY", "ETERNAL",
    "UNITDSPR", "GODFRYPHLP",
    "GMRAIRPORT", "PATANJALI",
    "PGEL", "FORTIS", "MAXHEALTH",
}

LARGE_CAP = NIFTY50 | NIFTY_NEXT50


def classify(symbol: str) -> tuple[str, str]:
    """Return (market_cap_type, sector) for a symbol."""
    sector = SECTOR.get(symbol, "")
    if symbol in LARGE_CAP:
        cap = "LARGE"
    elif symbol in NIFTY_MID:
        cap = "MID"
    else:
        cap = ""          # leave blank — user fills non-F&O unknowns
    return cap, sector


def enrich(symbols: list) -> list:
    enriched = []
    for s in symbols:
        cap, sec = classify(s["symbol"])
        enriched.append({**s, "market_cap_type": cap, "sector": sec})
    return enriched


def save_json(data, filename):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved {len(data):>5} records -> {path}")


def save_excel(all_syms, fno_syms, indices, filename):
    path = os.path.join(DATA_DIR, filename)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:

        def sheet(data, name, sort_col="symbol"):
            df = pd.DataFrame(data) if data else pd.DataFrame(columns=["symbol"])
            if sort_col in df.columns and not df.empty:
                df = df.sort_values(sort_col)
            df.to_excel(writer, sheet_name=name, index=False)

        sheet(all_syms, "All Stocks")
        sheet(fno_syms, "FnO Stocks")
        sheet(indices, "Indices", sort_col="fyers_symbol")

        # Symbol mapping guide sheet
        mapping = [{
            "Stock": s["symbol"],
            "Sector": s["sector"],
            "Market Cap": s["market_cap_type"],
            "WebSocket / Quotes": s["fyers_symbol"],
            "Option Chain API": s.get("option_chain_symbol", ""),
            "Futures (Current Month)": s.get("futures_symbol", ""),
            "Lot Size": s["lot_size"],
        } for s in sorted(fno_syms, key=lambda x: x["symbol"])]
        sheet(mapping, "FnO Mapping", sort_col="Stock")

        stats = pd.DataFrame([
            {"Metric": "Total Equity Stocks",  "Value": len(all_syms)},
            {"Metric": "F&O Enabled Stocks",   "Value": len(fno_syms)},
            {"Metric": "Indices",               "Value": len(indices)},
            {"Metric": "Large Cap (tagged)",    "Value": sum(1 for s in all_syms if s["market_cap_type"] == "LARGE")},
            {"Metric": "Mid Cap (tagged)",      "Value": sum(1 for s in all_syms if s["market_cap_type"] == "MID")},
            {"Metric": "Unclassified",          "Value": sum(1 for s in all_syms if not s["market_cap_type"])},
            {"Metric": "Enriched On",           "Value": date.today().isoformat()},
        ])
        stats.to_excel(writer, sheet_name="Summary", index=False)

    print(f"  Saved Excel        -> {path}")


if __name__ == "__main__":
    print("\n  Loading symbols...")
    with open(os.path.join(DATA_DIR, "symbols_all.json"), encoding="utf-8") as f:
        all_symbols = json.load(f)
    with open(os.path.join(DATA_DIR, "symbols_indices.json"), encoding="utf-8") as f:
        indices = json.load(f)

    print("  Enriching...")
    all_symbols = enrich(all_symbols)
    fno_symbols = [s for s in all_symbols if s["is_fno"]]

    large = sum(1 for s in all_symbols if s["market_cap_type"] == "LARGE")
    mid   = sum(1 for s in all_symbols if s["market_cap_type"] == "MID")
    blank = sum(1 for s in all_symbols if not s["market_cap_type"])
    with_sector = sum(1 for s in all_symbols if s["sector"])

    print(f"\n  {'='*50}")
    print(f"  Total stocks       : {len(all_symbols)}")
    print(f"  LARGE cap tagged   : {large}")
    print(f"  MID cap tagged     : {mid}")
    print(f"  Unclassified       : {blank}  (fill manually in Excel)")
    print(f"  Sector tagged      : {with_sector}")
    print(f"  {'='*50}")

    print("\n  Saving...")
    save_json(all_symbols, "symbols_all.json")
    save_json(fno_symbols, "symbols_fno.json")
    save_excel(all_symbols, fno_symbols, indices, "symbols_master.xlsx")

    print("\n  Done.\n")
