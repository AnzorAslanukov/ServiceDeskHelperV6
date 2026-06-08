"""
Knowledge Graph Query Interface
================================
Query the extracted knowledge graph for structured information.

Usage:
    python query_knowledge_graph.py --stats
    python query_knowledge_graph.py --system "PennChart"
    python query_knowledge_graph.py --team "LGH Telecom"
    python query_knowledge_graph.py --priority 1
    python query_knowledge_graph.py --procedure "Voicemail issues"
    python query_knowledge_graph.py --list systems
    python query_knowledge_graph.py --list teams
    python query_knowledge_graph.py --list procedures
    python query_knowledge_graph.py --search "password reset"
"""

import os
import json
import argparse
from knowledge_graph_store import KnowledgeGraphStore


GRAPH_PATH = os.path.join(os.path.dirname(__file__), 'output', 'knowledge_graph.json')


def print_separator():
    print(f"  {'='*60}")


def show_stats(store: KnowledgeGraphStore):
    """Display graph statistics."""
    stats = store.get_stats()
    print(f"\n  KNOWLEDGE GRAPH STATISTICS")
    print_separator()
    print(f"  Total nodes: {stats['total_nodes']}")
    print(f"  Total edges: {stats['total_edges']}")
    print(f"\n  Node Types:")
    for ntype, count in sorted(stats['node_types'].items(), key=lambda x: -x[1]):
        print(f"    {ntype:25s} {count:5d}")
    print(f"\n  Edge Types:")
    for etype, count in sorted(stats['edge_types'].items(), key=lambda x: -x[1]):
        print(f"    {etype:25s} {count:5d}")


def query_system(store: KnowledgeGraphStore, system_name: str):
    """Find all facts related to a system."""
    results = store.query_by_system(system_name)
    print(f"\n  FACTS RELATED TO SYSTEM: '{system_name}'")
    print_separator()

    if not results:
        # Try partial match
        all_systems = store.list_all("System")
        matches = [s for s in all_systems if system_name.lower() in s.lower()]
        if matches:
            print(f"  No exact match. Did you mean one of these?")
            for m in matches[:10]:
                print(f"    - {m}")
        else:
            print(f"  No system found matching '{system_name}'")
        return

    print(f"  Found {len(results)} related facts:\n")
    for r in results:
        attrs = r['node_attrs']
        node_type = attrs.get('type', 'Unknown')
        rel = r['relationship']
        print(f"  [{node_type}] ({rel})")
        if node_type == "Escalation":
            print(f"    Condition: {attrs.get('condition', 'N/A')}")
            print(f"    Urgency:   {attrs.get('urgency', 'N/A')}")
        elif node_type == "PriorityRule":
            print(f"    Condition: {attrs.get('condition', 'N/A')}")
            print(f"    Priority:  {attrs.get('priority', 'N/A')}")
            print(f"    Reason:    {attrs.get('reason', 'N/A')}")
        elif node_type == "TroubleshootingStep":
            print(f"    Action:    {attrs.get('action', 'N/A')}")
            print(f"    If fails:  {attrs.get('if_fails', 'N/A')}")
        elif node_type == "CallCapture":
            print(f"    Scenario:  {attrs.get('scenario', 'N/A')}")
            fields = attrs.get('required_fields', '[]')
            print(f"    Fields:    {fields}")
        print()


def query_team(store: KnowledgeGraphStore, team_name: str):
    """Find all facts that route/escalate to a team."""
    results = store.query_by_team(team_name)
    print(f"\n  FACTS ROUTING TO TEAM: '{team_name}'")
    print_separator()

    if not results:
        all_teams = store.list_all("Team")
        matches = [t for t in all_teams if team_name.lower() in t.lower()]
        if matches:
            print(f"  No exact match. Did you mean one of these?")
            for m in matches[:10]:
                print(f"    - {m}")
        else:
            print(f"  No team found matching '{team_name}'")
        return

    print(f"  Found {len(results)} routing rules:\n")
    for r in results:
        attrs = r['node_attrs']
        node_type = attrs.get('type', 'Unknown')
        rel = r['relationship']
        print(f"  [{node_type}] ({rel})")
        if 'condition' in attrs:
            print(f"    Condition: {attrs['condition']}")
        if 'urgency' in attrs:
            print(f"    Urgency:   {attrs['urgency']}")
        if 'scenario' in attrs:
            print(f"    Scenario:  {attrs['scenario']}")
        print()


def query_priority(store: KnowledgeGraphStore, level: str):
    """Find all facts requiring a specific priority level."""
    results = store.query_by_priority(level)
    print(f"\n  FACTS REQUIRING PRIORITY {level}")
    print_separator()

    if not results:
        print(f"  No facts found for priority level '{level}'")
        return

    print(f"  Found {len(results)} priority rules:\n")
    for r in results:
        attrs = r['node_attrs']
        print(f"  Condition: {attrs.get('condition', 'N/A')}")
        print(f"  Reason:    {attrs.get('reason', 'N/A')}")
        print(f"  Priority:  {attrs.get('priority', level)}")
        print()


