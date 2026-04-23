"""
Quick exploration: test different filter formats for the Validation queue.
Focus on finding a filter that returns only OPEN Validation tickets.
"""

import asyncio
import os
import json

import httpx
from dotenv import load_dotenv

load_dotenv()

ATHENA_BASE_URL = os.getenv("ATHENA_BASE_URL")
ATHENA_AUTH_URL = os.getenv("ATHENA_AUTH_URL") or os.getenv("ATHENA_TOKEN_URL")
ATHENA_USERNAME = os.getenv("ATHENA_USERNAME")
ATHENA_PASSWORD = os.getenv("ATHENA_PASSWORD")
ATHENA_CLIENT_ID = os.getenv("ATHENA_CLIENT_ID")

IR_VIEW_URL = os.getenv("ATHENA_INCIDENT_VIEW_URL")
SR_VIEW_URL = os.getenv("ATHENA_SERVICEREQUEST_VIEW_URL")

# Validation queue GUIDs
IR_VALIDATION_GUID = "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"
SR_VALIDATION_GUID = "c954d465-65a0-9e43-9b02-b353e87bdb37"

# IR Status GUIDs from skill.md
ACTIVE_GUID = "5e2d3932-ca6d-1515-7310-6f58584df73e"
WIP_GUID = "9accddda-fbf5-10d4-b402-69bdd276a69b"
RESOLVED_GUID = "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
CLOSED_GUID = "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf"


async def get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        ATHENA_AUTH_URL,
        data={
            "grant_type": "password",
            "username": ATHENA_USERNAME,
            "password": ATHENA_PASSWORD,
            "client_id": ATHENA_CLIENT_ID,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def query_view(client, headers, url, filters, label, top=200):
    """Run a view query and report results."""
    separator = "&" if "?" in url else "?"
    full_url = f"{url}{separator}$skip=0&$top={top}"
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    
    try:
        resp = await client.post(full_url, headers=headers, json=filters)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"  ERROR: {e.response.status_code} {e.response.reason_phrase}")
        try:
            print(f"  Body: {e.response.text[:200]}")
        except Exception:
            pass
        return None
    
    data = resp.json()
    
    if isinstance(data, dict):
        results = data.get("result", [])
        total = data.get("resultCount", len(results))
        has_more = data.get("hasMoreResults", False)
        print(f"  resultCount: {total}, returned: {len(results)}, hasMore: {has_more}")
        
        if results:
            # Analyze status distribution
            status_counts = {}
            for r in results:
                s = r.get("status")
                if isinstance(s, dict):
                    s_label = s.get("name", s.get("id", "?"))
                else:
                    s_label = str(s)
                status_counts[s_label] = status_counts.get(s_label, 0) + 1
            print(f"  Status distribution: {status_counts}")
            
            # Show sample
            sample = results[0]
            tq = sample.get("tierQueue")
            if isinstance(tq, dict):
                tq_name = tq.get("name", "?")
            else:
                tq_name = tq
            status = sample.get("status")
            if isinstance(status, dict):
                status_name = status.get("name", status.get("id", "?"))
            else:
                status_name = status
            print(f"  Sample: id={sample.get('id')}, tierQueue={tq_name}, status={status_name}")
        
        return data
    elif isinstance(data, list):
        print(f"  Returned list of {len(data)} items")
        return data
    else:
        print(f"  Unexpected response type: {type(data)}")
        return data


async def main():
    print(f"IR_VIEW_URL: {IR_VIEW_URL}")
    print(f"SR_VIEW_URL: {SR_VIEW_URL}")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        token = await get_token(client)
        headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        }

        # ── Test 1: IR GUID only (current code - no status filter) ──
        await query_view(client, headers, IR_VIEW_URL,
            [{"condition": "and", "filters": [
                {"condition": "and", "property": "TierQueue", "operator": "eq", "value": IR_VALIDATION_GUID},
            ]}],
            "Test 1: IR TierQueue=GUID (no status filter)")

        # ── Test 2: IR GUID + Status eq "Active" (name string) ──
        await query_view(client, headers, IR_VIEW_URL,
            [{"condition": "and", "filters": [
                {"condition": "and", "property": "TierQueue", "operator": "eq", "value": IR_VALIDATION_GUID},
                {"condition": "or", "filters": [
                    {"condition": "and", "property": "Status", "operator": "eq", "value": "Active"},
                    {"condition": "and", "property": "Status", "operator": "eq", "value": "Work in Progress"},
                ]},
            ]}],
            "Test 2: IR TierQueue=GUID + Status in (Active, WIP) by NAME")

        # ── Test 3: IR GUID + Status eq GUID ──
        await query_view(client, headers, IR_VIEW_URL,
            [{"condition": "and", "filters": [
                {"condition": "and", "property": "TierQueue", "operator": "eq", "value": IR_VALIDATION_GUID},
                {"condition": "or", "filters": [
                    {"condition": "and", "property": "Status", "operator": "eq", "value": ACTIVE_GUID},
                    {"condition": "and", "property": "Status", "operator": "eq", "value": WIP_GUID},
                ]},
            ]}],
            "Test 3: IR TierQueue=GUID + Status in (Active, WIP) by GUID")

        # ── Test 4: SR supportGroup=GUID (no status filter) ──
        await query_view(client, headers, SR_VIEW_URL,
            [{"condition": "and", "filters": [
                {"condition": "and", "property": "supportGroup", "operator": "eq", "value": SR_VALIDATION_GUID},
            ]}],
            "Test 4: SR supportGroup=GUID (no status filter)")

        # ── Test 5: SR supportGroup=GUID + Status by name ──
        await query_view(client, headers, SR_VIEW_URL,
            [{"condition": "and", "filters": [
                {"condition": "and", "property": "supportGroup", "operator": "eq", "value": SR_VALIDATION_GUID},
                {"condition": "or", "filters": [
                    {"condition": "and", "property": "Status", "operator": "eq", "value": "Submitted"},
                    {"condition": "and", "property": "Status", "operator": "eq", "value": "In Progress"},
                    {"condition": "and", "property": "Status", "operator": "eq", "value": "New"},
                ]},
            ]}],
            "Test 5: SR supportGroup=GUID + Status in (Submitted, In Progress, New) by NAME")

        print("\n\nDONE.")


asyncio.run(main())