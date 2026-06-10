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
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
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
        # Replace year placeholders so prompts always reference the current year.
        return text.replace("{current_year}", str(_CURRENT_YEAR)).replace(
            "{next_year}", str(_NEXT_YEAR)
        )
    except FileNotFoundError:
        logger.warning("Prompt file %s not found.", prompt_name)
        return ""


system_prompt = load_prompt("system_prompt.md")
system_instructions = load_prompt("system_instructions.md")
model_id = os.getenv("ADK_MODEL", "gemini-2.5-flash")


# Use setdefault so .env values are respected; only fill in if not already set.
_, _default_project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _default_project_id or "")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


async def query_external_regulations(file_path: str, query: str) -> str:
    """Queries a specific regulatory document stored in the secure external GCS bucket.
    
    Use this tool to extract precise mandates, timelines, and rules from SEC and FINRA 
    PDFs stored in the firm's controlled regulatory bucket.

    Args:
        file_path: The exact folder and filename of the document in the bucket
                   (e.g., 'sec/ia-6546.pdf', 'sec/ia-6865.pdf', or 'finra/finra-3310.pdf').
        query: The specific extraction request.
    
    Returns:
        The extracted rule and context from the document, or an error message.
    """
    bucket_name = os.getenv("FSI_EXTERNAL_REGS_BUCKET")
    if not bucket_name:
        return "Error: FSI_EXTERNAL_REGS_BUCKET environment variable is not set."

    # Now correctly handles the folder structure you created
    gcs_uri = f"gs://{bucket_name}/{file_path}"
    
    # Initialize the Vertex AI Gemini Client natively 
    client = Client(vertexai=True)

    try:
        print(f"\n[DEBUG] Analyzing Regulatory Document natively from GCS: {gcs_uri}", flush=True)
        
        # Gemini natively supports reading PDFs directly via GCS URI (VPC-SC compliant)
        doc_part = types.Part.from_uri(file_uri=gcs_uri, mime_type="application/pdf")
        
        response = await client.aio.models.generate_content(
            model=model_id,
            contents=[
                doc_part, 
                f"You are a compliance officer. Read this regulatory document and answer the following query precisely: {query}"
            ]
        )
        
        print("\n[DEBUG] Extraction complete.", flush=True)
        
        # Flawless URL construction: URL-encode the path to handle spaces/special characters
        from urllib.parse import quote
        safe_file_path = quote(file_path, safe="/")
        auth_url = f"https://storage.cloud.google.com/{bucket_name}/{safe_file_path}"
        
        # Return both the content and the exact URL to the LLM
        return f"--- DOCUMENT CONTENT ---\n{response.text}\n\n--- SOURCE URL ---\n{auth_url}"

    except Exception as e:
        logger.exception(f"Error querying document {file_path}")
        return f"Failed to analyze regulatory document {file_path}: {e}"


shared_model = Gemini(
    model=model_id,
    retry_options=types.HttpRetryOptions(attempts=3),
)

root_agent = LlmAgent(
    name="Regulatory_Intel_Agent",
    model=shared_model,
    description=(
        "A regulatory intelligence agent that securely reads and extracts key compliance mandates "
        "from dense regulatory documents (e.g., SEC Form PF Reporting Requirements, FINRA AML policies) "
        "stored entirely within the firm's secure Google Cloud Storage perimeter."
    ),
    instruction=f"{system_prompt}\n\n{system_instructions}",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2, # Lowered temperature for strict legal/regulatory accuracy
        max_output_tokens=8192,
    ),
    tools=[
        query_external_regulations,
    ],
)

# Force version injection for native Agent Engine A2A Agent Card generation
object.__setattr__(root_agent, "version", os.getenv("AGENT_VERSION", "0.1.0"))

app = App(
    root_agent=root_agent,
    name="app",
)

# ==============================================================================
# PASTE THIS NEW BLOCK AT THE ABSOLUTE BOTTOM OF THE FILE
# ==============================================================================
from google.adk.runners import Runner
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor

class RegulatoryExecutor(A2aAgentExecutor):
    def __init__(self, **kwargs):
        runner = Runner(
            app_name="RegulatoryIntel",
            agent=root_agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService()
        )
        super().__init__(runner=runner, **kwargs)