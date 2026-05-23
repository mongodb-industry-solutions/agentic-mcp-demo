#
# Copyright (c) 2026 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

"""
IBN demo seed loader.

Run once to populate MongoDB with the Intent-Based Networking demo fixtures:
    python seed/ibn_seed.py [--reset]

With --reset, all ibn_* collections are dropped before re-seeding.

After running, create the Atlas Search vector index for ibn_knowledge_chunks
in the Atlas UI using the JSON config printed at the end. The MCP services
will not work until that index is built.
"""

import argparse
import datetime
import os
import random
import sys

from pymongo import MongoClient, ASCENDING, GEOSPHERE


MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME   = "agent_registry"

NOW = datetime.datetime(2026, 5, 4, 9, 0, 0)  # demo "today"


def days_ago(n: int) -> datetime.datetime:
    return NOW - datetime.timedelta(days=n)


# ─── Customers ─────────────────────────────────────────────────────────────

CUSTOMERS = [
    {
        "_id":      "cust-alpenmarkt",
        "name":     "Alpenmarkt",
        "industry": "Retail",
        "tier":     "Enterprise",
        "notes":    "German retail chain, ~120 stores nationally. Standard offering: "
                    "POS connectivity with strict guest WiFi segmentation, camera uplink, "
                    "extended-hours availability targets.",
    },
]


# ─── Sites (with geospatial coordinates) ───────────────────────────────────

SITES = [
    {
        "_id": "site-muc-mar",
        "customer_id": "cust-alpenmarkt",
        "name": "Munich Marienplatz",
        "city": "München",
        "address": "Marienplatz 1, 80331 München",
        "location": {"type": "Point", "coordinates": [11.5755, 48.1374]},
        "status": "provisioning",  # the new store being onboarded
        "store_format": "flagship",
    },
    {
        "_id": "site-muc-sch",
        "customer_id": "cust-alpenmarkt",
        "name": "Munich Schwabing",
        "city": "München",
        "address": "Leopoldstraße 82, 80802 München",
        "location": {"type": "Point", "coordinates": [11.5807, 48.1657]},
        "status": "active",
        "store_format": "standard",
    },
    {
        "_id": "site-ham-alt",
        "customer_id": "cust-alpenmarkt",
        "name": "Hamburg Altona",
        "city": "Hamburg",
        "address": "Ottenser Hauptstraße 14, 22765 Hamburg",
        "location": {"type": "Point", "coordinates": [9.9356, 53.5500]},
        "status": "active",
        "store_format": "large",
    },
    {
        "_id": "site-ber-mit",
        "customer_id": "cust-alpenmarkt",
        "name": "Berlin Mitte",
        "city": "Berlin",
        "address": "Friedrichstraße 76, 10117 Berlin",
        "location": {"type": "Point", "coordinates": [13.4050, 52.5200]},
        "status": "active",
        "store_format": "standard",
        "kiosk_count": 5,
    },
    {
        "_id": "site-stu-koe",
        "customer_id": "cust-alpenmarkt",
        "name": "Stuttgart Königstraße",
        "city": "Stuttgart",
        "address": "Königstraße 1A, 70173 Stuttgart",
        "location": {"type": "Point", "coordinates": [9.1829, 48.7758]},
        "status": "active",
        "store_format": "standard",
    },
]


# ─── Resources (heterogeneous schemas — the document-model selling point) ──

