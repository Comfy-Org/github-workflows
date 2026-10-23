#!/bin/bash

# Slack DM notification script for GitHub Actions
# Usage: ./slack-notify.sh <status> <title> <message>
#
# Delivery: set SLACK_BOT_TOKEN + DM_GITHUB_USER. Resolves
# {DM_GITHUB_USER}@comfy.org to a Slack user ID and sends a DM. No-ops
# silently (exit 0) when either is unset or the user can't be resolved;
# only a bad token/scopes (a lookup error that isn't users_not_found)
# exits non-zero.

set -e

STATUS="${1:-failure}"
TITLE="${2:-GitHub Action Notification}"
MESSAGE="${3:-No details provided}"

# Determine color based on status
if [ "$STATUS" = "success" ]; then
    COLOR="good"
elif [ "$STATUS" = "warning" ]; then
    COLOR="warning"
else
    COLOR="danger"
fi

# ── DM mode ────────────────────────────────────────────────────────────────────
if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${DM_GITHUB_USER:-}" ]; then
    # GitHub handle → comfy.org email-prefix overrides for anyone whose GitHub
    # handle doesn't match their email prefix. Supplied as a JSON object via
    # $DM_EMAIL_MAP (e.g. {"somehandle":"firstname"}) so the team roster stays
    # out of this public repo — set it as an org/repo Actions variable and pass
    # it into this script. Keys MUST be lowercase; GitHub handles are
    # case-insensitive and we normalize to lowercase before lookup.
    NORMALIZED_USER="${DM_GITHUB_USER,,}"
    EMAIL_PREFIX="$NORMALIZED_USER"
    if [ -n "${DM_EMAIL_MAP:-}" ]; then
        MAPPED=$(echo "$DM_EMAIL_MAP" | jq -r --arg u "$NORMALIZED_USER" '.[$u] // empty' 2>/dev/null || echo "")
        if [ -n "$MAPPED" ]; then
            EMAIL_PREFIX="$MAPPED"
        fi
    fi
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

    PAYLOAD=$(jq -n \
        --arg channel  "$SLACK_USER_ID" \
        --arg color    "$COLOR" \
        --arg title    "$TITLE" \
        --arg text     "$MESSAGE" \
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

# ── No DM target ───────────────────────────────────────────────────────────────
# SLACK_BOT_TOKEN and/or DM_GITHUB_USER is unset. That is a designed-for case,
# not an error: a bot-authored PR whose triggerer resolves to empty, or a caller
# that never configured the token. These notifications are best-effort, so
# no-op green (matching this directory's README) rather than failing the step.
echo "Slack DM not configured (needs SLACK_BOT_TOKEN + DM_GITHUB_USER) — skipping"
exit 0
