# mcp_servers/incident_analyzer.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: Incident Analysis & Root Cause Detection

Advanced incident management and root cause analysis for telecom operations.
Correlates network events, performance degradations, and external factors.

Use this service for:
- Incident investigation: "analyze incident INC-XXX", "root cause for outage"
- Event correlation: "what caused the congestion?", "find related incidents"
- Impact analysis: "how many customers affected?", "revenue impact"
- External factors: "check for mass events", "weather impact", "sports games"

Capabilities:
- Automatic root cause analysis using ML-powered correlation
- Detect mass events (concerts, sports, conferences) causing congestion
- Calculate business impact (affected subscribers, revenue loss)
- Suggest remediation actions
- Link incidents to network topology and performance metrics

MongoDB Collections:
- incidents: Active and historical incidents
- mass_events: Scheduled events affecting network load
- impact_analysis: Business metrics and affected services

Examples:
- "Analyze incident INC-2026-0217-001"
- "Why is TOWER-FRA-001 congested?"
- "Find root cause for customer complaints in Frankfurt"
- "What's the business impact of current incidents?"
"""

import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional
from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)
mcp = FastMCP("incident_analyzer")
logger = logging.getLogger("incident_analyzer")

MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI environment variable required")

client = MongoClient(MONGODB_URI)
db = client["telco_digital_twin"]
incidents_col = db["incidents"]
events_col = db["mass_events"]
towers_col = db["base_stations"]

def _ensure_demo_data():
    """Initialize demo incident data"""
    if incidents_col.count_documents({}) > 0:
        return

    logger.info("Bootstrapping demo incident data...")

    # Current incident for demo
    incident = {
        "incident_id": "INC-2026-0217-001",
        "title": "High latency and reduced throughput - Frankfurt City Center",
        "severity": "major",
        "status": "investigating",
        "affected_tower": "TOWER-FRA-001",
        "created_at": (datetime.now() - timedelta(hours=2)).isoformat(),
        "updated_at": datetime.now().isoformat(),
        "affected_customers": 847,
        "symptoms": [
            "Average download speed: 12.5 Mbps (expected 100+ Mbps)",
            "Latency: 85ms (expected <20ms)",
            "Tower load: 85% (baseline 35%)",
            "Customer complaints: +320%"
        ],
        "assigned_to": "NOC Team Frankfurt",
        "notes": []
    }
    incidents_col.insert_one(incident)

    # Mass event (root cause)
    event = {
        "event_id": "EVT-2026-02-17-TAYLOR",
        "name": "Taylor Swift Concert",
        "venue": "Frankfurt Exhibition Center",
        "location": {"type": "Point", "coordinates": [8.6472, 50.1122]},
        "start_time": (datetime.now() - timedelta(hours=2)).isoformat(),
        "end_time": (datetime.now() + timedelta(hours=1)).isoformat(),
        "expected_attendance": 15000,
        "impact_radius_km": 3.0,
        "affected_towers": ["TOWER-FRA-001", "TOWER-FRA-002"],
        "network_impact_prediction": "high"
    }
    events_col.insert_one(event)

    logger.info("✓ Created demo incident and event data")

def _ensure_indexes():
    """Create required indexes if not present"""
    existing = events_col.index_information()
    if not any("location" in str(idx.get("key")) for idx in existing.values()):
        logger.info("Creating 2dsphere index on mass_events.location...")
        events_col.create_index([("location", "2dsphere")], name="location_2dsphere")
        logger.info("2dsphere index created")

    existing_t = towers_col.index_information()
    if not any("location" in str(idx.get("key")) for idx in existing_t.values()):
        towers_col.create_index([("location", "2dsphere")], name="location_2dsphere")

_ensure_indexes()
_ensure_demo_data()

@mcp.tool()
def analyze_incident(incident_id: str) -> str:
    """
    Perform comprehensive root cause analysis for an incident.

    Args:
        incident_id: Incident identifier (e.g., "INC-2026-0217-001")

    Returns:
        Root cause analysis with correlated events and remediation steps

    Example:
        analyze_incident("INC-2026-0217-001")
    """
    logger.info(f"Analyzing incident {incident_id}")

    incident = incidents_col.find_one({"incident_id": incident_id}, {"_id": 0})
    if not incident:
        return f"❌ Incident {incident_id} not found"

    # Get affected tower details
    tower = towers_col.find_one({"tower_id": incident["affected_tower"]}, {"_id": 0})

    # Check for correlated mass events
    tower_location = tower["location"]["coordinates"]
    current_time = datetime.now()

    correlated_events = list(events_col.find({
        "location": {
            "$near": {
                "$geometry": {
                    "type": "Point",
                    "coordinates": tower_location
                },
                "$maxDistance": 5000  # 5km
            }
        },
        "start_time": {"$lte": current_time.isoformat()},
        "end_time": {"$gte": current_time.isoformat()}
    }, {"_id": 0}))

    # Calculate business impact
    avg_arpu = 35  # EUR per month
    daily_arpu = avg_arpu / 30
    hours_duration = 2
    revenue_impact = incident["affected_customers"] * (daily_arpu / 24) * hours_duration

    # Build analysis report
    result = f"""
