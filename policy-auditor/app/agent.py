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


#-------------------------------------------    
# calling regulatory intel agent  
#-------------------------------------------  

async def query_regulatory_intel(query: str, tool_context: ToolContext) -> str:
    """ALWAYS call this tool FIRST to extract external regulatory mandates (e.g., SEC or FINRA rules).
    
    Args:
        query: What rules to extract. You MUST ask for detailed tables with a minimum of 7 rows.
        tool_context: ADK tool context to prevent duplicate network calls.
    """
    if tool_context.state.get("intel_fetched"):
        return tool_context.state.get("intel_data")

    endpoint_url = os.environ.get("REGULATORY_INTEL_AGENT_ID")
    if not endpoint_url:
        return "Error: REGULATORY_INTEL_AGENT_ID not set."

    # Construct URLs based on Medium article
    card_url = f"https://us-central1-aiplatform.googleapis.com/v1beta1/{endpoint_url}/a2a/v1/card"
    message_send_url = f"https://us-central1-aiplatform.googleapis.com/v1beta1/{endpoint_url}/a2a/v1/message:send"

    auth_headers = _get_a2a_auth_headers(endpoint_url)
    
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            # Step 1: Fetch Agent Card to verify active
            logger.info(f"Fetching agent card from {card_url}")
            card_response = await client.get(card_url, headers=auth_headers)
            card_response.raise_for_status()
            logger.info("Agent card fetched successfully.")

            # Step 2: Send message with new payload schema (no input wrapper)
            payload = {
                "request": {
                    "message_id": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "content": [{"text": query}]
                },
                "configuration": {
                    "blocking": True
                }
            }
            
            logger.info(f"Sending query to {message_send_url}")
            response = await client.post(message_send_url, json=payload, headers=auth_headers)
            response.raise_for_status()
            
            result_json = response.json()
            logger.info(f"Response received: {result_json}")
            print(f"RAW REG INTEL RESPONSE: {result_json}", flush=True)
            
            # Fallback to string representation if structure is unknown
            result = str(result_json)
            
            tool_context.state["intel_fetched"] = True
            tool_context.state["intel_data"] = result
            return result
            
    except Exception as e:
        logger.exception("Failed to fetch regulatory intel")
        return f"Failed to fetch regulatory intel: {e}"

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


def write_remediation_spec_to_gcs(
    spec_json_content: str, filename: str, tool_context: ToolContext
) -> dict:
    """Writes the machine-readable IT remediation JSON specification to the developer handoff bucket.

    Call this exactly ONCE per request after identifying a compliance gap, so the 
    IT team can use the Agents CLI to automatically scaffold the fix.

    Args:
        spec_json_content: The raw, structured JSON specification string.
        filename: The name of the file to save (e.g., 'SEC_compliance_portal_spec.json').
        tool_context: ADK tool context to prevent duplicate writes.

    Returns:
        A dict with status and the GCS URI of the written file.
    """
    if tool_context.state.get("spec_exported"):
        logger.info("write_remediation_spec_to_gcs: already exported this session, skipping.")
        return {
            "status": "skipped",
            "detail": "JSON specification was already exported to GCS this session.",
        }

    bucket_name = os.getenv("FSI_DEV_HANDOFF_BUCKET")
    if not bucket_name:
        return {"status": "error", "detail": "FSI_DEV_HANDOFF_BUCKET env var not set."}

    try:
        storage_client = gcs_storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(filename)
        
        blob.upload_from_string(spec_json_content, content_type="application/json")
        
        # Flawless URL construction: URL-encode the path to handle spaces/special characters
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