def query_procedure(store: KnowledgeGraphStore, procedure_name: str):
    """Get steps for a procedure."""
    steps = store.get_procedure_steps(procedure_name)
    print(f"\n  PROCEDURE: '{procedure_name}'")
    print_separator()

    if not steps:
        all_procs = store.list_all("Procedure")
        matches = [p for p in all_procs if procedure_name.lower() in p.lower()]
        if matches:
            print(f"  No exact match. Did you mean one of these?")
            for m in matches[:10]:
                print(f"    - {m}")
        else:
            print(f"  No procedure found matching '{procedure_name}'")
        return

    print(f"  {len(steps)} steps:\n")
    for step in steps:
        print(f"  Step {step['step_order']}:")
        print(f"    Action:   {step['action']}")
        if step['if_fails']:
            print(f"    If fails: {step['if_fails']}")
        print()


def list_entities(store: KnowledgeGraphStore, entity_type: str):
    """List all entities of a given type."""
    type_map = {
        "systems": "System",
        "teams": "Team",
        "procedures": "Procedure",
        "locations": "Location",
        "documents": "Document",
        "priorities": "Priority"
    }

    node_type = type_map.get(entity_type.lower(), entity_type)
    items = store.list_all(node_type)

    print(f"\n  ALL {node_type.upper()}S ({len(items)} total)")
    print_separator()

    for item in sorted(items):
        print(f"    {item}")


def search_graph(store: KnowledgeGraphStore, query: str):
    """Search across all node attributes for a query string."""
    query_lower = query.lower()
    results = []

    for node_id, attrs in store.graph.nodes(data=True):
        # Search across all string attributes
        for key, value in attrs.items():
            if isinstance(value, str) and query_lower in value.lower():
                results.append({
                    "node_id": node_id,
                    "type": attrs.get("type", "Unknown"),
                    "matched_field": key,
                    "matched_value": value[:200],
                    "attrs": attrs
                })
                break  # Only add each node once

    print(f"\n  SEARCH RESULTS FOR: '{query}'")
    print_separator()
    print(f"  Found {len(results)} matching nodes:\n")

    for r in results[:20]:  # Limit to 20 results
        print(f"  [{r['type']}] matched on '{r['matched_field']}':")
        attrs = r['attrs']
        if r['type'] == "Escalation":
            print(f"    Condition: {attrs.get('condition', '')[:100]}")
            print(f"    Urgency:   {attrs.get('urgency', '')}")
        elif r['type'] == "PriorityRule":
            print(f"    Condition: {attrs.get('condition', '')[:100]}")
            print(f"    Priority:  {attrs.get('priority', '')}")
        elif r['type'] == "TroubleshootingStep":
            print(f"    Action:    {attrs.get('action', '')[:100]}")
        elif r['type'] == "CallCapture":
            print(f"    Scenario:  {attrs.get('scenario', '')[:100]}")
        elif r['type'] == "GeneralFact":
            print(f"    Subject:   {attrs.get('subject', '')}")
            print(f"    Predicate: {attrs.get('predicate', '')}")
            print(f"    Object:    {attrs.get('object', '')}")
        elif r['type'] == "Document":
            print(f"    Title:     {attrs.get('name', '')}")
            print(f"    Section:   {attrs.get('section', '')}")
        elif r['type'] in ("System", "Team", "Location", "Procedure"):
            print(f"    Name:      {attrs.get('name', '')}")
        print()

    if len(results) > 20:
        print(f"  ... and {len(results) - 20} more results")


def main():
    parser = argparse.ArgumentParser(description="Query the knowledge graph")
    parser.add_argument("--stats", action="store_true", help="Show graph statistics")
    parser.add_argument("--system", type=str, help="Query facts about a system")
    parser.add_argument("--team", type=str, help="Query facts routing to a team")
    parser.add_argument("--priority", type=str, help="Query facts for a priority level")
    parser.add_argument("--procedure", type=str, help="Get steps for a procedure")
    parser.add_argument("--list", type=str, help="List all entities of a type (systems, teams, procedures, locations, documents)")
    parser.add_argument("--search", type=str, help="Full-text search across all nodes")
    parser.add_argument("--graph-path", type=str, default=GRAPH_PATH, help="Path to knowledge graph JSON")
    args = parser.parse_args()

    if not os.path.exists(args.graph_path):
        print(f"\n  ERROR: Knowledge graph not found at {args.graph_path}")
        print(f"  Run the extraction first: python extract_knowledge_graph.py --limit 50")
        return

    store = KnowledgeGraphStore(args.graph_path)

    if args.stats:
        show_stats(store)
    elif args.system:
        query_system(store, args.system)
    elif args.team:
        query_team(store, args.team)
    elif args.priority:
        query_priority(store, args.priority)
    elif args.procedure:
        query_procedure(store, args.procedure)
    elif args.list:
        list_entities(store, args.list)
    elif args.search:
        search_graph(store, args.search)
    else:
        # Default: show stats
        show_stats(store)


if __name__ == '__main__':
    main()