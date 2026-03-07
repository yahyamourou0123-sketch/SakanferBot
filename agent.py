"""
AI Agent Ultimate v4 — 24/7 Telegram Bot
Features: Infinite retry, permanent memory, scheduler,
          file/image support, BOT BUILDER (creates ready Telegram bots)
"""
import os, requests, subprocess, re, time, json, logging, sqlite3, asyncio, base64, zipfile, io
from datetime import datetime, timedelta
from pathlib import Path
from duckduckgo_search import DDGS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, JobQueue, CallbackQueryHandler
)

# ─── LOGGING ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────
API_KEY   = os.environ.get("OPENROUTER_KEY",  "YOUR_KEY_HERE")
TG_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
MODEL     = os.environ.get("AI_MODEL",        "anthropic/claude-sonnet-4-5")
WORKDIR   = Path(os.environ.get("WORKDIR",    "/tmp/workspace"))
DB_PATH   = Path(os.environ.get("DB_PATH",    "/tmp/agent_memory.db"))
WORKDIR.mkdir(parents=True, exist_ok=True)

# ─── DATABASE (permanent memory) ──────────────────────────
def db_connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            uid       INTEGER NOT NULL,
            role      TEXT    NOT NULL,
            content   TEXT    NOT NULL,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            uid       INTEGER NOT NULL,
            key       TEXT    NOT NULL,
            value     TEXT    NOT NULL,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(uid, key) ON CONFLICT REPLACE
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            uid       INTEGER NOT NULL,
            task      TEXT    NOT NULL,
            interval  TEXT    NOT NULL,
            next_run  DATETIME,
            active    INTEGER DEFAULT 1,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_msg_uid ON messages(uid);
        CREATE INDEX IF NOT EXISTS idx_mem_uid ON memory(uid);
        CREATE INDEX IF NOT EXISTS idx_sch_uid ON schedules(uid);
        """)

def db_add_message(uid: int, role: str, content: str):
    with db_connect() as c:
        c.execute("INSERT INTO messages (uid,role,content) VALUES (?,?,?)",
                  (uid, role, content))
        # Keep last 60 messages per user
        c.execute("""DELETE FROM messages WHERE uid=? AND id NOT IN (
                       SELECT id FROM messages WHERE uid=? ORDER BY id DESC LIMIT 60
                   )""", (uid, uid))

def db_get_history(uid: int) -> list:
    with db_connect() as c:
        rows = c.execute(
            "SELECT role,content FROM messages WHERE uid=? ORDER BY id DESC LIMIT 40",
            (uid,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def db_clear_history(uid: int):
    with db_connect() as c:
        c.execute("DELETE FROM messages WHERE uid=?", (uid,))

def db_set_memory(uid: int, key: str, value: str):
    with db_connect() as c:
        c.execute("INSERT OR REPLACE INTO memory (uid,key,value) VALUES (?,?,?)",
                  (uid, key, value))

def db_get_memory(uid: int) -> dict:
    with db_connect() as c:
        rows = c.execute("SELECT key,value FROM memory WHERE uid=?", (uid,)).fetchall()
    return {r["key"]: r["value"] for r in rows}

def db_add_schedule(uid: int, task: str, interval: str, next_run: datetime):
    with db_connect() as c:
        c.execute(
            "INSERT INTO schedules (uid,task,interval,next_run) VALUES (?,?,?,?)",
            (uid, task, interval, next_run.isoformat())
        )

def db_get_due_schedules() -> list:
    now = datetime.utcnow().isoformat()
    with db_connect() as c:
        rows = c.execute(
            "SELECT * FROM schedules WHERE active=1 AND next_run <= ?", (now,)
        ).fetchall()
    return [dict(r) for r in rows]

def db_update_schedule_next(schedule_id: int, next_run: datetime):
    with db_connect() as c:
        c.execute("UPDATE schedules SET next_run=? WHERE id=?",
                  (next_run.isoformat(), schedule_id))

def db_list_schedules(uid: int) -> list:
    with db_connect() as c:
        rows = c.execute(
            "SELECT id,task,interval,next_run FROM schedules WHERE uid=? AND active=1",
            (uid,)
        ).fetchall()
    return [dict(r) for r in rows]

def db_delete_schedule(schedule_id: int, uid: int):
    with db_connect() as c:
        c.execute("UPDATE schedules SET active=0 WHERE id=? AND uid=?",
                  (schedule_id, uid))

# ─── SYSTEM PROMPT ────────────────────────────────────────
def build_system(uid: int) -> str:
    mem = db_get_memory(uid)
    mem_str = ""
    if mem:
        mem_str = "\n\n=== PERSISTENT MEMORY (what you know about this user) ===\n"
        for k, v in mem.items():
            mem_str += f"• {k}: {v}\n"

    return f"""You are an extremely intelligent autonomous AI Agent — as smart as Claude itself.
