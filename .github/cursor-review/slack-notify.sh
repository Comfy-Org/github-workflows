#!/bin/bash

# Generic Slack notification script for GitHub Actions
# Usage: ./slack-notify.sh <status> <title> <message> [details_file] [channel]
#
# Delivery modes (checked in order):
#   DM mode:      set SLACK_BOT_TOKEN + DM_GITHUB_USER
#                 Resolves {DM_GITHUB_USER}@comfy.org to a Slack user ID and sends a DM.
#                 Falls back silently (exit 0) if the user can't be resolved.
#   Webhook mode: set SLACK_WEBHOOK_URL
#                 Posts to the channel the webhook is configured for.

set -e

STATUS="${1:-failure}"
TITLE="${2:-GitHub Action Notification}"
MESSAGE="${3:-No details provided}"
DETAILS_FILE="${4:-}"
CHANNEL="${5:-#prod-alerts}"
WEBHOOK_URL="${SLACK_WEBHOOK_URL}"

# Determine color based on status
if [ "$STATUS" = "success" ]; then
    COLOR="good"
elif [ "$STATUS" = "warning" ]; then
    COLOR="warning"
else
    COLOR="danger"
fi

# Read additional details if file provided
ADDITIONAL_DETAILS=""
if [ -n "$DETAILS_FILE" ] && [ -f "$DETAILS_FILE" ]; then
    ADDITIONAL_DETAILS=$(head -c 3000 "$DETAILS_FILE")
fi

# ── DM mode ────────────────────────────────────────────────────────────────────
if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${DM_GITHUB_USER:-}" ]; then
    # GitHub username → comfy.org email prefix overrides for anyone whose
    # GitHub handle doesn't match their email prefix. Keys are lowercase for
    # case-insensitive lookup (GitHub usernames are case-insensitive).
    declare -A GITHUB_EMAIL_MAP
    GITHUB_EMAIL_MAP[millermedia]=mattmiller
    GITHUB_EMAIL_MAP[huntcsg]=hunter
    GITHUB_EMAIL_MAP[skishore23]=kishore
    GITHUB_EMAIL_MAP[robinjhuang]=robin
    GITHUB_EMAIL_MAP[luke-mino-altherr]=luke
    GITHUB_EMAIL_MAP[fengsi]=si
    GITHUB_EMAIL_MAP[deepanjanroy]=dproy
    GITHUB_EMAIL_MAP[deepme987]=deep
    GITHUB_EMAIL_MAP[synap5e]=simonpinfold
    GITHUB_EMAIL_MAP[purzbeats]=purz

    NORMALIZED_USER="${DM_GITHUB_USER,,}"
    EMAIL_PREFIX="${GITHUB_EMAIL_MAP[$NORMALIZED_USER]:-$NORMALIZED_USER}"
    echo "DM mode: resolving ${EMAIL_PREFIX}@comfy.org (GitHub: ${DM_GITHUB_USER})"

    EMAIL="${EMAIL_PREFIX}@comfy.org"
    # --data-urlencode safely encodes the email (guards against [ ] in bot logins
    # being treated as curl URL glob syntax). Drop -f so HTTP errors return JSON
    # instead of aborting under set -e; we inspect .ok ourselves.
    if ! LOOKUP=$(curl -sSL \
            -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
            --get --data-urlencode "email=${EMAIL}" \
            "https://slack.com/api/users.lookupByEmail"); then
        echo "Slack lookup transport failed for ${EMAIL} — skipping DM"
        exit 0
    fi

    SLACK_USER_ID=$(echo "$LOOKUP" | jq -r 'if .ok and (.user.id != null) then .user.id else "" end')

    if [ -z "$SLACK_USER_ID" ]; then
        SLACK_ERROR=$(echo "$LOOKUP" | jq -r '.error // "unknown"')
        if [ "$SLACK_ERROR" = "users_not_found" ]; then
            echo "Slack user not found for $EMAIL — skipping DM"
            exit 0
        else
            echo "⚠️ Slack lookup failed for $EMAIL ($SLACK_ERROR) — check token/scopes"
            exit 1
        fi
    fi

    echo "Resolved ${DM_GITHUB_USER} → $SLACK_USER_ID"

    RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

    DM_TEXT="$MESSAGE"
    if [ -n "$ADDITIONAL_DETAILS" ]; then
        DM_TEXT="${MESSAGE}
