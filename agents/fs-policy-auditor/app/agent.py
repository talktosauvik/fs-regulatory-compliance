# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import datetime
import logging
import os
import pathlib

import google.auth
import google.auth.transport.requests
from google.cloud import storage as gcs_storage
from googleapiclient.discovery import build as google_api_build

import json as _json
import re
import uuid
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.client.legacy import A2AClient
from a2a.types import Message, Part, Role, TextPart, SendMessageRequest, MessageSendParams, SendMessageSuccessResponse, Task, AgentCard

from pydantic import BaseModel, Field
from app.a2ui_schema import A2UI_SCHEMA, COMPLIANCE_A2UI_EXAMPLE
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import LongRunningFunctionTool, ToolContext
from google.genai import types, Client


logger = logging.getLogger(__name__)

load_dotenv(override=True)

# Dynamic year tokens used in prompt templates so trend references stay current.
_CURRENT_YEAR = datetime.date.today().year
_NEXT_YEAR = _CURRENT_YEAR + 1


def load_prompt(prompt_name: str) -> str:
    prompt_path = pathlib.Path(__file__).parent / "prompts" / prompt_name
    try:
        text = prompt_path.read_text()
        return text.replace("{current_year}", str(_CURRENT_YEAR)).replace(
            "{next_year}", str(_NEXT_YEAR)
        )
    except FileNotFoundError:
        logger.warning("Prompt file %s not found.", prompt_name)
        return ""


system_prompt = load_prompt("system_prompt.md")
system_instructions = load_prompt("system_instructions.md")

model_id = os.getenv("ADK_MODEL", "gemini-3-flash-preview")

# Use setdefault so .env values are respected; only fill in if not already set.
_, _default_project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _default_project_id or "")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


def request_user_input(message: str) -> dict:
    """Request additional input from the user.

    Use this tool when you need more information from the user to complete a task.
    Calling this tool will pause execution until the user responds.

    Args:
        message: The question or clarification request to show the user.
    """
    return {"status": "pending", "message": message}
_AGENT_ENGINE_PATTERN = re.compile(
    r"^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$"
)

_a2a_client_cache: dict[str, tuple[httpx.AsyncClient, object]] = {}
_a2a_client_locks: dict[str, asyncio.Lock] = {}

def _get_a2a_auth_headers(endpoint_url: str) -> dict[str, str]:
    if endpoint_url.startswith("http://localhost") or endpoint_url.startswith("http://127.0.0.1"):
        return {}

    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import Request

    try:
        if _AGENT_ENGINE_PATTERN.match(endpoint_url):
            # For Agent Engine, we need a standard OAuth token with cloud-platform scope
            import google.auth
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(Request())
            token = credentials.token
        else:
            # For Cloud Run services, we need an ID token
            from google.oauth2 import id_token as google_id_token
            token = google_id_token.fetch_id_token(Request(), endpoint_url)
            
        return {"Authorization": f"Bearer {token}"}
    except DefaultCredentialsError as e:
        logger.warning("Could not fetch auth token for %s: %s", endpoint_url, e)
        return {}

async def _get_or_create_a2a_client(endpoint_url: str) -> tuple[httpx.AsyncClient, object]:
    if endpoint_url in _a2a_client_cache:
        return _a2a_client_cache[endpoint_url]

    lock = _a2a_client_locks.setdefault(endpoint_url, asyncio.Lock())
    async with lock:
        if endpoint_url in _a2a_client_cache:
            return _a2a_client_cache[endpoint_url]

        auth_headers = _get_a2a_auth_headers(endpoint_url)
        httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0), headers=auth_headers)
        if _AGENT_ENGINE_PATTERN.match(endpoint_url):
            # For Agent Engine, we fetch the card from the specific path
            card_url = f"https://us-central1-aiplatform.googleapis.com/v1beta1/{endpoint_url}/a2a/v1/card"
            card_response = await httpx_client.get(card_url)
            card_response.raise_for_status()
            card = AgentCard.model_validate(card_response.json())
            
            # Use legacy A2AClient directly with the card
            # Use legacy A2AClient directly with the card, overriding URL to use project number
            a2a_client = A2AClient(httpx_client=httpx_client, agent_card=card, url=f"https://us-central1-aiplatform.googleapis.com/v1beta1/{endpoint_url}/a2a")
        else:
            # For standard URLs, use legacy A2AClient with URL
            a2a_client = A2AClient(httpx_client=httpx_client, url=endpoint_url.rstrip("/"))

        _a2a_client_cache[endpoint_url] = (httpx_client, a2a_client)
        return httpx_client, a2a_client

