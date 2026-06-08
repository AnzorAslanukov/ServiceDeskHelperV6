# Knowledge Graph — Penn Medicine IT Service Desk Documentation

## Overview

This is a **Knowledge Graph extraction and query system** that converts Penn Medicine IT Service Desk OneNote documentation into a structured, queryable graph of relational facts.

An LLM (Claude Sonnet 4.5 via Databricks) reads through documentation records and extracts structured knowledge facts that are relationally connected to other facts in a graph database.

## Current State

- **50 documents processed** out of 6,709 total eligible
- **720 facts extracted** (average 14.4 facts per document)
- **984 nodes** and **1,609 edges** stored in `output/knowledge_graph.json`
- Node distribution: 396 GeneralFacts, 134 TroubleshootingSteps, 113 Systems, 88 Escalations, 58 Procedures, 50 PriorityRules, 49 Documents, 46 CallCaptures, 39 Teams, 8 Locations, 3 Priority levels

## Architecture

```
OneNote Docs (Databricks SQL)
        │
        ▼
┌─────────────────────────┐
│  extract_knowledge_graph │  ← Claude Sonnet 4.5 extracts structured JSON facts
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│  knowledge_graph_store   │  ← NetworkX + JSON persistence (file-based)
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│  query_knowledge_graph   │  ← CLI query interface
└─────────────────────────┘
```

## Data Source

- **Table**: `hive_metastore.embeddings_db.onenote_documentation`
- **6,709 pages** from two OneNote notebooks:
  - `uphs_notebook` (4,507 docs) — UPHS/Penn Medicine procedures
  - `lgh_notebook` (2,202 docs) — Lancaster General Health procedures
- **Columns**: title, content, notebook, section, embeddings (1024-dim GTE-Large-EN)

## Fact Types Extracted

| Type | Description | Example |
|------|-------------|---------|
| `priority_rule` | Conditions determining ticket priority | "Enterprise outage → Priority 1" |
| `escalation` | Routing conditions to support groups | "Citrix VDI issue at LGH → LGH Telecom" |
| `troubleshooting_step` | Ordered resolution steps | "Step 1: Verify user credentials" |
| `system_dependency` | How IT systems relate | "PennChart depends on Citrix" |
| `call_capture` | Required caller information | "Collect: username, error message, location" |
| `general_fact` | Other operational knowledge | "LGH uses separate Active Directory" |

## Graph Schema

**Node Types**: Document, System, Location, Team, Priority, Procedure, PriorityRule, Escalation, TroubleshootingStep, CallCapture, GeneralFact

**Relationships**: REQUIRES_PRIORITY, ESCALATES_TO, INVOLVES_SYSTEM, AT_LOCATION, EXTRACTED_FROM, HAS_STEP, DEPENDS_ON, ROUTES_TO, DOCUMENTS

## Usage

### Extract knowledge from documents
```bash
# Dry run (extract but don't store)
python extract_knowledge_graph.py --limit 10 --dry-run

# Process 50 documents
python extract_knowledge_graph.py --limit 50

# Resume from offset
python extract_knowledge_graph.py --limit 100 --offset 50

# Show graph stats
python extract_knowledge_graph.py --stats-only
```

### Query the knowledge graph
```bash
# Graph statistics
python query_knowledge_graph.py --stats

# Query by system
python query_knowledge_graph.py --system "PennChart"

# Query by team/support group
python query_knowledge_graph.py --team "LGH Telecom"

# Query by priority level
python query_knowledge_graph.py --priority 1

# Get procedure steps
python query_knowledge_graph.py --procedure "Voicemail issues"

# List all entities of a type
python query_knowledge_graph.py --list systems
python query_knowledge_graph.py --list teams
python query_knowledge_graph.py --list procedures

# Full-text search
python query_knowledge_graph.py --search "password reset"
```

### Explore source documentation
```bash
python sample_onenote_docs.py
```

## Storage

### File-based (current — no infrastructure needed)
The graph is stored as a JSON file using NetworkX for in-memory operations. No Docker or database server required.

### Neo4j (optional — for larger scale)
A `docker-compose.yml` is included for running Neo4j locally:
```bash
docker compose up -d
# Neo4j Browser: http://localhost:7474
# Bolt: bolt://localhost:7687
# Auth: neo4j/password
```

The extraction script has a full Neo4j storage class (currently unused) that can be re-enabled if Docker is available.

## Environment Variables Required

See `.env.example` for the full list. Key variables:
- `DATABRICKS_API_KEY` — Databricks Personal Access Token
- `DATABRICKS_SERVER_HOSTNAME` — Databricks workspace hostname
- `DATABRICKS_HTTP_PATH` — SQL Warehouse HTTP path
- `DATABRICKS_SONNET_4.5_URL` — Claude Sonnet 4.5 serving endpoint URL
- `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` — (optional) Neo4j connection

## Dependencies

```
pip install -r requirements.txt