RESOURCES = [
    # Marienplatz — provisioning candidates
    {
        "_id": "AN-MUC-MAR-01",
        "site_id": "site-muc-mar",
        "type": "access_node",
        "vendor": "Cisco",
        "model": "NCS-540-24Z8Q2C",
        "os": "IOS-XR 7.10",
        "capabilities": ["EVPN", "VLAN", "VXLAN", "DSCP-marking", "policer", "shaper"],
        "capacity_gbps": 10,
        "free_gbps": 8.4,
        "state": "available",
        "location": {"type": "Point", "coordinates": [11.5755, 48.1374]},
    },
    {
        "_id": "UP-MUC-MAR-F1",
        "site_id": "site-muc-mar",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 1000,
        "monthly_cost_eur": 480,
        "provider": "DTAG",
        "state": "available",
    },
    {
        "_id": "UP-MUC-MAR-F10",
        "site_id": "site-muc-mar",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 10000,
        "monthly_cost_eur": 1850,
        "provider": "DTAG",
        "state": "available",
    },
    {
        "_id": "CPE-MUC-MAR-CISCO",
        "site_id": "site-muc-mar",
        "type": "cpe",
        "vendor": "Cisco",
        "model": "C8200L-1N-4T",
        "feature_set": ["VLAN", "QoS-8class", "DSCP-marking", "PoE", "DMVPN"],
        "state": "available",
    },
    {
        "_id": "CPE-MUC-MAR-JUNI",
        "site_id": "site-muc-mar",
        "type": "cpe",
        "vendor": "Juniper",
        "model": "SRX320",
        "feature_set": ["VLAN", "QoS-4class", "DSCP-marking", "PoE-passive"],
        "state": "available",
    },
    {
        "_id": "CPE-MUC-MAR-MTK",
        "site_id": "site-muc-mar",
        "type": "cpe",
        "vendor": "Mikrotik",
        "model": "CCR2004-1G-12S+2XS",
        "feature_set": ["VLAN", "QoS-basic", "no-DSCP-marking"],
        "state": "available",
    },
    # Schwabing (existing) — has spare capacity for failover narrative
    {
        "_id": "AN-MUC-SCH-02",
        "site_id": "site-muc-sch",
        "type": "access_node",
        "vendor": "Cisco",
        "model": "NCS-540-24Z8Q2C",
        "os": "IOS-XR 7.10",
        "capabilities": ["EVPN", "VLAN", "VXLAN", "DSCP-marking", "policer", "shaper"],
        "capacity_gbps": 10,
        "free_gbps": 4.0,
        "state": "active",
        "location": {"type": "Point", "coordinates": [11.5807, 48.1657]},
    },
    # Schwabing uplink + CPE
    {
        "_id": "UP-MUC-SCH-F1",
        "site_id": "site-muc-sch",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 1000,
        "monthly_cost_eur": 420,
        "provider": "DTAG",
        "state": "active",
    },
    {
        "_id": "CPE-MUC-SCH-CISCO",
        "site_id": "site-muc-sch",
        "type": "cpe",
        "vendor": "Cisco",
        "model": "C8200L-1N-4T",
        "feature_set": ["VLAN", "QoS-8class", "DSCP-marking", "PoE", "DMVPN"],
        "state": "active",
    },
    # Hamburg Altona
    {
        "_id": "AN-HAM-ALT-01",
        "site_id": "site-ham-alt",
        "type": "access_node",
        "vendor": "Juniper",
        "model": "ACX7100-32C",
        "os": "Junos 23.4",
        "capabilities": ["EVPN", "VLAN", "DSCP-marking", "policer"],
        "capacity_gbps": 10,
        "free_gbps": 2.1,
        "state": "active",
        "location": {"type": "Point", "coordinates": [9.9356, 53.5500]},
    },
    {
        "_id": "UP-HAM-ALT-F1",
        "site_id": "site-ham-alt",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 1000,
        "monthly_cost_eur": 395,
        "provider": "Vodafone Business",
        "state": "active",
    },
    {
        "_id": "CPE-HAM-ALT-JUNI",
        "site_id": "site-ham-alt",
        "type": "cpe",
        "vendor": "Juniper",
        "model": "SRX380",
        "feature_set": ["VLAN", "QoS-8class", "DSCP-marking", "PoE"],
        "state": "active",
    },
    # Berlin Mitte
    {
        "_id": "AN-BER-MIT-01",
        "site_id": "site-ber-mit",
        "type": "access_node",
        "vendor": "Cisco",
        "model": "NCS-540-24Z8Q2C",
        "os": "IOS-XR 7.10",
        "capabilities": ["EVPN", "VLAN", "VXLAN", "DSCP-marking", "policer", "shaper"],
        "capacity_gbps": 10,
        "free_gbps": 1.5,
        "state": "active",
        "location": {"type": "Point", "coordinates": [13.4050, 52.5200]},
    },
    {
        "_id": "UP-BER-MIT-F10",
        "site_id": "site-ber-mit",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 10000,
        "monthly_cost_eur": 1750,
        "provider": "DTAG",
        "state": "active",
    },
    {
        "_id": "CPE-BER-MIT-CISCO",
        "site_id": "site-ber-mit",
        "type": "cpe",
        "vendor": "Cisco",
        "model": "C8300-2N2S-6T",
        "feature_set": ["VLAN", "QoS-8class", "DSCP-marking", "PoE", "DMVPN", "kiosk-uplink"],
        "state": "active",
    },
    # Stuttgart Königstraße
    {
        "_id": "AN-STU-KOE-01",
        "site_id": "site-stu-koe",
        "type": "access_node",
        "vendor": "Cisco",
        "model": "NCS-540-24Z8Q2C",
        "os": "IOS-XR 7.10",
        "capabilities": ["EVPN", "VLAN", "DSCP-marking"],
        "capacity_gbps": 10,
        "free_gbps": 5.7,
        "state": "active",
        "location": {"type": "Point", "coordinates": [9.1829, 48.7758]},
    },
    {
        "_id": "UP-STU-KOE-F1",
        "site_id": "site-stu-koe",
        "type": "uplink",
        "medium": "fiber",
        "capacity_mbps": 1000,
        "monthly_cost_eur": 360,
        "provider": "Vodafone Business",
        "state": "active",
    },
    {
        "_id": "CPE-STU-KOE-CISCO",
        "site_id": "site-stu-koe",
        "type": "cpe",
        "vendor": "Cisco",
        "model": "C8200L-1N-4T",
        "feature_set": ["VLAN", "QoS-8class", "DSCP-marking", "PoE"],
        "state": "active",
    },
]


# ─── Policy snapshots (one per pre-existing active intent) ─────────────────

