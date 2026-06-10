import os
import json
import urllib.request
import base64
from pathlib import Path

def get_jira_fields():
    env_path = Path("/usr/local/google/home/iamsouvik/fs-regulatory-compliance/agents/fs-jira-agent/.env")
    if not env_path.exists():
        print("Jira .env file not found.")
        return

    # Read env variables manually to avoid dependency issues
    env = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")

    jira_url = env.get("JIRA_BASE_URL", "").rstrip("/")
    email = env.get("JIRA_EMAIL") or env.get("JIRA_USER_EMAIL")
    api_token = env.get("JIRA_API_TOKEN")

    if not all([jira_url, email, api_token]):
        print(f"Missing Jira config. URL={jira_url}, Email={email}, Token={'Present' if api_token else 'Missing'}")
        return

    print(f"Connecting to Jira: {jira_url} as {email}...")
    auth_str = f"{email}:{api_token}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    url = f"{jira_url}/rest/api/2/field"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth_b64}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            fields = json.loads(response.read().decode())
            
            # Search for fields matching sprint or epic
            print("\n--- Custom Fields Found ---")
            for field in fields:
                name = field.get("name", "").lower()
                custom = field.get("custom", False)
                field_id = field.get("id", "")
                if "sprint" in name or "epic" in name or "priority" in name or "due" in name:
                    print(f"Name: {field.get('name')} | ID: {field_id} | Custom: {custom} | Schema: {field.get('schema')}")
    except Exception as e:
        print(f"Failed to fetch Jira fields: {e}")

if __name__ == "__main__":
    get_jira_fields()
