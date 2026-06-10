# fs-regulatory-intel-agent

A standalone ADK agent deployed to Cloud Run that securely reads, parses, and extracts key compliance mandates from dense regulatory documents (e.g., SEC Form PF, FINRA AML policies) stored entirely within a secure Google Cloud Storage (GCS) perimeter.

## Overview

This agent leverages the native multimodal capabilities of Vertex AI and the Gemini model to read PDFs directly from GCS without downloading them locally or relying on public internet web scrapers. When invoked, it:

1. Accepts a target document name (e.g., `sec/ia-6546.pdf`) and a specific compliance query.
2. Uses `types.Part.from_uri` to securely process the PDF via Vertex AI.
3. Returns a highly structured, objective extraction of the regulatory rule.

The agent exposes an A2A (Agent-to-Agent) protocol interface served via FastAPI, allowing the Orchestrator agent to call it securely over HTTP.

## Quick Start

```bash
# Install dependencies
make install

# Start local server (port 8000 default)
make local-backend

# Or use the ADK playground
make playground
```

## Authentication & Configuration (VPC-SC Compliant)

This agent runs on **Vertex AI** for its LLM operations. Authentication is handled automatically via [Application Default Credentials (ADC)](https://cloud.google.com/docs/authentication/application-default-credentials) (`google.auth.default()`). No external API keys are required.

Ensure you have valid ADC configured locally before running or deploying:
```bash
gcloud auth application-default login
```

### Environment Variables

Copy `.env.sample` to `.env` and fill in the required values:

| Variable | Description |
|----------|-------------|
| `ADK_MODEL` | Model ID for the LLM agent (default: `gemini-3-flash-preview`) |
| `GOOGLE_CLOUD_PROJECT` | Your Google Cloud project ID. |
| `GOOGLE_CLOUD_LOCATION` | API location (default: `global`) |
| `GOOGLE_GENAI_USE_VERTEXAI` | SDK requirement to force Vertex AI routing (`True`) |
| `FSI_EXTERNAL_REGS_BUCKET` | The secure GCS bucket containing the SEC/FINRA PDFs. |
| `LOGS_BUCKET_NAME` | GCS bucket for telemetry and GenAI logs. |
| `AGENT_VERSION` | Version of the agent for A2A routing (e.g., `0.1.0`) |

*(Note: For Cloud Run deployment, Gemini Enterprise specific `AGENT_AUTHORIZATION` and `GEMINI_ENTERPRISE_APP_ID` are not required in the environment.)*

## Architecture

```text
agents/fs-regulatory-intel/
├── app/
│   ├── __init__.py
│   ├── agent.py            # Core agent with native GCS document extraction tool
│   ├── fast_api_app.py     # FastAPI server for A2A protocol
│   ├── prompts/
│   │   ├── system_prompt.md
│   │   └── system_instructions.md
│   └── app_utils/
│       ├── telemetry.py    # OpenTelemetry tracing to GCP
│       └── typing.py       # Pydantic data schemas
├── pyproject.toml
├── Dockerfile          # Dockerfile for Cloud Run deployment
├── Makefile            # Deployment and development commands
└── .env.sample
```

## Deployment

```bash
# Deploy to Cloud Run
make deploy
```