def _parse_markdown_to_doc_requests(text: str) -> list[dict]:
    """Converts markdown text to Google Docs API batchUpdate requests."""
    lines = text.split("\n")
    plain_lines: list[str] = []
    heading_ranges: list[tuple[int, int, str]] = []  
    bold_ranges: list[tuple[int, int]] =[]

    offset = 1  
    for line in lines:
        heading_style = None
        clean_line = line
        if line.startswith("#### "):
            heading_style = "HEADING_4"
            clean_line = line[5:]
        elif line.startswith("### "):
            heading_style = "HEADING_3"
            clean_line = line[4:]
        elif line.startswith("## "):
            heading_style = "HEADING_2"
            clean_line = line[3:]
        elif line.startswith("# "):
            heading_style = "HEADING_1"
            clean_line = line[2:]

        processed = ""
        i = 0
        while i < len(clean_line):
            if clean_line[i : i + 2] == "**":
                end = clean_line.find("**", i + 2)
                if end != -1:
                    bold_start = offset + len(processed)
                    inner = clean_line[i + 2 : end]
                    processed += inner
                    bold_end = offset + len(processed)
                    bold_ranges.append((bold_start, bold_end))
                    i = end + 2
                    continue
            processed += clean_line[i]
            i += 1

        line_text = processed + "\n"
        if heading_style:
            heading_ranges.append((offset, offset + len(line_text) - 1, heading_style))

        plain_lines.append(line_text)
        offset += len(line_text)

    full_text = "".join(plain_lines)
    requests: list[dict] =[{"insertText": {"location": {"index": 1}, "text": full_text}}]

    for start, end, style in heading_ranges:
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }
        })

    for start, end in bold_ranges:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })

    return requests


def export_report_to_google_doc(
    report_content: str,
    tool_context: ToolContext,
) -> dict:
    """Creates a Google Doc with the compliance gap analysis and saves it to a Shared Drive.

    Args:
        report_content: The full markdown content of the gap analysis.
        tool_context: ADK tool context used to prevent duplicate exports.

    Returns:
        A dict with status, the Google Doc URL, and the document title.
    """
    if tool_context.state.get("doc_exported"):
        return {
            "status": "skipped",
            "detail": "Report was already exported to Google Drive this session.",
            "url": tool_context.state.get("doc_url", ""),
        }

    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        return {"status": "error", "detail": "GOOGLE_DRIVE_FOLDER_ID env var not set."}

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/documents"]
        )
        credentials.refresh(google.auth.transport.requests.Request())

        drive_service = google_api_build("drive", "v3", credentials=credentials)
        docs_service = google_api_build("docs", "v1", credentials=credentials)

        today = datetime.date.today()
        iso_year, iso_week, _ = today.isocalendar()
        doc_title = f"Compliance Gap Analysis - {iso_year}-W{iso_week:02d}"

        query = (
            f"name contains '{iso_year}-W{iso_week:02d}' "
            f"and '{folder_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.document' and trashed = false"
        )
        try:
            existing_files = drive_service.files().list(
                q=query, corpora="drive", driveId=folder_id,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
                fields="files(id, name, webViewLink)",
            ).execute().get("files",[])
        except Exception:
            existing_files =[]

        doc_requests = _parse_markdown_to_doc_requests(report_content)

        if existing_files:
            existing = existing_files[0]
            doc_id = existing["id"]
            doc_url = existing.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}")
            
            try:
                drive_service.files().update(
                    fileId=doc_id, body={"name": doc_title},
                    supportsAllDrives=True, fields="id",
                ).execute()
            except Exception:
                pass

            try:
                existing_doc = docs_service.documents().get(documentId=doc_id).execute()
                body_end = existing_doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
                clear_and_insert =[]
                if body_end > 2:
                    clear_and_insert.append({
                        "deleteContentRange": {"range": {"startIndex": 1, "endIndex": body_end - 1}}
                    })
                clear_and_insert.extend(doc_requests)
                if clear_and_insert:
                    docs_service.documents().batchUpdate(
                        documentId=doc_id, body={"requests": clear_and_insert},
                    ).execute()
            except Exception as update_err:
                return {"status": "error", "detail": f"Could not update doc: {update_err}"}
        else:
            try:
                doc = drive_service.files().create(
                    body={"name": doc_title, "mimeType": "application/vnd.google-apps.document", "parents":[folder_id]},
                    supportsAllDrives=True, fields="id, webViewLink",
                ).execute()
                doc_id = doc["id"]
                doc_url = doc.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}")
            except Exception as create_err:
                return {"status": "error", "detail": f"Could not create Doc: {create_err}"}

            try:
                if doc_requests:
                    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": doc_requests}).execute()
            except Exception as content_err:
                return {"status": "partial", "url": doc_url, "title": doc_title, "detail": f"Doc created, content failed: {content_err}"}

        tool_context.state["doc_exported"] = True
        tool_context.state["doc_url"] = doc_url

        return {"status": "success", "url": doc_url, "title": doc_title}

    except Exception as e:
        logger.exception("Error creating Google Doc")
        return {"status": "error", "detail": f"Error creating Google Doc: {e}"}

