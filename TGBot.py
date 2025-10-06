"""
Telegram moderation & utility bot
Features:
1. Kick any mentioned user (supports reply or @username)
2. Broadcast messages to all known bot users and provided channel list (admin only)
3. Send requested files from a pre-configured files list
4. Auto-delete messages containing links or banned/vulgar words. Warn users and ban after 3 warnings per chat.
5. Return user's Telegram ID on request (/id)

Usage:
- Set TOKEN and ADMIN_IDS in environment or directly in the file (not recommended for production).
- Keep a `data.json` alongside this script to persist users, warnings, banned list, and files mapping.
- Ensure the bot has necessary rights in groups/channels (can delete messages, ban users, read messages).

Dependencies:
- python-telegram-bot>=20.0

This script is written as a single-file bot using PTB v20 (asyncio).
"""

import asyncio
import json
import logging
import os
import re
from typing import Dict, Any, Optional, List

from telegram import Update, ChatPermissions, MessageEntity
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# -------------------- Configuration --------------------
TOKEN = os.environ.get("TG_BOT_TOKEN") or "7577442080:AAHZinguRZv0ZsA3VkYB_uIF0Kqve7TBx1w"
# Put admin user ids here (ints). Only these users can execute admin commands.
ADMIN_IDS = [1293486023]  # <- replace with your Telegram user id(s)
# Path to data storage
DATA_FILE = "data.json"
# Directory where files for /getfile are stored
FILES_DIR = "files"
# Warning limit before ban
WARN_LIMIT = 3

# Banned/vulgar words (lowercase). Extend as needed.
BANNED_WORDS = {
    "badword1",
    "badword2",
    "somevulgarword",
}

# Link regex (detects http/https, www, t.me and common URL tokens)
LINK_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|\S+\.com\b|\S+\.in\b)", re.IGNORECASE)

# -------------------- Logging --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------- Persistence Helpers --------------------
DEFAULT_DATA = {
    "users": {},  # user_id -> {"first_name":..., "username":..., "chats": [chat_id,...]}
    "warnings": {},  # chat_id -> {user_id -> warning_count}
    "banned": {},  # chat_id -> [user_id,...]
    "files": {},  # key -> {"path": "files/example.pdf", "desc": "..."}
    "channels": []  # list of channel chat_ids where bot should broadcast (optional)
}


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# initialize data on startup
data = load_data()

# Ensure files dir exists
os.makedirs(FILES_DIR, exist_ok=True)

# -------------------- Utility Functions --------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def register_user(user) -> None:
    uid = str(user.id)
    updated = False
    if uid not in data["users"]:
        data["users"][uid] = {
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "chats": [],
        }
        updated = True
    # Don't duplicate chat ids when called with chat context
    save_if_updated = False
    if hasattr(user, 'id'):
        pass


def ensure_chat_user(chat_id: int, user_id: int, user) -> None:
    # Register user and record chat membership for broadcasting
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "first_name": getattr(user, 'first_name', '') or '',
            "last_name": getattr(user, 'last_name', '') or '',
            "username": getattr(user, 'username', '') or '',
            "chats": [],
        }
    if chat_id not in data["users"][uid]["chats"]:
        data["users"][uid]["chats"].append(chat_id)
        save_data(data)


def increment_warning(chat_id: int, user_id: int) -> int:
    chat_key = str(chat_id)
    uid = str(user_id)
    if chat_key not in data["warnings"]:
        data["warnings"][chat_key] = {}
    data["warnings"][chat_key][uid] = data["warnings"][chat_key].get(uid, 0) + 1
    save_data(data)
    return data["warnings"][chat_key][uid]


def reset_warnings(chat_id: int, user_id: int) -> None:
    chat_key = str(chat_id)
    uid = str(user_id)
    if chat_key in data["warnings"] and uid in data["warnings"][chat_key]:
        data["warnings"][chat_key][uid] = 0
        save_data(data)


def get_warnings(chat_id: int, user_id: int) -> int:
    chat_key = str(chat_id)
    uid = str(user_id)
    return data.get("warnings", {}).get(chat_key, {}).get(uid, 0)


def ban_user_record(chat_id: int, user_id: int) -> None:
    chat_key = str(chat_id)
    if chat_key not in data["banned"]:
        data["banned"][chat_key] = []
    if user_id not in data["banned"][chat_key]:
        data["banned"][chat_key].append(user_id)
        save_data(data)


def unban_user_record(chat_id: int, user_id: int) -> None:
    chat_key = str(chat_id)
    if chat_key in data["banned"] and user_id in data["banned"][chat_key]:
        data["banned"][chat_key].remove(user_id)
        save_data(data)


