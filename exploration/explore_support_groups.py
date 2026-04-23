"""
Explore Athena Support Group Structure and GUIDs

This script fetches the support group enum trees for both Incident (IR) and 
Service Request (SR) ticket types. The support group GUIDs are critical for 
ticket assignment — each ticket must be routed to the correct support group.

The two enum tree endpoints are:
  - IR Support Groups: /v1/enums/tree/c3264527-a501-029f-6872-31300080b3bf
  - SR Support Groups: /v1/enums/tree/23c243f6-9365-d46f-dff2-03826e24d228
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

IR_SUPPORT_GROUP_GUID = os.getenv('ATHENA_IR_SUPPORT_GROUP_GUID')
SR_SUPPORT_GROUP_GUID = os.getenv('ATHENA_SR_SUPPORT_GROUP_GUID')


def fetch_support_groups(url, headers):
    """Fetch support group tree from the given enum URL."""
    print(f"\nFetching support groups from: {url}")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! Received {len(data)} top-level groups.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def flatten_tree(nodes, parent_path="", results=None):
    """
    Recursively flatten the support group tree into a list of dicts.
    Each entry contains: guid, label, fullname, disabled, depth, parent_path
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


def print_tree(nodes, indent=0):
    """Print the support group tree in a readable format."""
    for node in nodes:
        prefix = "  " * indent
        disabled_marker = " [DISABLED]" if node.get('disabled', False) else ""
        guid = node.get('value', 'N/A')
        label = node.get('label', 'N/A')
        print(f"{prefix}+-- {label}{disabled_marker}")
        print(f"{prefix}|   GUID: {guid}")
        
        children = node.get('children', [])
        if children:
            print_tree(children, indent + 1)


def save_results(data, filename, label):
    """Save raw JSON and flattened CSV-like data to files."""
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
        f.write(f"{'GUID':<40} {'Disabled':<10} {'Depth':<6} {'Full Name'}\n")
        f.write("=" * 120 + "\n")
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
    
    print(f"\n  === {label} Summary ===")
    print(f"  Total groups: {total}")
    print(f"  Active (assignable): {active}")
    print(f"  Disabled: {disabled}")
    print(f"  Leaf nodes (no children): {leaf_nodes}")
    print(f"  Max depth: {max_depth}")
    
    return flat


def compare_ir_sr(ir_flat, sr_flat):
    """Compare IR and SR support groups to find differences."""
    ir_guids = {item['guid'] for item in ir_flat}
    sr_guids = {item['guid'] for item in sr_flat}
    
    ir_only = ir_guids - sr_guids
    sr_only = sr_guids - ir_guids
    shared = ir_guids & sr_guids
    
    print(f"\n{'='*60}")
    print(f"COMPARISON: IR vs SR Support Groups")
    print(f"{'='*60}")
    print(f"  Shared groups: {len(shared)}")
    print(f"  IR-only groups: {len(ir_only)}")
    print(f"  SR-only groups: {len(sr_only)}")
    
    if ir_only:
        print(f"\n  --- Groups ONLY in IR ---")
        ir_lookup = {item['guid']: item for item in ir_flat}
        for guid in sorted(ir_only):
            item = ir_lookup[guid]
            print(f"    {guid}  {item['fullname']}")
    
    if sr_only:
        print(f"\n  --- Groups ONLY in SR ---")
        sr_lookup = {item['guid']: item for item in sr_flat}
        for guid in sorted(sr_only):
            item = sr_lookup[guid]
            print(f"    {guid}  {item['fullname']}")
    
    # Save comparison
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    comp_path = os.path.join(output_dir, "support_group_comparison.txt")
    with open(comp_path, 'w', encoding='utf-8') as f:
        f.write("IR vs SR Support Group Comparison\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Shared groups: {len(shared)}\n")
        f.write(f"IR-only groups: {len(ir_only)}\n")
        f.write(f"SR-only groups: {len(sr_only)}\n\n")
        
        if ir_only:
            f.write("--- Groups ONLY in IR ---\n")
            ir_lookup = {item['guid']: item for item in ir_flat}
            for guid in sorted(ir_only):
                item = ir_lookup[guid]
                f.write(f"  {guid}  {item['fullname']}\n")
            f.write("\n")
        
        if sr_only:
            f.write("--- Groups ONLY in SR ---\n")
            sr_lookup = {item['guid']: item for item in sr_flat}
            for guid in sorted(sr_only):
                item = sr_lookup[guid]
                f.write(f"  {guid}  {item['fullname']}\n")
        
        f.write("\n--- Shared Groups ---\n")
        ir_lookup = {item['guid']: item for item in ir_flat}
        for guid in sorted(shared):
            item = ir_lookup[guid]
            f.write(f"  {guid}  {item['fullname']}\n")
    
    print(f"\n  Comparison saved to: {comp_path}")


def main():
    print("=" * 60)
    print("ATHENA SUPPORT GROUP EXPLORER")
    print("=" * 60)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    # Fetch IR Support Groups
    print("\n" + "=" * 60)
    print("INCIDENT (IR) SUPPORT GROUPS")
    print("=" * 60)
    ir_data = fetch_support_groups(IR_SUPPORT_GROUP_GUID, headers)
    ir_flat = None
    if ir_data:
        print("\nTree structure:")
        print_tree(ir_data)
        ir_flat = save_results(ir_data, "ir_support_groups", "IR Support Groups")
    
    # Fetch SR Support Groups
    print("\n" + "=" * 60)
    print("SERVICE REQUEST (SR) SUPPORT GROUPS")
    print("=" * 60)
    sr_data = fetch_support_groups(SR_SUPPORT_GROUP_GUID, headers)
    sr_flat = None
    if sr_data:
        print("\nTree structure:")
        print_tree(sr_data)
        sr_flat = save_results(sr_data, "sr_support_groups", "SR Support Groups")
    
    # Compare IR vs SR
    if ir_flat and sr_flat:
        compare_ir_sr(ir_flat, sr_flat)
    
    print("\n" + "=" * 60)
    print("EXPLORATION COMPLETE")
    print("=" * 60)


if __name__ == '__main__':
    main()