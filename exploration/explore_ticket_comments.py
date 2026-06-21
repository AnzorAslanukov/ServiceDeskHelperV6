"""
Explore Athena API — Adding Comments to Tickets

Discovers how to add analyst/user comments to incident tickets via the Athena API.

Test ticket: IR10522528 (created specifically for this exploration)

Approaches to test:
1. GET the ticket to see current comment structure
2. PUT with analystComments array (new comment object)
3. PUT with userComments array
4. PUT with actionLogs
5. PUT with a flat 'comment' field
6. Verify after each attempt

Usage:
    python exploration/explore_ticket_comments.py
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_TICKET_ID = "IR10522528"


def save_json(data, filename):
    """Save JSON data to the output directory."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def get_ticket(headers):
    """GET the ticket to check current state and comments."""
    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    print(f"\n  GET {url}")

    response = requests.get(url, headers=headers)
    print(f"  Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"  Ticket ID: {data.get('id')}")
        print(f"  Entity ID: {data.get('entityId')}")
        print(f"  Title: {data.get('title')}")
        print(f"  Status: {data.get('status')}")

        # Check comment fields
        analyst_comments = data.get('analystComments')
        user_comments = data.get('userComments')
        action_logs = data.get('actionLogs')

        print(f"\n  analystComments: {type(analyst_comments).__name__} = {json.dumps(analyst_comments, indent=4, default=str) if analyst_comments else 'None/Empty'}")
        print(f"  userComments: {type(user_comments).__name__} = {json.dumps(user_comments, indent=4, default=str) if user_comments else 'None/Empty'}")
        print(f"  actionLogs: {type(action_logs).__name__} = {json.dumps(action_logs, indent=4, default=str)[:500] if action_logs else 'None/Empty'}")

        return data
    else:
        print(f"  Error: {response.text[:500]}")
        return None


def test_put(headers, label, payload):
    """Run a PUT test and show result."""
    print(f"\n{'=' * 60}")
    print(f"TEST: {label}")
    print(f"{'=' * 60}")

    url = f"{INCIDENT_URL}"
    print(f"  PUT {url}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(url, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "label": label,
        "status_code": response.status_code,
        "payload_sent": payload,
    }

    if response.status_code == 200:
        put_data = response.json()
        result["response_analystComments"] = put_data.get("analystComments")
        result["response_userComments"] = put_data.get("userComments")
        result["response_actionLogs_count"] = len(put_data.get("actionLogs") or [])

        # Check if our comment appeared
        analyst_comments = put_data.get("analystComments") or []
        user_comments = put_data.get("userComments") or []
        print(f"  Response analystComments count: {len(analyst_comments)}")
        print(f"  Response userComments count: {len(user_comments)}")

        if analyst_comments:
            print(f"  Latest analystComment: {json.dumps(analyst_comments[-1] if analyst_comments else None, indent=4, default=str)}")
        if user_comments:
            print(f"  Latest userComment: {json.dumps(user_comments[-1] if user_comments else None, indent=4, default=str)}")
    else:
        result["error"] = response.text[:2000]
        print(f"  Error: {response.text[:500]}")

    return result


def main():
    print("=" * 60)
    print("  Athena Comment Creation Exploration")
    print(f"  Test Ticket: {TEST_TICKET_ID}")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    # Step 1: GET current state
    print("\n--- Step 1: GET current ticket state ---")
    ticket_data = get_ticket(headers)
    if not ticket_data:
        print("Failed to fetch test ticket. Exiting.")
        sys.exit(1)

    entity_id = ticket_data.get('entityId')
    print(f"\n  Entity ID: {entity_id}")
    save_json(ticket_data, "comment_explore_step1_get.json")

    all_results = []
    now_iso = datetime.now(timezone(timedelta(hours=-4))).isoformat()

    # Test 1: PUT with analystComments array containing a new comment
    r1 = test_put(headers, "PUT with analystComments array (new comment object)", {
        "entityId": entity_id,
        "analystComments": [
            {
                "comment": f"[TEST] Analyst comment added via API exploration at {now_iso}",
                "isPrivate": False,
            }
        ],
    })
    all_results.append(r1)

    # Verify
    print("\n  --- Verifying after Test 1 ---")
    get_ticket(headers)

    # Test 2: PUT with userComments array
    r2 = test_put(headers, "PUT with userComments array (new comment object)", {
        "entityId": entity_id,
        "userComments": [
            {
                "comment": f"[TEST] User comment added via API exploration at {now_iso}",
                "isPrivate": False,
            }
        ],
    })
    all_results.append(r2)

    # Verify
    print("\n  --- Verifying after Test 2 ---")
    get_ticket(headers)

    # Test 3: PUT with actionLogs
    r3 = test_put(headers, "PUT with actionLogs array (new entry)", {
        "entityId": entity_id,
        "actionLogs": [
            {
                "comment": f"[TEST] Action log added via API exploration at {now_iso}",
            }
        ],
    })
    all_results.append(r3)

    # Verify
    print("\n  --- Verifying after Test 3 ---")
    get_ticket(headers)

    # Test 4: PUT with flat 'comment' field
    r4 = test_put(headers, "PUT with flat 'comment' string field", {
        "entityId": entity_id,
        "comment": f"[TEST] Flat comment field via API exploration at {now_iso}",
    })
    all_results.append(r4)

    # Verify
    print("\n  --- Verifying after Test 4 ---")
    get_ticket(headers)

    # Test 5: PUT with analystComments including enteredBy and enteredDate
    r5 = test_put(headers, "PUT with analystComments (full fields: comment, enteredBy, enteredDate, isPrivate)", {
        "entityId": entity_id,
        "analystComments": [
            {
                "comment": f"[TEST] Full-field analyst comment at {now_iso}",
                "enteredBy": "aslanuka",
                "enteredDate": now_iso,
                "isPrivate": False,
            }
        ],
    })
    all_results.append(r5)

    # Verify
    print("\n  --- Verifying after Test 5 ---")
    get_ticket(headers)

    # Test 6: Try POST to a potential comments sub-endpoint
    print(f"\n{'=' * 60}")
    print("TEST: POST to /v1/incident/{ticket_id} (sub-resource attempt)")
    print(f"{'=' * 60}")

    # Try various sub-resource URLs
    sub_urls = [
        f"{ATHENA_BASE_URL}v1/incident/{TEST_TICKET_ID}/comments",
        f"{ATHENA_BASE_URL}v1/incident/{entity_id}/comments",
        f"{ATHENA_BASE_URL}v1/object/{entity_id}/comments",
    ]

    for sub_url in sub_urls:
        print(f"\n  POST {sub_url}")
        payload = {
            "comment": f"[TEST] Sub-resource comment at {now_iso}",
            "isPrivate": False,
        }
        response = requests.post(sub_url, headers=headers, json=payload)
        print(f"  Status: {response.status_code}")
        print(f"  Response: {response.text[:300]}")
        all_results.append({
            "label": f"POST {sub_url}",
            "status_code": response.status_code,
            "payload_sent": payload,
            "response": response.text[:1000],
        })

    # Final state
    print("\n\n--- Final ticket state ---")
    final_data = get_ticket(headers)
    if final_data:
        save_json(final_data, "comment_explore_final_state.json")

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in all_results:
        status = "OK" if r.get("status_code") == 200 else f"FAIL ({r.get('status_code')})"
        print(f"  [{status}] {r['label']}")

    save_json(all_results, "comment_explore_results.json")
    print("\nDone! Check exploration/output/ for detailed results.")


if __name__ == '__main__':
    main()