def is_user_banned(chat_id: int, user_id: int) -> bool:
    chat_key = str(chat_id)
    return user_id in data.get("banned", {}).get(chat_key, [])


def list_files_text() -> str:
    if not data.get("files"):
        return "No files available." 
    lines = ["Available files:"]
    for k, v in data["files"].items():
        desc = v.get("desc", "")
        lines.append(f"{k} - {os.path.basename(v.get('path',''))} {('- ' + desc) if desc else ''}")
    return "\n".join(lines)


# -------------------- Moderation Helpers --------------------

def contains_link_or_banned(text: str) -> bool:
    if not text:
        return False
    if LINK_PATTERN.search(text):
        return True
    lowered = text.lower()
    for w in BANNED_WORDS:
        if w in lowered:
            return True
    return False


def extract_target_user_id(update: Update) -> Optional[int]:
    # Support reply: /kick (as reply) -> target is replied user
    msg = update.effective_message
    if msg.reply_to_message:
        return msg.reply_to_message.from_user.id

    # Support username: /kick @username or user id directly
    args = msg.text.split()
    if len(args) >= 2:
        target_raw = args[1].strip()
        if target_raw.startswith("@"):
            # Try to resolve username to user via get_chat
            try:
                chat = update.effective_chat
                # get_chat won't resolve @username in general unless it's a channel or bot knows; try get_chat
                resolved = asyncio.run_coroutine_threadsafe(
                    update.get_bot().get_chat(target_raw), asyncio.get_event_loop()
                ).result()
                return resolved.id
            except Exception:
                return None
        else:
            # maybe numeric id
            if target_raw.isdigit():
                return int(target_raw)
    return None


# -------------------- Command Handlers --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_chat_user(update.effective_chat.id if update.effective_chat else update.effective_user.id, user.id, user)
    await update.message.reply_text("Hello! I'm moderation bot. Use /id to get your id or contact admin for help.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Your user id is: {user.id}")


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = list_files_text()
    await update.message.reply_text(text)


async def cmd_getfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /getfile <file_key>")
        return
    key = args[0]
    fileinfo = data.get("files", {}).get(key)
    if not fileinfo:
        await update.message.reply_text("File not found. Use /files to list available files.")
        return
    path = fileinfo.get("path")
    if not path or not os.path.exists(path):
        await update.message.reply_text("File path invalid or missing on server.")
        return
    # send file
    try:
        await update.message.reply_document(document=open(path, "rb"))
    except Exception as e:
        await update.message.reply_text(f"Failed to send file: {e}")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin only
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You are not allowed to use this command.")
        return
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text("Usage: /broadcast <message>\nOr reply to a message with /broadcast to forward it to everyone.")
        return

    # Build message text
    if update.message.reply_to_message and not context.args:
        # Broadcast the replied message (forward)
        forward_from = update.message.reply_to_message
        text = None
        # We'll forward the message to each user (keeps format)
        recipients = list(data.get("users", {}).keys())
        sent, failed = 0, 0
        for uid in recipients:
            try:
                await context.bot.forward_message(int(uid), update.effective_chat.id, forward_from.message_id)
                sent += 1
            except Exception as e:
                failed += 1
        await update.message.reply_text(f"Broadcast completed. Sent: {sent}, Failed: {failed}")
        return
    else:
        text = " ".join(context.args)

    recipients = []
    # Broadcast to users
    for uid, meta in data.get("users", {}).items():
        recipients.append(int(uid))
    # Also include channels stored in data["channels"] if any
    channel_ids = data.get("channels", [])

    sent, failed = 0, 0
    for rid in recipients + channel_ids:
        try:
            await context.bot.send_message(rid, text)
            sent += 1
        except Exception as e:
            failed += 1
    await update.message.reply_text(f"Broadcast completed. Sent: {sent}, Failed: {failed}")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /kick")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("/kick must be used in a group or channel by replying to a user or specifying @username/id")
        return

    target_id = None
    # If reply
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        # try username or id
        raw = context.args[0]
        if raw.startswith("@"):
            try:
                resolved = await context.bot.get_chat(raw)
                target_id = resolved.id
            except Exception:
                await update.message.reply_text("Couldn't resolve that username. Try replying to the user instead.")
                return
        elif raw.isdigit():
            target_id = int(raw)

    if not target_id:
        await update.message.reply_text("Could not determine target. Use /kick as a reply to a user, or /kick @username or /kick <user_id>")
        return

    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
        await update.message.reply_text("User kicked successfully.")
    except Exception as e:
        await update.message.reply_text(f"Failed to kick user: {e}")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /warn")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message you want to warn and use /warn")
        return
    target = update.message.reply_to_message.from_user
    new_count = increment_warning(update.effective_chat.id, target.id)
    await update.message.reply_text(f"Warned {target.full_name}. Total warnings in this chat: {new_count}")
    if new_count >= WARN_LIMIT:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            ban_user_record(update.effective_chat.id, target.id)
            await update.message.reply_text(f"{target.full_name} has been banned for reaching {new_count} warnings.")
        except Exception as e:
            await update.message.reply_text(f"Failed to ban: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /ban")
        return
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text("Use by replying to user's message or /ban <user_id>")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target_id)
        ban_user_record(update.effective_chat.id, target_id)
        await update.message.reply_text("User banned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to ban: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /unban")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    if not context.args[0].isdigit():
        await update.message.reply_text("Provide numeric user id")
        return
    target_id = int(context.args[0])
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target_id)
        unban_user_record(update.effective_chat.id, target_id)
        await update.message.reply_text("User unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unban: {e}")


