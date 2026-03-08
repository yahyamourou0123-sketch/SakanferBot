"""
SakanferBot v5 - 24/7 AI Agent
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

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ============================================================
# PUT YOUR KEYS HERE
# ============================================================

TG_TOKEN = "8682338493:AAGbnyn71vnnOe7paPqPHPltQmejSUiyIZQ"

API_KEYS = [
    "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",
    "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",
    "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",
    "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",
    "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",
    "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",
        "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",
    # "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",

"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",

"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",

"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",

"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",

"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"
]

# ============================================================
# DO NOT CHANGE ANYTHING BELOW THIS LINE
# ============================================================

WORKDIR = Path("/tmp/workspace")
DB_PATH = Path("/tmp/agent_memory.db")
WORKDIR.mkdir(parents=True, exist_ok=True)

if "PUT_YOUR_TELEGRAM" in TG_TOKEN:
    raise ValueError("ERROR: Set your TELEGRAM TOKEN in TG_TOKEN")
if "PUT_YOUR_OPENROUTER" in API_KEYS[0]:
    raise ValueError("ERROR: Set your OPENROUTER KEY in API_KEYS")

log.info(f"Keys loaded: {len(API_KEYS)}")

_key_index = 0
def get_next_key():
    global _key_index
    key = API_KEYS[_key_index % len(API_KEYS)]
    _key_index += 1
    return key

CODING_TRIGGERS = [
    "code", "python", "javascript", "js", "html", "css", "sql", "bash",
    "script", "error", "fix", "bug", "app", "api", "bot", "function",
    "class", "import", "debug", "dockerfile", "math", "pdf", "file",
    "programme", "erreur", "application", "developpe",
    "barmej", "kod", "5ata2", "كود", "برمج", "خطأ", "تطبيق",
    "بوت", "احسب", "رياضيات", "معادلة", "ملف", "صمم", "كوود"
]

def get_model(user_input):
    text = user_input.lower()
    if any(k in text for k in CODING_TRIGGERS):
        return "anthropic/claude-sonnet-4-5"
    return "google/gemini-2.0-flash-001"

def db_connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(uid, key) ON CONFLICT REPLACE
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL, task TEXT NOT NULL,
            interval TEXT NOT NULL, next_run DATETIME,
            active INTEGER DEFAULT 1, ts DATETIME DEFAULT CURRENT_TIMESTAMP
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

SYSTEM_PROMPT = """You are Sakanfer - an elite AI agent and expert software engineer.

=== LANGUAGE RULES (MOST IMPORTANT) ===
You understand and speak ALL languages naturally.

TUNISIAN DIALECT & FRANCO-ARABIC (TOP PRIORITY):
- You fully understand Franco-Arabic (Arabizi/Tunisian dialect)
- Examples you recognize: chnahwa, bark, 3lah, kifesh, bhi, ya5i, sahbi,
  wesh, 9rib, mazel, barcha, chwaya, taw, fama, ma3ndich, nheb, huni,
  bch, mrigel, 3aychek, barka, yezzi, chkoun, win, ki, 3lech, taw
- When user writes in Franco or Tunisian dialect -> ALWAYS reply in same style
- NEVER use Egyptian, Syrian, or formal Arabic if user speaks Tunisian
- NEVER change language without reason

French -> reply in French
English -> reply in English  
Arabic -> reply in Arabic
Any mix -> adapt naturally

GOLDEN RULE: Always reply in the SAME language and dialect as the user.
Even short or unclear messages -> ALWAYS reply, never stay silent.

=== AVAILABLE TOOLS ===
[THINK: analysis]            -> deep thinking before any step (ALWAYS first)
[SEARCH: query]              -> web search
[FETCH: url]                 -> read full webpage
[RUN: bash_command]          -> execute command
[CREATE: filepath | content] -> create file
[READ: filepath]             -> read file
[LIST: directory]            -> list directory
[REMEMBER: key | value]      -> save to permanent memory
[FORGET: key]                -> delete from memory
[SCHEDULE: task | interval]  -> schedule task (daily/hourly/weekly/Xm/Xh)
[UNSCHEDULE: id]             -> cancel scheduled task