async def _send_a2a_message(endpoint_url: str, message_text: str) -> str:
    """Sends a message to a remote A2A agent and parses text/JSON dynamically."""
    try:
        _, client = await _get_or_create_a2a_client(endpoint_url)

        # Construct the payload according to the codelab example
        message_id = str(uuid.uuid4())
        session_id = "policy-auditor-session" # Using a dummy session ID
        
        payload = {
            "message": {
                "role": "user",
                "parts": [
                    {"type": "text", "text": message_text}
                ],
                "messageId": message_id,
                "contextId": session_id,
            },
        }

        message_request = SendMessageRequest(
            id=message_id, params=MessageSendParams.model_validate(payload)
        )
        
        # Call send_message which returns SendMessageResponse
        send_response = await client.send_message(request=message_request)
        
        if not isinstance(send_response.root, SendMessageSuccessResponse):
            logger.error(f"Received non-success response: {send_response}")
            return "Error: Received non-success response from remote agent."

        # Extract the result
        result = send_response.root.result
        
        # Extract text from result (Task or Artifacts)
        all_texts = []
        
        def add_text(text):
            if text:
                all_texts.append(text)

        def extract_from_parts(parts_list):
            if not parts_list: 
                return
            for part in parts_list:
                root = getattr(part, "root", part) 
                if hasattr(root, "text") and getattr(root, "text"):
                    add_text(getattr(root, "text"))
                elif isinstance(root, dict) and root.get("text"):
                    add_text(root["text"])

        # Search for parts in the result object
        if hasattr(result, "response") and result.response:
             extract_from_parts(result.response.parts)
        elif hasattr(result, "artifacts") and result.artifacts:
             for artifact in result.artifacts:
                  extract_from_parts(artifact.parts)
                  
        if all_texts:
            return "\n\n".join(all_texts)
            
        return str(result) # Fallback to string representation

    except Exception as e:
        logger.exception("Error in _send_a2a_message")
        return f"Error in _send_a2a_message: {e}"




