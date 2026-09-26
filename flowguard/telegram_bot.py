"""
flowguard/telegram_bot.py
─────────────────────────
Telegram bot client for FlowGuard. A thin layer over the existing FastAPI
backend — it does not touch scoring/pipeline logic directly, it only calls:

    POST /pipeline       — free text  → decisions / ingest / filter
    POST /upload/csv      — CSV/XLSX file
    POST /upload/pdf      — PDF file
    POST /upload/image    — photo / scanned bill
    GET  /profile         — fetch tenant profile
    POST /profile         — save tenant profile

Each Telegram chat_id is passed to the backend as `user_id`, so every
chat's obligations/cash/transactions stay isolated (see database.py's
user_id scoping).

SETUP (local dev — polling mode)
  pip install python-telegram-bot httpx python-dotenv
  Set in .env:
    TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    FLOWGUARD_API_URL=http://localhost:8000   (optional, this is the default)

  Run the FastAPI server first:
    uvicorn flowguard.main:app --host 0.0.0.0 --port 8000 --reload

  Then run this bot (polling mode):
    python -m flowguard.telegram_bot

PRODUCTION (webhook mode)
  main.py imports get_application()/process_update() from this module and
  registers a POST /telegram/webhook route directly on the FastAPI app, so
  one process serves both the API and the bot — no separate polling loop.
  See main.py's lifespan handler for webhook registration.

COMMANDS
  /start, /help   → usage instructions
  /profile        → show the stored business profile for this chat
  <any text>      → forwarded to /pipeline
  <photo/file>    → routed to /upload/csv, /upload/pdf or /upload/image
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

# Load .env from the project root (one level up from this package)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
FLOWGUARD_API_URL = os.getenv("FLOWGUARD_API_URL", "http://localhost:8000").rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

TELEGRAM_MAX_LEN = 4096

ONBOARDING_FIELDS = [
    ("business_name", "What's your business called?"),
    ("industry", "What industry are you in? (e.g. retail, manufacturing, trading, services)"),
    ("full_name", "And your name?"),
]

HELP_TEXT = (
    "*FlowGuard* — your cash flow priority engine\n\n"
    "Tell me your bills and cash balance in plain text, e.g.:\n"
    "_gst 20k friday, rent 25k monday, cash 1 lakh_\n\n"
    "Or send me a *photo of a bill*, a *PDF invoice*, or a *CSV/Excel* "
    "obligations sheet — I'll extract and record them automatically.\n\n"
    "Commands:\n"
    "• /profile — show your saved business profile\n"
    "• /help — show this message\n\n"
    "Each Telegram chat has its own private ledger — nothing is shared "
    "between users."
)


# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

def _user_id(update: Update) -> str:
    """Map a Telegram chat to a FlowGuard tenant id."""
    return f"tg{update.effective_chat.id}"


async def _post_pipeline(client: httpx.AsyncClient, user_id: str, text: str) -> dict:
    resp = await client.post(
        f"{FLOWGUARD_API_URL}/pipeline",
        params={"user_id": user_id},
        json={"raw_text": text, "language": "en"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _post_upload(client: httpx.AsyncClient, user_id: str, endpoint: str,
                       filename: str, content: bytes) -> dict:
    resp = await client.post(
        f"{FLOWGUARD_API_URL}/upload/{endpoint}",
        params={"user_id": user_id},
        files={"file": (filename, content)},
        timeout=90.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _get_profile(client: httpx.AsyncClient, user_id: str) -> dict:
    resp = await client.get(
        f"{FLOWGUARD_API_URL}/profile", params={"user_id": user_id}, timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()


async def _save_profile(client: httpx.AsyncClient, user_id: str, data: dict) -> None:
    resp = await client.post(
        f"{FLOWGUARD_API_URL}/profile", params={"user_id": user_id}, json=data, timeout=30.0
    )
    resp.raise_for_status()


# ─────────────────────────────────────────────
# Onboarding — collect basic business details before first use
# ─────────────────────────────────────────────

async def _needs_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if this chat has no saved profile yet (first-time user)."""
    if context.chat_data.get("onboarded"):
        return False
    user_id = _user_id(update)
    async with httpx.AsyncClient() as client:
        try:
            profile = await _get_profile(client, user_id)
        except httpx.HTTPError:
            profile = {}
    if profile.get("business_name"):
        context.chat_data["onboarded"] = True
        return False
    return True


async def _start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["onboard_step"] = 0
    context.chat_data["onboard_answers"] = {}
    _, question = ONBOARDING_FIELDS[0]
    await update.message.reply_text(
        f"Welcome to FlowGuard! Quick setup first — {len(ONBOARDING_FIELDS)} questions.\n\n{question}"
    )


