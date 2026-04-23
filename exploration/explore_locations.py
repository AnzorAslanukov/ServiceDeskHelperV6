"""
Explore Athena Location and Floor Enum Values

This script fetches the Location and Floor enum trees from the Athena API.
Location data is important for ticket context — tickets reference physical
locations within Penn Medicine facilities.

Enum IDs (from skill.md):
  - Location: 31595f15-44d1-58cf-f5b4-03d0f1b1921b
  - Floor:    bf0bc17f-9091-92bd-912f-6284eb05947c
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

LOCATION_ENUM_ID = '31595f15-44d1-58cf-f5b4-03d0f1b1921b'
FLOOR_ENUM_ID = 'bf0bc17f-9091-92bd-912f-6284eb05947c'


def fetch_enum_tree(enum_id, headers, label=""):
    """Fetch an enum tree by its ID."""
    url = f"{ATHENA_BASE_URL}v1/enums/tree/{enum_id}"
    print(f"\nFetching {label} enum tree from: {url}")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! Received {len(data)} top-level items.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def fetch_enum_flat(enum_id, headers, label=""):
    """Fetch an enum as a flat list by its ID."""
    url = f"{ATHENA_BASE_URL}v1/enums/{enum_id}"
    print(f"\nFetching {label} enum (flat) from: {url}")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! Received {len(data)} items.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def flatten_tree(nodes, parent_path="", results=None):
    """
    Recursively flatten an enum tree into a list of dicts.
    Each entry contains: guid, label, fullname, disabled, depth, parent_path, has_children
    """
    if results is None:
        results = []
    
    for node in nodes:
        depth = parent_path.count("\\") + 1 if parent_path else 0
        results.append({
            'guid': node.get('value', ''),
            'label': node.get('label', ''),
            'fullname': node.get('fullname', ''),
            'disabled': node.get('disabled', False),
            'depth': depth,
            'parent_path': parent_path,
            'has_children': len(node.get('children', [])) > 0
        })
        
        children = node.get('children', [])
        if children:
            child_path = node.get('fullname', '')
            flatten_tree(children, child_path, results)
    
    return results


def print_tree(nodes, indent=0, max_depth=3):
    """Print the enum tree in a readable format, limited to max_depth."""
    for node in nodes:
        prefix = "  " * indent
        disabled_marker = " [DISABLED]" if node.get('disabled', False) else ""
        guid = node.get('value', 'N/A')
        label = node.get('label', 'N/A')
        children = node.get('children', [])
        child_count = f" ({len(children)} children)" if children else ""
        print(f"{prefix}+-- {label}{disabled_marker}{child_count}")
        print(f"{prefix}|   GUID: {guid}")
        
        if children and indent < max_depth:
            print_tree(children, indent + 1, max_depth)
        elif children and indent >= max_depth:
            print(f"{prefix}|   ... ({len(children)} children not shown)")


def save_results(data, filename, label):
    """Save raw JSON and flattened text data to files."""
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    # Save raw JSON
    json_path = os.path.join(output_dir, f"{filename}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\n  Raw JSON saved to: {json_path}")
    
    # Save flattened list
    flat = flatten_tree(data)
    flat_path = os.path.join(output_dir, f"{filename}_flat.txt")
    with open(flat_path, 'w', encoding='utf-8') as f:
        f.write(f"{label}\n")
        f.write(f"{'='*120}\n\n")
        f.write(f"{'GUID':<40} {'Disabled':<10} {'Depth':<6} {'Full Name'}\n")
        f.write("-" * 120 + "\n")
        for item in flat:
            disabled = "YES" if item['disabled'] else "no"
            f.write(f"{item['guid']:<40} {disabled:<10} {item['depth']:<6} {item['fullname']}\n")
    print(f"  Flattened list saved to: {flat_path}")
    
    # Print summary stats
    total = len(flat)
    active = sum(1 for item in flat if not item['disabled'])
    disabled = sum(1 for item in flat if item['disabled'])
    leaf_nodes = sum(1 for item in flat if not item['has_children'])
    max_depth = max(item['depth'] for item in flat) if flat else 0
    top_level = [item for item in flat if item['depth'] == 0]
    
    print(f"\n  === {label} Summary ===")
    print(f"  Total items: {total}")
    print(f"  Active: {active}")
    print(f"  Disabled: {disabled}")
    print(f"  Leaf nodes (no children): {leaf_nodes}")
    print(f"  Max depth: {max_depth}")
    print(f"  Top-level items: {len(top_level)}")
    
    # Print top-level items
    print(f"\n  --- Top-Level {label} ---")
    for item in top_level:
        disabled_marker = " [DISABLED]" if item['disabled'] else ""
        print(f"    {item['guid']}  {item['label']}{disabled_marker}")
    
    return flat


def main():
    print("=" * 60)
    print("ATHENA LOCATION & FLOOR EXPLORER")
    print("=" * 60)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    # ===== LOCATIONS =====
    print("\n" + "=" * 60)
    print("LOCATIONS (Tree)")
    print("=" * 60)
    location_tree = fetch_enum_tree(LOCATION_ENUM_ID, headers, "Location")
    location_flat = None
    if location_tree:
        print("\nTree structure (max depth 3):")
        print_tree(location_tree, max_depth=3)
        location_flat = save_results(location_tree, "locations", "Locations")
    
    # Also try flat endpoint
    print("\n" + "=" * 60)
    print("LOCATIONS (Flat)")
    print("=" * 60)
    location_flat_data = fetch_enum_flat(LOCATION_ENUM_ID, headers, "Location")
    if location_flat_data:
        output_dir = os.path.join(os.path.dirname(__file__), 'output')
        flat_path = os.path.join(output_dir, "locations_flat_api.json")
        with open(flat_path, 'w', encoding='utf-8') as f:
            json.dump(location_flat_data, f, indent=2, ensure_ascii=False)
        print(f"  Flat API response saved to: {flat_path}")
    
    # ===== FLOORS =====
    print("\n" + "=" * 60)
    print("FLOORS (Tree)")
    print("=" * 60)
    floor_tree = fetch_enum_tree(FLOOR_ENUM_ID, headers, "Floor")
    floor_flat = None
    if floor_tree:
        print("\nTree structure (max depth 3):")
        print_tree(floor_tree, max_depth=3)
        floor_flat = save_results(floor_tree, "floors", "Floors")
    
    # Also try flat endpoint
    print("\n" + "=" * 60)
    print("FLOORS (Flat)")
    print("=" * 60)
    floor_flat_data = fetch_enum_flat(FLOOR_ENUM_ID, headers, "Floor")
    if floor_flat_data:
        output_dir = os.path.join(os.path.dirname(__file__), 'output')
        flat_path = os.path.join(output_dir, "floors_flat_api.json")
        with open(flat_path, 'w', encoding='utf-8') as f:
            json.dump(floor_flat_data, f, indent=2, ensure_ascii=False)
        print(f"  Flat API response saved to: {flat_path}")
    
    print("\n" + "=" * 60)
    print("EXPLORATION COMPLETE")
    print("=" * 60)


if __name__ == '__main__':
    main()