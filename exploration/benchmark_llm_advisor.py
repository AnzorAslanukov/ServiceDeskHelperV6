"""
Benchmark: LLM-Assisted Ticket Advisor (RAG Pipeline)
=====================================================
Tests the Feature #2-style RAG pipeline for support group recommendation:
  1. Query knowledge graph for structured facts
  2. Generate embedding → search similar docs (local vector store)
  3. Search similar tickets (local vector store)
  4. Build context with candidate support groups (keyword pre-filtered)
  5. Call Claude Sonnet 4.5 with structured JSON prompt
  6. Parse response → extract recommended group

Compares LLM advisor accuracy against TF-IDF classifier on the same
100-ticket benchmark dataset (exploration/output/benchmark_dataset.json).

Usage:
    python -m exploration.benchmark_llm_advisor [--limit N] [--resume] [--report-only]

Requirements:
    - Databricks API access (DATABRICKS_API_KEY in .env)
    - Local vector store data (data/vectors/)
    - Knowledge graph (knowledge_graph/output/knowledge_graph.json)
    - Benchmark dataset (exploration/output/benchmark_dataset.json)
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.clients.databricks_client import DatabricksClient
from src.services.local_vector_store import LocalVectorStore
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.assignment import (
    IR_SUPPORT_GROUPS,
    SR_SUPPORT_GROUPS,
    check_specific_triage,
    check_service_desk_triage,
    resolve_group_guid,
)
from src.services.ticket_classifier import get_ticket_classifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Inline helpers (not in production assignment.py) ───────────────────


def normalize_predicted_group(group_name: str) -> str:
    """Extract leaf name from a full hierarchical path (e.g., 'EUS\\HUP' -> 'HUP')."""
    if not group_name:
        return "Service Desk"
    # Return the last segment of the path
    parts = group_name.split("\\")
    return parts[-1].strip() if parts else group_name.strip()


def keyword_prefilter(
    title: str, description: str, location: str, ticket_type: str
) -> list[str]:
    """
    Simple keyword-based pre-filter to narrow candidate support groups.
    Returns a list of group names most likely relevant to this ticket.
    """
    support_groups = IR_SUPPORT_GROUPS if ticket_type == "IR" else SR_SUPPORT_GROUPS
    all_groups = list(support_groups.keys())
    text = f"{title} {description}".lower()
    location_lower = location.lower() if location else ""

    scored: dict[str, float] = {}

    # Load keyword mappings if available
    mappings_path = PROJECT_ROOT / "exploration" / "output" / "keyword_group_mappings.json"
    if mappings_path.exists():
        with open(mappings_path, "r") as f:
            keyword_mappings = json.load(f)

        for group_name, rule in keyword_mappings.items():
            if group_name not in support_groups:
                continue
            keywords = rule.get("keywords", [])
            negatives = rule.get("negative_keywords", [])

            # Check negatives first
            if any(neg in text for neg in negatives):
                continue

            # Score by keyword matches
            score = sum(1.0 for kw in keywords if kw.lower() in text)
            if score > 0:
                scored[group_name] = scored.get(group_name, 0) + score

    # Location-based EUS matching
    eus_location_map = {
        "hup": "HUP", "hup cedar": "HUP Cedar", "hup pavilion": "HUP Pavilion",
        "ppmc": "PPMC", "pah": "PaH", "pcam": "PCAM", "cch": "CCH",
        "mcp": "MCP", "princeton": "MCP", "lgh": "LGH", "ritt": "RITT",
        "pmuc": "PMUC", "campus": "Campus", "rsi": "RSI", "remote": "RSI",
        "doylestown": "PMDH Dispatch", "pmdh": "PMDH Dispatch",
    }
    for loc_key, eus_group in eus_location_map.items():
        if loc_key in location_lower or loc_key in text:
            full_name = f"EUS\\{eus_group}" if eus_group not in ("PMDH Dispatch",) else eus_group
            if full_name in support_groups:
                scored[full_name] = scored.get(full_name, 0) + 3.0
            # Also try just the leaf name
            for g in all_groups:
                if g.endswith(f"\\{eus_group}") or g == eus_group:
                    scored[g] = scored.get(g, 0) + 3.0

    # Always include Service Desk as a candidate
    scored["Service Desk"] = scored.get("Service Desk", 0) + 1.0

    # Sort by score and take top 30
    sorted_groups = sorted(scored.keys(), key=lambda g: scored[g], reverse=True)
    candidates = sorted_groups[:30]

    # If we got very few candidates, pad with common groups
    if len(candidates) < 10:
        common = ["Service Desk", "PennChart", "IS Education", "ATLAS",
                  "Account Provisioning", "User Provisioning", "HRIS/PennforPeopleHR"]
        for g in common:
            if g in support_groups and g not in candidates:
                candidates.append(g)

    return candidates


logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = PROJECT_ROOT / "exploration" / "output" / "benchmark_dataset.json"
RESULTS_PATH = PROJECT_ROOT / "exploration" / "output" / "llm_advisor_results.json"
REPORT_PATH = PROJECT_ROOT / "exploration" / "output" / "llm_advisor_report.txt"

# ── LLM Advisor System Prompt ──────────────────────────────────────────

LLM_ADVISOR_SYSTEM_PROMPT = """You are an expert IT ticket routing advisor for Penn Medicine (UPHS).
Your job is to analyze a support ticket and recommend the correct support group to handle it.

