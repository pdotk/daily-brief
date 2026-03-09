import os
import json
import re
import requests
from datetime import datetime, timezone, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================
# Configuration (pulled from GitHub Secrets)
# ============================================
LINEAR_API_KEY = os.environ["LINEAR_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
GOOGLE_CALENDAR_CREDENTIALS = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# ─── Talent channel IDs to summarize ───
TALENT_CHANNELS = {
    "C072E3BBSVC": "#talent-recruiting-and-operations",
    "C0A4MSN40RF": "#referrals-talent",
    "CUYUC6CGJ": "#talent",
}

# ─── NEW: User name cache (populated lazily) ───
_user_cache = {}


# ============================================
# 1. Fetch Linear PMK Issues
# ============================================
def fetch_linear_issues():
    """Fetch In Progress and Todo issues from PMK team."""
    query = """
    query {
        teams(filter: { key: { eq: "PMK" } }) {
            nodes {
                id
                name
                issues(
                    filter: {
                        state: {
                            type: { in: ["started", "unstarted"] }
                        }
                    }
                    orderBy: updatedAt
                    first: 50
                ) {
                    nodes {
                        identifier
                        title
                        priority
                        state {
                            name
                            type
                        }
                        url
                        updatedAt
                    }
                }
            }
        }
    }
    """

    response = requests.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json",
        },
        json={"query": query},
    )

    print(f"   Linear API status code: {response.status_code}")

    data = response.json()

    if "errors" in data:
        print(f"   ❌ Linear API errors: {data['errors']}")
        return [], []

    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    print(f"   Teams found: {len(teams)}")

    for team in teams:
        issues = team.get("issues", {}).get("nodes", [])
        print(f"   Team '{team.get('name')}': {len(issues)} issues")

    in_progress = []
    todo = []

    for team in teams:
        for issue in team.get("issues", {}).get("nodes", []):
            item = {
                "id": issue["identifier"],
                "title": issue["title"],
                "priority": issue.get("priority", 0),
                "url": issue["url"],
                "state": issue["state"]["name"],
                "state_type": issue["state"]["type"],
            }
            if issue["state"]["type"] == "started":
                in_progress.append(item)
            elif issue["state"]["type"] == "unstarted":
                todo.append(item)

    in_progress.sort(key=lambda x: x["priority"] if x["priority"] > 0 else 99)
    todo.sort(key=lambda x: x["priority"] if x["priority"] > 0 else 99)

    return in_progress, todo


# ============================================
# 2. Fetch Google Calendar Events
# ============================================
def fetch_calendar_events():
    """Fetch today's calendar events from Google Calendar."""
    if not GOOGLE_CALENDAR_CREDENTIALS:
        return None

    try:
        creds_json = json.loads(GOOGLE_CALENDAR_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(
            creds_json,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )

        service = build("calendar", "v3", credentials=credentials)

        now = datetime.now(timezone(timedelta(hours=-5)))
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        result = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=start_of_day,
                timeMax=end_of_day,
                singleEvents=True,
                orderBy="startTime",
                maxResults=15,
            )
            .execute()
        )

        events = []
        for event in result.get("items", []):
            start = event.get("start", {})
            time_str = start.get("dateTime", start.get("date", ""))
            if "T" in time_str:
                time_display = datetime.fromisoformat(time_str).strftime("%-I:%M%p")
            else:
                time_display = "All day"

            events.append(
                {
                    "time": time_display,
                    "title": event.get("summary", "No title"),
                }
            )

        print(f"   Found {len(events)} events today")
        return events

    except Exception as e:
        print(f"   ⚠️ Calendar error: {e}")
        return None


# ============================================
# 3. Fetch Slack Highlights
# ============================================
def fetch_slack_highlights():
    """Fetch recent Slack mentions and DMs."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.get(
        "https://slack.com/api/conversations.list",
        headers=headers,
        params={"types": "im,mpim", "limit": 20},
    )

    unread_dms = 0
    if response.status_code == 200:
        data = response.json()
        for channel in data.get("channels", []):
            if channel.get("is_im") and channel.get("unread_count_display", 0) > 0:
                unread_dms += 1

    return {"unread_dms": unread_dms}


# ============================================
# 3b. Slack User Resolution  ← NEW
# ============================================
def resolve_slack_user(user_id):
    """Look up a Slack user ID and return their display name. Results are cached."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            "https://slack.com/api/users.info",
            headers=headers,
            params={"user": user_id},
        )
        data = response.json()

        if data.get("ok"):
            profile = data["user"].get("profile", {})
            # Prefer display_name, fall back to real_name, then user_id
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or data["user"].get("real_name")
                or user_id
            )
            _user_cache[user_id] = name
            return name
        else:
            _user_cache[user_id] = user_id  # Cache the miss too
            return user_id

    except Exception:
        _user_cache[user_id] = user_id
        return user_id


def humanize_slack_text(text):
    """Replace <@U12345> user mentions with display names, and clean up Slack markup."""
    # Replace user mentions: <@U12345> → "Display Name"
    def replace_user_mention(match):
        user_id = match.group(1)
        name = resolve_slack_user(user_id)
        return f"*{name}*"

    text = re.sub(r"<@(U[A-Z0-9]+)>", replace_user_mention, text)

    # Replace channel mentions: <#C12345|channel-name> → #channel-name
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)

    # Replace URLs: <https://example.com|label> → label, <https://example.com> → URL
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    return text