async def fetch_external_regulatory_intel(query: str, tool_context: ToolContext) -> str:
    """Mock tool to return hardcoded regulatory intelligence for demo purposes.
    
    Args:
        query: What rules to extract.
        tool_context: ADK tool context.
    """
    return r"""**Executive Summary**

The SEC has issued critical error corrections to the Form PF amendments, clarifying reporting requirements for master-feeder arrangements, general instructions, and specific data fields. These corrections, detailed in SEC Release IA-6865, refine the foundational mandates of SEC Release IA-6546. Concurrently, FINRA Rule 3310 outlines specific independent Anti-Money Laundering (AML) testing frequencies, which vary based on the firm's client engagement model.

**Rule Identifiers & Agencies**

* SEC IA-6546  
* SEC IA-6865  
* FINRA 3310

**Detailed SEC Delta**

The following table details the corrections and alterations made by SEC Release IA-6865 to the Form PF amendments initially outlined in SEC Release IA-6546.

| Section/Glossary Term | Field/Instruction | Original (IA-6546) | Altered Text (IA-6865) |
| :---- | :---- | :---- | :---- |
| I. General Instructions | General Instruction 6 | Instruction stated "report information for any private fund advised by any of your related persons unless you have identified that related person in Question 1(b) as a related person for which you are filing Form PF." (Implied omission of "do not") | Amends the instruction to correctly state "**do not** report information for any private fund advised by any of your related persons unless you have identified that related person in Question 1(b) as a related person for which you are filing Form PF." (Corrected omission of "do not") |
| I. General Instructions | General Instruction 7 (Trading vehicles) | Erroneous cross-reference to "Question 7(b)". | Corrects an erroneous cross-reference, changing "Question 7(b)" to "**Question 9**." |
| II. Reporting Fields and Questions / Master-Feeder Arrangements | Question 7(a) | Instruction indicated only sub-questions (i) and (ii) needed completion for each feeder fund. | Revises the instruction for feeder funds. Filers are now directed to complete sub-questions **(i), (ii), and (iii)** for each feeder fund. |
| II. Reporting Fields and Questions | Question 47 | Included column headings "Not relevant" and "Relevant/not formally tested". | Removes the column headings "Not relevant" and "Relevant/not formally tested". Instruction updated to require filers to enter "**zero**" for market factors that have no direct effect. |
| II. Reporting Fields and Questions / MMF Amendments | Question 57 (Unsecured/Secured borrowing tables) | Categories included (A) U.S. financial institutions, (B) Non-U.S. financial institutions, (C) Other U.S. creditors, (D) Other non-U.S. creditors. | Revises categories to: (A) U.S. depository institutions, (B) U.S. creditors that are not U.S. depository institutions, (C) Non-U.S. creditors. |
| II. Reporting Fields and Questions / MMF Amendments | Question 65(f) | Original list of investment categories for identifying the instrument. | Revises the **entire list of investment categories** to choose from for identifying the instrument, including specific types of debt, repo agreements, and other instruments. |
| II. Reporting Fields and Questions | Question 73 | Questions were numbered as 70 and 71\. | Redesignates two questions (formerly Questions 70 and 71\) as **Question 73(a) and Question 73(b)**. |
| III. Glossary Definitions | "Collateral posted entries" and "Collateral received entries" definitions | Erroneous references to a "counterparty credit exposure and collateral table"; incorrect cross-references (e.g., Question 26, Question 41). | Replaces all erroneous references to a "counterparty credit exposure and collateral table" with the correct "**consolidated counterparty exposure table**"; corrects cross-references (e.g., Question 26 to Questions 27 and 28 for posted, Question 28 for received). Adds "(other than cash and cash equivalents)" after "other securities". |
| III. Glossary Definitions | "WAL" definition | Defined as "weighted average portfolio maturity". | Changes the definition from "weighted average portfolio maturity" to "**weighted average portfolio life**." |

**FINRA Mandates (Highlighted)**

As per FINRA Rule 3310(c):

* Independent AML testing is required **annually** (on a calendar-year basis) for most members.  
* Independent AML testing is required **every two years** (on a calendar-year basis) if the member does not execute transactions for customers or otherwise hold customer accounts or act as an introducing broker with respect to customer accounts (e.g., engages solely in proprietary trading or conducts business only with other broker-dealers).

**Impacted Business Units**

* Institutional Asset Management  
* Wealth Management  
* IT  
* Compliance  
* Operations

**Source Citations**

* [ia-6546.pdf](https://storage.cloud.google.com/vertexai-l300-capstone-external/sec/ia-6546.pdf)  
* [ia-6865.pdf](https://storage.cloud.google.com/vertexai-l300-capstone-external/sec/ia-6865.pdf)  
* [finra-3310.pdf](https://storage.cloud.google.com/vertexai-l300-capstone-external/finra/finra-3310.pdf)
"""

async def read_internal_policy(file_path: str, query: str) -> str:
    """Securely reads and queries a highly confidential internal policy document.
    
    Use this tool to extract exact internal clauses, schedules, and instructions 
    from the firm's private GCS bucket to compare against external regulations.

    Args:
        file_path: The exact filename of the internal document in the bucket
                   (e.g., 'FSI_AML_Manual.pdf'). DO NOT include subfolders.
        query: The specific extraction request (e.g., 'What is the testing frequency?').

    Returns:
        The extracted internal policy text, or an error message.
    """
    bucket_name = os.getenv("FSI_INTERNAL_POLICIES_BUCKET")
    if not bucket_name:
        return "Error: FSI_INTERNAL_POLICIES_BUCKET environment variable is not set."

    gcs_uri = f"gs://{bucket_name}/{file_path}"
    client = Client(vertexai=True)

    try:
        print(f"\n[DEBUG] Auditing Internal Policy natively from GCS: {gcs_uri}", flush=True)
        doc_part = types.Part.from_uri(file_uri=gcs_uri, mime_type="application/pdf")
        
        response = await client.aio.models.generate_content(
            model=model_id,
            contents=[
                doc_part, 
                f"You are an internal auditor. Read this confidential corporate policy and answer precisely: {query}"
            ]
        )
        print("\n[DEBUG] Internal audit extraction complete.", flush=True)
        return response.text

    except Exception as e:
        logger.exception(f"Error querying internal document {file_path}")
        return f"Failed to analyze internal document {file_path}: {e}"


