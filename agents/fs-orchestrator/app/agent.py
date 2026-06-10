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
import json as _json
import logging
import os
import pathlib
import re
import uuid

import google.auth
import google.auth.transport.requests
import httpx
import requests
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

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
model_id = os.getenv("ADK_MODEL", "gemini-3-flash-preview")


# ---------------------------------------------------------------------------
# Shared credentials — single google.auth.default() call for the whole module.
# Reused by _extract_token() (for access tokens) and for project_id discovery.
# Fixes C6/E5: eliminates duplicate google.auth.default() calls.
# ---------------------------------------------------------------------------
_cached_adc_credentials, _default_project_id = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

# Use setdefault so .env values are respected; only fill in if not already set.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _default_project_id or "")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# ---------------------------------------------------------------------------
# E3: Initialize aiplatform once at module level (lazy, on first import).
# ---------------------------------------------------------------------------
_aiplatform_initialized = False


def _ensure_aiplatform_init() -> None:
    """Initialize the Vertex AI SDK exactly once."""
    global _aiplatform_initialized
    if not _aiplatform_initialized:
        from google.cloud import aiplatform

        aiplatform.init()
        _aiplatform_initialized = True


# ---------------------------------------------------------------------------
# E1/E2: Module-level A2A client cache — reuses httpx connections and
# avoids re-discovering the agent card on every call to the same endpoint.
# ---------------------------------------------------------------------------
_a2a_client_cache: dict[str, tuple[httpx.AsyncClient, object]] = {}
_a2a_client_locks: dict[str, asyncio.Lock] = {}


async def query_regulatory_intel_agent(
    query: str,
    tool_context: ToolContext,
) -> str:
    """Sends a query to the Regulatory Intel Agent to extract rules from external regulations.

    Args:
        query: The extraction query (e.g., "Extract master-feeder fund reporting rules from SEC Form PF").
        tool_context: ADK tool context for session state tracking.

    Returns:
        Structured extraction of regulatory rules.
    """
    cached = tool_context.state.get("_intel_result")
    if cached:
        logger.info("query_regulatory_intel_agent: returning cached result.")
        return cached

    endpoint_url = os.environ.get("REGULATORY_INTEL_AGENT_URL", "")
    if not endpoint_url:
        return "Error: REGULATORY_INTEL_AGENT_URL environment variable is not set."

    if _AGENT_ENGINE_PATTERN.match(endpoint_url):
        result = await _call_agent_engine(endpoint_url, query)
    else:
        result = await _send_a2a_message(endpoint_url, query)

    tool_context.state["_intel_result"] = result
    return result


async def _extract_token(tool_context: ToolContext) -> str | None:
    """Extracts an access token from ToolContext or falls back to default credentials.

    Priority order:
      1. ADK session state (populated by Agent Engine / Orcas).
      2. Application Default Credentials (Cloud Run service account).

    Note: the inbound request Authorization header contains a Cloud Run
    **ID token** (audience = Cloud Run URL), NOT an access token suitable
    for calling Google APIs like Discovery Engine.  It is intentionally
    not used here.
    """
    global _cached_adc_credentials

    auth_id = os.environ.get("ADK_AUTH_ID")
    access_token = None

    # 1. Try ADK session state (e.g. Agent Engine injects the user token here).
    if auth_id and auth_id in tool_context.state:
        access_token = tool_context.state[auth_id]
        logger.debug("Using access token from ADK session state (key=%s).", auth_id)

    # 2. Fall back to Application Default Credentials (service account).
    #    Cache the credentials object and only refresh when the token has expired.
    #    The refresh() call does synchronous HTTP I/O, so we offload it to a
    #    thread to avoid blocking the asyncio event loop.
    if not access_token:
        if not _cached_adc_credentials.valid or _cached_adc_credentials.expired:
            await asyncio.to_thread(
                _cached_adc_credentials.refresh,
                google.auth.transport.requests.Request(),
            )
        access_token = _cached_adc_credentials.token
        logger.debug("Using access token from Application Default Credentials.")

    return access_token





# ---------------------------------------------------------------------------
# Remote Agent (A2A) Helpers
# ---------------------------------------------------------------------------

_AGENT_ENGINE_PATTERN = re.compile(
    r"^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$"
)


def _get_a2a_auth_headers(endpoint_url: str) -> dict[str, str]:
    """Returns auth headers appropriate for the endpoint type.

    - HTTP(S) endpoints (localhost / Cloud Run): uses a Google ID token
      for Cloud Run or no auth for localhost.
    - Agent Engine resource names are handled separately by the Vertex SDK.

    Used for both Market Research and Product Strategy A2A agents.
    """
    if endpoint_url.startswith("http://localhost") or endpoint_url.startswith(
        "http://127.0.0.1"
    ):
        # Local development — no auth required.
        return {}

    # Cloud Run or other HTTPS endpoints — use an ID token.
    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token as google_id_token

    try:
        token = google_id_token.fetch_id_token(Request(), endpoint_url)
        return {"Authorization": f"Bearer {token}"}
    except DefaultCredentialsError as e:
        logger.warning(
            "Could not fetch ID token for %s: %s. Assuming public endpoint.",
            endpoint_url,
            e,
        )
        return {}