async def _continue_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume a text message as the next onboarding answer. Returns True if handled."""
    step = context.chat_data.get("onboard_step")
    if step is None:
        return False

    field, _ = ONBOARDING_FIELDS[step]
    answer = (update.message.text or "").strip()
    if not answer:
        await update.message.reply_text("Please send a short text answer.")
        return True

    context.chat_data.setdefault("onboard_answers", {})[field] = answer
    step += 1

    if step < len(ONBOARDING_FIELDS):
        context.chat_data["onboard_step"] = step
        _, question = ONBOARDING_FIELDS[step]
        await update.message.reply_text(question)
        return True

    # Last question answered — save profile and hand off to normal flow
    user_id = _user_id(update)
    answers = context.chat_data["onboard_answers"]
    async with httpx.AsyncClient() as client:
        try:
            await _save_profile(client, user_id, answers)
        except httpx.HTTPError as e:
            logger.warning("Profile save failed for %s: %s", user_id, e)

    context.chat_data["onboard_step"] = None
    context.chat_data["onboarded"] = True
    await update.message.reply_text(
        f"Got it, {answers.get('full_name') or 'there'} — you're all set!\n\n"
        "Now tell me your bills and cash balance (e.g. _gst 20k friday, rent "
        "25k monday, cash 1 lakh_), or send a bill photo/PDF/CSV.",
        parse_mode="Markdown",
    )
    return True


async def _gate_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          for_file: bool = False) -> bool:
    """
    Onboarding gate shared by text/photo/document handlers.
    Returns True if the update was consumed by onboarding (caller should stop).
    """
    if context.chat_data.get("onboard_step") is not None:
        if for_file:
            await update.message.reply_text(
                "Let's finish quick setup first — please answer in text."
            )
            return True
        return await _continue_onboarding(update, context)

    if await _needs_onboarding(update, context):
        await _start_onboarding(update, context)
        return True

    return False


# ─────────────────────────────────────────────
# Response formatting — JSON → readable Telegram text
# ─────────────────────────────────────────────

def _truncate(text: str, limit: int = TELEGRAM_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n… (truncated)"


def _format_pipeline_response(resp: dict) -> str:
    intent = resp.get("intent", "STATUS")

    if intent == "INGEST":
        stored = resp.get("stored", [])
        lines = [resp.get("bot_reply") or "Recorded."]
        for ob in stored:
            tag = "🆕" if ob.get("new") else "🔁"
            lines.append(f"{tag} {ob['counterparty']} — ₹{ob['amount']:,.0f}")
        if resp.get("cash_recorded_inr"):
            lines.append(f"💰 Cash balance updated: ₹{resp['cash_recorded_inr']:,.0f}")
        return "\n".join(lines)

    if intent == "FILTER":
        lines = [resp.get("bot_reply") or "Here's what I found:"]
        txns = resp.get("transactions", [])
        obs = resp.get("obligations", [])
        for t in txns[:15]:
            arrow = "⬅️ IN " if t.get("direction") == "IN" else "➡️ OUT"
            lines.append(
                f"{arrow} ₹{t['amount_inr']:,.0f} — {t.get('counterparty') or '—'} "
                f"({t.get('txn_date')})"
            )
        for o in obs[:15]:
            lines.append(
                f"📌 {o['counterparty_name']} — ₹{o['amount_inr']:,.0f} "
                f"due {o.get('due_date')}"
            )
        if resp.get("total_found", 0) == 0:
            lines.append("No matching records.")
        return "\n".join(lines)

    # STATUS — full narrated result already produced by /pipeline
    narrative = resp.get("narrative") or resp.get("whatsapp_preview")
    if narrative:
        return narrative
    note = resp.get("note")
    if note:
        return f"{resp.get('bot_reply', '')}\n{note}".strip()
    return resp.get("bot_reply") or "No response from the engine."


def _format_upload_result(result: dict) -> str:
    if not result.get("success"):
        return f"⚠️ Import failed: {result.get('error') or 'unknown error'}"

    if result.get("skipped_duplicate"):
        return f"↩️ {result['filename']}: already imported earlier."

    lines = [
        f"✅ *{result['filename']}* processed "
        f"({result.get('file_type', 'FILE')})",
        f"Obligations found: {result.get('obligations_found', 0)} "
        f"(new: {result.get('obligations_new', 0)}, "
        f"updated: {result.get('obligations_updated', 0)})",
    ]
    if result.get("transactions_found"):
        lines.append(f"Transactions found: {result['transactions_found']}")
    for ob in result.get("obligations", [])[:15]:
        lines.append(
            f"• {ob.get('counterparty_name', '?')} — "
            f"₹{ob.get('amount_inr', 0):,.0f} due {ob.get('due_date')}"
        )
    errors = result.get("validation_errors") or []
    if errors:
        lines.append(f"\n⚠️ {len(errors)} row(s) could not be validated.")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _needs_onboarding(update, context):
        await _start_onboarding(update, context)
        return
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    async with httpx.AsyncClient() as client:
        try:
            profile = await _get_profile(client, user_id)
        except httpx.HTTPError as e:
            await update.message.reply_text(f"Could not fetch profile: {e}")
            return
    if not profile:
        await update.message.reply_text(
            "No profile saved yet. Use the FlowGuard web UI (or POST /profile) "
            f"with user_id={user_id} to set one up."
        )
        return
    lines = [f"*{k.replace('_', ' ').title()}*: {v}" for k, v in profile.items() if v]
    await update.message.reply_markdown("\n".join(lines) or "Profile is empty.")


# ─────────────────────────────────────────────
# Message handlers
# ─────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if not text or not text.strip():
        return
    if await _gate_onboarding(update, context):
        return
    user_id = _user_id(update)
    await update.message.chat.send_action(ChatAction.TYPING)

    async with httpx.AsyncClient() as client:
        try:
            resp = await _post_pipeline(client, user_id, text)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", e.response.text) if e.response.content else str(e)
            await update.message.reply_text(f"⚠️ {detail}")
            return
        except httpx.HTTPError as e:
            logger.exception("pipeline call failed")
            await update.message.reply_text(f"⚠️ Couldn't reach FlowGuard backend: {e}")
            return

    reply = _format_pipeline_response(resp)
    await update.message.reply_text(_truncate(reply))


async def _handle_file(update: Update, user_id: str, filename: str, content: bytes) -> None:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "xlsx", "xls"):
        endpoint = "csv"
    elif ext == "pdf":
        endpoint = "pdf"
    elif ext in ("jpg", "jpeg", "png", "webp", "bmp", "tiff"):
        endpoint = "image"
    else:
        await update.message.reply_text(
            f"Unsupported file type '.{ext}'. Send a CSV/XLSX, PDF, or image (jpg/png/webp)."
        )
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    async with httpx.AsyncClient() as client:
        try:
            result = await _post_upload(client, user_id, endpoint, filename, content)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", e.response.text) if e.response.content else str(e)
            await update.message.reply_text(f"⚠️ Upload failed: {detail}")
            return
        except httpx.HTTPError as e:
            logger.exception("upload call failed")
            await update.message.reply_text(f"⚠️ Couldn't reach FlowGuard backend: {e}")
            return

    await update.message.reply_markdown(_truncate(_format_upload_result(result)))


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _gate_onboarding(update, context, for_file=True):
        return
    user_id = _user_id(update)
    photo = update.message.photo[-1]  # highest resolution
    tg_file = await context.bot.get_file(photo.file_id)
    content = bytes(await tg_file.download_as_bytearray())
    await _handle_file(update, user_id, f"{photo.file_unique_id}.jpg", content)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _gate_onboarding(update, context, for_file=True):
        return
    user_id = _user_id(update)
    doc = update.message.document
    tg_file = await context.bot.get_file(doc.file_id)
    content = bytes(await tg_file.download_as_bytearray())
    await _handle_file(update, user_id, doc.file_name or "upload.bin", content)


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN not set. Add it to .env (get one from @BotFather)."
        )
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start"], cmd_start))
    app.add_handler(CommandHandler(["help"], cmd_help))
    app.add_handler(CommandHandler(["profile"], cmd_profile))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    app = build_app()
    logger.info("FlowGuard Telegram bot starting (API: %s)", FLOWGUARD_API_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


# ─────────────────────────────────────────────
# WEBHOOK MODE — used by main.py in production (no polling loop)
# ─────────────────────────────────────────────

_application: Optional[Application] = None


def get_application() -> Application:
    """Singleton PTB Application, built lazily so importing this module
    without TELEGRAM_BOT_TOKEN set (e.g. running main.py alone) doesn't fail."""
    global _application
    if _application is None:
        _application = build_app()
    return _application


async def start_webhook(public_url: str) -> None:
    """Initialize the PTB application and register the Telegram webhook.
    Call once from main.py's FastAPI lifespan startup."""
    application = get_application()
    await application.initialize()
    await application.start()
    webhook_url = f"{public_url.rstrip('/')}/telegram/webhook"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=TELEGRAM_WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Telegram webhook registered at %s", webhook_url)


async def stop_webhook() -> None:
    """Cleanly stop the PTB application. Call from main.py's shutdown."""
    if _application is not None:
        await _application.bot.delete_webhook()
        await _application.stop()
        await _application.shutdown()


async def process_update(update_data: dict) -> None:
    """Feed one raw Telegram update (already-parsed JSON body) into PTB."""
    application = get_application()
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)


if __name__ == "__main__":
    main()
