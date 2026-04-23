#!/usr/bin/env python
"""
Service Desk Helper — CLI Testing Tool

A command-line interface for testing all implemented features (1, 2, 3, 5)
without running the FastAPI server. Calls the service layer directly.

Usage:
    python cli.py <command> <subcommand> [options]

Run `python cli.py --help` for full usage details.
"""

import argparse
import asyncio
import json
import sys
import textwrap

from src.dependencies import (
    get_assignment_service,
    get_athena_client,
    get_bulk_assignment_service,
    get_chatbot_service,
    get_search_service,
    get_turnover_service,
)
from src.models.bulk import TicketAssignment
from src.models.turnover import TurnoverRequest


# ── Formatting Helpers ────────────────────────────────────────────────


def _header(text: str) -> str:
    """Return a formatted section header."""
    bar = "=" * 60
    return f"\n{bar}\n  {text}\n{bar}"


def _subheader(text: str) -> str:
    """Return a formatted sub-section header."""
    return f"\n--- {text} ---"


def _print_json(obj) -> None:
    """Print a Pydantic model as indented JSON."""
    if hasattr(obj, "model_dump"):
        print(json.dumps(obj.model_dump(), indent=2, default=str))
    else:
        print(json.dumps(obj, indent=2, default=str))


def _truncate(text: str | None, length: int = 120) -> str:
    """Truncate text to a given length."""
    if not text:
        return "N/A"
    if len(text) <= length:
        return text
    return text[:length] + "..."


# ── Feature #1: Search Commands ──────────────────────────────────────


async def cmd_search_field(args) -> None:
    """Execute a field-based ticket search."""
    service = get_search_service()
    result = await service.search_by_field(
        field=args.field,
        value=args.value,
        ticket_type=args.type,
        operator=args.operator,
    )

    if args.json:
        _print_json(result)
        return

    print(_header(f"Field Search: {args.field} {args.operator} '{args.value}'"))
    print(f"  Ticket type: {args.type}")
    print(f"  Results: {result.total}")

    if not result.tickets:
        print("\n  No tickets found.")
        return

    for t in result.tickets:
        print(f"\n  {t.id}  |  {t.status or 'N/A'}  |  P{t.priority or '?'}")
        print(f"    Title: {_truncate(t.title)}")
        print(f"    Support Group: {t.support_group or 'N/A'}")
        print(f"    Affected User: {t.affected_user or 'N/A'}")
        print(f"    Created: {t.created_date or 'N/A'}")


async def cmd_search_description(args) -> None:
    """Execute a description-based ticket search."""
    service = get_search_service()
    result = await service.search_by_description(
        text=args.text,
        ticket_type=args.type,
    )

    if args.json:
        _print_json(result)
        return

    print(_header(f"Description Search: '{args.text}'"))
    print(f"  Ticket type: {args.type}")
    print(f"  Results: {result.total}")

    if not result.tickets:
        print("\n  No tickets found.")
        return

    for t in result.tickets:
        print(f"\n  {t.id}  |  {t.status or 'N/A'}  |  P{t.priority or '?'}")
        print(f"    Title: {_truncate(t.title)}")
        print(f"    Description: {_truncate(t.description, 200)}")
        print(f"    Support Group: {t.support_group or 'N/A'}")


async def cmd_search_semantic(args) -> None:
    """Execute a semantic search."""
    service = get_search_service()
    result = await service.semantic_search(
        query=args.query,
        top_k=args.top_k,
    )

    if args.json:
        _print_json(result)
        return

    print(_header(f"Semantic Search: '{args.query}'"))

    print(_subheader(f"Similar Tickets ({len(result.similar_tickets)})"))
    if not result.similar_tickets:
        print("  No similar tickets found.")
    else:
        for t in result.similar_tickets:
            print(f"  {t.id}  (similarity: {t.similarity:.4f})")

    print(_subheader(f"Documentation Matches ({len(result.documentation)})"))
    if not result.documentation:
        print("  No documentation matches found.")
    else:
        for d in result.documentation:
            print(f"  [{d.notebook}] {d.section} > {d.title}  (similarity: {d.similarity:.4f})")
            print(f"    {_truncate(d.content, 200)}")