POLICY_SNAPSHOTS = [
    {
        "_id": "PLAN-IBN-001-SEED",
        "intent_id":    "IBN-001",
        "snapshot_at":  days_ago(142),
        "access_node":  "AN-HAM-ALT-01",
        "access_node_vendor": "Juniper",
        "access_node_model":  "ACX7100-32C",
        "access_node_capabilities": ["EVPN", "VLAN", "DSCP-marking", "policer"],
        "uplink":       "UP-HAM-ALT-F1",
        "uplink_mbps":  1000,
        "uplink_medium": "fiber",
        "uplink_provider": "Vodafone Business",
        "cpe":          "CPE-HAM-ALT-JUNI",
        "cpe_vendor":   "Juniper",
        "cpe_model":    "SRX380",
        "template":     "strict-retail-v3",
        "estimated_mbps": 220,
    },
    {
        "_id": "PLAN-IBN-002-SEED",
        "intent_id":    "IBN-002",
        "snapshot_at":  days_ago(98),
        "access_node":  "AN-BER-MIT-01",
        "access_node_vendor": "Cisco",
        "access_node_model":  "NCS-540-24Z8Q2C",
        "access_node_capabilities": ["EVPN", "VLAN", "VXLAN", "DSCP-marking", "policer", "shaper"],
        "uplink":       "UP-BER-MIT-F10",
        "uplink_mbps":  10000,
        "uplink_medium": "fiber",
        "uplink_provider": "DTAG",
        "cpe":          "CPE-BER-MIT-CISCO",
        "cpe_vendor":   "Cisco",
        "cpe_model":    "C8300-2N2S-6T",
        "template":     "strict-retail-v3",
        "estimated_mbps": 680,
    },
    {
        "_id": "PLAN-IBN-003-SEED",
        "intent_id":    "IBN-003",
        "snapshot_at":  days_ago(67),
        "access_node":  "AN-STU-KOE-01",
        "access_node_vendor": "Cisco",
        "access_node_model":  "NCS-540-24Z8Q2C",
        "access_node_capabilities": ["EVPN", "VLAN", "DSCP-marking"],
        "uplink":       "UP-STU-KOE-F1",
        "uplink_mbps":  1000,
        "uplink_medium": "fiber",
        "uplink_provider": "Vodafone Business",
        "cpe":          "CPE-STU-KOE-CISCO",
        "cpe_vendor":   "Cisco",
        "cpe_model":    "C8200L-1N-4T",
        "template":     "strict-retail-v3",
        "estimated_mbps": 190,
    },
    {
        "_id": "PLAN-IBN-004-SEED",
        "intent_id":    "IBN-004",
        "snapshot_at":  days_ago(60),
        "access_node":  "AN-MUC-SCH-02",
        "access_node_vendor": "Cisco",
        "access_node_model":  "NCS-540-24Z8Q2C",
        "access_node_capabilities": ["EVPN", "VLAN", "VXLAN", "DSCP-marking", "policer", "shaper"],
        "uplink":       "UP-MUC-SCH-F1",
        "uplink_mbps":  1000,
        "uplink_medium": "fiber",
        "uplink_provider": "DTAG",
        "cpe":          "CPE-MUC-SCH-CISCO",
        "cpe_vendor":   "Cisco",
        "cpe_model":    "C8200L-1N-4T",
        "template":     "strict-retail-v3",
        "estimated_mbps": 210,
    },
]


# ─── Active intents (the existing fleet) ───────────────────────────────────
# Marienplatz intent is created during the demo by the user; not seeded here.

INTENTS = [
    {
        "_id": "IBN-001",
        "customer_id": "cust-alpenmarkt",
        "site_id": "site-ham-alt",
        "raw_text": "New Hamburg Altona store. POS priority, guest WiFi strictly "
                    "separated, camera uplink, online by 14:00, max 45ms POS latency, "
                    "99.95% availability.",
        "parsed": {
            "site_name": "Hamburg Altona",
            "services": ["pos", "guest_wifi", "camera_uplink"],
            "targets": {
                "pos_latency_ms": 45,
                "availability_pct": 99.95,
                "segmentation": "strict",
            },
            "deadline": days_ago(142).replace(hour=14, minute=0, second=0),
        },
        "status": "active",
        "submitted_at": days_ago(142),
        "activated_at": days_ago(142),
        "version": 1,
        "history":     [],
        "template":    "strict-retail-v3",
    },
    {
        "_id": "IBN-002",
        "customer_id": "cust-alpenmarkt",
        "site_id": "site-ber-mit",
        "raw_text": "Berlin Mitte flagship. POS priority, guest WiFi strict, "
                    "5 kiosk uplinks, camera uplink, max 40ms POS latency, "
                    "99.95% availability, extended hours.",
        "parsed": {
            "site_name": "Berlin Mitte",
            "services": ["pos", "guest_wifi", "camera_uplink", "kiosk"],
            "targets": {
                "pos_latency_ms": 40,
                "availability_pct": 99.95,
                "segmentation": "strict",
                "kiosk_count": 5,
            },
            "deadline": days_ago(98).replace(hour=18, minute=0, second=0),
        },
        "status": "active",
        "submitted_at": days_ago(98),
        "activated_at": days_ago(98),
        "version": 1,
        "history":     [],
        "template":    "strict-retail-v3",
    },
    {
        "_id": "IBN-003",
        "customer_id": "cust-alpenmarkt",
        "site_id": "site-stu-koe",
        "raw_text": "Stuttgart Königstraße store. POS priority, guest WiFi separated, "
                    "camera uplink, max 40ms POS latency, 99.9% availability.",
        "parsed": {
            "site_name": "Stuttgart Königstraße",
            "services": ["pos", "guest_wifi", "camera_uplink"],
            "targets": {
                "pos_latency_ms": 40,
                "availability_pct": 99.9,
                "segmentation": "strict",
            },
            "deadline": days_ago(67).replace(hour=18, minute=0, second=0),
        },
        "status": "active",
        "submitted_at": days_ago(67),
        "activated_at": days_ago(67),
        "version": 1,
        "history":     [],
        "template":    "strict-retail-v3",
    },
    {
        "_id": "IBN-004",
        "customer_id": "cust-alpenmarkt",
        "site_id": "site-muc-sch",
        "raw_text": "Munich Schwabing store. POS priority, guest WiFi strictly separated, "
                    "camera uplink, max 40ms POS latency, 99.95% availability.",
        "parsed": {
            "site_name": "Munich Schwabing",
            "services": ["pos", "guest_wifi", "camera_uplink"],
            "targets": {
                "pos_latency_ms": 40,
                "availability_pct": 99.95,
                "segmentation": "strict",
            },
            "deadline": days_ago(60).replace(hour=18, minute=0, second=0),
        },
        "status": "active",
        "submitted_at": days_ago(60),
        "activated_at": days_ago(60),
        "version": 2,
        "history": [
            {
                "ts":     days_ago(21),
                "event":  "runbook_applied",
                "runbook_id": "RB-007",
                "note":   "POS latency violation 47ms, runbook RB-007 (POS→EF queue, "
                          "POS class PIR +30%) applied. Latency restored to 28ms.",
            },
        ],
        "template":    "strict-retail-v3",
    },
]


