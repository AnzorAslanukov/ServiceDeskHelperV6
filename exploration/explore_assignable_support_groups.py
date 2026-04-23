"""
Explore Athena Support Group Assignability

GOAL: Determine which support groups are actually assignable to tickets
vs. which are parent/category groups that cannot be assigned.

The support group enum tree has a 'disabled' field on each node. This script:
1. Fetches the IR and SR support group trees
2. Categorizes groups by their 'disabled' flag and whether they have children
3. Empirically tests assigning the test ticket (IR10377668) to:
   a. A disabled parent group (e.g., "Applications") — expect failure or silent ignore
   b. An enabled leaf group (e.g., "Service Desk") — expect success
   c. A disabled leaf group (e.g., "Service Desk\\Validation") — to see behavior
4. Saves a comprehensive report of assignable vs non-assignable groups

Test ticket: IR10377668 (entityId: acdc128a-e800-a420-9951-a6d6762c1464)
SAFETY: Only changes tierQueue, never sets priority to 1 or 2. Always restores to Service Desk at end.

Usage:
    python exploration/explore_assignable_support_groups.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from athena_auth import get_auth_headers, ATHENA_BASE_URL

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

INCIDENT_URL = os.getenv('ATHENA_INCIDENT_URL')
IR_SUPPORT_GROUP_ENUM_URL = os.getenv('ATHENA_IR_SUPPORT_GROUP_GUID')
SR_SUPPORT_GROUP_ENUM_URL = os.getenv('ATHENA_SR_SUPPORT_GROUP_GUID')

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_TICKET_ID = "IR10377668"
TEST_ENTITY_ID = "acdc128a-e800-a420-9951-a6d6762c1464"

# Known GUIDs for testing
SERVICE_DESK_IR_GUID = "ec749166-07c5-eba6-35ba-bd32fa8ed7d2"  # enabled, depth 0
APPLICATIONS_IR_GUID = "4f8bfaca-a980-27c5-11e7-957b0bbff24b"  # DISABLED, depth 0, has children
PENNCHART_IR_GUID = "ab139906-1ce2-7115-9124-5bc600369550"     # DISABLED, depth 0, has children
EUS_IR_GUID = "ae9eb3ff-458a-206f-7815-129d50efa285"           # DISABLED, depth 0, has children
VALIDATION_IR_GUID = "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"    # DISABLED, depth 1, leaf (no children)
PENNCHART_ED_IR_GUID = "72bac846-40cc-8c58-c749-30e2a59cdde5"  # enabled, depth 1, leaf
IS_OPS_IR_GUID = "f6f5d0e0-d01d-8f10-0bfe-73a7efd77315"       # DISABLED, depth 0, has children
TECH_INFRA_IR_GUID = "17326dc5-8e2f-bc10-1085-85bb81fee7db"    # DISABLED, depth 0, has children


def save_json(data, filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved to {filepath}")


def flatten_tree(nodes, parent_path="", results=None):
    """Recursively flatten the support group tree."""
    if results is None:
        results = []
    for node in nodes:
        depth = parent_path.count("\\") + 1 if parent_path else 0
        children = node.get('children', [])
        results.append({
            'guid': node.get('value', ''),
            'label': node.get('label', ''),
            'fullname': node.get('fullname', ''),
            'disabled': node.get('disabled', False),
            'depth': depth,
            'parent_path': parent_path,
            'has_children': len(children) > 0,
            'child_count': len(children),
        })
        if children:
            child_path = node.get('fullname', '')
            flatten_tree(children, child_path, results)
    return results


def fetch_support_groups(url, headers):
    """Fetch support group tree from the given enum URL."""
    print(f"\n  Fetching from: {url}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        print(f"  Success! {len(data)} top-level groups.")
        return data
    else:
        print(f"  Failed. Status: {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        return None


def analyze_groups(flat_list, label):
    """Analyze and categorize support groups."""
    total = len(flat_list)
    disabled = [g for g in flat_list if g['disabled']]
    enabled = [g for g in flat_list if not g['disabled']]
    
    disabled_with_children = [g for g in disabled if g['has_children']]
    disabled_leaf = [g for g in disabled if not g['has_children']]
    enabled_with_children = [g for g in enabled if g['has_children']]
    enabled_leaf = [g for g in enabled if not g['has_children']]
    
    print(f"\n  === {label} Analysis ===")
    print(f"  Total groups: {total}")
    print(f"  Enabled (assignable): {len(enabled)}")
    print(f"    - Enabled with children: {len(enabled_with_children)}")
    print(f"    - Enabled leaf (no children): {len(enabled_leaf)}")
    print(f"  Disabled (non-assignable): {len(disabled)}")
    print(f"    - Disabled with children (parent categories): {len(disabled_with_children)}")
    print(f"    - Disabled leaf (no children): {len(disabled_leaf)}")
    
    print(f"\n  --- Disabled Groups (NON-ASSIGNABLE) ---")
    for g in disabled:
        children_note = f" [{g['child_count']} children]" if g['has_children'] else " [LEAF]"
        print(f"    [DISABLED] {g['fullname']}{children_note}")
    
    print(f"\n  --- Enabled Groups WITH Children ---")
    for g in enabled_with_children:
        print(f"    [ENABLED+PARENT] {g['fullname']} [{g['child_count']} children]")
    
    return {
        'total': total,
        'enabled': len(enabled),
        'disabled': len(disabled),
        'disabled_with_children': len(disabled_with_children),
        'disabled_leaf': len(disabled_leaf),
        'enabled_with_children': len(enabled_with_children),
        'enabled_leaf': len(enabled_leaf),
        'disabled_groups': [{'fullname': g['fullname'], 'guid': g['guid'], 'has_children': g['has_children'], 'depth': g['depth']} for g in disabled],
        'enabled_with_children_groups': [{'fullname': g['fullname'], 'guid': g['guid'], 'child_count': g['child_count'], 'depth': g['depth']} for g in enabled_with_children],
    }


def get_ticket(headers):
    """GET the test ticket to check current state."""
    url = f"{INCIDENT_URL}{TEST_TICKET_ID}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        tq = data.get('tierQueue')
        return data
    else:
        print(f"  GET failed: {response.status_code}")
        return None


def test_assign_to_group(headers, label, guid, group_name):
    """
    Try to assign the test ticket to a specific support group via PUT.
    Returns a result dict with the outcome.
    """
    print(f"\n  --- Test: {label} ---")
    print(f"  Group: {group_name} (GUID: {guid})")
    
    payload = {
        "entityId": TEST_ENTITY_ID,
        "tierQueue": {
            "id": guid,
        },
    }
    
    url = f"{INCIDENT_URL}"
    response = requests.put(url, headers=headers, json=payload)
    
    result = {
        "label": label,
        "group_name": group_name,
        "guid": guid,
        "put_status": response.status_code,
    }
    
    if response.status_code == 200:
        put_data = response.json()
        put_tq = put_data.get('tierQueue')
        result["put_response_tierQueue"] = put_tq
        
        # Verify with GET
        get_data = get_ticket(headers)
        if get_data:
            get_tq = get_data.get('tierQueue')
            result["get_verify_tierQueue"] = get_tq
            
            # Check if the assignment actually took effect
            if isinstance(get_tq, dict):
                actual_guid = get_tq.get('id', get_tq.get('value', ''))
                actual_name = get_tq.get('name', get_tq.get('displayName', ''))
            else:
                actual_guid = str(get_tq) if get_tq else ''
                actual_name = ''
            
            result["actual_guid"] = actual_guid
            result["actual_name"] = actual_name
            result["assignment_took_effect"] = (actual_guid == guid)
            
            if actual_guid == guid:
                print(f"  [OK] PUT 200 -- Assignment TOOK EFFECT -> {actual_name} ({actual_guid})")
            else:
                print(f"  [FAIL] PUT 200 -- Assignment SILENTLY IGNORED. Still at: {actual_name} ({actual_guid})")
    else:
        result["error"] = response.text[:1000]
        result["assignment_took_effect"] = False
        print(f"  [FAIL] PUT {response.status_code} -- Assignment REJECTED")
        print(f"    Error: {response.text[:300]}")
    
    return result


def main():
    print("=" * 70)
    print("  ATHENA SUPPORT GROUP ASSIGNABILITY EXPLORATION")
    print("=" * 70)
    
    headers = get_auth_headers()
    if not headers:
        print("ERROR: Could not authenticate. Exiting.")
        return
    
    # ── Phase 1: Analyze the enum tree structure ──────────────────────
    print("\n" + "=" * 70)
    print("  PHASE 1: Analyze Support Group Enum Trees")
    print("=" * 70)
    
    ir_data = fetch_support_groups(IR_SUPPORT_GROUP_ENUM_URL, headers)
    sr_data = fetch_support_groups(SR_SUPPORT_GROUP_ENUM_URL, headers)
    
    ir_flat = flatten_tree(ir_data) if ir_data else []
    sr_flat = flatten_tree(sr_data) if sr_data else []
    
    ir_analysis = analyze_groups(ir_flat, "IR Support Groups") if ir_flat else {}
    sr_analysis = analyze_groups(sr_flat, "SR Support Groups") if sr_flat else {}
    
    # ── Phase 2: Empirical testing ────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PHASE 2: Empirical Assignment Tests")
    print(f"  Test Ticket: {TEST_TICKET_ID} (entityId: {TEST_ENTITY_ID})")
    print("=" * 70)
    
    # First, ensure ticket is at Service Desk (known good state)
    print("\n  Setting ticket to Service Desk (baseline)...")
    test_assign_to_group(headers, "BASELINE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    test_results = []
    
    # Test 1: Assign to a DISABLED parent group (Applications — has children)
    r1 = test_assign_to_group(
        headers,
        "DISABLED parent with children",
        APPLICATIONS_IR_GUID,
        "Applications"
    )
    test_results.append(r1)
    
    # Restore to Service Desk
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 2: Assign to a DISABLED parent group (PennChart — has children)
    r2 = test_assign_to_group(
        headers,
        "DISABLED parent with children",
        PENNCHART_IR_GUID,
        "PennChart"
    )
    test_results.append(r2)
    
    # Restore
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 3: Assign to a DISABLED parent group (EUS — has children)
    r3 = test_assign_to_group(
        headers,
        "DISABLED parent with children",
        EUS_IR_GUID,
        "EUS"
    )
    test_results.append(r3)
    
    # Restore
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 4: Assign to a DISABLED leaf group (Validation — no children but disabled)
    r4 = test_assign_to_group(
        headers,
        "DISABLED leaf (no children)",
        VALIDATION_IR_GUID,
        "Service Desk\\Validation"
    )
    test_results.append(r4)
    
    # Restore
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 5: Assign to an ENABLED leaf group (PennChart\ED)
    r5 = test_assign_to_group(
        headers,
        "ENABLED leaf (no children)",
        PENNCHART_ED_IR_GUID,
        "PennChart\\ED"
    )
    test_results.append(r5)
    
    # Restore
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 6: Assign to a DISABLED parent (IS Operations — has children)
    r6 = test_assign_to_group(
        headers,
        "DISABLED parent with children",
        IS_OPS_IR_GUID,
        "IS Operations"
    )
    test_results.append(r6)
    
    # Restore
    test_assign_to_group(headers, "RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # Test 7: Assign to a DISABLED parent (Technology\Infrastructure — has children)
    r7 = test_assign_to_group(
        headers,
        "DISABLED parent with children",
        TECH_INFRA_IR_GUID,
        "Technology\\Infrastructure"
    )
    test_results.append(r7)
    
    # Final restore
    print("\n  --- Final Restore to Service Desk ---")
    test_assign_to_group(headers, "FINAL RESTORE", SERVICE_DESK_IR_GUID, "Service Desk")
    
    # ── Phase 3: Summary ──────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PHASE 3: Summary of Empirical Tests")
    print("=" * 70)
    
    for r in test_results:
        took_effect = r.get('assignment_took_effect', False)
        status = "ASSIGNABLE" if took_effect else "NOT ASSIGNABLE"
        symbol = "OK" if took_effect else "FAIL"
        print(f"  [{symbol}] {r['group_name']:<45} -> {status}")
        print(f"       Type: {r['label']}, PUT status: {r['put_status']}")
    
    # ── Save comprehensive report ─────────────────────────────────────
    report = {
        "ir_analysis": ir_analysis,
        "sr_analysis": sr_analysis,
        "empirical_tests": test_results,
        "conclusion": {
            "disabled_field_meaning": "The 'disabled' field in the enum tree indicates whether a support group is selectable/assignable. disabled=true means the group CANNOT be assigned to tickets.",
            "assignable_groups": "Only groups with disabled=false are assignable to tickets.",
            "parent_categories": "Groups with disabled=true AND children are organizational categories (e.g., Applications, PennChart, EUS).",
            "disabled_leaves": "Some groups are disabled=true but have no children (e.g., Validation) — these may be special queues.",
        }
    }
    
    save_json(report, "support_group_assignability_report.json")
    
    # Save a clean list of assignable groups for both IR and SR
    ir_assignable = [
        {'fullname': g['fullname'], 'guid': g['guid'], 'depth': g['depth'], 'has_children': g['has_children']}
        for g in ir_flat if not g['disabled']
    ]
    sr_assignable = [
        {'fullname': g['fullname'], 'guid': g['guid'], 'depth': g['depth'], 'has_children': g['has_children']}
        for g in sr_flat if not g['disabled']
    ]
    
    save_json({'ir_assignable': ir_assignable, 'sr_assignable': sr_assignable}, "assignable_support_groups.json")
    
    # Save human-readable report
    report_path = os.path.join(OUTPUT_DIR, "support_group_assignability_report.txt")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("ATHENA SUPPORT GROUP ASSIGNABILITY REPORT\n")
        f.write("=" * 70 + "\n\n")
        
        f.write("FINDING: The 'disabled' field in the support group enum tree determines\n")
        f.write("whether a group can be assigned to tickets.\n")
        f.write("  - disabled=false → ASSIGNABLE (can be set as tierQueue on tickets)\n")
        f.write("  - disabled=true  → NOT ASSIGNABLE (parent category or special queue)\n\n")
        
        f.write("=" * 70 + "\n")
        f.write("IR SUPPORT GROUPS\n")
        f.write("=" * 70 + "\n")
        f.write(f"Total: {len(ir_flat)}\n")
        f.write(f"Assignable (disabled=false): {len(ir_assignable)}\n")
        f.write(f"Non-assignable (disabled=true): {len(ir_flat) - len(ir_assignable)}\n\n")
        
        f.write("--- NON-ASSIGNABLE IR Groups (disabled=true) ---\n")
        for g in ir_flat:
            if g['disabled']:
                children_note = f" [{g['child_count']} children]" if g['has_children'] else " [LEAF]"
                f.write(f"  {g['guid']}  {g['fullname']}{children_note}\n")
        
        f.write(f"\n--- ASSIGNABLE IR Groups (disabled=false) --- ({len(ir_assignable)} groups)\n")
        for g in ir_assignable:
            children_note = " [HAS CHILDREN]" if g['has_children'] else ""
            indent = "  " * (g['depth'] + 1)
            f.write(f"{indent}{g['guid']}  {g['fullname']}{children_note}\n")
        
        f.write("\n" + "=" * 70 + "\n")
        f.write("SR SUPPORT GROUPS\n")
        f.write("=" * 70 + "\n")
        f.write(f"Total: {len(sr_flat)}\n")
        f.write(f"Assignable (disabled=false): {len(sr_assignable)}\n")
        f.write(f"Non-assignable (disabled=true): {len(sr_flat) - len(sr_assignable)}\n\n")
        
        f.write("--- NON-ASSIGNABLE SR Groups (disabled=true) ---\n")
        for g in sr_flat:
            if g['disabled']:
                children_note = f" [{g['child_count']} children]" if g['has_children'] else " [LEAF]"
                f.write(f"  {g['guid']}  {g['fullname']}{children_note}\n")
        
        f.write(f"\n--- ASSIGNABLE SR Groups (disabled=false) --- ({len(sr_assignable)} groups)\n")
        for g in sr_assignable:
            children_note = " [HAS CHILDREN]" if g['has_children'] else ""
            indent = "  " * (g['depth'] + 1)
            f.write(f"{indent}{g['guid']}  {g['fullname']}{children_note}\n")
        
        f.write("\n" + "=" * 70 + "\n")
        f.write("EMPIRICAL TEST RESULTS\n")
        f.write("=" * 70 + "\n\n")
        for r in test_results:
            took_effect = r.get('assignment_took_effect', False)
            status = "ASSIGNABLE" if took_effect else "NOT ASSIGNABLE"
            symbol = "✓" if took_effect else "✗"
            f.write(f"  [{symbol}] {r['group_name']:<45} → {status}\n")
            f.write(f"       Type: {r['label']}, PUT status: {r['put_status']}\n")
            if not took_effect and r.get('error'):
                f.write(f"       Error: {r['error'][:200]}\n")
            f.write("\n")
    
    print(f"\n  Report saved to: {report_path}")
    print("\n" + "=" * 70)
    print("  EXPLORATION COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()