You will be given:
1. The ticket details (title, description, location, affected user)
2. Structured knowledge from the knowledge graph (escalation paths, procedures, priority rules)
3. Similar documentation from the OneNote knowledge base
4. Similar historical tickets and their assigned support groups

Based on ALL available context, recommend the single best support group from the CANDIDATE GROUPS list.

RESPOND WITH ONLY A JSON OBJECT — no other text before or after:
{
  "support_group": "<exact group name from CANDIDATE GROUPS>",
  "priority": <number 1-3 for IR tickets>,
  "rationale": "<1-2 sentence explanation>"
}

CRITICAL RULES:
- support_group MUST be one of the exact names from the CANDIDATE GROUPS list below
- Do NOT invent group names or modify the names in any way
- If the knowledge graph mentions a specific escalation target, prefer that group
- If similar tickets were consistently assigned to one group, that's a strong signal
- For hardware/device issues, consider the ticket's physical location for EUS sub-group routing
- Password resets, account lockouts, and basic access issues typically stay at Service Desk
- If truly unsure, pick "Service Desk" as the safe default
"""


def build_llm_advisor_prompt(
    ticket: dict,
    kg_context: str,
    doc_context: str,
    ticket_context: str,
    candidate_groups: list[str],
) -> list[dict[str, str]]:
    """Build the messages array for the LLM advisor call."""

    # Format candidate groups
    candidates_text = "\n".join(f"  - {g}" for g in sorted(candidate_groups))

    # Format ticket info
    ticket_text = f"""TICKET DETAILS:
