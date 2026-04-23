# Service Desk Helper — CLI Testing Tool

A command-line interface for testing all implemented features without running the FastAPI server. The CLI calls the service layer directly and outputs results to the terminal.

## Prerequisites

1. **Python 3.11+** installed
2. **Dependencies installed:**
   ```
   pip install -r requirements.txt
   ```
3. **`.env` file configured** in the project root with all required credentials (Athena API, Databricks API). This is the same `.env` used by the FastAPI server.

## Usage

```
python cli.py <command> [subcommand] [options]
```

### Global Options

| Option   | Description                                      |
|----------|--------------------------------------------------|
| `--help` | Show help for any command or subcommand           |
| `--json` | Output raw JSON instead of human-readable format  |

---

## Feature #1: Enhanced Ticket Search

### Search by Field

Search tickets by a specific Athena field value.

```bash
python cli.py search field --field contactMethod --value "215-555-1234"
python cli.py search field --field supportGroup --value "Service Desk" --operator contains
python cli.py search field --field title --value "VPN" --operator contains --type servicerequest
```

| Option       | Required | Default    | Description                                           |
|--------------|----------|------------|-------------------------------------------------------|
| `--field`    | Yes      | —          | Athena field name (e.g., `contactMethod`, `title`)    |
| `--value`    | Yes      | —          | Value to match                                        |
| `--type`     | No       | `incident` | Ticket type: `incident` or `servicerequest`           |
| `--operator` | No       | `eq`       | Filter operator: `eq`, `ne`, `contains`, `like`, `gt`, `lt` |

### Search by Description

Search tickets by substring match in the description field.

```bash
python cli.py search description --text "printer not working"
python cli.py search description --text "password reset" --type servicerequest
```

| Option   | Required | Default    | Description                                 |
|----------|----------|------------|---------------------------------------------|
| `--text` | Yes      | —          | Text to search for in ticket descriptions   |
| `--type` | No       | `incident` | Ticket type: `incident` or `servicerequest` |

### Semantic Search

Natural language search using AI embeddings. Searches both historical tickets and knowledge base documentation.

```bash
python cli.py search semantic --query "user cannot log into VPN after password reset"
python cli.py search semantic --query "PennChart access issue" --top-k 5
```

| Option    | Required | Default | Description                              |
|-----------|----------|---------|------------------------------------------|
| `--query` | Yes      | —       | Natural language description of the issue |
| `--top-k` | No       | `10`    | Number of similar tickets to return       |

### Find Similar Tickets

Find tickets similar to a given ticket ID using pre-computed embeddings.

```bash
python cli.py search similar --ticket-id IR1959493
python cli.py search similar --ticket-id IR1959493 --top-k 5
```

| Option        | Required | Default | Description                         |
|---------------|----------|---------|-------------------------------------|
| `--ticket-id` | Yes      | —       | Ticket ID (e.g., `IR1959493`)       |
| `--top-k`     | No       | `10`    | Number of similar tickets to return |

---

## Feature #2: Q&A Chatbot

### Send a Message

Ask the chatbot a question. It uses RAG (Retrieval-Augmented Generation) to find relevant documentation and similar tickets, then generates an AI response.

```bash
python cli.py chat --message "How do I reset a user's PennChart password?"
python cli.py chat -m "What about for VPN users?"
```

| Option         | Required | Default | Description                                    |
|----------------|----------|---------|------------------------------------------------|
| `--message`/`-m` | Yes   | —       | Your question or message                       |
| `--session-id`/`-s` | No | (new)   | Session ID for continuing a conversation       |

#### Multi-turn Conversations

The chatbot maintains conversation history per session. After the first message, the CLI prints a session ID. Pass it back to continue the conversation:

```bash
# First message — note the session ID in the output
python cli.py chat -m "How do I reset a user's PennChart password?"

# Follow-up using the session ID from the previous response
python cli.py chat -m "What if they're locked out completely?" -s <session-id>
```

> **Note:** Sessions are stored in memory within the chatbot service singleton. They persist across multiple CLI invocations within the same Python process, but since each `python cli.py` call is a separate process, multi-turn conversations via the CLI require the FastAPI server to be running. For single-turn Q&A, the CLI works standalone.

### View Chat History

```bash
python cli.py chat history --session-id <session-id>
```

### Reset a Session

```bash
python cli.py chat reset --session-id <session-id>
```

---

## Feature #3: Ticket Assignment Recommendation

Analyze a ticket and get an AI-recommended support group assignment and priority level.

```bash
python cli.py assign IR1959493
python cli.py assign SR1959584
```

The ticket ID is a positional argument (no flag needed). Supports `IR` (incident) and `SR` (service request) prefixes.

**Output includes:**
- Ticket details (title, status, priority, current support group, etc.)
- AI recommendation (support group name, GUID, priority, rationale)
- Sources used (documentation articles and similar tickets)