=== THINKING RULES ===
1. ALWAYS start with [THINK:] - think step by step out loud
2. Before any solution ask yourself:
   - What is the REAL problem behind the question?
   - Is there deeper context not mentioned?
   - What is the optimal long-term solution?
   - Are there risks or side effects?
3. For complex projects: break into clear phases
4. If context is unclear: ask ONE precise question

=== CODING & SELF-DEBUGGING RULES ===
PHASE 1 - PLANNING:
- Understand ALL requirements before writing one line
- Choose correct architecture (OOP / functional / modular)
- Identify required libraries upfront

PHASE 2 - WRITING:
- Write clean code with clear comments
- Use descriptive variable names
- Handle all edge cases
- Add comprehensive error handling

PHASE 3 - EXECUTION & SELF-REPAIR:
[RUN: pip install required_packages]
[RUN: python file.py]

If error appears:
-> Read the full error carefully
-> Identify the exact line and root cause
-> Change only what is necessary
-> Re-execute
-> Repeat until complete success - NEVER give up

If same error appears 3 times:
-> Change strategy completely
-> Search for alternative solution
-> Try different library if needed

PHASE 4 - VERIFICATION:
- Confirm output is 100% correct
- Test multiple cases
- Provide clear summary of what was done

=== TEACHING RULES ===
1. Start with the big picture - what and why?
2. Move to details gradually
3. Use real concrete examples
4. Compare technical concepts to everyday things
5. Summarize key points at the end

Levels:
- Beginner: simple language + many examples + small steps
- Intermediate: concepts + practical application + best practices
- Advanced: technical depth + optimizations + comparisons

=== COMPLEX PROJECT RULES ===
STEP 1 - Full understanding (goal, user, constraints, platform)
STEP 2 - Architecture planning (structure, modules, integration points)
STEP 3 - Phased execution (MVP first, test each phase)
STEP 4 - Optimization (performance, security, features)

=== MEMORY RULES ===
Auto-save with [REMEMBER:]:
- User name and preferred language
- Preferred programming languages
- Current projects and their status
- Recurring errors and their solutions
- Long-term goals
- Any important info mentioned

=== PHILOSOPHY ===
You are a real thinking partner, not just a command executor.
Your goal is the CORRECT answer, not the fast one.
Every problem has a solution - your job is to find it no matter what.

