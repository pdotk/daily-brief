import os
import json
import re
import argparse
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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
RECAP_MODEL = os.environ.get("RECAP_MODEL") or "gpt-4o-mini"

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
# 3e. Work Recap Helpers
# ============================================

def activity_item(source, type_, title, text="", url="", created_at="", people=None, metadata=None):
    """Normalize all recap activity into one common shape."""
    return {
        "source": source,
        "type": type_,
        "title": title or "",
        "text": text or "",
        "url": url or "",
        "created_at": created_at or "",
        "people": people or [],
        "metadata": metadata or {},
    }


def linear_graphql(query, variables=None):
    """Run a Linear GraphQL query."""
    response = requests.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": variables or {},
        },
    )

    data = response.json()

    if "errors" in data:
        print(f"   ❌ Linear API errors: {data['errors']}")
        return None

    return data.get("data", {})


def fetch_linear_viewer_id():
    """Return the current Linear user's ID."""
    query = """
    query {
        viewer {
            id
            name
            email
        }
    }
    """

    data = linear_graphql(query)
    if not data:
        return None

    viewer = data.get("viewer")
    if not viewer:
        return None

    return viewer.get("id")


def fetch_linear_recap_activity(days=14):
    """Fetch Linear issues assigned to or created by you that changed recently."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    user_id = fetch_linear_viewer_id()
    if not user_id:
        print("   ⚠️ Could not determine Linear viewer")
        return []

    query = """
    query($userId: ID!, $since: DateTimeOrDuration!) {
        issues(
            filter: {
                updatedAt: { gte: $since }
                or: [
                    { assignee: { id: { eq: $userId } } }
                    { creator: { id: { eq: $userId } } }
                ]
            }
            orderBy: updatedAt
            first: 100
        ) {
            nodes {
                identifier
                title
                description
                url
                createdAt
                updatedAt
                completedAt
                priority
                state {
                    name
                    type
                }
                team {
                    key
                    name
                }
                assignee {
                    name
                    email
                }
                creator {
                    name
                    email
                }
            }
        }
    }
    """

    data = linear_graphql(
        query,
        {
            "userId": user_id,
            "since": since,
        },
    )

    if not data:
        return []

    issues = data.get("issues", {}).get("nodes", [])
    items = []

    for issue in issues:
        state = issue.get("state", {}) or {}
        team = issue.get("team", {}) or {}

        text_parts = []

        if issue.get("description"):
            text_parts.append(issue["description"][:700])

        text_parts.append(f"State: {state.get('name', '')}")
        text_parts.append(f"Team: {team.get('key', '')}")

        if issue.get("completedAt"):
            text_parts.append(f"Completed: {issue['completedAt']}")

        people = []
        if issue.get("assignee", {}).get("name"):
            people.append(issue["assignee"]["name"])
        if issue.get("creator", {}).get("name"):
            people.append(issue["creator"]["name"])

        items.append(
            activity_item(
                source="linear",
                type_="issue",
                title=f"{issue['identifier']}: {issue['title']}",
                text="\n".join(text_parts),
                url=issue.get("url", ""),
                created_at=issue.get("updatedAt", ""),
                people=people,
                metadata={
                    "identifier": issue.get("identifier", ""),
                    "state": state.get("name", ""),
                    "state_type": state.get("type", ""),
                    "team": team.get("key", ""),
                    "completed_at": issue.get("completedAt", ""),
                },
            )
        )

    print(f"   Found {len(items)} Linear recap issues")
    return items


def fetch_calendar_activity(days=14):
    """Fetch calendar events from the past N days for recap."""
    if not GOOGLE_CALENDAR_CREDENTIALS:
        print("   Calendar not configured — skipping recap calendar activity")
        return []

    try:
        creds_json = json.loads(GOOGLE_CALENDAR_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(
            creds_json,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )

        service = build("calendar", "v3", credentials=credentials)

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        result = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )

        ignore_keywords = [
            "lunch",
            "ooo",
            "pto",
            "focus",
            "hold",
            "blocked",
            "school",
        ]

        items = []

        for event in result.get("items", []):
            title = event.get("summary", "No title")

            if any(keyword in title.lower() for keyword in ignore_keywords):
                continue

            if event.get("status") == "cancelled":
                continue

            start_data = event.get("start", {})
            start_time = start_data.get("dateTime", start_data.get("date", ""))

            attendees = []
            for attendee in event.get("attendees", []):
                if attendee.get("responseStatus") == "declined":
                    continue
                email = attendee.get("email")
                if email:
                    attendees.append(email)

            description = event.get("description", "")
            description = re.sub(r"<[^>]+>", "", description)

            items.append(
                activity_item(
                    source="calendar",
                    type_="event",
                    title=title,
                    text=description[:500],
                    url=event.get("htmlLink", ""),
                    created_at=start_time,
                    people=attendees[:10],
                    metadata={
                        "organizer": event.get("organizer", {}).get("email", ""),
                    },
                )
            )

        print(f"   Found {len(items)} calendar recap events")
        return items

    except Exception as e:
        print(f"   ⚠️ Calendar recap error: {e}")
        return []


def fetch_slack_recap_activity(days=14):
    """Fetch your messages from configured Slack channels over the past N days."""
    if not SLACK_CHANNELS:
        print("   No SLACK_CHANNELS configured — skipping Slack recap activity")
        return []

    token = SLACK_BOT_TOKEN

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    oldest = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    items = []

    for channel_id, channel_name in SLACK_CHANNELS.items():
        try:
            cursor = None

            while True:
                params = {
                    "channel": channel_id,
                    "oldest": str(oldest),
                    "limit": 200,
                }

                if cursor:
                    params["cursor"] = cursor

                response = requests.get(
                    "https://slack.com/api/conversations.history",
                    headers=headers,
                    params=params,
                )

                data = response.json()

                if not data.get("ok"):
                    print(f"   ⚠️ Slack recap error for {channel_name}: {data.get('error')}")
                    break

                for msg in data.get("messages", []):
                    if msg.get("user") != SLACK_USER_ID:
                        continue

                    subtype = msg.get("subtype")
                    if subtype in ("channel_join", "channel_leave", "bot_message"):
                        continue

                    text = humanize_slack_text(msg.get("text", ""))
                    if not text.strip():
                        continue

                    ts = msg.get("ts", "")
                    created_at = datetime.fromtimestamp(float(ts), timezone.utc).isoformat()

                    permalink = ""
                    try:
                        link_response = requests.get(
                            "https://slack.com/api/chat.getPermalink",
                            headers=headers,
                            params={
                                "channel": channel_id,
                                "message_ts": ts,
                            },
                        )
                        link_data = link_response.json()
                        if link_data.get("ok"):
                            permalink = link_data.get("permalink", "")
                    except Exception:
                        pass

                    items.append(
                        activity_item(
                            source="slack",
                            type_="message",
                            title=f"Slack message in {channel_name}",
                            text=text[:1000],
                            url=permalink,
                            created_at=created_at,
                            metadata={
                                "channel": channel_name,
                                "thread_ts": msg.get("thread_ts", ""),
                            },
                        )
                    )

                cursor = data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        except Exception as e:
            print(f"   ⚠️ Exception fetching Slack recap for {channel_name}: {e}")

    print(f"   Found {len(items)} Slack recap messages")
    return items


def summarize_recap_with_openai(items, days=14):
    """Summarize recap items using OpenAI, if configured."""
    if not OPENAI_API_KEY:
        print("   OPENAI_API_KEY not configured — skipping AI summary")
        return None

    compact_items = []

    for item in items:
        compact_items.append(
            {
                "source": item["source"],
                "type": item["type"],
                "title": item["title"],
                "text": item["text"][:1200],
                "url": item["url"],
                "created_at": item["created_at"],
                "metadata": item["metadata"],
            }
        )

    prompt = f"""
