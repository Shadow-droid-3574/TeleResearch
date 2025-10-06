"""
Telegram moderation & utility bot — Webhook-capable version
Features added/changed from previous file:
- Runs as a webhook (suitable for Codespaces / cloud deployment)
- Advanced link detection (including obfuscated links like "example[.]com", shorteners, IPs)
- Robust rotating-file logging (info + error logs)
- Per-chat customizable settings persisted in data.json (toggle link deletion, per-chat banned words, warn limit)
- Admin-only management endpoints (kick, ban, warn, broadcast, addfile, rmfile, setwebhook, setconfig)
- Uses python-telegram-bot v20 (asyncio) and aiohttp builtin server via `run_webhook`

Configuration (environment variables):
- TG_BOT_TOKEN : your bot token (required)
- ADMIN_IDS : comma-separated admin user ids (optional fallback to ADMIN_IDS in file)
- PORT : port to listen on (default 8443)
- HOST : host to bind (default 0.0.0.0)
- WEBHOOK_URL : public URL where Telegram will send updates (e.g. https://<your-domain>/<TOKEN>)

Notes:
- For deployment behind tunnels (ngrok / codespaces preview), set WEBHOOK_URL accordingly.
- Ensure the bot has these rights in groups: read messages, delete messages, ban users.

"""

import asyncio
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
from typing import Any, Dict, List, Optional

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# -------------------- Basic Configuration --------------------
TOKEN = os.environ.get("TG_BOT_TOKEN") or "REPLACE_WITH_YOUR_BOT_TOKEN"
# Provide ADMIN_IDS in env as comma-separated, else fallback to this list
env_admins = os.environ.get("ADMIN_IDS")
if env_admins:
    ADMIN_IDS = [int(x) for x in env_admins.split(",") if x.strip().isdigit()]
else:
    ADMIN_IDS = [123456789]  # Replace as needed

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
FILES_DIR = os.environ.get("FILES_DIR", "files")
DEFAULT_PORT = int(os.environ.get("PORT", 8443))
DEFAULT_HOST = os.environ.get("HOST", "0.0.0.0")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # must be full https://.../path
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH") or f"/webhook/{TOKEN}"

# Default global configuration
GLOBAL_CONFIG = {
    "warn_limit_default": 3,
    "delete_links_default": True,
    "ban_on_limit_default": True,
}

# -------------------- Advanced Link Detection --------------------
# This set of regexes aims to detect common URL forms, IP addresses, shorteners and obfuscated variants.
URL_RE = re.compile(
    r"(?P<url>https?://[\w\-.~:/?#\[\]@!$&'()*+,;=%]+|www\.[\w\-]+\.[\w\-\./?=&%]+|[\w\-]+\.(com|net|org|in|io|info|biz|co|me|xyz|tk)\b)",
    re.IGNORECASE,
)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Obfuscated dot patterns like example[.]com or example(dot)com
OBFUSCATED_DOT_RE = re.compile(r"\[\.\]|\(dot\)|\sdot\s|\{\.\}", re.IGNORECASE)
# Typical URL shorteners (common) - extend as needed
SHORTENER_DOMAINS = [
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "is.gd",
    "ow.ly",
]
SHORTENER_RE = re.compile(r"\b(" + "|".join([re.escape(x) for x in SHORTENER_DOMAINS]) + r")\b", re.IGNORECASE)

# Words that are banned globally by default (lowercase)
GLOBAL_BANNED_WORDS = {"badword1", "badword2", "somevulgarword"}

# -------------------- Logging --------------------
LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("tg_moderation_bot")
logger.setLevel(logging.INFO)
# Rotating handler for info
info_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "bot_info.log"), maxBytes=10 * 1024 * 1024, backupCount=5
)
info_handler.setLevel(logging.INFO)
# Separate handler for errors
error_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "bot_error.log"), maxBytes=5 * 1024 * 1024, backupCount=5
)
error_handler.setLevel(logging.ERROR)
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
info_handler.setFormatter(fmt)
error_handler.setFormatter(fmt)
logger.addHandler(info_handler)
logger.addHandler(error_handler)
# Also print to stderr
stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setFormatter(fmt)
logger.addHandler(stream_handler)