# ─── Knowledge base (vector-indexed) ───────────────────────────────────────
# These are the fixtures the WOW query searches across. The two POS-segmentation
# incidents (Schwabing + Altona) share a fingerprint; the runbook RB-007 fixes
# them; the strict-retail-v3 policy is the latent root cause.

KNOWLEDGE_CHUNKS = [
    # ── Incidents (8) ──────────────────────────────────────────────────────
    {
        "_id": "INC-2026-04-13-MUC-SCH",
        "kind": "incident",
        "title": "Munich Schwabing POS latency spike during morning rush",
        "site_id": "site-muc-sch",
        "site_name": "Munich Schwabing",
        "customer": "Alpenmarkt",
        "ts":  days_ago(21),
        "lng": 11.5807, "lat": 48.1657,
        "runbook_id": "RB-007",
        "fingerprint": {
            "trigger": "morning_rush",
            "segmentation": "strict",
            "link_util_pct": 24,
            "latency_ms_observed": 51,
            "latency_ms_threshold": 40,
        },
        "text": (
            "Munich Schwabing retail branch reported elevated POS terminal latency reaching "
            "51 milliseconds during the morning customer rush between 09:00 and 09:45 local "
            "time. The site runs a strict guest segmentation policy with isolated VLANs for "
            "guest WiFi, camera uplink, and POS. Uplink utilization remained below 25% "
            "throughout the incident, ruling out bandwidth saturation. CPE diagnostics "
            "showed no faults. Root cause analysis identified that POS payment traffic was "
            "sharing the best-effort queue with bulk camera uploads during the rush window, "
            "leading to head-of-line blocking. Resolution: applied runbook RB-007, which "
            "moved POS traffic to the EF (Expedited Forwarding) queue with DSCP marking 46 "
            "and raised the POS class PIR by 30%. Latency dropped to 28 ms within three "
            "minutes and remained stable for the remainder of the day. Recommendation: "
            "include EF queue mapping in the default segmentation template for retail "
            "intents to prevent recurrence at sister sites."
        ),
    },
    {
        "_id": "INC-2026-02-15-HAM-ALT",
        "kind": "incident",
        "title": "Hamburg Altona POS latency from segmentation template",
        "site_id": "site-ham-alt",
        "site_name": "Hamburg Altona",
        "customer": "Alpenmarkt",
        "ts":  days_ago(78),
        "lng": 9.9356, "lat": 53.5500,
        "runbook_id": "RB-007",
        "fingerprint": {
            "trigger": "morning_rush",
            "segmentation": "strict",
            "link_util_pct": 28,
            "latency_ms_observed": 49,
            "latency_ms_threshold": 45,
        },
        "text": (
            "Hamburg Altona reported POS latency exceeding the 45 ms threshold during peak "
            "morning trading. Symptom fingerprint matches a chain-wide pattern: strict guest "
            "segmentation enabled, link utilization low (28%), morning customer rush, no CPE "
            "or uplink faults detected. Investigation traced the cause to segmentation "
            "template strict-retail-v3, which provisions VLAN isolation correctly but does "
            "not assign POS traffic to the EF queue. Without DSCP-46 marking, payment "
            "transactions share scheduling priority with bulk uploads. Resolution: runbook "
            "RB-007 applied (POS to EF queue, POS class PIR +30%). This is a chain-wide "
            "pattern; remediation has been site-by-site rather than via template update."
        ),
    },
    {
        "_id": "INC-2026-03-22-BER-MIT",
        "kind": "incident",
        "title": "Berlin Mitte fiber cut at street excavation",
        "site_id": "site-ber-mit",
        "site_name": "Berlin Mitte",
        "customer": "Alpenmarkt",
        "ts":  days_ago(43),
        "lng": 13.4050, "lat": 52.5200,
        "runbook_id": "RB-001",
        "text": (
            "Berlin Mitte experienced complete uplink loss at 11:42 due to fiber cut during "
            "street excavation by a third-party utility crew on Friedrichstraße. Failover to "
            "LTE backup activated within 14 seconds. Bandwidth-limited operation for ~6 hours "
            "until splice repair completed. POS continued operating in offline-capture mode. "
            "Root cause: external. Resolution: runbook RB-001 fiber repair workflow, splice "
            "completed 17:30, full service restored. No SLA breach due to backup activation."
        ),
    },
    {
        "_id": "INC-2026-04-02-STU-KOE",
        "kind": "incident",
        "title": "Stuttgart Königstraße BGP flap from operator misconfig",
        "site_id": "site-stu-koe",
        "site_name": "Stuttgart Königstraße",
        "customer": "Alpenmarkt",
        "ts":  days_ago(32),
        "lng": 9.1829, "lat": 48.7758,
        "runbook_id": None,
        "text": (
            "Stuttgart Königstraße experienced BGP session flapping with upstream provider "
            "for 11 minutes. Caused by accidental MED change pushed during a maintenance "
            "window outside change control. Customer impact minimal; POS continued via "
            "warm-cache. Root cause: operator misconfiguration. Resolution: rolled back "
            "MED setting, sessions stable. Process improvement: change-control gate added "
            "to BGP attribute modifications."
        ),
    },
    {
        "_id": "INC-2026-04-25-MUC-SCH",
        "kind": "incident",
        "title": "Munich Schwabing DNS resolver misconfiguration",
        "site_id": "site-muc-sch",
        "site_name": "Munich Schwabing",
        "customer": "Alpenmarkt",
        "ts":  days_ago(9),
        "lng": 11.5807, "lat": 48.1657,
        "runbook_id": "RB-003",
        "text": (
            "Munich Schwabing experienced intermittent DNS resolution failures for ~22 "
            "minutes during a CPE firmware push that reset the local resolver list. "
            "Symptom: scattered application timeouts, no impact on POS (uses static IPs "
            "for payment gateway). Root cause: firmware bundle stripped resolver config. "
            "Resolution: runbook RB-003 applied to restore resolvers and added a config-"
            "preservation check to the firmware push pipeline."
        ),
    },
    {
        "_id": "INC-2026-04-08-HAM-ALT",
        "kind": "incident",
        "title": "Hamburg Altona UPS battery failure",
        "site_id": "site-ham-alt",
        "site_name": "Hamburg Altona",
        "customer": "Alpenmarkt",
        "ts":  days_ago(26),
        "lng": 9.9356, "lat": 53.5500,
        "runbook_id": "RB-009",
        "text": (
            "Hamburg Altona network rack UPS reported battery health below threshold during "
            "monthly self-test; batteries had reached end of design life (4.2 years). No "
            "service interruption. Resolution: runbook RB-009 UPS battery rotation, hot-swap "
            "completed during low-traffic window. Reminder for fleet UPS battery inventory "
            "audit scheduled."
        ),
    },
    {
        "_id": "INC-2026-04-29-BER-MIT",
        "kind": "incident",
        "title": "Berlin Mitte camera uplink down — bad PoE injector",
        "site_id": "site-ber-mit",
        "site_name": "Berlin Mitte",
        "customer": "Alpenmarkt",
        "ts":  days_ago(5),
        "lng": 13.4050, "lat": 52.5200,
        "runbook_id": "RB-005",
        "text": (
            "Berlin Mitte rear-aisle camera lost uplink at 06:12. POS and guest networks "
            "unaffected (separate VLAN segments). Diagnosis identified faulty PoE midspan "
            "injector for the camera segment. Resolution: runbook RB-005 PoE diagnosis. "
            "Replaced injector during morning prep, camera back online at 06:48."
        ),
    },
    {
        "_id": "INC-2026-04-18-MIXED",
        "kind": "incident",
        "title": "Multi-site bandwidth saturation during marketing campaign",
        "site_id": None,
        "site_name": "Multiple",
        "customer": "Alpenmarkt",
        "ts":  days_ago(16),
        "lng": 10.4515, "lat": 51.1657,  # roughly geographic Germany centroid
        "runbook_id": None,
        "text": (
            "Three Alpenmarkt sites (Stuttgart, Berlin, Hamburg) reported guest WiFi "
            "throughput degradation during a national marketing campaign launch that drove "
            "unusual visitor counts. Root cause: actual bandwidth saturation on guest "
            "uplinks (utilization sustained 92-97%). POS and segmentation policies held "
            "throughout — strict isolation prevented impact on payment traffic. Resolution: "
            "temporary uplink burst-allowance increase requested from carrier; permanent "
            "uplink upgrade scheduled for affected sites."
        ),
    },

    # ── Runbooks (5) ───────────────────────────────────────────────────────
    {
        "_id": "RB-007",
        "kind": "runbook",
        "title": "POS to EF queue with PIR uplift (segmentation latency mitigation)",
        "ts":  days_ago(120),
        "lng": None, "lat": None,
        "text": (
            "Runbook RB-007: POS-to-EF queue migration with PIR uplift. "
            "Indication: POS terminal latency exceeds intent SLA threshold during peak "
            "windows, with strict segmentation in place and uplink utilization below 30%. "
            "Pattern indicates queue-scheduling head-of-line blocking rather than bandwidth "
            "saturation. Action: (1) re-mark POS payment traffic with DSCP 46 (EF) at the "
            "CPE; (2) ensure access node QoS policy maps EF to the priority queue; "
            "(3) raise the POS class PIR by 30% to absorb morning rush bursts without "
            "policer drops. Verification: observe POS round-trip latency drop within 3 "
            "minutes; latency should stabilize 25-30 ms typical. Side effects: minor "
            "guest-class throughput reduction during rush windows; acceptable per QoS "
            "policy. Permanent fix: roll EF mapping into the segmentation template so new "
            "intents inherit the configuration."
        ),
        "actions": [
            {"step": 1, "command": "Mark POS DSCP 46 at CPE egress",
             "context": "applies at CPE ingress classifier"},
            {"step": 2, "command": "Confirm access-node QoS maps EF to priority queue",
             "context": "default in NCS-540 IOS-XR template, verify on Juniper ACX"},
            {"step": 3, "command": "Increase POS class PIR by 30%",
             "context": "two-rate three-color policer parameter"},
        ],
    },
    {
        "_id": "RB-001",
        "kind": "runbook",
        "title": "Fiber cut splice repair",
        "ts":  days_ago(180),
        "lng": None, "lat": None,
        "text": (
            "Runbook RB-001: Fiber cut splice repair workflow. Trigger: complete uplink "
            "loss with confirmed external fiber damage (third-party excavation, vehicle "
            "impact, rodent damage). Step 1: confirm LTE failover engaged. Step 2: dispatch "
            "splice crew with site GPS and fiber map. Step 3: reflectometer to locate "
            "break. Step 4: fusion splice. Step 5: OTDR validation. Step 6: cutover from "
            "LTE backup. Typical resolution: 4-6 hours. Variations: (a) underground vault "
            "blocked, plan extended; (b) aerial, weather-dependent."
        ),
        "actions": [],
    },
    {
        "_id": "RB-003",
        "kind": "runbook",
        "title": "DNS resolver recovery after firmware push",
        "ts":  days_ago(150),
        "lng": None, "lat": None,
        "text": (
            "Runbook RB-003: DNS resolver recovery. Trigger: scattered application timeouts "
            "or NXDOMAIN responses post-firmware-push, no impact on services using static "
            "IPs. Step 1: verify resolver config from CPE. Step 2: restore from backup or "
            "push via configuration management. Step 3: flush local cache. Step 4: validate "
            "resolution latency under 50ms to designated resolver. Add config-preservation "
            "checks to the firmware push pipeline to prevent recurrence."
        ),
        "actions": [],
    },
    {
        "_id": "RB-005",
        "kind": "runbook",
        "title": "PoE injector diagnosis and replacement",
        "ts":  days_ago(90),
        "lng": None, "lat": None,
        "text": (
            "Runbook RB-005: PoE injector diagnosis and replacement. Trigger: PoE-powered "
            "device (camera, AP, phone) loses link with no upstream switch fault. Step 1: "
            "verify device LEDs (no power = injector or upstream supply). Step 2: swap "
            "injector with known-good unit. Step 3: if device returns, dispose of failed "
            "injector and update inventory. Common failure mode: capacitor aging in "
            "midspan injectors after ~3 years."
        ),
        "actions": [],
    },
    {
        "_id": "RB-009",
        "kind": "runbook",
        "title": "UPS battery rotation",
        "ts":  days_ago(200),
        "lng": None, "lat": None,
        "text": (
            "Runbook RB-009: UPS battery rotation. Trigger: monthly self-test reports "
            "battery health below threshold, or batteries past design life (typical 4-5 "
            "years for VRLA). Step 1: schedule rotation during low-traffic window. Step 2: "
            "verify generator standby. Step 3: hot-swap battery modules per manufacturer "
            "procedure. Step 4: complete self-test. Inventory: maintain 2 spare battery "
            "modules per regional cluster."
        ),
        "actions": [],
    },

    # ── Policies (4) ───────────────────────────────────────────────────────
    {
        "_id": "POL-strict-retail-v3",
        "kind": "policy",
        "title": "Segmentation template strict-retail-v3",
        "ts":  days_ago(300),
        "lng": None, "lat": None,
        "text": (
            "Segmentation template strict-retail-v3. Defines VLAN isolation profile for "
            "Alpenmarkt-style retail intents. Provides three isolated VLANs: VLAN 10 for "
            "POS payment traffic, VLAN 20 for guest WiFi, VLAN 30 for camera and back-of-"
            "house infrastructure. Inter-VLAN routing disabled; firewall enforces strict "
            "egress policy per VLAN. NOTE: this template provisions VLAN isolation only — "
            "it does NOT assign DSCP markings or queue mappings. QoS configuration must be "
            "applied separately. Known limitation: under morning rush load, POS traffic in "
            "default best-effort queue can experience head-of-line blocking — see runbook "
            "RB-007 for mitigation. Successor template strict-retail-v4 (proposed) folds in "
            "EF queue mapping for POS class."
        ),
    },
    {
        "_id": "POL-qos-retail",
        "kind": "policy",
        "title": "QoS policy retail standard",
        "ts":  days_ago(400),
        "lng": None, "lat": None,
        "text": (
            "QoS policy for retail intents. Eight traffic classes: EF (priority, payment), "
            "AF41 (interactive video), AF31 (signaling), AF21 (transactional), AF11 (bulk), "
            "CS6 (control), CS1 (scavenger), BE (default). Queue allocations: EF 15%, AF41 "
            "20%, AF31 5%, AF21 20%, AF11 15%, CS6 5%, CS1 5%, BE 15%. Policer applied per "
            "class with PIR/CIR per intent commitments. EF class is strict-priority; others "
            "weighted-fair queue."
        ),
    },
    {
        "_id": "POL-availability-9995",
        "kind": "policy",
        "title": "99.95% availability SLA template",
        "ts":  days_ago(500),
        "lng": None, "lat": None,
        "text": (
            "99.95% availability template. Permitted downtime budget: ~21 minutes per "
            "month, ~4.4 hours per year. Implemented via dual-uplink with sub-second BFD "
            "failover, LTE backup for last-mile redundancy, UPS for site-local resilience. "
            "Compliance computed monthly across all customer-impacting outages weighted by "
            "service criticality."
        ),
    },
    {
        "_id": "POL-change-window",
        "kind": "policy",
        "title": "Change-window rules",
        "ts":  days_ago(600),
        "lng": None, "lat": None,
        "text": (
            "Change-window policy. Routine changes: weekday 22:00-04:00 local time. "
            "Emergency changes: any time with on-call lead approval. Risky changes (BGP "
            "attributes, routing policy, MTU, IPv6 transition steps): change advisory "
            "board approval required, documented rollback plan, on-call lead on standby."
        ),
    },

    # ── Vendor cheat-sheets (3) — distractors with vendor-specific noise ────
    {
        "_id": "VEN-cisco-evpn",
        "kind": "vendor_note",
        "vendor": "Cisco",
        "title": "Cisco IOS-XR EVPN configuration snippets",
        "ts":  days_ago(250),
        "lng": None, "lat": None,
        "text": (
            "Cisco IOS-XR EVPN configuration. Configure l2vpn evpn instance, define "
            "EVI per VLAN segment. RT auto: enable for type-2 MAC routes. NVE interface "
            "ties EVPN to VXLAN encapsulation. Verify with show l2vpn evpn evi and "
            "show bgp l2vpn evpn. Common gotcha: control-word negotiation differs between "
            "IOS-XR releases; pin to 7.10+ for stable operation with Juniper peers."
        ),
    },
    {
        "_id": "VEN-juniper-qos",
        "kind": "vendor_note",
        "vendor": "Juniper",
        "title": "Juniper Junos QoS policy basics",
        "ts":  days_ago(220),
        "lng": None, "lat": None,
        "text": (
            "Juniper Junos QoS basics. Define forwarding-classes, scheduler-maps, "
            "schedulers per class. Apply via class-of-service interfaces. EF class: "
            "scheduler with strict-high priority, transmit-rate percent. DSCP rewrite "
            "rules at egress. Verify with show class-of-service interface."
        ),
    },
    {
        "_id": "VEN-mikrotik-vlan",
        "kind": "vendor_note",
        "vendor": "Mikrotik",
        "title": "Mikrotik RouterOS VLAN tagging",
        "ts":  days_ago(180),
        "lng": None, "lat": None,
        "text": (
            "Mikrotik RouterOS VLAN tagging. Use bridge VLAN filtering for hardware "
            "offload on CRS series. Define VLANs under /interface bridge vlan. Tagged "
            "vs untagged ports configured per VLAN. Note: DSCP-marking support varies "
            "by hardware revision; CCR2004 supports basic match/mark, CRS3xx hardware "
            "offload limited."
        ),
    },
]