You are helping Pam prepare a concise work recap for the past {days} days.

Summarize the activity into:

1. Highlights
2. Work by theme/project
3. Decisions, outcomes, or progress
4. Meetings/collaboration
5. Follow-ups

Rules:
- Do not invent work that is not supported by the activity.
- Merge duplicates across Slack, calendar, and Linear.
- Prefer concrete verbs: coordinated, reviewed, resolved, drafted, shipped, followed up.
- Keep it useful for a personal work log or check-in.
- Include source links where helpful.
- Be concise, but not vague.
- Avoid including sensitive candidate details. Generalize names if needed.

Activity:
{json.dumps(compact_items, indent=2)}
"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": RECAP_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "You write concise, accurate work recaps from activity logs.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "temperature": 0.2,
            },
            timeout=60,
        )

        data = response.json()

        if response.status_code >= 400:
            print(f"   ⚠️ OpenAI error: {response.status_code} {data}")
            return None

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print(f"   ⚠️ OpenAI recap error: {e}")
        return None


def summarize_recap_fallback(items, days=14):
    """Simple fallback recap if OpenAI is not configured."""
    by_source = {}

    for item in items:
        by_source.setdefault(item["source"], []).append(item)

    lines = [
        f"*Work recap — last {days} days*",
        "",
        "*Activity collected:*",
    ]

    for source, source_items in by_source.items():
        lines.append(f"• {source}: {len(source_items)} items")

    lines.append("")
    lines.append("*Recent highlights by source:*")

    for source, source_items in by_source.items():
        lines.append("")
        lines.append(f"*{source.title()}*")

        for item in source_items[:10]:
            title = item["title"]
            url = item.get("url")
            if url:
                lines.append(f"• <{url}|{title}>")
            else:
                lines.append(f"• {title}")

    return "\n".join(lines)


def build_recap_blocks(summary, days=14, item_count=0):
    """Build Slack blocks for the recap DM."""
    today = datetime.now().strftime("%A, %B %-d")

    max_len = 2800
    if len(summary) > max_len:
        summary = summary[:max_len] + "\n\n_Trimmed because Slack has opinions._"

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🧾 Work Recap — Last {days} Days",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Generated {today} from {item_count} activity items.",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": summary,
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🤖 _Your work recap, powered by GitHub Actions_",
                }
            ],
        },
    ]

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

def run_daily():
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


def run_recap(days=14):
    print(f"🧾 Generating work recap for last {days} days...")

    all_items = []

    print("📋 Fetching Linear recap activity...")
    all_items.extend(fetch_linear_recap_activity(days=days))

    print("📅 Fetching calendar recap activity...")
    all_items.extend(fetch_calendar_activity(days=days))

    print("💬 Fetching Slack recap activity...")
    all_items.extend(fetch_slack_recap_activity(days=days))

    all_items = sorted(
        all_items,
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )

    print(f"   Total recap items: {len(all_items)}")

    summary = summarize_recap_with_openai(all_items, days=days)

    if not summary:
        print("   Using fallback recap summary")
        summary = summarize_recap_fallback(all_items, days=days)

    blocks = build_recap_blocks(summary, days=days, item_count=len(all_items))

    print("📨 Sending work recap...")
    send_slack_dm(blocks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily brief and work recap bot")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["daily", "recap"],
        default="daily",
        help="Run daily brief or recap",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days to include for recap",
    )

    args = parser.parse_args()

    if args.mode == "recap":
        run_recap(days=args.days)
    else:
        run_daily()
