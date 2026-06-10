import os
import json
import urllib.request
import urllib.parse
import base64
from pathlib import Path

def find_epic():
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
        print("Missing Jira config.")
        return

    auth_str = f"{email}:{api_token}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    # JQL query to search for epics or issues matching "GE App Demo"
    jql = 'summary ~ "GE App Demo"'
    encoded_jql = urllib.parse.quote(jql)
    url = f"{jira_url}/rest/api/3/search?jql={encoded_jql}"
    
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth_b64}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            issues = result.get("issues", [])
            print("\n--- Search Results for 'GE App Demo' ---")
            for issue in issues:
                fields = issue.get("fields", {})
                summary = fields.get("summary", "")
                key = issue.get("key", "")
                issuetype = fields.get("issuetype", {}).get("name", "")
                print(f"Key: {key} | Summary: {summary} | Type: {issuetype}")
    except Exception as e:
        print(f"Failed to search Jira: {e}")

if __name__ == "__main__":
    find_epic()
