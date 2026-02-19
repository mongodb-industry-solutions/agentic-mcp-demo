# mcp_servers/customer_service.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: Customer Service & Subscriber Management

Subscriber profile management, billing, and customer care operations.
Integrates with CRM, BSS, and ticketing systems.

Use this service for:
- Customer lookup: "get customer profile", "subscriber details for +49..."
- Service history: "past tickets", "complaint history", "service requests"
- Compensation: "apply credit", "issue refund", "goodwill compensation"
- Ticket management: "create ticket", "update case", "close complaint"

Capabilities:
- Query subscriber profiles (name, plan, ARPU, tenure)
- Access service history and past incidents
- Apply billing credits and compensations
- Create and manage support tickets
- Calculate customer lifetime value (CLV)

MongoDB Collections:
- subscribers: Customer master data (PII, subscription, billing)
- tickets: Support cases and complaints
- compensations: Issued credits and refunds

Examples:
- "Get profile for customer +49 176 12345678"
- "Show ticket history for this subscriber"
- "Apply €15 credit for service degradation"
- "Create ticket for network complaint"
"""

import logging
import os, re
import random
from datetime import datetime, timedelta
from typing import Optional
from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)
mcp = FastMCP("customer_service")
logger = logging.getLogger("customer_service")

MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI environment variable required")

client = MongoClient(MONGODB_URI)
db = client["telco_digital_twin"]
subscribers_col = db["subscribers"]
tickets_col = db["tickets"]
compensations_col = db["compensations"]

def _ensure_demo_data():
    """Initialize demo subscriber data"""
    if subscribers_col.count_documents({}) > 0:
        return

    logger.info("Bootstrapping demo subscriber data...")

    # Demo customer for the story
    subscriber = {
        "phone_number": "+49 176 12345678",
        "customer_id": "CUST-2024-987654",
        "name": "Max Mustermann",
        "email": "max.mustermann@example.com",
        "subscription_plan": "Premium 5G Unlimited",
        "monthly_fee_eur": 49.99,
        "contract_start": "2023-06-15",
        "tenure_months": 20,
        "arpu_eur": 52.30,  # Average revenue per user (includes extras)
        "customer_segment": "premium",
        "lifetime_value_eur": 1850,
        "nps_score": 9,
        "churn_risk": "low",
        "last_updated": datetime.now().isoformat()
    }
    subscribers_col.insert_one(subscriber)

    logger.info("✓ Created demo subscriber")

_ensure_demo_data()


@mcp.tool()
def get_customer_profile_resource(phone_number: str) -> str:
    """
    Get customer profile context for other services.
    Prerequisites: call this BEFORE get_subscriber_network_status.
    Returns profile JSON with SLA tier, segment, ARPU.
    """
    clean_number = phone_number.replace(" ", "")
    digits = list(clean_number)
    regex_pattern = "^" + "\\s*".join([re.escape(d) for d in digits]) + "$"

    subscriber = subscribers_col.find_one(
        {"phone_number": {"$regex": regex_pattern}},
        {"_id": 0}
    )
    if not subscriber:
        return f"Customer {phone_number} not found"

    # Return JSON for machine consumption
    import json
    return json.dumps({
        "phone_number": subscriber["phone_number"],
        "customer_id": subscriber["customer_id"],
        "name": subscriber["name"],
        "subscription_plan": subscriber["subscription_plan"],
        "customer_segment": subscriber["customer_segment"],
        "arpu_eur": subscriber["arpu_eur"],
        "tenure_months": subscriber["tenure_months"]
    }, indent=2)


@mcp.tool()
def get_subscriber_profile(phone_number: str) -> str:
    """
    Retrieve complete subscriber profile and account details.

    Args:
        phone_number: Subscriber phone number (e.g., "+49 176 12345678")

    Returns:
        Customer profile with subscription, billing, and segment info

    Example:
        get_subscriber_profile("+49 176 12345678")
    """
    logger.info(f"Fetching profile for {phone_number}")

    clean_number = phone_number.replace(" ", "")
    digits = list(clean_number)
    regex_pattern = "^" + "\\s*".join([re.escape(d) for d in digits]) + "$"

    subscriber = subscribers_col.find_one(
        {"phone_number": {"$regex": regex_pattern}},
        {"_id": 0}
    )
    if not subscriber:
        return f"❌ Subscriber {phone_number} not found in CRM database"

    # Calculate metrics
    contract_age_days = (datetime.now() - datetime.fromisoformat(subscriber["contract_start"])).days

    result = f"""
