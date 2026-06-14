"""
Explore Athena API — Adding Comments to Tickets

Goal: Discover how to programmatically add analyst comments to tickets via the Athena API.

Tests:
1. PUT with 'analystComments' array in body (alongside entityId)
2. PUT with 'userInput' field in body
3. POST to /v1/incident/{ticket_id}/comments (if such endpoint exists)
4. PUT with 'actionLog' or 'notes' field

Test ticket: Use a ticket from the Validation queue that we can safely modify.

Usage:
    python exploration/explore_ticket_comments.py
"""

import os
import sys
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
SR_URL = os.getenv('ATHENA_SERVICEREQUEST_URL')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Use an ACTIVE test ticket from Validation queue
TEST_IR_TICKET_ID = "IR10509394"
TEST_IR_ENTITY_ID = "9c38ab22-cd2e-df1d-dd61-9c802d0392d7"

TIMESTAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
TEST_COMMENT = f"[TEST] Service Desk Helper comment exploration - {TIMESTAMP}"


def save_json(data, filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def get_ticket_comments(headers, ticket_id):
    """GET the ticket and extract current comments."""
    url = f"{INCIDENT_URL}{ticket_id}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        analyst_comments = data.get('analystComments', [])
        user_comments = data.get('userComments', [])
        print(f"  Current analystComments count: {len(analyst_comments)}")
        print(f"  Current userComments count: {len(user_comments)}")
        if analyst_comments:
            latest = analyst_comments[0] if analyst_comments else None
            if latest:
                print(f"  Latest analyst comment: {latest.get('comment', '')[:100]}...")
                print(f"  Latest comment enteredBy: {latest.get('enteredBy')}")
                print(f"  Latest comment enteredDate: {latest.get('enteredDate')}")
        return data
    else:
        print(f"  GET failed: {response.status_code} - {response.text[:300]}")
        return None


def test_put_with_analyst_comments(headers):
    """Test 1: PUT with analystComments array in body."""
    print(f"\n{'=' * 60}")
    print("TEST 1: PUT with analystComments array")
    print(f"{'=' * 60}")

    payload = {
        "entityId": TEST_IR_ENTITY_ID,
        "analystComments": [
            {
                "comment": TEST_COMMENT,
            }
        ],
    }

    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "test": "PUT with analystComments array",
        "status_code": response.status_code,
        "payload": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response_analystComments_count"] = len(data.get("analystComments", []))
        result["response_keys"] = list(data.keys())[:20]
        print(f"  Response analystComments count: {len(data.get('analystComments', []))}")
        # Check if our comment appears
        comments = data.get("analystComments", [])
        found = any(TEST_COMMENT in (c.get("comment", "") or "") for c in comments)
        result["comment_found_in_response"] = found
        print(f"  Our comment found in response: {found}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def test_put_with_user_input(headers):
    """Test 2: PUT with userInput field."""
    print(f"\n{'=' * 60}")
    print("TEST 2: PUT with userInput field")
    print(f"{'=' * 60}")

    payload = {
        "entityId": TEST_IR_ENTITY_ID,
        "userInput": TEST_COMMENT,
    }

    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "test": "PUT with userInput",
        "status_code": response.status_code,
        "payload": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response_userInput"] = data.get("userInput", "")[:200]
        print(f"  Response userInput: {data.get('userInput', '')[:100]}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def test_put_with_action_log(headers):
    """Test 3: PUT with actionLog field."""
    print(f"\n{'=' * 60}")
    print("TEST 3: PUT with actionLog field")
    print(f"{'=' * 60}")

    payload = {
        "entityId": TEST_IR_ENTITY_ID,
        "actionLog": TEST_COMMENT,
    }

    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "test": "PUT with actionLog",
        "status_code": response.status_code,
        "payload": payload,
    }

    if response.status_code == 200:
        data = response.json()
        result["response_actionLog"] = str(data.get("actionLog", ""))[:200]
        print(f"  Response actionLog: {str(data.get('actionLog', ''))[:100]}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def test_put_with_comment_field(headers):
    """Test 4: PUT with 'comment' field (singular)."""
    print(f"\n{'=' * 60}")
    print("TEST 4: PUT with 'comment' field (singular)")
    print(f"{'=' * 60}")

    payload = {
        "entityId": TEST_IR_ENTITY_ID,
        "comment": TEST_COMMENT,
    }

    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "test": "PUT with comment field",
        "status_code": response.status_code,
        "payload": payload,
    }

    if response.status_code == 200:
        data = response.json()
        # Check if comment appears in analystComments
        comments = data.get("analystComments", [])
        found = any(TEST_COMMENT in (c.get("comment", "") or "") for c in comments)
        result["comment_found_in_analystComments"] = found
        result["analystComments_count"] = len(comments)
        print(f"  analystComments count: {len(comments)}")
        print(f"  Our comment found: {found}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def test_post_comment_endpoint(headers):
    """Test 5: POST to a comments sub-endpoint."""
    print(f"\n{'=' * 60}")
    print("TEST 5: POST to /v1/incident/{id}/comments")
    print(f"{'=' * 60}")

    # Try various URL patterns
    url_patterns = [
        f"{INCIDENT_URL}{TEST_IR_TICKET_ID}/comments",
        f"{INCIDENT_URL}{TEST_IR_TICKET_ID}/analystComments",
        f"{ATHENA_BASE_URL}v1/comment/",
        f"{ATHENA_BASE_URL}v1/workitem/{TEST_IR_TICKET_ID}/comments",
    ]

    results = []
    for url in url_patterns:
        print(f"\n  Trying POST {url}")
        payload = {
            "comment": TEST_COMMENT,
            "entityId": TEST_IR_ENTITY_ID,
        }

        try:
            response = requests.post(url, headers=headers, json=payload)
            print(f"  Status: {response.status_code}")
            result = {
                "url": url,
                "status_code": response.status_code,
            }
            if response.status_code == 200:
                result["response"] = response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text[:500]
                print(f"  SUCCESS! Response: {str(result['response'])[:200]}")
            elif response.status_code == 404:
                print(f"  404 Not Found")
                result["error"] = "Not Found"
            else:
                result["error"] = response.text[:500]
                print(f"  Error: {response.text[:300]}")
            results.append(result)
        except Exception as e:
            print(f"  Exception: {e}")
            results.append({"url": url, "error": str(e)})

    return {"test": "POST comment endpoints", "attempts": results}


def test_put_with_analyst_comment_singular(headers):
    """Test 6: PUT with 'analystComment' (singular, not array)."""
    print(f"\n{'=' * 60}")
    print("TEST 6: PUT with 'analystComment' (singular string)")
    print(f"{'=' * 60}")

    payload = {
        "entityId": TEST_IR_ENTITY_ID,
        "analystComment": TEST_COMMENT,
    }

    print(f"  PUT {INCIDENT_URL}")
    print(f"  Payload: {json.dumps(payload, indent=2)}")

    response = requests.put(INCIDENT_URL, headers=headers, json=payload)
    print(f"  Status: {response.status_code}")

    result = {
        "test": "PUT with analystComment (singular)",
        "status_code": response.status_code,
        "payload": payload,
    }

    if response.status_code == 200:
        data = response.json()
        comments = data.get("analystComments", [])
        found = any(TEST_COMMENT in (c.get("comment", "") or "") for c in comments)
        result["comment_found_in_analystComments"] = found
        result["analystComments_count"] = len(comments)
        print(f"  analystComments count: {len(comments)}")
        print(f"  Our comment found: {found}")
    else:
        result["error"] = response.text[:1000]
        print(f"  Error: {response.text[:500]}")

    return result


def main():
    print("=" * 60)
    print("  Athena API — Comment Exploration")
    print(f"  Test Ticket: {TEST_IR_TICKET_ID}")
    print(f"  Entity ID: {TEST_IR_ENTITY_ID}")
    print(f"  Test Comment: {TEST_COMMENT}")
    print("=" * 60)

    headers = get_auth_headers()
    if not headers:
        print("Failed to authenticate. Exiting.")
        sys.exit(1)

    # Check initial state
    print("\n--- Initial State ---")
    initial_data = get_ticket_comments(headers, TEST_IR_TICKET_ID)
    initial_comment_count = len(initial_data.get('analystComments', [])) if initial_data else 0

    all_results = []

    # Run tests
    r1 = test_put_with_analyst_comments(headers)
    all_results.append(r1)

    # Verify if comment was added
    print("\n  --- Verifying with GET after Test 1 ---")
    after_t1 = get_ticket_comments(headers, TEST_IR_TICKET_ID)
    after_t1_count = len(after_t1.get('analystComments', [])) if after_t1 else 0
    r1["comment_count_before"] = initial_comment_count
    r1["comment_count_after"] = after_t1_count
    r1["comment_was_added"] = after_t1_count > initial_comment_count
    print(f"  Comment count before: {initial_comment_count}, after: {after_t1_count}")
    print(f"  COMMENT WAS ADDED: {r1['comment_was_added']}")

    r2 = test_put_with_user_input(headers)
    all_results.append(r2)

    r3 = test_put_with_action_log(headers)
    all_results.append(r3)

    r4 = test_put_with_comment_field(headers)
    all_results.append(r4)

    # Verify after test 4
    print("\n  --- Verifying with GET after Test 4 ---")
    after_t4 = get_ticket_comments(headers, TEST_IR_TICKET_ID)
    after_t4_count = len(after_t4.get('analystComments', [])) if after_t4 else 0
    r4["comment_count_after"] = after_t4_count
    r4["comment_was_added"] = after_t4_count > after_t1_count
    print(f"  Comment count after Test 4: {after_t4_count}")
    print(f"  COMMENT WAS ADDED by Test 4: {r4.get('comment_was_added')}")

    r5 = test_post_comment_endpoint(headers)
    all_results.append(r5)

    r6 = test_put_with_analyst_comment_singular(headers)
    all_results.append(r6)

    # Final verification
    print("\n--- Final State ---")
    final_data = get_ticket_comments(headers, TEST_IR_TICKET_ID)
    final_count = len(final_data.get('analystComments', [])) if final_data else 0
    print(f"\n  Initial comment count: {initial_comment_count}")
    print(f"  Final comment count: {final_count}")
    print(f"  Total comments added: {final_count - initial_comment_count}")

    # If any comments were added, show the new ones
    if final_data and final_count > initial_comment_count:
        print("\n  NEW COMMENTS ADDED:")
        all_comments = final_data.get('analystComments', [])
        for c in all_comments[:final_count - initial_comment_count]:
            print(f"    - [{c.get('enteredDate')}] {c.get('enteredBy')}: {c.get('comment', '')[:100]}")

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in all_results:
        test_name = r.get("test", "unknown")
        status = r.get("status_code", "N/A")
        added = r.get("comment_was_added", "unknown")
        print(f"  [{status}] {test_name} — Comment added: {added}")

    save_json(all_results, "ticket_comment_exploration.json")
    print("\nDone!")


if __name__ == '__main__':
    main()