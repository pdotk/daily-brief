import os
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

# ============================================
# Configuration (pulled from GitHub Secrets)
# ============================================
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
ROUTE_EMOJI = os.environ.get("ROUTE_EMOJI", "bookmark")
TARGET_CHANNEL_ID = os.environ["REACTION_TARGET_CHANNEL_ID"]

app = App(token=SLACK_BOT_TOKEN)
user_client = WebClient(token=SLACK_USER_TOKEN) if SLACK_USER_TOKEN else None


# ============================================
# Helpers
# ============================================
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


# ============================================
# Reaction Handler
# ============================================
@app.event("reaction_added")
def handle_reaction(event, client):
    # Only trigger for your user ID
    if event["user"] != SLACK_USER_ID:
        return

    # Only trigger for your chosen emoji
    if event["reaction"] != ROUTE_EMOJI:
        return

    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if not channel_id or not message_ts:
        print("⚠️ Missing channel or ts in reaction event")
        return

    # Use user token for DMs (channel IDs starting with D), bot token for everything else
    is_dm = channel_id.startswith("D")
    fetch_client = user_client if is_dm and user_client else client

    if is_dm and not user_client:
        print("⚠️ SLACK_USER_TOKEN not configured — cannot fetch DM messages")
        return

    # Fetch the original message
    try:
        result = fetch_client.conversations_history(
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


# ============================================
# Entry Point
# ============================================
if __name__ == "__main__":
    print("🔄 Starting up...")
    print(f"🎯 Reaction router listening for :{ROUTE_EMOJI}: reactions...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