def write_audit_report_to_gcs(
    report_markdown: str, filename: str, tool_context: ToolContext
) -> dict:
    """Converts the Markdown gap analysis into a styled document and writes it to GCS.

    Call this tool to generate the human-readable Business Report.

    Args:
        report_markdown: The full markdown text, including the delta table.
        filename: The name of the file to save (e.g., 'Audit_Report.html').
        tool_context: ADK tool context to prevent duplicate writes.

    Returns:
        A dict with status and the secure GCS URI of the written report.
    """
    if tool_context.state.get("report_exported"):
        return {"status": "skipped", "detail": "Report was already exported."}

    bucket_name = os.getenv("FSI_DEV_HANDOFF_BUCKET")
    if not bucket_name:
        return {"status": "error", "detail": "FSI_DEV_HANDOFF_BUCKET env var not set."}

    try:
        import markdown
        # Convert Markdown to HTML and inject styling to make it look like a professional document
        html_body = markdown.markdown(report_markdown, extensions=['tables'])
        styled_html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: 'Arial', sans-serif; padding: 40px; max-width: 900px; margin: auto; color: #1a1a1a; }}
                table {{ border-collapse: collapse; width: 100%; margin-top: 20px; margin-bottom: 20px; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #f4f7f9; color: #0033a0; font-weight: bold; }}
                h1, h2, h3 {{ color: #0033a0; }}
            </style>
        </head>
        <body>{html_body}</body>
        </html>
        """

        storage_client = gcs_storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(filename)
        
        # Upload as HTML so it renders natively in the browser when clicked
        blob.upload_from_string(styled_html, content_type="text/html")
        
        from urllib.parse import quote
        safe_filename = quote(filename, safe="/")
        auth_url = f"https://storage.cloud.google.com/{bucket_name}/{safe_filename}"
        
        logger.info("Exported Audit Report to GCS: %s", auth_url)
        tool_context.state["report_exported"] = True
        
        return {
            "status": "success",
            "gcs_uri": auth_url,
            "detail": f"Successfully wrote Business Report. Link: {auth_url}",
        }
    except Exception as e:
        logger.exception("Error writing report to GCS")
        return {"status": "error", "detail": f"Failed to write report to GCS: {e}"}

def create_compliance_jira_tasks_tool(remediation_spec_gcs_uri: str) -> str:
    """Parses the remediation spec JSON from GCS and creates separate Jira tickets for each task.
    
    Args:
        remediation_spec_gcs_uri: The GCS URI (gs://...) of the remediation spec JSON file.
    """
    from atlassian import Jira
    import json
    from google.cloud import storage
    
    email = os.environ.get("JIRA_EMAIL", "vikib4u@gmail.com")
    api_token = os.environ.get("JIRA_API_TOKEN")
    jira_url = "https://vikib4u.atlassian.net/"
    
    if not api_token:
        return "Error: JIRA_API_TOKEN environment variable is not set."
        
    try:
        # Download from GCS
        client = storage.Client()
        if not remediation_spec_gcs_uri.startswith("gs://"):
            return "Error: Invalid GCS URI. Must start with gs://"
            
        path_parts = remediation_spec_gcs_uri[5:].split("/", 1)
        bucket_name = path_parts[0]
        blob_name = path_parts[1]
        
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        spec_bytes = blob.download_as_bytes()
        spec = json.loads(spec_bytes)
        
        tasks = spec.get("remediation_tasks", [])
        
        if not tasks:
            return "No remediation tasks found in the provided JSON."
            
        jira = Jira(url=jira_url, username=email, password=api_token, cloud=True)
        created_issues = []
        
        for task in tasks:
            regulation = task.get("regulation", "Unknown Regulation")
            description = task.get("description", "No description provided.")
            priority = task.get("priority", "Medium")
            due_date = task.get("due_date", "No due date")
            
            summary = f"Compliance Task: {regulation}"
            
            full_description = f"{description}\n\nPriority: {priority}\nDue Date: {due_date}\n\nDetails:\n"
            
            details = task.get("details", [])
            for detail in details:
                if "field_instruction" in detail:
                    full_description += f"- **{detail['field_instruction']}**: {detail['change_required']} (Impact: {detail['impact']})\n"
                elif "policy_area" in detail:
                    full_description += f"- **{detail['policy_area']}**: {detail['remediation_action']} (Severity: {detail['gap_severity']})\n"
            
            issue = jira.issue_create(
                fields={
                    'project': {'key': 'SCRUM'},
                    'summary': summary,
                    'description': full_description,
                    'issuetype': {'name': 'Task'}
                }
            )
            created_issues.append(issue.get('key'))
            
        return f"Success! Created {len(created_issues)} JIRA issues: {', '.join(created_issues)}"
        
    except Exception as e:
        return f"Error creating JIRA issues: {e}"

# ---------------------------------------------------------------------------
# Inject A2UI output instructions into the agent's system prompt.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Inject A2UI output instructions into the agent's system prompt.
# ---------------------------------------------------------------------------
_A2UI_INSTRUCTIONS = f"""

---

## A2UI Rich UI Output

**IMPORTANT**: Your final output MUST include A2UI UI JSON to render the compliance dashboard.
To generate the response, you MUST follow these rules:

1. Your response MUST be in two parts, separated by the delimiter: `---a2ui_JSON---`.
2. The first part is your conversational text response. This MUST include your Executive Summary, the Detailed Findings Markdown table, and the Handoff Links from your tools.
3. The second part is a single, raw JSON array of A2UI messages.
4. The JSON part MUST validate against the A2UI JSON SCHEMA provided below.
5. You MUST dynamically populate the `literalString` values in the A2UI JSON using the `Field/Instruction` as the title and the `Altered Text` as the body for each row in your findings table, and NOT simply copy the placeholders from the example below.

--- FINAL OUTPUT TEMPLATE (CRITICAL MANDATE) ---

Your response MUST perfectly match this exact structural template:

**Executive Summary**
* **SEC Form PF:** [Summary]
* **FINRA Rule 3310:** [Summary]

### Detailed SEC Delta[Insert the EXACT `report_markdown` string passed to your tool call here]

**Handoff Links**
* 📄 **Official Audit Report saved**:[View Audit Report](URL_FROM_AUDIT_REPORT_TOOL)
* ✅ **IT Remediation Specification written to GCS**:[remediation_spec.json](URL_FROM_REMEDIATION_SPEC_TOOL)

---a2ui_JSON---
{COMPLIANCE_A2UI_EXAMPLE}

---BEGIN A2UI JSON SCHEMA---

{A2UI_SCHEMA}

---END A2UI JSON SCHEMA---
"""

root_agent = Agent(
    name="Internal_Policy_Auditor",
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
        LongRunningFunctionTool(func=query_regulatory_intel),
        LongRunningFunctionTool(func=read_internal_policy),
        #export_report_to_google_doc,
        write_remediation_spec_to_gcs,
        write_audit_report_to_gcs,
        create_compliance_jira_tasks_tool,
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