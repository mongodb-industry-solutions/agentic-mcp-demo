# mcp_servers/billing_service.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: Billing & Invoice Management with Transactional Add-on Booking

Query invoices, explain charges, recommend cost optimizations, and book add-ons with full transaction support.

Use this service when users say:
- Query: "invoice", "bill", "charges", "current invoice", "what do I owe"
- Analyze: "why expensive", "why so high", "cost breakdown", "explain charges"
- Optimize: "reduce costs", "save money", "cheaper option", "optimize bill"
- Book: "activate", "book", "purchase", "add option", "subscribe to"
- Confirm: "confirm", "yes", "proceed", "approve transaction"
- Cancel: "cancel", "no", "abort", "rollback"

Capabilities:
- Retrieve current and historical invoices
- Analyze cost drivers and provide explanations
- Recommend cost-saving options (tariff changes, add-ons)
- Book add-ons with two-phase commit (pending → confirmed)
- Cancel pending transactions
- Full audit trail of all transactions
"""

import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)

mcp = FastMCP("billing_service")
logger = logging.getLogger("billing_service")

DATA_FILE = Path("/tmp/billing_data.json")

# ============================================================================
# DATA STRUCTURE & PERSISTENCE
# ============================================================================

def _load_data() -> dict:
    """Load billing data from file"""
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to load data: {e}")

    # Initialize with realistic telco data structure
    return {
        "customer": {
            "id": "CUST-2026-001",
            "name": "Demo Customer",
            "plan": {
                "id": "PLAN-STD-L",
                "name": "Standard Mobile L",
                "monthly_fee": 39.99,
                "included": {
                    "data_gb": 20,
                    "calls": "unlimited",
                    "sms": "unlimited",
                    "eu_roaming": True
                }
            }
        },
        "current_invoice": {
            "id": "INV-2026-02",
            "period": "2026-02",
            "period_name": "February 2026",
            "status": "finalized",  # ← FINALIZED = kann nicht mehr geändert werden
            "due_date": "2026-02-28",
            "base_fee": 39.99,
            "charges": [
                {
                    "type": "extra_data",
                    "date": "2026-02-05",
                    "description": "Extra data 3 GB",
                    "amount": 5.99,
                    "reason": "Exceeded plan limit of 20 GB"
                },
                {
                    "type": "roaming_usa",
                    "date": "2026-02-08",
                    "description": "US roaming (3 days)",
                    "amount": 24.99,
                    "details": {
                        "country": "USA",
                        "days": 3,
                        "data_mb": 850,
                        "rate": "8.33 EUR/day"
                    }
                },
                {
                    "type": "roaming_uk",
                    "date": "2026-02-12",
                    "description": "UK roaming (2 days)",
                    "amount": 0.00,
                    "details": {
                        "country": "UK",
                        "days": 2,
                        "included_in_eu": True
                    }
                }
            ]
        },
        "next_invoice": {
            "id": "INV-2026-03",
            "period": "2026-03",
            "period_name": "March 2026",
            "status": "preview",  # ← PREVIEW = noch nicht final
            "base_fee": 39.99,
            "charges": []  # ← Neue Charges kommen hier rein
        },
        "available_addons": {
            "roaming_world_monthly": {
                "id": "ADDON-WORLD-MONTHLY",
                "name": "World Roaming Plus",
                "description": "Unlimited data + calls in 150+ countries including USA, Asia, Australia",
                "type": "subscription",
                "price": 19.99,
                "billing_type": "monthly_recurring",
                "cancellation": "monthly",
                "activation_mode": "immediate",
                "covers": ["USA", "Canada", "Mexico", "UK", "China", "Japan", "Australia", "150+ countries"],
                "proration_eligible": False  # ← Full month charge
            },
            "extra_data_pack": {
                "id": "ADDON-DATA-10GB",
                "name": "Extra Data 10 GB",
                "description": "10 GB additional high-speed data, valid current billing period",
                "type": "one_time",
                "price": 9.99,
                "billing_type": "one_time",
                "activation_mode": "immediate",
                "proration_eligible": False
            }
        },
        "active_addons": [],
        "pending_transaction": None,
        "transaction_history": []
    }

def _save_data(data: dict):
    """Save billing data to file"""
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Data saved")
    except Exception as e:
        logger.error(f"Failed to save data: {e}")

def _calculate_total(invoice: dict) -> float:
    """Calculate invoice total dynamically"""
    return invoice["base_fee"] + sum(charge["amount"] for charge in invoice["charges"])

def _generate_transaction_id() -> str:
    """Generate unique transaction ID"""
    return f"TXN-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

# ============================================================================
# CORE TOOLS
# ============================================================================

@mcp.tool()
def get_current_invoice() -> str:
    """
    Retrieve the current invoice with all charges.
    Shows base fee and any additional charges with descriptions.

    Returns:
        Formatted invoice with line-by-line breakdown
    """
    logger.info("Retrieving current invoice")

    data = _load_data()
    invoice = data["current_invoice"]
    plan = data["customer"]["plan"]

    total = _calculate_total(invoice)

    # Check for pending transaction
    pending_note = ""
    if data["pending_transaction"]:
        txn = data["pending_transaction"]
        pending_note = (
            f"\n\n⏳ [yellow]Pending transaction: {txn['transaction_id']}[/yellow]\n"
            f"[dim]Use confirm_transaction() or cancel_transaction()[/dim]"
        )

    # Build response
    response = (
        f"📄 [bold cyan]Invoice {invoice['id']}[/bold cyan]\n"
        f"📅 Period: {invoice['period_name']}\n"
        f"📅 Status: [green]{invoice['status'].upper()}[/green] (cannot be changed)\n"
        f"📅 Due: {invoice.get('due_date', 'N/A')}\n"
        f"💰 [bold]Total: [yellow]{total:.2f} EUR[/yellow][/bold]\n\n"
        f"[bold]Breakdown:[/bold]\n"
        f"  • Base fee ({plan['name']}): [cyan]{invoice['base_fee']:.2f} EUR[/cyan]\n"
    )

    if invoice["charges"]:
        for charge in invoice["charges"]:
            if charge["amount"] == 0:
                color = "green"
                amount_text = "0.00 EUR (included)"
            else:
                color = "yellow"
                amount_text = f"{charge['amount']:.2f} EUR"

            response += f"  • {charge['description']}: [{color}]{amount_text}[/{color}]\n"

    response += pending_note

    return response

@mcp.tool()
def get_next_invoice_preview() -> str:
    """
    Preview the next invoice with projected charges.
    Shows what the next month's bill will look like.

    Returns:
        Preview of next invoice with current projected charges
    """
    logger.info("Retrieving next invoice preview")

    data = _load_data()
    next_invoice = data["next_invoice"]
    plan = data["customer"]["plan"]

    total = _calculate_total(next_invoice)

    response = (
        f"📄 [bold cyan]Invoice {next_invoice['id']} (PREVIEW)[/bold cyan]\n"
        f"📅 Period: {next_invoice['period_name']}\n"
        f"📅 Status: [yellow]{next_invoice['status'].upper()}[/yellow] (subject to change)\n"
        f"💰 [bold]Projected Total: [yellow]{total:.2f} EUR[/yellow][/bold]\n\n"
        f"[bold]Breakdown:[/bold]\n"
        f"  • Base fee ({plan['name']}): [cyan]{next_invoice['base_fee']:.2f} EUR[/cyan]\n"
    )

    if next_invoice["charges"]:
        for charge in next_invoice["charges"]:
            color = "yellow" if charge["amount"] > 0 else "green"
            response += f"  • {charge['description']}: [{color}]{charge['amount']:.2f} EUR[/{color}]\n"
    else:
        response += f"  [dim](No additional charges projected)[/dim]\n"

    response += (
        f"\n[dim]Note: This is a preview. Final charges may differ based on actual usage.[/dim]"
    )

    return response

@mcp.tool()
def analyze_charges(focus: str = "all") -> str:
    """
    Analyze charges and identify cost drivers.
    Provides explanations for why the bill is high.

    Args:
        focus: What to analyze - "all", "roaming", "data", or specific charge type

    Returns:
        Analysis with cost drivers, patterns, and root causes
    """
    logger.info(f"Analyzing charges with focus: {focus}")

    data = _load_data()
    invoice = data["current_invoice"]
    plan = data["customer"]["plan"]

    total = _calculate_total(invoice)
    extra_costs = total - invoice["base_fee"]

    if extra_costs == 0:
        return (
            f"✅ [green]Your bill is exactly as expected![/green]\n\n"
            f"Base fee: {invoice['base_fee']:.2f} EUR\n"
            f"No additional charges.\n"
            f"You stayed within your plan limits."
        )

    # Group charges by type
    charge_types = {}
    for charge in invoice["charges"]:
        if charge["amount"] == 0:
            continue

        charge_type = charge["type"]
        if charge_type not in charge_types:
            charge_types[charge_type] = []
        charge_types[charge_type].append(charge)

    response = (
        f"📊 [bold]Cost Analysis[/bold]\n\n"
        f"Base plan: {invoice['base_fee']:.2f} EUR ({plan['name']})\n"
        f"Extra charges: [yellow]{extra_costs:.2f} EUR[/yellow]\n"
        f"[bold]Total: {total:.2f} EUR (+{(extra_costs/invoice['base_fee']*100):.0f}%)[/bold]\n\n"
        f"[bold]Cost Drivers:[/bold]\n"
    )

    # Analyze each charge type
    for charge_type, charges in sorted(charge_types.items(),
                                       key=lambda x: sum(c["amount"] for c in x[1]),
                                       reverse=True):
        total_for_type = sum(c["amount"] for c in charges)
        count = len(charges)

        response += f"\n[yellow]• {charge_type.replace('_', ' ').title()}[/yellow]\n"
        response += f"  Amount: {total_for_type:.2f} EUR ({count} charge(s))\n"

        if "roaming" in charge_type:
            countries = [c["details"].get("country", "Unknown") for c in charges if "details" in c]
            response += f"  Countries: {', '.join(set(countries))}\n"

            if charge_type.startswith("roaming_usa"):
                response += f"  [dim]USA is not included in your plan's EU roaming[/dim]\n"

        elif charge_type == "extra_data":
            for charge in charges:
                response += f"  [dim]{charge.get('reason', 'Additional data purchased')}[/dim]\n"

    response += (
        f"\n💡 [bold]Tip:[/bold] Use [cyan]get_cost_reduction_options()[/cyan] "
        f"to see how you can reduce these costs."
    )

    return response

@mcp.tool()
def get_cost_reduction_options(cost_type: str = "all") -> str:
    """
    Get personalized recommendations to reduce costs.
    Suggests add-ons, plan changes, or usage tips based on current charges.

    Args:
        cost_type: Type of cost to optimize - "roaming", "data", or "all"

    Returns:
        List of actionable recommendations with potential savings
    """
    logger.info(f"Getting cost reduction options for: {cost_type}")

    data = _load_data()
    invoice = data["current_invoice"]
    addons = data["available_addons"]
    active_addon_ids = [a["addon_id"] for a in data["active_addons"]]

    # Identify relevant charges
    roaming_charges = [c for c in invoice["charges"]
                      if "roaming" in c["type"] and c["amount"] > 0]
    data_charges = [c for c in invoice["charges"] if c["type"] == "extra_data"]

    response = f"💡 [bold]Cost Reduction Options[/bold]\n\n"
    recommendations = []

    # Roaming optimization
    if (cost_type == "all" or cost_type == "roaming") and roaming_charges:
        usa_charges = [c for c in roaming_charges if "usa" in c["type"].lower()]
        if usa_charges:
            world_addon = addons["roaming_world_monthly"]

            if world_addon["id"] not in active_addon_ids:
                daily_rate = 8.33
                breakeven_days = world_addon["price"] / daily_rate

                recommendations.append({
                    "type": "roaming",
                    "title": f"Book {world_addon['name']}",
                    "description": world_addon["description"],
                    "option_cost": world_addon["price"],
                    "billing_type": world_addon["billing_type"],
                    "addon_id": world_addon["id"],
                    "benefit": f"Avoid {daily_rate:.2f} EUR/day roaming charges",
                    "breakeven": f"Break-even after {breakeven_days:.0f} roaming days per month"
                })

    # Data optimization
    if (cost_type == "all" or cost_type == "data") and data_charges:
        total_data = sum(c["amount"] for c in data_charges)
        data_addon = addons["extra_data_pack"]

        if data_addon["id"] not in active_addon_ids:
            if total_data >= data_addon["price"] * 0.6:
                recommendations.append({
                    "type": "data",
                    "title": f"Book {data_addon['name']}",
                    "description": data_addon["description"],
                    "option_cost": data_addon["price"],
                    "billing_type": data_addon["billing_type"],
                    "addon_id": data_addon["id"],
                    "benefit": "More data for less cost per GB"
                })

    if not recommendations:
        return (
            f"✅ [green]No optimization opportunities found.[/green]\n\n"
            f"Your current usage is already cost-efficient.\n"
            f"Keep using your plan as-is!"
        )

    for i, rec in enumerate(recommendations, 1):
        response += f"[bold cyan]{i}. {rec['title']}[/bold cyan]\n"
        response += f"   {rec['description']}\n"

        if rec['billing_type'] == "monthly_recurring":
            response += f"   Type: [yellow]Monthly subscription[/yellow] (cancel anytime)\n"
        else:
            response += f"   Type: One-time purchase\n"

        response += f"   Price: [green]{rec['option_cost']:.2f} EUR"
        if rec['billing_type'] == "monthly_recurring":
            response += "/month"
        response += "[/green]\n"

        if rec.get('benefit'):
            response += f"   💡 {rec['benefit']}\n"

        if rec.get('breakeven'):
            response += f"   [dim]{rec['breakeven']}[/dim]\n"

        response += f"   [dim]Book with: initiate_addon_booking('{rec['addon_id']}')[/dim]\n\n"

    return response

@mcp.tool()
def initiate_addon_booking(addon_id: str) -> str:
    """
    Initiate add-on booking transaction (Phase 1: Pending).
    Creates a pending transaction that must be confirmed.

    Args:
        addon_id: ID of the add-on to book (e.g., "ADDON-WORLD-MONTHLY")

    Returns:
        Transaction summary awaiting confirmation
    """
    logger.info(f"Initiating booking for add-on: {addon_id}")

    data = _load_data()
    addons = data["available_addons"]

    if data["pending_transaction"]:
        existing = data["pending_transaction"]
        return (
            f"⚠️ [yellow]You have a pending transaction:[/yellow]\n\n"
            f"Transaction ID: {existing['transaction_id']}\n"
            f"Add-on: {existing['addon_name']}\n\n"
            f"Please confirm with [cyan]confirm_transaction()[/cyan] "
            f"or cancel with [cyan]cancel_transaction()[/cyan] first."
        )

    addon = None
    for addon_data in addons.values():
        if addon_data["id"] == addon_id:
            addon = addon_data
            break

    if not addon:
        available = ", ".join([a["id"] for a in addons.values()])
        return f"❌ [red]Add-on '{addon_id}' not found.[/red]\n\nAvailable: {available}"

    active_addon_ids = [a["addon_id"] for a in data["active_addons"]]
    if addon_id in active_addon_ids:
        return f"ℹ️ [yellow]{addon['name']} is already active.[/yellow]"

    # Calculate impact on invoices
    current_invoice = data["current_invoice"]
    next_invoice = data["next_invoice"]
    plan = data["customer"]["plan"]

    current_total = _calculate_total(current_invoice)
    next_total_before = _calculate_total(next_invoice)
    next_total_after = next_total_before + addon["price"]

    # Future benefit note
    benefit_note = ""
    if addon_id == "ADDON-WORLD-MONTHLY":
        benefit_note = (
            f"\n💡 [bold cyan]What You Get:[/bold cyan]\n"
            f"Unlimited roaming in 150+ countries starting immediately.\n"
            f"No more daily roaming fees (e.g., 8.33 EUR/day in USA).\n"
        )

    # Create pending transaction
    transaction_id = _generate_transaction_id()

    transaction = {
        "transaction_id": transaction_id,
        "status": "pending",
        "addon_id": addon_id,
        "addon_name": addon["name"],
        "addon_price": addon["price"],
        "billing_type": addon.get("billing_type", "one_time"),
        "cancellation_policy": addon.get("cancellation", "none"),
        "current_invoice_id": current_invoice["id"],
        "current_invoice_total": current_total,
        "next_invoice_id": next_invoice["id"],
        "next_invoice_total_after": next_total_after,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
        "requires_financial_disclaimer": False
    }

    data["pending_transaction"] = transaction
    _save_data(data)

    # Build response
    response = (
        f"⏳ [bold yellow]Transaction Initiated[/bold yellow]\n\n"
        f"🎫 Transaction ID: [cyan]{transaction_id}[/cyan]\n"
        f"📦 Add-on: [bold]{addon['name']}[/bold]\n"
    )

    if addon.get("billing_type") == "monthly_recurring":
        response += f"💳 Type: [yellow]Monthly Subscription[/yellow] (cancel anytime)\n"
    else:
        response += f"💳 Type: One-time purchase\n"

    response += benefit_note

    response += (
        f"\n📊 [bold]Billing Impact:[/bold]\n\n"
        f"📄 [bold]Next Invoice ({next_invoice['id']}) - {next_invoice['period_name']}:[/bold]\n"
        f"   Base plan: {plan['monthly_fee']:.2f} EUR\n"
        f"   Add-on: +{addon['price']:.2f} EUR\n"
        f"   [bold]Total: [yellow]{next_total_after:.2f} EUR[/yellow][/bold]\n"
    )

    response += (
        f"\n[dim]Note: Your current invoice ({current_invoice['id']}) is finalized and remains {current_total:.2f} EUR[/dim]\n"
        f"\n⏰ [dim]This transaction expires in 5 minutes[/dim]\n\n"
        f"[cyan]Confirm[/cyan] or [cyan]Cancel[/cyan]?"
    )

    return response

@mcp.tool()
def confirm_transaction() -> str:
    """
    Confirm and commit the pending transaction (Phase 2: Commit).
    Activates the add-on and applies charges to NEXT invoice.

    Returns:
        Confirmation with activation details
    """
    logger.info("Confirming pending transaction")

    data = _load_data()

    if not data["pending_transaction"]:
        return "❌ [red]No pending transaction to confirm.[/red]"

    transaction = data["pending_transaction"]

    # Check if expired
    expires_at = datetime.fromisoformat(transaction["expires_at"])
    if datetime.now() > expires_at:
        data["pending_transaction"] = None
        _save_data(data)
        return (
            f"⏰ [red]Transaction {transaction['transaction_id']} has expired.[/red]\n\n"
            f"Please initiate a new booking."
        )

    addon_id = transaction["addon_id"]
    addon = next((a for a in data["available_addons"].values() if a["id"] == addon_id), None)

    if not addon:
        return f"❌ [red]Add-on configuration error.[/red]"

    # COMMIT PHASE: Apply to NEXT invoice
    data["next_invoice"]["charges"].append({
        "type": "addon",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "description": f"{addon['name']}",
        "amount": addon["price"],
        "transaction_id": transaction["transaction_id"]
    })

    # Activate addon
    validity_days = 30

    activation = {
        "addon_id": addon_id,
        "activated_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(days=validity_days)).isoformat(),
        "price": addon["price"],
        "billing_type": addon.get("billing_type", "one_time"),
        "transaction_id": transaction["transaction_id"]
    }
    data["active_addons"].append(activation)

    # Update transaction status
    transaction["status"] = "confirmed"
    transaction["confirmed_at"] = datetime.now().isoformat()
    data["transaction_history"].append(transaction)
    data["pending_transaction"] = None

    _save_data(data)

    # Build confirmation
    next_invoice_total = _calculate_total(data["next_invoice"])
    plan = data["customer"]["plan"]

    response = (
        f"✅ [bold green]Transaction Confirmed![/bold green]\n\n"
        f"🎫 Transaction ID: {transaction['transaction_id']}\n"
        f"📦 Add-on: [bold]{addon['name']}[/bold]\n\n"
        f"🎉 [bold green]{addon['name']} is now active![/bold green]\n"
        f"📅 Activated: {datetime.now().strftime('%Y-%m-%d')}\n"
    )

    if addon.get("covers"):
        response += f"🌍 Coverage: {', '.join(addon['covers'][:4])}"
        if len(addon['covers']) > 4:
            response += f", +{len(addon['covers'])-4} more"
        response += "\n"

    response += (
        f"\n📊 [bold]Next Invoice ({transaction['next_invoice_id']}):[/bold]\n"
        f"   Base plan: {plan['monthly_fee']:.2f} EUR\n"
        f"   {addon['name']}: +{addon['price']:.2f} EUR\n"
        f"   [bold]Total: [yellow]{next_invoice_total:.2f} EUR[/yellow][/bold]\n"
    )

    if addon.get("billing_type") == "monthly_recurring":
        response += f"\n[dim]This is a monthly subscription. Cancel anytime to stop future charges.[/dim]\n"

    if addon_id == "ADDON-WORLD-MONTHLY":
        response += f"\n💡 Your international roaming is now included!"

    return response

@mcp.tool()
def cancel_transaction() -> str:
    """
    Cancel the pending transaction (Rollback).
    No changes are applied.

    Returns:
        Cancellation confirmation
    """
    logger.info("Canceling pending transaction")

    data = _load_data()

    if not data["pending_transaction"]:
        return "ℹ️ No pending transaction to cancel."

    transaction = data["pending_transaction"]

    transaction["status"] = "cancelled"
    transaction["cancelled_at"] = datetime.now().isoformat()
    data["transaction_history"].append(transaction)
    data["pending_transaction"] = None

    _save_data(data)

    response = (
        f"❌ [yellow]Transaction Cancelled[/yellow]\n\n"
        f"🎫 Transaction ID: {transaction['transaction_id']}\n"
        f"📦 Add-on: {transaction['addon_name']}\n\n"
        f"✅ No changes were made.\n"
        f"💰 Current invoice remains: {transaction['current_invoice_total']:.2f} EUR\n"
        f"💰 Next invoice projection remains: {transaction['next_invoice_total_before']:.2f} EUR"
    )

    return response

@mcp.tool()
def get_transaction_history(limit: int = 5) -> str:
    """
    View transaction history with all confirmed and cancelled bookings.

    Args:
        limit: Maximum number of transactions to show (default: 5)

    Returns:
        List of recent transactions with status and details
    """
    logger.info(f"Retrieving transaction history (limit: {limit})")

    data = _load_data()
    history = data["transaction_history"]

    if not history:
        return "📋 No transaction history yet."

    recent = sorted(history, key=lambda x: x["created_at"], reverse=True)[:limit]

    response = f"📋 [bold]Transaction History (Last {len(recent)})[/bold]\n\n"

    for txn in recent:
        status_icon = {"confirmed": "✅", "cancelled": "❌"}.get(txn["status"], "❓")
        status_color = {"confirmed": "green", "cancelled": "yellow"}.get(txn["status"], "white")

        response += f"{status_icon} [{status_color}]{txn['transaction_id']}[/{status_color}]\n"
        response += f"   Add-on: {txn['addon_name']}\n"
        response += f"   Amount: {txn['addon_price']:.2f} EUR\n"
        response += f"   Status: {txn['status'].upper()}\n"
        response += f"   Date: {txn['created_at'][:10]}\n\n"

    return response

@mcp.tool()
def list_active_addons() -> str:
    """
    List all currently active add-ons and their expiration dates.

    Returns:
        List of active add-ons with validity information
    """
    logger.info("Listing active add-ons")

    data = _load_data()
    active = data["active_addons"]

    if not active:
        return "📋 No active add-ons.\n\nYou're using your base plan only."

    response = f"📋 [bold]Active Add-Ons ({len(active)})[/bold]\n\n"

    for activation in active:
        addon_id = activation["addon_id"]
        addon = next((a for a in data["available_addons"].values() if a["id"] == addon_id), None)

        if addon:
            expires = datetime.fromisoformat(activation["expires_at"])
            days_left = (expires - datetime.now()).days

            response += f"[bold cyan]• {addon['name']}[/bold cyan]\n"
            response += f"  Activated: {activation['activated_at'][:10]}\n"

            if activation.get("billing_type") == "monthly_recurring":
                response += f"  Type: Monthly subscription\n"
                response += f"  Next renewal: {activation['expires_at'][:10]} ({days_left} days)\n"
                response += f"  Monthly cost: {activation['price']:.2f} EUR\n"
            else:
                response += f"  Expires: {activation['expires_at'][:10]} ({days_left} days left)\n"
                response += f"  Cost: {activation['price']:.2f} EUR\n"

            response += "\n"

    return response

if __name__ == "__main__":
    logger.info("🚀 Starting Billing Service with Transaction Support...")
    mcp.run()
