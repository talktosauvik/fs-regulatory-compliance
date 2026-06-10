# Policy Auditor Agent

A standalone ADK agent that performs deep semantic gap analyses by comparing external regulatory mandates (e.g., SEC Form PF, FINRA AML) against the firm's highly confidential internal policies. 

## Overview

This agent is the "Action Engine" of the FSI compliance workflow. When invoked by the Orchestrator via the A2A (Agent-to-Agent) protocol, it:

1. Uses Vertex AI's native multimodal capabilities to securely read internal proprietary PDFs from Google Cloud Storage (VPC-SC compliant).
2. Performs a mathematical/semantic delta analysis to find regulatory violations.
3. Exports a human-readable Official Audit Log to Google Drive.
4. Writes a machine-readable JSON Remediation Specification to a developer handoff GCS bucket.
5. Emits strict **A2UI v0.8** JSON payloads to render interactive, branded compliance alert cards natively in Gemini Enterprise.

## Project Structure

Because this agent is deployed natively to Agent Engine, it does not require Dockerfiles, Makefiles, or FastAPI wrappers.

```text
policy-auditor/
├── app/                       # Core agent code
│   ├── __init__.py
│   ├── agent.py               # Gap analysis logic, GCS read/write, Drive export tools
│   ├── a2ui_schema.py         # A2UI v0.8 JSON Schema and Compliance Card definitions
│   ├── a2ui_executor.py       # Custom executor for parsing A2UI payloads natively in AE
│   ├── prompts/               # System prompts and instructions
│   └── app_utils/             # Telemetry and typing utilities
├── tests/                     # Unit, integration, and load tests
├── deploy.py                  # Custom deployment & Gemini Enterprise registration script
├── pyproject.toml             # Project dependencies
└── .env.sample
```

## Quick Start & Dependencies

This project uses `uv` for dependency management.

```bash
# Navigate to the agent directory
cd agents/policy-auditor

# Install dependencies into a local .venv
uv sync
```

## Configuration

### Environment Variables

Copy `.env.sample` to `.env` and fill in the required values. Ensure you have run `gcloud auth application-default login --update-adc` to authenticate your local environment.

| Variable | Description |
|----------|-------------|
| `ADK_MODEL` | The Gemini model to use (default: `gemini-2.5-flash`). |
| `GOOGLE_CLOUD_PROJECT` | Your Google Cloud project ID. |
| `GOOGLE_CLOUD_LOCATION` | API location (default: `global`). |
| `GOOGLE_GENAI_USE_VERTEXAI` | SDK requirement to force Vertex AI routing (`True`). |
| `LOGS_BUCKET_NAME` | GCS bucket for OpenTelemetry and GenAI logging. |
| `FSI_EXTERNAL_REGS_BUCKET` | The secure GCS bucket containing external SEC/FINRA PDFs. |
| `FSI_INTERNAL_POLICIES_BUCKET` | The secure GCS bucket containing internal proprietary policies. |
| `FSI_DEV_HANDOFF_BUCKET` | The secure GCS bucket where the IT JSON specs are written. |
| `GOOGLE_DRIVE_FOLDER_ID` | The Shared Google Drive Folder ID for executive audit logs. |
| `GEMINI_ENTERPRISE_APP_ID` | Your Gemini Enterprise application ID. |
| `AGENT_AUTHORIZATION` | The OAuth resource name generated in the GE Authorizations UI. |
| `AGENT_VERSION` | Version of the agent for A2A routing (e.g., `0.1.0`). |

---

## Deployment & Gemini Enterprise Registration

This agent is deployed natively to Vertex AI Agent Engine. The deployment script automatically packages the custom `a2ui_executor.py` logic, injects A2UI v0.8 capabilities into the Agent Card, and registers the agent to your Gemini Enterprise App.

```bash
# Deploy to Vertex AI Agent Engine
uv run python deploy.py --project=<YOUR_PROJECT_ID> --region=us-central1 --display_name="Policy Auditor"
```

## A2UI / A2A Protocol

This agent supports the[A2A Protocol](https://a2a-protocol.org/) and utilizes the **A2UI v0.8** specification to render rich Compliance Gap Analysis cards directly in the Gemini Enterprise UI. 
* The UI layout is defined in `a2ui_schema.py`.
* Because Agent Engine natively passes raw text, `a2ui_executor.py` acts as a custom `A2aAgentExecutor`. It intercepts the JSON payload from the LLM, prevents delimiter leakage, and packages it as an A2A `DataPart` for Gemini Enterprise to render securely.