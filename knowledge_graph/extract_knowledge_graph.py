"""
Knowledge Graph Extraction Pipeline
====================================
Reads documents from hive_metastore.embeddings_db.onenote_documentation,
uses Claude Sonnet 4.5 to extract structured knowledge facts,
and stores them in a knowledge graph.

Usage:
    python extract_knowledge_graph.py --limit 10 --dry-run
    python extract_knowledge_graph.py --limit 50
    python extract_knowledge_graph.py --batch-size 5

Requirements:
    pip install -r requirements.txt
"""

import os
import json
import time
import uuid
import argparse
import requests
from typing import Optional
from dotenv import load_dotenv
from databricks import sql as databricks_sql
try:
    from neo4j import GraphDatabase
    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False

from knowledge_graph_store import KnowledgeGraphStore as FileGraphStore

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Databricks config
DATABRICKS_SERVER_HOSTNAME = os.getenv('DATABRICKS_SERVER_HOSTNAME')
DATABRICKS_HTTP_PATH = os.getenv('DATABRICKS_HTTP_PATH')
DATABRICKS_API_KEY = os.getenv('DATABRICKS_API_KEY')
DATABRICKS_SONNET_URL = os.getenv('DATABRICKS_SONNET_4.5_URL')

# Neo4j config (defaults for local Docker instance)
NEO4J_URI = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
NEO4J_USER = os.getenv('NEO4J_USER', 'neo4j')
NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD', 'password')

# ============================================================================
# LLM EXTRACTION PROMPT
# ============================================================================

EXTRACTION_PROMPT_TEMPLATE = """You are a knowledge extraction system for Penn Medicine's IT Service Desk.
Read the following documentation and extract ALL structured knowledge into a JSON array.

For each piece of knowledge, output ONE of these types:

1. PRIORITY_RULE: When a specific condition determines priority level
   {{"type": "priority_rule", "condition": "description of the condition", "priority": "1|2|3|4 or High|Medium|Low", "reason": "why this priority", "systems": ["system names"], "locations": ["location names"]}}

2. ESCALATION: When a condition should be routed to a specific support group
   {{"type": "escalation", "condition": "what triggers escalation", "target_group": "support group name", "urgency": "High|Medium|Low|null", "systems": ["system names"]}}

3. TROUBLESHOOTING_STEP: An ordered step in resolving an issue
   {{"type": "troubleshooting_step", "procedure_name": "name of the procedure", "step_order": 1, "action": "what to do", "if_fails": "what to do if this step fails", "systems": ["system names"]}}

4. SYSTEM_DEPENDENCY: How systems relate to each other
   {{"type": "system_dependency", "system": "system name", "depends_on": "other system", "relationship": "description of dependency"}}

5. CALL_CAPTURE: Information that must be collected from the caller
   {{"type": "call_capture", "scenario": "when to collect this", "required_fields": ["field1", "field2"], "support_group": "where to route", "ticket_type": "IR|SR|null"}}

6. GENERAL_FACT: Any other important operational knowledge
   {{"type": "general_fact", "subject": "what this is about", "predicate": "relationship or action", "object": "target or value", "context": "additional context"}}

RULES:
- Extract EVERY actionable piece of information, no matter how small
- Use exact support group names as written in the document (e.g., "EUS / ATLAS", "LGH\\Epic (LGH)\\Revenue Cycle (LGH)")
- Priority levels: 1=Critical/Enterprise outage, 2=High/Patient care impact, 3=Medium/Standard, 4=Low
- If a document lists multiple scenarios with different priorities, create a separate priority_rule for EACH
- Include system names exactly as mentioned (PennChart, Citrix, VDI, Avaya, etc.)
- Include location names exactly as mentioned (HUP, LGH, PCAM, patient room, OR, etc.)

Document Title: {title}
Section: {section}
Notebook: {notebook}

Content:
{content}

Output ONLY a valid JSON array of extracted knowledge objects. No other text."""


# ============================================================================
# DATABRICKS CLIENT
# ============================================================================

