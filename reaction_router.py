import os
import re
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

if __name__ == "__main__":
    print("🔄 Starting up...")
    print(f"🎯 Reaction router listening for :{ROUTE_EMOJI}: reactions...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()

# ============================================
# Configuration (pulled from GitHub Secrets)
# ============================================
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]       # New: needs xapp- token
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
ROUTE_EMOJI = os.environ.get("ROUTE_EMOJI", "bookmark") # emoji that triggers routing
TARGET_CHANNEL_ID = os.environ["REACTION_TARGET_CHANNEL_ID"] # where to copy messages

app = App(token=SLACK_BOT_TOKEN)

# ── Reuse helpers from daily_brief ──────────────────────────────────────────
_user_cache = {}

def resolve_slack_user(user_id):
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        res = app.client.users_info(user=user_id)
        if res["ok"]:
            profile = res["user"].get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or res["user"].get("real_name")
                or user_id
            )
            _user_cache[user_id] = name
            return name
    except Exception:
        pass
    _user_cache[user_id] = user_id
    return user_id

def humanize_slack_text(text):
    def replace_mention(match):
        return f"*{resolve_slack_user(match.group(1))}*"
    text = re.sub(r"<@(U[A-Z0-9]+)>", replace_mention, text)
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text

# ── Reaction handler ─────────────────────────────────────────────────────────
@app.event("reaction_added")
def handle_reaction(event, client):
    # Only you
    if event["user"] != SLACK_USER_ID:
        return

    # Only your chosen emoji
    if event["reaction"] != ROUTE_EMOJI:
        return

    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if not channel_id or not message_ts:
        print("⚠️ Missing channel or ts in reaction event")
        return

    # Fetch the original message
    try:
        result = client.conversations_history(
            channel=channel_id,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
        messages = result.get("messages", [])
        if not messages:
            print("⚠️ Could not fetch original message")
            return

        msg = messages[0]
        text = humanize_slack_text(msg.get("text", "_(no text)_"))
        author_id = msg.get("user", "")
        author = resolve_slack_user(author_id) if author_id else "Unknown"

        # Build a permalink for context
        try:
            permalink_res = client.chat_getPermalink(
                channel=channel_id, message_ts=message_ts
            )
            permalink = permalink_res.get("permalink", "")
        except Exception:
            permalink = ""

        # Post to target channel
        forwarded_text = f"*Saved by you* from *{author}*:\n\n{text}"
        if permalink:
            forwarded_text += f"\n\n<{permalink}|View original>"

        client.chat_postMessage(
            channel=TARGET_CHANNEL_ID,
            text=forwarded_text,
            unfurl_links=False,
        )
        print(f"✅ Routed message from {author} to {TARGET_CHANNEL_ID}")

    except Exception as e:
        print(f"❌ Error routing message: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🎯 Reaction router listening for :{ROUTE_EMOJI}: reactions...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