async def _get_or_create_a2a_client(
    endpoint_url: str,
) -> tuple[httpx.AsyncClient, object]:
    """Returns a cached (httpx_client, a2a_client) pair for the endpoint.

    E1/E2 fix: Reuses the httpx connection pool and avoids re-discovering
    the agent card on every call to the same endpoint.

    Uses a per-endpoint asyncio.Lock to prevent race conditions where
    concurrent requests could create duplicate clients for the same endpoint.
    """
    # Fast path: already cached — no lock needed.
    if endpoint_url in _a2a_client_cache:
        return _a2a_client_cache[endpoint_url]

    # Ensure a lock exists for this endpoint (setdefault is atomic in CPython).
    lock = _a2a_client_locks.setdefault(endpoint_url, asyncio.Lock())

    async with lock:
        # Double-check after acquiring the lock.
        if endpoint_url in _a2a_client_cache:
            return _a2a_client_cache[endpoint_url]

        auth_headers = _get_a2a_auth_headers(endpoint_url)
        base_url = endpoint_url.rstrip("/")

        httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            headers=auth_headers,
        )
        client_config = ClientConfig(
            httpx_client=httpx_client,
            streaming=True,
        )
        a2a_client = await ClientFactory.connect(
            base_url,
            client_config=client_config,
            resolver_http_kwargs={"headers": auth_headers} if auth_headers else None,
        )

        _a2a_client_cache[endpoint_url] = (httpx_client, a2a_client)
        return httpx_client, a2a_client


async def _send_a2a_message(endpoint_url: str, message_text: str) -> str:
    """Sends a message to a remote A2A agent and returns the text response.

    Supports HTTP/HTTPS endpoints (localhost, Cloud Run). For Agent Engine
    endpoints, a separate path is used.
    """
    try:
        _, client = await _get_or_create_a2a_client(endpoint_url)

        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=message_text))],
            messageId=f"orchestrator-{uuid.uuid4()}",
        )
        final_state = None
        event_count = 0
        async for event in client.send_message(message):
            event_count += 1
            # The event stream yields tuples of (aggregated_state, raw_event)
            if isinstance(event, tuple) and len(event) > 0:
                state_obj = event[0]
            else:
                state_obj = event

            if state_obj is not None:
                final_state = state_obj

        # Helper function to recursively extract text or serialized JSON data from any Part object/dict
        def _extract_text_from_part(p) -> str:
            if p is None:
                return ""
            # Unwrap 'root' if present (Pydantic or dict)
            if hasattr(p, "root"):
                return _extract_text_from_part(p.root)
            if isinstance(p, dict) and "root" in p:
                return _extract_text_from_part(p["root"])

            # Extract 'text' (Pydantic or dict)
            if hasattr(p, "text") and p.text:
                return p.text
            if isinstance(p, dict) and "text" in p and p["text"]:
                return p["text"]

            # Extract 'data' (Pydantic or dict)
            if hasattr(p, "data") and p.data:
                try:
                    return _json.dumps(p.data)
                except Exception:
                    return str(p.data)
            if isinstance(p, dict) and "data" in p and p["data"]:
                try:
                    return _json.dumps(p["data"])
                except Exception:
                    return str(p["data"])

            # Recursively check for nested parts inside a part (like message parts)
            if hasattr(p, "parts") and getattr(p, "parts"):
                sub_texts = []
                for sub_p in p.parts:
                    txt = _extract_text_from_part(sub_p)
                    if txt:
                        sub_texts.append(txt)
                return "\n".join(sub_texts)
            if isinstance(p, dict) and "parts" in p and p["parts"]:
                sub_texts = []
                for sub_p in p["parts"]:
                    txt = _extract_text_from_part(sub_p)
                    if txt:
                        sub_texts.append(txt)
                return "\n".join(sub_texts)

            return ""

        if final_state is None:
            return (
                "The remote agent did not return any response "
                "(the event stream was empty)."
            )

        # Extract text from the final_state's parts, history, or artifacts
        texts = []
        
        # 1. Check if final_state contains a history (Task object pattern)
        history = None
        if hasattr(final_state, "history"):
            history = final_state.history
        elif isinstance(final_state, dict) and "history" in final_state:
            history = final_state["history"]

        if history:
            print(f"[DEBUG EXTRACTION] Inspecting Task history. Messages count: {len(history)}.", flush=True)
            # A Task contains a list of Messages. The last non-user message is the final agent response.
            for msg in reversed(history):
                role = None
                if hasattr(msg, "role"):
                    role = msg.role
                elif isinstance(msg, dict) and "role" in msg:
                    role = msg["role"]

                role_str = str(role).lower() if role else ""
                if "user" in role_str:
                    continue

                parts = None
                if hasattr(msg, "parts"):
                    parts = msg.parts
                elif isinstance(msg, dict) and "parts" in msg:
                    parts = msg["parts"]

                if parts:
                    for part in parts:
                        txt = _extract_text_from_part(part)
                        if txt:
                            print(f"[DEBUG EXTRACTION] Extracted from history: {txt[:150].replace(chr(10), ' ')}...", flush=True)
                            texts.append(txt)
                
                # Break on the first non-user message we find starting from the end
                if texts:
                    break

        # 2. Check for direct parts (Message or direct response)
        parts = None
        if hasattr(final_state, "parts"):
            parts = final_state.parts
        elif isinstance(final_state, dict) and "parts" in final_state:
            parts = final_state["parts"]

        if parts:
            for part in parts:
                txt = _extract_text_from_part(part)
                if txt:
                    print(f"[DEBUG EXTRACTION] Extracted from direct parts: {txt[:150].replace(chr(10), ' ')}...", flush=True)
                    texts.append(txt)

        # 3. Check for task-level artifacts (e.g., for custom executors like Policy Auditor)
        artifacts = None
        if hasattr(final_state, "artifacts"):
            artifacts = final_state.artifacts
        elif isinstance(final_state, dict) and "artifacts" in final_state:
            artifacts = final_state["artifacts"]

        if artifacts:
            print(f"[DEBUG EXTRACTION] Inspecting Task artifacts. Artifacts count: {len(artifacts)}.", flush=True)
            for artifact in artifacts:
                txt = _extract_text_from_part(artifact)
                if txt:
                    print(f"[DEBUG EXTRACTION] Extracted from task artifact: {txt[:150].replace(chr(10), ' ')}...", flush=True)
                    texts.append(txt)

        print(f"[DEBUG EXTRACTION] Extraction complete. Blocks count: {len(texts)}.", flush=True)

        if texts:
            return "\n".join(texts)

        return (
            "The remote agent returned a response but no "
            "extractable text was found."
        )

    except Exception as e:
        logger.exception("Error communicating with remote A2A agent at %s", endpoint_url)
        return f"Error communicating with remote A2A agent: {e}"