# -------------------- Message Handler (moderation + register) --------------------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not user or not msg or not chat:
        return

    # Register user for broadcasting
    ensure_chat_user(chat.id if chat else user.id, user.id, user)

    # Only moderate group & supergroup messages (you can tweak to moderate private chats too)
    if chat.type in ("group", "supergroup"):
        text = (msg.text or "") + " " + (msg.caption or "")
        if contains_link_or_banned(text):
            # delete message if possible
            try:
                await msg.delete()
            except Exception as e:
                logger.warning(f"Couldn't delete message: {e}")
            # increment warning and notify
            new_count = increment_warning(chat.id, user.id)
            try:
                await context.bot.send_message(chat.id, f"{user.mention_html()} — your message contained a link/forbidden word and was removed. Warning {new_count}/{WARN_LIMIT}.", parse_mode="HTML")
            except Exception:
                pass
            if new_count >= WARN_LIMIT:
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    ban_user_record(chat.id, user.id)
                    await context.bot.send_message(chat.id, f"{user.mention_html()} has been banned after {new_count} warnings.", parse_mode="HTML")
                except Exception as e:
                    logger.exception("Error banning user after warnings")

    # Optionally react to commands or keywords in private chats
    # For example, respond to '/id' in private if user asks


# -------------------- Admin utility endpoints for managing files --------------------

async def cmd_addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /addfile")
        return

    # Usage: reply to a file with /addfile <key> <optional description>
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Reply to a document with /addfile <key> [description]")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Provide a file key (single word) to identify this file.")
        return
    key = args[0]
    desc = " ".join(args[1:]) if len(args) > 1 else ""
    doc = update.message.reply_to_message.document
    file_obj = await doc.get_file()
    filename = os.path.join(FILES_DIR, f"{key}_{doc.file_name}")
    await file_obj.download_to_drive(filename)
    data.setdefault("files", {})[key] = {"path": filename, "desc": desc}
    save_data(data)
    await update.message.reply_text(f"Saved file as key: {key}")


async def cmd_rmfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use /rmfile")
        return
    if not context.args:
        await update.message.reply_text("Usage: /rmfile <key>")
        return
    key = context.args[0]
    fileinfo = data.get("files", {}).get(key)
    if not fileinfo:
        await update.message.reply_text("Unknown key.")
        return
    path = fileinfo.get("path")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    del data["files"][key]
    save_data(data)
    await update.message.reply_text("Removed file entry.")


# -------------------- Setup & Run --------------------

def main():
    if TOKEN.startswith("REPLACE"):
        print("Please set your bot token in the TOKEN variable or TG_BOT_TOKEN environment variable.")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler(["start"], start))
    app.add_handler(CommandHandler(["id"], cmd_id))
    app.add_handler(CommandHandler(["files"], cmd_files))
    app.add_handler(CommandHandler(["getfile"], cmd_getfile))
    app.add_handler(CommandHandler(["broadcast"], cmd_broadcast))
    app.add_handler(CommandHandler(["kick"], cmd_kick))
    app.add_handler(CommandHandler(["warn"], cmd_warn))
    app.add_handler(CommandHandler(["ban"], cmd_ban))
    app.add_handler(CommandHandler(["unban"], cmd_unban))
    app.add_handler(CommandHandler(["addfile"], cmd_addfile))
    app.add_handler(CommandHandler(["rmfile"], cmd_rmfile))

    # Message handler for moderation and general registering
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))

    print("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
