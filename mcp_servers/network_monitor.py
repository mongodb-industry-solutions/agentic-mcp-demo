# mcp_servers/network_monitor.py

# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz

"""
SERVER: Network Monitoring & Service Assurance

Real-time network infrastructure monitoring for telecommunications.
Tracks base stations, cell towers, subscriber connections, and performance metrics.

Use this service for:
- Network status checks: "check network for customer +49...", "tower status", "cell site health"
- Performance queries: "signal strength", "throughput", "latency", "packet loss"
- Coverage analysis: "which tower serves location X", "find nearest base station"
- Capacity monitoring: "tower load", "congestion status", "bandwidth usage"

Capabilities:
- Query subscriber's current network connection (tower, signal, throughput)
- Get tower/base station status and metrics (load, capacity, health)
- Geolocate subscribers and map to serving cells
- Detect performance degradation and congestion
- Correlate network events with location data

MongoDB Collections:
- base_stations: Tower metadata (location, capacity, technology)
- active_connections: Real-time subscriber-to-tower mappings
- performance_metrics: Time-series data (signal, throughput, latency)

Examples:
- "Check network status for customer +49 176 12345678"
- "Show all towers in Frankfurt"
- "Which cell site serves 50.1109, 8.6821?"
- "Get performance metrics for TOWER-FRA-001"
"""

import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional
from pymongo import MongoClient
from mcp.server.fastmcp import FastMCP

logging.disable(logging.WARNING)
mcp = FastMCP("network_monitor")
logger = logging.getLogger("network_monitor")

MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI environment variable required")

client = MongoClient(MONGODB_URI)
db = client["telco_digital_twin"]
towers_col = db["base_stations"]
connections_col = db["active_connections"]
metrics_col = db["performance_metrics"]

def _ensure_demo_data():
    """Initialize demo network topology if empty"""
    if towers_col.count_documents({}) > 0:
        return

    logger.info("Bootstrapping demo network data...")

    # Frankfurt base stations
    towers = [
        {
            "tower_id": "TOWER-FRA-001",
            "name": "Frankfurt City Center",
            "location": {"type": "Point", "coordinates": [8.6821, 50.1109]},
            "technology": "5G NR",
            "frequency_band": "3.5 GHz (n78)",
            "max_capacity_mbps": 10000,
            "coverage_radius_km": 2.5,
            "status": "operational",
            "last_maintenance": (datetime.now() - timedelta(days=15)).isoformat()
        },
        {
            "tower_id": "TOWER-FRA-002",
            "name": "Frankfurt Exhibition Center",
            "location": {"type": "Point", "coordinates": [8.6472, 50.1122]},
            "technology": "5G NR",
            "frequency_band": "3.5 GHz (n78)",
            "max_capacity_mbps": 10000,
            "coverage_radius_km": 3.0,
            "status": "operational",
            "last_maintenance": (datetime.now() - timedelta(days=8)).isoformat()
        },
        {
            "tower_id": "TOWER-FRA-003",
            "name": "Frankfurt Airport",
            "location": {"type": "Point", "coordinates": [8.5622, 50.0379]},
            "technology": "5G NR + LTE",
            "frequency_band": "3.5 GHz (n78), 1800 MHz",
            "max_capacity_mbps": 15000,
            "coverage_radius_km": 4.0,
            "status": "operational",
            "last_maintenance": (datetime.now() - timedelta(days=3)).isoformat()
        }
    ]
    towers_col.insert_many(towers)

    # Create geospatial index for location queries
    towers_col.create_index([("location", "2dsphere")])

    logger.info(f"✓ Created {len(towers)} base stations")

_ensure_demo_data()