def get_databricks_connection():
    """Create a Databricks SQL connection."""
    return databricks_sql.connect(
        server_hostname=DATABRICKS_SERVER_HOSTNAME,
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_API_KEY
    )


def fetch_documents(limit: int = 10, offset: int = 0, min_length: int = 50):
    """Fetch documents from the onenote_documentation table."""
    query = f"""
        SELECT title, section, notebook, content
        FROM hive_metastore.embeddings_db.onenote_documentation
        WHERE LENGTH(content) >= {min_length}
        ORDER BY notebook, section, title
        LIMIT {limit} OFFSET {offset}
    """
    with get_databricks_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
            return [
                {"title": r[0], "section": r[1], "notebook": r[2], "content": r[3]}
                for r in rows
            ]


def get_document_count(min_length: int = 50) -> int:
    """Get total count of documents meeting minimum length."""
    query = f"""
        SELECT COUNT(*) FROM hive_metastore.embeddings_db.onenote_documentation
        WHERE LENGTH(content) >= {min_length}
    """
    with get_databricks_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchone()[0]


# ============================================================================
# LLM EXTRACTION
# ============================================================================

def call_claude_sonnet(prompt: str, max_tokens: int = 4096, max_retries: int = 3) -> str:
    """Call Claude Sonnet 4.5 via Databricks serving endpoint with retry logic."""
    headers = {
        "Authorization": f"Bearer {DATABRICKS_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1  # Low temperature for consistent extraction
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(DATABRICKS_SONNET_URL, headers=headers, json=payload, timeout=180)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                wait_time = 5 * (3 ** (attempt - 1))  # 5s, 15s, 45s
                print(f"    [RETRY {attempt}/{max_retries}] Connection error, waiting {wait_time}s: {type(e).__name__}")
                time.sleep(wait_time)
            else:
                raise  # Re-raise on final attempt


def extract_knowledge_from_document(doc: dict) -> list[dict]:
    """Extract structured knowledge from a single document using Claude Sonnet 4.5."""
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        title=doc["title"],
        section=doc["section"],
        notebook=doc["notebook"],
        content=doc["content"]
    )

    try:
        response_text = call_claude_sonnet(prompt)

        # Strip markdown code fences if present
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove first and last lines (```json and ```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines)

        facts = json.loads(response_text)

        if not isinstance(facts, list):
            facts = [facts]

        # Add source metadata to each fact
        for fact in facts:
            fact["_source_title"] = doc["title"]
            fact["_source_section"] = doc["section"]
            fact["_source_notebook"] = doc["notebook"]
            fact["_id"] = str(uuid.uuid4())

        return facts

    except json.JSONDecodeError as e:
        print(f"    [WARN] JSON parse error for '{doc['title']}': {e}")
        print(f"    Response preview: {response_text[:200]}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"    [ERROR] API error for '{doc['title']}': {e}")
        return []


# ============================================================================
# NEO4J GRAPH STORAGE (optional — requires Docker)
# ============================================================================

class Neo4jKnowledgeGraphStore:
    """Stores extracted knowledge facts in Neo4j."""

    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def setup_constraints(self):
        """Create uniqueness constraints and indexes for performance."""
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Fact) REQUIRE f.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (s:System) REQUIRE s.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (l:Location) REQUIRE l.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (t:Team) REQUIRE t.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Priority) REQUIRE p.level IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (pr:Procedure) REQUIRE pr.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.title IS UNIQUE",
            "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.type)",
            "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.condition)",
        ]
        with self.driver.session(database="neo4j") as session:
            for constraint in constraints:
                try:
                    session.run(constraint)
                except Exception as e:
                    # Some constraints may already exist
                    pass

    def store_fact(self, fact: dict):
        """Store a single extracted fact in Neo4j with appropriate nodes and relationships."""
        fact_type = fact.get("type", "general_fact")

        with self.driver.session(database="neo4j") as session:
            # Create source document node
            session.run(
                """MERGE (d:Document {title: $title})
                   SET d.section = $section, d.notebook = $notebook""",
                title=fact["_source_title"],
                section=fact["_source_section"],
                notebook=fact["_source_notebook"]
            )

            if fact_type == "priority_rule":
                self._store_priority_rule(session, fact)
            elif fact_type == "escalation":
                self._store_escalation(session, fact)
            elif fact_type == "troubleshooting_step":
                self._store_troubleshooting_step(session, fact)
            elif fact_type == "system_dependency":
                self._store_system_dependency(session, fact)
            elif fact_type == "call_capture":
                self._store_call_capture(session, fact)
            elif fact_type == "general_fact":
                self._store_general_fact(session, fact)

    def _store_priority_rule(self, session, fact: dict):
        """Store a priority rule: Condition -[REQUIRES_PRIORITY]-> Priority."""
        session.run(
            """
            MERGE (f:Fact:PriorityRule {id: $id})
            SET f.condition = $condition,
                f.reason = $reason,
                f.type = 'priority_rule'
            MERGE (p:Priority {level: $priority})
            MERGE (f)-[:REQUIRES_PRIORITY]->(p)
            WITH f
            MATCH (d:Document {title: $source_title})
            MERGE (f)-[:EXTRACTED_FROM]->(d)
            """,
            id=fact["_id"],
            condition=fact.get("condition", ""),
            reason=fact.get("reason", ""),
            priority=str(fact.get("priority", "3")),
            source_title=fact["_source_title"]
        )

        # Link to systems
        for system in fact.get("systems", []):
            if system:
                session.run(
                    """MERGE (s:System {name: $name})
                       WITH s
                       MATCH (f:Fact {id: $fact_id})
                       MERGE (f)-[:INVOLVES_SYSTEM]->(s)""",
                    name=system, fact_id=fact["_id"]
                )

        # Link to locations
        for location in fact.get("locations", []):
            if location:
                session.run(
                    """MERGE (l:Location {name: $name})
                       WITH l
                       MATCH (f:Fact {id: $fact_id})
                       MERGE (f)-[:AT_LOCATION]->(l)""",
                    name=location, fact_id=fact["_id"]
                )

    def _store_escalation(self, session, fact: dict):
        """Store an escalation path: Condition -[ESCALATES_TO]-> Team."""
        session.run(
            """
            MERGE (f:Fact:Escalation {id: $id})
            SET f.condition = $condition,
                f.urgency = $urgency,
                f.type = 'escalation'
            MERGE (t:Team {name: $target_group})
            MERGE (f)-[:ESCALATES_TO]->(t)
            WITH f
            MATCH (d:Document {title: $source_title})
            MERGE (f)-[:EXTRACTED_FROM]->(d)
            """,
            id=fact["_id"],
            condition=fact.get("condition", ""),
            urgency=fact.get("urgency", ""),
            target_group=fact.get("target_group", "Unknown"),
            source_title=fact["_source_title"]
        )

        for system in fact.get("systems", []):
            if system:
                session.run(
                    """MERGE (s:System {name: $name})
                       WITH s
                       MATCH (f:Fact {id: $fact_id})
                       MERGE (f)-[:INVOLVES_SYSTEM]->(s)""",
                    name=system, fact_id=fact["_id"]
                )

    def _store_troubleshooting_step(self, session, fact: dict):
        """Store a troubleshooting step linked to a procedure."""
        session.run(
            """
            MERGE (pr:Procedure {name: $procedure_name})
            MERGE (f:Fact:TroubleshootingStep {id: $id})
            SET f.action = $action,
                f.step_order = $step_order,
                f.if_fails = $if_fails,
                f.type = 'troubleshooting_step'
            MERGE (pr)-[:HAS_STEP {order: $step_order}]->(f)
            WITH f
            MATCH (d:Document {title: $source_title})
            MERGE (f)-[:EXTRACTED_FROM]->(d)
            """,
            id=fact["_id"],
            procedure_name=fact.get("procedure_name", "Unknown Procedure"),
            action=fact.get("action", ""),
            step_order=fact.get("step_order", 0),
            if_fails=fact.get("if_fails", ""),
            source_title=fact["_source_title"]
        )

        for system in fact.get("systems", []):
            if system:
                session.run(
                    """MERGE (s:System {name: $name})
                       WITH s
                       MATCH (f:Fact {id: $fact_id})
                       MERGE (f)-[:INVOLVES_SYSTEM]->(s)""",
                    name=system, fact_id=fact["_id"]
                )

    def _store_system_dependency(self, session, fact: dict):
        """Store a system dependency relationship."""
        session.run(
            """
            MERGE (s1:System {name: $system})
            MERGE (s2:System {name: $depends_on})
            MERGE (s1)-[:DEPENDS_ON {relationship: $relationship, fact_id: $id}]->(s2)
            WITH s1
            MATCH (d:Document {title: $source_title})
            MERGE (d)-[:DOCUMENTS]->(s1)
            """,
            id=fact["_id"],
            system=fact.get("system", "Unknown"),
            depends_on=fact.get("depends_on", "Unknown"),
            relationship=fact.get("relationship", ""),
            source_title=fact["_source_title"]
        )

    def _store_call_capture(self, session, fact: dict):
        """Store call capture requirements."""
        session.run(
            """
            MERGE (f:Fact:CallCapture {id: $id})
            SET f.scenario = $scenario,
                f.required_fields = $required_fields,
                f.ticket_type = $ticket_type,
                f.type = 'call_capture'
            WITH f
            MATCH (d:Document {title: $source_title})
            MERGE (f)-[:EXTRACTED_FROM]->(d)
            """,
            id=fact["_id"],
            scenario=fact.get("scenario", ""),
            required_fields=json.dumps(fact.get("required_fields", [])),
            ticket_type=fact.get("ticket_type", ""),
            source_title=fact["_source_title"]
        )

        target = fact.get("support_group", "")
        if target:
            session.run(
                """MERGE (t:Team {name: $name})
                   WITH t
                   MATCH (f:Fact {id: $fact_id})
                   MERGE (f)-[:ROUTES_TO]->(t)""",
                name=target, fact_id=fact["_id"]
            )

    def _store_general_fact(self, session, fact: dict):
        """Store a general knowledge fact."""
        session.run(
            """
            MERGE (f:Fact:GeneralFact {id: $id})
            SET f.subject = $subject,
                f.predicate = $predicate,
                f.object = $object,
                f.context = $context,
                f.type = 'general_fact'
            WITH f
            MATCH (d:Document {title: $source_title})
            MERGE (f)-[:EXTRACTED_FROM]->(d)
            """,
            id=fact["_id"],
            subject=fact.get("subject", ""),
            predicate=fact.get("predicate", ""),
            object=fact.get("object", ""),
            context=fact.get("context", ""),
            source_title=fact["_source_title"]
        )

    def get_stats(self) -> dict:
        """Get graph statistics."""
        with self.driver.session(database="neo4j") as session:
            result = session.run("""
                MATCH (n) 
                RETURN labels(n) as labels, count(n) as count
                ORDER BY count DESC
            """)
            node_counts = {str(r["labels"]): r["count"] for r in result}

            result = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) as type, count(r) as count
                ORDER BY count DESC
            """)
            edge_counts = {r["type"]: r["count"] for r in result}

            return {"nodes": node_counts, "edges": edge_counts}


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Extract knowledge graph from OneNote documentation")
    parser.add_argument("--limit", type=int, default=10, help="Number of documents to process")
    parser.add_argument("--offset", type=int, default=0, help="Offset for pagination")
    parser.add_argument("--min-length", type=int, default=50, help="Minimum document content length")
    parser.add_argument("--batch-size", type=int, default=5, help="Documents per batch (with delay between)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between LLM calls (seconds)")
    parser.add_argument("--save-every", type=int, default=25, help="Save graph to disk every N documents")
    parser.add_argument("--dry-run", action="store_true", help="Extract but don't store in graph")
    parser.add_argument("--stats-only", action="store_true", help="Only show graph stats")
    args = parser.parse_args()

    # Stats only mode
    if args.stats_only:
        graph_path = os.path.join(os.path.dirname(__file__), 'output', 'knowledge_graph.json')
        print(f"\n  Loading graph from {graph_path}...")
        store = FileGraphStore(graph_path)
        stats = store.get_stats()
        print("\n  Graph Statistics:")
        print(f"    Total nodes: {stats['total_nodes']}")
        print(f"    Total edges: {stats['total_edges']}")
        print(f"    Node types: {json.dumps(stats['node_types'], indent=6)}")
        print(f"    Edge types: {json.dumps(stats['edge_types'], indent=6)}")
        return

    # Initialize graph store
    store = None
    if not args.dry_run:
        graph_path = os.path.join(os.path.dirname(__file__), 'output', 'knowledge_graph.json')
        print(f"\n  Initializing graph store at {graph_path}...")
        store = FileGraphStore(graph_path)
        print("  Graph store ready.")

    # Fetch documents
    total_docs = get_document_count(args.min_length)
    print(f"\n  Total eligible documents: {total_docs}")
    print(f"  Processing: {args.limit} documents starting at offset {args.offset}")
    print(f"  LLM endpoint: {DATABRICKS_SONNET_URL}")
    print(f"  Dry run: {args.dry_run}")
    print()

    docs = fetch_documents(limit=args.limit, offset=args.offset, min_length=args.min_length)
    print(f"  Fetched {len(docs)} documents from Databricks.")

    # Determine which documents are already processed (skip-existing logic)
    already_processed = set()
    if store:
        already_processed = set(store.list_all("Document"))
    skipped_count = sum(1 for doc in docs if doc["title"] in already_processed)
    print(f"  Already in graph: {skipped_count} documents (will be skipped)")
    print(f"  To process: {len(docs) - skipped_count} new documents\n")

    # Process documents
    all_facts = []
    total_extracted = 0
    errors = 0
    processed_count = 0

    for i, doc in enumerate(docs):
        # Skip documents already in the graph
        if doc["title"] in already_processed:
            print(f"  [{i+1}/{len(docs)}] [SKIP] Already processed: {doc['title'][:60]}")
            continue

        print(f"  [{i+1}/{len(docs)}] Processing: {doc['title'][:60]}...")

        facts = extract_knowledge_from_document(doc)

        if facts:
            print(f"           Extracted {len(facts)} facts")
            all_facts.extend(facts)
            total_extracted += len(facts)

            # Store in graph
            if store:
                for fact in facts:
                    try:
                        store.store_fact(fact)
                    except Exception as e:
                        print(f"           [WARN] Store error: {e}")
                        errors += 1
        else:
            print(f"           No facts extracted (or error)")
            errors += 1

        processed_count += 1

        # Periodic save to avoid losing progress
        if store and processed_count > 0 and processed_count % args.save_every == 0:
            store.save()
            stats = store.get_stats()
            print(f"           [SAVED] Graph checkpoint: {stats['total_nodes']} nodes, {stats['total_edges']} edges")

        # Rate limiting
        if i < len(docs) - 1:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"  EXTRACTION COMPLETE")
    print(f"{'='*60}")
    print(f"  Documents processed: {processed_count} (skipped {skipped_count} already in graph)")
    print(f"  Total facts extracted: {total_extracted}")
    print(f"  Errors: {errors}")
    print(f"  Avg facts per document: {total_extracted / max(processed_count, 1):.1f}")

    # Save extracted facts to JSON for inspection
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, 'extracted_knowledge_facts.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_facts, f, indent=2, ensure_ascii=False)
    print(f"  Facts saved to: {output_path}")

    # Show graph stats and save
    if store:
        store.save()
        stats = store.get_stats()
        print(f"\n  Graph Stats:")
        print(f"    Total nodes: {stats['total_nodes']}")
        print(f"    Total edges: {stats['total_edges']}")
        print(f"    Node types: {stats['node_types']}")
        print(f"    Edge types: {stats['edge_types']}")


if __name__ == '__main__':
    main()