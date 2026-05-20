import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ollama
from app import storage, worker

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
POLL_INTERVAL = 30

_STATUS_ICONS = {
    "done": "✅",
    "running": "🔄",
    "error": "❌",
    "pending": "⏳",
    "cancelled": "🚫",
    "cancelling": "⏹",
}

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": (
                "Get overall download statistics: total number of jobs "
                "and count per status (pending, running, done, error, cancelled)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_jobs",
            "description": (
                "Get the most recent download jobs with their ID, status, "
                "creation time, update time, result directory, and error message if any."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of jobs to return (1-20, default 5).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_detail",
            "description": (
                "Get full details of a specific job by its ID, including "
                "all parameters, output directory, and error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID (12-character hex string).",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_log",
            "description": (
                "Get the execution log of a specific job. Useful for diagnosing "
                "failures or checking progress. Returns the last part of the log."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID.",
                    }
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_settings",
            "description": (
                "Get current system settings: project_id, save_path, scale_m, "
                "max_cloud, max_workers, win_main, win_fill, dates_dir."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_accounts",
            "description": "Get list of configured Google Earth Engine accounts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_SYSTEM_PROMPT = (
    "You are a helpful assistant for the Sentinel Hub Satellite Image Downloader system. "
    "You help users check the status of satellite image downloads, view job statistics, "
    "diagnose failures by reading logs, and understand system settings. "
    "Use the available tools to fetch real-time data. "
    "Be concise and clear. Format responses for Telegram using plain text. "
    "Do not use markdown unless it clearly helps readability."
)

def _execute_tool(name: str, args: dict) -> dict:
    if name == "get_stats":
        jobs = storage.list_jobs()
        by_status: dict = {}
        for j in jobs:
            s = j["status"]
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": len(jobs), "by_status": by_status}

    if name == "get_recent_jobs":
        limit = min(int(args.get("limit", 5)), 20)
        jobs = storage.list_jobs(limit=limit)
        return [
            {
                "id": j["id"],
                "status": j["status"],
                "created_at": j["created_at"],
                "updated_at": j["updated_at"],
                "result_dir": j.get("result_dir"),
                "error": j.get("error"),
            }
            for j in jobs
        ]

    if name == "get_job_detail":
        job = storage.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "Job not found"}
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "params": job.get("params", {}),
            "result_dir": job.get("result_dir"),
            "error": job.get("error"),
        }

    if name == "get_job_log":
        job = storage.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "Job not found"}
        log = job.get("log", "") or ""
        return {"job_id": job["id"], "status": job["status"], "log": log[-3000:]}

    if name == "get_settings":
        return storage.get_settings()

    if name == "get_accounts":
        accounts = storage.list_accounts()
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "type": a["type"],
                "project_id": a["project_id"],
                "is_default": a["is_default"],
            }
            for a in accounts
        ]

    return {"error": f"Unknown tool: {name}"}

def _ask_ollama_sync(user_text: str) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    for _ in range(8):
        resp = ollama.chat(model=OLLAMA_MODEL, messages=messages, tools=_TOOLS)
        msg = resp.message
        if not msg.tool_calls:
            return msg.content or "No response."
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": msg.tool_calls,
        })
        for tc in msg.tool_calls:
            result = _execute_tool(tc.function.name, dict(tc.function.arguments))
            messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
    return "Could not complete the request after multiple steps."

def _format_job_line(job: dict) -> str:
    icon = _STATUS_ICONS.get(job["status"], "•")
    date = job["created_at"][:10]
    params = job.get("params", {})
    product = params.get("product", "") if isinstance(params, dict) else ""
    extra = f" [{product}]" if product else ""
    return f"{icon} {job['id']}{extra} — {date}"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage.add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "Hi! I'm the Sentinel Hub Bot.\n\n"
        "Commands:\n"
        "/status — quick stats overview\n"
        "/jobs — list recent jobs\n"
        "/logs <id> — view job log\n"
        "/cancel <id> — cancel a running job\n"
        "/subscribe — enable finish notifications\n"
        "/unsubscribe — disable notifications\n\n"
        "Or just ask me anything in plain language:\n"
        "• How many downloads failed?\n"
        "• Why did the last job fail?\n"
        "• What are the current settings?"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = storage.list_jobs()
    by_status: dict = {}
    for j in jobs:
        s = j["status"]
        by_status[s] = by_status.get(s, 0) + 1
    lines = [f"Total jobs: {len(jobs)}"]
    for s, count in sorted(by_status.items()):
        icon = _STATUS_ICONS.get(s, "•")
        lines.append(f"{icon} {s}: {count}")
    await update.message.reply_text("\n".join(lines))