async def cmd_search_similar(args) -> None:
    """Find tickets similar to a given ticket ID."""
    service = get_search_service()
    try:
        result = await service.find_similar_tickets(
            ticket_id=args.ticket_id,
            top_k=args.top_k,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _print_json(result)
        return

    print(_header(f"Similar Tickets to {result.source_ticket_id}"))
    if not result.similar_tickets:
        print("  No similar tickets found.")
    else:
        for t in result.similar_tickets:
            print(f"  {t.id}  (similarity: {t.similarity:.4f})")


# ── Feature #2: Chat Commands ────────────────────────────────────────


async def cmd_chat_message(args) -> None:
    """Send a message to the Q&A chatbot."""
    service = get_chatbot_service()
    result = await service.chat(
        message=args.message,
        session_id=args.session_id,
    )

    if args.json:
        _print_json(result)
        return

    print(_header("Q&A Chatbot Response"))
    print(f"  Session ID: {result.session_id}")
    print(f"  (Use --session-id {result.session_id} for follow-up questions)")
    print(_subheader("Response"))
    # Wrap the response text for readability
    wrapped = textwrap.fill(result.message, width=80, initial_indent="  ", subsequent_indent="  ")
    print(wrapped)

    if result.sources:
        print(_subheader(f"Sources ({len(result.sources)})"))
        for s in result.sources:
            if s.type.value == "documentation":
                print(f"  [DOC] {s.title} ({s.notebook}/{s.section}) — similarity: {s.similarity:.4f}")
                if s.content_preview:
                    print(f"         {_truncate(s.content_preview, 150)}")
            else:
                print(f"  [TICKET] {s.title} — similarity: {s.similarity:.4f}")


async def cmd_chat_history(args) -> None:
    """Retrieve chat history for a session."""
    service = get_chatbot_service()
    result = service.get_history(args.session_id)

    if args.json:
        _print_json(result)
        return

    print(_header(f"Chat History — Session {result.session_id}"))
    if not result.messages:
        print("  No messages in this session.")
        return

    for msg in result.messages:
        role_label = "YOU" if msg.role.value == "user" else "ASSISTANT"
        print(f"\n  [{role_label}] ({msg.timestamp})")
        wrapped = textwrap.fill(msg.content, width=76, initial_indent="    ", subsequent_indent="    ")
        print(wrapped)


async def cmd_chat_reset(args) -> None:
    """Reset a chat session."""
    service = get_chatbot_service()
    found = service.reset_session(args.session_id)

    if args.json:
        _print_json({"session_id": args.session_id, "reset": found})
        return

    if found:
        print(f"Session '{args.session_id}' has been reset.")
    else:
        print(f"Session '{args.session_id}' not found (may already be cleared).")


# ── Feature #3: Assignment Command ───────────────────────────────────


async def cmd_assign(args) -> None:
    """Get an assignment recommendation for a ticket."""
    service = get_assignment_service()
    try:
        result = await service.recommend_assignment(ticket_id=args.ticket_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _print_json(result)
        return

    t = result.ticket
    r = result.recommendation

    print(_header(f"Assignment Recommendation for {t.id}"))

    print(_subheader("Ticket Info"))
    print(f"  ID:             {t.id}")
    print(f"  Type:           {t.ticket_type}")
    print(f"  Title:          {_truncate(t.title)}")
    print(f"  Status:         {t.status or 'N/A'}")
    print(f"  Priority:       {t.priority or 'N/A'}")
    print(f"  Support Group:  {t.support_group or 'N/A'}")
    print(f"  Affected User:  {t.affected_user or 'N/A'}")
    print(f"  Location:       {t.location or 'N/A'}")
    print(f"  Created:        {t.created_date or 'N/A'}")

    print(_subheader("Recommendation"))
    print(f"  Support Group:  {r.support_group_name}")
    print(f"  Group GUID:     {r.support_group_guid}")
    print(f"  Priority:       {r.priority}")
    print(f"  Rationale:")
    wrapped = textwrap.fill(r.rationale, width=72, initial_indent="    ", subsequent_indent="    ")
    print(wrapped)

    if result.sources:
        print(_subheader(f"Sources ({len(result.sources)})"))
        for s in result.sources:
            if s.type.value == "documentation":
                print(f"  [DOC] {s.title} ({s.notebook}/{s.section}) — similarity: {s.similarity:.4f}")
            else:
                print(f"  [TICKET] {s.title} — similarity: {s.similarity:.4f}")


# ── Feature #4: Bulk Commands ────────────────────────────────────────


async def cmd_bulk_queue(args) -> None:
    """Fetch and display the Validation queue."""
    service = get_bulk_assignment_service()
    result = await service.fetch_queue(
        tier_queue_name=args.queue_name,
    )

    if args.json:
        _print_json(result)
        return

    print(_header(f"Validation Queue: {args.queue_name}"))
    print(f"  Total tickets: {result.total}")
    print(f"  Active locks: {len(result.locks)}")

    if not result.tickets:
        print("\n  No tickets in queue.")
        return

    for t in result.tickets:
        lock_info = f" [LOCKED by {t.locked_by}]" if t.locked_by else ""
        print(f"\n  {t.id} ({t.ticket_type}){lock_info}")
        print(f"    Title: {_truncate(t.title)}")
        print(f"    Status: {t.status or 'N/A'}  |  Priority: {t.priority or 'N/A'}")
        print(f"    Tier Queue: {t.tier_queue or 'N/A'}")
        print(f"    Affected User: {t.affected_user or 'N/A'}")
        print(f"    Created: {t.created_date or 'N/A'}")
        print(f"    Entity ID: {t.entity_id}")


async def cmd_bulk_recommend(args) -> None:
    """Generate AI recommendations for specific tickets."""
    service = get_bulk_assignment_service()
    ticket_ids = [tid.strip() for tid in args.ticket_ids.split(",")]

    print(f"Generating recommendations for {len(ticket_ids)} ticket(s)...")
    result = await service.batch_recommend(ticket_ids=ticket_ids)

    if args.json:
        _print_json(result)
        return

    print(_header(f"Bulk Recommendations ({result.total} tickets)"))
    print(f"  Successful: {result.total - result.failed}")
    print(f"  Failed: {result.failed}")

    for rec in result.recommendations:
        status = "OK" if rec.success else "FAILED"
        print(f"\n  [{status}] {rec.ticket_id}")
        if rec.success:
            r = rec.recommendation
            print(f"    Support Group: {r.support_group_name}")
            print(f"    Group GUID:    {r.support_group_guid}")
            print(f"    Priority:      {r.priority}")
            print(f"    Rationale:")
            wrapped = textwrap.fill(r.rationale, width=68, initial_indent="      ", subsequent_indent="      ")
            print(wrapped)
        else:
            print(f"    Error: {rec.error}")


async def cmd_bulk_assign(args) -> None:
    """Assign a single ticket (for CLI testing)."""
    service = get_bulk_assignment_service()

    assignment = TicketAssignment(
        ticket_id=args.ticket_id,
        entity_id=args.entity_id,
        tier_queue_guid=args.tier_queue_guid,
        tier_queue_name=args.tier_queue_name or "",
        priority=args.priority,
    )

    result = await service.assign_tickets([assignment])

    if args.json:
        _print_json(result)
        return

    print(_header(f"Assign Ticket: {args.ticket_id}"))
    for r in result.results:
        if r.success:
            print(f"  Status: SUCCESS")
            print(f"  Updated Tier Queue: {r.updated_tier_queue}")
            print(f"  Updated Priority: {r.updated_priority}")
        else:
            print(f"  Status: FAILED")
            print(f"  Error: {r.error}")


# ── Reassign Command ─────────────────────────────────────────────────


async def cmd_reassign(args) -> None:
    """Reassign a ticket's support group (and optionally priority)."""
    athena = get_athena_client()

    # Step 1: Fetch the ticket to get entityId and current state
    print(f"Fetching ticket {args.ticket_id}...")
    try:
        ticket = await athena.get_ticket(args.ticket_id)
    except Exception as e:
        print(f"Error: Could not fetch ticket '{args.ticket_id}': {e}", file=sys.stderr)
        sys.exit(1)

    entity_id = ticket.get("entityId")
    if not entity_id:
        print(f"Error: Ticket '{args.ticket_id}' has no entityId.", file=sys.stderr)
        sys.exit(1)

    # Determine ticket type
    ticket_type = "incident" if args.ticket_id.upper().startswith("IR") else "servicerequest"

    # Extract current tier queue info
    current_tq = ticket.get("tierQueue")
    current_tq_name = current_tq.get("name") if isinstance(current_tq, dict) else str(current_tq or "N/A")
    current_tq_guid = current_tq.get("id") if isinstance(current_tq, dict) else None
    current_priority = ticket.get("priority", "N/A")

    # Step 2: Resolve the target support group GUID
    if args.tier_queue_guid:
        target_guid = args.tier_queue_guid
        target_name = args.support_group or target_guid
    elif args.support_group:
        print(f"Resolving support group '{args.support_group}' for {ticket_type}...")
        try:
            target_guid = await athena.resolve_support_group_guid(
                args.support_group, ticket_type
            )
        except Exception as e:
            print(f"Error: Failed to resolve support group: {e}", file=sys.stderr)
            sys.exit(1)

        if not target_guid:
            print(
                f"Error: Support group '{args.support_group}' not found in the "
                f"{ticket_type} enum tree.\n"
                f"  Tip: Use the full path (e.g., 'Service Desk\\Validation') or "
                f"provide the GUID directly with --tier-queue-guid.",
                file=sys.stderr,
            )
            sys.exit(1)
        target_name = args.support_group
    else:
        print("Error: Provide --support-group or --tier-queue-guid.", file=sys.stderr)
        sys.exit(1)

    target_priority = args.priority

    # Step 3: Show confirmation
    print(_header(f"Reassign Ticket: {args.ticket_id}"))
    print(_subheader("Current State"))
    print(f"  Ticket ID:      {args.ticket_id}")
    print(f"  Title:          {_truncate(ticket.get('title', 'N/A'))}")
    print(f"  Status:         {_extract_name(ticket.get('status'))}")
    print(f"  Support Group:  {current_tq_name}")
    print(f"  Priority:       {current_priority}")

    print(_subheader("Proposed Change"))
    print(f"  Support Group:  {current_tq_name}  →  {target_name}")
    print(f"  Group GUID:     {target_guid}")
    if target_priority is not None:
        print(f"  Priority:       {current_priority}  →  {target_priority}")
    else:
        print(f"  Priority:       (no change)")

    if not args.yes:
        confirm = input("\nProceed with reassignment? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Step 4: Execute the update
    print("\nUpdating ticket in Athena...")
    try:
        updated = await athena.update_ticket(
            ticket_id=args.ticket_id,
            entity_id=entity_id,
            tier_queue_guid=target_guid,
            priority=target_priority,
        )
    except Exception as e:
        print(f"Error: Update failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 5: Display result
    if args.json:
        _print_json(updated)
        return

    updated_tq = updated.get("tierQueue")
    updated_tq_name = updated_tq.get("name") if isinstance(updated_tq, dict) else str(updated_tq or "N/A")
    updated_priority = updated.get("priority", "N/A")

    print(_subheader("Result"))
    print(f"  Status:         SUCCESS")
    print(f"  Support Group:  {updated_tq_name}")
    print(f"  Priority:       {updated_priority}")


def _extract_name(field) -> str:
    """Extract the 'name' from a dict field, or return the value as string."""
    if isinstance(field, dict):
        return field.get("name", str(field))
    return str(field) if field else "N/A"


# ── Feature #5: Turnover Command ─────────────────────────────────────


async def cmd_turnover(args) -> None:
    """Generate a turnover email draft."""
    request = TurnoverRequest(
        turnover_agent_name=args.receiver,
        sender_name=args.sender,
        notes=args.notes or "",
        escalation_notes=args.escalation_notes or "",
        voicemail_notes=args.voicemail_notes or "",
        hours_lookahead=args.hours_lookahead,
    )

    service = get_turnover_service()
    result = await service.generate_turnover(request)

    if args.json:
        _print_json(result)
        return

    print(_header("Turnover Email Draft"))

    print(_subheader("Email Metadata"))
    print(f"  To:      {result.email_to}")
    print(f"  CC:      {result.email_cc}")
    print(f"  Subject: {result.email_subject}")

    print(_subheader("Ticket Summary"))
    print(f"  Total SEV tickets:     {result.total_tickets}")
    print(f"  Upcoming outages/CRs:  {len(result.upcoming_outages)}")
    print(f"  Parent tickets:        {len(result.parent_tickets)}")
    print(f"  Active SEVs:           {len(result.active_sevs)}")
    print(f"  Pended SEVs:           {len(result.pended_sevs)}")

    print(_subheader("Email Body"))
    print()
    print(result.email_body)


# ── Argument Parser ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Service Desk Helper — CLI Testing Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python cli.py search field --field contactMethod --value "215-555-1234"
              python cli.py search description --text "printer not working"
              python cli.py search semantic --query "user cannot log into VPN"
              python cli.py search similar --ticket-id IR1959493
              python cli.py chat --message "How do I reset a password?"
              python cli.py chat history --session-id <id>
              python cli.py chat reset --session-id <id>
              python cli.py assign IR1959493
              python cli.py reassign IR1959493 --support-group "Service Desk\\Validation"
              python cli.py reassign IR1959493 --tier-queue-guid "1a59b3b9-..."
              python cli.py turnover --sender "John Smith" --receiver "Jane Doe"
        """),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted text.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── search ────────────────────────────────────────────────────────
    search_parser = subparsers.add_parser("search", help="Feature #1: Enhanced Ticket Search")
    search_sub = search_parser.add_subparsers(dest="search_mode", help="Search modes")

    # search field
    sf = search_sub.add_parser("field", help="Search by a specific ticket field value")
    sf.add_argument("--field", required=True, help="Athena field name (e.g., contactMethod, supportGroup, title)")
    sf.add_argument("--value", required=True, help="Value to match")
    sf.add_argument("--type", default="incident", choices=["incident", "servicerequest"], help="Ticket type (default: incident)")
    sf.add_argument("--operator", default="eq", help="Filter operator: eq, ne, contains, like, gt, lt (default: eq)")

    # search description
    sd = search_sub.add_parser("description", help="Search by substring in ticket descriptions")
    sd.add_argument("--text", required=True, help="Text to search for in descriptions")
    sd.add_argument("--type", default="incident", choices=["incident", "servicerequest"], help="Ticket type (default: incident)")

    # search semantic
    ss = search_sub.add_parser("semantic", help="Natural language semantic search")
    ss.add_argument("--query", required=True, help="Natural language description of the issue")
    ss.add_argument("--top-k", type=int, default=10, help="Number of results to return (default: 10)")

    # search similar
    si = search_sub.add_parser("similar", help="Find tickets similar to a given ticket ID")
    si.add_argument("--ticket-id", required=True, help="Ticket ID (e.g., IR1959493)")
    si.add_argument("--top-k", type=int, default=10, help="Number of results to return (default: 10)")

    # ── chat ──────────────────────────────────────────────────────────
    chat_parser = subparsers.add_parser("chat", help="Feature #2: Q&A Chatbot")
    chat_parser.add_argument("--message", "-m", help="Message to send to the chatbot")
    chat_parser.add_argument("--session-id", "-s", default=None, help="Session ID for multi-turn conversation")

    chat_sub = chat_parser.add_subparsers(dest="chat_action", help="Chat sub-actions")

    # chat history
    ch = chat_sub.add_parser("history", help="View conversation history for a session")
    ch.add_argument("--session-id", "-s", required=True, help="Session ID to retrieve history for")

    # chat reset
    cr = chat_sub.add_parser("reset", help="Reset (clear) a chat session")
    cr.add_argument("--session-id", "-s", required=True, help="Session ID to reset")

    # ── assign ────────────────────────────────────────────────────────
    assign_parser = subparsers.add_parser("assign", help="Feature #3: Ticket Assignment Recommendation")
    assign_parser.add_argument("ticket_id", help="Ticket ID to analyze (e.g., IR1959493 or SR1959584)")

    # ── bulk ──────────────────────────────────────────────────────────
    bulk_parser = subparsers.add_parser("bulk", help="Feature #4: Bulk Ticket Assignment")
    bulk_sub = bulk_parser.add_subparsers(dest="bulk_action", help="Bulk sub-actions")

    # bulk queue
    bq = bulk_sub.add_parser("queue", help="Fetch the Validation queue")
    bq.add_argument("--queue-name", default="Validation", help="Tier queue name (default: Validation)")

    # bulk recommend
    br = bulk_sub.add_parser("recommend", help="Generate AI recommendations for tickets")
    br.add_argument("--ticket-ids", required=True, help="Comma-separated ticket IDs (e.g., IR10001,SR20001)")

    # bulk assign
    ba = bulk_sub.add_parser("assign", help="Assign a single ticket (for testing)")
    ba.add_argument("--ticket-id", required=True, help="Ticket ID (e.g., IR10001)")
    ba.add_argument("--entity-id", required=True, help="Athena entityId GUID")
    ba.add_argument("--tier-queue-guid", required=True, help="Target tier queue GUID")
    ba.add_argument("--tier-queue-name", default="", help="Target tier queue name (for display)")
    ba.add_argument("--priority", default=None, help="Priority to set (optional)")

    # ── reassign ──────────────────────────────────────────────────────
    reassign_parser = subparsers.add_parser(
        "reassign",
        help="Reassign a ticket's support group",
        description="Change the support group (tier queue) assignment for a ticket. "
        "Provide either --support-group (name lookup) or --tier-queue-guid (direct GUID).",
    )
    reassign_parser.add_argument(
        "ticket_id",
        help="Ticket ID to reassign (e.g., IR1959493 or SR1959584)",
    )
    reassign_group = reassign_parser.add_mutually_exclusive_group(required=True)
    reassign_group.add_argument(
        "--support-group", "-g",
        help="Target support group name (e.g., 'Service Desk\\Validation', 'PennChart\\ED'). "
        "Resolved via the Athena enum API.",
    )
    reassign_group.add_argument(
        "--tier-queue-guid",
        help="Target tier queue GUID directly (skips name resolution).",
    )
    reassign_parser.add_argument(
        "--priority", "-p",
        default=None,
        help="New priority to set (optional). For IR: integer (1-9). For SR: text (Low/Medium/High/Immediate).",
    )
    reassign_parser.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Skip confirmation prompt.",
    )

    # ── turnover ──────────────────────────────────────────────────────
    turnover_parser = subparsers.add_parser("turnover", help="Feature #5: Turnover Email Draft Generator")
    turnover_parser.add_argument("--sender", required=True, help="Name of the person sending the turnover")
    turnover_parser.add_argument("--receiver", required=True, help="Name of the agent taking over the shift")
    turnover_parser.add_argument("--notes", default="", help="Verbal turnover notes")
    turnover_parser.add_argument("--escalation-notes", default="", help="Escalation notes")
    turnover_parser.add_argument("--voicemail-notes", default="", help="Voicemail notes")
    turnover_parser.add_argument("--hours-lookahead", type=int, default=24, help="Hours ahead for upcoming CRs (default: 24)")

    return parser


# ── Main Entry Point ─────────────────────────────────────────────────


def main() -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Route to the correct handler
    if args.command == "search":
        if not args.search_mode:
            parser.parse_args(["search", "--help"])
            sys.exit(0)
        handlers = {
            "field": cmd_search_field,
            "description": cmd_search_description,
            "semantic": cmd_search_semantic,
            "similar": cmd_search_similar,
        }
        handler = handlers[args.search_mode]

    elif args.command == "chat":
        if args.chat_action == "history":
            handler = cmd_chat_history
        elif args.chat_action == "reset":
            handler = cmd_chat_reset
        elif args.message:
            handler = cmd_chat_message
        else:
            parser.parse_args(["chat", "--help"])
            sys.exit(0)

    elif args.command == "assign":
        handler = cmd_assign

    elif args.command == "bulk":
        if not args.bulk_action:
            parser.parse_args(["bulk", "--help"])
            sys.exit(0)
        bulk_handlers = {
            "queue": cmd_bulk_queue,
            "recommend": cmd_bulk_recommend,
            "assign": cmd_bulk_assign,
        }
        handler = bulk_handlers[args.bulk_action]

    elif args.command == "reassign":
        handler = cmd_reassign

    elif args.command == "turnover":
        handler = cmd_turnover

    else:
        parser.print_help()
        sys.exit(0)

    # Run the async handler
    try:
        asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()