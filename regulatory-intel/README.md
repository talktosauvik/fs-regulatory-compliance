# Regulatory Intel Agent

A standalone ADK agent deployed natively to Vertex AI Agent Engine that securely reads, parses, and extracts key compliance mandates from dense regulatory documents (e.g., SEC Form PF, FINRA AML policies) stored entirely within a secure Google Cloud Storage (GCS) perimeter.

## Overview

This agent leverages the native multimodal capabilities of Vertex AI and the Gemini model to read PDFs directly from GCS without downloading them locally or relying on public internet web scrapers. When invoked, it:

1. Accepts a target document name (e.g., `SEC_IA-6546_BaseRule.pdf`) and a specific compliance query.
2. Uses `types.Part.from_uri` to securely process the PDF via Vertex AI.
3. Returns a highly structured, objective extraction of the regulatory rule.

The agent exposes a native A2A (Agent-to-Agent) protocol interface hosted directly on Vertex AI Reasoning Engine, allowing the Orchestrator agent to call it securely.

## Quick Start & Dependencies

This project uses `uv` for lightning-fast dependency management.

```bash
# Navigate to the agent directory
cd agents/regulatory-intel

# Install dependencies into a local .venv
uv sync
```

## Authentication & Configuration (VPC-SC Compliant)

This agent runs natively on **Vertex AI**. Authentication is handled automatically via [Application Default Credentials (ADC)](https://cloud.google.com/docs/authentication/application-default-credentials) (`google.auth.default()`). No external API keys are required.

Ensure you have valid ADC configured locally before running or deploying:
```bash
gcloud auth application-default login --update-adc
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
| `AGENT_VERSION` | Version of the agent for A2A routing (e.g., `0.1.0`) |
| `AGENT_AUTHORIZATION` | The A2A authorization resource path for Gemini Enterprise. |
| `GEMINI_ENTERPRISE_APP_ID` | The ID of the parent Gemini Enterprise app. |

## Architecture

Because this agent is deployed natively to Agent Engine, it does not require Dockerfiles, Makefiles, or FastAPI wrappers.

```text
agents/regulatory-intel/
├── app/
│   ├── __init__.py
│   ├── agent.py            # Core agent with native GCS document extraction tool
│   ├── prompts/
│   │   ├── system_prompt.md
│   │   └── system_instructions.md
│   └── app_utils/
│       ├── telemetry.py    # OpenTelemetry tracing to GCP
│       └── typing.py       # Pydantic data schemas
├── pyproject.toml
├── requirements.txt
├── deploy.py               # Serializes and deploys the agent to Vertex AI
└── .env.sample
```

## Deployment

Deploy directly to Vertex AI Agent Engine using the deployment script:

```bash
uv run python deploy.py --project=<YOUR_PROJECT_ID> --region=us-central1 --display_name="regulatory-intel"
```