If you don't know something -> search
If code fails -> fix it
If question is unclear -> clarify
If task is hard -> break it down
NEVER give up."""


def build_system(uid):
    mem = db_get_memory(uid)
    mem_str = ""
    if mem:
        mem_str = "\n\n=== PERSISTENT MEMORY (what you know about this user) ===\n"
        for k, v in mem.items():
            mem_str += f"- {k}: {v}\n"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return f"{SYSTEM_PROMPT}\n\nCurrent time: {now}{mem_str}"


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
        h = {"User-Agent": "Mozilla/5.0 Chrome/120"}
        r = requests.get(url.strip(), headers=h, timeout=20)
        t = r.text
        t = re.sub(r'<script[^>]*>.*?</script>', ' ', t, flags=re.DOTALL|re.I)
        t = re.sub(r'<style[^>]*>.*?</style>',  ' ', t, flags=re.DOTALL|re.I)
        t = re.sub(r'',               ' ', t, flags=re.DOTALL)
        t = re.sub(r'<[^>]+>',                  ' ', t)
        t = re.sub(r'&[a-z#0-9]+;',             ' ', t)
        t = re.sub(r'\s+',                       ' ', t).strip()
        return t[:6000] + ("...[truncated]" if len(t) > 6000 else "")
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
        return "TIMEOUT"
    except Exception as e:
        return f"RUN_ERROR: {e}"

def create_file(path, content):
    try:
        full = WORKDIR / path.strip().lstrip("/")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"CREATED: {full}"
    except Exception as e:
        return f"CREATE_ERROR: {e}"

def read_file(path):
    try:
        full = WORKDIR / path.strip().lstrip("/")
        return full.read_text(encoding="utf-8")[:5000]
    except Exception as e:
        return f"READ_ERROR: {e}"

def list_dir(path):
    try:
        target = WORKDIR / path.strip().lstrip("/") if path.strip() else WORKDIR
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"[DIR] {i.name}/" if i.is_dir() else f"[FILE] {i.name}" for i in items]
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

ERR_PATTERNS = [
    "traceback (most recent call last)", "syntaxerror", "nameerror",
    "typeerror", "valueerror", "importerror", "modulenotfounderror",
    "attributeerror", "stderr:\n", "create_error", "run_error"
]

def has_error(text):
    return any(p in text.lower() for p in ERR_PATTERNS)

def call_ai(messages, model):
    last_err = None
    for attempt in range(len(API_KEYS) * 2):
        key = get_next_key()
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://sakanferbot.railway.app",
                },
                json={"model": model, "messages": messages, "max_tokens": 4096},
                timeout=120,
            )
            data = resp.json()
            if "error" in data:
                err = str(data["error"])
                if any(x in err.lower() for x in ["rate limit", "quota", "429"]):
                    log.warning(f"Key rate limited, trying next ({attempt+1})")
                    last_err = err
                    time.sleep(1)
                    continue
                raise RuntimeError(f"API Error: {err}")
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            last_err = "timeout"
            time.sleep(2)
            continue
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            continue
    raise RuntimeError(f"All keys failed: {last_err}")

# ============================================================
# UPDATED RUN_AGENT (NO MORE INFINITE LOOPS)
# ============================================================
def run_agent(uid, user_msg, force_model=None):
    db_add_message(uid, "user", user_msg)
    model = force_model or get_model(user_msg)
    
    # Max 3 iterations for safety and speed
    for iteration in range(1, 4): 
        log.info(f"[uid={uid}] iter={iteration} model={model}")
        hist = db_get_history(uid)
        system = build_system(uid)
        
        try:
            resp = call_ai([{"role": "system", "content": system}] + hist, model)
        except Exception as e:
            log.error(f"AI error: {e}")
            return f"❌ API Error: {e}"

        processed = process_actions(resp, uid)
        
        if processed == resp:
            db_add_message(uid, "assistant", resp)
            return resp
            
        db_add_message(uid, "assistant", resp)
        if has_error(processed):
            db_add_message(uid, "user", f"=== Iteration {iteration} ERROR ===\n{processed[:1000]}\nFix it.")
            continue 
        else:
            db_add_message(uid, "user", f"Results:\n{processed[:2000]}\nSummarize now.")
            try:
                final = call_ai([{"role": "system", "content": system}] + db_get_history(uid), model)
                db_add_message(uid, "assistant", final)
                return final
            except:
                return processed[:2000]

    return "⚠️ Limit reached. Try a simpler question."

def analyze_image(image_bytes, caption, uid):
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "Describe this image in detail"
    db_add_message(uid, "user", f"[image] {prompt}")
    key = get_next_key()
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
        }, timeout=60,
    )
    data = resp.json()
    if "error" in data: return f"Error: {data['error']}"
    result = data["choices"][0]["message"]["content"]
    db_add_message(uid, "assistant", result)
    return result

def analyze_document(file_bytes, filename, caption, uid):
    save_path = WORKDIR / filename
    save_path.write_bytes(file_bytes)
    return run_agent(uid, f"File uploaded: {filename}. {caption or 'Read this file and summarize it'}", "anthropic/claude-sonnet-4-5")

BOT_BUILDER_PROMPT = """You are an expert Telegram bot developer.
Build a COMPLETE, production-ready Python bot using python-telegram-bot>=20.7.
- Use os.environ.get("TELEGRAM_TOKEN") for token
- Handle all edge cases
- Fully functional with zero modifications needed

