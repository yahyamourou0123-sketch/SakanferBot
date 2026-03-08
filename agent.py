"""
AI Agent Ultimate v5 — 24/7 Telegram Bot
Features: Infinite retry, permanent memory, scheduler,
          file/image support, BOT BUILDER, SMART MODEL ROUTING,
          UNLIMITED KEY ROTATION (comma-separated keys)
"""
import os, requests, subprocess, re, time, logging, sqlite3, asyncio, base64, zipfile, io
from datetime import datetime, timedelta
from pathlib import Path
from duckduckgo_search import DDGS
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

# ─── LOGGING ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
WORKDIR  = Path(os.environ.get("WORKDIR", "/tmp/workspace"))
DB_PATH  = Path(os.environ.get("DB_PATH",  "/tmp/agent_memory.db"))
WORKDIR.mkdir(parents=True, exist_ok=True)

if not TG_TOKEN:
    raise ValueError("TELEGRAM_TOKEN غير موجود في Environment Variables")

# ─── UNLIMITED KEY ROTATION ✅ ────────────────────────────
# طريقة 1: مفاتيح مفصولة بفاصلة في Variable واحد
#   OPENROUTER_KEYS = key1,key2,key3,...,key5000
# طريقة 2: مفاتيح منفردة
#   OPENROUTER_KEY, OPENROUTER_KEY2, OPENROUTER_KEY3, ...

def _load_keys() -> list:
    keys = []
    # طريقة 1 — Variable واحد بفواصل (يدعم آلاف المفاتيح)
    bulk = os.environ.get("OPENROUTER_KEYS", "")
    if bulk:
        keys += [k.strip() for k in bulk.split(",") if k.strip()]
    # طريقة 2 — Variables منفردة (حتى 20)
    for i in range(1, 21):
        suffix = "" if i == 1 else str(i)
        k = os.environ.get(f"OPENROUTER_KEY{suffix}", "")
        if k.strip():
            keys.append(k.strip())
    # إزالة المكررات مع الحفاظ على الترتيب
    seen = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique

API_KEYS = _load_keys()

if not API_KEYS:
    raise ValueError("لا يوجد أي مفتاح — أضف OPENROUTER_KEYS أو OPENROUTER_KEY في Railway")

log.info(f"✅ {len(API_KEYS)} مفتاح محمّل")

_key_index = 0

def get_next_key() -> str:
    global _key_index
    key = API_KEYS[_key_index % len(API_KEYS)]
    _key_index += 1
    return key

# ─── SMART MODEL ROUTER ✅ ────────────────────────────────
#
# الخريطة الكاملة:
#   بحث / أسئلة عامة / ترجمة  → Gemini 2.0 Flash  (مربوط بـ Google، أسرع وأرخص)
#   برمجة / كود / debug        → Claude Sonnet 4.5  (الأقوى في البرمجة)
#   رياضيات / منطق / تحليل    → Claude Sonnet 4.5  (أقوى في المنطق)
#   تحليل ملفات / PDF          → Claude Sonnet 4.5  (أفضل في المستندات)
#   صور                        → GPT-4o             (vision الأفضل)
#   /opus — مهام صعبة معقدة   → Claude Opus 4.6    (الأذكى والأعمق)
#   /buildbot                  → Claude Opus 4.6    (مشروع كبير يحتاج تفكير)

CODING_TRIGGERS = [
    "برمج", "كود", "كوود", "code", "python", "javascript", "js",
    "html", "css", "sql", "bash", "linux", "script", "خطأ", "error",
    "fix", "bug", "تطبيق", "app", "api", "بوت", "bot", "صمم",
    "function", "class", "import", "debug", "dockerfile", "json",
    "احسب", "رياضيات", "معادلة", "math", "حساب", "قانون",
    "ملف", "pdf", "document", "اقرأ الملف", "حلل الملف"
]

