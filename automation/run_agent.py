"""Run one website pipeline role (intake / builder / reviewer / merger) as an Agent Canvas conversation.

Standard library only, so an automation run starts in seconds instead of installing the SDK.

Starts a conversation on the agent server from an Agent Profile (e.g. Claude Code over ACP),
passes GITHUB_TOKEN as a LookupSecret, sends the role prompt, and waits for it to finish.

Env vars:
  AGENT_SERVER_URL              agent server base URL (default http://127.0.0.1:18000)
  OH_SESSION_API_KEYS_0         agent server session key (fallback: SESSION_API_KEY)
  PIPELINE_AGENT_PROFILE_ID     Agent Profile to launch (default: the active profile)
  PIPELINE_WORK_ROOT            parent dir for per-run workspaces
  AUTOMATION_CALLBACK_URL / AUTOMATION_CALLBACK_API_KEY / AUTOMATION_RUN_ID
                                set by the automation service; used to report completion

Usage: python run_agent.py intake|builder|reviewer|merger
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "rammanohar99/company-website"
HERE = Path(__file__).resolve().parent
TERMINAL_STATES = {"finished", "error", "stuck"}
MAX_WAIT_SECONDS = 25 * 60

SERVER = os.environ.get("AGENT_SERVER_URL", "http://127.0.0.1:18000").rstrip("/")
SESSION_KEY = os.environ.get("OH_SESSION_API_KEYS_0") or os.environ.get("SESSION_API_KEY", "")
WORK_ROOT = (
    os.environ.get("PIPELINE_WORK_ROOT")
    or os.environ.get("WORKSPACE_BASE")
    or "/home/openhands/.openhands/workspaces/agent-pipeline"
)

SETUP = f"""## Environment setup (do this first)
- Your current directory is an empty workspace. Clone the repo into it and work inside it:
  `git clone https://x-access-token:${{GITHUB_TOKEN}}@github.com/{REPO}.git repo && cd repo`
  `git config user.name "{{role}}-agent" && git config user.email "{{role}}-agent@users.noreply.github.com"`
- GitHub credentials are in the `GITHUB_TOKEN` environment variable. Never print it.
- Talk to GitHub with `curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/repos/{REPO}/...`
  (use `gh` only if it is installed).
- There is no build step. Validate HTML with `npx --yes html-validate@8.18.2 "site/**/*.html"`.
- To look at an image (e.g. a preview screenshot), download it with curl and open the PNG file.
- When you are done, end with a one-paragraph summary of what you did (or "No work").

"""


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{SERVER}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Session-API-Key": SESSION_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def build_prompt(role: str) -> str:
    role_prompt = (HERE.parent / ".agents" / "prompts" / f"{role}.md").read_text(encoding="utf-8")
    return SETUP.replace("{role}", role) + role_prompt.replace("{{REPO}}", REPO)


def saved_secrets() -> dict:
    """Every saved secret as a LookupSecret, like the Canvas UI sends.

    Includes the agent's own credentials (e.g. CLAUDE_CODE_OAUTH_TOKEN), not just GITHUB_TOKEN.
    """
    headers = {"X-Session-API-Key": SESSION_KEY} if SESSION_KEY else {}
    secrets = {}
    for item in api("GET", "/api/settings/secrets").get("secrets", []):
        secret = {"kind": "LookupSecret", "url": f"/api/settings/secrets/{item['name']}"}
        if headers:
            secret["headers"] = headers
        secrets[item["name"]] = secret
    if "GITHUB_TOKEN" not in secrets:
        raise RuntimeError("GITHUB_TOKEN is not saved in Settings -> Secrets")
    return secrets


def start_conversation(role: str) -> str:
    profile_id = os.environ.get("PIPELINE_AGENT_PROFILE_ID") or api(
        "GET", "/api/agent-profiles"
    )["active_agent_profile_id"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    conversation = api(
        "POST",
        "/api/conversations",
        {
            "agent_profile_id": profile_id,
            "workspace": {"working_dir": f"{WORK_ROOT}/{role}-{stamp}"},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": build_prompt(role)}],
                "run": True,
            },
            "secrets": saved_secrets(),
            "autotitle": False,
            "tags": {"pipeline": "company-website", "role": role},
        },
    )
    return conversation["id"]


def wait_for(conversation_id: str) -> str:
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    status = "unknown"
    while time.monotonic() < deadline:
        status = api("GET", f"/api/conversations/{conversation_id}").get("execution_status")
        if status in TERMINAL_STATES:
            return status
        time.sleep(10)
    return f"timeout (last status: {status})"


def report(success: bool, detail: str, conversation_id: str | None = None) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL")
    if not url:
        return
    body = {
        "run_id": os.environ.get("AUTOMATION_RUN_ID"),
        "conversation_id": conversation_id,
        "status": "COMPLETED" if success else "FAILED",
        "error": None if success else detail,
    }
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except urllib.error.URLError as e:
        print(f"callback failed (non-fatal): {e}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # agent replies contain non-ASCII
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role not in {"intake", "builder", "reviewer", "merger"}:
        print("usage: run_agent.py intake|builder|reviewer|merger")
        return 2
    try:
        conversation_id = start_conversation(role)
        print(f"[{role}] conversation {conversation_id} started", flush=True)
        status = wait_for(conversation_id)
        final = api("GET", f"/api/conversations/{conversation_id}/agent_final_response")
        print(f"[{role}] status: {status}\n[{role}] final response:\n{final.get('response', '')}")
        ok = status == "finished"
        report(ok, f"conversation {conversation_id} ended with status {status}", conversation_id)
        return 0 if ok else 1
    except Exception as e:  # report every failure so the run never hangs until its watchdog
        report(False, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