# -------------------- Persistence Helpers --------------------
DEFAULT_DATA = {
    "users": {},  # user_id -> {first_name, last_name, username, chats}
    "warnings": {},  # chat_id -> {user_id -> count}
    "banned": {},  # chat_id -> [user_id,...]
    "files": {},  # key -> {path, desc}
    "channels": [],  # list of channel ids for broadcasts
    "chats_config": {},  # chat_id -> {delete_links:bool, warn_limit:int, banned_words:set}
}


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return DEFAULT_DATA.copy()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
            # ensure required keys exist
            for k, v in DEFAULT_DATA.items():
                if k not in obj:
                    obj[k] = v
            # convert banned word lists to sets where necessary
            # (we'll keep them as lists in JSON but manage as sets at runtime)
            return obj
    except Exception as e:
        logger.exception("Failed to load data.json, using defaults")
        return DEFAULT_DATA.copy()


def save_data(obj: Dict[str, Any]):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write data file")


data = load_data()
os.makedirs(FILES_DIR, exist_ok=True)

# -------------------- Utilities --------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def ensure_user_registered(user, chat_id: Optional[int] = None):
    uid = str(user.id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "chats": [],
        }
        save_data(data)
    if chat_id is not None and chat_id not in data["users"][uid]["chats"]:
        data["users"][uid]["chats"].append(chat_id)
        save_data(data)


def get_chat_config(chat_id: int) -> Dict[str, Any]:
    key = str(chat_id)
    conf = data.get("chats_config", {}).get(key, {})
    # fill defaults
    return {
        "delete_links": conf.get("delete_links", GLOBAL_CONFIG["delete_links_default"]),
        "warn_limit": conf.get("warn_limit", GLOBAL_CONFIG["warn_limit_default"]),
        "ban_on_limit": conf.get("ban_on_limit", GLOBAL_CONFIG["ban_on_limit_default"]),
        "banned_words": set(conf.get("banned_words", [])) | GLOBAL_BANNED_WORDS,
    }


def set_chat_config(chat_id: int, conf: Dict[str, Any]):
    key = str(chat_id)
    data.setdefault("chats_config", {})[key] = conf
    # convert sets to lists for JSON
    if isinstance(data["chats_config"][key].get("banned_words"), set):
        data["chats_config"][key]["banned_words"] = list(data["chats_config"][key]["banned_words"])
    save_data(data)


def increment_warning(chat_id: int, user_id: int) -> int:
    ckey = str(chat_id)
    ukey = str(user_id)
    if ckey not in data["warnings"]:
        data["warnings"][ckey] = {}
    data["warnings"][ckey][ukey] = data["warnings"][ckey].get(ukey, 0) + 1
    save_data(data)
    return data["warnings"][ckey][ukey]


def reset_warnings(chat_id: int, user_id: int):
    ckey = str(chat_id)
    ukey = str(user_id)
    if ckey in data["warnings"] and ukey in data["warnings"][ckey]:
        data["warnings"][ckey][ukey] = 0
        save_data(data)


def ban_user_record(chat_id: int, user_id: int):
    ckey = str(chat_id)
    data.setdefault("banned", {}).setdefault(ckey, [])
    if user_id not in data["banned"][ckey]:
        data["banned"][ckey].append(user_id)
        save_data(data)


def is_user_banned(chat_id: int, user_id: int) -> bool:
    ckey = str(chat_id)
    return user_id in data.get("banned", {}).get(ckey, [])

# -------------------- Advanced detection helpers --------------------

def normalize_obfuscated(text: str) -> str:
    # replace common obfuscations with a dot to help detection
    t = text
    t = re.sub(r"\[\.\]|\{\.\}|\(dot\)|\sdot\s", ".", t, flags=re.IGNORECASE)
    t = re.sub(r"\[at\]|\(at\)|\sat\s", "@", t, flags=re.IGNORECASE)
    return t


def text_contains_link(text: str) -> bool:
    if not text:
        return False
    t = normalize_obfuscated(text)
    if URL_RE.search(t):
        return True
    if IP_RE.search(t):
        return True
    if SHORTENER_RE.search(t):
        return True
    # also catch things like 'example dot com' after normalization
    return False


def text_contains_banned_word(text: str, banned_words: set) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for w in banned_words:
        if w and w in lowered:
            return w
    return None

# -------------------- Command Handlers --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_registered(user)
    await update.message.reply_text("Hello! Moderation bot (webhook). Use /id to get your id.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Your user id is: {user.id}")


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data.get("files"):
        await update.message.reply_text("No files available.")
        return
    lines = ["Available files:"]
    for k, v in data["files"].items():
        lines.append(f"{k} - {v.get('path')} - {v.get('desc','')}")
    await update.message.reply_text("\n".join(lines))


