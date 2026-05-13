# Retrace GitHub App setup

This page walks you through running Retrace's PR review as a GitHub App
webhook. The CLI version (`retrace review`) covers ad-hoc local use; the
App lets a PR get analysed automatically when someone comments
`@retrace review`.

> **Prerequisite**: an instance of `retrace api serve` reachable from
> `github.com`. Self-host users typically front this with a TLS
> terminator (nginx, Caddy, Cloudflare Tunnel). For local hacking,
> `ngrok http 8788` works.

## 1. Create the GitHub App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New
   GitHub App**.
2. Fill in:
   - **GitHub App name** — anything (e.g. `Retrace`).
   - **Homepage URL** — your Retrace install URL, or
     `https://github.com/your-org/retrace`.
   - **Webhook URL** — `https://<your-retrace-host>/api/github/webhook`.
   - **Webhook secret** — pick a long random value (`openssl rand -hex 32`).
     **Save it** — you'll paste it into Retrace's `.env` below.
3. **Permissions** (Repository):
   - **Issues**: Read & write (for posting summary comments).
   - **Pull requests**: Read & write (for posting review comments).
   - **Contents**: Read-only (to fetch the diff).
   - **Metadata**: Read-only (always required).
4. **Subscribe to events**:
   - `Issue comment` — the trigger comes through here when someone
     writes `@retrace review` on a PR.
5. Click **Create GitHub App**.
6. On the next page, click **Generate a private key**, save the `.pem`
   file (you'll only need this if your install does anything beyond
   webhook ingest; for the comment-driven review flow above the
   webhook secret is enough).

## 2. Install the App on a repository

1. From the App's settings page, click **Install App** in the sidebar.
2. Pick the org or user account, choose **Only select repositories**,
   and tick the repos you want Retrace reviewing.
3. Confirm.

## 3. Wire the secret into Retrace

Add the webhook secret you generated in step 1 to your Retrace
deployment's `.env`:

```ini
RETRACE_GITHUB_WEBHOOK_SECRET=<the value from step 1>
```

Restart `retrace api serve` so it picks the secret up.

## 4. Trigger a review

On any PR in an installed repo, leave a comment:

```text
@retrace review
```

Within a few seconds, Retrace will:

1. Verify the webhook signature against `RETRACE_GITHUB_WEBHOOK_SECRET`.
2. Fetch the PR's diff.
3. Run `analyze_pr_diff` — detect changed files, infer affected flows,
   link prior failures, recommend missing tests.
4. Post the analysis as a PR comment (and optionally as inline review
   comments).
5. File `qa_incidents` for each finding, so `retrace qa list` shows
   them too.

## 5. Restrictions

By default the webhook only acts on comments from a trusted association
(`OWNER`, `MEMBER`, `COLLABORATOR`) — see
`TRUSTED_COMMENT_AUTHOR_ASSOCIATIONS` in `src/retrace/github_app.py`.
Adjust the set if you want bots or first-time contributors to be able
to trigger reviews.

## Local testing without GitHub

If you don't want to set up the App yet, you can simulate the webhook:

```bash
# Build a synthetic issue_comment payload
gh api repos/<org>/<repo>/issues/<pr>/comments \
  --jq '.[-1]' > /tmp/payload.json

# Sign and POST it
BODY=$(jq -c . < /tmp/payload.json)
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$RETRACE_GITHUB_WEBHOOK_SECRET" -hex | awk '{print $NF}')"
curl -X POST http://127.0.0.1:8788/api/github/webhook \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" \
  --data "$BODY"
```

Or just use the CLI:

```bash
retrace review --pr https://github.com/org/repo/pull/42 --file-incidents --post-comment
```