@mcp.tool()
def get_subscriber_network_status(phone_number: str) -> str:
    """
    Get current network connection status (Signal, Throughput, Latency).

    CRITICAL WORKFLOW:
    1. FIRST call read_resource(uri='customer://profile/...') to get the SLA tier.
    2. THEN YOU MUST CALL THIS TOOL (get_subscriber_network_status) to get the actual live data.
    Getting the profile alone is NOT enough. You need both.

    Args:
        phone_number: Subscriber phone number (e.g., "+49 176 12345678")

    Returns:
        Detailed network status including tower, signal strength, throughput

    Example:
        The agent should first call: read_resource(uri="customer://profile/+4917612345678")
        Then call: get_subscriber_network_status("+4917612345678")
    """
    logger.info(f"Querying network status for {phone_number}")

    # Simulate subscriber location (in production: from HLR/HSS)
    # Frankfurt coordinates with slight randomization
    subscriber_data = {
        "phone_number": phone_number,
        "location": {
            "latitude": 50.1109 + random.uniform(-0.02, 0.02),
            "longitude": 8.6821 + random.uniform(-0.02, 0.02),
            "accuracy_meters": random.randint(10, 50)
        },
        "device": "iPhone 15 Pro",
        "subscription_tier": "Premium 5G Unlimited"
    }

    # Find nearest tower (geospatial query)
    nearest_tower = towers_col.find_one({
        "location": {
            "$near": {
                "$geometry": {
                    "type": "Point",
                    "coordinates": [
                        subscriber_data["location"]["longitude"],
                        subscriber_data["location"]["latitude"]
                    ]
                },
                "$maxDistance": 5000  # 5km radius
            }
        }
    })

    if not nearest_tower:
        return f"❌ No coverage found for {phone_number} at current location"

    # Simulate performance metrics (in production: from PM counters)
    # Check if tower is congested (for demo story)
    is_congested = nearest_tower["tower_id"] == "TOWER-FRA-001"

    metrics = {
        "signal_dbm": random.randint(-85, -65) if not is_congested else -72,
        "sinr_db": random.randint(10, 25) if not is_congested else 8,
        "download_mbps": random.uniform(80, 150) if not is_congested else 12.5,
        "upload_mbps": random.uniform(30, 60) if not is_congested else 4.2,
        "latency_ms": random.randint(10, 20) if not is_congested else 85,
        "packet_loss_pct": random.uniform(0, 0.5) if not is_congested else 2.1
    }

    # Store connection in MongoDB
    connection_doc = {
        "phone_number": phone_number,
        "tower_id": nearest_tower["tower_id"],
        "timestamp": datetime.now().isoformat(),
        "location": subscriber_data["location"],
        "metrics": metrics
    }
    connections_col.insert_one(connection_doc)

    # Format output
    signal_quality = "Excellent" if metrics["signal_dbm"] > -75 else "Good" if metrics["signal_dbm"] > -85 else "Fair"

    result = f"""
📱 Subscriber: {phone_number}
📋 Subscription: {subscriber_data['subscription_tier']}
📍 Location: {subscriber_data['location']['latitude']:.4f}, {subscriber_data['location']['longitude']:.4f}

🗼 Connected Tower: {nearest_tower['tower_id']} ({nearest_tower['name']})
📶 Technology: {nearest_tower['technology']}
📡 Signal Strength: {metrics['signal_dbm']} dBm ({signal_quality})
🔢 SINR: {metrics['sinr_db']} dB

📊 Current Performance:
  ⬇️  Download: {metrics['download_mbps']:.1f} Mbps
  ⬆️  Upload: {metrics['upload_mbps']:.1f} Mbps
  ⏱️  Latency: {metrics['latency_ms']:.0f} ms
  📉 Packet Loss: {metrics['packet_loss_pct']:.1f}%
"""

    # Add warning if performance is degraded
    if is_congested:
        result += f"""
⚠️  PERFORMANCE ISSUE DETECTED:
  Expected throughput: 100+ Mbps (5G)
  Actual throughput: {metrics['download_mbps']:.1f} Mbps
  Latency above threshold: {metrics['latency_ms']}ms (expected <20ms)

  → Possible Cause: Tower congestion or capacity issue
  → Recommendation: Check tower status and active incidents
"""

    return result.strip()

