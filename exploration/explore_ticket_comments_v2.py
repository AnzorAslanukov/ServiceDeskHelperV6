"""
Explore Athena API — Adding Comments to Tickets (v2)

Follow-up from v1 findings:
- analystComments, userComments via PUT → 200 OK but silently ignored
- actionLogs via PUT → 400 error: "Title field is required", "Description field is required"
  This means actionLogs IS a valid writable field, just needs correct structure!
- Sub-resource POST endpoints → 404

This script tests:
1. actionLogs with Title + Description fields
2. actionLogs with various field combinations
3. Other potential comment mechanisms (notes, description append)

Test ticket: IR10522528 (entityId: b1a485a4-7423-4b90-bd5c-a5bc048be1c0)

Usage:
    python exploration/explore_ticket_comments_v2.py
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
TEST_ENTITY_ID = "b1a485a4-7423-4b90-bd5c-a5bc048be1c0"


def save_json(data, filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def get_ticket(headers, verbose=True):
    """GET the ticket to check current state and comments."""
    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        data = response.json()
        if verbose:
            analyst_comments = data.get('analystComments') or []
            user_comments = data.get('userComments') or []
            action_logs = data.get('actionLogs') or []
            print(f"  analystComments: {len(analyst_comments)}")
            print(f"  userComments: {len(user_comments)}")
            print(f"  actionLogs: {len(action_logs)}")
            if action_logs:
                print(f"  Latest actionLog: {json.dumps(action_logs[-1], indent=4, default=str)[:500]}")
            if analyst_comments:
                print(f"  Latest analystComment: {json.dumps(analyst_comments[-1], indent=4, default=str)[:500]}")
        return data
    else:
        print(f"  GET failed: {response.status_code} - {response.text[:300]}")
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
        analyst_comments = put_data.get("analystComments") or []
        user_comments = put_data.get("userComments") or []
        action_logs = put_data.get("actionLogs") or []
        result["response_analystComments_count"] = len(analyst_comments)
        result["response_userComments_count"] = len(user_comments)
        result["response_actionLogs_count"] = len(action_logs)
        print(f"  analystComments: {len(analyst_comments)}, userComments: {len(user_comments)}, actionLogs: {len(action_logs)}")

        if action_logs:
            print(f"  Latest actionLog: {json.dumps(action_logs[-1], indent=4, default=str)[:500]}")
        if analyst_comments:
            print(f"  Latest analystComment: {json.dumps(analyst_comments[-1], indent=4, default=str)[:500]}")
        if user_comments:
            print(f"  Latest userComment: {json.dumps(user_comments[-1], indent=4, default=str)[:500]}")

        # Check for any new fields in response that might indicate comment was added
        result["success_indicator"] = len(action_logs) > 0 or len(analyst_comments) > 0 or len(user_comments) > 0
    else:
        result["error"] = response.text[:2000]
        print(f"  Error: {response.text[:500]}")

    return result


def main():
    print("=" * 60)
    print("  Athena Comment Creation Exploration v2")
    print(f"  Test Ticket: {TEST_TICKET_ID}")
    print(f"  Entity ID: {TEST_ENTITY_ID}")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    # Initial state
    print("\n--- Initial State ---")
    get_ticket(headers)

    all_results = []
    now_iso = datetime.now(timezone(timedelta(hours=-4))).isoformat()

    # Test 1: actionLogs with Title + Description (the error told us these are required)
    r1 = test_put(headers, "actionLogs with Title + Description", {
        "entityId": TEST_ENTITY_ID,
        "actionLogs": [
            {
                "Title": "Test Comment via API",
                "Description": f"[TEST] Action log with Title+Description at {now_iso}",
            }
        ],
    })
    all_results.append(r1)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 2: actionLogs with title + description (lowercase)
    r2 = test_put(headers, "actionLogs with title + description (lowercase)", {
        "entityId": TEST_ENTITY_ID,
        "actionLogs": [
            {
                "title": "Test Comment via API (lowercase)",
                "description": f"[TEST] Action log with lowercase title+description at {now_iso}",
            }
        ],
    })
    all_results.append(r2)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 3: actionLogs with Title + Description + EnteredBy
    r3 = test_put(headers, "actionLogs with Title + Description + EnteredBy", {
        "entityId": TEST_ENTITY_ID,
        "actionLogs": [
            {
                "Title": "API Comment with Author",
                "Description": f"[TEST] Action log with author at {now_iso}",
                "EnteredBy": "aslanuka",
            }
        ],
    })
    all_results.append(r3)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 4: actionLogs with Title + Description + enteredDate + isPrivate
    r4 = test_put(headers, "actionLogs with all fields (Title, Description, EnteredBy, EnteredDate, IsPrivate)", {
        "entityId": TEST_ENTITY_ID,
        "actionLogs": [
            {
                "Title": "Full Field Action Log",
                "Description": f"[TEST] Full-field action log at {now_iso}",
                "EnteredBy": "aslanuka",
                "EnteredDate": now_iso,
                "IsPrivate": False,
            }
        ],
    })
    all_results.append(r4)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 5: Try 'ActionLog' (singular) at top level
    r5 = test_put(headers, "ActionLog (singular) with Title + Description", {
        "entityId": TEST_ENTITY_ID,
        "ActionLog": {
            "Title": "Singular ActionLog Test",
            "Description": f"[TEST] Singular ActionLog at {now_iso}",
        },
    })
    all_results.append(r5)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 6: Try 'notes' field
    r6 = test_put(headers, "notes field (string)", {
        "entityId": TEST_ENTITY_ID,
        "notes": f"[TEST] Notes field at {now_iso}",
    })
    all_results.append(r6)
    print("\n  --- Verify ---")
    data6 = get_ticket(headers)
    if data6:
        print(f"  notes field in response: {data6.get('notes')}")

    # Test 7: Try the escalate endpoint (POST /v1/incident/escalate) which might accept comments
    print(f"\n{'=' * 60}")
    print("TEST: Check if POST /v1/incident/escalate accepts a comment")
    print(f"{'=' * 60}")
    # Don't actually escalate — just check the endpoint structure
    # Skip this to avoid changing ticket state

    # Test 8: Try analystComments with 'Comment' (capital C) field name
    r8 = test_put(headers, "analystComments with 'Comment' (capital C)", {
        "entityId": TEST_ENTITY_ID,
        "analystComments": [
            {
                "Comment": f"[TEST] Capital-C Comment field at {now_iso}",
                "IsPrivate": False,
            }
        ],
    })
    all_results.append(r8)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 9: Try AnalystComments (capital A)
    r9 = test_put(headers, "AnalystComments (capital A) with Comment field", {
        "entityId": TEST_ENTITY_ID,
        "AnalystComments": [
            {
                "Comment": f"[TEST] Capital AnalystComments at {now_iso}",
                "IsPrivate": False,
            }
        ],
    })
    all_results.append(r9)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Test 10: Try UserComments (capital U) with Comment field
    r10 = test_put(headers, "UserComments (capital U) with Comment field", {
        "entityId": TEST_ENTITY_ID,
        "UserComments": [
            {
                "Comment": f"[TEST] Capital UserComments at {now_iso}",
                "IsPrivate": False,
            }
        ],
    })
    all_results.append(r10)
    print("\n  --- Verify ---")
    get_ticket(headers)

    # Final state
    print("\n\n--- FINAL STATE ---")
    final_data = get_ticket(headers)
    if final_data:
        save_json(final_data, "comment_explore_v2_final_state.json")

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in all_results:
        status_code = r.get("status_code")
        if status_code == 200:
            has_comments = r.get("success_indicator", False)
            status = "OK + COMMENT ADDED" if has_comments else "OK (silently ignored)"
        else:
            status = f"FAIL ({status_code})"
        print(f"  [{status}] {r['label']}")

    save_json(all_results, "comment_explore_v2_results.json")
    print("\nDone!")


if __name__ == '__main__':
    main()