Execute every task completely and independently. Never ask the user for help mid-task.
Today: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
{mem_str}

══ TOOLS ══
[THINK: analysis]            → plan step by step before acting (use ALWAYS first)
[SEARCH: query]              → web search (titles + snippets + URLs)
[FETCH: url]                 → read full webpage content
[RUN: bash_command]          → execute bash/python command (infinite retries until success)
[CREATE: filepath | content] → create or overwrite a file
[READ: filepath]             → read a file
[LIST: directory]            → list directory contents
[REMEMBER: key | value]      → save info to permanent memory (survives restarts)
[FORGET: key]                → delete from permanent memory
[SCHEDULE: task | interval]  → schedule recurring task (daily/hourly/weekly/Xm)
[UNSCHEDULE: id]             → cancel a scheduled task

══ INTELLIGENCE RULES ══
1. ALWAYS start with [THINK:] — full step-by-step plan
2. CODE TASKS — infinite retry loop until code works:
   a. [THINK:] about architecture first
   b. Install dependencies: [RUN: pip install X Y Z]
   c. Write code: [CREATE: file.py | ...]
   d. Test: [RUN: python file.py]
   e. Read FULL error — identify EXACT line and cause
   f. Fix precisely — NOT generally
   g. Retry — NO LIMIT until it works perfectly
   h. If same approach fails 3 times → completely change strategy
   i. Verify final result before reporting success
3. RESEARCH — use [FETCH:] on multiple pages, not just snippets
4. MEMORY — use [REMEMBER:] to save user preferences, project details, recurring info
5. ERROR DIAGNOSIS must be specific:
   - ModuleNotFoundError → pip install the missing module
   - SyntaxError line N → fix exact syntax at that line
   - KeyError 'x' → add .get('x') or check dict
   - ConnectionError → check URL format, try alternative
   - PermissionError → use /tmp or different path