def save_audit_findings(findings: str, tool_context: ToolContext) -> str:
    """Saves the calculated compliance gap analysis findings into the session state.
    
    Call this tool in the first turn after performing the delta analysis, so the
    data is available for generating the remediation specification later.
    
    Args:
        findings: The full text or structured data of the findings.
    """
    tool_context.state["audit_findings"] = findings
    logger.info("Saved audit findings to session state.")
    return "Successfully saved findings to session state."


class RemediationTask(BaseModel):
    regulation: str = Field(description="The regulation name, e.g., SEC Form PF (IA-6865)")
    compliance_area: str = Field(description="The specific area of compliance")
    task_id: str = Field(description="A unique task ID, e.g., SEC-PF-001")
    description: str = Field(description="Detailed description of the task and the gap it addresses")
    priority: str = Field(description="Priority level (Critical, High, Medium, Low)")
    due_date: str = Field(description="Due date in YYYY-MM-DD format")
    assigned_to: str = Field(description="Team or role assigned to the task")

class RemediationSpec(BaseModel):
    remediation_tasks: list[RemediationTask]

def write_remediation_spec_to_gcs(
    filename: str, tool_context: ToolContext
) -> dict:
    """Writes the machine-readable IT remediation JSON specification to the developer handoff bucket.

    This tool reads the findings from the saved session state and converts them to JSON
    using controlled generation to ensure a strict schema.

    Args:
        filename: The name of the file to save (e.g., 'SEC_compliance_portal_spec.json').
        tool_context: ADK tool context to read state.
    """
    if tool_context.state.get("spec_exported"):
        logger.info("write_remediation_spec_to_gcs: already exported this session, skipping.")
        return {
            "status": "skipped",
            "detail": "JSON specification was already exported to GCS this session.",
        }

    findings = tool_context.state.get("audit_findings")
    if not findings:
        return {"status": "error", "detail": "No saved audit findings found in session state."}

    bucket_name = os.getenv("FSI_DEV_HANDOFF_BUCKET")
    if not bucket_name:
        return {"status": "error", "detail": "FSI_DEV_HANDOFF_BUCKET env var not set."}

    try:
        # Call LLM to convert findings to JSON using controlled generation
        from google.genai import Client as GenAIClient
        client = GenAIClient(vertexai=True)
        model_id = os.getenv("ADK_MODEL", "gemini-3-flash-preview")
        
        prompt = f"""
        You are a compliance data engineer. Convert these findings into a machine-readable JSON specification for IT remediation.
        
        Findings:
        {findings}
        """
        
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RemediationSpec,
                temperature=0.1,
            ),
        )
        spec_json_content = response.text.strip()
        
        storage_client = gcs_storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(filename)
        
        blob.upload_from_string(spec_json_content, content_type="application/json")
        
        from urllib.parse import quote
        safe_filename = quote(filename, safe="/")
        auth_url = f"https://storage.cloud.google.com/{bucket_name}/{safe_filename}"
        
        logger.info("Exported Remediation Spec to GCS: %s", auth_url)
        tool_context.state["spec_exported"] = True
        
        return {
            "status": "success",
            "gcs_uri": auth_url,
            "detail": f"Successfully wrote IT specification. Link: {auth_url}",
        }
    except Exception as e:
        logger.exception("Error writing specification to GCS")
        return {"status": "error", "detail": f"Failed to write to GCS: {e}"}


# Removed export_report_to_google_doc and _parse_markdown_to_doc_requests as they are not used.

