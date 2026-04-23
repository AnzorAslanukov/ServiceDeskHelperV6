"""
Explore Sample Athena Tickets

This script fetches sample incident and service request tickets to discover
the actual field values, GUIDs, and structure used in production tickets.
This helps identify real status GUIDs, support group GUIDs, priority values,
and other enum values in use.
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
SERVICEREQUEST_VIEW_URL = os.getenv('ATHENA_SERVICEREQUEST_VIEW_URL')
INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
SERVICEREQUEST_URL = os.getenv('ATHENA_SERVICEREQUEST_URL')


def fetch_recent_incidents(headers, limit=5):
    """Fetch recent incidents using the object query endpoint."""
    url = f"{ATHENA_BASE_URL}v1/object/query"
    params = {
        'type': 'incident',
        '$filter': "CreatedDate gt '1-1-2025'",
        '$orderby': 'CreatedDate Desc',
        '$top': limit
    }
    
    print(f"\nFetching {limit} recent incidents...")
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        count = data.get('resultCount', 0)
        print(f"  Success! Got {count} incidents.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def fetch_recent_service_requests(headers, limit=5):
    """Fetch recent service requests using the object query endpoint."""
    url = f"{ATHENA_BASE_URL}v1/object/query"
    params = {
        'type': 'servicerequest',
        '$filter': "CreatedDate gt '1-1-2025'",
        '$orderby': 'CreatedDate Desc',
        '$top': limit
    }
    
    print(f"\nFetching {limit} recent service requests...")
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        count = data.get('resultCount', 0)
        print(f"  Success! Got {count} service requests.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def fetch_ticket_detail(ticket_id, headers, type_proj='incidentform'):
    """Fetch full detail of a single ticket by its ID."""
    # Try using the incident endpoint first
    url = f"{ATHENA_BASE_URL}v1/incident/{ticket_id}"
    
    print(f"\n  Fetching detail for {ticket_id}...")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        # Try service request endpoint
        url = f"{ATHENA_BASE_URL}v1/servicerequest/{ticket_id}"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        
        print(f"    Failed. Status: {response.status_code}")
        return None


def extract_guid_fields(ticket, field_guids=None):
    """Extract all GUID-referenced fields from a ticket for analysis."""
    if field_guids is None:
        field_guids = {}
    
    if not isinstance(ticket, dict):
        return field_guids
    
    for key, value in ticket.items():
        if isinstance(value, dict):
            # Check if this is an enum-style field (has 'id' and 'name')
            if 'id' in value and 'name' in value:
                if key not in field_guids:
                    field_guids[key] = []
                entry = {'id': value['id'], 'name': value['name']}
                if entry not in field_guids[key]:
                    field_guids[key].append(entry)
            # Check for user-style fields
            elif 'entityId' in value and 'displayName' in value:
                if key not in field_guids:
                    field_guids[key] = []
                entry = {
                    'entityId': value.get('entityId', ''),
                    'displayName': value.get('displayName', ''),
                    'userName': value.get('userName', '')
                }
                if entry not in field_guids[key]:
                    field_guids[key].append(entry)
            else:
                extract_guid_fields(value, field_guids)
    
    return field_guids


def save_results(data, filename):
    """Save data to JSON file."""
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    json_path = os.path.join(output_dir, f"{filename}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved to: {json_path}")
    return json_path


def main():
    print("=" * 60)
    print("ATHENA SAMPLE TICKET EXPLORER")
    print("=" * 60)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    all_guid_fields = {}
    
    # 1. Fetch recent incidents
    print("\n" + "=" * 60)
    print("STEP 1: Recent Incidents")
    print("=" * 60)
    incidents = fetch_recent_incidents(headers, limit=10)
    if incidents:
        save_results(incidents, "sample_incidents")
        
        # Extract GUID fields from each incident
        results = incidents.get('result', [])
        for ticket in results:
            extract_guid_fields(ticket, all_guid_fields)
            ticket_id = ticket.get('id', 'unknown')
            print(f"  Processed: {ticket_id} - {ticket.get('title', 'N/A')[:60]}")
    
    # 2. Fetch a detailed incident (if we got any)
    if incidents and incidents.get('result'):
        print("\n" + "=" * 60)
        print("STEP 2: Detailed Incident")
        print("=" * 60)
        first_ticket = incidents['result'][0]
        entity_id = first_ticket.get('entityId', '')
        ticket_id = first_ticket.get('id', '')
        
        if ticket_id:
            detail = fetch_ticket_detail(ticket_id, headers)
            if detail:
                save_results(detail, "sample_incident_detail")
                extract_guid_fields(detail, all_guid_fields)
    
    # 3. Fetch recent service requests
    print("\n" + "=" * 60)
    print("STEP 3: Recent Service Requests")
    print("=" * 60)
    service_requests = fetch_recent_service_requests(headers, limit=10)
    if service_requests:
        save_results(service_requests, "sample_service_requests")
        
        results = service_requests.get('result', [])
        for ticket in results:
            extract_guid_fields(ticket, all_guid_fields)
            ticket_id = ticket.get('id', 'unknown')
            print(f"  Processed: {ticket_id} - {ticket.get('title', 'N/A')[:60]}")
    
    # 4. Save discovered GUID fields
    print("\n" + "=" * 60)
    print("STEP 4: GUID Field Summary")
    print("=" * 60)
    
    if all_guid_fields:
        save_results(all_guid_fields, "discovered_guid_fields")
        
        # Print summary
        output_dir = os.path.join(os.path.dirname(__file__), 'output')
        summary_path = os.path.join(output_dir, "guid_field_summary.txt")
        
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write("Athena Ticket GUID Field Summary\n")
            f.write("Discovered from sample tickets\n")
            f.write("=" * 80 + "\n\n")
            
            for field_name, values in sorted(all_guid_fields.items()):
                f.write(f"\n--- {field_name} ---\n")
                print(f"\n  {field_name}:")
                for val in values:
                    if 'id' in val and 'name' in val:
                        line = f"    GUID: {val['id']}  Name: {val['name']}"
                        print(line)
                        f.write(f"  GUID: {val['id']:<40}  Name: {val['name']}\n")
                    elif 'entityId' in val:
                        line = f"    EntityId: {val['entityId']}  User: {val.get('displayName', 'N/A')}"
                        print(line)
                        f.write(f"  EntityId: {val['entityId']:<40}  User: {val.get('displayName', 'N/A')} ({val.get('userName', 'N/A')})\n")
        
        print(f"\n  Summary saved to: {summary_path}")
    
    print("\n" + "=" * 60)
    print("EXPLORATION COMPLETE")
    print("=" * 60)


if __name__ == '__main__':
    main()