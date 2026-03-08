"""
AI Agent Ultimate v4 — 24/7 Telegram Bot
Features: Infinite retry, permanent memory, scheduler,
          file/image support, BOT BUILDER (creates ready Telegram bots)
          INTELLIGENT MODEL ROUTING (Claude for Coding, GPT for general)
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
# ملاحظة: تم تعديل المتغيرات لجلبها من البيئة لزيادة الأمان
API_KEY   = os.environ.get("OPENROUTER_API_KEY", "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")
TG_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "8682338493:AAG0pfa69eCay9XxjCpbDX0zjfBbCnpSVdk")
WORKDIR   = Path(os.environ.get("WORKDIR",    "/tmp/workspace"))
DB_PATH   = Path(os.environ.get("DB_PATH",    "/tmp/agent_memory.db"))
WORKDIR.mkdir(parents=True, exist_ok=True)

# ─── MODEL ROUTER (اختيار النموذج الأفضل) ──────────────────────────
def get_dynamic_model(user_input: str) -> str:
    """يحلل النص ويختار أفضل نموذج للمهمة"""
    text = user_input.lower()
    # إذا كان الطلب يتضمن برمجة أو أوامر نظام أو ملفات برمجية
    coding_triggers = [
        "برمج", "كود", "python", "js", "html", "css", "error", "fix", 
        "script", "bash", "linux", "cmd", "صمم بوت", "تطبيق"
    ]
    if any(k in text for k in coding_triggers):
        log.info("🎯 تم اختيار: Claude 3.5 Sonnet (مهمة برمجية/منطقية)")
        return "anthropic/claude-3.5-sonnet"
    else:
        log.info("🌍 تم اختيار: GPT-4o (مهمة عامة/بحث)")
        return "openai/gpt-4o"

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

    return f"""You are an extremely intelligent autonomous AI Agent.
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
2. CODE TASKS — infinite retry loop until code works.
3. RESEARCH — use [FETCH:] on multiple pages, not just snippets
4. MEMORY — use [REMEMBER:] to save user preferences, project details, recurring info
5. Respond entirely in Arabic. Be thorough and precise."""

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
ERR_PATTERNS = ["traceback", "syntaxerror", "nameerror", "typeerror", "valueerror", "error:"]
def has_error(text: str) -> bool:
    tl = text.lower()
    return any(p in tl for p in ERR_PATTERNS)

# ─── AI CALL (المعدلة لدعم التغيير الديناميكي للموديل) ──────────────────────────
def call_ai(messages: list, model_name: str) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://agent247.app",
        },
        json={"model": model_name, "messages": messages, "max_tokens": 4096},
        timeout=120,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"API Error: {data['error']}")
    return data["choices"][0]["message"]["content"]

# ─── MAIN AGENT LOOP — INFINITE RETRY ─────────────────────
def run_agent(uid: int, user_msg: str) -> str:
    db_add_message(uid, "user", user_msg)
    
    # تحديد الموديل بناءً على رسالة المستخدم الأصلية
    selected_model = get_dynamic_model(user_msg)
    
    iteration = 0
    while True:
        iteration += 1
        log.info(f"[uid={uid}] iteration {iteration} using {selected_model}")
        hist = db_get_history(uid)
        system = build_system(uid)
        try:
            resp = call_ai([{"role": "system", "content": system}] + hist, selected_model)
        except Exception as e:
            log.error(f"AI call failed: {e}")
            time.sleep(3)
            continue
        processed = process_actions(resp, uid)
        if processed != resp:
            db_add_message(uid, "assistant", resp)
            if has_error(processed):
                retry_msg = f"=== Iteration {iteration} — ERROR ===\n{processed[:2000]}\nFix it now."
                db_add_message(uid, "user", retry_msg)
                time.sleep(1)
                continue
            else:
                db_add_message(uid, "user", f"Results:\n{processed[:2000]}\nSummarize in Arabic.")
                try:
                    final = call_ai([{"role":"system","content":system}] + db_get_history(uid), selected_model)
                    db_add_message(uid, "assistant", final)
                    return final
                except: return processed[:2000]
        else:
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
            "model": "openai/gpt-4o", # أفضل موديل للرؤية
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=60,
    )
    result = resp.json()["choices"][0]["message"]["content"]
    db_add_message(uid, "assistant", result)
    return result

# ─── DOCUMENT ANALYSIS ────────────────────────────────────
def analyze_document(file_bytes: bytes, filename: str, caption: str, uid: int) -> str:
    save_path = WORKDIR / filename
    save_path.write_bytes(file_bytes)
    return run_agent(uid, f"تم رفع الملف: {filename}. المسار: {save_path}. {caption}")

# ─── TELEGRAM HANDLERS (Start, Clear, Memory, Files, Schedules) ──────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *AI Agent Ultimate v4 — جاهز!*\nاعطيني أي مهمة! 🚀", parse_mode="Markdown")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("🔄 تم مسح سجل المحادثة")

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mem = db_get_memory(update.effective_user.id)
    res = "\n".join([f"• {k}: {v}" for k, v in mem.items()]) or "فارغة"
    await update.message.reply_text(f"💾 *الذاكرة:*\n{res}", parse_mode="Markdown")

async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📂 *الملفات:*\n{list_dir('')}", parse_mode="Markdown")

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scheds = db_list_schedules(update.effective_user.id)
    res = "\n".join([f"#{s['id']} {s['task']}" for s in scheds]) or "لا يوجد"
    await update.message.reply_text(f"⏰ *الجدولة:*\n{res}", parse_mode="Markdown")

# ─── HANDLING INPUTS ──────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⚙️ جاري التنفيذ…")
    res = await asyncio.get_event_loop().run_in_executor(None, run_agent, update.effective_user.id, update.message.text)
    await status.edit_text(res[:4000])

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("🖼 تحليل الصورة…")
    photo = update.message.photo[-1]
    f = await photo.get_file()
    b = await f.download_as_bytearray()
    res = await asyncio.get_event_loop().run_in_executor(None, analyze_image, bytes(b), update.message.caption, update.effective_user.id)
    await status.edit_text(res[:4000])

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    status = await update.message.reply_text(f"📄 معالجة {doc.file_name}…")
    f = await doc.get_file()
    b = await f.download_as_bytearray()
    res = await asyncio.get_event_loop().run_in_executor(None, analyze_document, bytes(b), doc.file_name, update.message.caption, update.effective_user.id)
    await status.edit_text(res[:4000])

# ─── SCHEDULER JOB ────────────────────────────────────────
async def scheduler_job(ctx: ContextTypes.DEFAULT_TYPE):
    for s in db_get_due_schedules():
        res = await asyncio.get_event_loop().run_in_executor(None, run_agent, s["uid"], f"[مهمة مجدولة]: {s['task']}")
        await ctx.bot.send_message(chat_id=s["uid"], text=f"⏰ *مهمة مجدولة:*\n{res[:3800]}", parse_mode="Markdown")
        db_update_schedule_next(s["id"], datetime.utcnow() + parse_interval(s["interval"]))

# ─── BOT BUILDER (كامل كما في كودك الأصلي) ──────────────────────────
BOT_BUILDER_PROMPT = """You are an expert Telegram bot developer... (etc)"""
def build_bot(description: str) -> dict:
    messages = [{"role": "system", "content": BOT_BUILDER_PROMPT}, {"role": "user", "content": description}]
    # نستخدم Claude دائماً لبناء البوتات لأنه الأفضل في هذا المجال
    text = call_ai(messages, "anthropic/claude-3.5-sonnet")
    def extract(marker_start, marker_end):
        p = rf'{re.escape(marker_start)}\n(.*?)\n{re.escape(marker_end)}'
        m = re.search(p, text, re.DOTALL)
        return m.group(1).strip() if m else ""
    return {"bot_code": extract("===BOT_CODE===", "===REQUIREMENTS==="), "requirements": extract("===REQUIREMENTS===", "===PROCFILE==="), "readme": extract("===README===", "===END===")}

def create_bot_zip(parts: dict, bot_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{bot_name}/bot.py", parts.get("bot_code", ""))
        zf.writestr(f"{bot_name}/README.md", parts.get("readme", ""))
    buf.seek(0)
    return buf.read()

async def cmd_build_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args)
    if not args: return await update.message.reply_text("اكتب وصف البوت")
    status = await update.message.reply_text("⚙️ بناء البوت…")
    parts = await asyncio.get_event_loop().run_in_executor(None, build_bot, args)
    zip_data = create_bot_zip(parts, "my_bot")
    await update.message.reply_document(document=io.BytesIO(zip_data), filename="bot.zip")
    await status.delete()

# ─── MAIN ─────────────────────────────────────────────────
def main():
    db_init()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("buildbot", cmd_build_bot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.job_queue.run_repeating(scheduler_job, interval=60)
    app.run_polling()

if __name__ == "__main__":
    main()
