import asyncio
import hashlib
import html
import json
import logging
import os
import re
import signal
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import uvicorn
from fastapi import FastAPI
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("livegram-clone-bot")

TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")

# --- Dummy FastAPI Server ---
app = FastAPI(title="Livegram Clone Bot Health")

@app.get("/")
async def health_check():
    """Dummy endpoint to keep the web service alive via pinging."""
    return {"status": "online", "message": "Bot is running"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_token(token: str) -> str:
    if ":" not in token:
        return "***"
    bot_id, secret = token.split(":", 1)
    return f"{bot_id}:{secret[:4]}...{secret[-4:]}"


def parse_id_list(raw_value: str) -> List[int]:
    ids: List[int] = []
    for item in raw_value.replace(" ", "").split(","):
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            logger.warning("Ignoring invalid ADMIN_IDS entry: %s", item)
    return ids


def parse_bool(raw_value: str, default: bool = True) -> bool:
    if raw_value == "":
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def parse_chat_id(raw_value: str) -> Optional[int]:
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("FORWARD_CHAT_ID must be numeric; got %s", raw_value)
        return None


@dataclass
class CloneRecord:
    fingerprint: str
    token: str
    owner_id: int
    owner_username: Optional[str]
    bot_id: int
    bot_username: str
    bot_name: str
    created_at: str


class ManagedApplication:
    def __init__(self, application: Application, label: str):
        self.application = application
        self.label = label
        self.started = False

    async def start(self) -> None:
        await self.application.initialize()
        await self.application.start()
        if self.application.updater is None:
            raise RuntimeError("Application updater is not available")
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        self.started = True
        logger.info("Started %s", self.label)

    async def stop(self) -> None:
        if not self.started:
            return
        try:
            if self.application.updater is not None:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Stopped %s", self.label)
        finally:
            self.started = False


class CloneManager:
    def __init__(self, clones_file: Path, factory_token: str, enabled: bool):
        self.clones_file = clones_file
        self.factory_fingerprint = token_fingerprint(factory_token)
        self.enabled = enabled
        self.records: Dict[str, CloneRecord] = {}
        self.runners: Dict[str, ManagedApplication] = {}
        self._load_records()

    def _load_records(self) -> None:
        if not self.clones_file.exists():
            return

        try:
            raw_records = json.loads(self.clones_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read clone store %s: %s", self.clones_file, exc)
            return

        for raw_record in raw_records:
            try:
                record = CloneRecord(**raw_record)
            except TypeError as exc:
                logger.warning("Skipping invalid clone record: %s", exc)
                continue
            self.records[record.fingerprint] = record

    def _save_records(self) -> None:
        self.clones_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(record) for record in self.records.values()]
        self.clones_file.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    async def start_existing_clones(self) -> None:
        if not self.enabled:
            logger.info("Clone loading is disabled by ENABLE_CLONING=false")
            return

        for record in list(self.records.values()):
            try:
                await self._start_record(record)
            except Exception as exc:
                logger.warning(
                    "Could not start saved clone @%s: %s",
                    record.bot_username,
                    exc,
                )

    async def create_clone(self, token: str, owner_user) -> CloneRecord:
        if not self.enabled:
            raise ValueError("Cloning is disabled on this deployment.")

        token = token.strip()
        if not TOKEN_PATTERN.match(token):
            raise ValueError("That does not look like a Telegram bot token.")

        fingerprint = token_fingerprint(token)
        if fingerprint == self.factory_fingerprint:
            raise ValueError("Use a different bot token, not the factory bot token.")

        existing = self.records.get(fingerprint)
        if existing is not None:
            if existing.owner_id == owner_user.id:
                if fingerprint not in self.runners:
                    await self._start_record(existing)
                return existing
            raise ValueError("That bot token is already cloned by another user.")

        async with Bot(token=token) as probe_bot:
            me = await probe_bot.get_me()

        if not me.username:
            raise ValueError("Telegram did not return a username for that bot.")

        record = CloneRecord(
            fingerprint=fingerprint,
            token=token,
            owner_id=owner_user.id,
            owner_username=owner_user.username,
            bot_id=me.id,
            bot_username=me.username,
            bot_name=me.full_name,
            created_at=utc_now(),
        )

        await self._start_record(record)
        self.records[fingerprint] = record
        self._save_records()
        logger.info("Created clone @%s for owner %s", me.username, owner_user.id)
        return record

    async def remove_clone(self, owner_id: int, identifier: str) -> CloneRecord:
        identifier = identifier.strip().lstrip("@").lower()
        if not identifier:
            raise ValueError("Please pass a clone username or clone id.")

        record = None
        for candidate in self.records.values():
            values = {
                candidate.fingerprint.lower(),
                candidate.fingerprint[:12].lower(),
                str(candidate.bot_id),
                candidate.bot_username.lower(),
            }
            if identifier in values:
                record = candidate
                break

        if record is None or record.owner_id != owner_id:
            raise ValueError("I could not find one of your clones with that id.")

        runner = self.runners.pop(record.fingerprint, None)
        if runner is not None:
            await runner.stop()

        self.records.pop(record.fingerprint, None)
        self._save_records()
        logger.info("Removed clone @%s for owner %s", record.bot_username, owner_id)
        return record

    def records_for_owner(self, owner_id: int) -> List[CloneRecord]:
        return [
            record
            for record in self.records.values()
            if record.owner_id == owner_id
        ]

    async def stop_all(self) -> None:
        for runner in list(self.runners.values()):
            await runner.stop()
        self.runners.clear()

    async def _start_record(self, record: CloneRecord) -> None:
        if record.fingerprint in self.runners:
            return

        clone_bot = LivegramBot(
            token=record.token,
            admin_ids=[record.owner_id],
            forward_chat_id=record.owner_id,
            manager=None,
            label=f"clone @{record.bot_username}",
        )
        runner = ManagedApplication(clone_bot.application, f"clone @{record.bot_username}")
        await runner.start()
        self.runners[record.fingerprint] = runner


class LivegramBot:
    def __init__(
        self,
        token: str,
        admin_ids: Iterable[int],
        forward_chat_id: Optional[int],
        manager: Optional[CloneManager],
        label: str,
    ):
        self.token = token
        self.admin_ids = set(admin_ids)
        self.forward_chat_id = forward_chat_id
        self.manager = manager
        self.label = label
        self.started_at = datetime.now(timezone.utc)
        self.user_data: Dict[int, Dict[str, object]] = {}
        self.application = Application.builder().token(token).build()
        self.setup_handlers()

    @property
    def is_factory(self) -> bool:
        return self.manager is not None

    def setup_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("broadcast", self.broadcast_command))

        if self.is_factory:
            self.application.add_handler(CommandHandler("clone", self.clone_command))
            self.application.add_handler(CommandHandler("myclones", self.myclones_command))
            self.application.add_handler(CommandHandler("removeclone", self.removeclone_command))

        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.handle_media))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_error_handler(self.error_handler)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return

        self._remember_user(user)
        if self.is_factory:
            text = (
                f"Welcome {html.escape(user.first_name or 'there')}.\n\n"
                "Send me a message to contact the admin team.\n"
                "To create your own Livegram clone, send:\n"
                "/clone YOUR_BOT_TOKEN\n\n"
                "Create a bot token with @BotFather first, then send it here in private chat."
            )
        elif user.id in self.admin_ids:
            text = (
                "Your clone admin inbox is connected.\n\n"
                "When users message this bot, their messages will be forwarded here. "
                "Use the Reply button on a forwarded message, then send your reply."
            )
        else:
            text = (
                f"Welcome {html.escape(user.first_name or 'there')}.\n\n"
                "Send a message and I will forward it to the admin."
            )

        keyboard = [
            [
                InlineKeyboardButton("Help", callback_data="help"),
                InlineKeyboardButton("Status", callback_data="status"),
            ]
        ]
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = [
            "Livegram Bot Help",
            "",
            "/start - Start the bot",
            "/help - Show this help",
            "/status - Check bot status",
            "/stats - Admin statistics",
            "/broadcast MESSAGE - Admin broadcast",
            "",
            "Send any normal message and it will be forwarded to the admin.",
        ]
        if self.is_factory:
            lines.extend(
                [
                    "",
                    "Clone commands:",
                    "/clone YOUR_BOT_TOKEN - Start your own clone",
                    "/myclones - List your clones",
                    "/removeclone USERNAME_OR_ID - Stop and remove a clone",
                ]
            )
        await self._reply(update, "\n".join(lines))

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        total_messages = sum(int(user.get("message_count", 0)) for user in self.user_data.values())
        clone_count = len(self.manager.records) if self.manager is not None else 0
        lines = [
            "Bot Status",
            "",
            f"Users seen since restart: {len(self.user_data)}",
            f"Messages since restart: {total_messages}",
            f"Uptime: {self.get_uptime()}",
            "Active: online",
        ]
        if self.is_factory:
            lines.append(f"Running clones: {clone_count}")
        await self._reply(update, "\n".join(lines))

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.admin_ids:
            await self._reply(update, "Admin access required.")
            return

        stats_text = (
            "Admin Statistics\n\n"
            f"Total users since restart: {len(self.user_data)}\n"
            f"Active today: {self.get_active_today()}\n"
            f"Messages today estimate: {self.get_messages_today()}"
        )
        await self._reply(update, stats_text)

    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.admin_ids:
            await self._reply(update, "Admin access required.")
            return

        if not context.args:
            await self._reply(update, "Usage: /broadcast <message>")
            return

        message = " ".join(context.args)
        success_count = 0
        for user_id in list(self.user_data.keys()):
            try:
                await context.bot.send_message(user_id, f"Broadcast:\n\n{message}")
                success_count += 1
                await asyncio.sleep(0.1)
            except TelegramError as exc:
                logger.warning("Broadcast to %s failed: %s", user_id, exc)

        await self._reply(update, f"Broadcast sent to {success_count} users.")

    async def clone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.manager is None or update.message is None or update.effective_user is None:
            return

        if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
            await update.message.reply_text("For safety, send /clone only in private chat with me.")
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /clone YOUR_BOT_TOKEN\n\n"
                "Create a token with @BotFather, then paste it after /clone."
            )
            return

        token = context.args[0].strip()
        try:
            record = await self.manager.create_clone(token, update.effective_user)
        except Exception as exc:
            logger.info(
                "Clone request failed for user %s using token %s: %s",
                update.effective_user.id,
                mask_token(token),
                exc,
            )
            await update.message.reply_text(f"Clone failed: {exc}")
            return

        await update.message.reply_text(
            f"Clone is running as @{record.bot_username}.\n\n"
            "Important: open your new bot and press /start so it can send your admin inbox messages.\n"
            "Users can now message your clone bot."
        )

    async def myclones_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.manager is None or update.effective_user is None:
            return

        records = self.manager.records_for_owner(update.effective_user.id)
        if not records:
            await self._reply(update, "You do not have any clones yet. Use /clone YOUR_BOT_TOKEN.")
            return

        lines = ["Your clones:", ""]
        for record in records:
            state = "running" if record.fingerprint in self.manager.runners else "saved"
            lines.append(f"@{record.bot_username} - {state} - id {record.fingerprint[:12]}")
        await self._reply(update, "\n".join(lines))

    async def removeclone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.manager is None or update.effective_user is None:
            return

        if not context.args:
            await self._reply(update, "Usage: /removeclone USERNAME_OR_ID")
            return

        try:
            record = await self.manager.remove_clone(update.effective_user.id, context.args[0])
        except Exception as exc:
            await self._reply(update, f"Remove failed: {exc}")
            return

        await self._reply(update, f"Removed clone @{record.bot_username}.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None or message.text is None:
            return

        self._remember_user(user)
        self.user_data[user.id]["message_count"] = int(self.user_data[user.id].get("message_count", 0)) + 1
        self.user_data[user.id]["last_active"] = datetime.now(timezone.utc)

        replying_to = context.user_data.pop("replying_to", None)
        if replying_to is not None and user.id in self.admin_ids:
            sent = await self._send_admin_reply(context, int(replying_to), message.text)
            if sent:
                await message.reply_text("Reply sent.")
            else:
                await message.reply_text("Reply failed. The user may have blocked this bot.")
            return

        if user.id in self.admin_ids and not self.is_factory:
            await message.reply_text("Use the Reply button on a forwarded user message before sending a reply.")
            return

        auto_response = self._auto_response(message.text)
        if auto_response is not None:
            await message.reply_text(auto_response)
            return

        forwarded = await self._forward_user_message(context, user, message.text, media_label=None)
        if forwarded:
            await message.reply_text("Your message has been forwarded to the admin.")
        else:
            await message.reply_text("The admin inbox is not configured yet. Please try again later.")

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if user is None or message is None:
            return

        self._remember_user(user)
        caption = message.caption or ""
        media_label = "photo" if message.photo else "document"
        forwarded = await self._forward_user_message(context, user, caption, media_label=media_label)
        if not forwarded:
            await message.reply_text("The admin inbox is not configured yet.")
            return

        try:
            if message.photo:
                await context.bot.send_photo(
                    self.forward_chat_id,
                    message.photo[-1].file_id,
                    caption="Original photo",
                )
            elif message.document:
                await context.bot.send_document(
                    self.forward_chat_id,
                    message.document.file_id,
                    caption="Original document",
                )
        except TelegramError as exc:
            logger.warning("Media relay failed in %s: %s", self.label, exc)

        await message.reply_text("Your media has been forwarded.")

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        await query.answer()
        data = query.data or ""

        if data.startswith("reply:"):
            user = update.effective_user
            if user is None or user.id not in self.admin_ids:
                await query.edit_message_text("Admin access required.")
                return
            target_user_id = int(data.split(":", 1)[1])
            context.user_data["replying_to"] = target_user_id
            await query.edit_message_text(f"Replying to user {target_user_id}. Send your reply message now.")
            return

        if data == "ignore":
            await query.edit_message_text("Message ignored.")
            return

        if data == "help":
            await query.edit_message_text(self._help_text_for_callback())
            return

        if data == "status":
            total_messages = sum(int(user.get("message_count", 0)) for user in self.user_data.values())
            await query.edit_message_text(
                "Bot Status\n\n"
                f"Users seen since restart: {len(self.user_data)}\n"
                f"Messages since restart: {total_messages}\n"
                f"Uptime: {self.get_uptime()}\n"
                "Active: online"
            )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling update in %s", self.label, exc_info=context.error)

    async def _reply(self, update: Update, text: str) -> None:
        if update.message is not None:
            await update.message.reply_text(text)
        elif update.callback_query is not None:
            await update.callback_query.edit_message_text(text)

    def _remember_user(self, user) -> None:
        if user.id not in self.user_data:
            self.user_data[user.id] = {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "join_date": datetime.now(timezone.utc),
                "message_count": 0,
            }

    def _auto_response(self, text: str) -> Optional[str]:
        auto_responses = {
            "hello": "Hello! How can I help you?",
            "help": "Use /help to see available commands.",
            "thanks": "You are welcome!",
        }
        text_lower = text.lower()
        for keyword, response in auto_responses.items():
            if keyword in text_lower:
                return response
        return None

    async def _forward_user_message(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user,
        text: str,
        media_label: Optional[str],
    ) -> bool:
        if self.forward_chat_id is None:
            return False

        message_kind = f"New {media_label} message" if media_label else "New message"
        safe_name = html.escape(user.full_name or "Unknown")
        safe_username = f"@{html.escape(user.username)}" if user.username else "none"
        safe_text = html.escape(text or "(no text)")
        forward_text = (
            f"<b>{message_kind}</b>\n\n"
            f"From: {safe_name}\n"
            f"Username: {safe_username}\n"
            f"User ID: <code>{user.id}</code>\n\n"
            f"{safe_text}"
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Reply", callback_data=f"reply:{user.id}"),
                    InlineKeyboardButton("Ignore", callback_data="ignore"),
                ]
            ]
        )

        try:
            await context.bot.send_message(
                self.forward_chat_id,
                forward_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except TelegramError as exc:
            logger.warning("Forward failed in %s: %s", self.label, exc)
            return False

    async def _send_admin_reply(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        target_user_id: int,
        text: str,
    ) -> bool:
        try:
            await context.bot.send_message(
                target_user_id,
                f"Admin reply:\n\n{text}",
            )
            return True
        except TelegramError as exc:
            logger.warning("Admin reply failed in %s: %s", self.label, exc)
            return False

    def _help_text_for_callback(self) -> str:
        lines = [
            "Livegram Bot Help",
            "",
            "/start - Start the bot",
            "/help - Show help",
            "/status - Check status",
            "",
            "Send any normal message and it will be forwarded to the admin.",
        ]
        if self.is_factory:
            lines.extend(
                [
                    "",
                    "/clone YOUR_BOT_TOKEN - Start your own clone",
                    "/myclones - List your clones",
                    "/removeclone USERNAME_OR_ID - Remove a clone",
                ]
            )
        return "\n".join(lines)

    def get_uptime(self) -> str:
        delta = datetime.now(timezone.utc) - self.started_at
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"

    def get_active_today(self) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(
            1
            for user in self.user_data.values()
            if isinstance(user.get("last_active"), datetime)
            and user["last_active"].date() == today
        )

    def get_messages_today(self) -> int:
        return sum(int(user.get("message_count", 0)) for user in self.user_data.values())


async def main() -> None:
    load_dotenv()

    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("BOT_TOKEN is required.")

    admin_ids = parse_id_list(os.environ.get("ADMIN_IDS", ""))
    forward_chat_id = parse_chat_id(os.environ.get("FORWARD_CHAT_ID", ""))
    if forward_chat_id is None and admin_ids:
        forward_chat_id = admin_ids[0]

    clones_file = Path(os.environ.get("CLONES_FILE", "clones.json"))
    cloning_enabled = parse_bool(os.environ.get("ENABLE_CLONING", "true"), default=True)
    clone_manager = CloneManager(clones_file, bot_token, enabled=cloning_enabled)

    factory_bot = LivegramBot(
        token=bot_token,
        admin_ids=admin_ids,
        forward_chat_id=forward_chat_id,
        manager=clone_manager,
        label="factory bot",
    )
    factory_runner = ManagedApplication(factory_bot.application, "factory bot")

    # Start the bots
    await factory_runner.start()
    await clone_manager.start_existing_clones()

    # Configure the FastAPI server using Uvicorn
    # This automatically picks up the PORT environment variable most platforms inject.
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        loop="asyncio"
    )
    server = uvicorn.Server(config)

    # Uvicorn will block here, handle signals gracefully, and keep the process alive
    try:
        logger.info(f"Starting FastAPI server on port {port}...")
        await server.serve()
    finally:
        # This cleanup block runs when the web server shuts down
        await clone_manager.stop_all()
        await factory_runner.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if str(exc):
            logger.info("%s", exc)