---

## Feature #5: Turnover Email Draft Generator

Generate a copy-paste-ready SEV turnover email. Queries Athena for active P1/P2 incidents and upcoming change requests.

```bash
python cli.py turnover --sender "John Smith" --receiver "Jane Doe"
python cli.py turnover --sender "John Smith" --receiver "Jane Doe" --notes "Verbal handoff completed" --escalation-notes "IR1234567 escalated to ISOD" --hours-lookahead 48
```

| Option               | Required | Default | Description                                          |
|----------------------|----------|---------|------------------------------------------------------|
| `--sender`           | Yes      | —       | Name of the person sending the turnover              |
| `--receiver`         | Yes      | —       | Name of the agent taking over the shift              |
| `--notes`            | No       | (empty) | Verbal turnover notes                                |
| `--escalation-notes` | No       | (empty) | Escalation notes (Manager/ISOD/ISMT)                |
| `--voicemail-notes`  | No       | (empty) | On-call analyst voicemail notes                      |
| `--hours-lookahead`  | No       | `24`    | Hours ahead to look for upcoming change requests     |

**Output includes:**
- Email metadata (To, CC, Subject)
- Ticket summary counts
- Full email body ready to copy-paste into Outlook

---

## JSON Output

Add `--json` before the command to get raw JSON output (useful for piping to other tools):

```bash
python cli.py --json search field --field title --value "VPN" --operator contains
python cli.py --json chat -m "How do I fix a printer issue?"
python cli.py --json assign IR1959493
python cli.py --json turnover --sender "John" --receiver "Jane"
```

---

## Reassign a Ticket's Support Group

Change the support group (tier queue) assignment for a ticket. Supports both name-based lookup (resolved via the Athena enum API) and direct GUID specification.

```bash
# Reassign by support group name
python cli.py reassign IR1959493 --support-group "Service Desk\Validation"
python cli.py reassign IR1959493 -g "PennChart\ED"

# Reassign by direct GUID (skips name resolution)
python cli.py reassign IR1959493 --tier-queue-guid "1a59b3b9-84a3-13ce-f50c-79b8a99f5531"

# Also change priority
python cli.py reassign IR1959493 --support-group "PennChart" --priority 3
python cli.py reassign SR1959584 --support-group "Service Desk" --priority Medium

# Skip confirmation prompt
python cli.py reassign IR1959493 --support-group "Service Desk\Validation" --yes
```

| Option               | Required         | Default | Description                                                                 |
|----------------------|------------------|---------|-----------------------------------------------------------------------------|
| `ticket_id`          | Yes (positional) | —       | Ticket ID (e.g., `IR1959493` or `SR1959584`)                               |
| `--support-group`/`-g` | One of these two | —    | Target support group name (resolved via Athena enum API)                    |
| `--tier-queue-guid`  | is required       | —       | Target tier queue GUID directly (skips name resolution)                     |
| `--priority`/`-p`    | No               | (none)  | New priority. IR: integer (1–9). SR: text (Low/Medium/High/Immediate)      |
| `--yes`/`-y`         | No               | `false` | Skip the confirmation prompt                                                |

**Workflow:**
1. Fetches the ticket from Athena to get its `entityId` and current support group
2. If `--support-group` is used, resolves the name to a GUID via the Athena enum tree (using the correct IR or SR enum based on ticket type)
3. Shows current state and proposed change, then asks for confirmation
4. Updates the ticket in Athena via PUT
5. Displays the result

> **Note:** IR and SR tickets use different GUIDs for the same support group names. The command automatically selects the correct enum based on the ticket ID prefix.

---

## Feature #4: Bulk Assignment

Feature #4 (Bulk Ticket Recommendation and Assignment) provides queue management, batch AI recommendations, and bulk ticket assignment.

### Fetch Validation Queue

```bash
python cli.py bulk queue
python cli.py bulk queue --queue-name "Service Desk"
```

### Generate AI Recommendations

```bash
python cli.py bulk recommend --ticket-ids IR10001,SR20001,IR10002
```

### Assign a Ticket (Low-Level)

```bash
python cli.py bulk assign --ticket-id IR10001 --entity-id <GUID> --tier-queue-guid <GUID>
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError` | Run from the project root directory. Ensure `pip install -r requirements.txt` has been run. |
| `ValidationError` on startup | Check that your `.env` file has all required variables (see `src/config.py` for the full list). |
| `401 Unauthorized` | Your Athena or Databricks credentials may be expired. Update the `.env` file. |
| `Connection refused` | Ensure you have network access to the Athena API and Databricks endpoints (may require VPN). |
| Timeout errors | The Athena API or Databricks SQL warehouse may be slow. Try again or check service status. |