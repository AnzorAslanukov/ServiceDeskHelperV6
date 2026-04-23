"""
Explore Athena Enum Values and GUIDs

This script fetches all available enumerated list items from the Athena API.
These enums include statuses, priorities, impacts, urgencies, classifications,
and other GUID-referenced fields used across ticket types.

Endpoint: GET /v1/enums/all
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def fetch_all_enums(headers):
    """Fetch all enumerated list items from cache."""
    url = f"{ATHENA_BASE_URL}v1/enums/all"
    print(f"Fetching all enums from: {url}")
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! Received data.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def fetch_enum_by_id(enum_id, headers, use_tree=False):
    """Fetch a specific enum by its ID."""
    endpoint = "tree" if use_tree else ""
    if use_tree:
        url = f"{ATHENA_BASE_URL}v1/enums/tree/{enum_id}"
    else:
        url = f"{ATHENA_BASE_URL}v1/enums/{enum_id}"
    
    print(f"\nFetching enum {enum_id} from: {url}")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! Received {len(data)} items.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def explore_ticket_metadata(headers):
    """
    Fetch metadata for incident and service request classes to discover
    all available fields and their enum type IDs.
    """
    results = {}
    
    for ticket_type in ['incident', 'servicerequest', 'changerequest']:
        url = f"{ATHENA_BASE_URL}v1/object/meta?type={ticket_type}"
        print(f"\nFetching metadata for '{ticket_type}' from: {url}")
        
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            results[ticket_type] = data
            print(f"  Success! Received metadata for {ticket_type}.")
        else:
            print(f"  Failed. Status: {response.status_code}")
            print(f"  Response: {response.text[:500]}")
    
    return results


def save_results(data, filename):
    """Save data to JSON file."""
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    json_path = os.path.join(output_dir, f"{filename}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved to: {json_path}")
    return json_path


def save_enum_summary(all_data, filename):
    """Save a human-readable summary of enum data."""
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    summary_path = os.path.join(output_dir, f"{filename}.txt")
    with open(summary_path, 'w', encoding='utf-8') as f:
        if isinstance(all_data, list):
            f.write(f"Total items: {len(all_data)}\n")
            f.write("=" * 80 + "\n\n")
            for item in all_data:
                if isinstance(item, dict):
                    # Handle tree-style items
                    if 'value' in item:
                        f.write(f"GUID: {item.get('value', 'N/A')}\n")
                        f.write(f"  Label: {item.get('label', 'N/A')}\n")
                        f.write(f"  Full Name: {item.get('fullname', 'N/A')}\n")
                        f.write(f"  Disabled: {item.get('disabled', 'N/A')}\n")
                        children = item.get('children', [])
                        f.write(f"  Children: {len(children)}\n\n")
                    # Handle flat-style items
                    elif 'id' in item:
                        f.write(f"GUID: {item.get('id', 'N/A')}\n")
                        f.write(f"  Name: {item.get('name', 'N/A')}\n\n")
                    else:
                        f.write(f"{json.dumps(item, indent=2)}\n\n")
                else:
                    f.write(f"{item}\n")
        elif isinstance(all_data, dict):
            f.write(json.dumps(all_data, indent=2, ensure_ascii=False))
    
    print(f"  Summary saved to: {summary_path}")


# Known enum IDs to explore (discovered from documentation and ticket responses)
KNOWN_ENUM_IDS = {
    'ir_support_groups': 'c3264527-a501-029f-6872-31300080b3bf',
    'sr_support_groups': '23c243f6-9365-d46f-dff2-03826e24d228',
}


def main():
    print("=" * 60)
    print("ATHENA ENUM & GUID EXPLORER")
    print("=" * 60)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    # 1. Fetch all enums
    print("\n" + "=" * 60)
    print("STEP 1: Fetch All Enums")
    print("=" * 60)
    all_enums = fetch_all_enums(headers)
    if all_enums:
        save_results(all_enums, "all_enums")
        save_enum_summary(all_enums, "all_enums_summary")
    
    # 2. Fetch ticket type metadata
    print("\n" + "=" * 60)
    print("STEP 2: Fetch Ticket Type Metadata")
    print("=" * 60)
    metadata = explore_ticket_metadata(headers)
    if metadata:
        for ticket_type, data in metadata.items():
            save_results(data, f"metadata_{ticket_type}")
    
    # 3. Try to discover additional enum IDs from metadata
    print("\n" + "=" * 60)
    print("STEP 3: Discover Enum IDs from Metadata")
    print("=" * 60)
    
    discovered_enums = {}
    if metadata:
        for ticket_type, data in metadata.items():
            print(f"\n  Scanning {ticket_type} metadata for enum references...")
            if isinstance(data, dict):
                scan_for_enum_ids(data, ticket_type, discovered_enums)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        scan_for_enum_ids(item, ticket_type, discovered_enums)
    
    if discovered_enums:
        save_results(discovered_enums, "discovered_enum_ids")
        print(f"\n  Discovered {len(discovered_enums)} potential enum references.")
        
        # Try fetching each discovered enum
        print("\n" + "=" * 60)
        print("STEP 4: Fetch Discovered Enums")
        print("=" * 60)
        
        for name, enum_info in discovered_enums.items():
            enum_id = enum_info.get('id', '')
            if enum_id and enum_id != '00000000-0000-0000-0000-000000000000':
                data = fetch_enum_by_id(enum_id, headers)
                if data:
                    safe_name = name.replace('/', '_').replace('\\', '_')
                    save_results(data, f"enum_{safe_name}")
    
    print("\n" + "=" * 60)
    print("EXPLORATION COMPLETE")
    print("=" * 60)


def scan_for_enum_ids(data, context, results, path=""):
    """Recursively scan metadata for GUID-like values that might be enum IDs."""
    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key
            if isinstance(value, str) and is_guid(value) and value != '00000000-0000-0000-0000-000000000000':
                results[current_path] = {
                    'id': value,
                    'context': context,
                    'path': current_path
                }
            elif isinstance(value, (dict, list)):
                scan_for_enum_ids(value, context, results, current_path)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            scan_for_enum_ids(item, context, results, f"{path}[{i}]")


def is_guid(s):
    """Check if a string looks like a GUID."""
    import re
    return bool(re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', s, re.IGNORECASE))


if __name__ == '__main__':
    main()