SEARCH_TRIGGERS = [
    "ابحث", "بحث", "search", "اخبار", "اخبر", "news", "ما هو", "من هو",
    "متى", "اين", "كيف", "لماذا", "ما هي", "شرح", "explain",
    "ترجم", "translate", "translation", "ترجمة", "معنى",
    "سعر", "price", "طقس", "weather", "افضل", "مقارنة"
]

def get_model(user_input: str) -> str:
    text = user_input.lower()
    # برمجة/منطق/ملفات → Claude Sonnet
    if any(k in text for k in CODING_TRIGGERS):
        log.info("Model: claude-sonnet-4-5 (برمجة/منطق/ملفات)")
        return "anthropic/claude-sonnet-4-5"
    # بحث/أسئلة/ترجمة → Gemini (مربوط بـ Google)
    if any(k in text for k in SEARCH_TRIGGERS):
        log.info("Model: gemini-2.0-flash (بحث/أسئلة/ترجمة)")
        return "google/gemini-2.0-flash-001"
    # الباقي → Gemini افتراضي (سريع ورخيص)
    log.info("Model: gemini-2.0-flash (عام)")
    return "google/gemini-2.0-flash-001"

# ─── DATABASE ─────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            uid     INTEGER NOT NULL,
            role    TEXT    NOT NULL,
            content TEXT    NOT NULL,
            ts      DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            uid     INTEGER NOT NULL,
            key     TEXT    NOT NULL,
            value   TEXT    NOT NULL,
            ts      DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(uid, key) ON CONFLICT REPLACE
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            uid      INTEGER NOT NULL,
            task     TEXT    NOT NULL,
            interval TEXT    NOT NULL,
            next_run DATETIME,
            active   INTEGER DEFAULT 1,
            ts       DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_msg_uid ON messages(uid);
        CREATE INDEX IF NOT EXISTS idx_mem_uid ON memory(uid);
        CREATE INDEX IF NOT EXISTS idx_sch_uid ON schedules(uid);
        """)

def db_add_message(uid, role, content):
    with db_connect() as c:
        c.execute("INSERT INTO messages (uid,role,content) VALUES (?,?,?)", (uid, role, content))
        c.execute("""DELETE FROM messages WHERE uid=? AND id NOT IN (
                     SELECT id FROM messages WHERE uid=? ORDER BY id DESC LIMIT 60
                   )""", (uid, uid))

def db_get_history(uid):
    with db_connect() as c:
        rows = c.execute(
            "SELECT role,content FROM messages WHERE uid=? ORDER BY id DESC LIMIT 40", (uid,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def db_clear_history(uid):
    with db_connect() as c:
        c.execute("DELETE FROM messages WHERE uid=?", (uid,))

def db_set_memory(uid, key, value):
    with db_connect() as c:
        c.execute("INSERT OR REPLACE INTO memory (uid,key,value) VALUES (?,?,?)", (uid, key, value))

def db_get_memory(uid):
    with db_connect() as c:
        rows = c.execute("SELECT key,value FROM memory WHERE uid=?", (uid,)).fetchall()
    return {r["key"]: r["value"] for r in rows}

def db_add_schedule(uid, task, interval, next_run):
    with db_connect() as c:
        c.execute("INSERT INTO schedules (uid,task,interval,next_run) VALUES (?,?,?,?)",
                  (uid, task, interval, next_run.isoformat()))

def db_get_due_schedules():
    with db_connect() as c:
        rows = c.execute(
            "SELECT * FROM schedules WHERE active=1 AND next_run <= ?",
            (datetime.utcnow().isoformat(),)
        ).fetchall()
    return [dict(r) for r in rows]

def db_update_schedule_next(sid, next_run):
    with db_connect() as c:
        c.execute("UPDATE schedules SET next_run=? WHERE id=?", (next_run.isoformat(), sid))

def db_list_schedules(uid):
    with db_connect() as c:
        rows = c.execute(
            "SELECT id,task,interval,next_run FROM schedules WHERE uid=? AND active=1", (uid,)
        ).fetchall()
    return [dict(r) for r in rows]

def db_delete_schedule(sid, uid):
    with db_connect() as c:
        c.execute("UPDATE schedules SET active=0 WHERE id=? AND uid=?", (sid, uid))

# ─── SYSTEM PROMPT ────────────────────────────────────────
def build_system(uid):
    mem = db_get_memory(uid)
    mem_str = ""
    if mem:
        mem_str = "\n\n=== PERSISTENT MEMORY ===\n"
        for k, v in mem.items():
            mem_str += f"• {k}: {v}\n"

    return f"""You are an extremely intelligent autonomous AI Agent.
