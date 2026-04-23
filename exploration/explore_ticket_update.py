"""
Explore Athena API — PUT Ticket Update

Discovers the exact JSON payload format for updating incidents and service requests
via PUT /v1/incident and PUT /v1/servicerequest.

Test ticket: IR10377668 (safe to modify — DO NOT change priority to 1 or 2)

This script tests:
1. GET the ticket first to see its current state
2. PUT with various payload formats to discover what works
3. Specifically testing supportGroup update (the main field for Feature #4)

Usage:
    python exploration/explore_ticket_update.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')  # e.g., https://.../athenaapi/v1/incident/
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_TICKET_ID = "IR10377668"

# Service Desk\Validation IR GUID (the queue we're working with)
VALIDATION_IR_GUID = "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"
VALIDATION_NAME = "Service Desk\\Validation"

# Service Desk IR GUID (to test reassignment)
SERVICE_DESK_IR_GUID = "ec749166-07c5-eba6-35ba-bd32fa8ed7d2"
SERVICE_DESK_NAME = "Service Desk"


def save_json(data, filename):
    """Save JSON data to the output directory."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def step_1_get_ticket(headers):
    """Step 1: GET the test ticket to see its current state."""
    print("\n" + "=" * 60)
    print("STEP 1: GET current state of test ticket")
    print("=" * 60)

    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    print(f"  GET {url}")

    response = requests.get(url, headers=headers)
    print(f"  Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        save_json(data, "ticket_update_step1_get.json")

        # Print key fields
        print(f"\n  Ticket ID: {data.get('id')}")
        print(f"  Entity ID: {data.get('entityId')}")
        print(f"  Title: {data.get('title')}")
        print(f"  Status: {data.get('status')}")
        print(f"  Priority: {data.get('priority')}")
        print(f"  Support Group: {data.get('supportGroup')}")
        print(f"  Tier Queue: {data.get('tierQueue')}")

        return data
    else:
        print(f"  Error: {response.text[:500]}")
        return None


def step_2_put_with_id_and_support_group_guid(headers, entity_id):
    """
    Step 2: Try PUT with entityId + supportGroup as {id: GUID}.
    This is the most common pattern in ITSM APIs.
    """
    print("\n" + "=" * 60)
    print("STEP 2: PUT with entityId + supportGroup as {id: GUID}")
    print("=" * 60)

    url = f"{INCIDENT_URL}"
    payload = {
        "entityId": entity_id,
        "supportGroup": {
            "id": VALIDATION_IR_GUID,
        },
    }

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response"] = data
        print(f"  SUCCESS! Support group after update: {data.get('supportGroup')}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    save_json(result, "ticket_update_step2_put_guid.json")
    return response.status_code == 200


def step_3_put_with_id_and_support_group_name(headers, entity_id):
    """
    Step 3: Try PUT with entityId + supportGroup as {name: "..."}.
    """
    print("\n" + "=" * 60)
    print("STEP 3: PUT with entityId + supportGroup as {name: '...'}")
    print("=" * 60)

    url = f"{INCIDENT_URL}"
    payload = {
        "entityId": entity_id,
        "supportGroup": {
            "name": SERVICE_DESK_NAME,
        },
    }

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response"] = data
        print(f"  SUCCESS! Support group after update: {data.get('supportGroup')}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    save_json(result, "ticket_update_step3_put_name.json")
    return response.status_code == 200


def step_4_put_with_ticket_id_instead_of_entity_id(headers):
    """
    Step 4: Try PUT using the ticket ID (IR...) instead of entityId.
    """
    print("\n" + "=" * 60)
    print("STEP 4: PUT with 'id' (ticket ID) instead of entityId")
    print("=" * 60)

    url = f"{INCIDENT_URL}"
    payload = {
        "id": TEST_TICKET_ID,
        "supportGroup": {
            "id": VALIDATION_IR_GUID,
        },
    }

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response"] = data
        print(f"  SUCCESS! Support group after update: {data.get('supportGroup')}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    save_json(result, "ticket_update_step4_put_ticket_id.json")
    return response.status_code == 200


def step_5_put_with_both_ids(headers, entity_id):
    """
    Step 5: Try PUT with both id and entityId + supportGroup {id, name}.
    """
    print("\n" + "=" * 60)
    print("STEP 5: PUT with both id + entityId + supportGroup {id, name}")
    print("=" * 60)

    url = f"{INCIDENT_URL}"
    payload = {
        "id": TEST_TICKET_ID,
        "entityId": entity_id,
        "supportGroup": {
            "id": VALIDATION_IR_GUID,
            "name": VALIDATION_NAME,
        },
    }

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response"] = data
        print(f"  SUCCESS! Support group after update: {data.get('supportGroup')}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    save_json(result, "ticket_update_step5_put_both_ids.json")
    return response.status_code == 200


def step_6_put_to_url_with_ticket_id(headers, entity_id):
    """
    Step 6: Try PUT to /v1/incident/{ticket_id} (URL path) with body.
    """
    print("\n" + "=" * 60)
    print("STEP 6: PUT to /v1/incident/{ticket_id} with body")
    print("=" * 60)

    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    payload = {
        "entityId": entity_id,
        "supportGroup": {
            "id": SERVICE_DESK_IR_GUID,
        },
    }

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response"] = data
        print(f"  SUCCESS! Support group after update: {data.get('supportGroup')}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    save_json(result, "ticket_update_step6_put_url_path.json")
    return response.status_code == 200


def step_7_restore_original(headers, entity_id, original_support_group):
    """
    Step 7: Restore the ticket to its original support group.
    Uses the first successful method discovered.
    """
    print("\n" + "=" * 60)
    print("STEP 7: Restore ticket to original support group")
    print("=" * 60)

    if not original_support_group:
        print("  No original support group to restore. Skipping.")
        return

    # Try to extract the original support group info
    if isinstance(original_support_group, dict):
        sg_id = original_support_group.get('id')
        sg_name = original_support_group.get('name')
    else:
        sg_id = None
        sg_name = str(original_support_group)

    print(f"  Original support group: {sg_name} (id: {sg_id})")

    # Use entityId + supportGroup {id} format (most likely to work)
    url = f"{INCIDENT_URL}"
    payload = {
        "entityId": entity_id,
        "supportGroup": {},
    }
    if sg_id:
        payload["supportGroup"]["id"] = sg_id
    if sg_name:
        payload["supportGroup"]["name"] = sg_name

    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"  Restored! Support group: {data.get('supportGroup')}")
    else:
        print(f"  Failed to restore: {response.text[:500]}")
        print("  WARNING: Ticket may need manual restoration!")


def main():
    """Run all exploration steps."""
    print("=" * 60)
    print("  Athena PUT Ticket Update Exploration")
    print(f"  Test Ticket: {TEST_TICKET_ID}")
    print("  WARNING: Do NOT change priority to 1 or 2!")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    # Step 1: Get current state
    ticket_data = step_1_get_ticket(headers)
    if not ticket_data:
        print("Failed to fetch test ticket. Exiting.")
        sys.exit(1)

    entity_id = ticket_data.get('entityId')
    original_support_group = ticket_data.get('supportGroup')

    if not entity_id:
        print("No entityId found in ticket data. Exiting.")
        sys.exit(1)

    print(f"\n  Entity ID: {entity_id}")
    print(f"  Original Support Group: {original_support_group}")

    # Track which methods work
    results = {}

    # Step 2: PUT with entityId + supportGroup {id: GUID}
    results['step2_entity_id_sg_guid'] = step_2_put_with_id_and_support_group_guid(headers, entity_id)

    # Step 3: PUT with entityId + supportGroup {name: "..."}
    results['step3_entity_id_sg_name'] = step_3_put_with_id_and_support_group_name(headers, entity_id)

    # Step 4: PUT with ticket ID instead of entityId
    results['step4_ticket_id'] = step_4_put_with_ticket_id_instead_of_entity_id(headers)

    # Step 5: PUT with both IDs + supportGroup {id, name}
    results['step5_both_ids'] = step_5_put_with_both_ids(headers, entity_id)

    # Step 6: PUT to URL path /v1/incident/{id}
    results['step6_url_path'] = step_6_put_to_url_with_ticket_id(headers, entity_id)

    # Step 7: Restore original
    step_7_restore_original(headers, entity_id, original_support_group)

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY OF RESULTS")
    print("=" * 60)
    for step, success in results.items():
        status = "✓ SUCCESS" if success else "✗ FAILED"
        print(f"  {step}: {status}")

    save_json(results, "ticket_update_summary.json")
    print("\nDone! Check exploration/output/ for detailed results.")


if __name__ == '__main__':
    main()