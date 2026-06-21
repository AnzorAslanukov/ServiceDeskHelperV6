"""
Explore Athena API — Resolve a Ticket

Discovers the exact JSON payload format for resolving an incident ticket
(changing status from Active/Pending to Resolved).

Test ticket: IR10522528

Key references from skill.md:
- Resolved Status GUID (IR): 2b8830b6-59f0-f574-9c2a-f4b4682f1681
- PUT goes to base URL /v1/incident/ (no ticket ID in path)
- entityId is REQUIRED in the PUT body
- IncidentResolutionCategoryEnum: 72674491-02cb-1d90-a48f-1b269eb83602

This script tests:
1. GET the ticket to see current state and available fields
2. PUT with status {id: RESOLVED_GUID} to resolve it
3. Test with/without resolutionDescription and resolutionCategory
4. Verify with GET after each attempt

Usage:
    python exploration/explore_resolve_ticket.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')  # https://.../athenaapi/v1/incident/
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_TICKET_ID = "IR10522528"

# Incident Status GUIDs
RESOLVED_STATUS_GUID = "2b8830b6-59f0-f574-9c2a-f4b4682f1681"
ACTIVE_STATUS_GUID = "5e2d3932-ca6d-1515-7310-6f58584df73e"
CLOSED_STATUS_GUID = "bd0ae7c4-3315-2eb3-7933-82dfc482dbaf"


def save_json(data, filename):
    """Save JSON data to the output directory."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def get_ticket(headers, ticket_id=TEST_TICKET_ID):
    """GET the ticket to check current state."""
    url = f"{INCIDENT_URL}{ticket_id}"
    print(f"  GET {url}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        print(f"  Ticket ID: {data.get('id')}")
        print(f"  Entity ID: {data.get('entityId')}")
        print(f"  Title: {data.get('title')}")
        print(f"  Status: {data.get('status')}")
        print(f"  Priority: {data.get('priority')}")
        print(f"  Tier Queue: {data.get('tierQueue')}")
        print(f"  Resolution Description: {data.get('resolutionDescription')}")
        print(f"  Resolution Category: {data.get('resolutionCategory')}")
        print(f"  Resolved Date: {data.get('resolvedDate')}")
        print(f"  Closed Date: {data.get('closedDate')}")
        return data
    else:
        print(f"  GET failed: {response.status_code} - {response.text[:500]}")
        return None


def test_put(headers, label, payload):
    """Run a PUT test and verify with GET."""
    print(f"\n{'=' * 60}")
    print(f"TEST: {label}")
    print(f"{'=' * 60}")

    url = f"{INCIDENT_URL}"
    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  PUT Status: {response.status_code}")

    result = {
        "label": label,
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        put_data = response.json()
        result["put_response_status"] = put_data.get("status")
        result["put_response_resolutionDescription"] = put_data.get("resolutionDescription")
        result["put_response_resolutionCategory"] = put_data.get("resolutionCategory")
        result["put_response_resolvedDate"] = put_data.get("resolvedDate")
        print(f"  PUT response status: {put_data.get('status')}")
        print(f"  PUT response resolutionDescription: {put_data.get('resolutionDescription')}")
        print(f"  PUT response resolutionCategory: {put_data.get('resolutionCategory')}")
        print(f"  PUT response resolvedDate: {put_data.get('resolvedDate')}")

        # Verify with GET
        print("\n  Verifying with GET...")
        get_data = get_ticket(headers)
        if get_data:
            result["get_verify_status"] = get_data.get("status")
            result["get_verify_resolutionDescription"] = get_data.get("resolutionDescription")
            result["get_verify_resolvedDate"] = get_data.get("resolvedDate")
    else:
        result["error"] = response.text[:2000]
        print(f"  Error: {response.text[:500]}")

    return result


def main():
    print("=" * 60)
    print("  Athena Resolve Ticket Exploration")
    print(f"  Test Ticket: {TEST_TICKET_ID}")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    # Step 1: GET current state
    print("\n--- Step 1: GET current state ---")
    ticket_data = get_ticket(headers)
    if not ticket_data:
        print("Failed to fetch test ticket. Exiting.")
        sys.exit(1)

    entity_id = ticket_data.get('entityId')
    current_status = ticket_data.get('status')

    if not entity_id:
        print("No entityId found in ticket data. Exiting.")
        sys.exit(1)

    print(f"\n  Entity ID: {entity_id}")
    print(f"  Current Status: {current_status}")

    # Save initial state
    save_json(ticket_data, "resolve_ticket_initial_state.json")

    all_results = []

    # Test 1: PUT with status {id: RESOLVED_GUID} only
    r1 = test_put(headers, "status {id: RESOLVED_GUID} only", {
        "entityId": entity_id,
        "status": {
            "id": RESOLVED_STATUS_GUID,
        },
    })
    all_results.append(r1)

    # If Test 1 failed, try with resolution description
    if r1["status_code"] != 200 or (r1.get("get_verify_status") and 
        isinstance(r1["get_verify_status"], dict) and 
        r1["get_verify_status"].get("id") != RESOLVED_STATUS_GUID):
        
        # Test 2: PUT with status + resolutionDescription
        r2 = test_put(headers, "status + resolutionDescription", {
            "entityId": entity_id,
            "status": {
                "id": RESOLVED_STATUS_GUID,
            },
            "resolutionDescription": "Resolved via Service Desk Helper automation testing.",
        })
        all_results.append(r2)
    else:
        print("\n  Test 1 succeeded — skipping additional tests.")

    # Test 3: Try with status {name: "Resolved"} format
    if all(r["status_code"] != 200 for r in all_results):
        r3 = test_put(headers, "status {name: 'Resolved'}", {
            "entityId": entity_id,
            "status": {
                "name": "Resolved",
            },
        })
        all_results.append(r3)

    # Test 4: Try with resolutionCategory as well
    if all(r.get("get_verify_status", {}).get("id") != RESOLVED_STATUS_GUID 
           if isinstance(r.get("get_verify_status"), dict) else True 
           for r in all_results):
        r4 = test_put(headers, "status + resolutionDescription + resolutionCategory", {
            "entityId": entity_id,
            "status": {
                "id": RESOLVED_STATUS_GUID,
            },
            "resolutionDescription": "Resolved via Service Desk Helper automation testing.",
            "resolutionCategory": {
                "name": "Resolved by Tier 1",
            },
        })
        all_results.append(r4)

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in all_results:
        status_code = r["status_code"]
        http_status = "HTTP 200" if status_code == 200 else f"HTTP {status_code}"
        verify_status = r.get("get_verify_status", "N/A")
        print(f"  [{http_status}] {r['label']}")
        print(f"       Verified status after: {verify_status}")
        if r.get("error"):
            print(f"       Error: {r['error'][:200]}")
        print()

    save_json(all_results, "resolve_ticket_results.json")
    print("\nDone! Check exploration/output/ for detailed results.")


if __name__ == '__main__':
    main()