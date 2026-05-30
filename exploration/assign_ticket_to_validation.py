"""
Explore Athena API — Assign ticket IR10410437 to Service Desk\Validation queue.

Steps:
1. GET the ticket to retrieve its entityId
2. PUT to /v1/incident/ with entityId + tierQueue GUID to assign to Validation

Usage:
    python exploration/assign_ticket_to_validation.py
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

# Target ticket
TICKET_ID = "IR10410437"

# Service Desk\Validation IR GUID
VALIDATION_IR_GUID = "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"
VALIDATION_NAME = "Service Desk\\Validation"


def main():
    print("=" * 60)
    print(f"ASSIGN TICKET {TICKET_ID} TO {VALIDATION_NAME}")
    print("=" * 60)

    # Authenticate
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Failed to authenticate.")
        return

    # Step 1: GET the ticket to retrieve entityId
    print(f"\n--- Step 1: GET ticket {TICKET_ID} ---")
    get_url = f"{INCIDENT_URL}{TICKET_ID}"
    print(f"  GET {get_url}")

    response = requests.get(get_url, headers=headers)
    print(f"  Status: {response.status_code}")

    if response.status_code != 200:
        print(f"  ERROR: Could not fetch ticket. Response: {response.text[:500]}")
        return

    ticket_data = response.json()
    entity_id = ticket_data.get('entityId')
    current_tier_queue = ticket_data.get('tierQueue')
    current_status = ticket_data.get('status')
    title = ticket_data.get('title')

    print(f"  Ticket ID: {ticket_data.get('id')}")
    print(f"  Entity ID: {entity_id}")
    print(f"  Title: {title}")
    print(f"  Status: {current_status}")
    print(f"  Current Tier Queue: {current_tier_queue}")

    if not entity_id:
        print("  ERROR: No entityId found on ticket. Cannot proceed with PUT.")
        return

    # Step 2: PUT to assign to Validation queue
    print(f"\n--- Step 2: PUT to assign to {VALIDATION_NAME} ---")
    payload = {
        "entityId": entity_id,
        "tierQueue": {"id": VALIDATION_IR_GUID}
    }
    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    put_response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {put_response.status_code}")

    if put_response.status_code == 200:
        result = put_response.json()
        updated_tier_queue = result.get('tierQueue')
        print(f"\n  SUCCESS -- Ticket assigned to Validation queue!")
        print(f"  Updated Tier Queue: {updated_tier_queue}")
        print(f"  Ticket ID: {result.get('id')}")

        # Step 3: Verify by re-fetching the ticket
        print(f"\n--- Step 3: Verify assignment ---")
        verify_response = requests.get(get_url, headers=headers)
        if verify_response.status_code == 200:
            verify_data = verify_response.json()
            print(f"  Current Tier Queue: {verify_data.get('tierQueue')}")
        else:
            print(f"  Could not verify: {verify_response.status_code}")
    else:
        print(f"  FAILED -- Response: {put_response.text[:500]}")


if __name__ == '__main__':
    main()