Execute every task completely and independently. Never ask for help mid-task.
Today: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
{mem_str}

TOOLS:
[THINK: analysis]            → plan before acting (ALWAYS use first)
[SEARCH: query]              → web search
[FETCH: url]                 → read full webpage
[RUN: bash_command]          → execute command
[CREATE: filepath | content] → create/overwrite file
[READ: filepath]             → read file
[LIST: directory]            → list directory
[REMEMBER: key | value]      → save to permanent memory
[FORGET: key]                → delete from memory
[SCHEDULE: task | interval]  → schedule task (daily/hourly/weekly/Xm/Xh)
[UNSCHEDULE: id]             → cancel scheduled task

RULES:
1. Always [THINK:] first
2. CODE: write → install → test → read full error → fix exact line → retry INFINITELY
3. If same error 3x → change strategy completely
4. Use [REMEMBER:] for important user info
5. Respond entirely in Arabic."""

# ─── TOOLS ────────────────────────────────────────────────
def search_web(query):
    try:
        results = []
        with DDGS() as d:
            for r in d.text(query.strip(), max_results=8):
                results.append(f"TITLE: {r['title']}\nSNIPPET: {r['body'][:400]}\nURL: {r['href']}")
        return "\n---\n".join(results) or "no results"
    except Exception as e:
        return f"SEARCH_ERROR: {e}"

def fetch_page(url):
    try:
        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"}
        r = requests.get(url.strip(), headers=h, timeout=20)
        t = r.text
        t = re.sub(r'<script[^>]*>.*?</script>', ' ', t, flags=re.DOTALL | re.I)
        t = re.sub(r'<style[^>]*>.*?</style>',  ' ', t, flags=re.DOTALL | re.I)
        t = re.sub(r'<!--.*?-->',               ' ', t, flags=re.DOTALL)
        t = re.sub(r'<[^>]+>',                  ' ', t)
        t = re.sub(r'&[a-z#0-9]+;',             ' ', t)
        t = re.sub(r'\s+',                       ' ', t).strip()
        return t[:6000] + ("…[truncated]" if len(t) > 6000 else "")
    except Exception as e:
        return f"FETCH_ERROR: {e}"

def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=120, cwd=str(WORKDIR))
        out, err = r.stdout.strip(), r.stderr.strip()
        if err and not out: return f"STDERR:\n{err[:3000]}"
        if err: return f"STDOUT:\n{out[:2000]}\nSTDERR:\n{err[:1000]}"
        return out[:3000] or "done"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: >120s"
    except Exception as e:
        return f"RUN_ERROR: {e}"

def create_file(path, content):
    try:
        full = WORKDIR / path.strip().lstrip("/")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"CREATED: {full} ({full.stat().st_size:,} bytes)"
    except Exception as e:
        return f"CREATE_ERROR: {e}"

def read_file(path):
    try:
        full = WORKDIR / path.strip().lstrip("/")
        content = full.read_text(encoding="utf-8")
        return content[:5000] + ("…[truncated]" if len(content) > 5000 else "")
    except Exception as e:
        return f"READ_ERROR: {e}"

def list_dir(path):
    try:
        target = WORKDIR / path.strip().lstrip("/") if path.strip() else WORKDIR
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"📁 {i.name}/" if i.is_dir() else f"📄 {i.name} ({i.stat().st_size:,} bytes)"
                 for i in items]
        return "\n".join(lines) or "(empty)"
    except Exception as e:
        return f"LIST_ERROR: {e}"

def parse_interval(s):
    s = s.lower().strip()
    if s == "hourly": return timedelta(hours=1)
    if s == "daily":  return timedelta(days=1)
    if s == "weekly": return timedelta(weeks=1)
    m = re.match(r'^(\d+)m$', s)
    if m: return timedelta(minutes=int(m.group(1)))
    h = re.match(r'^(\d+)h$', s)
    if h: return timedelta(hours=int(h.group(1)))
    return timedelta(hours=1)

# ─── ACTION PROCESSOR ─────────────────────────────────────
def process_actions(text, uid):
    out = text
    for t in re.findall(r'\[THINK: (.+?)\]', text, re.DOTALL):
        out = out.replace(f"[THINK: {t}]", f"\n💭 {t.strip()}\n")
    for q in re.findall(r'\[SEARCH: (.+?)\]', text, re.DOTALL):
        out = out.replace(f"[SEARCH: {q}]", f"\n🔍 RESULTS:\n{search_web(q)}\n")
    for u in re.findall(r'\[FETCH: (.+?)\]', text):
        out = out.replace(f"[FETCH: {u}]", f"\n📄 PAGE:\n{fetch_page(u)}\n")
    for c in re.findall(r'\[RUN: (.+?)\]', text, re.DOTALL):
        out = out.replace(f"[RUN: {c}]", f"\n💻 OUTPUT:\n{run_cmd(c)}\n")
    for m in re.finditer(r'\[CREATE: (.+?) \| (.+?)\]', text, re.DOTALL):
        out = out.replace(m.group(0), f"\n{create_file(m.group(1), m.group(2))}\n")
    for fp in re.findall(r'\[READ: (.+?)\]', text):
        out = out.replace(f"[READ: {fp}]", f"\n📁 FILE:\n{read_file(fp)}\n")
    for dp in re.findall(r'\[LIST: (.+?)\]', text):
        out = out.replace(f"[LIST: {dp}]", f"\n📂 DIR:\n{list_dir(dp)}\n")
    for m in re.finditer(r'\[REMEMBER: (.+?) \| (.+?)\]', text, re.DOTALL):
        db_set_memory(uid, m.group(1).strip(), m.group(2).strip())
        out = out.replace(m.group(0), f"\nSAVED: {m.group(1).strip()}\n")
    for k in re.findall(r'\[FORGET: (.+?)\]', text):
        with db_connect() as conn:
            conn.execute("DELETE FROM memory WHERE uid=? AND key=?", (uid, k.strip()))
        out = out.replace(f"[FORGET: {k}]", f"\nFORGOTTEN: {k.strip()}\n")
    for m in re.finditer(r'\[SCHEDULE: (.+?) \| (.+?)\]', text, re.DOTALL):
        task, interval = m.group(1).strip(), m.group(2).strip()
        next_run = datetime.utcnow() + parse_interval(interval)
        db_add_schedule(uid, task, interval, next_run)
        out = out.replace(m.group(0), f"\nSCHEDULED: '{task}' every {interval}\n")
    for sid in re.findall(r'\[UNSCHEDULE: (.+?)\]', text):
        try:
            db_delete_schedule(int(sid.strip()), uid)
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\nCANCELLED: #{sid.strip()}\n")
        except Exception as e:
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\nERROR: {e}\n")
    return out

# ─── ERROR DETECTION ──────────────────────────────────────
ERR_PATTERNS = [
    "traceback (most recent call last)", "syntaxerror", "nameerror",
    "typeerror", "valueerror", "importerror", "modulenotfounderror",
    "attributeerror", "keyerror", "indexerror", "filenotfounderror",
    "connectionerror", "oserror", "runtimeerror",
    "stderr:\n", "create_error", "run_error", "fetch_error"
]

def has_error(text):
    return any(p in text.lower() for p in ERR_PATTERNS)

# ─── AI CALL — Key Rotation تلقائي ───────────────────────
def call_ai(messages, model):
    last_err = None
    attempts = len(API_KEYS) * 2

    for attempt in range(attempts):
        key = get_next_key()
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://sakanferbot.railway.app",
                },
                json={"model": model, "messages": messages, "max_tokens": 4096},
                timeout=120,
            )
            data = resp.json()
            if "error" in data:
                err_msg = str(data["error"])
                if any(x in err_msg.lower() for x in ["rate limit", "quota", "limit exceeded", "429"]):
                    log.warning(f"Key محدود → Key التالي ({attempt+1}/{attempts})")
                    last_err = err_msg
                    time.sleep(0.5)
                    continue
                raise RuntimeError(f"API Error: {err_msg}")
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            log.warning(f"Timeout → retry ({attempt+1})")
            last_err = "timeout"
            time.sleep(2)
            continue
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            continue

    raise RuntimeError(f"كل المفاتيح فشلت: {last_err}")

# ─── MAIN AGENT LOOP ──────────────────────────────────────
def run_agent(uid, user_msg, force_model=None):
    db_add_message(uid, "user", user_msg)
    model     = force_model or get_model(user_msg)
    iteration = 0

    while True:
        iteration += 1
        log.info(f"[uid={uid}] iter={iteration} model={model}")
        hist   = db_get_history(uid)
        system = build_system(uid)

        try:
            resp = call_ai([{"role": "system", "content": system}] + hist, model)
        except Exception as e:
            log.error(f"AI error: {e}")
            time.sleep(3)
            continue

        processed   = process_actions(resp, uid)
        had_actions = processed != resp

        if had_actions:
            db_add_message(uid, "assistant", resp)
            if has_error(processed):
                db_add_message(uid, "user",
                    f"=== Iteration {iteration} — ERROR ===\n{processed[:3500]}\n\n"
                    f"Diagnose: exact error? exact line? root cause? fix now. No giving up.")
                time.sleep(1)
                continue
            else:
                db_add_message(uid, "user",
                    f"Results:\n{processed[:3000]}\nSummarize in Arabic what was done.")
                try:
                    final = call_ai(
                        [{"role": "system", "content": system}] + db_get_history(uid), model)
                    db_add_message(uid, "assistant", final)
                    return final
                except:
                    return processed[:2000]
        else:
            db_add_message(uid, "assistant", resp)
            return resp

# ─── IMAGE ANALYSIS — GPT-4o (الأفضل للصور) ──────────────
def analyze_image(image_bytes, caption, uid):
    b64    = base64.b64encode(image_bytes).decode()
    prompt = caption or "صف هذه الصورة بالتفصيل"
    db_add_message(uid, "user", f"[صورة] {prompt}")
    key  = get_next_key()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "openai/gpt-4o",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt}
            ]}]
        },
        timeout=60,
    )
    data = resp.json()
    if "error" in data:
        return f"خطأ: {data['error']}"
    result = data["choices"][0]["message"]["content"]
    db_add_message(uid, "assistant", result)
    return result

# ─── DOCUMENT ANALYSIS — Claude Sonnet (أفضل للمستندات) ──
def analyze_document(file_bytes, filename, caption, uid):
    save_path = WORKDIR / filename
    save_path.write_bytes(file_bytes)
    prompt = f"تم رفع الملف: {filename}. {caption or 'اقرأ الملف وقدم ملخصاً كاملاً'}"
    return run_agent(uid, prompt, "anthropic/claude-sonnet-4-5")

# ─── BOT BUILDER — Claude Opus 4.6 (الأفضل للمشاريع الكبيرة)
BOT_BUILDER_PROMPT = """You are an expert Telegram bot developer.
Build a COMPLETE, production-ready Python bot using python-telegram-bot>=20.7.