# ============================================
# 3c. Fetch Talent Channel Summaries
# ============================================
def fetch_talent_channel_summaries():
    """Fetch messages from the past 24 hours in each talent channel."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    oldest = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    summaries = {}

    for channel_id, channel_name in TALENT_CHANNELS.items():
        try:
            response = requests.get(
                "https://slack.com/api/conversations.history",
                headers=headers,
                params={
                    "channel": channel_id,
                    "oldest": str(oldest),
                    "limit": 50,
                },
            )

            data = response.json()

            if not data.get("ok"):
                print(f"   ⚠️ Error fetching {channel_name}: {data.get('error')}")
                summaries[channel_name] = {
                    "count": 0,
                    "highlights": [],
                    "error": data.get("error"),
                }
                continue

            messages = data.get("messages", [])

            # Filter out bot join/leave messages, keep substantive ones
            real_messages = [
                m
                for m in messages
                if m.get("subtype")
                not in ("channel_join", "channel_leave", "bot_add", "bot_remove")
            ]

            # Pull first ~120 chars of each message as a preview
            highlights = []
            for msg in real_messages[:5]:  # Top 5 most recent
                text = msg.get("text", "")

                # ─── NEW: Resolve user IDs to names ───
                text = humanize_slack_text(text)
                # ──────────────────────────────────────

                preview = (text[:120] + "…") if len(text) > 120 else text
                highlights.append(preview)

            summaries[channel_name] = {
                "count": len(real_messages),
                "highlights": highlights,
                "error": None,
            }

            print(f"   {channel_name}: {len(real_messages)} messages")

        except Exception as e:
            print(f"   ⚠️ Exception fetching {channel_name}: {e}")
            summaries[channel_name] = {"count": 0, "highlights": [], "error": str(e)}

    return summaries


# ============================================
# 4. Format & Send Slack Message
# ============================================
def priority_emoji(priority):
    return {1: "🔴", 2: "🟠", 3: "🟡", 4: "⚪"}.get(priority, "⚪")


def build_message(in_progress, todo, calendar_events, slack_highlights, channel_summaries):
    today = datetime.now().strftime("%A, %B %-d")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"☀️ Daily Brief — {today}"},
        },
        {"type": "divider"},
    ]

    # --- In Progress ---
    if in_progress:
        text = "*🔥 In Progress (PMK)*\n"
        for item in in_progress[:7]:
            emoji = priority_emoji(item["priority"])
            text += f"  {emoji} <{item['url']}|{item['id']}>: {item['title']}\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    # --- Todo ---
    if todo:
        text = "*📋 Up Next (PMK Todo)*\n"
        for item in todo[:7]:
            emoji = priority_emoji(item["priority"])
            text += f"  {emoji} <{item['url']}|{item['id']}>: {item['title']}\n"
        if len(todo) > 7:
            text += f"  _...and {len(todo) - 7} more_\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    blocks.append({"type": "divider"})

    # --- Calendar ---
    if calendar_events is not None:
        if calendar_events:
            text = "*📅 Today's Calendar*\n"
            for event in calendar_events:
                text += f"  • *{event['time']}* — {event['title']}\n"
        else:
            text = "*📅 Today's Calendar*\n  No meetings today! 🎉\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*📅 Calendar*\n  _Not configured — add Google Calendar API key to enable_",
                },
            }
        )

    # --- Slack ---
    if slack_highlights:
        text = "*💬 Slack*\n"
        text += f"  • {slack_highlights['unread_dms']} unread DM conversations\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    blocks.append({"type": "divider"})

    # ─── Talent Channel Summaries ────────────────────────
    if channel_summaries:
        text = "*📢 Talent Channel Activity (last 24h)*\n"
        for channel_name, info in channel_summaries.items():
            count = info["count"]
            if info.get("error"):
                text += f"\n  *{channel_name}*: ⚠️ _{info['error']}_\n"
            elif count == 0:
                text += f"\n  *{channel_name}*: _No new messages_ 🤫\n"
            else:
                text += f"\n  *{channel_name}*: {count} message{'s' if count != 1 else ''}\n"
                for highlight in info["highlights"]:
                    text += f"    • {highlight}\n"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    # ─────────────────────────────────────────────────────

    # --- Footer ---
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🤖 _Your daily brief, powered by GitHub Actions_",
                }
            ],
        }
    )

    return blocks


def send_slack_dm(blocks):
    """Send a DM to yourself via Slack."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        "https://slack.com/api/conversations.open",
        headers=headers,
        json={"users": SLACK_USER_ID},
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        print(f"❌ Slack error opening DM: {data.get('error', 'unknown error')}")
        print(f"   Full response: {data}")
        return

    channel_id = data["channel"]["id"]

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json={"channel": channel_id, "blocks": blocks, "text": "☀️ Your Daily Brief"},
    )
    response.raise_for_status()
    data = response.json()

    if data.get("ok"):
        print("✅ Daily brief sent successfully!")
    else:
        print(f"❌ Slack error sending message: {data.get('error', 'unknown error')}")
        print(f"   Full response: {data}")


# ============================================
# Main
# ============================================
if __name__ == "__main__":
    print("📋 Fetching Linear PMK issues...")
    in_progress, todo = fetch_linear_issues()
    print(f"   Found {len(in_progress)} in progress, {len(todo)} todo")

    print("📅 Fetching calendar events...")
    calendar_events = fetch_calendar_events()

    print("💬 Fetching Slack highlights...")
    slack_highlights = fetch_slack_highlights()

    print("📢 Fetching talent channel summaries...")
    channel_summaries = fetch_talent_channel_summaries()

    print("📨 Building and sending daily brief...")
    blocks = build_message(
        in_progress, todo, calendar_events, slack_highlights, channel_summaries
    )
    send_slack_dm(blocks)