\`\`\`${ADDITIONAL_DETAILS}\`\`\`"
    fi

    PAYLOAD=$(jq -n \
        --arg channel  "$SLACK_USER_ID" \
        --arg color    "$COLOR" \
        --arg title    "$TITLE" \
        --arg text     "$DM_TEXT" \
        --arg repo     "${GITHUB_REPOSITORY:-}" \
        --arg run_url  "$RUN_URL" \
        '{
            channel: $channel,
            attachments: [{
                color: $color,
                title: $title,
                text: $text,
                mrkdwn_in: ["text"],
                fields: [
                    { title: "Repository", value: $repo,    short: true },
                    { title: "Run",        value: ("<" + $run_url + "|View Details>"), short: true }
                ],
                footer: "GitHub Actions"
            }]
        }')

    if ! RESPONSE=$(curl -sSL -X POST \
            -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
            -H "Content-Type: application/json" \
            --data "$PAYLOAD" \
            https://slack.com/api/chat.postMessage); then
        echo "❌ chat.postMessage transport failed — skipping"
        exit 0
    fi

    if echo "$RESPONSE" | jq -e '.ok' > /dev/null; then
        echo "✅ DM sent to $SLACK_USER_ID"
        exit 0
    else
        echo "❌ Failed to send DM: $(echo "$RESPONSE" | jq -r '.error // "unknown"')"
        exit 0
    fi
fi

# ── Webhook mode ───────────────────────────────────────────────────────────────
if [ -z "$WEBHOOK_URL" ]; then
    echo "❌ No delivery method configured: set SLACK_BOT_TOKEN+DM_GITHUB_USER or SLACK_WEBHOOK_URL"
    exit 1
fi

# Optional top-level mention (webhook mode only).
MENTION_TEXT=""
if [ -n "${MENTION_USER_ID:-}" ]; then
    MENTION_TEXT=$(jq -nr --arg u "$MENTION_USER_ID" '"<@" + $u + ">"')
fi

RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# Build the full Slack payload via jq so every interpolated value (PR titles
# containing quotes, branches with special chars, ADDITIONAL_DETAILS captured
# from arbitrary command output) is properly JSON-escaped. The previous
# heredoc-with-${var} approach generated invalid JSON whenever a value
# contained `"` or `\`, which then tripped the `jq empty` validator below
# and silently dropped notifications.
SLACK_MESSAGE=$(jq -n \
    --arg channel       "$CHANNEL" \
    --arg mention_text  "$MENTION_TEXT" \
    --arg color         "$COLOR" \
    --arg title         "$TITLE" \
    --arg message       "$MESSAGE" \
    --arg repository    "${GITHUB_REPOSITORY:-}" \
    --arg workflow      "${GITHUB_WORKFLOW:-}" \
    --arg run_url       "$RUN_URL" \
    --arg event_name    "${GITHUB_EVENT_NAME:-}" \
    --arg ref_name      "${GITHUB_REF_NAME:-}" \
    --arg actor         "${GITHUB_ACTOR:-}" \
    --arg details       "$ADDITIONAL_DETAILS" \
    --argjson ts        "$(date +%s)" \
    '{
        channel: $channel,
        attachments: (
            [{
                color: $color,
                title: $title,
                text: $message,
                fields: [
                    { title: "Repository", value: $repository, short: true },
                    { title: "Workflow",   value: $workflow,   short: true },
                    { title: "Run",        value: ("<" + $run_url + "|View Details>"), short: true },
                    { title: "Trigger",    value: $event_name, short: true },
                    { title: "Branch",     value: $ref_name,   short: true },
                    { title: "Actor",      value: $actor,      short: true }
                ],
                footer: "GitHub Actions",
                ts: $ts
            }]
            + (if ($details | length) > 0 then [{
                color: $color,
                title: "Details",
                text: ("```" + $details + "```"),
                mrkdwn_in: ["text"]
            }] else [] end)
        )
    }
    + (if ($mention_text | length) > 0 then { text: $mention_text } else {} end)')

# Validate JSON before sending
echo "🔍 Validating JSON payload..."
if ! echo "$SLACK_MESSAGE" | jq empty 2>/dev/null; then
    echo "❌ Invalid JSON payload generated"
    echo "Payload:"
    echo "$SLACK_MESSAGE"
    exit 1
fi

# Send to Slack and capture response
echo "📤 Sending notification to Slack..."
RESPONSE=$(curl -X POST -H 'Content-type: application/json' \
    --data "$SLACK_MESSAGE" \
    "$WEBHOOK_URL" \
    --silent --show-error --write-out "\n%{http_code}")

# Extract HTTP status code and body
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

# Check if Slack accepted the message
if [ "$BODY" = "ok" ] && [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Slack notification sent successfully"
    exit 0
else
    echo "❌ Slack notification failed"
    echo "HTTP Code: $HTTP_CODE"
    echo "Response: $BODY"
    exit 1
fi