# ─── Loader ────────────────────────────────────────────────────────────────

IBN_COLLECTIONS = [
    "ibn_customers",
    "ibn_sites",
    "ibn_resources",
    "ibn_intents",
    "ibn_knowledge_chunks",
    "ibn_policy_snapshots",
    "ibn_compliance_events",
]


def reset(db):
    print("⚠️  Resetting IBN collections...")
    for c in IBN_COLLECTIONS:
        db[c].drop()
    if "ibn_telemetry" in db.list_collection_names():
        db["ibn_telemetry"].drop()
    print("    done.")


def ensure_telemetry_timeseries(db):
    """Create the time-series collection for telemetry if not present."""
    if "ibn_telemetry" not in db.list_collection_names():
        print("📈 Creating time-series collection ibn_telemetry...")
        db.create_collection(
            "ibn_telemetry",
            timeseries={
                "timeField":   "ts",
                "metaField":   "meta",
                "granularity": "seconds",
            },
        )
    else:
        print("📈 ibn_telemetry already exists")


def ensure_indexes(db):
    print("🔧 Ensuring indexes...")
    db["ibn_sites"].create_index([("location", GEOSPHERE)], name="site_geo_2dsphere")
    db["ibn_resources"].create_index([("location", GEOSPHERE)], name="resource_geo_2dsphere")
    db["ibn_resources"].create_index([("site_id", ASCENDING)], name="resource_by_site")
    db["ibn_intents"].create_index([("status", ASCENDING)], name="intent_status")
    db["ibn_intents"].create_index([("site_id", ASCENDING)], name="intent_site")
    db["ibn_compliance_events"].create_index([("intent_id", ASCENDING), ("ts", -1)],
                                              name="compliance_by_intent_ts")


