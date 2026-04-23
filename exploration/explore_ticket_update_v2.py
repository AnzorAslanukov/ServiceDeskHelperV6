"""
Explore Athena API — PUT Ticket Update v2

Follow-up exploration after discovering:
1. entityId is REQUIRED (not ticket ID)
2. PUT goes to base URL /v1/incident/ (not /v1/incident/{id})
3. The queue field is called 'tierQueue' in the API response, NOT 'supportGroup'
4. PUT with 'supportGroup' returned 200 but was silently ignored

This script tests:
1. Using 'tierQueue' instead of 'supportGroup' in the PUT body
2. Verifying the update actually takes effect with a GET after PUT
3. Testing priority update (safe value only — priority 3 or 4)

Test ticket: IR10377668 (entityId: acdc128a-e800-a420-9951-a6d6762c1464)

Usage:
    python exploration/explore_ticket_update_v2.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_TICKET_ID = "IR10377668"
TEST_ENTITY_ID = "acdc128a-e800-a420-9951-a6d6762c1464"

# GUIDs
VALIDATION_IR_GUID = "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"
SERVICE_DESK_IR_GUID = "ec749166-07c5-eba6-35ba-bd32fa8ed7d2"


def save_json(data, filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def get_ticket(headers):
    """GET the ticket to check current state."""
    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        tq = data.get('tierQueue')
        sg = data.get('supportGroup')
        print(f"  Current tierQueue: {tq}")
        print(f"  Current supportGroup: {sg}")
        print(f"  Current priority: {data.get('priority')}")
        return data
    else:
        print(f"  GET failed: {response.status_code} - {response.text[:300]}")
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
        result["put_response_tierQueue"] = put_data.get("tierQueue")
        result["put_response_supportGroup"] = put_data.get("supportGroup")
        result["put_response_priority"] = put_data.get("priority")
        print(f"  PUT response tierQueue: {put_data.get('tierQueue')}")
        print(f"  PUT response supportGroup: {put_data.get('supportGroup')}")
        print(f"  PUT response priority: {put_data.get('priority')}")

        # Verify with GET
        print("  Verifying with GET...")
        get_data = get_ticket(headers)
        if get_data:
            result["get_verify_tierQueue"] = get_data.get("tierQueue")
            result["get_verify_priority"] = get_data.get("priority")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def main():
    print("=" * 60)
    print("  Athena PUT Ticket Update Exploration v2")
    print(f"  Test Ticket: {TEST_TICKET_ID}")
    print(f"  Entity ID: {TEST_ENTITY_ID}")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    print("\n--- Initial State ---")
    get_ticket(headers)

    all_results = []

    # Test 1: PUT with tierQueue {id: GUID}
    r1 = test_put(headers, "tierQueue with GUID", {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "id": VALIDATION_IR_GUID,
        },
    })
    all_results.append(r1)

    # Test 2: PUT with tierQueue {name: "..."}
    r2 = test_put(headers, "tierQueue with name", {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "name": "Service Desk",
        },
    })
    all_results.append(r2)

    # Test 3: PUT with tierQueue {id, name}
    r3 = test_put(headers, "tierQueue with id+name", {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "id": VALIDATION_IR_GUID,
            "name": "Service Desk\\Validation",
        },
    })
    all_results.append(r3)

    # Test 4: PUT with priority change (3 -> 4, safe)
    r4 = test_put(headers, "priority change 3->4", {
        "entityId": TEST_ENTITY_ID,
        "priority": 4,
    })
    all_results.append(r4)

    # Test 5: PUT with priority as string
    r5 = test_put(headers, "priority as string '3'", {
        "entityId": TEST_ENTITY_ID,
        "priority": "3",
    })
    all_results.append(r5)

    # Test 6: PUT with both tierQueue and priority
    r6 = test_put(headers, "tierQueue + priority together", {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "id": VALIDATION_IR_GUID,
        },
        "priority": 4,
    })
    all_results.append(r6)

    # Restore: Set back to Service Desk, priority 3
    print("\n--- Restoring to original state ---")
    test_put(headers, "RESTORE: Service Desk + priority 3", {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "id": SERVICE_DESK_IR_GUID,
        },
        "priority": 3,
    })

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in all_results:
        status = "OK" if r["status_code"] == 200 else "FAIL"
        tq_changed = r.get("get_verify_tierQueue", {})
        pri_changed = r.get("get_verify_priority", "?")
        print(f"  [{status}] {r['label']}")
        print(f"       tierQueue after: {tq_changed}, priority after: {pri_changed}")

    save_json(all_results, "ticket_update_v2_results.json")
    print("\nDone!")


if __name__ == '__main__':
    main()