Output EXACTLY this format:
===BOT_CODE===
[full python code]
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
[Arabic README with setup instructions]
===END==="""

def build_bot(description):
    messages = [
        {"role": "system", "content": BOT_BUILDER_PROMPT},
        {"role": "user", "content": f"Build this Telegram bot:\n{description}"}
    ]
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
        zf.writestr(f"{bot_name}/railway.toml",     parts.get("railway_toml", ""))
        zf.writestr(f"{bot_name}/README.md",        parts.get("readme", ""))
    buf.seek(0)
    return buf.read()

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "SakanferBot v5 Ready!\n\n"
        f"Keys: {len(API_KEYS)} active\n\n"
        "Search -> Gemini 2.0\n"
        "Coding -> Claude Sonnet 4.5\n"
        "Images -> GPT-4o\n"
        "/opus -> Claude Opus 4.6\n\n"
        "Commands:\n"
        "/opus [task] - hardest tasks\n"
        "/buildbot [description] - build a bot\n"
        "/memory - show memory\n"
        "/files - show files\n"
        "/schedules - show schedules\n"
        "/clear - clear history\n\n"
        "Talk to me in any language!"
    )

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_clear_history(update.effective_user.id)
    await update.message.reply_text("History cleared!")

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mem = db_get_memory(update.effective_user.id)
    res = "\n".join([f"- {k}: {v}" for k, v in mem.items()]) or "Empty"
    await update.message.reply_text(f"Memory:\n{res}")

async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Files:\n{list_dir('')}")

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scheds = db_list_schedules(update.effective_user.id)
    if not scheds:
        return await update.message.reply_text("No scheduled tasks")
    lines = [f"#{s['id']} - {s['task']}\nEvery {s['interval']}" for s in scheds]
    await update.message.reply_text("Schedules:\n\n" + "\n\n".join(lines))

async def cmd_opus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip() if ctx.args else ""
    if not args:
        return await update.message.reply_text("Usage: /opus [your hard task]")
    uid = update.effective_user.id
    status = await update.message.reply_text("Claude Opus 4.6 thinking...")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, args, "anthropic/claude-opus-4-6")
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"Error: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    status = await update.message.reply_text("Processing...")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, update.message.text, None)
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"Error: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    status = await update.message.reply_text("Analyzing image...")
    try:
        f = await (update.message.photo[-1]).get_file()
        b = await f.download_as_bytearray()
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_image, bytes(b), update.message.caption or "", uid)
        await status.edit_text(result[:4000])
    except Exception as e:
        await status.edit_text(f"Error: {e}")

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    status = await update.message.reply_text(f"Processing {doc.file_name}...")
    try:
        f = await doc.get_file()
        b = await f.download_as_bytearray()
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_document, bytes(b), doc.file_name,
            update.message.caption or "", uid)
        chunks = [result[i:i+4000] for i in range(0, max(len(result), 1), 4000)]
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await status.edit_text(f"Error: {e}")

async def cmd_build_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip() if ctx.args else ""
    if not args:
        return await update.message.reply_text("Usage: /buildbot [bot description]")
    status = await update.message.reply_text("Claude Opus building your bot...")
    try:
        parts = await asyncio.get_event_loop().run_in_executor(None, build_bot, args)
        bot_name = re.sub(r'[^a-z0-9_]', '_', args[:25].lower().replace(' ', '_'))
        zip_data = create_bot_zip(parts, bot_name)
        await status.edit_text(f"Bot ready: {bot_name}")
        await update.message.reply_document(
            document=io.BytesIO(zip_data), filename=f"{bot_name}.zip",
            caption="bot.py + requirements.txt + Procfile + railway.toml")
    except Exception as e:
        await status.edit_text(f"Error: {e}")

async def scheduler_job(ctx: ContextTypes.DEFAULT_TYPE):
    for s in db_get_due_schedules():
        uid, task = s["uid"], s["task"]
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, run_agent, uid, f"[Scheduled task]: {task}", None)
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"Scheduled task:\n{task}\n\n{result[:3800]}")
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        finally:
            db_update_schedule_next(s["id"], datetime.utcnow() + parse_interval(s["interval"]))

def main():
    db_init()
    log.info(f"SakanferBot starting | Keys={len(API_KEYS)}")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("memory",    cmd_memory))
    app.add_handler(CommandHandler("files",     cmd_files))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("buildbot",  cmd_build_bot))
    app.add_handler(CommandHandler("opus",      cmd_opus))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.job_queue.run_repeating(scheduler_job, interval=60, first=10)
    log.info("Bot ready!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
