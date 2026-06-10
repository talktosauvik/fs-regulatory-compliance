"""Deploy script for Policy Auditor to Agent Engine and Gemini Enterprise."""

import argparse
import json
import os
import sys

from a2a.types import AgentSkill
from dotenv import load_dotenv
from google.auth import default
from google.auth.transport.requests import Request
import httpx
import requests
import vertexai
from vertexai.preview.reasoning_engines import A2aAgent
from vertexai.preview.reasoning_engines.templates.a2a import create_agent_card

def _get_bearer_token():
    """Gets a bearer token for authenticating with Google Cloud APIs."""
    try:
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        request = Request()
        credentials.refresh(request)
        return credentials.token
    except Exception as e:
        print(f"Error getting credentials: {e}")
        return None

def _register_agent_on_gemini_enterprise(
    project_id: str,
    app_id: str,
    agent_card: str,
    agent_name: str,
    display_name: str,
    description: str,
    agent_authorization: str | None = None,
    is_update: bool = False,
):
    """Registers or Updates the Agent Engine in Gemini Enterprise via the A2A API route."""
    base_endpoint = (
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{project_id}/"
        f"locations/global/collections/default_collection/engines/{app_id}/"
        "assistants/default_assistant/agents"
    )

    payload = {
        "name": agent_name,
        "displayName": display_name,
        "description": description,
        "a2aAgentDefinition": {"jsonAgentCard": agent_card},
    }

    if agent_authorization:
        print(f"🔒 Applying secure Agent Authorization: {agent_authorization.split('/')[-1]}")
        payload["authorization_config"] = {"agent_authorization": agent_authorization}

    bearer_token = _get_bearer_token()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project_id,
    }

    if is_update:
        print("\n[GE] Updating existing agent in Gemini Enterprise...")
        api_endpoint = f"{base_endpoint}/{agent_name}"
        params = {"updateMask": "displayName,description,a2aAgentDefinition,authorizationConfig"}
        response = requests.patch(api_endpoint, headers=headers, json=payload, params=params)
    else:
        print("\n[GE] Registering new agent to Gemini Enterprise...")
        api_endpoint = base_endpoint
        response = requests.post(api_endpoint, headers=headers, json=payload)

    if response.status_code == 200:
        print(f"✅ Successfully synced '{display_name}' with Gemini Enterprise!")
    elif response.status_code == 409 and not is_update:
        print(f"⚠️ Agent '{display_name}' already exists. Use --engine_id to update it next time.")
    else:
        print(f"❌ GE Sync failed ({response.status_code}): {response.text}")
        response.raise_for_status()

