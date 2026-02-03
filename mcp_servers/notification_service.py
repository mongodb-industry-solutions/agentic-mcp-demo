#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
SERVER: Notification & Alert Service
Sends push notifications via ntfy.sh (no auth required).
Use this for transaction confirmations, urgent alerts, and user notifications.
"""
import logging
logging.basicConfig(level=logging.ERROR)
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("notification_service")

DEMO_CHANNEL = "agentic-mcp-demo"

@mcp.resource("config://channel")
def get_channel() -> str:
    """Returns the active notification channel"""
    return f"https://ntfy.sh/{DEMO_CHANNEL}"

@mcp.tool()
def send_alert(message: str, priority: str = "default") -> str:
    """
    Sends a push notification to the user's device.
    Use for: transaction confirmations, urgent alerts, price thresholds.
    Args:
        message: The notification text.
        priority: 'urgent', 'high', 'default', or 'low'.
    """
    prio_map = {"urgent": 5, "high": 4, "default": 3, "low": 1}
    p = prio_map.get(priority.lower(), 3)

    try:
        resp = requests.post(
            f"https://ntfy.sh/{DEMO_CHANNEL}",
            data=message.encode("utf-8"),
            headers={"Title": "🚨 Agent Alert", "Priority": str(p), "Tags": "bell"},
            timeout=5
        )
        resp.raise_for_status()
        return f"✅ Alert sent to ntfy.sh/{DEMO_CHANNEL}"
    except Exception as e:
        return f"❌ Failed to send: {e}"

if __name__ == "__main__":
    print(f"📡 Notification server running. Subscribe: https://ntfy.sh/{DEMO_CHANNEL}")
    mcp.run()