def insert_all(db):
    print("📥 Inserting fixtures...")
    db["ibn_customers"].insert_many(CUSTOMERS)
    db["ibn_sites"].insert_many(SITES)
    db["ibn_resources"].insert_many(RESOURCES)
    db["ibn_intents"].insert_many(INTENTS)
    db["ibn_policy_snapshots"].insert_many(POLICY_SNAPSHOTS)
    db["ibn_knowledge_chunks"].insert_many(KNOWLEDGE_CHUNKS)
    print(f"    {len(CUSTOMERS)} customers")
    print(f"    {len(SITES)} sites")
    print(f"    {len(RESOURCES)} resources")
    print(f"    {len(INTENTS)} active intents")
    print(f"    {len(POLICY_SNAPSHOTS)} policy snapshots")
    print(f"    {len(KNOWLEDGE_CHUNKS)} knowledge chunks")


def seed_baseline_telemetry(db):
    """Seed 120s of healthy POS-latency telemetry for every active intent."""
    telemetry = db["ibn_telemetry"]
    now = datetime.datetime.now()
    n = 120
    total = 0
    for intent in INTENTS:
        if intent.get("status") != "active":
            continue
        threshold = intent["parsed"]["targets"].get("pos_latency_ms", 40)
        lo = max(15, threshold - 18)
        hi = max(20, threshold - 8)
        docs = [
            {
                "ts": now - datetime.timedelta(seconds=n - 1 - i),
                "meta": {
                    "intent_id": intent["_id"],
                    "site_id":   intent["site_id"],
                    "metric":    "pos_latency_ms",
                },
                "value": round(random.uniform(lo, hi), 1),
            }
            for i in range(n)
        ]
        telemetry.insert_many(docs)
        total += n
    print(f"📡 Seeded {total} baseline telemetry samples ({n}s × {len(INTENTS)} intents)")


