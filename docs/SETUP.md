# Setting up slack-to-website-agents

This sets up the full flow (Slack message → ticket → PR → screenshots → review → merge → GitHub Pages →
"it's live" reply) on your own copy.

**You need:** Docker, the GitHub CLI (`gh`, logged in), a Slack workspace where you can install apps, and a
Claude Pro/Max subscription or an Anthropic API key.

> Windows PowerShell 5.1 strips double quotes from arguments to programs like `gh`. Run the commands below in
> Git Bash, or put long text in files and use `--body-file`.

## 1. The repository and GitHub Pages

```bash
gh repo fork rammanohar99/slack-to-website-agents --clone   # or push a copy to your account
cd slack-to-website-agents
R=your-user/slack-to-website-agents

# Publish with GitHub Actions (the Deploy workflow); do this before the first push to main.
gh api -X POST repos/$R/pages -f build_type=workflow
```

Set `REPO = "your-user/slack-to-website-agents"` in [`automation/run_agent.py`](../automation/run_agent.py).

Create the labels:

```bash
for l in "agent:todo 0e8a16" "agent:in-progress fbca04" "agent:review 1d76db" "agent:changes d93f0b" \
         "agent:approved 5319e7" "agent:human b60205" "needs-triage c5def5" "triage:approved bfdadc"; do
  set -- $l; gh label create "$1" --repo $R --color "$2" --force
done
```

Push to `main` once. The Deploy workflow publishes `site/` to `https://<you>.github.io/slack-to-website-agents/`.

## 2. Agent Canvas with Claude Code

```bash
mkdir -p ~/projects ~/.openhands
docker run -d --name agent-canvas -p 127.0.0.1:8000:8000 \
  -e AGENT_CANVAS_ALLOW_LAN_SESSION_KEY=true \
  -v "$HOME/.openhands:/home/openhands/.openhands" -v "$HOME/projects:/projects" \
  ghcr.io/openhands/agent-canvas:1.32.0
```

Open <http://localhost:8000/canvas>, choose **Claude Code** as the agent, and add credentials:
- **Subscription:** run `claude setup-token` and save the token as `CLAUDE_CODE_OAUTH_TOKEN` (leave the base URL
  blank), **or**
- **API:** save `ANTHROPIC_API_KEY`.

Say "hi" in a chat to confirm it replies.

## 3. GitHub token for the agents

Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to **only this
repository**:

| Permission | Access |
|---|---|
| Contents, Pull requests, Issues | Read and write |
| Actions, Commit statuses | Read-only |

Save it in Canvas under **Settings → Secrets** as `GITHUB_TOKEN`.

> **Check it can actually write.** A repo's `permissions.push` in the API reflects *your account's* role, not
> the token's scope. Test with `git push --dry-run` using the token instead.

## 4. Slack

1. <https://api.slack.com/apps> → **Create New App → From a manifest** → paste
   [`slack-app-manifest.yml`](slack-app-manifest.yml) → **Install to Workspace**.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) from **OAuth & Permissions**.
3. Create `#website-requests`, then run `/invite @website-bot` in it. Copy the channel ID (channel name →
   **About**, starts with `C`).
4. Save in Canvas → **Settings → Secrets**: `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID`.
5. Save the token for the Deploy workflow too: `gh secret set SLACK_BOT_TOKEN --repo $R`.

## 5. The four automations

Each agent is a **custom automation** that runs the runner from an uploaded bundle. Use the Canvas automation
API with your session key:

```bash
export KEY=$(docker exec agent-canvas cat /home/openhands/.openhands/agent-canvas/api-key.txt)
API=http://127.0.0.1:8000/api/automation/v1

# Bundle the runner + prompts
git archive --format=tar.gz -o runner.tar.gz HEAD automation/run_agent.py .agents/prompts
curl -s -H "X-Session-API-Key: $KEY" -H "Content-Type: application/gzip" \
  -X POST "$API/uploads?name=website-runner" --data-binary @runner.tar.gz      # -> tarball_path

# Your Claude Code agent profile
curl -s -H "X-Session-API-Key: $KEY" http://127.0.0.1:8000/api/agent-profiles  # -> active_agent_profile_id
```

Create one automation per row (all start inactive):

| Name | `entrypoint` | `schedule` |
|---|---|---|
| Website - Intake | `python3 automation/run_agent.py intake` | `3,18,33,48 * * * *` |
| Website - Builder | `python3 automation/run_agent.py builder` | `15,45 * * * *` |
| Website - Reviewer | `python3 automation/run_agent.py reviewer` | `7,22,37,52 * * * *` |
| Website - Merger | `python3 automation/run_agent.py merger` | `12,27,42,57 * * * *` |

```bash
curl -s -H "X-Session-API-Key: $KEY" -H "Content-Type: application/json" -X POST "$API" -d '{
  "name": "Website - Intake",
  "tarball_path": "oh-internal://uploads/<id>",
  "entrypoint": "python3 automation/run_agent.py intake",
  "agent_profile_id": "<profile-id>",
  "trigger": {"type": "cron", "schedule": "3,18,33,48 * * * *", "timezone": "Asia/Kolkata"},
  "timeout": 1800,
  "state": "INACTIVE"
}'
```

> **Why custom automations?** Canvas's simpler *prompt automations* always launch the built-in OpenHands agent
> with an LLM API key and ignore the Claude Code profile. The runner starts Claude Code directly and passes every
> saved secret (including `CLAUDE_CODE_OAUTH_TOKEN`) to the conversation.

## 6. Test once, then switch on

1. Post a request in `#website-requests` from your own account (the Intake agent ignores bots).
2. Trigger each automation by hand, in order, checking the result after each:
   ```bash
   curl -s -H "X-Session-API-Key: $KEY" -H "Content-Type: application/json" -X POST "$API/<automation-id>/dispatch" -d '{}'
   ```
   Intake (🎫 reaction + ticket) → Builder (PR) → wait for the Preview workflow → Reviewer (`agent:approved`)
   → Merger (merged) → Deploy (✅ reply in the Slack thread).
3. Turn the four automations on under **Automations** in Canvas.

After changing a prompt or the runner, upload a new bundle and update each automation's `tarball_path`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ACP error: Authentication required` | Claude Code started without credentials | Save `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in Canvas secrets |
| Builder says push/comment was denied (403, "not accessible by personal access token") | Token not scoped to this repo | Add the repo to the fine-grained token's repository access |
| Preview fails: `Executable doesn't exist … webkit` | iPhone device profiles need WebKit | Use a Chromium phone profile (the workflow uses "Pixel 7") |
| Reviewer replies "Waiting for preview" | The Preview workflow hasn't finished for the latest commit | Normal; it reviews on its next run |
| No Slack "live" reply | `SLACK_BOT_TOKEN` missing as a GitHub Actions secret, or the ticket lacks the `<!-- slack:… -->` line | Check the Deploy log for `Posted to Slack thread` / `Slack error` |
| Intake ignores a message | Posted by a bot, older than 3 days, or already has the bot's reaction | Post from a person's account |
| Runs stay `RUNNING` for hours | The host slept | Expected on a laptop; use an always-on host |
