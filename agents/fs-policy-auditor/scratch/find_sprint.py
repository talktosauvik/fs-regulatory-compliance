import os
import json
import urllib.request
import base64
from pathlib import Path

def find_sprint():
    env_path = Path("/usr/local/google/home/iamsouvik/fs-regulatory-compliance/agents/fs-jira-agent/.env")
    if not env_path.exists():
        print("Jira .env file not found.")
        return

    # Read env variables manually
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

    # Step 1: Get all boards
    boards_url = f"{jira_url}/rest/agile/1.0/board"
    req = urllib.request.Request(boards_url)
    req.add_header("Authorization", f"Basic {auth_b64}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            boards = result.get("values", [])
            print("\n--- Boards Found ---")
            for board in boards:
                board_id = board.get("id")
                board_name = board.get("name")
                print(f"Board Name: {board_name} | Board ID: {board_id}")
                
                # For each board, get all sprints
                sprints_url = f"{jira_url}/rest/agile/1.0/board/{board_id}/sprint"
                sreq = urllib.request.Request(sprints_url)
                sreq.add_header("Authorization", f"Basic {auth_b64}")
                sreq.add_header("Accept", "application/json")
                try:
                    with urllib.request.urlopen(sreq) as sresponse:
                        sresult = json.loads(scall := sresponse.read().decode())
                        sprints = sresult.get("values", [])
                        for sprint in sprints:
                            print(f"  -> Sprint Name: {sprint.get('name')} | Sprint ID: {sprint.get('id')} | State: {sprint.get('state')}")
                except Exception as se:
                    # Some boards might not support sprints
                    pass
    except Exception as e:
        print(f"Failed to fetch boards/sprints: {e}")

if __name__ == "__main__":
    find_sprint()