Respond entirely in Arabic. Be thorough and precise."""

# ─── TOOL IMPLEMENTATIONS ─────────────────────────────────
def search_web(query: str) -> str:
    try:
        results = []
        with DDGS() as d:
            for r in d.text(query.strip(), max_results=8):
                results.append(
                    f"TITLE: {r['title']}\n"
                    f"SNIPPET: {r['body'][:400]}\n"
                    f"URL: {r['href']}"
                )
        return "\n---\n".join(results) or "no results"
    except Exception as e:
        return f"SEARCH_ERROR: {e}"

def fetch_page(url: str) -> str:
    try:
        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"}
        r = requests.get(url.strip(), headers=h, timeout=20)
        t = r.text
        t = re.sub(r'<script[^>]*>.*?</script>', ' ', t, flags=re.DOTALL|re.I)
        t = re.sub(r'<style[^>]*>.*?</style>',  ' ', t, flags=re.DOTALL|re.I)
        t = re.sub(r'',               ' ', t, flags=re.DOTALL)
        t = re.sub(r'<[^>]+>',                  ' ', t)
        t = re.sub(r'&[a-z#0-9]+;',             ' ', t)
        t = re.sub(r'\s+',                       ' ', t).strip()
        return t[:6000] + ("…[truncated]" if len(t) > 6000 else "")
    except Exception as e:
        return f"FETCH_ERROR: {e}"

def run_cmd(cmd: str) -> str:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, timeout=120, cwd=str(WORKDIR)
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        if err and not out: return f"STDERR:\n{err[:3000]}"
        if err: return f"STDOUT:\n{out[:2000]}\nSTDERR:\n{err[:1000]}"
        return out[:3000] or "✓ done (no output)"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: >120s"
    except Exception as e:
        return f"RUN_ERROR: {e}"

def create_file(path: str, content: str) -> str:
    try:
        full = WORKDIR / path.strip().lstrip("/")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"✓ CREATED: {full} ({full.stat().st_size:,} bytes)"
    except Exception as e:
        return f"CREATE_ERROR: {e}"

def read_file(path: str) -> str:
    try:
        full = WORKDIR / path.strip().lstrip("/")
        content = full.read_text(encoding="utf-8")
        return content[:5000] + ("…[truncated]" if len(content) > 5000 else "")
    except Exception as e:
        return f"READ_ERROR: {e}"

def list_dir(path: str) -> str:
    try:
        target = WORKDIR / path.strip().lstrip("/") if path.strip() else WORKDIR
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for item in items:
            if item.is_dir():
                lines.append(f"📁 {item.name}/")
            else:
                lines.append(f"📄 {item.name} ({item.stat().st_size:,} bytes)")
        return "\n".join(lines) or "(empty)"
    except Exception as e:
        return f"LIST_ERROR: {e}"

def parse_interval(interval_str: str) -> timedelta:
    s = interval_str.lower().strip()
    if s == "hourly":   return timedelta(hours=1)
    if s == "daily":    return timedelta(days=1)
    if s == "weekly":   return timedelta(weeks=1)
    m = re.match(r'^(\d+)m$', s)
    if m: return timedelta(minutes=int(m.group(1)))
    h = re.match(r'^(\d+)h$', s)
    if h: return timedelta(hours=int(h.group(1)))
    return timedelta(hours=1)

# ─── ACTION PROCESSOR ─────────────────────────────────────
def process_actions(text: str, uid: int) -> str:
    out = text

    for t in re.findall(r'\[THINK: (.+?)\]', text, re.DOTALL):
        out = out.replace(f"[THINK: {t}]", f"\n💭 {t.strip()}\n")

    for q in re.findall(r'\[SEARCH: (.+?)\]', text, re.DOTALL):
        res = search_web(q)
        out = out.replace(f"[SEARCH: {q}]", f"\n🔍 RESULTS:\n{res}\n")

    for u in re.findall(r'\[FETCH: (.+?)\]', text):
        res = fetch_page(u)
        out = out.replace(f"[FETCH: {u}]", f"\n📄 PAGE:\n{res}\n")

    for c in re.findall(r'\[RUN: (.+?)\]', text, re.DOTALL):
        res = run_cmd(c)
        out = out.replace(f"[RUN: {c}]", f"\n💻 OUTPUT:\n{res}\n")

    for m in re.finditer(r'\[CREATE: (.+?) \| (.+?)\]', text, re.DOTALL):
        res = create_file(m.group(1), m.group(2))
        out = out.replace(m.group(0), f"\n{res}\n")

    for fp in re.findall(r'\[READ: (.+?)\]', text):
        res = read_file(fp)
        out = out.replace(f"[READ: {fp}]", f"\n📁 FILE:\n{res}\n")

    for dp in re.findall(r'\[LIST: (.+?)\]', text):
        res = list_dir(dp)
        out = out.replace(f"[LIST: {dp}]", f"\n📂 DIR:\n{res}\n")

    for m in re.finditer(r'\[REMEMBER: (.+?) \| (.+?)\]', text, re.DOTALL):
        db_set_memory(uid, m.group(1).strip(), m.group(2).strip())
        out = out.replace(m.group(0), f"\n✓ SAVED TO MEMORY: {m.group(1).strip()}\n")

    for k in re.findall(r'\[FORGET: (.+?)\]', text):
        with db_connect() as conn:
            conn.execute("DELETE FROM memory WHERE uid=? AND key=?", (uid, k.strip()))
        out = out.replace(f"[FORGET: {k}]", f"\n✓ FORGOTTEN: {k.strip()}\n")

    for m in re.finditer(r'\[SCHEDULE: (.+?) \| (.+?)\]', text, re.DOTALL):
        task     = m.group(1).strip()
        interval = m.group(2).strip()
        delta    = parse_interval(interval)
        next_run = datetime.utcnow() + delta
        db_add_schedule(uid, task, interval, next_run)
        out = out.replace(m.group(0),
            f"\n✓ SCHEDULED: '{task}' every {interval}, next: {next_run.strftime('%H:%M UTC')}\n")

    for sid in re.findall(r'\[UNSCHEDULE: (.+?)\]', text):
        try:
            db_delete_schedule(int(sid.strip()), uid)
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\n✓ CANCELLED schedule #{sid.strip()}\n")
        except Exception as e:
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\nERROR: {e}\n")

    return out

# ─── ERROR DETECTION ──────────────────────────────────────
ERR_PATTERNS = [
    "traceback (most recent call last)", "syntaxerror", "nameerror",
    "typeerror", "valueerror", "importerror", "modulenotfounderror",
    "attributeerror", "keyerror", "indexerror", "filenotfounderror",
    "connectionerror", "timeouterror", "permissionerror", "oserror",
    "runtimeerror", "assertionerror", "stderr:\n", "error:",
    "create_error", "run_error", "fetch_error", "read_error",
    "exception", " failed\n", "no such file"
]

def has_error(text: str) -> bool:
    tl = text.lower()
    return any(p in tl for p in ERR_PATTERNS)

# ─── AI CALL ──────────────────────────────────────────────
def call_ai(messages: list) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://agent247.app",
        },
        json={"model": MODEL, "messages": messages, "max_tokens": 4096},
        timeout=120,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"API Error: {data['error']}")
    return data["choices"][0]["message"]["content"]

# ─── MAIN AGENT LOOP — INFINITE RETRY ─────────────────────
def run_agent(uid: int, user_msg: str) -> str:
    db_add_message(uid, "user", user_msg)
    iteration = 0
    resp = ""

    while True:
        iteration += 1
        log.info(f"[uid={uid}] iteration {iteration}")

        hist = db_get_history(uid)
        system = build_system(uid)

        try:
            resp = call_ai([{"role": "system", "content": system}] + hist)
        except Exception as e:
            log.error(f"AI call failed: {e}")
            time.sleep(3)
            continue

        processed = process_actions(resp, uid)
        had_actions = processed != resp

        if had_actions:
            db_add_message(uid, "assistant", resp)
            error_found = has_error(processed)

            if error_found:
                # Infinite retry — tell AI exactly what failed
                retry_msg = (
                    f"=== Iteration {iteration} — ERROR DETECTED ===\n"
                    f"{processed[:4000]}\n\n"
                    f"Analyze the error precisely:\n"
                    f"1. Exact error type and message?\n"
                    f"2. Which file and line?\n"
                    f"3. Root cause?\n"
                    f"4. Exact fix?\n"
                    f"Fix it now. Keep trying until it works — no giving up."
                )
                db_add_message(uid, "user", retry_msg)
                # Small pause to avoid rate limits
                time.sleep(1)
                continue
            else:
                # Success — summarize
                db_add_message(uid, "user",
                    f"Results:\n{processed[:3500]}\n\n"
                    f"Summarize in Arabic what was accomplished and what files were created."
                )
                try:
                    hist2 = db_get_history(uid)
                    final = call_ai([{"role":"system","content":system}] + hist2)
                    db_add_message(uid, "assistant", final)
                    return final
                except Exception as e:
                    return processed[:2000]
        else:
            # Pure text response
            db_add_message(uid, "assistant", resp)
            return resp

# ─── IMAGE ANALYSIS ───────────────────────────────────────
def analyze_image(image_bytes: bytes, caption: str, uid: int) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "صف هذه الصورة بالتفصيل وأي شيء مفيد فيها"

    db_add_message(uid, "user", f"[صورة مرفقة] {prompt}")

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "anthropic/claude-sonnet-4-5",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=60,
    )
    data = resp.json()
    if "error" in data:
        return f"خطأ تحليل الصورة: {data['error']}"
    result = data["choices"][0]["message"]["content"]
    db_add_message(uid, "assistant", result)
    return result

# ─── DOCUMENT ANALYSIS ────────────────────────────────────
def analyze_document(file_bytes: bytes, filename: str, caption: str, uid: int) -> str:
    # Save file
    save_path = WORKDIR / filename
    save_path.write_bytes(file_bytes)

    prompt = (
        f"تم رفع الملف: {filename}\n"
        f"المسار: {save_path}\n"
        + (f"طلب المستخدم: {caption}" if caption else "اقرأ الملف وقدم ملخصاً كاملاً")
    )
    return run_agent(uid, prompt)

# ─── TELEGRAM HANDLERS ────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "🤖 *AI Agent Ultimate v4 — جاهز!*\n\n"
        "🧠 Claude — أذكى موديل متاح\n"
        "🔍 بحث عميق في المواقع\n"
        "🔄 Retry لا نهائي حتى يشتغل الكود\n"
        "💾 ذاكرة دائمة لا تُمسح\n"
        "⏰ مهام مجدولة تلقائية\n"
        "🖼 تحليل صور وملفات\n"
        "🛠 يصنع بوتات Telegram جاهزة\n"
        "☁️ يشتغل 24/7\n\n"
        "الأوامر:\n"
        "/buildbot [وصف] — اصنع بوت Telegram جديد\n"
        "/memory — عرض الذاكرة الدائمة\n"
        "/files — عرض الملفات\n"
        "/schedules — المهام المجدولة\n"
        "/clear — مسح المحادثة\n\n"
        "اعطيني أي مهمة! 🚀",
        parse_mode="Markdown"
    )

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db_clear_history(uid)
    await update.message.reply_text("🔄 تم مسح سجل المحادثة — الذاكرة الدائمة محفوظة")

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mem = db_get_memory(uid)
    if not mem:
        await update.message.reply_text("📭 الذاكرة الدائمة فارغة")
        return
    lines = [f"• *{k}:* {v}" for k, v in mem.items()]
    await update.message.reply_text(
        "💾 *الذاكرة الدائمة:*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    listing = list_dir("")
    await update.message.reply_text(f"📂 *الملفات:*\n\n{listing}", parse_mode="Markdown")

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    scheds = db_list_schedules(uid)
    if not scheds:
        await update.message.reply_text("📭 لا توجد مهام مجدولة")
        return
    lines = []
    for s in scheds:
        lines.append(f"#{s['id']} — {s['task']}\n⏱ كل {s['interval']} | التالية: {s['next_run'][:16]}")
    await update.message.reply_text(
        "⏰ *المهام المجدولة:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown"
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text
    log.info(f"[uid={uid}] '{text[:80]}'")

    status = await update.message.reply_text("⚙️ جاري التنفيذ…")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, text
        )
        chunks = [result[i:i+4000] for i in range(0, max(len(result),1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        log.error(f"Error: {e}")
        await status.edit_text(f"❌ خطأ: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    caption = update.message.caption or ""
    status  = await update.message.reply_text("🖼 جاري تحليل الصورة…")
    try:
        photo   = update.message.photo[-1]
        tf      = await photo.get_file()
        img_bytes = await tf.download_as_bytearray()
        result  = await asyncio.get_event_loop().run_in_executor(
            None, analyze_image, bytes(img_bytes), caption, uid
        )
        await status.edit_text(result[:4000])
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    caption  = update.message.caption or ""
    doc      = update.message.document
    status   = await update.message.reply_text(f"📄 جاري معالجة {doc.file_name}…")
    try:
        tf       = await doc.get_file()
        fb       = await tf.download_as_bytearray()
        result   = await asyncio.get_event_loop().run_in_executor(
            None, analyze_document, bytes(fb), doc.file_name, caption, uid
        )
        chunks = [result[i:i+4000] for i in range(0, max(len(result),1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

# ─── SCHEDULER JOB ────────────────────────────────────────
async def scheduler_job(ctx: ContextTypes.DEFAULT_TYPE):
    due = db_get_due_schedules()
    for s in due:
        uid  = s["uid"]
        task = s["task"]
        log.info(f"Running scheduled task #{s['id']} for uid={uid}: {task}")
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, run_agent, uid, f"[مهمة مجدولة تلقائية]: {task}"
            )
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"⏰ *مهمة مجدولة:*\n{task}\n\n{result[:3800]}",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Scheduled task error: {e}")
            try:
                await ctx.bot.send_message(chat_id=uid, text=f"❌ فشل المهمة المجدولة: {e}")
            except:
                pass
        finally:
            delta    = parse_interval(s["interval"])
            next_run = datetime.utcnow() + delta
            db_update_schedule_next(s["id"], next_run)

# ─── BOT BUILDER ──────────────────────────────────────────
BOT_BUILDER_PROMPT = """You are an expert Telegram bot developer.
The user wants to create a new Telegram bot. Your job:

1. Ask the user (via the description provided) what the bot should do
2. Write a COMPLETE, production-ready Python bot using python-telegram-bot>=20.7
3. The code must:
   - Use environment variables for TOKEN (os.environ.get("TELEGRAM_TOKEN"))
   - Handle all edge cases
   - Include helpful /start message explaining what the bot does
   - Be fully functional with zero modifications needed
4. Also generate:
   - requirements.txt
   - Procfile: worker: python bot.py
   - railway.toml with restart policy
   - A clear README in Arabic explaining:
     a. What the bot does
     b. How to get a token from @BotFather
     c. How to deploy on Railway step by step

Output format — use EXACTLY these markers:
===BOT_CODE===
[full python code here]
===REQUIREMENTS===
[requirements here]
===PROCFILE===
worker: python bot.py
===RAILWAY_TOML===
[railway.toml content]
===README===
[Arabic README here]

Make the bot impressive, useful and complete."""

def build_bot(description: str) -> dict:
    """Generate a complete ready-to-deploy Telegram bot from a description."""
    log.info(f"Building bot: {description[:80]}")

    messages = [
        {"role": "system", "content": BOT_BUILDER_PROMPT},
        {"role": "user",   "content": f"اصنعلي بوت Telegram بهذه المواصفات:\n{description}"}
    ]

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "max_tokens": 4096},
        timeout=120,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"API Error: {data['error']}")

    text = data["choices"][0]["message"]["content"]

    # Parse sections
    def extract(marker_start, marker_end):
        pattern = rf'{re.escape(marker_start)}\n(.*?)\n{re.escape(marker_end)}'
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    return {
        "bot_code":    extract("===BOT_CODE===",      "===REQUIREMENTS==="),
        "requirements":extract("===REQUIREMENTS===",  "===PROCFILE==="),
        "procfile":    extract("===PROCFILE===",       "===RAILWAY_TOML==="),
        "railway_toml":extract("===RAILWAY_TOML===",  "===README==="),
        "readme":      extract("===README===",         "===END==="),
        "raw":         text,
    }

def create_bot_zip(parts: dict, bot_name: str) -> bytes:
    """Pack all bot files into a ZIP in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{bot_name}/bot.py",           parts.get("bot_code", ""))
        zf.writestr(f"{bot_name}/requirements.txt", parts.get("requirements", ""))
        zf.writestr(f"{bot_name}/Procfile",         parts.get("procfile", "worker: python bot.py"))
        zf.writestr(f"{bot_name}/railway.toml",     parts.get("railway_toml",
            '[build]\nbuilder = "NIXPACKS"\n\n[deploy]\nrestartPolicyType = "ON_FAILURE"\nrestartPolicyMaxRetries = 10\n'))
        zf.writestr(f"{bot_name}/README.md",        parts.get("readme", ""))
    buf.seek(0)
    return buf.read()