- ID: {ticket['ticket_id']}
- Type: {ticket['ticket_type']}
- Title: {ticket['title']}
- Description: {ticket.get('description', 'N/A')}
- Location: {ticket.get('location', 'N/A')}
- Affected User: {ticket.get('affected_user', 'N/A')}
- User Job Title: {ticket.get('affected_user_title', 'N/A')}"""

    # Assemble user message
    user_parts = [ticket_text]

    if kg_context:
        user_parts.append(f"\n{kg_context}")

    if doc_context:
        user_parts.append(f"\nRELEVANT DOCUMENTATION:\n{doc_context}")

    if ticket_context:
        user_parts.append(f"\nSIMILAR HISTORICAL TICKETS:\n{ticket_context}")

    user_parts.append(f"\nCANDIDATE GROUPS (you MUST pick from this list):\n{candidates_text}")

    return [
        {"role": "system", "content": LLM_ADVISOR_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


# ── Pipeline ───────────────────────────────────────────────────────────


async def run_llm_advisor(
    ticket: dict,
    databricks_client: DatabricksClient,
    vector_store: LocalVectorStore,
    kg_service: KnowledgeGraphService,
) -> dict:
    """
    Run the full RAG-based LLM advisor pipeline for a single ticket.

    Returns dict with: predicted_group, predicted_priority, rationale, method,
                       latency_ms, kg_facts_count, docs_count, similar_tickets_count
    """
    start_time = time.time()
    ticket_type = ticket["ticket_type"]
    search_text = f"{ticket['title']} {ticket.get('description', '')}"

    # ── Step 1: Triage rules (same as Feature #3) ──
    support_groups = IR_SUPPORT_GROUPS if ticket_type == "IR" else SR_SUPPORT_GROUPS
    triage_result = check_specific_triage(
        ticket["title"],
        ticket.get("description", ""),
        ticket.get("location", ""),
        support_groups,
    )
    if triage_result:
        # triage_result may be a string or tuple (group_name, guid)
        triage_group = triage_result[0] if isinstance(triage_result, tuple) else triage_result
        elapsed = (time.time() - start_time) * 1000
        return {
            "predicted_group": triage_group,
            "predicted_priority": ticket.get("actual_priority", 3),
            "rationale": "Matched specific triage rule",
            "method": "triage_rule",
            "latency_ms": elapsed,
            "kg_facts_count": 0,
            "docs_count": 0,
            "similar_tickets_count": 0,
        }

    sd_triage = check_service_desk_triage(
        ticket["title"],
        ticket.get("description", ""),
    )
    if sd_triage:
        elapsed = (time.time() - start_time) * 1000
        return {
            "predicted_group": "Service Desk",
            "predicted_priority": ticket.get("actual_priority", 3),
            "rationale": "Matched Service Desk triage rule",
            "method": "triage_rule",
            "latency_ms": elapsed,
            "kg_facts_count": 0,
            "docs_count": 0,
            "similar_tickets_count": 0,
        }

    # ── Step 2: Knowledge Graph query ──
    kg_context = ""
    kg_facts_count = 0
    if kg_service.is_available:
        kg_result = kg_service.query_for_chat(search_text)
        kg_facts_count = len(kg_result.get("facts", []))
        if kg_facts_count > 0:
            kg_context = kg_service.format_facts_for_llm(kg_result)

    # ── Step 3: Generate embedding ──
    embedding = await databricks_client.generate_embedding(search_text[:2000])

    # ── Step 4: Search similar documentation ──
    docs = vector_store.find_similar_documentation(embedding, top_k=5)
    doc_context = ""
    if docs:
        doc_parts = []
        for i, doc in enumerate(docs[:3], 1):
            content_preview = doc["content"][:300] if doc.get("content") else ""
            doc_parts.append(
                f"  {i}. [{doc.get('section', '')}] {doc.get('title', '')} "
                f"(similarity: {doc['similarity']:.3f})\n     {content_preview}"
            )
        doc_context = "\n".join(doc_parts)

    # ── Step 5: Search similar tickets ──
    similar_tickets = vector_store.find_similar_by_embedding(embedding, top_k=10)
    ticket_context = ""
    similar_count = 0
    if similar_tickets:
        # Load ticket support group mapping if available
        mapping_path = PROJECT_ROOT / "exploration" / "output" / "ticket_support_group_mapping.json"
        ticket_sg_map = {}
        if mapping_path.exists():
            with open(mapping_path, "r") as f:
                ticket_sg_map = json.load(f)

        ticket_parts = []
        for t in similar_tickets[:5]:
            tid = t["id"]
            sg = ticket_sg_map.get(tid, "unknown")
            ticket_parts.append(f"  - {tid} (similarity: {t['similarity']:.3f}) → assigned to: {sg}")
            similar_count += 1
        ticket_context = "\n".join(ticket_parts)

    # ── Step 6: Keyword pre-filter for candidate groups ──
    support_groups = IR_SUPPORT_GROUPS if ticket_type == "IR" else SR_SUPPORT_GROUPS
    candidates = keyword_prefilter(
        ticket["title"],
        ticket.get("description", ""),
        ticket.get("location", ""),
        ticket_type,
    )
    # Ensure we have at least some candidates
    if len(candidates) < 5:
        candidates = list(support_groups.keys())[:30]

    # ── Step 7: Call LLM ──
    messages = build_llm_advisor_prompt(
        ticket, kg_context, doc_context, ticket_context, candidates
    )

    try:
        llm_response = await databricks_client.call_llm(messages, max_tokens=512)
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        return {
            "predicted_group": "Service Desk",
            "predicted_priority": 3,
            "rationale": f"LLM call failed: {e}",
            "method": "llm_error_fallback",
            "latency_ms": elapsed,
            "kg_facts_count": kg_facts_count,
            "docs_count": len(docs),
            "similar_tickets_count": similar_count,
        }

    # ── Step 8: Parse LLM response ──
    predicted_group, predicted_priority, rationale = parse_llm_response(llm_response)

    # ── Step 9: Normalize group name ──
    predicted_group = normalize_predicted_group(predicted_group)

    elapsed = (time.time() - start_time) * 1000
    return {
        "predicted_group": predicted_group,
        "predicted_priority": predicted_priority,
        "rationale": rationale,
        "method": "llm_advisor",
        "latency_ms": elapsed,
        "kg_facts_count": kg_facts_count,
        "docs_count": len(docs),
        "similar_tickets_count": similar_count,
        "raw_llm_response": llm_response[:500],
    }


def parse_llm_response(response: str) -> tuple[str, int | str, str]:
    """Parse the LLM JSON response. Returns (group, priority, rationale)."""
    # Strip code fences
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    # Fix unescaped backslashes in group paths
    cleaned = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r"\\\\", cleaned)

    try:
        data = json.loads(cleaned)
        group = data.get("support_group", "Service Desk")
        priority = data.get("priority", 3)
        rationale = data.get("rationale", "")

        # Normalize priority
        if isinstance(priority, str):
            priority_map = {"low": 4, "medium": 3, "high": 2, "critical": 1, "urgent": 1}
            priority = priority_map.get(priority.lower(), 3)

        return group, priority, rationale
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Failed to parse LLM response: %s — response: %s", e, response[:200])
        return "Service Desk", 3, f"Parse error: {response[:200]}"


# ── TF-IDF Classifier (for comparison) ────────────────────────────────


def run_tfidf_classifier(ticket: dict) -> dict:
    """Run the TF-IDF classifier for comparison."""
    ticket_type = ticket["ticket_type"]
    support_groups = IR_SUPPORT_GROUPS if ticket_type == "IR" else SR_SUPPORT_GROUPS

    # Check triage rules first (same as LLM pipeline)
    triage_result = check_specific_triage(
        ticket["title"],
        ticket.get("description", ""),
        ticket.get("location", ""),
        support_groups,
    )
    if triage_result:
        triage_group = triage_result[0] if isinstance(triage_result, tuple) else triage_result
        return {"predicted_group": triage_group, "method": "triage_rule", "confidence": 1.0}

    sd_triage = check_service_desk_triage(
        ticket["title"],
        ticket.get("description", ""),
    )
    if sd_triage:
        return {"predicted_group": "Service Desk", "method": "triage_rule", "confidence": 1.0}

    # Run classifier
    classifier = get_ticket_classifier()
    if classifier is None:
        return {"predicted_group": "Service Desk", "method": "no_classifier", "confidence": 0.0}

    results = classifier.predict(
        title=ticket["title"],
        description=ticket.get("description", ""),
        ticket_type=ticket["ticket_type"],
        location=ticket.get("location", ""),
    )

    # predict() returns a list of dicts sorted by confidence; take top result
    if results and isinstance(results, list) and len(results) > 0:
        top = results[0]
        predicted = normalize_predicted_group(top["support_group"])
        return {
            "predicted_group": predicted,
            "method": "classifier",
            "confidence": top.get("confidence", 0.0),
        }
    return {"predicted_group": "Service Desk", "method": "classifier_empty", "confidence": 0.0}


# ── Scoring ────────────────────────────────────────────────────────────


def score_prediction(predicted: str, actual: str) -> dict:
    """Score a single prediction against ground truth."""
    predicted_lower = predicted.lower().strip()
    actual_lower = actual.lower().strip()

    # Exact match
    exact = predicted_lower == actual_lower

    # Leaf match (last segment of path)
    pred_leaf = predicted_lower.split("\\")[-1].strip()
    actual_leaf = actual_lower.split("\\")[-1].strip()
    leaf = pred_leaf == actual_leaf

    # Contains match (one contains the other)
    contains = actual_lower in predicted_lower or predicted_lower in actual_lower

    # Top-level match (first segment)
    pred_top = predicted_lower.split("\\")[0].strip()
    actual_top = actual_lower.split("\\")[0].strip()
    top_level = pred_top == actual_top

    return {
        "exact": exact,
        "leaf": leaf,
        "contains": contains,
        "top_level": top_level,
    }


# ── Main ───────────────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(description="Benchmark LLM Advisor vs TF-IDF Classifier")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of tickets to process")
    parser.add_argument("--resume", action="store_true", help="Resume from previous partial run")
    parser.add_argument("--report-only", action="store_true", help="Generate report from existing results")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between LLM calls (seconds)")
    args = parser.parse_args()

    # Load dataset
    if not DATASET_PATH.exists():
        logger.error("Benchmark dataset not found at %s", DATASET_PATH)
        logger.error("Run 'python -m exploration.benchmark_assignment_accuracy' first to generate it.")
        sys.exit(1)

    with open(DATASET_PATH, "r") as f:
        dataset = json.load(f)
    tickets = dataset["tickets"]
    logger.info("Loaded %d tickets from benchmark dataset", len(tickets))

    if args.limit:
        tickets = tickets[: args.limit]
        logger.info("Limited to %d tickets", len(tickets))

    # Report-only mode
    if args.report_only:
        if not RESULTS_PATH.exists():
            logger.error("No results file found at %s", RESULTS_PATH)
            sys.exit(1)
        with open(RESULTS_PATH, "r") as f:
            results = json.load(f)
        generate_report(results, tickets)
        return

    # Resume support
    existing_results = []
    processed_ids = set()
    if args.resume and RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r") as f:
            existing_results = json.load(f)
        processed_ids = {r["ticket_id"] for r in existing_results}
        logger.info("Resuming: %d tickets already processed", len(processed_ids))

    # Initialize services
    logger.info("Initializing services...")
    settings = Settings()
    databricks_client = DatabricksClient(settings)

    vector_store = LocalVectorStore()
    vector_store.load()
    logger.info(
        "Vector store: %d docs, %d tickets",
        vector_store.documentation_count,
        vector_store.ticket_count,
    )

    kg_service = KnowledgeGraphService()
    kg_service.load()
    logger.info("Knowledge graph: available=%s", kg_service.is_available)

    # Process tickets
    results = list(existing_results)
    total = len(tickets)
    skipped = 0

    for i, ticket in enumerate(tickets):
        tid = ticket["ticket_id"]

        if tid in processed_ids:
            skipped += 1
            continue

        logger.info(
            "[%d/%d] Processing %s: %s",
            i + 1, total, tid, ticket["title"][:60],
        )

        # Run LLM advisor
        llm_result = await run_llm_advisor(
            ticket, databricks_client, vector_store, kg_service
        )

        # Run TF-IDF classifier for comparison
        tfidf_result = run_tfidf_classifier(ticket)

        # Score both
        actual_group = ticket["actual_support_group"]
        llm_score = score_prediction(llm_result["predicted_group"], actual_group)
        tfidf_score = score_prediction(tfidf_result["predicted_group"], actual_group)

        result_entry = {
            "ticket_id": tid,
            "ticket_type": ticket["ticket_type"],
            "title": ticket["title"],
            "actual_group": actual_group,
            "actual_priority": ticket.get("actual_priority"),
            # LLM results
            "llm_predicted_group": llm_result["predicted_group"],
            "llm_predicted_priority": llm_result["predicted_priority"],
            "llm_rationale": llm_result["rationale"],
            "llm_method": llm_result["method"],
            "llm_latency_ms": llm_result["latency_ms"],
            "llm_kg_facts": llm_result["kg_facts_count"],
            "llm_docs": llm_result["docs_count"],
            "llm_similar_tickets": llm_result["similar_tickets_count"],
            "llm_exact": llm_score["exact"],
            "llm_leaf": llm_score["leaf"],
            "llm_contains": llm_score["contains"],
            # TF-IDF results
            "tfidf_predicted_group": tfidf_result["predicted_group"],
            "tfidf_method": tfidf_result["method"],
            "tfidf_confidence": tfidf_result.get("confidence", 0.0),
            "tfidf_exact": tfidf_score["exact"],
            "tfidf_leaf": tfidf_score["leaf"],
            "tfidf_contains": tfidf_score["contains"],
        }
        results.append(result_entry)

        # Save progress after each ticket
        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2)

        # Rate limiting
        if llm_result["method"] == "llm_advisor":
            await asyncio.sleep(args.delay)

    if skipped:
        logger.info("Skipped %d already-processed tickets", skipped)

    logger.info("All %d tickets processed. Generating report...", len(results))
    generate_report(results, tickets)


def generate_report(results: list[dict], tickets: list[dict]) -> None:
    """Generate a human-readable comparison report."""
    total = len(results)
    if total == 0:
        logger.error("No results to report on.")
        return

    # Aggregate scores
    llm_exact = sum(1 for r in results if r["llm_exact"])
    llm_leaf = sum(1 for r in results if r["llm_leaf"])
    llm_contains = sum(1 for r in results if r["llm_contains"])
    tfidf_exact = sum(1 for r in results if r["tfidf_exact"])
    tfidf_leaf = sum(1 for r in results if r["tfidf_leaf"])
    tfidf_contains = sum(1 for r in results if r["tfidf_contains"])

    # Method breakdown
    llm_triage = sum(1 for r in results if r["llm_method"] == "triage_rule")
    llm_advisor = sum(1 for r in results if r["llm_method"] == "llm_advisor")
    llm_error = sum(1 for r in results if r["llm_method"] == "llm_error_fallback")

    # Latency stats
    latencies = [r["llm_latency_ms"] for r in results if r["llm_method"] == "llm_advisor"]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    max_latency = max(latencies) if latencies else 0
    min_latency = min(latencies) if latencies else 0

    # Priority accuracy
    priority_exact = sum(
        1 for r in results
        if r.get("llm_predicted_priority") == r.get("actual_priority")
    )
    priority_within1 = sum(
        1 for r in results
        if r.get("actual_priority") is not None
        and r.get("llm_predicted_priority") is not None
        and abs(int(r["llm_predicted_priority"]) - int(r["actual_priority"])) <= 1
    )

    # Cases where LLM is right but TF-IDF is wrong (the value-add)
    llm_wins = [r for r in results if r["llm_leaf"] and not r["tfidf_leaf"]]
    tfidf_wins = [r for r in results if r["tfidf_leaf"] and not r["llm_leaf"]]
    both_right = [r for r in results if r["llm_leaf"] and r["tfidf_leaf"]]
    both_wrong = [r for r in results if not r["llm_leaf"] and not r["tfidf_leaf"]]

    # KG usage stats
    kg_used = sum(1 for r in results if r.get("llm_kg_facts", 0) > 0)
    avg_kg_facts = (
        sum(r.get("llm_kg_facts", 0) for r in results) / total if total else 0
    )

    # Build report
    lines = []
    lines.append("=" * 70)
    lines.append("LLM ADVISOR vs TF-IDF CLASSIFIER — BENCHMARK REPORT")
    lines.append("=" * 70)
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append(f"Tickets processed: {total}")
    lines.append("")

    lines.append("─" * 70)
    lines.append("SUPPORT GROUP ACCURACY")
    lines.append("─" * 70)
    lines.append(f"{'Metric':<20} {'LLM Advisor':<20} {'TF-IDF Classifier':<20}")
    lines.append(f"{'─'*20} {'─'*20} {'─'*20}")
    lines.append(f"{'Exact Match':<20} {llm_exact}/{total} ({llm_exact*100/total:.1f}%){'':<5} {tfidf_exact}/{total} ({tfidf_exact*100/total:.1f}%)")
    lines.append(f"{'Leaf Match':<20} {llm_leaf}/{total} ({llm_leaf*100/total:.1f}%){'':<5} {tfidf_leaf}/{total} ({tfidf_leaf*100/total:.1f}%)")
    lines.append(f"{'Contains Match':<20} {llm_contains}/{total} ({llm_contains*100/total:.1f}%){'':<5} {tfidf_contains}/{total} ({tfidf_contains*100/total:.1f}%)")
    lines.append("")

    lines.append("─" * 70)
    lines.append("PRIORITY ACCURACY (LLM Advisor)")
    lines.append("─" * 70)
    lines.append(f"  Exact: {priority_exact}/{total} ({priority_exact*100/total:.1f}%)")
    lines.append(f"  Within-1: {priority_within1}/{total} ({priority_within1*100/total:.1f}%)")
    lines.append("")

    lines.append("─" * 70)
    lines.append("HEAD-TO-HEAD COMPARISON (Leaf Match)")
    lines.append("─" * 70)
    lines.append(f"  Both correct:      {len(both_right)} ({len(both_right)*100/total:.1f}%)")
    lines.append(f"  LLM wins:          {len(llm_wins)} ({len(llm_wins)*100/total:.1f}%) — LLM correct, TF-IDF wrong")
    lines.append(f"  TF-IDF wins:       {len(tfidf_wins)} ({len(tfidf_wins)*100/total:.1f}%) — TF-IDF correct, LLM wrong")
    lines.append(f"  Both wrong:        {len(both_wrong)} ({len(both_wrong)*100/total:.1f}%)")
    lines.append("")

    lines.append("─" * 70)
    lines.append("LLM ADVISOR METHOD BREAKDOWN")
    lines.append("─" * 70)
    lines.append(f"  Triage rules:      {llm_triage} tickets")
    lines.append(f"  LLM advisor:       {llm_advisor} tickets")
    lines.append(f"  LLM error/fallback:{llm_error} tickets")
    lines.append("")

    lines.append("─" * 70)
    lines.append("LATENCY (LLM advisor calls only)")
    lines.append("─" * 70)
    lines.append(f"  Average: {avg_latency:.0f} ms")
    lines.append(f"  Min:     {min_latency:.0f} ms")
    lines.append(f"  Max:     {max_latency:.0f} ms")
    lines.append("")

    lines.append("─" * 70)
    lines.append("CONTEXT USAGE")
    lines.append("─" * 70)
    lines.append(f"  Knowledge Graph used: {kg_used}/{total} tickets ({kg_used*100/total:.1f}%)")
    lines.append(f"  Avg KG facts per ticket: {avg_kg_facts:.1f}")
    lines.append("")

    # LLM wins detail
    if llm_wins:
        lines.append("─" * 70)
        lines.append(f"LLM WINS (correct where TF-IDF failed) — {len(llm_wins)} tickets")
        lines.append("─" * 70)
        for r in llm_wins:
            lines.append(f"  {r['ticket_id']}: {r['title'][:50]}")
            lines.append(f"    Actual: {r['actual_group']}")
            lines.append(f"    LLM:    {r['llm_predicted_group']} ✓")
            lines.append(f"    TF-IDF: {r['tfidf_predicted_group']} ✗")
            lines.append(f"    Rationale: {r.get('llm_rationale', 'N/A')[:100]}")
            lines.append("")

    # TF-IDF wins detail
    if tfidf_wins:
        lines.append("─" * 70)
        lines.append(f"TF-IDF WINS (correct where LLM failed) — {len(tfidf_wins)} tickets")
        lines.append("─" * 70)
        for r in tfidf_wins[:10]:  # Limit to 10
            lines.append(f"  {r['ticket_id']}: {r['title'][:50]}")
            lines.append(f"    Actual: {r['actual_group']}")
            lines.append(f"    TF-IDF: {r['tfidf_predicted_group']} ✓")
            lines.append(f"    LLM:    {r['llm_predicted_group']} ✗")
            lines.append(f"    Rationale: {r.get('llm_rationale', 'N/A')[:100]}")
            lines.append("")

    # Both wrong detail
    if both_wrong:
        lines.append("─" * 70)
        lines.append(f"BOTH WRONG — {len(both_wrong)} tickets (hardest cases)")
        lines.append("─" * 70)
        for r in both_wrong[:10]:  # Limit to 10
            lines.append(f"  {r['ticket_id']}: {r['title'][:50]}")
            lines.append(f"    Actual: {r['actual_group']}")
            lines.append(f"    LLM:    {r['llm_predicted_group']}")
            lines.append(f"    TF-IDF: {r['tfidf_predicted_group']}")
            lines.append("")

    lines.append("=" * 70)
    lines.append("CONCLUSION")
    lines.append("=" * 70)
    if len(llm_wins) > len(tfidf_wins):
        lines.append(f"LLM Advisor provides NET VALUE: +{len(llm_wins) - len(tfidf_wins)} tickets correct vs TF-IDF alone.")
        lines.append("Recommendation: INTEGRATE as backup advisor in Feature #4.")
    elif len(llm_wins) == len(tfidf_wins):
        lines.append("LLM Advisor and TF-IDF are roughly equivalent in accuracy.")
        lines.append("LLM adds rationale/explanation value but no accuracy improvement.")
        lines.append("Recommendation: INTEGRATE for rationale value (user can see 'why').")
    else:
        lines.append(f"TF-IDF outperforms LLM Advisor by {len(tfidf_wins) - len(llm_wins)} tickets.")
        lines.append("LLM Advisor does NOT provide accuracy improvement.")
        lines.append("Recommendation: SKIP integration unless rationale value alone justifies the latency cost.")
    lines.append("")

    report_text = "\n".join(lines)

    # Save report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text)

    # Print to console (handle Windows encoding)
    try:
        print(report_text)
    except UnicodeEncodeError:
        print(report_text.encode("ascii", errors="replace").decode("ascii"))
    logger.info("Report saved to %s", REPORT_PATH)
    logger.info("Results saved to %s", RESULTS_PATH)


if __name__ == "__main__":
    asyncio.run(main())