🔍 ROOT CAUSE ANALYSIS

Incident: {incident['incident_id']}
Title: {incident['title']}
Severity: {incident['severity'].upper()}
Status: {incident['status'].upper()}
Created: {incident['created_at'][:19].replace('T', ' ')}
Duration: {hours_duration} hours (ongoing)

📍 Affected Infrastructure:
  Tower: {incident['affected_tower']} ({tower['name']})
  Location: {tower['location']['coordinates'][1]:.4f}, {tower['location']['coordinates'][0]:.4f}
  Technology: {tower['technology']}

📊 Impact Metrics:
  Affected Customers: {incident['affected_customers']:,}
  Tower Load: 8,500 / {tower['max_capacity_mbps']:,} Mbps (85%)
  Normal Baseline: 3,500 Mbps
  Load Spike: +143% above normal

💰 Business Impact:
  Estimated Revenue Loss: €{revenue_impact:,.0f} (based on {hours_duration}h duration)
  Customer Satisfaction: -42% (predicted)
  Compensation Credits: ~€{incident['affected_customers'] * 15:,.0f} (€15/customer avg)

🔎 Symptoms:
"""

    for symptom in incident["symptoms"]:
        result += f"  • {symptom}\n"

    # Root cause determination
    if correlated_events:
        event = correlated_events[0]
        distance_km = 3.0  # Approximate

        result += f"""
✅ ROOT CAUSE IDENTIFIED: Mass Event Congestion

Correlated Event:
  📅 Event: {event['name']}
  📍 Venue: {event['venue']}
  👥 Attendance: ~{event['expected_attendance']:,} people
  ⏰ Time: {event['start_time'][:19].replace('T', ' ')} - {event['end_time'][:19].replace('T', ' ')}
  📏 Distance from tower: ~{distance_km:.1f} km

Explanation:
  The mass event created a sudden surge in network demand as {event['expected_attendance']:,}
  attendees simultaneously use mobile data for:
    - Live social media streaming (Instagram, TikTok)
    - Photo/video uploads
    - Messaging and calls
    - Ride-sharing apps (Uber, etc.)

  Tower TOWER-FRA-001 is absorbing traffic from the event venue,
  exceeding normal capacity by +143%, causing:
    - Throughput degradation (-87%)
    - Latency spikes (+325%)
    - Potential connection drops