# NOTE: this is the Atlas auto-embed pattern — `type: "text"` with `model`,
# matching the existing `mcp_services.vector_index` style. Atlas embeds the
# `text` field on insert and on query (when you pass `query: <raw text>`
# instead of `queryVector`). DO NOT use `type: "vector"` here — that mode
# requires you to pre-compute and supply the vector yourself, and the
# orchestrator's pattern (and our diagnose tool) passes raw text.
VECTOR_INDEX_CONFIG = {
    "name": "ibn_knowledge_index",
    "type": "vectorSearch",
    "definition": {
        "fields": [
            {
                "type":     "autoEmbed",
                "modality": "text",
                "path":     "text",
                "model":    "voyage-4",
            },
            {"type": "filter", "path": "kind"},
            {"type": "filter", "path": "ts"},
            {"type": "filter", "path": "lng"},
            {"type": "filter", "path": "lat"},
            {"type": "filter", "path": "customer"},
            {"type": "filter", "path": "site_id"},
        ],
    },
}


def create_vector_index(db):
    """Try to create the vector index programmatically. No-op if it exists."""
    from pymongo.operations import SearchIndexModel
    coll = db["ibn_knowledge_chunks"]
    existing = [i for i in coll.list_search_indexes() if i.get("name") == "ibn_knowledge_index"]
    if existing:
        print(f"⚡ Vector index already exists (status={existing[0].get('status')})")
        return
    try:
        coll.create_search_index(SearchIndexModel(
            definition=VECTOR_INDEX_CONFIG["definition"],
            name="ibn_knowledge_index",
            type="vectorSearch",
        ))
        print("⚡ Vector index 'ibn_knowledge_index' submitted to Atlas.")
        print("   It will become queryable in ~30-90 seconds. Check Atlas UI for status.")
    except Exception as e:
        print(f"⚠️  Could not create vector index automatically: {e}")
        print_index_instructions()