@mcp.tool()
def get_tower_status(tower_id: str) -> str:
    """
    Get detailed status and metrics for a base station.

    Args:
        tower_id: Tower identifier (e.g., "TOWER-FRA-001")

    Returns:
        Tower health, capacity, load, and connected subscribers

    Example:
        get_tower_status("TOWER-FRA-001")
    """
    logger.info(f"Querying status for {tower_id}")

    tower = towers_col.find_one({"tower_id": tower_id}, {"_id": 0})
    if not tower:
        return f"❌ Tower {tower_id} not found in network inventory"

    # Simulate current load (in production: from PM counters)
    is_congested = tower_id == "TOWER-FRA-001"

    current_load_mbps = random.randint(2000, 4000) if not is_congested else 8500
    load_percentage = int((current_load_mbps / tower["max_capacity_mbps"]) * 100)

    # Count active subscribers (from connections collection)
    active_subs = connections_col.count_documents({
        "tower_id": tower_id,
        "timestamp": {"$gte": (datetime.now() - timedelta(minutes=5)).isoformat()}
    })

    # Simulate baseline for comparison
    baseline_load = 3500 if not is_congested else 3500
    load_delta = current_load_mbps - baseline_load
    load_delta_pct = int((load_delta / baseline_load) * 100)

    status_emoji = "✅" if load_percentage < 70 else "⚠️" if load_percentage < 90 else "🔴"

    result = f"""
🗼 Tower: {tower['tower_id']}
📍 Location: {tower['name']}
🌐 Coordinates: {tower['location']['coordinates'][1]:.4f}, {tower['location']['coordinates'][0]:.4f}
📡 Technology: {tower['technology']}
📻 Frequency: {tower['frequency_band']}
🔧 Status: {tower['status'].upper()} {status_emoji}

📊 Capacity & Load:
  Max Capacity: {tower['max_capacity_mbps']:,} Mbps
  Current Load: {current_load_mbps:,} Mbps ({load_percentage}%)
  Baseline Load: {baseline_load:,} Mbps
  Load Delta: {'+' if load_delta > 0 else ''}{load_delta:,} Mbps ({'+' if load_delta_pct > 0 else ''}{load_delta_pct}%)

👥 Active Subscribers: {active_subs + random.randint(800, 900) if is_congested else random.randint(150, 300)}
📅 Last Maintenance: {tower['last_maintenance'][:10]}
"""

    if is_congested:
        result += f"""
⚠️  TOWER STATUS: CONGESTED
  Load: {load_percentage}% (threshold: 80%)
  Subscribers experiencing:
    - Reduced throughput (avg -87%)
    - Increased latency (avg +325%)
    - Potential call drops

  → Check for correlated incidents
  → Consider traffic offloading to neighboring cells
"""

    return result.strip()

@mcp.tool()
def find_towers_near_location(latitude: float, longitude: float, radius_km: float = 5.0) -> str:
    """
    Find all base stations within a radius of a geographic location.

    Args:
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        radius_km: Search radius in kilometers (default: 5.0)

    Returns:
        List of nearby towers with distances

    Example:
        find_towers_near_location(50.1109, 8.6821, 3.0)
    """
    logger.info(f"Searching towers near ({latitude}, {longitude}) within {radius_km}km")

    towers = list(towers_col.find({
        "location": {
            "$near": {
                "$geometry": {
                    "type": "Point",
                    "coordinates": [longitude, latitude]
                },
                "$maxDistance": radius_km * 1000  # Convert km to meters
            }
        }
    }, {"_id": 0}).limit(10))

    if not towers:
        return f"❌ No towers found within {radius_km}km of ({latitude}, {longitude})"

    result = f"🗼 Found {len(towers)} tower(s) within {radius_km}km:\n\n"

    for i, tower in enumerate(towers, 1):
        # Calculate distance (simplified)
        tower_lat = tower["location"]["coordinates"][1]
        tower_lon = tower["location"]["coordinates"][0]
        dist_km = ((tower_lat - latitude)**2 + (tower_lon - longitude)**2)**0.5 * 111  # Rough approximation

        result += f"{i}. {tower['tower_id']} - {tower['name']}\n"
        result += f"   Distance: ~{dist_km:.1f} km\n"
        result += f"   Technology: {tower['technology']}\n"
        result += f"   Status: {tower['status']}\n\n"

    return result.strip()

if __name__ == "__main__":
    logger.info("🚀 Starting Network Monitor Service...")
    mcp.run()
