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
LINEAR_TEAM_KEY = os.environ.get("LINEAR_TEAM_KEY", "")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
GOOGLE_CALENDAR_CREDENTIALS = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Channel config from environment (JSON string: {"CHANNEL_ID": "#channel-name", ...})
SLACK_CHANNELS = json.loads(os.environ.get("SLACK_CHANNELS", "{}"))

# React-based save emoji (without colons)
SAVE_EMOJI = os.environ.get("SAVE_EMOJI", "bookmark")

# User name cache (populated lazily)
_user_cache = {}


# ============================================
# 1. Fetch Linear Issues
# ============================================
def fetch_linear_issues():
    """Fetch In Progress and Todo issues from your team."""
    query = """
    query($teamKey: String!) {
        teams(filter: { key: { eq: $teamKey } }) {
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
        json={"query": query, "variables": {"teamKey": LINEAR_TEAM_KEY}},
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
    """Placeholder — Slack unread DM tracking not currently reliable via API."""
    return None
    """Fetch unread Slack DM conversations with sender names."""
    token = SLACK_USER_TOKEN or SLACK_BOT_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        # Paginate through all DM conversations
        all_channels = []
        cursor = None

        while True:
            params = {"types": "im", "limit": 200}
            if cursor:
                params["cursor"] = cursor

            response = requests.get(
                "https://slack.com/api/conversations.list",
                headers=headers,
                params=params,
            )

            data = response.json()
            if not data.get("ok"):
                print(f"   ⚠️ Error listing DMs: {data.get('error')}")
                return {"unread_dms": 0, "unread_from": []}

            all_channels.extend(data.get("channels", []))

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        print(f"   Checking {len(all_channels)} DM conversations...")

        unread_from = []
        for channel in all_channels:
            channel_id = channel.get("id")
            dm_user_id = channel.get("user")

            # Get the latest message in this DM
            history = requests.get(
                "https://slack.com/api/conversations.history",
                headers=headers,
                params={"channel": channel_id, "limit": 1},
            )

            hist_data = history.json()
            if not hist_data.get("ok"):
                continue

            messages = hist_data.get("messages", [])
            if not messages:
                continue

            latest_msg = messages[0]
            latest_ts = float(latest_msg.get("ts", 0))
            sender_id = latest_msg.get("user", "")

            # Skip if the last message is from you — that's not "unread"
            if sender_id == SLACK_USER_ID:
                continue

            # Get the last_read marker
            info = requests.get(
                "https://slack.com/api/conversations.info",
                headers=headers,
                params={"channel": channel_id},
            )

            info_data = info.json()
            if not info_data.get("ok"):
                continue

            last_read = float(info_data.get("channel", {}).get("last_read", 0))

            if latest_ts > last_read:
                name = resolve_slack_user(dm_user_id) if dm_user_id else "Unknown"
                text = latest_msg.get("text", "")
                text = humanize_slack_text(text)
                preview = (text[:80] + "…") if len(text) > 80 else text
                unread_from.append({"name": name, "preview": preview})

        print(f"   Found {len(unread_from)} unread DM conversations")
        return {"unread_dms": len(unread_from), "unread_from": unread_from}

    except Exception as e:
        print(f"   ⚠️ Exception fetching DMs: {e}")
        return {"unread_dms": 0, "unread_from": []}
# ============================================
# 3b. Slack User Resolution & Text Cleanup
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
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or data["user"].get("real_name")
                or user_id
            )
            _user_cache[user_id] = name
            return name
        else:
            _user_cache[user_id] = user_id
            return user_id

    except Exception:
        _user_cache[user_id] = user_id
        return user_id


def humanize_slack_text(text):
    """Replace <@U12345> user mentions with display names, and clean up Slack markup."""

    def replace_user_mention(match):
        user_id = match.group(1)
        name = resolve_slack_user(user_id)
        return f"*{name}*"

    text = re.sub(r"<@(U[A-Z0-9]+)>", replace_user_mention, text)
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    return text


# ============================================
# 3c. Fetch Channel Summaries
# ============================================
def fetch_channel_summaries():
    """Fetch messages from the past 24 hours in each configured channel."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    oldest = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    summaries = {}

    for channel_id, channel_name in SLACK_CHANNELS.items():
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

            real_messages = [
                m
                for m in messages
                if m.get("subtype")
                not in ("channel_join", "channel_leave", "bot_add", "bot_remove")
            ]

            highlights = []
            for msg in real_messages[:5]:
                text = msg.get("text", "")
                text = humanize_slack_text(text)
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
# 3d. Fetch React-Based Saved Items
# ============================================
def fetch_saved_reactions():
    """Fetch messages the user reacted to with the save emoji in the past 24h."""
    if not SLACK_USER_TOKEN:
        print("   ⚠️ SLACK_USER_TOKEN not configured — skipping saved items")
        return None

    headers = {
        "Authorization": f"Bearer {SLACK_USER_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            "https://slack.com/api/reactions.list",
            headers=headers,
            params={"limit": 50},
        )

        data = response.json()

        if not data.get("ok"):
            print(f"   ⚠️ Error fetching reactions: {data.get('error')}")
            return None

        items = data.get("items", [])
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()

        saved = []
        for item in items:
            if item.get("type") != "message":
                continue

            msg = item.get("message", {})

            has_save = False
            for reaction in msg.get("reactions", []):
                if reaction["name"] == SAVE_EMOJI:
                    has_save = True
                    break

            if not has_save:
                continue

            ts = float(msg.get("ts", 0))
            if ts < cutoff:
                continue

            text = msg.get("text", "")
            text = humanize_slack_text(text)
            preview = (text[:120] + "…") if len(text) > 120 else text

            channel_id = item.get("channel")
            channel_label = f"<#{channel_id}>" if channel_id else ""

            user_id = msg.get("user", "")
            author = resolve_slack_user(user_id) if user_id else ""
            author_label = f"*{author}*: " if author else ""

            saved.append({
                "preview": f"{author_label}{preview}",
                "channel": channel_label,
            })

        print(f"   Found {len(saved)} saved messages in last 24h")
        return saved

    except Exception as e:
        print(f"   ⚠️ Exception fetching reactions: {e}")
        return None


