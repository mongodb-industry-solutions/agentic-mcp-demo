#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
Portfolio Service - Personal Investment Portfolio Management

Track securities by ISIN with live market prices fetched from Yahoo Finance,
quantities owned, and security names. Backed by MongoDB.

Use this service when users say:
- Add:     "add ISIN <code> to my portfolio", "add <ISIN> quantity <n>",
           "add MongoDB currency USD", "add Apple to portfolio", "add <name> <currency>",
           "I bought <ISIN>", "new position", "add position"
- Update:  "update ISIN <code> quantity <n>", "update Microsoft 23",
           "set quantity of <name> to <n>", "I now hold <n> shares of <name>",
           "<name> quantity now <n>", "<name> quantity <n>", "change <name> to <n>"
- Delete:  "delete ISIN <code>", "remove <name> from portfolio",
           "delete position", "sell all <name>"
- View:    "show my portfolio", "list my holdings", "what stocks do I own",
           "portfolio overview", "what is my portfolio worth"
- Refresh: "refresh prices", "update portfolio prices", "get current prices"
"""

import logging, os, re, datetime
import requests
from pymongo import MongoClient, ASCENDING
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp    = FastMCP("portfolio_service")
logger = logging.getLogger("portfolio_service")

YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
YAHOO_CHART  = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_QUOTE  = "https://query1.finance.yahoo.com/v7/finance/quote"
REQ_HEADERS  = {"User-Agent": "Mozilla/5.0"}

mongo_client  = MongoClient(os.environ["MONGODB_URI"])
db            = mongo_client["agent_registry"]
collection    = db["portfolio"]
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def _ensure_indexes():
    collection.create_index([("ISIN", ASCENDING)], unique=True, name="isin_unique")
    collection.create_index([("name", ASCENDING)], name="name_idx")


def _fetch_security_info(isin: str) -> dict:
    """
    Look up security name and current price from Yahoo Finance.
    Returns a dict with name, price, currency, ticker.
    Falls back gracefully if lookup fails.
    """
    try:
        resp = requests.get(
            YAHOO_SEARCH,
            params={"q": isin, "lang": "en-US", "region": "US"},
            headers=REQ_HEADERS,
            timeout=10
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes", [])

        if not quotes:
            logger.warning(f"No Yahoo Finance results for ISIN {isin}")
            return {"name": isin, "price": 0.0, "currency": "N/A", "ticker": None}

        best   = quotes[0]
        ticker = best.get("symbol", isin)
        name   = (best.get("longname") or best.get("shortname") or ticker).strip()

        price_resp = requests.get(
            YAHOO_CHART.format(ticker=ticker),
            params={"interval": "1d", "range": "1d"},
            headers=REQ_HEADERS,
            timeout=10
        )
        price_resp.raise_for_status()
        meta     = price_resp.json()["chart"]["result"][0]["meta"]
        price    = float(meta.get("regularMarketPrice", 0.0))
        currency = meta.get("currency", "N/A")

        return {"name": name, "price": price, "currency": currency, "ticker": ticker}

    except Exception as e:
        logger.error(f"Price lookup failed for {isin}: {e}")
        return {"name": isin, "price": 0.0, "currency": "N/A", "ticker": None}


ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')


def _resolve_isin(ticker: str, name: str) -> str | None:
    """
    Resolve the ISIN for a known ticker+name using three sources in order:

    1. Yahoo Finance v7 quote — fast, works for many European securities.
    2. Yahoo Finance v1 search by ticker — sometimes includes isin in quotes.
    3. OpenAI — reliable fallback; GPT has accurate ISIN data for all major
       publicly traded companies worldwide.

    Returns a validated 12-character ISIN, or None if all sources fail.
    """
    def _valid(s: str) -> str | None:
        return s if s and ISIN_RE.match(s.upper()) else None

    # Stage 1 — Yahoo Finance v7 quote
    try:
        resp = requests.get(
            YAHOO_QUOTE, params={"symbols": ticker},
            headers=REQ_HEADERS, timeout=10
        )
        resp.raise_for_status()
        for item in resp.json().get("quoteResponse", {}).get("result", []):
            if v := _valid(item.get("isin", "")):
                logger.debug(f"ISIN from v7 quote: {v}")
                return v
    except Exception as e:
        logger.debug(f"v7 quote ISIN lookup failed for {ticker}: {e}")

    # Stage 2 — Yahoo Finance search by ticker symbol
    try:
        resp = requests.get(
            YAHOO_SEARCH, params={"q": ticker},
            headers=REQ_HEADERS, timeout=10
        )
        resp.raise_for_status()
        for q in resp.json().get("quotes", []):
            if q.get("symbol") == ticker:
                if v := _valid(q.get("isin", "")):
                    logger.debug(f"ISIN from search: {v}")
                    return v
    except Exception as e:
        logger.debug(f"search ISIN lookup failed for {ticker}: {e}")

    # Stage 3 — OpenAI
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"What is the ISIN for this publicly traded security?\n"
                    f"Company: {name}\n"
                    f"Ticker: {ticker}\n\n"
                    f"Reply with ONLY the 12-character ISIN (e.g. DE0005190003). "
                    f"If you are not certain, reply with exactly: UNKNOWN"
                )
            }],
            temperature=0,
            max_tokens=20,
        )
        candidate = resp.choices[0].message.content.strip().upper()
        if v := _valid(candidate):
            logger.debug(f"ISIN from OpenAI: {v}")
            return v
    except Exception as e:
        logger.debug(f"OpenAI ISIN lookup failed for {ticker}: {e}")

    return None


def _find_position(identifier: str) -> dict | None:
    """
    Find a position by trying, in order:
    1. Exact ISIN match          (e.g. 'DE0005190003')
    2. Case-insensitive ticker   (e.g. 'BMW' matches 'BMW.DE')
    3. Case-insensitive name     (e.g. 'MongoDB' matches 'MongoDB, Inc.')
    """
    doc = collection.find_one({"ISIN": identifier.upper()})
    if doc:
        return doc
    doc = collection.find_one(
        {"ticker": {"$regex": re.escape(identifier), "$options": "i"}}
    )
    if doc:
        return doc
    return collection.find_one(
        {"name": {"$regex": re.escape(identifier), "$options": "i"}}
    )


def _search_by_name(name: str, currency: str = "USD") -> dict:
    """
    Search Yahoo Finance by company name, optionally filtering by currency.
    Returns dict with ticker, name, price, currency — or raises on failure.
    """
    resp = requests.get(
        YAHOO_SEARCH,
        params={"q": name, "lang": "en-US", "region": "US"},
        headers=REQ_HEADERS,
        timeout=10
    )
    resp.raise_for_status()
    quotes = [q for q in resp.json().get("quotes", []) if q.get("quoteType") == "EQUITY"]

    if not quotes:
        raise ValueError(f"No equity found for '{name}'")

    # Prefer listings whose exchange matches the requested currency
    CURRENCY_EXCHANGES = {
        "USD": {"NMS", "NGM", "NYQ", "PCX", "BTS"},
        "EUR": {"GER", "PAR", "AMS", "MCE", "MIL"},
        "GBP": {"LSE"},
        "CHF": {"EBS"},
    }
    preferred_exchanges = CURRENCY_EXCHANGES.get(currency.upper(), set())
    preferred = [q for q in quotes if q.get("exchange") in preferred_exchanges]
    best = preferred[0] if preferred else quotes[0]

    ticker    = best["symbol"]
    name_out  = (best.get("longname") or best.get("shortname") or ticker).strip()

    price_resp = requests.get(
        YAHOO_CHART.format(ticker=ticker),
        params={"interval": "1d", "range": "1d"},
        headers=REQ_HEADERS,
        timeout=10
    )
    price_resp.raise_for_status()
    meta         = price_resp.json()["chart"]["result"][0]["meta"]
    price        = float(meta.get("regularMarketPrice", 0.0))
    currency_out = meta.get("currency", "N/A")
    isin_out = meta.get("isin") or _resolve_isin(ticker, name_out)

    return {"ticker": ticker, "name": name_out, "price": price,
            "currency": currency_out, "isin": isin_out}


@mcp.tool()
def add_position(isin: str, quantity: int = 0) -> str:
    """
    Add a security to the portfolio by ISIN. Automatically fetches the current
    market price and security name from Yahoo Finance.

    Args:
        isin:     ISIN of the security (e.g. 'US0231351067')
        quantity: Number of shares/units held (default: 0)
    """
    isin = isin.upper().strip()

    if collection.find_one({"ISIN": isin}):
        return (
            f"❌ ISIN {isin} already exists in the portfolio. "
            f"Use update_position to change the quantity."
        )

    info = _fetch_security_info(isin)

    collection.insert_one({
        "ISIN":     isin,
        "name":     info["name"],
        "ticker":   info["ticker"],
        "price":    info["price"],
        "currency": info["currency"],
        "quantity": quantity,
        "addedAt":  datetime.datetime.now(datetime.timezone.utc),
    })

    price_str = (
        f"{info['price']:.2f} {info['currency']}"
        if info["price"] else "price unavailable"
    )
    return (
        f"✅ Added {info['name']} ({isin}) to portfolio\n"
        f"   Quantity: {quantity} | Price: {price_str}"
    )


@mcp.tool()
def add_position_by_name(name: str, currency: str = "USD", quantity: int = 0) -> str:
    """
    Add a security to the portfolio by company name when the ISIN is unknown.
    Searches Yahoo Finance to resolve the ticker, current price, and full name.
    Use when the user says e.g. 'add MongoDB', 'add Apple USD', 'add SAP EUR'.

    Args:
        name:     Company or security name (e.g. 'MongoDB', 'Apple', 'SAP')
        currency: Preferred currency to select the right listing (default: 'USD')
        quantity: Number of shares/units held (default: 0)
    """
    try:
        info = _search_by_name(name, currency)
    except Exception as e:
        return f"❌ Could not find '{name}' on Yahoo Finance: {e}"

    ticker     = info["ticker"]
    identifier = info["isin"] or ticker   # use real ISIN when available

    if collection.find_one({"ISIN": identifier}):
        return (
            f"❌ {info['name']} ({identifier}) already exists in the portfolio. "
            f"Use update_position to change the quantity."
        )

    collection.insert_one({
        "ISIN":     identifier,
        "name":     info["name"],
        "ticker":   ticker,
        "price":    info["price"],
        "currency": info["currency"],
        "quantity": quantity,
        "addedAt":  datetime.datetime.now(datetime.timezone.utc),
    })

    price_str = (
        f"{info['price']:.2f} {info['currency']}"
        if info["price"] else "price unavailable"
    )
    isin_note = f"ISIN: {identifier}" if identifier != ticker else f"ticker: {ticker}"
    return (
        f"✅ Added {info['name']} ({isin_note}) to portfolio\n"
        f"   Quantity: {quantity} | Price: {price_str}"
    )


@mcp.tool()
def update_position(identifier: str, quantity: int) -> str:
    """
    Update the quantity of a portfolio position. Accepts ISIN or security name.
    Use for any phrasing like "update BMW to 25", "BMW quantity now 25",
    "change Microsoft quantity to 10", "set Apple to 50 shares".

    Args:
        identifier: ISIN (e.g. 'US0231351067') or name (e.g. 'Microsoft', 'BMW')
        quantity:   New number of shares/units held
    """
    doc = _find_position(identifier)
    if not doc:
        return f"❌ Position '{identifier}' not found in the portfolio."

    old_qty = doc["quantity"]
    collection.update_one({"ISIN": doc["ISIN"]}, {"$set": {"quantity": quantity}})

    return (
        f"✅ Updated {doc['name']} ({doc['ISIN']})\n"
        f"   Quantity: {old_qty} → {quantity}"
    )


@mcp.tool()
def delete_position(identifier: str) -> str:
    """
    Remove a position from the portfolio. Accepts ISIN or security name.

    Args:
        identifier: ISIN (e.g. 'US0231351067') or name (e.g. 'Apple Inc.')
    """
    doc = _find_position(identifier)
    if not doc:
        return f"❌ Position '{identifier}' not found in the portfolio."

    collection.delete_one({"ISIN": doc["ISIN"]})
    return f"🗑️ Removed {doc['name']} ({doc['ISIN']}) from portfolio."


FRANKFURTER_URL  = "https://api.frankfurter.app/latest"
_rates_cache: dict  = {}
_rates_cache_ts: float = 0.0
RATES_TTL = 300  # seconds


def _get_eur_rates() -> dict:
    """
    Fetch EUR-based exchange rates from Frankfurter (free, no key needed).
    Cached for RATES_TTL seconds to avoid a network call on every list_portfolio.
    """
    global _rates_cache, _rates_cache_ts
    now = datetime.datetime.now().timestamp()
    if _rates_cache and (now - _rates_cache_ts) < RATES_TTL:
        return _rates_cache
    resp = requests.get(FRANKFURTER_URL, params={"base": "EUR"}, timeout=10)
    resp.raise_for_status()
    rates = resp.json()["rates"]
    rates["EUR"] = 1.0
    _rates_cache    = rates
    _rates_cache_ts = now
    return rates


def _convert(amount: float, from_cur: str, to_cur: str, rates: dict) -> float:
    """Convert amount from from_cur to to_cur using EUR-based rates."""
    if from_cur == to_cur:
        return amount
    # to EUR first, then to target
    eur = amount / rates[from_cur] if from_cur != "EUR" else amount
    return eur * rates[to_cur] if to_cur != "EUR" else eur


@mcp.tool()
def list_portfolio(currency: str = "EUR") -> str:
    """
    List all portfolio positions and total value normalized to a single currency.
    When positions are in different currencies, converts all totals using live
    exchange rates from Frankfurter. Default target currency is EUR.

    Args:
        currency: Target currency for the total (e.g. 'EUR', 'USD', 'GBP').
                  Defaults to EUR.
    """
    positions = list(collection.find({}, {"_id": 0}).sort("name", ASCENDING))

    if not positions:
        return "Portfolio is empty."

    currency    = currency.upper()
    native_curs = {p.get("currency", "N/A") for p in positions}

    # Fetch exchange rates when needed
    rates = {}
    needs_conversion = native_curs != {currency}
    if needs_conversion:
        try:
            rates = _get_eur_rates()
        except Exception as e:
            logger.error(f"Exchange rate fetch failed: {e}")
            rates = {}

    lines       = [f"📊 Portfolio (total in {currency}):\n"]
    total_conv  = 0.0
    conv_failed = False

    for p in positions:
        cur   = p.get("currency", "N/A")
        value = p["price"] * p["quantity"]

        if rates and cur in rates and currency in rates:
            value_conv = _convert(value, cur, currency, rates)
            total_conv += value_conv
        else:
            # Unknown currency or rate fetch failed — add as-is and flag
            total_conv += value
            if cur != currency:
                conv_failed = True

        lines.append(
            f"  {p['name']} ({p['ISIN']})\n"
            f"    Qty: {p['quantity']} | "
            f"Price: {p['price']:.2f} {cur} | "
            f"Value: {value:.2f} {cur}"
        )

    total_note = f"{total_conv:.2f} {currency}"
    if conv_failed:
        total_note += " (⚠️ some currencies could not be converted)"
    elif needs_conversion and rates:
        total_note += " (converted at live rates)"

    lines.append(f"\n  Total value: {total_note}")
    return "\n".join(lines)


@mcp.tool()
def refresh_prices() -> str:
    """
    Refresh market prices for all portfolio positions from Yahoo Finance.
    Use this to get current valuations before reviewing the portfolio.
    """
    positions = list(collection.find({}, {"_id": 0, "ISIN": 1, "ticker": 1, "name": 1}))

    if not positions:
        return "Portfolio is empty — nothing to refresh."

    updated = []
    failed  = []

    for p in positions:
        ticker = p.get("ticker")
        if not ticker:
            failed.append(p["name"])
            continue

        try:
            resp = requests.get(
                YAHOO_CHART.format(ticker=ticker),
                params={"interval": "1d", "range": "1d"},
                headers=REQ_HEADERS,
                timeout=10
            )
            resp.raise_for_status()
            meta     = resp.json()["chart"]["result"][0]["meta"]
            price    = float(meta.get("regularMarketPrice", 0.0))
            currency = meta.get("currency", "N/A")

            collection.update_one(
                {"ISIN": p["ISIN"]},
                {"$set": {"price": price, "currency": currency}}
            )
            updated.append(f"{p['name']}: {price:.2f} {currency}")

        except Exception as e:
            logger.error(f"Refresh failed for {p['name']}: {e}")
            failed.append(p["name"])

    lines = [f"🔄 Refreshed {len(updated)} position(s):"]
    lines += [f"  ✅ {u}" for u in updated]
    if failed:
        lines += [f"  ❌ Failed: {', '.join(failed)}"]

    return "\n".join(lines)


if __name__ == "__main__":
    _ensure_indexes()
    mcp.run()