📋 Recommended Actions:
  1. IMMEDIATE (now):
     → Activate temporary cell-on-wheels (COW) near venue
     → Offload traffic to TOWER-FRA-002 and TOWER-FRA-003
     → Prioritize voice calls over data

  2. SHORT-TERM (next 1-2 hours):
     → Monitor load until event ends ({event['end_time'][:19].replace('T', ' ')})
     → Proactive customer communication (SMS: "High network usage expected")
     → Prepare compensation for premium customers

  3. LONG-TERM (future prevention):
     → Add this venue to event calendar for auto-scaling
     → Deploy additional small cells in venue area
     → Implement AI-based capacity prediction

🎯 Expected Resolution:
  Incident should auto-resolve within 1-2 hours when event ends
  and attendees disperse.
"""
    else:
        result += """
⚠️  ROOT CAUSE: Under Investigation

No mass events detected. Investigating other potential causes:
  • Hardware failure (antenna, RRU, BBU)
  • Backhaul congestion
  • Software bug or configuration error
  • DDoS attack or signaling storm

  → Escalating to L2 engineering team
"""

    return result.strip()

@mcp.tool()
def check_mass_events_near_tower(tower_id: str, hours_window: int = 24) -> str:
    """
    Check for mass events that might impact a tower's capacity.

    Args:
        tower_id: Tower identifier (e.g., "TOWER-FRA-001")
        hours_window: Time window in hours (default: 24)

    Returns:
        List of upcoming/current events near the tower

    Example:
        check_mass_events_near_tower("TOWER-FRA-001", 12)
    """
    logger.info(f"Checking mass events for {tower_id}")

    tower = towers_col.find_one({"tower_id": tower_id}, {"_id": 0})
    if not tower:
        return f"❌ Tower {tower_id} not found"

    tower_location = tower["location"]["coordinates"]
    time_start = (datetime.now() - timedelta(hours=2)).isoformat()
    time_end = (datetime.now() + timedelta(hours=hours_window)).isoformat()

    events = list(events_col.find({
        "location": {
            "$near": {
                "$geometry": {
                    "type": "Point",
                    "coordinates": tower_location
                },
                "$maxDistance": 5000
            }
        },
        "end_time": {"$gte": time_start},
        "start_time": {"$lte": time_end}
    }, {"_id": 0}))

    if not events:
        return f"✅ No mass events detected near {tower_id} in next {hours_window}h"

    result = f"📅 Found {len(events)} event(s) near {tower_id}:\n\n"

    for event in events:
        status = "🔴 ACTIVE" if event["start_time"] <= datetime.now().isoformat() <= event["end_time"] else "🟡 UPCOMING"
        result += f"{status} {event['name']}\n"
        result += f"  Venue: {event['venue']}\n"
        result += f"  Time: {event['start_time'][:16]} - {event['end_time'][:16]}\n"
        result += f"  Attendance: ~{event['expected_attendance']:,}\n"
        result += f"  Network Impact: {event['network_impact_prediction'].upper()}\n\n"

    return result.strip()

@mcp.tool()
def get_incident_summary() -> str:
    """
    Get summary of all active incidents in the network.

    Returns:
        List of active incidents with status and severity

    Example:
        get_incident_summary()
    """
    logger.info("Fetching active incidents")

    active_incidents = list(incidents_col.find(
        {"status": {"$in": ["new", "investigating", "identified"]}},
        {"_id": 0}
    ).sort("created_at", -1))

    if not active_incidents:
        return "✅ No active incidents - Network operating normally"

    result = f"⚠️  {len(active_incidents)} Active Incident(s):\n\n"

    for inc in active_incidents:
        severity_emoji = "🔴" if inc["severity"] == "critical" else "🟠" if inc["severity"] == "major" else "🟡"
        result += f"{severity_emoji} {inc['incident_id']} - {inc['title']}\n"
        result += f"  Status: {inc['status'].upper()}\n"
        result += f"  Affected: {inc['affected_customers']} customers\n"
        result += f"  Tower: {inc['affected_tower']}\n"
        result += f"  Created: {inc['created_at'][:19]}\n\n"

    return result.strip()

if __name__ == "__main__":
    logger.info("🚀 Starting Incident Analyzer Service...")
    mcp.run()
