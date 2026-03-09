import os
import json
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

    # Check for errors
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

        # Get today's events in Central Time
        now = datetime.now(timezone(timedelta(hours=-5)))  # CDT (adjust to -6 for CST)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_of_day,
            timeMax=end_of_day,
            singleEvents=True,
            orderBy="startTime",
            maxResults=15,
        ).execute()

        events = []
        for event in result.get("items", []):
            start = event.get("start", {})
            time_str = start.get("dateTime", start.get("date", ""))
            if "T" in time_str:
                time_display = datetime.fromisoformat(time_str).strftime("%-I:%M%p")
            else:
                time_display = "All day"

            events.append({
                "time": time_display,
                "title": event.get("summary", "No title"),
            })

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

    # Get unread conversation count
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
# 4. Format & Send Slack Message
# ============================================
def priority_emoji(priority):
    return {1: "🔴", 2: "🟠", 3: "🟡", 4: "⚪"}.get(priority, "⚪")


def build_message(in_progress, todo, calendar_events, slack_highlights):
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
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*📅 Calendar*\n  _Not configured — add Google Calendar API key to enable_"},
        })

    # --- Slack ---
    if slack_highlights:
        text = "*💬 Slack*\n"
        text += f"  • {slack_highlights['unread_dms']} unread DM conversations\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    # --- Footer ---
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": "🤖 _Your daily brief, powered by GitHub Actions_"}
        ],
    })

    return blocks


def send_slack_dm(blocks):
    """Send a DM to yourself via Slack."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    # Open a DM channel with yourself
    response = requests.post(
        "https://slack.com/api/conversations.open",
        headers=headers,
        json={"users": SLACK_USER_ID},
    )
    response.raise_for_status()
    data = response.json()

    # Check for Slack API errors
    if not data.get("ok"):
        print(f"❌ Slack error opening DM: {data.get('error', 'unknown error')}")
        print(f"   Full response: {data}")
        return

    channel_id = data["channel"]["id"]

    # Send the message
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

    print("📨 Building and sending daily brief...")
    blocks = build_message(in_progress, todo, calendar_events, slack_highlights)
    send_slack_dm(blocks)
