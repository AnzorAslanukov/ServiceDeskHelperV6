# Service Desk Helper

An AI-powered IT service desk assistant built for Penn Medicine (UPHS). Provides intelligent ticket search, Q&A chatbot, automated assignment recommendations, bulk ticket processing, and turnover email generation.

## Features

| # | Feature | Description |
|---|---------|-------------|
| 1 | **Enhanced Search** | 4-mode ticket search: field match, description search, semantic (vector) search, and ticket similarity |
| 2 | **Q&A Chatbot** | RAG-powered chatbot using OneNote documentation + similar tickets for context-aware troubleshooting guidance |
| 3 | **Assignment Recommendation** | TF-IDF classifier (80.7% accuracy) that predicts the correct support group and priority for incoming tickets |
| 4 | **Bulk Assignment** | Multi-user real-time WebSocket tool for processing the Service Desk Validation queue in bulk |
| 5 | **Turnover Email** | Generates SEV turnover email drafts summarizing P1/P2 incidents and upcoming change requests |

## Tech Stack

- **Backend:** Python 3.11+, FastAPI (REST + WebSocket)
- **Frontend:** Jinja2 + HTMX (server-side rendered, no build toolchain)
- **ML Model:** TF-IDF + SGDClassifier (scikit-learn)
- **External APIs:** Athena ITSM (ticketing), Databricks on Azure (LLM, embeddings, SQL warehouse)
- **Testing:** pytest + pytest-asyncio (287+ unit tests)

## Architecture

```
Routers (HTTP/WS handlers)
    → Services (business logic)
        → Clients (I/O boundary: Athena API, Databricks API)
```

- **Dependency injection** via FastAPI `Depends()` with constructor injection
- **Feature #4 isolation:** All bulk assignment code lives in `feature4/` (separate from core `src/`)
- **Frontend:** HTMX for Features 1/2/3/5; vanilla JS WebSocket for Feature 4

## Project Structure

```
service_desk_helper/
├── src/                        # Core application code
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Pydantic settings (env vars)
│   ├── dependencies.py         # DI factory functions
│   ├── clients/                # External API clients (Athena, Databricks)
│   ├── models/                 # Pydantic request/response models
│   ├── routers/                # API route handlers
│   └── services/               # Business logic layer
├── frontend/                   # Jinja2 templates + static assets
│   ├── static/css/             # Stylesheets
│   ├── static/js/              # Client-side JavaScript
│   ├── static/img/             # Images (logo, favicon)
│   └── templates/              # HTML templates (base, search, chat, assignment)
├── feature4/                   # Bulk Assignment (isolated module)
│   ├── router.py               # REST + WebSocket endpoints
│   ├── service.py              # Bulk assignment business logic
│   ├── models.py               # Feature-specific models
│   ├── websocket/              # WebSocket manager + events
│   ├── templates/              # Bulk UI template
│   ├── static/                 # Bulk-specific CSS/JS
│   └── tests/                  # Feature #4 tests
├── tests/                      # Core unit + integration tests
│   ├── test_search/
│   ├── test_chat/
│   ├── test_assignment/
│   └── test_turnover/
├── exploration/                # Research & benchmarking scripts
├── ticket_classifier/          # ML model training artifacts
│   ├── train_improved_classifier.py
│   └── improved_metrics.json
├── cli.py                      # CLI testing tool
├── CLI_README.md               # CLI documentation
├── requirements.txt            # Python dependencies
├── pytest.ini                  # Test configuration
└── .env.example                # Environment variable template
```

## Getting Started

### Prerequisites

- Python 3.11+
- Access to Athena ITSM API (OAuth2 credentials)
- Access to Databricks workspace (PAT token, serving endpoints, SQL warehouse)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/service_desk_helper.git
cd service_desk_helper

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your actual credentials
```

### Running the Server

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

The web UI will be available at `http://localhost:8000/ui/`.

### Running Tests

```bash
# Unit tests only (fast, no external APIs needed)
pytest

# Include integration tests (requires real API access)
pytest -m integration
```

### Using the CLI

```bash
# Search by field
python cli.py search field --field title --value "printer jam"

# Semantic search
python cli.py search semantic --query "user cannot log into PennChart"

# Chat
python cli.py chat -m "How do I reset a PennChart password?"

# Assignment recommendation
python cli.py assign IR1234567

# Turnover email
python cli.py turnover --sender "John Smith" --receiver "Jane Doe"
```

See [CLI_README.md](CLI_README.md) for full CLI documentation.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/search/field` | Search tickets by field value |
| POST | `/search/description` | Search tickets by description text |
| POST | `/search/semantic` | Semantic vector search |
| GET | `/search/similar/{id}` | Find similar tickets |
| POST | `/chat` | Send chat message (RAG pipeline) |
| POST | `/chat/reset` | Reset chat session |
| GET | `/chat/history/{session_id}` | Get chat history |
| POST | `/assignment/{ticket_id}` | Get assignment recommendation |
| POST | `/turnover/generate` | Generate turnover email draft |
| WS | `/bulk/ws/{user_id}` | Bulk assignment WebSocket |

## ML Model

The assignment recommendation system uses a TF-IDF + SGDClassifier trained on 200K historical tickets:

- **Top-1 accuracy:** 80.7%
- **Top-3 accuracy:** 91.1%
- **Top-5 accuracy:** 93.3%
- **Support groups:** 226 classes
- **Features:** TF-IDF (50K features, bigrams) + one-hot categoricals (TicketType, Location, Classification, Source)

The model file (`ticket_classifier/improved_classifier.pkl`, ~90MB) is not included in the repository. To generate it:

```bash
python ticket_classifier/train_improved_classifier.py
```

This requires access to the Databricks SQL warehouse with the `prepared.ticketing.athena_tickets` view.

## Environment Variables

See [.env.example](.env.example) for all required configuration. Key integrations:

- **Athena API:** OAuth2 credentials, endpoint URLs, support group GUIDs
- **Databricks:** PAT token, LLM endpoint (Claude Sonnet 4.5), embedding endpoint (GTE-Large-EN), SQL warehouse

## License

Internal use only — Penn Medicine IS Operations.