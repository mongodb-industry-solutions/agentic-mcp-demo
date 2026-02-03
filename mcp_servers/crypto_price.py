#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Cryptocurrency Price Service
Fetches real-time crypto prices (Solana/SOL).
Use this for cryptocurrency market queries, price checks, and financial data.
"""

import logging
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("crypto_service")

logging.basicConfig(level=logging.ERROR)

@mcp.resource("crypto://disclaimer")
def get_disclaimer() -> str:
    """Legal disclaimer for financial data"""
    return (
        "DISCLAIMER: This is not financial advice. "
        "Cryptocurrency is volatile. Invest responsibly."
    )

@mcp.tool()
def get_sol_price() -> float:
    """
    Fetches current Solana (SOL) price in USD.
    Use this for cryptocurrency, market, or SOL price queries.
    """
    try:
        url = "https://min-api.cryptocompare.com/data/price"
        params = {"fsym": "SOL", "tsyms": "USD"}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        return float(data.get("USD", 0.0))
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    mcp.run()
