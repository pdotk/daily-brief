# ☀️ Daily Brief Bot

A personal daily briefing bot that sends you a Slack DM every weekday morning with your tasks, calendar, and team activity — powered by GitHub Actions.

![Python](https://img.shields.io/badge/python-3.12-blue)
![GitHub Actions](https://img.shields.io/badge/runs%20on-GitHub%20Actions-2088FF)

## What You'll Get

Every weekday morning, a Slack DM like this:

```
☀️ Daily Brief — Monday, March 10

────────────────────────────
🔥 In Progress
  🔴 PMK-123: Update hiring dashboard
  🟠 PMK-456: Review candidate pipeline

📋 Up Next (Todo)
  🟡 PMK-789: Prepare weekly sync notes
  ⚪ PMK-101: Archive old job postings

────────────────────────────
📅 Today's Calendar
  • 9:00AM — Team standup
  • 11:30AM — 1:1 with manager
  • 2:00PM — Interview: Senior Engineer

💬 Slack
  • 3 unread DM conversations

🔖 Saved for Later (last 24h)
  • Gaby Garcia: New referral process doc…  #talent

────────────────────────────
📢 Channel Activity (last 24h)

  #talent-recruiting-and-operations: 9 messages
    • Updated the hiring reset timeline…
    • Reminder: submit feedback by Friday…

  #referrals-talent: 1 message
    • New referral submitted for Senior Engineer…

  #talent: No new messages 🤫
────────────────────────────
🤖 Your daily brief, powered by GitHub Actions
```

## Features

| Feature | Description |
|---|---|
| 📋 **Linear issues** | In Progress and Todo items from your team |
| 📅 **Google Calendar** | Today's meetings and events |
| 💬 **Slack DMs** | Unread conversation count |
| 🔖 **Saved items** | Messages you reacted to with a chosen emoji (last 24h) |
| 📢 **Channel summaries** | Recent messages from channels you care about |
| 👤 **Name resolution** | Slack user IDs automatically replaced with display names |

---

## Prerequisites

- A [GitHub](https://github.com) account (free tier works)
- A [Slack workspace](https://slack.com) where you can install apps
- A [Linear](https://linear.app) account (optional)
- A [Google Cloud](https://console.cloud.google.com) account (optional, for calendar)

---

## Setup

### 1. Create the Repo

1. Fork or clone this repo (or create a new **private** repo)
2. It should contain:
   ```
   daily-brief/
   ├── .github/workflows/daily-brief.yml
   ├── daily_brief.py
   └── README.md
   ```

### 2. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it (e.g., `Daily Brief Bot`) and select your workspace

#### Bot Token Scopes

**OAuth & Permissions** → **Bot Token Scopes**:

| Scope | Purpose |
|---|---|
| `chat:write` | Send DMs to you |
| `im:history` | Read DM unread counts |
| `im:read` | List DM conversations |
| `channels:history` | Read public channel messages |
| `groups:history` | Read private channel messages |
| `users:read` | Resolve user IDs to display names |

#### User Token Scopes

**OAuth & Permissions** → **User Token Scopes**:

| Scope | Purpose |
|---|---|
| `reactions:read` | Read your emoji reactions (for saved items) |

#### Install the App

1. Click **Install to Workspace** → **Allow**
2. Copy the **Bot User OAuth Token** (`xoxb-...`)
3. Copy the **User OAuth Token** (`xoxp-...`)

#### Invite the Bot to Channels

In each Slack channel you want summaries from:

```
/invite @Daily Brief Bot
```

#### Find Your Slack User ID

1. Click your name in Slack → **Profile**
2. Click the **⋮** menu → **Copy member ID**

### 3. Get a Linear API Key

1. Go to [linear.app/settings/api](https://linear.app/settings/api)
2. Click **Create Key** → name it `Daily Brief` → copy the key

### 4. Set Up Google Calendar (Optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a new project
2. Enable the **Google Calendar API**
3. Go to **IAM & Admin** → **Service Accounts** → **Create Service Account**
4. Create a JSON key for the service account and download it
5. In Google Calendar, share your calendar with the service account email (`...@...iam.gserviceaccount.com`) — give it **"See all event details"** permission
6. Your Calendar ID is usually your email address, or find it in **Calendar Settings** → **Integrate calendar**

> **Tip:** If you skip this step, the bot will still work — it'll just show "Calendar not configured" in the brief.

### 5. Configure GitHub Secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret Name | Value | Required? |
|---|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` (Bot User OAuth Token) | ✅ Yes |
| `SLACK_USER_TOKEN` | `xoxp-...` (User OAuth Token) | Optional (for saved items) |
| `SLACK_USER_ID` | Your Slack member ID (e.g., `U12345ABC`) | ✅ Yes |
| `LINEAR_API_KEY` | `lin_api_...` | ✅ Yes |
| `GOOGLE_CALENDAR_CREDENTIALS` | Entire JSON key file contents | Optional |
| `GOOGLE_CALENDAR_ID` | Your calendar ID (e.g., `you@gmail.com`) | Optional |

### 6. Test It!

1. Push your code
2. Go to **Actions** tab → **Daily Brief** → **Run workflow** → **Run**
3. Check your Slack DMs! ☀️

> **Note:** The first _scheduled_ run may take 24-48 hours to trigger. Use the manual **Run workflow** button to test immediately.

---

## Customization

### Channels to Summarize

Edit the `TALENT_CHANNELS` dict in `daily_brief.py`:

```python
TALENT_CHANNELS = {
    "C12345ABCDE": "#your-channel-name",
    "C67890FGHIJ": "#another-channel",
}
```

To find a channel ID: right-click the channel name in Slack → **View channel details** → scroll to the bottom.

### Linear Team

Change the team key in the GraphQL query to match your team:

```python
teams(filter: { key: { eq: "YOUR-TEAM-KEY" } })
```

Find your team key in your Linear URL: `linear.app/YOUR-TEAM-KEY/...`

### Save Emoji

Change the emoji used for react-based saves:

```python
SAVE_EMOJI = "bookmark"  # Change to any emoji name: star, eyes, pushpin, reminder, etc.
```

React with that emoji on any Slack message and it'll appear in your next morning's brief.

### Schedule

Edit the cron in `.github/workflows/daily-brief.yml`:

```yaml
schedule:
  - cron: '0 13 * * 1-5'  # UTC time!
```

Use [crontab.guru](https://crontab.guru) to build your schedule. **GitHub Actions cron is always UTC.**

| Your Time Zone | 8:00 AM local | Cron value |
|---|---|---|
| US Eastern (EDT) | 12:00 PM UTC | `0 12 * * 1-5` |
| US Central (CDT) | 1:00 PM UTC | `0 13 * * 1-5` |
| US Pacific (PDT) | 3:00 PM UTC | `0 15 * * 1-5` |
| UTC | 8:00 AM UTC | `0 8 * * 1-5` |
| CET (Central Europe) | 7:00 AM UTC | `0 7 * * 1-5` |

> ⚠️ The cron doesn't adjust for daylight saving time. Your brief will shift by 1 hour when clocks change.

### Calendar Time Zone

Adjust the UTC offset in `fetch_calendar_events()`:

```python
now = datetime.now(timezone(timedelta(hours=-5)))  # CDT=-5, CST=-6, EST=-5, PST=-8
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `channel_not_found` | Channel is private — add `groups:history` scope and reinstall |
| `not_in_channel` | Run `/invite @Your Bot Name` in the channel |
| `missing_scope` | Add the required scope in Slack app settings and **reinstall** the app |
| `token_revoked` | Reinstall the app and update the GitHub secret |
| `invalid_auth` on reactions | Use the **User Token** (`xoxp-`), not the Bot Token (`xoxb-`) |
| Workflow didn't trigger | New workflows can take 24-48h. Use **Run workflow** to test manually |
| Workflow disabled | GitHub disables workflows after 60 days of inactivity. Re-enable in Actions tab |
| No calendar events | Make sure you shared your calendar with the service account email |
| Wrong events / times | Check the UTC offset in `fetch_calendar_events()` and the `GOOGLE_CALENDAR_ID` |

---

## Architecture

```
GitHub Actions (cron: weekday mornings)
  │
  ├── Linear GraphQL API → fetch team issues
  ├── Google Calendar API → fetch today's events
  ├── Slack API (bot token) → fetch DMs, channel history, user profiles
  ├── Slack API (user token) → fetch emoji reactions
  │
  └── Slack API → send formatted DM to you
```

No servers, no databases, no hosting costs. Runs entirely on GitHub Actions free tier.

---

## License

MIT — use it, fork it, make it your own. ☀️