def main():
    parser = argparse.ArgumentParser(description="Deploy Policy Auditor to Agent Engine")
    parser.add_argument("--display_name", default="Policy Auditor", help="GE Display Name")
    parser.add_argument("--engine_id", default=None, help="If provided, updates existing Agent Engine ID.")
    parser.add_argument("--ge_agent_id", default="Policy_Auditor_Agent", help="Exact Agent ID in Gemini Enterprise.")
    args = parser.parse_args()

    load_dotenv()

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    region = "us-central1"
    storage = os.environ.get("LOGS_BUCKET_NAME")
    ge_app_id = os.environ.get("GEMINI_ENTERPRISE_APP_ID")
    agent_auth = os.environ.get("AGENT_AUTHORIZATION")
    api_endpoint = f"{region}-aiplatform.googleapis.com"

    if not project_id or not ge_app_id or not storage:
        print("Error: GOOGLE_CLOUD_PROJECT, LOGS_BUCKET_NAME, and GEMINI_ENTERPRISE_APP_ID env vars required.")
        sys.exit(1)

    print("=" * 80)
    if args.engine_id:
        print(f"🚀 UPDATING Policy Auditor on Agent Engine (ID: {args.engine_id})...")
    else:
        print("🚀 CREATING NEW Policy Auditor on Agent Engine...")
    
    # 1. Initialize Vertex AI natively
    vertexai.init(project=project_id, location=region, staging_bucket=f"gs://{storage}")
    
    # 2. Initialize the Vertex AI Client (CRITICAL FIX: This exposes .agent_engines)
    from google.genai import types
    client = vertexai.Client(
        project=project_id,
        location=region,
        http_options=types.HttpOptions(api_version="v1beta1"),
    )
    
    # 3. Import agent & the zero-argument executor builder
    from app.agent import root_agent, PolicyAuditorExecutorBuilder
    
    # 4. Manually build the Agent Skill
    skill_auditor = AgentSkill(
        id="Policy_Auditor_Agent",
        name="Policy Auditor",
        description=root_agent.description,
        tags=["compliance", "audit", "finra", "sec"],
        examples=[
            "Compare FINRA Rule 3310 against our internal AML policy.",
            "Perform a delta analysis on SEC Form PF IA-6546 vs IA-6865.",
        ],
    )

    # 5. Create the Agent Card
    agent_card = create_agent_card(
        agent_name="Policy_Auditor_Agent",
        description=root_agent.description,
        skills=[skill_auditor],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )

    # 6. Use the A2aAgent wrapper WITH the custom zero-arg executor
    a2a_agent = A2aAgent(
        agent_card=agent_card,
        agent_executor_builder=PolicyAuditorExecutorBuilder,
    )
    a2a_agent.set_up()

    # 7. Prepare Env Vars
    env_vars = {}
    keys_to_pass =[
        "ADK_MODEL", "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI",
        "LOGS_BUCKET_NAME", "FSI_EXTERNAL_REGS_BUCKET", "FSI_INTERNAL_POLICIES_BUCKET",
        "FSI_DEV_HANDOFF_BUCKET", "GOOGLE_DRIVE_FOLDER_ID", "AGENT_VERSION", "REGULATORY_INTEL_AGENT_ID"
    ]
    for key in keys_to_pass:
        if os.environ.get(key):
            env_vars[key] = os.environ.get(key)

    requirements =[line.strip() for line in open("requirements.txt") if line.strip() and not line.startswith("#")]
    
    config = {
        "display_name": args.display_name,
        "description": root_agent.description,
        "agent_framework": "google-adk",
        "staging_bucket": f"gs://{storage}",
        "requirements": requirements,
        "http_options": {"api_version": "v1beta1"},
        "extra_packages":["./app"],
        "env_vars": env_vars,
    }

    # 8. Create or Update using EXACT PROVEN SYNTAX
    if args.engine_id:
        resource_name = f"projects/{project_id}/locations/{region}/reasoningEngines/{args.engine_id}"
        remote_agent = client.agent_engines.update(
            name=resource_name,
            agent=a2a_agent,
            config=config,
        )
        print(f"✅ Agent Engine updated: {resource_name}")
    else:
        remote_agent = client.agent_engines.create(
            agent=a2a_agent,
            config=config,
        )
        resource_name = remote_agent.api_resource.name
        print(f"✅ Agent Engine created: {resource_name}")

    # 9. Fetch A2A Card from the LIVE endpoint
    a2a_endpoint = f"https://{api_endpoint}/v1beta1/{resource_name}/a2a/v1/card"
    bearer_token = _get_bearer_token()
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    
    response = httpx.get(a2a_endpoint, headers=headers)
    if response.status_code != 200:
        print(f"\n❌ FATAL: Vertex AI rejected the Agent Card request (HTTP {response.status_code})")
        print(f"Raw Error Response from Google: {response.text}\n")
        response.raise_for_status()
        
    agent_card_json = response.json()
    
    # 10. Inject A2UI v0.8 capabilities into the Agent Card JSON
    agent_card_json["capabilities"] = {
        "streaming": False,
        "extensions":[{
            "uri": "https://a2ui.org/a2a-extension/a2ui/v0.8",
            "description": "Ability to render A2UI Gap Analysis Cards",
            "required": False,
            "params": {"supportedCatalogIds":["https://a2ui.org/specification/v0_8/standard_catalog_definition.json"]}
        }],
    }
    
    # 11. Sync with Gemini Enterprise
    _register_agent_on_gemini_enterprise(
        project_id=project_id,
        app_id=ge_app_id,
        agent_card=json.dumps(agent_card_json),
        agent_name=args.ge_agent_id,
        display_name=args.display_name,
        description=root_agent.description,
        agent_authorization=agent_auth,
        is_update=bool(args.engine_id),
    )
    print("=" * 80)

if __name__ == "__main__":
    main()