async def _call_agent_engine(resource_name: str, message_text: str) -> str:
    """Calls a remote agent deployed on Vertex Agent Engine."""
    try:
        from google.cloud import aiplatform

        _ensure_aiplatform_init()
        # Use the reasoning engine API to send a query.
        engine = aiplatform.reasoning_engines.ReasoningEngine(resource_name)
        response = await asyncio.to_thread(engine.query, input=message_text)
        if isinstance(response, dict) and "output" in response:
            return response["output"]
        return str(response)
    except Exception as e:
        logger.exception("Error communicating with Agent Engine")
        return f"Error communicating with Agent Engine: {e}"


async def query_policy_auditor_agent(
    rules: str,
    tool_context: ToolContext,
) -> str:
    """Sends the extracted regulatory rules to the Policy Auditor Agent to perform gap analysis.

    Args:
        rules: The extracted regulatory rules (from Regulatory Intel Agent).
        tool_context: ADK tool context for session state tracking.

    Returns:
        Gap analysis report and A2UI cards.
    """
    cached = tool_context.state.get("_audit_result")
    if cached:
        logger.info("query_policy_auditor_agent: returning cached result.")
        return cached

    endpoint_url = os.environ.get("POLICY_AUDITOR_AGENT_URL", "")
    if not endpoint_url:
        return "Error: POLICY_AUDITOR_AGENT_URL environment variable is not set."

    # We just pass the rules as the message to the policy auditor
    if _AGENT_ENGINE_PATTERN.match(endpoint_url):
        result = await _call_agent_engine(endpoint_url, rules)
    else:
        result = await _send_a2a_message(endpoint_url, rules)

    tool_context.state["_audit_result"] = result
    return result


shared_model = Gemini(
    model=model_id,
    retry_options=types.HttpRetryOptions(attempts=3),
)

# EF2: Removed research_and_analyze from the tool list — the LLM should use
# the sequential query_market_research_agent → query_data_insight_agent flow
# instead, since the data query typically depends on research results.
root_agent = LlmAgent(
    name="fs_orchestrator",
    model=shared_model,
    description=(
        "An orchestrator agent that coordinates the FSI compliance audit flow. "
        "It calls the Regulatory Intel Agent to extract rules and the Policy Auditor Agent to perform gap analysis."
    ),
    instruction=f"{system_prompt}\n\n{system_instructions}",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=8192,
    ),
    tools=[
        query_regulatory_intel_agent,
        query_policy_auditor_agent,
    ],
)

# Enable resumability only in deployed environments (Cloud Run sets the PORT env var).
_is_deployed = bool(os.environ.get("K_SERVICE"))

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(enabled=_is_deployed),
)