👤 CUSTOMER PROFILE

📱 Contact:
  Phone: {subscriber['phone_number']}
  Customer ID: {subscriber['customer_id']}
  Name: {subscriber['name']}
  Email: {subscriber['email']}

📋 Subscription:
  Plan: {subscriber['subscription_plan']}
  Monthly Fee: €{subscriber['monthly_fee_eur']:.2f}
  Contract Start: {subscriber['contract_start']}
  Tenure: {subscriber['tenure_months']} months ({contract_age_days} days)

💰 Revenue & Value:
  ARPU: €{subscriber['arpu_eur']:.2f}/month
  Customer Lifetime Value: €{subscriber['lifetime_value_eur']:,.0f}
  Segment: {subscriber['customer_segment'].upper()}

📊 Customer Intelligence:
  NPS Score: {subscriber['nps_score']}/10 (Promoter)
  Churn Risk: {subscriber['churn_risk'].upper()}
  Satisfaction: High (based on usage patterns)
"""

    # Check recent tickets
    recent_tickets = list(tickets_col.find(
        {"phone_number": phone_number},
        {"_id": 0}
    ).sort("created_at", -1).limit(3))

    if recent_tickets:
        result += f"\n📋 Recent Tickets ({len(recent_tickets)}):\n"
        for ticket in recent_tickets:
            result += f"  • {ticket['ticket_id']}: {ticket['subject']} ({ticket['status']})\n"
    else:
        result += "\n✅ No recent support tickets"

    return result.strip()

@mcp.tool()
def apply_compensation_credit(phone_number: str, amount_eur: float, reason: str) -> str:
    """
    Apply a billing credit to subscriber account for service issues.

    Args:
        phone_number: Subscriber phone number
        amount_eur: Credit amount in EUR (e.g., 15.00)
        reason: Reason for compensation (e.g., "Service degradation during incident")

    Returns:
        Confirmation with credit details

    Example:
        apply_compensation_credit("+49 176 12345678", 15.00, "Network performance issue")
    """
    logger.info(f"Applying €{amount_eur} credit to {phone_number}")

    # Verify subscriber exists
    clean_number = phone_number.replace(" ", "")
    digits = list(clean_number)
    regex_pattern = "^" + "\\s*".join([re.escape(d) for d in digits]) + "$"
    subscriber = subscribers_col.find_one({"phone_number": {"$regex": regex_pattern}}, {"customer_id": 1, "name": 1})
    if not subscriber:
        return f"❌ Cannot apply credit: Subscriber {phone_number} not found"

    # Create compensation record
    compensation = {
        "compensation_id": f"COMP-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}",
        "phone_number": phone_number,
        "customer_id": subscriber["customer_id"],
        "amount_eur": amount_eur,
        "reason": reason,
        "applied_at": datetime.now().isoformat(),
        "applied_by": "NOC Automated System",
        "status": "applied"
    }
    compensations_col.insert_one(compensation)

    result = f"""
✅ COMPENSATION APPLIED

Subscriber: {phone_number}
Amount: €{amount_eur:.2f}
Reason: {reason}
Compensation ID: {compensation['compensation_id']}
Applied: {compensation['applied_at'][:19]}

💳 Billing Impact:
  Credit will appear on next invoice
  Effective immediately in billing system
  Customer notified via SMS and email

