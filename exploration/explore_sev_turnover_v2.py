"""
Explore Athena API for SEV Turnover — Round 2

The view filter endpoint returned 500 errors with 'in'/'not in' operators.
This script tries alternative approaches:
1. Using the object query endpoint (GET /v1/object/query) instead of view
2. Using GUID values instead of names in view filters
3. Using 'eq' operator instead of 'in'
4. Testing what filter syntax actually works
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
OBJECT_QUERY_URL = f"{ATHENA_BASE_URL}v1/object/query"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def save_json(data, filename):
    path = os.path.join(OUTPUT_DIR, f"{filename}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved to: {path}")


# ── Test A: Object Query for P1/P2 incidents ─────────────────────────

def test_object_query_sev_incidents(headers):
    """
    Use GET /v1/object/query to find P1/P2 incidents.
    This endpoint uses simple $filter syntax.
    """
    print("\n" + "=" * 70)
    print("TEST A: Object Query — SEV Incidents (Priority 1 or 2)")
    print("=" * 70)
    
    # Try filtering by Priority
    params = {
        'type': 'incident',
        '$filter': "Priority eq 1 or Priority eq 2",
        '$orderby': 'CreatedDate Desc',
        '$top': 20
    }
    
    print(f"  URL: {OBJECT_QUERY_URL}")
    print(f"  Filter: {params['$filter']}")
    
    response = requests.get(OBJECT_QUERY_URL, headers=headers, params=params)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "sev_object_query_priority")
        
        for ticket in results[:10]:
            tid = ticket.get('id', 'N/A')
            title = ticket.get('title', 'N/A')[:70]
            priority = ticket.get('priority', 'N/A')
            status = ticket.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            urgency = ticket.get('urgency', {})
            urg_name = urgency.get('name', 'N/A') if isinstance(urgency, dict) else str(urgency)
            is_parent = ticket.get('isParent', 'N/A')
            assigned = ticket.get('assignedToUser', {})
            assigned_name = assigned.get('displayName', 'Unassigned') if isinstance(assigned, dict) else 'Unassigned'
            tier = ticket.get('tierQueue', {})
            tier_name = tier.get('name', 'N/A') if isinstance(tier, dict) else str(tier) if tier else 'N/A'
            
            print(f"  {tid} | P{priority} | Urg: {urg_name} | Status: {status_name} | "
                  f"Parent: {is_parent} | Assigned: {assigned_name} | Queue: {tier_name}")
            print(f"    Title: {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test B: Object Query for active SEV incidents ────────────────────

def test_object_query_active_sevs(headers):
    """
    Query active (not resolved/closed) SEV incidents.
    """
    print("\n" + "=" * 70)
    print("TEST B: Object Query — Active SEV Incidents")
    print("=" * 70)
    
    # Try with status filter
    params = {
        'type': 'incident',
        '$filter': "(Priority eq 1 or Priority eq 2) and Status ne 'Resolved' and Status ne 'Closed'",
        '$orderby': 'CreatedDate Desc',
        '$top': 20
    }
    
    print(f"  Filter: {params['$filter']}")
    
    response = requests.get(OBJECT_QUERY_URL, headers=headers, params=params)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "sev_object_query_active")
        
        for ticket in results[:10]:
            tid = ticket.get('id', 'N/A')
            title = ticket.get('title', 'N/A')[:70]
            priority = ticket.get('priority', 'N/A')
            status = ticket.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            is_parent = ticket.get('isParent', 'N/A')
            
            print(f"  {tid} | P{priority} | Status: {status_name} | Parent: {is_parent} | {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        
        # Try simpler filter
        print("\n  Trying simpler filter...")
        params2 = {
            'type': 'incident',
            '$filter': "Priority eq 2",
            '$orderby': 'CreatedDate Desc',
            '$top': 10
        }
        response2 = requests.get(OBJECT_QUERY_URL, headers=headers, params=params2)
        print(f"  Status: {response2.status_code}")
        if response2.status_code == 200:
            data2 = response2.json()
            print(f"  Result count: {data2.get('resultCount', 0)}")
            save_json(data2, "sev_object_query_p2_only")
            results = data2.get('result', [])
            for t in results[:5]:
                print(f"  {t.get('id')} | P{t.get('priority')} | {t.get('title', '')[:60]}")
            return data2
        else:
            print(f"  FAILED: {response2.text[:300]}")
        
        return None


# ── Test C: View filter with GUID values ─────────────────────────────

def test_view_filter_with_guids(headers):
    """
    Try the view filter endpoint using GUID values instead of names.
    The 'in' operator might need GUIDs.
    """
    print("\n" + "=" * 70)
    print("TEST C: View Filter — Using Status GUID (Active)")
    print("=" * 70)
    
    # Active status GUID: 5e2d3932-ca6d-1515-7310-6f58584df73e
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "status",
                    "operator": "eq",
                    "value": "5e2d3932-ca6d-1515-7310-6f58584df73e"
                }
            ]
        }
    ]
    
    print(f"  Filter: status eq Active_GUID")
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Result count: {data.get('resultCount', 0)}")
        print(f"  ✓ View filter with GUID works!")
        save_json(data, "view_filter_guid_test")
        
        results = data.get('result', [])
        if results:
            first = results[0]
            print(f"\n  Fields in view result ({len(first.keys())} fields):")
            for key in sorted(first.keys()):
                val = first[key]
                val_str = str(val)[:80] if val is not None else 'null'
                print(f"    {key}: {val_str}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test D: View filter with name value ──────────────────────────────

def test_view_filter_with_name(headers):
    """
    Try the view filter endpoint using name values.
    """
    print("\n" + "=" * 70)
    print("TEST D: View Filter — Using Status Name (Active)")
    print("=" * 70)
    
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "Status",
                    "operator": "eq",
                    "value": "Active"
                }
            ]
        }
    ]
    
    print(f"  Filter: Status eq 'Active'")
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Result count: {data.get('resultCount', 0)}")
        print(f"  ✓ View filter with name works!")
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test E: View filter with Priority ─────────────────────────────────

def test_view_filter_priority(headers):
    """
    Try filtering by Priority in the view endpoint.
    """
    print("\n" + "=" * 70)
    print("TEST E: View Filter — Priority eq 2")
    print("=" * 70)
    
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "Priority",
                    "operator": "eq",
                    "value": "2"
                }
            ]
        }
    ]
    
    print(f"  Filter: Priority eq 2")
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        print(f"  Result count: {result_count}")
        print(f"  ✓ Priority filter works!")
        save_json(data, "view_filter_priority_2")
        
        results = data.get('result', [])
        for t in results[:5]:
            tid = t.get('id', 'N/A')
            priority = t.get('priority', 'N/A')
            title = t.get('title', 'N/A')[:60]
            print(f"  {tid} | P{priority} | {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        
        # Try with numeric value
        print("\n  Trying with integer value...")
        filters2 = [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "Priority",
                        "operator": "eq",
                        "value": 2
                    }
                ]
            }
        ]
        response2 = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters2)
        print(f"  Status: {response2.status_code}")
        if response2.status_code == 200:
            data2 = response2.json()
            print(f"  Result count: {data2.get('resultCount', 0)}")
            print(f"  ✓ Priority filter with integer works!")
            return data2
        else:
            print(f"  FAILED: {response2.text[:300]}")
        
        return None


# ── Test F: View filter combining Priority and Status ─────────────────

def test_view_filter_sev_combined(headers):
    """
    Try combining Priority and Status filters using 'or' for P1/P2.
    """
    print("\n" + "=" * 70)
    print("TEST F: View Filter — Combined P1/P2 + Active Status")
    print("=" * 70)
    
    # Use 'or' condition for priority 1 or 2
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "or",
                    "filters": [
                        {
                            "condition": "and",
                            "property": "Priority",
                            "operator": "eq",
                            "value": "1"
                        },
                        {
                            "condition": "and",
                            "property": "Priority",
                            "operator": "eq",
                            "value": "2"
                        }
                    ]
                },
                {
                    "condition": "and",
                    "property": "Status",
                    "operator": "ne",
                    "value": "Resolved"
                },
                {
                    "condition": "and",
                    "property": "Status",
                    "operator": "ne",
                    "value": "Closed"
                }
            ]
        }
    ]
    
    print(f"  Filter: (Priority=1 OR Priority=2) AND Status!=Resolved AND Status!=Closed")
    response = requests.post(INCIDENT_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        print(f"  ✓ Combined SEV filter works!")
        
        save_json(data, "view_filter_sev_combined")
        
        for t in results[:15]:
            tid = t.get('id', 'N/A')
            priority = t.get('priority', 'N/A')
            title = t.get('title', 'N/A')[:60]
            status = t.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            is_parent = t.get('isParent', 'N/A')
            assigned = t.get('assignedToUser', {})
            assigned_name = assigned.get('displayName', 'Unassigned') if isinstance(assigned, dict) else 'Unassigned'
            
            print(f"  {tid} | P{priority} | Status: {status_name} | Parent: {is_parent} | "
                  f"Assigned: {assigned_name}")
            print(f"    Title: {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test G: Change Request view filter ────────────────────────────────

def test_cr_view_filter(headers):
    """
    Try the change request view filter with simpler syntax.
    """
    print("\n" + "=" * 70)
    print("TEST G: CR View Filter — Upcoming by ScheduledStartDate")
    print("=" * 70)
    
    # Simple filter: just scheduled start date
    filters = [
        {
            "condition": "and",
            "filters": [
                {
                    "condition": "and",
                    "property": "ScheduledStartDate",
                    "operator": "gt",
                    "value": "[today]"
                }
            ]
        }
    ]
    
    print(f"  Filter: ScheduledStartDate gt [today]")
    response = requests.post(CHANGEREQUEST_VIEW_URL, headers=headers, json=filters)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        print(f"  ✓ CR view filter works!")
        
        save_json(data, "cr_view_filter_upcoming")
        
        for cr in results[:10]:
            crid = cr.get('id', 'N/A')
            title = cr.get('title', 'N/A')[:60]
            sched_start = cr.get('scheduledStartDate', 'N/A')
            sched_end = cr.get('scheduledEndDate', 'N/A')
            status = cr.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            
            print(f"  {crid} | Status: {status_name} | Start: {sched_start}")
            print(f"    Title: {title}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Test H: Object Query for upcoming CRs ────────────────────────────

def test_object_query_upcoming_crs(headers):
    """
    Use object query endpoint for upcoming change requests.
    """
    print("\n" + "=" * 70)
    print("TEST H: Object Query — Upcoming Change Requests")
    print("=" * 70)
    
    params = {
        'type': 'changerequest',
        '$filter': "ScheduledStartDate gt '4-13-2026' and ScheduledStartDate lt '4-15-2026'",
        '$orderby': 'ScheduledStartDate Asc',
        '$top': 20
    }
    
    print(f"  Filter: {params['$filter']}")
    
    response = requests.get(OBJECT_QUERY_URL, headers=headers, params=params)
    print(f"  Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        result_count = data.get('resultCount', 0)
        results = data.get('result', [])
        print(f"  Result count: {result_count}")
        print(f"  Results returned: {len(results)}")
        
        save_json(data, "cr_object_query_upcoming")
        
        for cr in results[:10]:
            crid = cr.get('id', 'N/A')
            title = cr.get('title', 'N/A')[:60]
            sched_start = cr.get('scheduledStartDate', 'N/A')
            sched_end = cr.get('scheduledEndDate', 'N/A')
            status = cr.get('status', {})
            status_name = status.get('name', 'N/A') if isinstance(status, dict) else str(status)
            downtime = cr.get('downtime', cr.get('isDowntime', 'N/A'))
            
            print(f"  {crid} | Status: {status_name} | Start: {sched_start} | End: {sched_end}")
            print(f"    Title: {title}")
            print(f"    Downtime: {downtime}")
        
        # Save first result for field analysis
        if results:
            save_json(results[0], "cr_object_query_sample")
            print(f"\n  Fields in CR result ({len(results[0].keys())} fields):")
            for key in sorted(results[0].keys()):
                val = results[0][key]
                val_str = str(val)[:80] if val is not None else 'null'
                print(f"    {key}: {val_str}")
        
        return data
    else:
        print(f"  FAILED: {response.text[:500]}")
        return None


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("ATHENA SEV TURNOVER EXPLORATION — ROUND 2")
    print("Testing alternative query approaches")
    print("=" * 70)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    summary = []
    
    # Test A: Object query for SEV incidents
    result_a = test_object_query_sev_incidents(headers)
    summary.append(f"TEST A (object query P1/P2): {'SUCCESS' if result_a else 'FAILED'} — {result_a.get('resultCount', 0) if result_a else 0} results")
    
    # Test B: Object query active SEVs
    result_b = test_object_query_active_sevs(headers)
    summary.append(f"TEST B (object query active SEVs): {'SUCCESS' if result_b else 'FAILED'}")
    
    # Test C: View filter with GUID
    result_c = test_view_filter_with_guids(headers)
    summary.append(f"TEST C (view filter GUID): {'SUCCESS' if result_c else 'FAILED'}")
    
    # Test D: View filter with name
    result_d = test_view_filter_with_name(headers)
    summary.append(f"TEST D (view filter name): {'SUCCESS' if result_d else 'FAILED'}")
    
    # Test E: View filter priority
    result_e = test_view_filter_priority(headers)
    summary.append(f"TEST E (view filter priority): {'SUCCESS' if result_e else 'FAILED'}")
    
    # Test F: Combined SEV filter
    result_f = test_view_filter_sev_combined(headers)
    summary.append(f"TEST F (combined SEV filter): {'SUCCESS' if result_f else 'FAILED'}")
    
    # Test G: CR view filter
    result_g = test_cr_view_filter(headers)
    summary.append(f"TEST G (CR view filter): {'SUCCESS' if result_g else 'FAILED'}")
    
    # Test H: Object query upcoming CRs
    result_h = test_object_query_upcoming_crs(headers)
    summary.append(f"TEST H (object query CRs): {'SUCCESS' if result_h else 'FAILED'}")
    
    # Print summary
    print("\n" + "=" * 70)
    print("EXPLORATION SUMMARY — ROUND 2")
    print("=" * 70)
    for line in summary:
        print(f"  {line}")
    
    print("\n" + "=" * 70)
    print("EXPLORATION COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()