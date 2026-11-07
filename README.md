# ma-rag: Multi-Agent RAG over AI-Company Earnings (Azure)

A Multi-Agent RAG (Retrieval-Augmented Generation) system built with Azure services, featuring a complete data pipeline, multi-agent collaboration using LangGraph, and FastAPI backend. It answers questions about the earnings of eight AI companies (NVIDIA, AMD, Broadcom, Intel, Micron, Microsoft, Alphabet and Meta) from their earnings releases, call transcripts and a cross-company comparison workbook.

The code is adapted from [`multiagent-rag`](https://github.com/dukewillbe185/multiagent-rag) by Duke Wang ([@dukewillbe185](https://github.com/dukewillbe185)), called "the reference" below; see [Credits](#credits). It keeps that project's architecture: the 4-agent LangGraph workflow, the A2A Excel Narrator Agent, the MCP server and the FastAPI backend. The reference's sample data (`doc.pdf`, an insurance product disclosure statement, and `data/courses.xlsx`) is replaced by our corpus in `datasets/`. See [Our Data](#our-data) and [Changes from the Reference](#changes-from-the-reference).

**Setting up Azure:** [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md) walks through the Azure portal step by step, gives a `.env` template, and ends with `python scripts/check_azure.py`, which tests every setting with one small real call per service.

**Evaluating:** [`docs/EVALUATION.md`](docs/EVALUATION.md) explains the two evals and how to run them. Both use one golden set of 148 verified questions (`evaluation/golden_set.yaml`), from easy to hard: every indexed file is required by at least three of them, 33 need facts from two or more files, and 48 are vertical questions, which read one workbook column down many rows (the case a row-by-row index splits into many passages). `scripts/eval_retrieval.py` checks whether search brings back the passages that hold each answer. `scripts/eval_answers.py` checks the answers themselves: exact figures, an LLM judge (RAGAS), guardrail checks, follow-up conversations and an XLSX ablation.

## Table of Contents

- [Overview](#overview)
- [Our Data](#our-data)
- [Changes from the Reference](#changes-from-the-reference)
- [Architecture](#architecture)
- [MCP & A2A Protocol Integration](#mcp--a2a-protocol-integration)
- [Features](#features)
- [Technology Stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [CLI Interface](#cli-interface)
  - [FastAPI Server](#fastapi-server)
- [API Documentation](#api-documentation)
- [Project Structure](#project-structure)
- [Security Considerations](#security-considerations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [Credits](#credits)

## Overview

This project implements a RAG system that:
1. Extracts text from PDF documents using Azure AI Document Intelligence (in 2-page pieces on the Free tier)
2. Extracts text from HTML and DOCX press releases and transcripts locally, with no Azure calls
3. Chunks text using LangChain's RecursiveCharacterTextSplitter
4. Generates embeddings using Azure AI Foundry (text-embedding-ada-002)
5. Indexes documents in Azure AI Search with hybrid search capabilities, plus company, ticker and period fields for filtering
6. Answers questions using a 4-agent workflow powered by LangGraph and GPT-4
7. Also ingests XLSX/CSV spreadsheets through a standalone Excel Narrator Agent, reached over the [A2A protocol](#mcp--a2a-protocol-integration), which narrates each sheet into natural-language text before it enters the same chunk → embed → index pipeline

## Our Data

All data lives in `datasets/`; each folder has a `README.md` or `MANIFEST.md` with sources, sizes, checksums and licences. The RAG system indexes the **AI Earnings Desk** corpus (`datasets/ai-earnings-desk/`), following the plan in [`INDEXING_PLAN.md`](datasets/ai-earnings-desk/INDEXING_PLAN.md):

| Tier | What | Files | Chunks (plan estimate / offline test) | Document Intelligence pages |
|---|---|---|---|---|
| 1. Core | The comparison workbook, Microsoft's metrics workbook, and each company's results documents for calendar Q2 2026: press releases, CFO commentary or prepared remarks, and 5 call transcripts | 15 | 673 / 772 | 124 |
| 2. History | The same results document for CY2025 Q3 to CY2026 Q1, all 8 companies | 24 | 642 / 716 | 133 |

The offline test ran the real extractors and chunker, with `pypdf` text standing in for Document Intelligence, so real PDF chunk counts will differ a little. Both tiers fit the Free tier: about 1,500 chunks of at most about 22 KB each is about 33 MB of the 50 MiB index limit, and 257 of the 500 Document Intelligence pages a month.

The file list, with each file's company, ticker, document kind, fiscal period and calendar quarter, is [`index_selection.csv`](datasets/ai-earnings-desk/index_selection.csv). `python main.py ingest` reads it and stores those attributes with every chunk; `python main.py process <file>` does the same for any file listed there.

How each file type gets in:

| Type | Route | Notes |
|---|---|---|
| `.xlsx` | A2A Excel Narrator Agent (unchanged from the reference) | Needs row 1 = header and column A = subject. `comparison/ai_earnings_comparison.xlsx` (13 sheets, 259 rows) is built that way. Microsoft's metrics workbook isn't, so `datasets/ai-earnings-desk/scripts/prepare_msft_metrics.py` writes a reshaped copy to `datasets/ai-earnings-desk/prepared/`, and `ingest` uses that copy. |
| `.pdf` | Azure Document Intelligence `prebuilt-layout` | The Free (F0) tier reads only 2 pages per request, so PDFs are sent in 2-page pieces (`AZURE_DOCUMENT_INTELLIGENCE_PAGES_PER_REQUEST`). |
| `.html`, `.docx` | Local text extraction (`src/data_pipeline/local_extractor.py`) | No Azure calls and no page quota. For HTML, the page title and the `<article>` text (the press release) are kept and the site navigation is dropped. |

The comparison workbook is computed from SEC XBRL data; its rules and validation are in `datasets/ai-earnings-desk/comparison/README.md`. Segment figures such as NVIDIA's Data Center revenue are only in the companies' own documents.

The other dataset kits, in `datasets/archive/` (`dhl-earnings-desk`, `nem-gridwatch`, `otp-insight`, `portpulse`, `safeguardwatch`, `threatlens`, `uhc-finance-lens`, `uk-powerdesk`), each have a narrator-shaped `<slug>_clean.xlsx` that can be indexed with `python main.py process datasets/archive/<slug>/<slug>_clean.xlsx`. Put them in their own index (set `AZURE_SEARCH_INDEX_NAME`) so their chunks don't mix with the earnings corpus.

> **Not in this repository:** the downloaded documents (SEC filings, XBRL company facts and IR files) and the prepared MSFT copy. `INDEX.csv` and each company's `MANIFEST.md` list every file's source URL and SHA-256. The IR files and the prepared copy are © each company, so don't republish them. The DHL investor-relations data and the WHO GHED data may not be redistributed either, so `dhl-earnings-desk` and `uhc-finance-lens` hold only their README and cleaning script; each README says where to download the data.

## Changes from the Reference

The architecture and the code are the reference's. The differences:

- **Data:** `doc.pdf`, `data/courses.xlsx` and `scripts/make_sample_xlsx.py` were not copied. Examples, the guardrail's example question, the A2A agent card and these docs use our corpus.
- **Index:** a new index, `ai-earnings-desk` (the default `AZURE_SEARCH_INDEX_NAME`), with six more filterable and facetable fields: `company`, `ticker`, `doc_kind`, `file_type`, `fiscal_period` and `calendar_quarter`. For example, `AzureSearchRetriever().retrieve_with_filter(question, "company eq 'NVIDIA'")`.
- **HTML and DOCX:** a new local extractor. `main.py process` sends `.html`, `.htm` and `.docx` files to it.
- **PDFs on the Free tier:** `DocumentExtractor` splits PDFs into pieces of `AZURE_DOCUMENT_INTELLIGENCE_PAGES_PER_REQUEST` pages (default 2; 0 sends the whole file). New dependencies: `pypdf` and `defusedxml`.
- **`main.py ingest`:** indexes the planned selection (`--tier 1|2|all`, `--types pdf,html,docx,xlsx`, `--dry-run`).
- **Guardrail agent:** its prompt describes the indexed corpus, checks for prompt injection first, and no longer rejects a question because its date (such as 2026) looks like the future, or because it names a fiscal period ("Q2 FY27") or uses tickers and abbreviations. It runs at temperature 0, whatever `LLM_TEMPERATURE` says: at 0.7 the same legitimate question sometimes passed and sometimes was rejected. An unsafe rejection's reason always starts with "Unsafe:". When Azure OpenAI's own content filter rejects the guardrail's call, the question is treated as unsafe instead of being let through; other guardrail errors still let it through ("fail open"). With `GUARDRAIL_ENABLED=false` the guardrail makes no LLM call, and the API answers instead of rejecting.
- **Follow-up questions:** from the second turn of a session, the retrieval agent rewrites a follow-up ("And a year earlier?") into a standalone search query, using the previous question and answer for context. Before, it searched with the bare follow-up and found nothing useful.
- **Fixes:** `main.py query` passes the guardrail settings; with the guardrail disabled, `query()` no longer swaps a real answer for a rejection message; `requirements.txt` moves to LangChain 0.3, LangGraph 0.6 and FastAPI 0.115+; and the Application Insights logger sends nothing while the connection string is the `.env.example` placeholder.
- **API:** `/api/v1/query` also returns `guardrail_reason` (why the guardrail passed or rejected the question; for a guardrail error, only the error type) and `retrieval_query` (what was searched, the rewritten query for a follow-up). Both are model-written, so they go through the output Content Safety check with the answer, and come back null if it flags them. The guardrail's verdict is read more robustly: a verdict wrapped in a code block or with extra text still counts, and "unsafe" always wins.
- **New tools:** `scripts/check_azure.py` with [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md), and the two evals (`evaluation/`, `scripts/eval_retrieval.py`, `scripts/eval_answers.py`, `tests/`; see [`docs/EVALUATION.md`](docs/EVALUATION.md)). The retriever gained `search()` with hybrid, vector and keyword modes and an optional filter; `retrieve()` works as before.
- **Unchanged on purpose:** the intent and answer agents and their prompts, the other API routes (upload still accepts PDFs only), the MCP tools and the A2A code.
- **Not copied:** `outputs/` (the reference's slides and diagrams) and `docs/superpowers/` (the reference's design notes).

## Architecture

### Multi-Agent Workflow

The system uses **LangGraph** to orchestrate a 4-agent workflow with guardrails:

```
User Question
     ↓
[Guardrail Agent] → Validates question (relevant/irrelevant/unsafe)
     ↓ (if passed)
[Supervisor Retrieval Agent]
     ↓ (retrieve relevant chunks)
[Intent Identifier Agent]
     ↓ (identify user intent)
[Answer Generator Agent] → Uses conversation memory
     ↓
Final Answer
```

**Agent Roles:**
1. **Guardrail Agent** - Validates questions before processing (filters irrelevant/unsafe queries)
2. **Supervisor Retrieval Agent** - Retrieves relevant document chunks from Azure AI Search
3. **Intent Identifier Agent** - Identifies user's question intent (definition, explanation, etc.)
4. **Answer Generator Agent** - Generates comprehensive answer using retrieved context and conversation history

### Data Pipeline

```
PDF                              HTML / DOCX                XLSX / CSV
 ↓                                ↓                          ↓
[Azure AI Document Intelligence] [Local text extraction]    [A2A Excel Narrator Agent]
 (2-page pieces on F0)            (no Azure calls)           (one sentence per row)
      ↘                           ↓                          ↙
[LangChain Text Splitter] → Create Chunks (1200 chars, 20% overlap)
     ↓
[Azure AI Foundry Embedding] → Generate Vectors (1536-dim)
     ↓
[Azure AI Search] → Index with Hybrid Search (Vector + Keyword),
                    plus company / ticker / period fields from index_selection.csv
```

## MCP & A2A Protocol Integration

This project layers two agent-interoperability protocols on top of the existing LangGraph workflow, to teach three distinct, complementary patterns for connecting agents to the world.

### Three-Layer Architecture

| Layer | Direction | Role in this project |
|-------|-----------|-----------------------|
| **LangGraph** | in-process | Orchestrates the existing 4-agent query workflow (guardrail → retrieval → intent → answer) inside a single Python process. |
| **MCP** (Model Context Protocol) | agent ↔ tools/resources ("vertical") | This RAG system is exposed **as an MCP server**. Any MCP client (Claude Desktop, the MCP Inspector, a custom client) can call it with zero custom integration code. |
| **A2A** (Agent2Agent) | agent ↔ agent, across processes ("horizontal") | This RAG app is an **A2A client** of a standalone Excel Narrator Agent running in its own process. |

The symmetry is the teaching point: **MCP shows others using us. A2A shows us using others.**

### A2A Ingestion Path

XLSX/CSV files are not parsed in-process. They are handed to a separate agent over HTTP/JSON-RPC and narrated into text before entering the normal pipeline:

```
main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
        │
        ▼
A2AExcelClient (src/data_pipeline/a2a_excel_client.py)
        │
        │ 1. Agent discovery: GET /.well-known/agent-card.json
        ▼
Excel Narrator Agent -- standalone process, services/excel_agent, :9999
        │
        │ 2. Raw part upload: spreadsheet bytes + media_type + filename
        │    (a2a.helpers.new_raw_message)
        ▼
        │ 3. Streamed TASK_STATE_WORKING updates, one per step:
        │      "Reading ai_earnings_comparison.xlsx..."
        │      "Narrating sheet 1/13: 'Quarterly results'..."
        │      ...
        │      "Narrating sheet 13/13: 'Definitions'..."
        ▼
        │ 4. Artifact: narrated text plus structured sheet metadata
        │    (text part + data part in one artifact) + TASK_STATE_COMPLETED
        ▼
A2AExcelClient returns a DocumentExtractor-shaped dict
        │
        ▼
TextChunker → Embedder → AzureSearchIndexer   (unchanged pipeline)
```

Zero code coupling: the Excel Narrator Agent never imports `config` or `src`, and the main app never imports `services.excel_agent` -- the two processes share only `.env` conventions and talk exclusively over HTTP/JSON-RPC.

### Running the Demos

#### Demo 1: A2A (Excel Narrator Agent)

Two terminals, both from the project root, with the project's Python environment active (see [Installation](#installation)):

```bash
# Terminal 1: start the standalone Excel Narrator Agent
python -m services.excel_agent
```

Expected output:
```
Starting Excel Narrator Agent on http://127.0.0.1:9999
Agent card: http://127.0.0.1:9999/.well-known/agent-card.json
main app: python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
```

If another Excel Narrator Agent (for example the reference's) is already listening on port 9999, `main.py` would talk to that one instead. Stop it, or set `EXCEL_AGENT_PORT=9998` and `A2A_EXCEL_AGENT_URL=http://127.0.0.1:9998` in `.env`, as the template in [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md) does; the agent then starts on 9998.

```bash
# Terminal 2: first index the PDFs, HTML and DOCX of Tier 1 and ask a question
# that no single document answers; then ingest the comparison workbook and ask again
python main.py ingest --tier 1 --types pdf,html,docx
python main.py query "Which company had the highest revenue in calendar Q2 2026?"
python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
python main.py query "Which company had the highest revenue in calendar Q2 2026?"
```

Before the workbook is indexed, expect a partial or hedged answer: no single document compares all 8 companies. After it, the answer should come from the workbook's Rankings sheet: Alphabet, $119,796 million, ahead of NVIDIA, $96,221 million (source `ai_earnings_comparison.xlsx`). The wording varies from run to run. More questions, with answers checked against the files, are in section 6 of [`INDEXING_PLAN.md`](datasets/ai-earnings-desk/INDEXING_PLAN.md).

#### Demo 2: MCP (Multi-Agent RAG Server)

```bash
# Run the server directly (streamable-http transport, default)
python -m src.mcp_server
# MCP server: http://127.0.0.1:8100/mcp

# ...or stdio transport, for clients like Claude Desktop
python -m src.mcp_server --transport stdio

# ...or explore it interactively with the MCP Inspector (no server process needed)
mcp dev src/mcp_server/server.py
```

To use it from Claude Desktop, add to `claude_desktop_config.json`, with the absolute paths of your Python and of this project:

```json
{
  "mcpServers": {
    "ma-rag": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "src.mcp_server", "--transport", "stdio"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/ma-rag"
      }
    }
  }
}
```

### Ports

| Service | Port | Configured via |
|---------|------|----------------|
| FastAPI REST API | `8020` in this README's commands (uvicorn's default is `8000`) | `--port` flag to `uvicorn` |
| MCP server | `8100` | `MCP_SERVER_HOST` / `MCP_SERVER_PORT` env vars, or `--host` / `--port` CLI flags |
| A2A Excel Narrator Agent | `9999` by default (`9998` in the `.env` template of `docs/AZURE_SETUP.md`) | `EXCEL_AGENT_HOST` / `EXCEL_AGENT_PORT` env vars, or `--host` / `--port` CLI flags; the client reads `A2A_EXCEL_AGENT_URL` |

The reference uses the same default ports. So that both projects can run at once, this README starts the API with `--port 8020`, and [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md) sets `EXCEL_AGENT_PORT=9998` and `A2A_EXCEL_AGENT_URL=http://127.0.0.1:9998` in `.env`.

### MCP Primitives

| Primitive | Name | Wraps |
|-----------|------|-------|
| Tool | `search_documents(query, top_k=5)` | Raw hybrid retrieval (`AzureSearchRetriever`) -- no LLM calls. |
| Tool | `ask_rag(question)` | The full 4-agent LangGraph pipeline (`MultiAgentRAG.query`) -- spends LLM calls. |
| Resource | `rag://index/status` | `AzureSearchIndexer.get_index_statistics()` -- read-only index state. |
| Prompt | `cited_answer` | Reusable template instructing retrieval-before-answer plus per-claim citation. |

## Features

### Core Functionality
- **PDF Text Extraction** - Azure AI Document Intelligence with Layout model
- **Intelligent Chunking** - LangChain RecursiveCharacterTextSplitter with configurable size and overlap
- **Vector Embeddings** - Azure AI Foundry text-embedding-ada-002 (1536 dimensions)
- **Hybrid Search** - Combines vector similarity and keyword matching (BM25)
- **Multi-Agent RAG** - LangGraph-powered 4-agent workflow with guardrails
- **Intent Recognition** - Automatic classification of user intent
- **Source Citation** - Answers include source references with detailed chunk information

### 🆕 New Production Features

#### Guardrail System
- **Question Validation** - Filters irrelevant and unsafe questions before processing
- **Configurable Strictness** - Low, medium, or high guardrail sensitivity
- **Detailed Logging** - All guardrail decisions logged with reasons
- **Rejection Messages** - User-friendly messages for rejected queries

#### Session Management & Conversation Memory
- **Session Tracking** - Every query includes a session_id for conversation continuity
- **Multi-Turn Conversations** - Remembers previous question and answer (N-1 turn)
- **Follow-up Support** - Handle questions like "tell me more" or "what did I just ask?"
- **Automatic Cleanup** - Sessions expire after 30 minutes of inactivity
- **In-Memory Storage** - Fast, efficient session management without external databases

#### Enhanced Retrieval Transparency
- **Detailed Chunk Information** - Every retrieved chunk includes:
  - Chunk ID from Azure Search index
  - Source file name
  - Chunk position (index)
  - Search score (hybrid similarity)
  - Content preview (200 chars)
  - Full content for transparency
- **Retrieval Metrics** - Average scores, chunk counts tracked per query

#### Error Handling & Reliability
- **Retry Logic** - Exponential backoff for Azure API calls (max 3 retries)
- **Specific Error Types** - GuardrailRejectionError, RetrievalError, GenerationError, etc.
- **User-Friendly Messages** - Clear error messages distinguishing:
  - Guardrail rejection
  - Retrieval failures
  - Generation errors

### Infrastructure
- **Automatic Index Creation** - Programmatically creates Azure AI Search index if not exists
- **Monitoring** - Azure Application Insights integration with session_id tracking
- **REST API** - FastAPI backend with Swagger documentation
- **CLI Tools** - Command-line interface for all operations
- **Security** - Environment-based configuration, no hardcoded secrets
- **Background Tasks** - Automatic session cleanup every 5 minutes

### MCP & A2A Integration
- **MCP Server** - The RAG pipeline is exposed as a Model Context Protocol server (two tools, one resource, one prompt), usable from Claude Desktop, the MCP Inspector, or any MCP client with zero custom integration code
- **A2A Client** - XLSX/CSV ingestion delegates to a standalone Excel Narrator Agent over the Agent2Agent protocol, with zero code coupling between the two processes
- **Streamed Progress** - A2A task updates stream per-sheet narration progress live during ingestion, from agent discovery through the completed artifact

## Technology Stack

### Azure Services
- **Azure AI Document Intelligence** - PDF text extraction
- **Azure AI Foundry** - Embedding model (text-embedding-ada-002) and GPT-4
  - ⚠️ **Important**: Using Azure AI Foundry, NOT Azure OpenAI Service
  - Each deployed model has separate endpoint, deployment name, and API key
- **Azure AI Search** - Vector and hybrid search with HNSW algorithm
- **Azure Application Insights** - Monitoring and logging

### Python Libraries
- **LangChain** - Text splitting and AI orchestration
- **LangGraph** - Multi-agent workflow management
- **FastAPI** - REST API framework
- **Pydantic** - Data validation
- **OpenAI Python SDK** - Azure AI Foundry API client

## Prerequisites

### Azure Resources Required

You need the following Azure resources created. [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md) walks through creating or reusing each one in the Azure portal.

1. **Azure AI Document Intelligence**
   - Get: Endpoint and API Key

2. **Azure AI Foundry** (with deployed models)
   - Embedding model with 1,536 dimensions: text-embedding-ada-002 (in use) or text-embedding-3-small. Switching models means rebuilding the index
     - Get: Endpoint, Deployment Name, API Key
   - Chat model: gpt-4.1-mini (in use), or another model that accepts a temperature setting
     - Get: Endpoint, Deployment Name, API Key

3. **Azure AI Search**
   - Get: Endpoint and Admin API Key
   - Note: Index will be created programmatically

4. **Azure AI Content Safety** (optional)
   - Get: Endpoint and API Key
   - Screens the questions and answers of the FastAPI `/query` endpoint; without it they go through unchecked

5. **Azure Application Insights** (optional)
   - Get: Connection String
   - Without it, keep the placeholder from `.env.example`; the app then sends no telemetry

### Local Requirements
- Python 3.9 or higher
- pip (Python package manager)
- Git

## Installation

### 1. Get the Project

```bash
git clone https://github.com/JinyuJinyuJinyu/ma-rag.git
cd ma-rag
```

The clone holds the code, the docs and our own data files. The downloaded source documents are not included; see [Our Data](#our-data).

### 2. Python Environment

With conda:

```bash
conda create -n rag python=3.11
conda activate rag
```

Or create a virtual environment:

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Linux/Mac:
source venv/bin/activate
# On Windows:
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## Configuration

### 1. Environment Variables

Copy the example environment file:

```bash
cp .env.example .env
```

Or start from the `.env` template in Step 6 of [`docs/AZURE_SETUP.md`](docs/AZURE_SETUP.md).

### 2. Fill in Your Azure Credentials

Edit `.env` and fill in all values:

```bash
# Azure Document Intelligence
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://your-doc-intelligence.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=your_key_here

# Azure AI Foundry - Embedding Model
AZURE_AI_FOUNDRY_EMBEDDING_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT_NAME=text-embedding-ada-002
AZURE_AI_FOUNDRY_EMBEDDING_API_KEY=your_key_here
AZURE_AI_FOUNDRY_EMBEDDING_API_VERSION=2024-08-01-preview

# Azure AI Foundry - GPT-4 Model
AZURE_AI_FOUNDRY_GPT4_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_AI_FOUNDRY_GPT4_DEPLOYMENT_NAME=gpt-4
AZURE_AI_FOUNDRY_GPT4_API_KEY=your_key_here
AZURE_AI_FOUNDRY_GPT4_API_VERSION=2024-08-01-preview

# Azure AI Search
AZURE_SEARCH_ENDPOINT=https://your-search-service.search.windows.net
AZURE_SEARCH_ADMIN_KEY=your_admin_key_here
AZURE_SEARCH_INDEX_NAME=ai-earnings-desk

# PDFs are sent to Document Intelligence in pieces of this many pages
# (2 on the Free F0 tier, which reads only 2 pages per request; 0 = whole file)
AZURE_DOCUMENT_INTELLIGENCE_PAGES_PER_REQUEST=2

# Azure Application Insights (optional: keep the .env.example placeholder to send nothing)
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...

# Application Settings
CHUNK_SIZE=1200
CHUNK_OVERLAP=0.2
RETRIEVAL_TOP_K=5
RETRIEVAL_HYBRID_KNN=50
RETRIEVAL_WORKBOOK_EXPAND=20
RETRIEVAL_WORKBOOK_TRIGGER=3
RETRIEVAL_COMPANY_ROUTING=false
RETRIEVAL_SINGLE_COMPANY_TOP_K=0
LLM_TEMPERATURE=0.7

# 🆕 Guardrail Settings
GUARDRAIL_ENABLED=true
GUARDRAIL_STRICTNESS=medium  # low, medium, or high

# 🆕 Session Management
SESSION_TIMEOUT_MINUTES=30
CONVERSATION_MEMORY_TURNS=1  # Currently only supports 1 (previous turn)
```

`.env.example` also lists the optional settings: Content Safety (`AZURE_CONTENT_SAFETY_*`, `INPUT_GUARDRAIL_ENABLED`, `OUTPUT_GUARDRAIL_ENABLED`) and the ports (`EXCEL_AGENT_HOST`, `EXCEL_AGENT_PORT`, `A2A_EXCEL_AGENT_URL`, `MCP_SERVER_HOST`, `MCP_SERVER_PORT`).

**Reusing the reference's `.env`:** its Azure settings work here too, but its `AZURE_SEARCH_INDEX_NAME` is `rag-documents-index`. Change it, or this project's chunks go into the reference's index:

```bash
sed 's/^AZURE_SEARCH_INDEX_NAME=.*/AZURE_SEARCH_INDEX_NAME=ai-earnings-desk/' ../multiagent-rag/.env > .env
```

### 3. Important Notes on Azure AI Foundry

⚠️ **Critical Configuration Details:**

- **NOT Azure OpenAI Service**: This system uses Azure AI Foundry, which has different endpoints
- **Separate Credentials**: Each deployed model (embedding and GPT-4) has its own:
  - Endpoint URL
  - Deployment name
  - API key
- **Find Your Credentials**:
  - Go to Azure AI Foundry portal
  - Navigate to "Deployments"
  - Select each deployment to get its specific credentials

### 4. Verify Configuration

```bash
# Test configuration loading (no network calls)
python -c "from config import get_config; get_config(); print('✓ Configuration loaded successfully')"

# Test every Azure setting with one small real call per service (uses no Document Intelligence pages)
python scripts/check_azure.py
```

## Usage

### CLI Interface

The `main.py` script provides a command-line interface for all operations.

#### 1. Process a Document

```bash
# Process one PDF through the full pipeline
python main.py process datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf

# HTML and DOCX files are read locally
python main.py process datasets/ai-earnings-desk/AMD/ir/FY2026-Q2/AMD_FY2026-Q2_press_release.html

# Process an XLSX/CSV spreadsheet instead. This routes through the
# standalone Excel Narrator Agent, so start that agent first in another
# terminal:
#   python -m services.excel_agent
python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
```

This will:
1. Extract text from the PDF (Document Intelligence), HTML or DOCX (locally), or, for `.xlsx`/`.csv` files, narrate the spreadsheet via the [A2A Excel Narrator Agent](#mcp--a2a-protocol-integration)
2. Chunk the text
3. Generate embeddings
4. Create index (if needed)
5. Index all chunks, with the file's company, ticker and period if it is listed in `index_selection.csv`

#### 2. Index the Planned Selection

```bash
# List what would be indexed, with estimated chunks and Document Intelligence pages
# (reads only local files: no .env needed, nothing sent to Azure)
python main.py ingest --tier 1 --dry-run

# Index Tier 1 (start the Excel Narrator Agent first for its two XLSX files)
python main.py ingest --tier 1

# Only some file types, or Tier 2, or both tiers
python main.py ingest --tier 1 --types pdf,html,docx
python main.py ingest --tier 2
python main.py ingest --tier all
```

A file that fails is reported at the end and the others carry on. Re-running is safe: chunk IDs are stable, so chunks are replaced, not duplicated. Microsoft's metrics workbook is indexed from its cleaned copy in `datasets/ai-earnings-desk/prepared/`; rebuild it with `python -I datasets/ai-earnings-desk/scripts/prepare_msft_metrics.py`.

#### 3. Test Retrieval

```bash
# Retrieve relevant chunks for a query
python main.py retrieve "NVIDIA Data Center revenue" --top-k 5
```

#### 4. Test Multi-Agent Query

```bash
# Ask a question using the full multi-agent system
python main.py query "Which company had the highest revenue in calendar Q2 2026?" --top-k 5
```

#### 5. Manage Index

```bash
# Check index status
python main.py index status

# Create index
python main.py index create

# Recreate index (deletes all data!)
python main.py index recreate

# Delete index
python main.py index delete
```

### FastAPI Server

#### Start the Server

```bash
# Start with uvicorn on port 8020 (see Ports)
uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8020

# Or run directly (this listens on 0.0.0.0:8000)
python src/api/main.py
```

The server starts at `http://127.0.0.1:8020`. Keep it on `127.0.0.1`: the API has no login, so anyone who can reach it can spend tokens, write to the index or drop it.

#### Access Documentation

- **Swagger UI**: http://127.0.0.1:8020/docs
- **ReDoc**: http://127.0.0.1:8020/redoc

## API Documentation

### Endpoints

#### Health Check
```http
GET /api/v1/health
```

Returns service health status and connectivity.

#### Index Status
```http
GET /api/v1/index/status
```

Returns index statistics and configuration.

#### Upload Document
```http
POST /api/v1/upload
Content-Type: multipart/form-data
```

Upload and process a PDF document. (Use the CLI for HTML, DOCX and XLSX files.)

**Example using curl:**
```bash
curl -X POST "http://127.0.0.1:8020/api/v1/upload" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf"
```

**Response:**
```json
{
  "success": true,
  "message": "Document processed successfully",
  "file_name": "NVDA_FY2027-Q2_cfo_commentary.pdf",
  "chunks_created": 21,
  "chunks_indexed": 21,
  "processing_time_seconds": 12.5
}
```

Files uploaded through the API get no company, ticker or period fields; `main.py process` and `main.py ingest` add them.

#### Query System 🆕 Enhanced
```http
POST /api/v1/query
Content-Type: application/json
```

Ask a question using the multi-agent RAG system with session management and conversation memory.

**Request:**
```json
{
  "question": "Which company had the highest revenue in calendar Q2 2026?",
  "session_id": "session_12345",
  "top_k": 5,
  "user_id": "user_67890",
  "metadata": {"source": "web_app"}
}
```

**Required Fields:**
- `question` (string): User's question
- `session_id` (string): Session identifier (provided by backend)

**Optional Fields:**
- `top_k` (integer, 1-20, default: 5): Number of chunks to retrieve
- `user_id` (string): Optional user identifier for tracking
- `metadata` (object): Additional context

**Example using curl:**
```bash
curl -X POST "http://127.0.0.1:8020/api/v1/query" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Which company had the highest revenue in calendar Q2 2026?",
    "session_id": "session_12345",
    "top_k": 5
  }'
```

**Response (Success):**
```json
{
  "success": true,
  "question": "Which company had the highest revenue in calendar Q2 2026?",
  "answer": "Alphabet had the highest revenue in calendar Q2 2026, $119,796 million, ahead of NVIDIA with $96,221 million (Source 1)...",
  "intent": "comparison",
  "session_id": "session_12345",
  "conversation_turn": 1,
  "chunks_retrieved": 5,
  "retrieved_chunks": [
    {
      "chunk_id": "ai_earnings_comparison_xlsx_chunk_148",
      "source_file": "ai_earnings_comparison.xlsx",
      "chunk_index": 148,
      "score": 0.0331,
      "content_preview": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.",
      "full_content": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; ..."
    }
  ],
  "sources": ["ai_earnings_comparison.xlsx"],
  "processing_time_seconds": 2.3,
  "guardrail_passed": true,
  "guardrail_reason": "Question about the revenue of covered companies",
  "retrieval_query": "Which company had the highest revenue in calendar Q2 2026?"
}
```

**Response (Guardrail Rejection):**
```json
{
  "success": false,
  "question": "What's the weather today?",
  "answer": "Your question doesn't appear to be related to the indexed documents.",
  "intent": "rejected",
  "session_id": "session_12345",
  "conversation_turn": 1,
  "chunks_retrieved": 0,
  "retrieved_chunks": [],
  "sources": [],
  "processing_time_seconds": 0.5,
  "guardrail_passed": false,
  "guardrail_reason": "Weather is unrelated to the indexed documents",
  "retrieval_query": null
}
```

**Follow-up Question Example:**
```bash
# First question
curl -X POST "http://127.0.0.1:8020/api/v1/query" \
  -d '{"question": "How much revenue did NVIDIA report for calendar Q2 2026?", "session_id": "session_123"}'

# Follow-up question (uses conversation memory; the response's retrieval_query shows the
# standalone query it was rewritten into, e.g. "NVIDIA revenue in calendar Q2 2025")
curl -X POST "http://127.0.0.1:8020/api/v1/query" \
  -d '{"question": "And a year earlier?", "session_id": "session_123"}'
```

#### Create Index
```http
POST /api/v1/index/create?recreate=false
```

Create or recreate the search index.

## Project Structure

```
ma-rag/
├── config/
│   ├── __init__.py
│   └── azure_config.py          # Azure service configuration
├── src/
│   ├── data_pipeline/
│   │   ├── __init__.py
│   │   ├── document_extractor.py    # PDF text extraction (2-page pieces on the Free tier)
│   │   ├── local_extractor.py       # HTML/DOCX text extraction, no Azure calls (new)
│   │   ├── selection.py             # Reads index_selection.csv for `main.py ingest` (new)
│   │   ├── a2a_excel_client.py      # XLSX/CSV extraction via the A2A Excel Narrator Agent
│   │   ├── text_chunker.py          # Text chunking
│   │   ├── embedder.py              # Embedding generation
│   │   └── indexer.py               # Azure AI Search indexing (+ company/ticker/period fields)
│   ├── retrieval/
│   │   ├── __init__.py
│   │   └── retriever.py             # Hybrid search retrieval
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base_agent.py            # Base agent class
│   │   └── rag_agent.py             # Multi-agent RAG system
│   ├── monitoring/
│   │   ├── __init__.py
│   │   └── logger.py                # Azure Application Insights
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI application
│   │   ├── models.py                # Pydantic models
│   │   └── routes.py                # API endpoints
│   └── mcp_server/
│       ├── __init__.py
│       ├── __main__.py              # `python -m src.mcp_server` entry point
│       └── server.py                # FastMCP server: tools, resource, prompt
├── services/
│   ├── __init__.py
│   └── excel_agent/
│       ├── __init__.py
│       ├── __main__.py              # `python -m services.excel_agent` entry point
│       ├── server.py                # A2A server: AgentCard, executor, task streaming
│       └── narrator.py              # Spreadsheet -> narrative text (zero a2a-sdk imports)
├── datasets/                        # Our data files; the downloaded documents are not included (see "Our Data")
│   ├── ai-earnings-desk/            # The indexed corpus: 8 companies, SEC + IR files
│   │   ├── INDEXING_PLAN.md         # What to index and why, demo questions with checked answers
│   │   ├── index_selection.csv      # The files `main.py ingest` indexes, with their attributes
│   │   ├── comparison/              # Cross-company comparison workbook (from SEC XBRL data)
│   │   ├── prepared/                # Cleaned copies for the Excel Narrator (MSFT metrics)
│   │   ├── scripts/                 # build_comparison.py, prepare_msft_metrics.py, ...
│   │   └── <TICKER>/MANIFEST.md     # Each company's files (sec/, xbrl/, ir/<fiscal period>/ once downloaded)
│   └── archive/<8 dataset kits>/    # README.md, scripts/clean.py, <slug>_clean.xlsx
├── docs/
│   ├── AZURE_SETUP.md               # Step-by-step Azure setup and the .env template
│   ├── EVALUATION.md                # How the system is evaluated, in plain words, and how to run it
│   └── mcp-a2a-teaching-guide.md    # Classroom walkthrough for both demos (Chinese)
├── evaluation/                      # The two evals (see docs/EVALUATION.md)
│   ├── golden_set.yaml              # The questions, verified answers, checks and retrieval evidence
│   ├── gold.py, evidence.py         # Load the golden set; match chunks to its evidence
│   ├── retrieval.py, snapshot.py    # The retrieval eval; the index snapshot and check-gold
│   └── scoring.py, probe.py, collect.py, judge.py, report.py   # The answer eval
├── scripts/
│   ├── check_azure.py               # Tests each .env setting with one small real call per service
│   ├── eval_retrieval.py            # Retrieval eval: snapshot, check-gold, run, score
│   └── eval_answers.py              # Answer eval: run (ask the questions), score (exact checks + judge)
├── tests/
│   ├── test_eval_retrieval.py       # Offline tests for the retrieval eval
│   ├── test_eval_answers.py         # Offline tests for the answer eval
│   └── test_app_fixes.py            # Offline tests for the guardrail and follow-up behaviour
├── langgraph_test.py               # Multi-agent blueprint
├── main.py                         # CLI orchestration script
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment variables template
├── .gitignore
└── README.md
```

## Security Considerations

### 🔒 API Key Management

**NEVER commit secrets to version control!**

- ✅ All secrets in `.env` file (which is in `.gitignore`)
- ✅ `.env.example` contains only variable names, no actual values
- ✅ Environment variables validated at startup
- ✅ Secrets masked in logs (show first/last 4 characters only)

### 🏭 Production Recommendations

**For production deployments, use Azure Managed Identity instead of API keys:**

```python
from azure.identity import DefaultAzureCredential

credential = DefaultAzureCredential()
```

**Benefits:**
- No API keys to manage or rotate
- Automatic credential management
- Fine-grained RBAC (Role-Based Access Control)
- Works seamlessly in Azure-hosted environments

**See:** [Azure Identity Documentation](https://learn.microsoft.com/azure/developer/python/sdk/authentication-overview)

### 🛡️ Admin API Key

The Azure AI Search Admin Key has full management permissions:
- Can create/delete indexes
- Can read/write all data
- Must be carefully protected
- Consider using Query Keys for read-only operations in production

## Troubleshooting

### Configuration Errors

**Error: Required environment variable not set**
```
Solution: Check your .env file and ensure all variables from .env.example are filled in
```

**Error: Invalid endpoint URL**
```
Solution: Ensure endpoints start with https:// and are from correct Azure resources
```

### Azure AI Foundry Issues

**Error: Model not found or deployment not available**
```
Solution:
1. Verify models are deployed in Azure AI Foundry
2. Check deployment names match exactly
3. Ensure API keys are for the correct deployments
4. Verify API version is correct (2024-08-01-preview)
```

**Error: "715-123420: Our system has detected this request as unusual activity for your account" when deploying a model**
```
Solution:
Azure's fraud protection blocks model deployments, typically on new or recently converted
pay-as-you-go subscriptions. Retrying, another region or a new resource doesn't help, and
recreating resources can make it worse; only Microsoft support can lift it. Meanwhile, you can
use existing deployments on another Foundry resource: put that resource's endpoint and key in
.env (Step 2 of docs/AZURE_SETUP.md).
```

### Index Creation Issues

**Error: Index creation failed**
```
Solution:
1. Verify Azure Search Admin Key has management permissions
2. Check search service tier supports vector search
3. Ensure index name follows naming rules (lowercase, no special chars)
```

### Import Errors

**Error: No module named 'azure.ai.documentintelligence'**
```
Solution: Install dependencies
pip install -r requirements.txt
```

### Our Data

**Only the first 2 pages of a PDF made it into the index**
```
Solution: the Document Intelligence Free (F0) tier reads only 2 pages per request.
Keep AZURE_DOCUMENT_INTELLIGENCE_PAGES_PER_REQUEST=2 (the default) so PDFs are sent in 2-page pieces.
```

**Error: Excel Narrator Agent not reachable, or "address already in use" when starting it**
```
Solution: start the agent (python -m services.excel_agent) before processing XLSX/CSV files.
If port 9999 is taken (for example by the reference project's agent), run this one with
--port 9998 and set A2A_EXCEL_AGENT_URL=http://127.0.0.1:9998 in .env.
```

**`main.py ingest` reports "needs cleaning first" for MSFT_FY2026-Q4_metrics.xlsx**
```
Solution: rebuild the cleaned copy: python -I datasets/ai-earnings-desk/scripts/prepare_msft_metrics.py
```

**Error: The property 'company' does not exist on type 'search.documentFields'**
```
Solution: the index was created by the reference project's code, without the company/ticker/period fields.
Use the ai-earnings-desk index (AZURE_SEARCH_INDEX_NAME), or recreate the index: python main.py index recreate
```

### Connection Issues

**Error: Connection timeout or refused**
```
Solution:
1. Check internet connectivity
2. Verify Azure service endpoints are correct
3. Check firewall/network settings
4. Ensure Azure services are in same region (recommended)
```

## Monitoring

### Azure Application Insights

With a real `APPLICATIONINSIGHTS_CONNECTION_STRING`, the app sends these to your Application Insights resource:

- **Logs**: the log lines of the CLI and the FastAPI app
- **Metrics**: processing times, chunk counts and retrieval counts from the FastAPI endpoints
- **Events**: document uploads, queries and index operations from the FastAPI endpoints. Query events carry the first 100 characters of the question and the session and user IDs; upload events carry the file name
- **Exceptions**: errors raised in the FastAPI endpoints

With the placeholder from `.env.example`, nothing is sent. The MCP server sends nothing either way.

**View in Azure Portal:**
1. Go to your Application Insights resource
2. Navigate to "Logs" or "Metrics"
3. Query for custom events and metrics

### Local Logs

Logs are also output to console with format:
```
2024-01-15 10:30:00 - module.name - INFO - Message
```

## Testing

### Quick Test Workflow

1. **Process a test document:**
   ```bash
   python main.py ingest --tier 1 --dry-run
   python main.py process datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf
   ```

2. **Test retrieval:**
   ```bash
   python main.py retrieve "NVIDIA Data Center revenue"
   ```

3. **Test multi-agent query:**
   ```bash
   python main.py query "What was NVIDIA's Data Center revenue last quarter?"
   ```
   The CFO commentary gives a record $89.0 billion, up 117% year over year.

4. **Test API:**
   ```bash
   # Start server
   uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8020

   # In another terminal, test upload
   curl -X POST "http://127.0.0.1:8020/api/v1/upload" \
     -F "file=@datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf"

   # Test query
   curl -X POST "http://127.0.0.1:8020/api/v1/query" \
     -H "Content-Type: application/json" \
     -d '{"question": "What was NVIDIA Data Center revenue last quarter?", "session_id": "test_session"}'
   ```

## Performance Optimization

### Chunk Size Tuning

Adjust in `.env`:
```bash
CHUNK_SIZE=1200          # Larger = more context, fewer chunks
CHUNK_OVERLAP=0.2        # Higher = more overlap, better continuity
```

### Retrieval Tuning

```bash
RETRIEVAL_TOP_K=5             # Number of chunks to retrieve
RETRIEVAL_HYBRID_KNN=50       # Vector neighbours fused with the keyword results in hybrid search (at least top_k)
RETRIEVAL_WORKBOOK_EXPAND=20  # Up to this many extra workbook chunks for workbook questions (0: off)
RETRIEVAL_WORKBOOK_TRIGGER=3  # ...when this many of the top_k results are workbook chunks
RETRIEVAL_COMPANY_ROUTING=false  # true: search each company a question names (up to 3) on its own
RETRIEVAL_SINGLE_COMPANY_TOP_K=0  # ...and give a question naming one company this many results (0: top_k)
```

Hybrid search fuses the keyword (BM25) results with the vector neighbours. With only `top_k` neighbours,
every neighbour also matches some keyword of a long question, so the fusion always ranks them first and
hybrid returns exactly the vector top `top_k`; 50 neighbours let keyword-only chunks in.

Workbook expansion helps questions that read one column down many rows ("which of the eight companies had
the lowest margin"): the Excel narrator writes one sentence per row, so the rows sit in many chunks. When
the top results are mostly workbook chunks, a second search over the workbooks adds more of them.

Company routing (`RETRIEVAL_COMPANY_ROUTING=true`, off by default until the evals measure it) helps document
passages that never name their company, such as a segment table or a later paragraph of a press release:
they lose to passages that do, from other companies' documents and the workbook rows. A question that names
one to three of the eight companies (by name, ticker or a product such as Xbox or EPYC) searches each
company's chunks, plus the comparison workbook's, on its own (the filter `ticker eq 'AMD' or ticker eq
null`) and merges the results rank by rank, so each company it names gets `top_k` results. Other questions
run one search as before. With `RETRIEVAL_SINGLE_COMPANY_TOP_K` set (to 10, say), a question that names
just one company gets that many results instead: all of them are that company's chunks or workbook rows,
and the passage it needs often sits just below the top 5, among the same file's passages.

The Excel narrator also follows each workbook's sheets with **column views** (`EXCEL_AGENT_COLUMN_VIEWS=true`,
the default): each column down every row, in passages that name the sheet and column, so a column's values
sit in a few chunks. For a workbook indexed before column views existed, `python scripts/add_column_views.py
<file>` adds them without re-ingesting it (`--dry-run` to preview, `--delete` to remove them).

### LLM Temperature

```bash
LLM_TEMPERATURE=0.7      # Lower = more focused, Higher = more creative
```

The guardrail agent ignores this setting and always runs at 0, so the same question always gets the same verdict.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

No license has been chosen for this project yet. Most of its code comes from
[multiagent-rag](https://github.com/dukewillbe185/multiagent-rag), which has no license file either. Without a
license, that code's author keeps the rights to it. So ask Duke Wang before making this repository public or
sharing the code, and choose a license for this project once you have that answer.

The data has its own terms; see [Our Data](#our-data) and each dataset's `README.md` or `MANIFEST.md`.

## Support

For issues and questions:
- Create an issue in the repository
- Check Azure service health status
- Review Azure documentation for service-specific issues

## Credits

This project is adapted from **[multiagent-rag](https://github.com/dukewillbe185/multiagent-rag) by Duke Wang
([@dukewillbe185](https://github.com/dukewillbe185))**. Its design and most of its code come from that project:

- the 4-agent LangGraph workflow (guardrail, retrieval, intent and answer agents) and the session memory
- the A2A Excel Narrator Agent and the MCP server
- the FastAPI backend with its Content Safety guardrails
- the Azure data pipeline (Document Intelligence, chunking, embeddings, AI Search) and the monitoring code
- the classroom teaching guide, `docs/mcp-a2a-teaching-guide.md`

It is based on upstream commit `ef31684` (3 August 2026), adapted to the AI Earnings Desk data. [Changes from the Reference](#changes-from-the-reference) lists what changed.

Also built with LangChain and LangGraph, Azure AI services, FastAPI, and OpenAI models on Azure.

---

**Happy RAG Building! 🚀**
