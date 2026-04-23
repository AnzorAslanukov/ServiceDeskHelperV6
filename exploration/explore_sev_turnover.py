"""
Explore Athena API for SEV Turnover Email Feature (#5)

Investigates:
1. Querying P1/P2 (SEV) incidents by urgency
2. Querying upcoming Change Requests
3. Examining ticket fields: isParent, priority, status, tierQueue, assignedToUser
4. Testing various filter approaches for the turnover email generator
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_VIEW_URL = os.getenv('ATHENA_INCIDENT_VIEW_URL')
CHANGEREQUEST_VIEW_URL = f"{ATHENA_BASE_URL}v1/view/workitem?type=changeRequest"
INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
CHANGEREQUEST_URL = os.getenv('ATHENA_CHANGEREQUEST_URL')

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def save_json(data, filename):
    """Save data to JSON file in output directory."""
    path = os.path.join(OUTPUT_DIR, f"{filename}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved to: {path}")
    return path


def save_text(text, filename):
    """Save text to file in output directory."""
    path = os.path.join(OUTPUT_DIR, f"{filename}.txt")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"  Saved to: {path}")
    return path


# ── Test 1: Query P1/P2 Incidents by Urgency ─────────────────────────

def test_sev_incidents_by_urgency(headers):
    """
    Query incidents where urgency is Urgent (P1) or High (P2),
    and status is not Resolved or Closed.
    """
    print("\n" + "=" * 70)
    print("TEST 1: Query P1/P2 SEV Incidents by Urgency")
    print("=" * 70)
    
    # Filter: urgency in (Urgent, High) AND status not in (Resolved, Closed)
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "urgency",
                    "operator": "in",
                    "value": "Urgent,High"
                },
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "not in",
                    "value": "Resolved,Closed"
                }
            ]
        }
    ]
    
    print(f"  URL: {INCIDENT_VIEW_URL}")
    print(f"  Filter: {json.dumps(filters, indent=2)}")
    
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        has_more = data.get('hasMoreResults', False)
        results = data.get('result', [])
        
        print(f"  Result count: {result_count}")
        print(f"  Has more results: {has_more}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "sev_incidents_by_urgency")
        
        # Print summary of each ticket
        print("\n  --- SEV Ticket Summary ---")
        for ticket in results[:20]:  # Limit to first 20
            tid = ticket.get('id', 'N/A')
            title = ticket.get('title', 'N/A')[:80]
            status = ticket.get('status', {})
            if isinstance(status, dict):
                status_name = status.get('name', 'N/A')
            else:
                status_name = str(status)
            
            urgency = ticket.get('urgency', {})
            if isinstance(urgency, dict):
                urgency_name = urgency.get('name', 'N/A')
            else:
                urgency_name = str(urgency)
            
            priority = ticket.get('priority', 'N/A')
            is_parent = ticket.get('isParent', False)
            
            assigned = ticket.get('assignedToUser', {})
            if isinstance(assigned, dict):
                assigned_name = assigned.get('displayName', 'Unassigned')
            else:
                assigned_name = 'Unassigned'
            
            tier_queue = ticket.get('tierQueue', {})
            if isinstance(tier_queue, dict):
                tier_name = tier_queue.get('name', 'N/A')
            else:
                tier_name = str(tier_queue) if tier_queue else 'N/A'
            
            print(f"  {tid} | Urgency: {urgency_name} | Priority: {priority} | "
                  f"Status: {status_name} | Parent: {is_parent} | "
                  f"Assigned: {assigned_name} | Queue: {tier_name}")
            print(f"    Title: {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 2: Query P1/P2 Incidents using Priority field ───────────────

def test_sev_incidents_by_priority(headers):
    """
    Test if we can filter by the 'priority' field directly.
    Priority might be numeric (1, 2) or might be null on incidents.
    """
    print("\n" + "=" * 70)
    print("TEST 2: Query Incidents by Priority Field (numeric)")
    print("=" * 70)
    
    # Try filtering by priority = 1 or 2
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "priority",
                    "operator": "in",
                    "value": "1,2"
                },
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "not in",
                    "value": "Resolved,Closed"
                }
            ]
        }
    ]
    
    print(f"  Filter: priority in (1, 2)")
    
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "sev_incidents_by_priority")
        
        # Show first few
        for ticket in results[:5]:
            tid = ticket.get('id', 'N/A')
            priority = ticket.get('priority', 'N/A')
            urgency = ticket.get('urgency', {})
            urg_name = urgency.get('name', 'N/A') if isinstance(urgency, dict) else str(urgency)
            print(f"  {tid} | Priority: {priority} | Urgency: {urg_name}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 3: Query Upcoming Change Requests ────────────────────────────

def test_upcoming_change_requests(headers):
    """
    Query change requests with scheduled start dates in the near future.
    Tests the changeRequest view endpoint.
    """
    print("\n" + "=" * 70)
    print("TEST 3: Query Upcoming Change Requests")
    print("=" * 70)
    
    # Filter: ScheduledStartDate > [now] AND ScheduledStartDate < [now]+48h
    # AND status not in (Completed, Failed, Cancelled, Closed)
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "ScheduledStartDate",
                    "operator": "gt",
                    "value": "[now]"
                },
                {
                    "condition": "and",
                    "property": "ScheduledStartDate",
                    "operator": "lt",
                    "value": "[now]+48h"
                },
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "not in",
                    "value": "Completed,Failed,Cancelled,Closed"
                }
            ]
        }
    ]
    
    print(f"  URL: {CHANGEREQUEST_VIEW_URL}")
    print(f"  Filter: ScheduledStartDate between [now] and [now]+48h, active statuses")
    
    response = requests.post(CHANGEREQUEST_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        has_more = data.get('hasMoreResults', False)
        results = data.get('result', [])
        
        print(f"  Result count: {result_count}")
        print(f"  Has more results: {has_more}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "upcoming_change_requests")
        
        # Print summary
        print("\n  --- Upcoming Change Requests ---")
        for cr in results[:20]:
            crid = cr.get('id', 'N/A')
            title = cr.get('title', 'N/A')[:80]
            status = cr.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            
            sched_start = cr.get('scheduledStartDate', 'N/A')
            sched_end = cr.get('scheduledEndDate', 'N/A')
            downtime = cr.get('downtime', cr.get('isDowntime', 'N/A'))
            category = cr.get('category', {})
            cat_name = category.get('name', 'N/A') if isinstance(category, dict) else str(category)
            
            print(f"  {crid} | Status: {status_name} | Category: {cat_name}")
            print(f"    Title: {title}")
            print(f"    Scheduled: {sched_start} → {sched_end}")
            print(f"    Downtime: {downtime}")
        
        # Save first CR detail for field analysis
        if results:
            save_json(results[0], "sample_change_request_from_view")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        # Try alternative approach
        print("\n  Trying alternative: GET /v1/object/query with type=changerequest...")
        alt_url = f"{ATHENA_BASE_URL}v1/object/query"
        params = {
            'type': 'changerequest',
            '$filter': "ScheduledStartDate gt '4-1-2026'",
            '$orderby': 'ScheduledStartDate Desc',
            '$top': 10
        }
        alt_response = requests.get(alt_url, headers=headers, params=params)
        print(f"  Alt Status: {alt_response.status_code}")
        if alt_response.status_code == 200:
            alt_data = alt_response.json()
            print(f"  Alt Result count: {alt_data.get('resultCount', 0)}")
            save_json(alt_data, "upcoming_change_requests_alt")
            
            results = alt_data.get('result', [])
            for cr in results[:5]:
                crid = cr.get('id', 'N/A')
                title = cr.get('title', 'N/A')[:80]
                sched_start = cr.get('scheduledStartDate', 'N/A')
                print(f"  {crid} | {title} | Start: {sched_start}")
            
            if results:
                save_json(results[0], "sample_change_request_from_query")
            
            return alt_data
        else:
            print(f"  Alt FAILED: {alt_response.text[:500]}")
            return None


# ── Test 4: Fetch a specific SEV ticket detail ───────────────────────

def test_fetch_sev_detail(headers, ticket_id="IR10371854"):
    """
    Fetch full detail of a known SEV ticket to examine all fields.
    Using IR10371854 from the turnover email examples.
    """
    print("\n" + "=" * 70)
    print(f"TEST 4: Fetch SEV Ticket Detail ({ticket_id})")
    print("=" * 70)
    
    url = f"{ATHENA_BASE_URL}v1/incident/{ticket_id}"
    response = requests.get(url, headers=headers)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        save_json(data, f"sev_detail_{ticket_id}")
        
        # Print key fields
        print(f"  ID: {data.get('id')}")
        print(f"  Title: {data.get('title')}")
        print(f"  Status: {data.get('status')}")
        print(f"  Priority: {data.get('priority')}")
        print(f"  Urgency: {data.get('urgency')}")
        print(f"  Impact: {data.get('impact')}")
        print(f"  isParent: {data.get('isParent')}")
        print(f"  escalated: {data.get('escalated')}")
        print(f"  tierQueue: {data.get('tierQueue')}")
        print(f"  assignedToUser: {data.get('assignedToUser')}")
        print(f"  affectedUser displayName: {data.get('affectedUser', {}).get('displayName', 'N/A') if isinstance(data.get('affectedUser'), dict) else data.get('affectedUser')}")
        print(f"  location: {data.get('location')}")
        print(f"  floor: {data.get('floor')}")
        print(f"  room: {data.get('room')}")
        print(f"  createdDate: {data.get('createdDate')}")
        print(f"  parent: {data.get('parent')}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 5: Query incidents with isParent filter ─────────────────────

def test_parent_incidents(headers):
    """
    Test if we can filter for parent incidents using the view endpoint.
    """
    print("\n" + "=" * 70)
    print("TEST 5: Query Parent Incidents")
    print("=" * 70)
    
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "IsParent",
                    "operator": "eq",
                    "value": "True"
                },
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "not in",
                    "value": "Resolved,Closed"
                }
            ]
        }
    ]
    
    print(f"  Filter: IsParent = True AND status not in (Resolved, Closed)")
    
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "parent_incidents")
        
        for ticket in results[:10]:
            tid = ticket.get('id', 'N/A')
            title = ticket.get('title', 'N/A')[:80]
            is_parent = ticket.get('isParent', False)
            print(f"  {tid} | isParent: {is_parent} | {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 6: Query incidents with Pending status ──────────────────────

def test_pending_incidents(headers):
    """
    Test querying incidents with Pending status to understand pended SEVs.
    """
    print("\n" + "=" * 70)
    print("TEST 6: Query Pending Incidents (Pended SEVs)")
    print("=" * 70)
    
    # Pending is a child status of Active: GUID b6679968-e84e-96fa-1fec-8cd4ab39c3de
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "eq",
                    "value": "Pending"
                },
                {
                    "condition": "and",
                    "property": "urgency",
                    "operator": "in",
                    "value": "Urgent,High"
                }
            ]
        }
    ]
    
    print(f"  Filter: status = Pending AND urgency in (Urgent, High)")
    
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "pending_sev_incidents")
        
        for ticket in results[:10]:
            tid = ticket.get('id', 'N/A')
            title = ticket.get('title', 'N/A')[:80]
            status = ticket.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            print(f"  {tid} | Status: {status_name} | {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 7: Fetch a Change Request detail ─────────────────────────────

def test_fetch_cr_detail(headers, cr_id="CR10312956"):
    """
    Fetch full detail of a known CR from the turnover email examples.
    """
    print("\n" + "=" * 70)
    print(f"TEST 7: Fetch Change Request Detail ({cr_id})")
    print("=" * 70)
    
    url = f"{CHANGEREQUEST_URL}{cr_id}"
    response = requests.get(url, headers=headers)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        save_json(data, f"cr_detail_{cr_id}")
        
        print(f"  ID: {data.get('id')}")
        print(f"  Title: {data.get('title')}")
        print(f"  Status: {data.get('status')}")
        print(f"  Category: {data.get('category')}")
        print(f"  Risk: {data.get('risk')}")
        print(f"  ScheduledStartDate: {data.get('scheduledStartDate')}")
        print(f"  ScheduledEndDate: {data.get('scheduledEndDate')}")
        print(f"  Downtime: {data.get('downtime')}")
        print(f"  ScheduledDowntimeStartDate: {data.get('scheduledDowntimeStartDate')}")
        print(f"  ScheduledDowntimeEndDate: {data.get('scheduledDowntimeEndDate')}")
        print(f"  Description: {str(data.get('description', ''))[:200]}")
        print(f"  Command Center: {data.get('command_Center')}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test 8: Examine view endpoint result fields ──────────────────────

def test_view_result_fields(headers):
    """
    Fetch a small set of incidents from the view endpoint to see
    exactly which fields are returned (view may return fewer fields
    than the detail endpoint).
    """
    print("\n" + "=" * 70)
    print("TEST 8: Examine View Endpoint Result Fields")
    print("=" * 70)
    
    # Just get a couple recent active incidents
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "eq",
                    "value": "Active"
                },
                {
                    "condition": "and",
                    "property": "CreatedDate",
                    "operator": "gt",
                    "value": "[today]-7d"
                }
            ]
        }
    ]
    
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        results = data.get('result', [])
        
        if results:
            first = results[0]
            print(f"\n  Fields returned by view endpoint ({len(first.keys())} fields):")
            for key in sorted(first.keys()):
                val = first[key]
                val_preview = str(val)[:100] if val is not None else 'null'
                print(f"    {key}: {val_preview}")
            
            save_json(first, "view_endpoint_sample_fields")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("ATHENA SEV TURNOVER EXPLORATION")
    print("Feature #5: Turnover Email Draft Generator")
    print("=" * 70)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    summary = []
    
    # Test 1: SEV incidents by urgency
    result1 = test_sev_incidents_by_urgency(headers)
    if result1:
        count = result1.get('resultCount', 0)
        summary.append(f"TEST 1 (SEV by urgency): SUCCESS — {count} results")
    else:
        summary.append("TEST 1 (SEV by urgency): FAILED")
    
    # Test 2: Incidents by priority field
    result2 = test_sev_incidents_by_priority(headers)
    if result2:
        count = result2.get('resultCount', 0)
        summary.append(f"TEST 2 (by priority field): SUCCESS — {count} results")
    else:
        summary.append("TEST 2 (by priority field): FAILED")
    
    # Test 3: Upcoming Change Requests
    result3 = test_upcoming_change_requests(headers)
    if result3:
        count = result3.get('resultCount', len(result3.get('result', [])))
        summary.append(f"TEST 3 (upcoming CRs): SUCCESS — {count} results")
    else:
        summary.append("TEST 3 (upcoming CRs): FAILED")
    
    # Test 4: SEV ticket detail
    result4 = test_fetch_sev_detail(headers)
    if result4:
        summary.append(f"TEST 4 (SEV detail): SUCCESS — {result4.get('id')}")
    else:
        summary.append("TEST 4 (SEV detail): FAILED")
    
    # Test 5: Parent incidents
    result5 = test_parent_incidents(headers)
    if result5:
        count = result5.get('resultCount', 0)
        summary.append(f"TEST 5 (parent incidents): SUCCESS — {count} results")
    else:
        summary.append("TEST 5 (parent incidents): FAILED")
    
    # Test 6: Pending SEV incidents
    result6 = test_pending_incidents(headers)
    if result6:
        count = result6.get('resultCount', 0)
        summary.append(f"TEST 6 (pending SEVs): SUCCESS — {count} results")
    else:
        summary.append("TEST 6 (pending SEVs): FAILED")
    
    # Test 7: CR detail
    result7 = test_fetch_cr_detail(headers)
    if result7:
        summary.append(f"TEST 7 (CR detail): SUCCESS — {result7.get('id')}")
    else:
        summary.append("TEST 7 (CR detail): FAILED")
    
    # Test 8: View endpoint fields
    result8 = test_view_result_fields(headers)
    if result8:
        results = result8.get('result', [])
        field_count = len(results[0].keys()) if results else 0
        summary.append(f"TEST 8 (view fields): SUCCESS — {field_count} fields per ticket")
    else:
        summary.append("TEST 8 (view fields): FAILED")
    
    # Print summary
    print("\n" + "=" * 70)
    print("EXPLORATION SUMMARY")
    print("=" * 70)
    for line in summary:
        print(f"  {line}")
    
    # Save summary
    summary_text = "SEV Turnover Exploration Summary\n"
    summary_text += "=" * 70 + "\n\n"
    for line in summary:
        summary_text += f"{line}\n"
    save_text(summary_text, "sev_turnover_exploration_summary")
    
    print("\n" + "=" * 70)
    print("EXPLORATION COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()