Requirements:
- Use os.environ.get("TELEGRAM_TOKEN") for token — never hardcode it
- Handle all edge cases gracefully
- Include a helpful /start message
- Fully functional with zero modifications needed

Output EXACTLY with these markers:
===BOT_CODE===
[complete python code]
===REQUIREMENTS===
[pip packages one per line]
===PROCFILE===
worker: python bot.py
===RAILWAY_TOML===
[build]
builder = "NIXPACKS"

[deploy]
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
===README===
[Arabic README: what it does, how to get token from BotFather, how to deploy on Railway]
===END==="""

def build_bot(description):
    messages = [
        {"role": "system", "content": BOT_BUILDER_PROMPT},
        {"role": "user",   "content": f"اصنعلي بوت Telegram:\n{description}"}
    ]
    # Opus 4.6 للمشاريع الكبيرة
    text = call_ai(messages, "anthropic/claude-opus-4-6")

    def extract(s, e):
        m = re.search(rf'{re.escape(s)}\n(.*?)\n{re.escape(e)}', text, re.DOTALL)
        return m.group(1).strip() if m else ""

    return {
        "bot_code":    extract("===BOT_CODE===",     "===REQUIREMENTS==="),
        "requirements":extract("===REQUIREMENTS===", "===PROCFILE==="),
        "procfile":    extract("===PROCFILE===",      "===RAILWAY_TOML==="),
        "railway_toml":extract("===RAILWAY_TOML===",  "===README==="),
        "readme":      extract("===README===",         "===END==="),
    }

def create_bot_zip(parts, bot_name):
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

# ─── TELEGRAM HANDLERS ────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *SakanferBot v5 — جاهز!*\n\n"
        f"🔑 {len(API_KEYS)} مفتاح نشط\n\n"
        "*توزيع الذكاء:*\n"
        "🔍 بحث/أسئلة/ترجمة → Gemini 2.0 (Google)\n"
        "💻 برمجة/كود/منطق → Claude Sonnet 4.5\n"
        "📄 ملفات/PDF → Claude Sonnet 4.5\n"
        "🖼 صور → GPT-4o Vision\n"
        "👑 `/opus` → Claude Opus 4.6\n"
        "🛠 `/buildbot` → Claude Opus 4.6\n\n"
        "*الأوامر:*\n"
        "`/opus [مهمة]` — أصعب المهام\n"
        "`/buildbot [وصف]` — اصنع بوت\n"
        "`/memory` — الذاكرة\n"
        "`/files` — الملفات\n"
        "`/schedules` — المجدولة\n"
        "`/keys` — حالة المفاتيح\n"
        "`/clear` — مسح المحادثة\n\n"
        "اعطيني أي مهمة! 🚀",
        parse_mode="Markdown"
    )

async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"*حالة المفاتيح:*\n\n"
        f"✅ {len(API_KEYS)} مفتاح نشط\n"
        f"🔄 Rotation تلقائي\n\n"
        f"*لإضافة مفاتيح:*\n"
        f"في Railway → Variables:\n"
        f"`OPENROUTER_KEYS = key1,key2,key3,...`",
        parse_mode="Markdown"
    )

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("🔄 تم المسح — الذاكرة الدائمة محفوظة")

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mem = db_get_memory(update.effective_user.id)
    res = "\n".join([f"• *{k}:* {v}" for k, v in mem.items()]) or "فارغة"
    await update.message.reply_text(f"💾 *الذاكرة:*\n{res}", parse_mode="Markdown")

async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📂 *الملفات:*\n{list_dir('')}", parse_mode="Markdown")

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scheds = db_list_schedules(update.effective_user.id)
    if not scheds:
        return await update.message.reply_text("📭 لا توجد مهام مجدولة")
    lines = [f"#{s['id']} — {s['task']}\n⏱ كل {s['interval']}" for s in scheds]
    await update.message.reply_text(
        "⏰ *المهام المجدولة:*\n\n" + "\n\n".join(lines), parse_mode="Markdown")

async def cmd_opus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip() if ctx.args else ""
    if not args:
        return await update.message.reply_text(
            "👑 *Claude Opus 4.6*\n\n"
            "للمهام الصعبة والمعقدة\n\n"
            "مثال:\n`/opus اكتب خطة عمل كاملة لمشروع تقني`",
            parse_mode="Markdown")
    uid    = update.effective_user.id
    status = await update.message.reply_text("👑 Claude Opus 4.6 يفكر…")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, args, "anthropic/claude-opus-4-6")
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(f"👑 *Opus 4.6:*\n\n{chunks[0]}", parse_mode="Markdown")
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    status = await update.message.reply_text("⚙️ جاري التنفيذ…")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, update.message.text, None)
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    status = await update.message.reply_text("🖼 تحليل الصورة…")
    try:
        f = await (update.message.photo[-1]).get_file()
        b = await f.download_as_bytearray()
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_image, bytes(b), update.message.caption or "", uid)
        await status.edit_text(result[:4000])
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    doc    = update.message.document
    status = await update.message.reply_text(f"📄 معالجة {doc.file_name}…")
    try:
        f  = await doc.get_file()
        b  = await f.download_as_bytearray()
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_document, bytes(b), doc.file_name,
            update.message.caption or "", uid)
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

async def cmd_build_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip() if ctx.args else ""
    if not args:
        return await update.message.reply_text(
            "🛠 *صانع البوتات*\n\nمثال:\n"
            "`/buildbot بوت يجيب على أسئلة الرياضيات`",
            parse_mode="Markdown")
    status = await update.message.reply_text("⚙️ Claude Opus يبني البوت…")
    try:
        parts    = await asyncio.get_event_loop().run_in_executor(None, build_bot, args)
        bot_name = re.sub(r'[^a-z0-9_]', '_', args[:25].lower().replace(' ', '_'))
        zip_data = create_bot_zip(parts, bot_name)
        await status.edit_text(
            f"✅ *البوت جاهز!*\n\n📦 `{bot_name}`\n\n{parts.get('readme','')[:400]}",
            parse_mode="Markdown")
        await update.message.reply_document(
            document=io.BytesIO(zip_data),
            filename=f"{bot_name}.zip",
            caption="bot.py + requirements.txt + Procfile + railway.toml ✅")
    except Exception as e:
        await status.edit_text(f"❌ خطأ: {e}")

# ─── SCHEDULER ────────────────────────────────────────────
async def scheduler_job(ctx: ContextTypes.DEFAULT_TYPE):
    for s in db_get_due_schedules():
        uid, task = s["uid"], s["task"]
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, run_agent, uid, f"[مهمة مجدولة]: {task}", None)
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"⏰ *مهمة مجدولة:*\n{task}\n\n{result[:3800]}",
                parse_mode="Markdown")
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        finally:
            db_update_schedule_next(s["id"], datetime.utcnow() + parse_interval(s["interval"]))

# ─── MAIN ─────────────────────────────────────────────────
def main():
    db_init()
    log.info(f"DB={DB_PATH} | Workspace={WORKDIR} | Keys={len(API_KEYS)}")

    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("memory",    cmd_memory))
    app.add_handler(CommandHandler("files",     cmd_files))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("buildbot",  cmd_build_bot))
    app.add_handler(CommandHandler("opus",      cmd_opus))
    app.add_handler(CommandHandler("keys",      cmd_keys))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.job_queue.run_repeating(scheduler_job, interval=60, first=10)

    log.info("🤖 SakanferBot v5 يشتغل!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