def print_index_instructions():
    import json as _json
    print()
    print("━" * 72)
    print("⚡  Atlas Vector Search index — create manually in the Atlas UI")
    print("━" * 72)
    print()
    print("Database:    agent_registry")
    print("Collection:  ibn_knowledge_chunks")
    print("Index name:  ibn_knowledge_index")
    print()
    print("In Atlas → Search → Create Search Index → Atlas Vector Search →")
    print("JSON editor → paste:")
    print()
    print(_json.dumps(VECTOR_INDEX_CONFIG["definition"], indent=2))
    print()
    print("Atlas auto-embed (`type: autoEmbed` + `model: voyage-4`).")
    print("Atlas embeds on insert and at query time, so the diagnose tool can")
    print("pass raw text in `$vectorSearch.query` without computing vectors.")
    print("━" * 72)


def main():
    parser = argparse.ArgumentParser(description="IBN demo seed loader")
    parser.add_argument("--reset", action="store_true", help="drop ibn_* collections first")
    args = parser.parse_args()

    print(f"🍃 Connecting to {DB_NAME}...")
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]

    if args.reset:
        reset(db)

    ensure_telemetry_timeseries(db)
    ensure_indexes(db)
    insert_all(db)
    seed_baseline_telemetry(db)
    create_vector_index(db)

    print("✅ Seed complete.")
    client.close()


if __name__ == "__main__":
    main()