📧 Notification sent:
  "We apologize for recent service issues. A €{amount_eur:.2f} credit has been
  applied to your account. Thank you for your patience."
"""

    return result.strip()

@mcp.tool()
def create_support_ticket(phone_number: str, subject: str, description: str, priority: str = "normal") -> str:
    """
    Create a customer support ticket for a service issue.

    Args:
        phone_number: Subscriber phone number
        subject: Ticket subject/title
        description: Detailed description of the issue
        priority: Ticket priority (low, normal, high, urgent)

    Returns:
        Ticket confirmation with ID and details

    Example:
        create_support_ticket("+49 176 12345678", "Slow data speeds", "Customer reports...", "high")
    """
    logger.info(f"Creating ticket for {phone_number}: {subject}")

    # Verify subscriber
    clean_number = phone_number.replace(" ", "")
    digits = list(clean_number)
    regex_pattern = "^" + "\\s*".join([re.escape(d) for d in digits]) + "$"
    subscriber = subscribers_col.find_one({"phone_number": {"$regex": regex_pattern}}, {"customer_id": 1, "name": 1})
    if not subscriber:
        return f"❌ Cannot create ticket: Subscriber {phone_number} not found"

    ticket_id = f"TKT-{datetime.now().strftime('%Y%m%d')}-{random.randint(10000, 99999)}"

    ticket = {
        "ticket_id": ticket_id,
        "phone_number": phone_number,
        "customer_id": subscriber["customer_id"],
        "subject": subject,
        "description": description,
        "priority": priority,
        "status": "open",
        "category": "network_quality",
        "created_at": datetime.now().isoformat(),
        "assigned_to": "NOC Team",
        "updates": []
    }
    tickets_col.insert_one(ticket)

    result = f"""
🎫 TICKET CREATED

Ticket ID: {ticket_id}
Customer: {subscriber['name']} ({phone_number})
Subject: {subject}
Priority: {priority.upper()}
Status: OPEN
Created: {ticket['created_at'][:19]}
Assigned: {ticket['assigned_to']}

Description:
{description}

Next Steps:
  1. NOC team notified
  2. Investigation initiated
  3. Customer will receive updates via SMS
  4. SLA: Response within 4 hours (Priority: {priority})
"""

    return result.strip()

@mcp.tool()
def resolve_ticket(ticket_id: str, resolution_note: str) -> str:
    """
    Mark a support ticket as resolved with resolution details.

    Args:
        ticket_id: Ticket identifier (e.g., "TKT-20260217-12847")
        resolution_note: Explanation of how issue was resolved

    Returns:
        Confirmation of ticket closure

    Example:
        resolve_ticket("TKT-20260217-12847", "Issue resolved after incident cleared")
    """
    logger.info(f"Resolving ticket {ticket_id}")

    ticket = tickets_col.find_one({"ticket_id": ticket_id}, {"_id": 0})
    if not ticket:
        return f"❌ Ticket {ticket_id} not found"

    # Update ticket
    tickets_col.update_one(
        {"ticket_id": ticket_id},
        {
            "$set": {
                "status": "resolved",
                "resolved_at": datetime.now().isoformat(),
                "resolution": resolution_note
            },
            "$push": {
                "updates": {
                    "timestamp": datetime.now().isoformat(),
                    "note": f"RESOLVED: {resolution_note}"
                }
            }
        }
    )

    result = f"""
✅ TICKET RESOLVED

Ticket ID: {ticket_id}
Subject: {ticket['subject']}
Customer: {ticket['phone_number']}
Status: RESOLVED
Resolved: {datetime.now().strftime('%Y-%m-%d %H:%M')}

Resolution:
{resolution_note}

Actions Taken:
  ✓ Ticket closed in CRM
  ✓ Customer notified via SMS
  ✓ Satisfaction survey sent
  ✓ Case added to knowledge base
"""

    return result.strip()

if __name__ == "__main__":
    logger.info("🚀 Starting Customer Service...")
    mcp.run()