# ============================================
# 4. Format & Send Slack Message
# ============================================
def priority_emoji(priority):
    return {1: "🔴", 2: "🟠", 3: "🟡", 4: "⚪"}.get(priority, "⚪")


def build_message(in_progress, todo, calendar_events, slack_highlights, channel_summaries, saved_items):
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
        text = "*🔥 In Progress*\n"
        for item in in_progress[:7]:
            emoji = priority_emoji(item["priority"])
            text += f"  {emoji} <{item['url']}|{item['id']}>: {item['title']}\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    # --- Todo ---
    if todo:
        text = "*📋 Up Next (Todo)*\n"
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

    # --- Slack DMs ---
    # (Removed — Slack API doesn't reliably track read state)

    # --- Saved for Later ---
    if saved_items:
        text = "*🔖 Saved for Later (last 24h)*\n"
        for item in saved_items:
            channel_ctx = f"  {item['channel']}" if item["channel"] else ""
            text += f"  • {item['preview']}{channel_ctx}\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    elif saved_items is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*🔖 Saved for Later*\n  _No saved items in last 24h_ ✨",
                },
            }
        )

    blocks.append({"type": "divider"})

    # --- Channel Summaries ---
    if channel_summaries:
        text = "*📢 Channel Activity (last 24h)*\n"
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
    print("📋 Fetching Linear issues...")
    in_progress, todo = fetch_linear_issues()
    print(f"   Found {len(in_progress)} in progress, {len(todo)} todo")

    print("📅 Fetching calendar events...")
    calendar_events = fetch_calendar_events()

    print("💬 Fetching Slack highlights...")
    slack_highlights = fetch_slack_highlights()

    print("🔖 Fetching saved reactions...")
    saved_items = fetch_saved_reactions()

    print("📢 Fetching channel summaries...")
    channel_summaries = fetch_channel_summaries()

    print("📨 Building and sending daily brief...")
    blocks = build_message(
        in_progress, todo, calendar_events, slack_highlights, channel_summaries, saved_items
    )
    send_slack_dm(blocks)