async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = storage.list_jobs(limit=10)
    if not jobs:
        await update.message.reply_text("No jobs found.")
        return
    lines = ["Recent jobs:\n"]
    keyboard = []
    for j in jobs:
        lines.append(_format_job_line(j))
        keyboard.append([
            InlineKeyboardButton("Details", callback_data=f"detail:{j['id']}"),
            InlineKeyboardButton("Logs", callback_data=f"logs:{j['id']}"),
        ])
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("\n".join(lines), reply_markup=markup)

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /logs <job_id>")
        return
    job_id = context.args[0]
    job = storage.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job {job_id} not found.")
        return
    log = (job.get("log") or "").strip()
    if not log:
        await update.message.reply_text(f"No logs yet for job {job_id}.")
        return
    excerpt = log[-3500:]
    if len(log) > 3500:
        excerpt = "...(truncated)\n" + excerpt
    await update.message.reply_text(f"Log for {job_id} [{job['status']}]:\n\n{excerpt}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    job_id = context.args[0]
    job = storage.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job {job_id} not found.")
        return
    if job["status"] not in ("pending", "running"):
        await update.message.reply_text(
            f"Job {job_id} is already {job['status']}, cannot cancel."
        )
        return
    worker.cancel(job_id)
    storage.update_job(job_id, status="cancelling")
    await update.message.reply_text(f"⏹ Cancelling job {job_id}...")

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage.add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Subscribed! You'll get a message when any job finishes or fails."
    )

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage.remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🚫 Unsubscribed from notifications.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, job_id = query.data.split(":", 1)

    if action == "detail":
        job = storage.get_job(job_id)
        if not job:
            await query.message.reply_text(f"Job {job_id} not found.")
            return
        params = job.get("params", {})
        lines = [
            f"Job: {job['id']}",
            f"Status: {_STATUS_ICONS.get(job['status'], '')} {job['status']}",
            f"Created: {job['created_at'][:19]}",
            f"Updated: {job['updated_at'][:19]}",
        ]
        if isinstance(params, dict):
            for key in ("product", "date", "date_start", "date_end", "scale_m", "max_cloud"):
                if params.get(key):
                    lines.append(f"{key}: {params[key]}")
        if job.get("result_dir"):
            lines.append(f"Output: {job['result_dir']}")
        if job.get("error"):
            lines.append(f"Error: {job['error'][:300]}")
        await query.message.reply_text("\n".join(lines))

    elif action == "logs":
        job = storage.get_job(job_id)
        if not job:
            await query.message.reply_text(f"Job {job_id} not found.")
            return
        log = (job.get("log") or "").strip()
        if not log:
            await query.message.reply_text(f"No logs for {job_id}.")
            return
        excerpt = log[-3500:]
        if len(log) > 3500:
            excerpt = "...(truncated)\n" + excerpt
        await query.message.reply_text(f"Log [{job['status']}]:\n\n{excerpt}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    try:
        answer = await asyncio.to_thread(_ask_ollama_sync, update.message.text)
        await update.message.reply_text(answer)
    except Exception as exc:
        logger.error("Bot error: %s", exc)
        await update.message.reply_text(f"Error: {exc}")

async def notification_loop(bot):
    known: dict[str, str] = {}
    for job in storage.list_jobs():
        known[job["id"]] = job["status"]

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            jobs = storage.list_jobs()
            subscribers = storage.get_subscribers()
            for job in jobs:
                jid = job["id"]
                new_status = job["status"]
                old_status = known.get(jid)
                if (
                    old_status is not None
                    and old_status != new_status
                    and new_status in ("done", "error", "cancelled")
                ):
                    icon = _STATUS_ICONS.get(new_status, "•")
                    params = job.get("params", {})
                    lines = [f"{icon} Job {jid} finished: {new_status}"]
                    if isinstance(params, dict):
                        if params.get("product"):
                            lines.append(f"Product: {params['product']}")
                        date = params.get("date") or params.get("date_start", "")
                        if date:
                            lines.append(f"Date: {date}")
                    if new_status == "error" and job.get("error"):
                        lines.append(f"Error: {job['error'][:200]}")
                    if new_status == "done" and job.get("result_dir"):
                        lines.append(f"Output: {job['result_dir']}")
                    msg = "\n".join(lines)
                    for chat_id in subscribers:
                        try:
                            await bot.send_message(chat_id, msg)
                        except Exception as exc:
                            logger.warning("Failed to notify %s: %s", chat_id, exc)
                known[jid] = new_status
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Notification poller error: %s", exc)

def build_bot() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("jobs", cmd_jobs))
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application
