# fs-policy-auditor

A standalone ADK agent deployed to Cloud Run that performs deep semantic gap analyses by comparing external regulatory mandates (e.g., SEC Form PF, FINRA AML) against the firm's highly confidential internal policies.

## Overview

This agent is the "Action Engine" of the FSI compliance workflow. When invoked by the Orchestrator via the A2A (Agent-to-Agent) protocol, it:

1.  Uses Vertex AI's native multimodal capabilities to securely read internal proprietary PDFs from Google Cloud Storage (VPC-SC compliant).
2.  Performs a mathematical/semantic delta analysis to find regulatory violations.
3.  Exports a human-readable Official Audit Log to Google Drive.
4.  Writes a machine-readable JSON Remediation Specification to a developer handoff GCS bucket.
5.  Creates Jira tasks for the remediation items.
6.  Emits strict **A2UI v0.8** JSON payloads to render interactive, branded compliance alert cards natively in Gemini Enterprise.

## Project Structure

```
fs-policy-auditor/
├── app/                       # Core agent code
│   ├── __init__.py
│   ├── agent.py               # Gap analysis logic, GCS read/write, Drive export tools
│   ├── a2ui_schema.py         # A2UI v0.8 JSON Schema and Compliance Card definitions
│   ├── a2ui_executor.py       # Custom executor for parsing A2UI payloads
│   ├── fast_api_app.py        # FastAPI Backend server for A2A protocol
│   ├── prompts/               # System prompts and instructions
│   └── app_utils/             # Telemetry and typing utilities
├── tests/                     # Unit, integration, and load tests
├── Makefile                   # Development and deployment commands
├── pyproject.toml             # Project dependencies
└── .env.sample
```

## Quick Start

Install required packages and launch the local development environment:

```bash
make install && make playground
```

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `make install`       | Install dependencies using uv                                                               |
| `make playground`    | Launch local development environment                                                        |
| `make lint`          | Run code quality checks                                                                     |
| `make test`          | Run unit and integration tests                                                              |
| `make deploy`        | Deploy agent to Cloud Run                                                                   |
| `make local-backend` | Launch local development server with hot-reload                                             |
| `make inspector`     | Launch A2A Protocol Inspector                                                               |

## Configuration

### Environment Variables

Copy `.env.sample` to `.env` and fill in the values:

| Variable | Description |
|----------|-------------|
| `ADK_MODEL` | The Gemini model to use (default: `gemini-3-flash-preview`). |
| `GOOGLE_CLOUD_PROJECT` | Your Google Cloud project ID. |
| `GOOGLE_CLOUD_LOCATION` | API location (default: `global`). |
| `GOOGLE_GENAI_USE_VERTEXAI` | SDK requirement to force Vertex AI routing (`True`). |
| `LOGS_BUCKET_NAME` | GCS bucket for telemetry and GenAI logs. |
| `FSI_EXTERNAL_REGS_BUCKET` | The secure GCS bucket containing external SEC/FINRA PDFs. |
| `FSI_INTERNAL_POLICIES_BUCKET` | The secure GCS bucket containing internal proprietary policies. |
| `FSI_DEV_HANDOFF_BUCKET` | The secure GCS bucket where the IT JSON specs are written. |
| `GOOGLE_DRIVE_FOLDER_ID` | The Shared Google Drive Folder ID for executive audit logs. |
| `REGULATORY_INTEL_AGENT_URL` | URL of the deployed Regulatory Intel Agent. |
| `AGENT_VERSION` | Version of the agent for A2A routing (e.g., `0.1.0`). |

## Deployment

```bash
make deploy
```

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use `make inspector` to test interoperability.