async def cmd_getfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /getfile <key>")
        return
    key = args[0]
    fileinfo = data.get("files", {}).get(key)
    if not fileinfo:
        await update.message.reply_text("File not found. Use /files")
        return
    path = fileinfo.get("path")
    if not os.path.exists(path):
        await update.message.reply_text("File missing on server.")
        return
    await update.message.reply_document(open(path, "rb"))


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("You are not allowed to use this command.")
        return
    if update.message.reply_to_message and not context.args:
        # forward message to users and channels
        forward_msg = update.message.reply_to_message
        recipients = list(data.get("users", {}).keys())
        sent, failed = 0, 0
        for uid in recipients:
            try:
                await context.bot.forward_message(int(uid), update.effective_chat.id, forward_msg.message_id)
                sent += 1
            except Exception:
                failed += 1
        for cid in data.get("channels", []):
            try:
                await context.bot.forward_message(cid, update.effective_chat.id, forward_msg.message_id)
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"Broadcast: sent={sent}, failed={failed}")
        return

    text = " ".join(context.args)
    recipients = list(data.get("users", {}).keys())
    sent, failed = 0, 0
    for uid in recipients:
        try:
            await context.bot.send_message(int(uid), text)
            sent += 1
        except Exception:
            failed += 1
    for cid in data.get("channels", []):
        try:
            await context.bot.send_message(cid, text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Broadcast: sent={sent}, failed={failed}")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Only admins can use this command.")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("/kick must be used in a group/supergroup")
        return
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    elif context.args and context.args[0].startswith("@"):
        try:
            resolved = await context.bot.get_chat(context.args[0])
            target_id = resolved.id
        except Exception:
            await update.message.reply_text("Couldn't resolve username.")
            return
    if not target_id:
        await update.message.reply_text("Specify user by reply, id, or @username")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
        await update.message.reply_text("User kicked.")
    except Exception as e:
        await update.message.reply_text(f"Failed to kick: {e}")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message to warn them")
        return
    target = update.message.reply_to_message.from_user
    chat = update.effective_chat
    new = increment_warning(chat.id, target.id)
    await update.message.reply_text(f"Warned {target.full_name}. Count: {new}")
    conf = get_chat_config(chat.id)
    if new >= conf["warn_limit"] and conf["ban_on_limit"]:
        try:
            await context.bot.ban_chat_member(chat.id, target.id)
            ban_user_record(chat.id, target.id)
            await update.message.reply_text(f"{target.full_name} banned after reaching warnings.")
        except Exception as e:
            await update.message.reply_text(f"Error banning: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    chat = update.effective_chat
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target = int(context.args[0])
    if not target:
        await update.message.reply_text("Reply to a message or provide numeric user id")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target)
        ban_user_record(chat.id, target)
        await update.message.reply_text("User banned.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    target = int(context.args[0])
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target)
        await update.message.reply_text("User unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


# Admin APIs to manage files
async def cmd_addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Reply to a document with /addfile <key> [desc]")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Provide a key")
        return
    key = args[0]
    desc = " ".join(args[1:]) if len(args) > 1 else ""
    doc = update.message.reply_to_message.document
    file_obj = await doc.get_file()
    filename = os.path.join(FILES_DIR, f"{key}_{doc.file_name}")
    await file_obj.download_to_drive(filename)
    data.setdefault("files", {})[key] = {"path": filename, "desc": desc}
    save_data(data)
    await update.message.reply_text(f"Saved file key {key}")


async def cmd_rmfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    if not context.args:
        await update.message.reply_text("Usage: /rmfile <key>")
        return
    key = context.args[0]
    if key not in data.get("files", {}):
        await update.message.reply_text("Unknown key")
        return
    path = data["files"][key].get("path")
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.exception("Failed removing file from disk")
    del data["files"][key]
    save_data(data)
    await update.message.reply_text("Removed file entry")