# ─── BOT BUILDER COMMAND ──────────────────────────────────
async def cmd_build_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point: /buildbot [description]"""
    uid  = update.effective_user.id
    args = " ".join(ctx.args).strip() if ctx.args else ""

    if not args:
        await update.message.reply_text(
            "🤖 *صانع البوتات*\n\n"
            "اكتب وصف البوت اللي تبيه مباشرة بعد الأمر:\n\n"
            "مثال:\n"
            "`/buildbot بوت يجيب على أسئلة الطلاب عن الرياضيات`\n\n"
            "`/buildbot بوت يترجم النصوص من العربي للإنجليزي`\n\n"
            "`/buildbot بوت يحفظ المهام اليومية ويذكرك بها`",
            parse_mode="Markdown"
        )
        return

    status = await update.message.reply_text("⚙️ جاري بناء البوت… قد يأخذ دقيقة")

    try:
        parts    = await asyncio.get_event_loop().run_in_executor(None, build_bot, args)
        bot_name = re.sub(r'[^a-z0-9_]', '_', args[:30].lower().replace(' ', '_'))
        zip_data = create_bot_zip(parts, bot_name)

        # Send summary message
        readme_preview = parts.get("readme", "")[:800]
        await status.edit_text(
            f"✅ *البوت جاهز!*\n\n"
            f"📦 اسم المشروع: `{bot_name}`\n\n"
            f"{readme_preview}\n\n"
            f"⬇️ حمّل الـ ZIP وارفعه على Railway:",
            parse_mode="Markdown"
        )

        # Send ZIP file
        await update.message.reply_document(
            document=io.BytesIO(zip_data),
            filename=f"{bot_name}.zip",
            caption=(
                f"🤖 بوت Telegram جاهز للرفع\n"
                f"📋 الملفات: bot.py + requirements.txt + Procfile + railway.toml\n\n"
                f"خطوات الرفع:\n"
                f"1️⃣ ارفع الملفات على github.com/new\n"
                f"2️⃣ في railway.app اضغط New Project ← GitHub\n"
                f"3️⃣ أضف TELEGRAM_TOKEN في Variables\n"
                f"4️⃣ Deploy ✅"
            )
        )

        # Also send the code directly for preview
        if parts.get("bot_code"):
            code_preview = parts["bot_code"][:3000]
            await update.message.reply_text(
                f"👀 *معاينة الكود:*\n\n```python\n{code_preview}\n```",
                parse_mode="Markdown"
            )

    except Exception as e:
        log.error(f"Bot builder error: {e}")
        await status.edit_text(f"❌ خطأ في بناء البوت: {e}")

# ─── MAIN ─────────────────────────────────────────────────
def main():
    if not TG_TOKEN:
        raise ValueError("❌ TELEGRAM_TOKEN غير موجود في Environment Variables")

    db_init()
    log.info(f"Database: {DB_PATH}")
    log.info(f"Workspace: {WORKDIR}")
    log.info(f"Model: {MODEL}")

    app = ApplicationBuilder().token(TG_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("memory",    cmd_memory))
    app.add_handler(CommandHandler("files",     cmd_files))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("buildbot",  cmd_build_bot))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO,    handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Scheduler — check every 60 seconds
    app.job_queue.run_repeating(scheduler_job, interval=60, first=10)

    log.info("🤖 Agent Ultimate يشتغل!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