# ---------------------------------------------------------------------------
# Inject A2UI output instructions into the agent's system prompt.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Inject A2UI output instructions into the agent's system prompt.
# ---------------------------------------------------------------------------
_A2UI_INSTRUCTIONS = f"""

---

## A2UI Rich UI Output

**IMPORTANT**: Only use A2UI output when the user directly @-mentions you to generate the visual compliance cards or dashboard (Turn 2). For all other requests (when called as a tool by the Orchestrator in Turn 1, or when asked to export the IT spec JSON to GCS in Turn 3), respond with plain text ONLY — do NOT include the `---a2ui_JSON---` delimiter or any A2UI JSON.

To generate the A2UI response in Turn 2, you MUST follow these rules:
1. Your response MUST be in two parts, separated by the delimiter: `---a2ui_JSON---`.
2. The first part is a brief conversational text response acknowledging dashboard generation. Do NOT repeat the full markdown table or executive summary in this conversational text.
3. The second part is a single, raw JSON array of A2UI messages containing the compliance cards.
4. The JSON part MUST validate against the A2UI JSON SCHEMA provided below.
5. You MUST generate exactly ONE compliance card for every single row in your Detailed Findings table from Turn 1. Do not omit, merge, or skip any rows (e.g., if your table has 10 rows, you MUST output exactly 10 cards in your JSON array).
6. For each card, map the columns from the findings table as follows:
   - **Title**: Use the `Requirement Source` value (e.g., `SEC IA-6865 (Q 7a)` or `FINRA Rule 3310`) as the card's title.
   - **Body Text**: Use the `Altered Text` value as the card's body text. Do NOT prepend any raw priority labels (like "CRITICAL VIOLATION:" or "HIGH PRIORITY:") to this text; keep the card description clean and professional.
   - **Icon/Badge**: Use the `Remediation Priority` value to determine the exact header badge text:
     * Use `🔴 CRITICAL` for `CRITICAL` priority.
     * Use `🔴 HIGH` for `HIGH` priority.
     * Use `🟡 MEDIUM` for `MEDIUM` priority.
     * Use `🟡 LOW` for `LOW` priority.


--- A2UI DASHBOARD EXAMPLE ---
{COMPLIANCE_A2UI_EXAMPLE}

---BEGIN A2UI JSON SCHEMA---

{A2UI_SCHEMA}

---END A2UI JSON SCHEMA---
"""

root_agent = Agent(
    name="fs_policy_auditor",
    model=Gemini(
        model=model_id,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Performs deep semantic gap analyses by comparing external regulatory mandates "
        "(e.g., SEC Form PF, FINRA AML) against the firm's confidential internal policies. "
        "Identifies critical compliance violations, outputs A2UI reports, generates Google Docs, "
        "and writes remediation IT JSON task files to GCS."
    ),
    instruction=system_prompt + "\n\n" + system_instructions + _A2UI_INSTRUCTIONS,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2, # Strict accuracy required for legal document comparison
        max_output_tokens=8192,
    ),
    tools=[
        LongRunningFunctionTool(func=request_user_input),
        LongRunningFunctionTool(func=read_internal_policy),
        write_remediation_spec_to_gcs,
        save_audit_findings,
    ],
)

# Force version injection for native Agent Engine A2A Agent Card generation
object.__setattr__(root_agent, "version", os.getenv("AGENT_VERSION", "0.1.0"))

app = App(
    root_agent=root_agent,
    name="app",
)

# ==============================================================================
# BIND THE A2UI EXECUTOR FOR AGENT ENGINE NATIVE DEPLOYMENT
# ==============================================================================
from .a2ui_executor import A2UIAgentExecutor

# ==============================================================================
# BIND THE A2UI EXECUTOR FOR AGENT ENGINE NATIVE DEPLOYMENT
# ==============================================================================
from google.adk import runners
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from .a2ui_executor import A2UIAgentExecutor

class PolicyAuditorExecutorBuilder(A2UIAgentExecutor):
    """Zero-argument wrapper to instantiate the Runner for Vertex AI Agent Engine."""
    def __init__(self, **kwargs):
        runner_instance = runners.Runner(
            app_name="PolicyAuditor",
            agent=root_agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService()
        )
        super().__init__(runner=runner_instance)