# Admin: set chat config quickly
# Usage: /setconfig delete_links=True warn_limit=5 ban_on_limit=False add_banned=word remove_banned=word
async def cmd_setconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Use this in a group/supergroup to configure that chat")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage examples: /setconfig delete_links=False warn_limit=2 add_banned=spamword")
        return
    conf = get_chat_config(chat.id)
    for token in args:
        if token.startswith("delete_links="):
            val = token.split("=",1)[1].lower() in ("1","true","yes","on")
            conf["delete_links"] = val
        elif token.startswith("warn_limit=") and token.split("=",1)[1].isdigit():
            conf["warn_limit"] = int(token.split("=",1)[1])
        elif token.startswith("ban_on_limit="):
            val = token.split("=",1)[1].lower() in ("1","true","yes","on")
            conf["ban_on_limit"] = val
        elif token.startswith("add_banned="):
            word = token.split("=",1)[1].strip().lower()
            conf.setdefault("banned_words", set()).add(word)
        elif token.startswith("remove_banned="):
            word = token.split("=",1)[1].strip().lower()
            conf.setdefault("banned_words", set()).discard(word)
    # ensure banned_words stored as list
    if isinstance(conf.get("banned_words"), set):
        conf["banned_words"] = list(conf["banned_words"])
    set_chat_config(chat.id, conf)
    await update.message.reply_text(f"Updated config for this chat: {conf}")


# -------------------- Message Handler (moderation) --------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return

    ensure_user_registered(user, chat.id if chat else None)

    # Only moderate groups/supergroups
    if chat.type in ("group", "supergroup"):
        text = (msg.text or "") + " " + (msg.caption or "")
        conf = get_chat_config(chat.id)
        link_present = text_contains_link(text)
        banned_word = text_contains_banned_word(text, set(conf["banned_words"]))
        if conf["delete_links"] and (link_present or banned_word):
            # delete message
            try:
                await msg.delete()
                logger.info(f"Deleted message from {user.id} in {chat.id}. link_present={link_present}, banned_word={banned_word}")
            except Exception:
                logger.exception("Failed deleting message")
            new_count = increment_warning(chat.id, user.id)
            try:
                await context.bot.send_message(chat.id, f"{user.mention_html()} — your message was removed. Warning {new_count}/{conf['warn_limit']}", parse_mode="HTML")
            except Exception:
                pass
            if new_count >= conf["warn_limit"] and conf["ban_on_limit"]:
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    ban_user_record(chat.id, user.id)
                    await context.bot.send_message(chat.id, f"{user.mention_html()} has been banned after reaching warnings.", parse_mode="HTML")
                except Exception:
                    logger.exception("Error banning user")

# -------------------- Webhook control commands --------------------
async def cmd_setwebhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    if not WEBHOOK_URL:
        await update.message.reply_text("WEBHOOK_URL not configured. Set WEBHOOK_URL env var.")
        return
    # Set webhook using bot api
    bot = context.bot
    try:
        await bot.set_webhook(WEBHOOK_URL)
        await update.message.reply_text(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        await update.message.reply_text(f"Failed to set webhook: {e}")

async def cmd_removewebhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Not permitted")
        return
    try:
        await context.bot.delete_webhook()
        await update.message.reply_text("Webhook removed")
    except Exception as e:
        await update.message.reply_text(f"Failed to remove webhook: {e}")

# -------------------- Setup & Run --------------------

def build_application() -> Any:
    if TOKEN.startswith("REPLACE"):
        logger.error("Set TG_BOT_TOKEN environment variable or update TOKEN in file")
        raise SystemExit(1)
    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("getfile", cmd_getfile))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("addfile", cmd_addfile))
    app.add_handler(CommandHandler("rmfile", cmd_rmfile))
    app.add_handler(CommandHandler("setconfig", cmd_setconfig))
    app.add_handler(CommandHandler("setwebhook", cmd_setwebhook))
    app.add_handler(CommandHandler("removewhook", cmd_removewebhook))
    # Moderation message handler
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))

    return app


def run_webhook():
    app = build_application()
    listen = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    path = os.environ.get("WEBHOOK_PATH", WEBHOOK_PATH)
    # If WEBHOOK_URL provided, we will attempt to set it at startup
    logger.info(f"Starting webhook server on {listen}:{port} path={path}")
    try:
        if WEBHOOK_URL:
            # run_polling won't be used; run_webhook will bind to address and set webhook
            app.run_webhook(listen=listen, port=port, webhook_url=WEBHOOK_URL, webhook_path=path)
        else:
            # If no webhook URL, still start server on path but webhook must be set externally
            app.run_webhook(listen=listen, port=port, webhook_path=path)
    except Exception:
        logger.exception("Error running webhook")


if __name__ == "__main__":
    run_webhook()
