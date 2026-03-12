"""
SakanferBot v9 - Full AI Agent on Telegram
24/7 on Railway | Smart Routing | Self-Debug | Tunisian Dialect
"""
import os, sys, re, time, logging, sqlite3, asyncio, base64, io, zipfile
import subprocess, requests
from datetime import datetime, timedelta
from pathlib import Path

# ── DEPENDENCIES CHECK ───────────────────────────────────────────────────────
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, MessageHandler, CommandHandler,
        CallbackQueryHandler, filters, ContextTypes
    )
    from duckduckgo_search import DDGS
except ImportError as e:
    print(f"[ERROR] Missing package: {e}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

# ── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("SakanferBot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── ENVIRONMENT VARIABLES ────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TG_TOKEN or TG_TOKEN == "YOUR_TOKEN_HERE":
    log.error("Set TELEGRAM_TOKEN in Railway Variables!")
    sys.exit(1)

_keys_raw = os.environ.get("OPENROUTER_KEYS", "").strip()
API_KEYS = [k.strip() for k in _keys_raw.split(",") if k.strip()]
if not API_KEYS:
    log.error("Set OPENROUTER_KEYS in Railway Variables!")
    sys.exit(1)

log.info(f"Bot starting | Keys={len(API_KEYS)}")

# ── PATHS ────────────────────────────────────────────────────────────────────
WORKDIR = Path("/tmp/sakanfer_work")
DB_PATH = Path("/tmp/sakanfer.db")
WORKDIR.mkdir(parents=True, exist_ok=True)

# ── KEY ROTATION ─────────────────────────────────────────────────────────────
_key_idx = 0
def next_key():
    global _key_idx
    key = API_KEYS[_key_idx % len(API_KEYS)]
    _key_idx += 1
    return key

# ── MODELS ───────────────────────────────────────────────────────────────────
MODELS = {
    "fast":   "google/gemini-2.0-flash-001",
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus":   "anthropic/claude-opus-4-6",
    "vision": "openai/gpt-4o",
}

def pick_model(text: str) -> str:
    t = text.lower()
    hard = ["full app","complete application","architecture","machine learning",
            "deep learning","system design","تطبيق كامل","مشروع كامل","application complete"]
    if any(k in t for k in hard): return "opus"
    code = ["code","python","javascript","html","css","sql","bash","bug","fix","error",
            "debug","function","class","import","script","api","bot","programme","erreur",
            "barmej","barnamej","برمج","كود","خطأ","5ata2","khata","app","application",
            "deploy","dockerfile","regex","algorithm","sort","search algo"]
    if any(k in t for k in code): return "sonnet"
    return "fast"

# ── DATABASE ─────────────────────────────────────────────────────────────────
def db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(uid, key) ON CONFLICT REPLACE
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            task TEXT NOT NULL,
            interval_str TEXT NOT NULL,
            next_run TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_msg ON messages(uid, id);
        CREATE INDEX IF NOT EXISTS ix_mem ON memory(uid);
        CREATE INDEX IF NOT EXISTS ix_sch ON schedules(uid, active);
        """)
    log.info("Database initialized")

def db_add_msg(uid: int, role: str, content: str):
    with db() as c:
        c.execute("INSERT INTO messages(uid,role,content) VALUES(?,?,?)",
                  (uid, role, str(content)[:4000]))
        c.execute("""DELETE FROM messages WHERE uid=? AND id NOT IN
                     (SELECT id FROM messages WHERE uid=? ORDER BY id DESC LIMIT 50)""",
                  (uid, uid))

def db_history(uid: int, n: int = 20):
    with db() as c:
        rows = c.execute(
            "SELECT role,content FROM messages WHERE uid=? ORDER BY id DESC LIMIT ?",
            (uid, n)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def db_clear(uid: int):
    with db() as c:
        c.execute("DELETE FROM messages WHERE uid=?", (uid,))

def db_set_mem(uid: int, key: str, val: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO memory(uid,key,value) VALUES(?,?,?)",
                  (uid, key, str(val)[:1000]))

def db_get_mem(uid: int) -> dict:
    with db() as c:
        rows = c.execute("SELECT key,value FROM memory WHERE uid=?", (uid,)).fetchall()
    return {r["key"]: r["value"] for r in rows}

def db_del_mem(uid: int, key: str):
    with db() as c:
        c.execute("DELETE FROM memory WHERE uid=? AND key=?", (uid, key))

def db_clear_mem(uid: int):
    with db() as c:
        c.execute("DELETE FROM memory WHERE uid=?", (uid,))

def db_add_schedule(uid: int, task: str, interval: str, next_run: datetime):
    with db() as c:
        c.execute("INSERT INTO schedules(uid,task,interval_str,next_run) VALUES(?,?,?,?)",
                  (uid, task, interval, next_run.isoformat()))

def db_due_schedules():
    now = datetime.utcnow().isoformat()
    with db() as c:
        rows = c.execute(
            "SELECT * FROM schedules WHERE active=1 AND next_run<=?", (now,)
        ).fetchall()
    return [dict(r) for r in rows]

def db_update_schedule(sid: int, next_run: datetime):
    with db() as c:
        c.execute("UPDATE schedules SET next_run=? WHERE id=?",
                  (next_run.isoformat(), sid))

def db_list_schedules(uid: int):
    with db() as c:
        rows = c.execute(
            "SELECT id,task,interval_str,next_run FROM schedules WHERE uid=? AND active=1",
            (uid,)).fetchall()
    return [dict(r) for r in rows]

def db_del_schedule(sid: int, uid: int):
    with db() as c:
        c.execute("UPDATE schedules SET active=0 WHERE id=? AND uid=?", (sid, uid))

# ── LANGUAGE DETECTION ───────────────────────────────────────────────────────
def detect_lang(text: str) -> str:
    t = (text or "").lower()
    tn_signals = [
        "chnahwa","bark ","3lah","kifesh","ya5i","sahbi","wesh","barcha","chwaya",
        "taw ","fama ","ma3nd","nheb ","huni","barka","yezzi","chkoun","3lech","9al ",
        "lazem","5ater","n3mel","ykhdem","mochkla","aslema","brabi","bch ","mrigel",
        "9rib ","mazel","wela ","nqollek","3aychek","7al ","baro ","yser ","tfhem",
        "w zid","w aydan","manich","mafama","chwiya","barcha","inti ","wenti ",
        "ma t","ma n","ma y","ya zaama","ya3ni ","ki t","ki y",
    ]
    if any(s in t for s in tn_signals): return "tn"
    if re.search(r'[\u0600-\u06FF]', text): return "ar"
    fr_signals = [" je "," tu "," il "," nous "," vous ","c'est","j'ai","je veux",
                  "tu peux","comment faire","pourquoi ","qu'est","quel "]
    if any(s in " " + t + " " for s in fr_signals): return "fr"
    return "en"

# ── SYSTEM PROMPT ────────────────────────────────────────────────────────────
def build_system(uid: int, user_msg: str, model: str) -> str:
    lang = detect_lang(user_msg)
    mem = db_get_mem(uid)

    # Language block
    if lang == "tn":
        lang_block = (
            "LANGUE DETECTEE: TUNISIEN FRANCO-ARABE\n"
            "REPONDS UNIQUEMENT EN FRANCO-ARABE TUNISIEN.\n"
            "Parle comme un vrai ami tunisien — naturel, chaleureux, direct.\n"
            "Utilise: chnahwa, bark, 3lah, kifesh, ya5i/sahbi, barcha, chwaya,\n"
            "taw, fama, ma3ndich, nheb, huni, barka, yezzi, mochkla, 7al,\n"
            "5ater, n3mel, ykhdem, lazem, wela, ama, ki, 3lech, 9al,\n"
            "nqollek, aslema, brabi, bch, mrigel, 9rib, mazel, 3aychek.\n"
            "Mixes des mots français naturellement (normal, voilà, exactement, etc.)\n"
            "JAMAIS égyptien, syrien, golfe, ou arabe classique. JAMAIS.\n"
            "Termes tech: dis-les en français/anglais, explique en tunisien."
        )
    elif lang == "ar":
        lang_block = "اللغة: عربية\nأجب بالعربية الواضحة."
    elif lang == "fr":
        lang_block = "LANGUE: FRANÇAIS\nRéponds entièrement en français."
    else:
        lang_block = "LANGUAGE: ENGLISH\nReply entirely in English."

    # Memory block
    mem_block = ""
    if mem:
        mem_block = "\n\n=== PERSISTENT MEMORY ===\n"
        for k, v in mem.items():
            mem_block += f"• {k}: {v}\n"

    return f"""You are Sakanfer — elite AI agent and expert software engineer running 24/7 on Telegram.

=== LANGUAGE (ABSOLUTE PRIORITY #1) ===
{lang_block}

=== IDENTITY & CORE STRENGTHS ===
#1 superpower: SOFTWARE ENGINEERING & CODING
You write complete, production-ready code. Zero placeholders. Zero TODOs.
You debug relentlessly until the code works 100%.
You are also excellent at: questions, research, translation, explanations, math.

=== AVAILABLE TOOLS ===
[THINK: reasoning]              → Deep analysis before acting (use always for complex tasks)
[REMEMBER: key | value]         → Save permanently to user memory
[FORGET: key]                   → Delete from memory
[SEARCH: query]                 → Web search via DuckDuckGo
[FETCH: url]                    → Fetch full webpage content
[RUN: bash_command]             → Execute shell command
[CREATE: filepath | content]    → Create a file
[READ: filepath]                → Read a file
[LIST: path]                    → List directory
[GENERATE_IMAGE: prompt]        → Generate image (Pollinations, free)
[CODE: lang | full_code]        → Write COMPLETE working code
[SCHEDULE: task | interval]     → Schedule recurring task (e.g. daily, hourly, 30m, 6h)
[UNSCHEDULE: id]                → Cancel scheduled task

=== SELF-DEBUGGING CODE SYSTEM ===
1. Write 100% COMPLETE code — no placeholders, no "add your logic here"
2. I execute code in sandbox and return the EXACT error if it fails
3. Read error carefully → identify ROOT cause → write COMPLETE fixed code
4. Retry until 100% working. Repeat up to {os.environ.get('MAX_ITER', '50')} times if needed.
5. Same error 3 times in a row → COMPLETELY change approach/strategy
6. NEVER give up. Every bug has a fix.

=== MULTI-PART TASK EXECUTION ===
If user asks for multiple things → execute ALL of them in order.
Track: [REMEMBER: current_task | description]
Never skip any part. Deliver everything requested.

=== AUTO-MEMORY ===
Proactively save: user name, language preference, current projects,
preferred programming language, errors+solutions found, goals.

Model: {model} | Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}{mem_block}"""

# ── TOOLS ────────────────────────────────────────────────────────────────────
def tool_search(query: str) -> str:
    try:
        results = []
        with DDGS() as d:
            for r in d.text(query.strip(), max_results=6):
                results.append(f"TITLE: {r['title']}\nSNIPPET: {r['body'][:350]}\nURL: {r['href']}")
        return "\n---\n".join(results) or "No results found"
    except Exception as e:
        return f"SEARCH_ERROR: {e}"

def tool_fetch(url: str) -> str:
    try:
        r = requests.get(url.strip(), headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        t = re.sub(r'<script[^>]*>.*?</script>', ' ', r.text, flags=re.DOTALL | re.I)
        t = re.sub(r'<style[^>]*>.*?</style>', ' ', t, flags=re.DOTALL | re.I)
        t = re.sub(r'<[^>]+>', ' ', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t[:5000]
    except Exception as e:
        return f"FETCH_ERROR: {e}"

def tool_run(cmd: str) -> str:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=str(WORKDIR), encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        if err and not out: return f"STDERR:\n{err[:2000]}"
        if err: return f"STDOUT:\n{out[:1500]}\nSTDERR:\n{err[:500]}"
        return out[:2500] or "done (no output)"
    except subprocess.TimeoutExpired:
        return "TIMEOUT (60s)"
    except Exception as e:
        return f"RUN_ERROR: {e}"

def tool_create(path: str, content: str) -> str:
    try:
        full = WORKDIR / path.strip().lstrip("/")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"CREATED: {full} ({full.stat().st_size} bytes)"
    except Exception as e:
        return f"CREATE_ERROR: {e}"

def tool_read(path: str) -> str:
    try:
        full = WORKDIR / path.strip().lstrip("/")
        return full.read_text(encoding="utf-8", errors="replace")[:4000]
    except Exception as e:
        return f"READ_ERROR: {e}"

def tool_list(path: str = "") -> str:
    try:
        target = WORKDIR / path.strip().lstrip("/") if path.strip() else WORKDIR
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{'[DIR]' if i.is_dir() else '[FILE]'} {i.name}" for i in items]
        return "\n".join(lines) or "(empty)"
    except Exception as e:
        return f"LIST_ERROR: {e}"

def tool_genimage(prompt: str) -> str:
    """Returns image URL from Pollinations (free, no key needed)"""
    encoded = requests.utils.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=512&height=512&nologo=true&enhance=true&seed={int(time.time())}"

def parse_interval(s: str) -> timedelta:
    s = s.lower().strip()
    if s == "daily":   return timedelta(days=1)
    if s == "hourly":  return timedelta(hours=1)
    if s == "weekly":  return timedelta(weeks=1)
    m = re.match(r"^(\d+)m$", s)
    if m: return timedelta(minutes=int(m.group(1)))
    h = re.match(r"^(\d+)h$", s)
    if h: return timedelta(hours=int(h.group(1)))
    d = re.match(r"^(\d+)d$", s)
    if d: return timedelta(days=int(d.group(1)))
    return timedelta(hours=1)

# ── PROCESS AI TOOL CALLS ────────────────────────────────────────────────────
ERROR_PATTERNS = [
    "traceback (most recent call last)", "syntaxerror:", "nameerror:",
    "typeerror:", "valueerror:", "importerror:", "modulenotfounderror:",
    "attributeerror:", "indentationerror:", "stderr:\n", "create_error",
    "run_error", "fetch_error", "search_error", "error:", "exception:"
]

def has_error(text: str) -> bool:
    return any(p in text.lower() for p in ERROR_PATTERNS)

def process_tools(text: str, uid: int) -> tuple[str, bool]:
    """Process tool calls in AI response. Returns (processed_text, had_actions)"""
    out = text
    had = False

    # THINK
    for t in re.findall(r'\[THINK:\s*([\s\S]+?)\]', text):
        out = out.replace(f"[THINK: {t}]", f"\n💭 {t.strip()}\n")
        had = True

    # SEARCH
    for q in re.findall(r'\[SEARCH:\s*([\s\S]+?)\]', text):
        result = tool_search(q)
        out = out.replace(f"[SEARCH: {q}]", f"\n🔍 Search results:\n{result}\n")
        had = True

    # FETCH
    for u in re.findall(r'\[FETCH:\s*(.+?)\]', text):
        result = tool_fetch(u)
        out = out.replace(f"[FETCH: {u}]", f"\n📄 Page content:\n{result}\n")
        had = True

    # RUN
    for c in re.findall(r'\[RUN:\s*([\s\S]+?)\]', text):
        result = tool_run(c)
        out = out.replace(f"[RUN: {c}]", f"\n💻 Output:\n{result}\n")
        had = True

    # CREATE
    for m in re.finditer(r'\[CREATE:\s*(.+?)\s*\|\s*([\s\S]+?)\]', text):
        result = tool_create(m.group(1), m.group(2))
        out = out.replace(m.group(0), f"\n{result}\n")
        had = True

    # READ
    for fp in re.findall(r'\[READ:\s*(.+?)\]', text):
        result = tool_read(fp)
        out = out.replace(f"[READ: {fp}]", f"\n📁 File:\n{result}\n")
        had = True

    # LIST
    for dp in re.findall(r'\[LIST:\s*(.*?)\]', text):
        result = tool_list(dp)
        out = out.replace(f"[LIST: {dp}]", f"\n📂 Dir:\n{result}\n")
        had = True

    # REMEMBER
    for m in re.finditer(r'\[REMEMBER:\s*([^\|]+?)\s*\|\s*([\s\S]+?)\]', text):
        k, v = m.group(1).strip(), m.group(2).strip()
        db_set_mem(uid, k, v)
        out = out.replace(m.group(0), f"\n💾 Saved: {k}\n")
        had = True

    # FORGET
    for k in re.findall(r'\[FORGET:\s*(.+?)\]', text):
        db_del_mem(uid, k.strip())
        out = out.replace(f"[FORGET: {k}]", f"\n🗑️ Forgot: {k.strip()}\n")
        had = True

    # GENERATE_IMAGE (handled separately — returns URL)
    for prompt in re.findall(r'\[GENERATE_IMAGE:\s*([\s\S]+?)\]', text):
        url = tool_genimage(prompt.strip())
        out = out.replace(f"[GENERATE_IMAGE: {prompt}]", f"\n🎨 IMAGE:{url}\n")
        had = True

    # SCHEDULE
    for m in re.finditer(r'\[SCHEDULE:\s*([\s\S]+?)\s*\|\s*(.+?)\]', text):
        task, interval = m.group(1).strip(), m.group(2).strip()
        next_run = datetime.utcnow() + parse_interval(interval)
        db_add_schedule(uid, task, interval, next_run)
        out = out.replace(m.group(0), f"\n⏰ Scheduled: '{task}' every {interval}\n")
        had = True

    # UNSCHEDULE
    for sid in re.findall(r'\[UNSCHEDULE:\s*(\d+)\]', text):
        try:
            db_del_schedule(int(sid), uid)
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\n✅ Cancelled #{sid}\n")
        except Exception as e:
            out = out.replace(f"[UNSCHEDULE: {sid}]", f"\n❌ Error: {e}\n")
        had = True

    return out, had

# ── AI API ───────────────────────────────────────────────────────────────────
def call_ai(messages: list, model_key: str, max_retries: int = 3) -> str:
    model = MODELS.get(model_key, MODELS["fast"])
    last_err = None

    for attempt in range(max_retries * len(API_KEYS)):
        key = next_key()
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://sakanferbot.app",
                    "X-Title": "SakanferBot",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 4096,
                    "temperature": 0.75,
                },
                timeout=90,
            )
            data = resp.json()

            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if any(x in msg.lower() for x in ["rate limit", "quota", "429", "too many"]):
                    last_err = msg
                    time.sleep(2)
                    continue
                raise RuntimeError(f"API Error: {msg}")

            if not resp.ok:
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code in (401, 403):
                    continue  # try next key
                time.sleep(1)
                continue

            content = data["choices"][0]["message"]["content"]
            return content

        except requests.exceptions.Timeout:
            last_err = "Request timeout (90s)"
            time.sleep(3)
            continue
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            continue

    raise RuntimeError(f"All API attempts failed: {last_err}")

# ── IMAGE ANALYSIS ────────────────────────────────────────────────────────────
def analyze_image(image_bytes: bytes, caption: str, uid: int) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "Describe this image in detail. What do you see?"

    key = next_key()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": MODELS["vision"],
            "max_tokens": 2000,
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
    data = resp.json()
    if "error" in data:
        err = data["error"]
        return f"❌ Vision error: {err.get('message', str(err)) if isinstance(err, dict) else err}"
    return data["choices"][0]["message"]["content"]

# ── MAIN AGENT LOOP ───────────────────────────────────────────────────────────
MAX_ITER = int(os.environ.get("MAX_ITER", "50"))

def run_agent(uid: int, user_msg: str, force_model: str | None = None) -> list[dict]:
    """Run the agent loop. Returns list of {type, content} to send."""
    db_add_msg(uid, "user", user_msg)
    model = force_model or pick_model(user_msg)

    results = []
    iteration = 0
    last_error = ""
    same_error_count = 0
    fix_mode = False

    while iteration < MAX_ITER:
        iteration += 1
        log.info(f"[uid={uid}] iter={iteration} model={model}")

        # Escalate model on repeated failures
        if same_error_count >= 3 and model == "sonnet":
            model = "opus"
            log.info(f"[uid={uid}] Escalating to Opus")

        # Build messages
        history = db_history(uid, 20)
        system = build_system(uid, user_msg, model)
        msgs = [{"role": "system", "content": system}] + history

        if fix_mode and last_error:
            fix_prompt = (
                f"=== CODE ERROR — Attempt {iteration} ===\n{last_error}\n\n"
                + (
                    "⚠️ SAME ERROR 3x! COMPLETELY change your approach. Different strategy."
                    if same_error_count >= 3
                    else "Fix this error. Write the COMPLETE corrected code."
                )
            )
            msgs.append({"role": "user", "content": fix_prompt})

        # Call AI
        try:
            raw = call_ai(msgs, model)
        except Exception as e:
            log.error(f"AI call failed: {e}")
            results.append({"type": "error", "content": f"❌ API Error: {e}\n\nCheck your keys in Railway Variables."})
            return results

        # Process tools
        processed, had_actions = process_tools(raw, uid)

        if had_actions:
            db_add_msg(uid, "assistant", raw)

            # Check for image URL in processed output
            img_match = re.search(r'🎨 IMAGE:(https://[^\s\n]+)', processed)
            if img_match:
                img_url = img_match.group(1)
                processed_clean = re.sub(r'🎨 IMAGE:https://[^\s\n]+', '', processed).strip()
                if processed_clean:
                    results.append({"type": "text", "content": clean_text(processed_clean)})
                results.append({"type": "image_url", "content": img_url})

            elif has_error(processed):
                # Code/command error — retry
                last_err_new = processed[:800]
                if last_err_new == last_error:
                    same_error_count += 1
                else:
                    same_error_count = 1
                    last_error = last_err_new
                fix_mode = True
                db_add_msg(uid, "user", f"Error output:\n{processed[:1500]}\nFix it now.")
                time.sleep(1)
                continue

            else:
                # Success with tools — get summary
                db_add_msg(uid, "user",
                    f"Results:\n{processed[:2500]}\n\nSummarize the results to the user in their language.")
                try:
                    hist2 = db_history(uid, 18)
                    sys2 = build_system(uid, user_msg, model)
                    summary = call_ai([{"role": "system", "content": sys2}] + hist2, model)
                    db_add_msg(uid, "assistant", summary)
                    results.append({"type": "text", "content": clean_text(summary)})
                except Exception:
                    results.append({"type": "text", "content": clean_text(processed[:3000])})
                return results

        else:
            # Pure text response
            db_add_msg(uid, "assistant", raw)
            results.append({"type": "text", "content": clean_text(raw)})
            return results

    # Max iterations reached
    results.append({
        "type": "error",
        "content": f"⚠️ Reached {MAX_ITER} iterations.\nLast error:\n{last_error[:300]}\n\nTry breaking the task into smaller steps."
    })
    return results

def clean_text(text: str) -> str:
    """Clean text for Telegram — remove excess whitespace, limit length"""
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:4000]

# ── BOT BUILDER ───────────────────────────────────────────────────────────────
BOT_BUILDER_SYS = """You are an expert Telegram bot developer.
Build a COMPLETE, production-ready Python Telegram bot.
Use python-telegram-bot>=20.7. Use os.environ.get("TELEGRAM_TOKEN") for token.

Output EXACTLY in this format:
===BOT_CODE===
[complete bot.py code]
===REQUIREMENTS===
[one package per line]
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

def build_bot(description: str) -> dict:
    messages = [
        {"role": "system", "content": BOT_BUILDER_SYS},
        {"role": "user", "content": f"Build this Telegram bot:\n{description}"}
    ]
    text = call_ai(messages, "opus")

    def extract(start, end):
        m = re.search(rf'{re.escape(start)}\n([\s\S]+?)\n{re.escape(end)}', text)
        return m.group(1).strip() if m else ""

    return {
        "bot_code":    extract("===BOT_CODE===",     "===REQUIREMENTS==="),
        "requirements":extract("===REQUIREMENTS===", "===PROCFILE==="),
        "procfile":    extract("===PROCFILE===",     "===RAILWAY_TOML==="),
        "railway_toml":extract("===RAILWAY_TOML===", "===README==="),
        "readme":      extract("===README===",       "===END==="),
    }

def create_bot_zip(parts: dict, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/bot.py",           parts.get("bot_code", ""))
        zf.writestr(f"{name}/requirements.txt", parts.get("requirements", ""))
        zf.writestr(f"{name}/Procfile",         parts.get("procfile", "worker: python bot.py"))
        zf.writestr(f"{name}/railway.toml",     parts.get("railway_toml", ""))
        zf.writestr(f"{name}/README.md",        parts.get("readme", ""))
    buf.seek(0)
    return buf.read()

# ── SEND HELPERS ─────────────────────────────────────────────────────────────
async def send_results(update: Update, results: list[dict], status_msg=None):
    """Send agent results to user"""
    if status_msg:
        try: await status_msg.delete()
        except: pass

    for r in results:
        if r["type"] == "text":
            chunks = chunk_text(r["content"])
            for i, chunk in enumerate(chunks):
                try:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(chunk)

        elif r["type"] == "image_url":
            try:
                await update.message.reply_photo(
                    photo=r["content"],
                    caption="🎨 Generated by Pollinations AI"
                )
            except Exception:
                await update.message.reply_text(f"🎨 Image: {r['content']}")

        elif r["type"] == "error":
            await update.message.reply_text(r["content"])

def chunk_text(text: str, size: int = 4000) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:size])
        text = text[size:]
    return chunks

# ── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "Sahbi"
    lang = detect_lang(update.message.text or "")

    if lang == "tn":
        msg = (
            f"🟢 Aslema {name}!\n\n"
            f"Ana Sakanfer — AI Agent mte3ek, 24/7 huni.\n\n"
            f"🔑 Keys: {len(API_KEYS)} active\n\n"
            f"📋 *Commandes:*\n"
            f"/opus [task] — Claude Opus (mhemmet se3ba)\n"
            f"/buildbot [desc] — Build Telegram bot\n"
            f"/schedules — Tara les taches planifies\n"
            f"/memory — Mémoire mte3i\n"
            f"/clearmem — Clear memory\n"
            f"/clear — Clear historique\n\n"
            f"Nheblek! 7kili bch t7eb 🚀"
        )
    else:
        msg = (
            f"🟢 Welcome {name}!\n\n"
            f"I'm Sakanfer — your 24/7 AI Agent.\n\n"
            f"🔑 Keys: {len(API_KEYS)} active\n\n"
            f"📋 *Commands:*\n"
            f"/opus [task] — Claude Opus for hard tasks\n"
            f"/buildbot [desc] — Build a Telegram bot\n"
            f"/schedules — View scheduled tasks\n"
            f"/memory — View memory\n"
            f"/clearmem — Clear memory\n"
            f"/clear — Clear chat history\n\n"
            f"Talk to me in any language! 🚀"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db_clear(uid)
    lang = detect_lang(update.message.text or "")
    if lang == "tn":
        await update.message.reply_text("✅ L'historique effacé!")
    else:
        await update.message.reply_text("✅ History cleared!")

async def cmd_clearmem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db_clear_mem(uid)
    await update.message.reply_text("✅ Memory cleared!")

async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mem = db_get_mem(uid)
    if not mem:
        await update.message.reply_text("💾 No memories saved yet.")
        return
    lines = [f"• *{k}*: {v}" for k, v in mem.items()]
    await update.message.reply_text(
        "💾 *Memory:*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    scheds = db_list_schedules(uid)
    if not scheds:
        await update.message.reply_text("📅 No active scheduled tasks.")
        return
    lines = [f"• #{s['id']}: {s['task']} (every {s['interval_str']})" for s in scheds]
    await update.message.reply_text(
        "📅 *Scheduled Tasks:*\n\n" + "\n".join(lines) +
        "\n\nTo cancel: tell me to unschedule #ID",
        parse_mode="Markdown"
    )

async def cmd_opus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.message.reply_text("Usage: /opus [your hard task]")
        return

    status = await update.message.reply_text("👑 Claude Opus 4.6 thinking...")
    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, text, "opus"
        )
        await send_results(update, results, status)
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

async def cmd_buildbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    desc = " ".join(ctx.args).strip() if ctx.args else ""
    if not desc:
        await update.message.reply_text("Usage: /buildbot [describe your bot]")
        return

    status = await update.message.reply_text(
        "🤖 Claude Opus building your bot...\n⏱️ Est. 30-60s"
    )
    try:
        parts = await asyncio.get_event_loop().run_in_executor(None, build_bot, desc)
        bot_name = re.sub(r'[^a-z0-9_]', '_', desc[:25].lower().replace(' ', '_'))
        zip_data = create_bot_zip(parts, bot_name)
        await status.delete()
        await update.message.reply_document(
            document=io.BytesIO(zip_data),
            filename=f"{bot_name}.zip",
            caption=(
                f"✅ Bot *{bot_name}* ready!\n\n"
                f"📦 Contains:\n"
                f"• bot.py\n• requirements.txt\n• Procfile\n• railway.toml\n• README.md\n\n"
                f"🚀 Upload to GitHub → Deploy on Railway"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        await status.edit_text(f"❌ Build failed: {e}")

# ── MESSAGE HANDLERS ──────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""

    # Estimate time
    model = pick_model(text)
    model_names = {"fast": "Gemini Flash", "sonnet": "Claude Sonnet", "opus": "Claude Opus", "vision": "GPT-4o"}
    est = {"fast": "3-8s", "sonnet": "8-20s", "opus": "20-45s", "vision": "5-15s"}[model]

    status = await update.message.reply_text(
        f"⚡ {model_names[model]} thinking... (~{est})"
    )

    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, text, None
        )
        await send_results(update, results, status)
    except Exception as e:
        log.exception(f"Agent error: {e}")
        try: await status.delete()
        except: pass
        await update.message.reply_text(f"❌ Unexpected error: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    caption = update.message.caption or ""
    status = await update.message.reply_text("🖼 GPT-4o analyzing image...")
    try:
        file = await update.message.photo[-1].get_file()
        img_bytes = await file.download_as_bytearray()
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_image, bytes(img_bytes), caption, uid
        )
        await status.delete()
        # Save to history and run agent for follow-up
        db_add_msg(uid, "user", f"[Image analysis] Caption: {caption}\nAnalysis: {result}")
        db_add_msg(uid, "assistant", result)
        await update.message.reply_text(result[:4000])
    except Exception as e:
        await status.edit_text(f"❌ Image error: {e}")

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    fname = doc.file_name or "file"
    status = await update.message.reply_text(f"📄 Processing {fname}...")
    try:
        file = await doc.get_file()
        data = await file.download_as_bytearray()
        text_content = data.decode("utf-8", errors="replace")[:5000]
        user_msg = f"File uploaded: {fname}\n\nContent:\n{text_content}\n\nUser caption: {update.message.caption or 'analyze this file'}"
        results = await asyncio.get_event_loop().run_in_executor(
            None, run_agent, uid, user_msg, None
        )
        await send_results(update, results, status)
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
async def scheduler_tick(ctx: ContextTypes.DEFAULT_TYPE):
    due = db_due_schedules()
    for s in due:
        uid, task, interval = s["uid"], s["task"], s["interval_str"]
        log.info(f"Running scheduled task for uid={uid}: {task}")
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, run_agent, uid, f"[Scheduled task]: {task}", None
            )
            for r in results:
                if r["type"] == "text":
                    chunks = chunk_text(f"⏰ *Scheduled:* {task}\n\n{r['content']}")
                    for chunk in chunks:
                        try:
                            await ctx.bot.send_message(
                                chat_id=uid, text=chunk, parse_mode="Markdown"
                            )
                        except Exception:
                            await ctx.bot.send_message(chat_id=uid, text=chunk)
                elif r["type"] == "image_url":
                    await ctx.bot.send_photo(chat_id=uid, photo=r["content"])
        except Exception as e:
            log.error(f"Scheduled task error: {e}")
        finally:
            db_update_schedule(s["id"], datetime.utcnow() + parse_interval(interval))

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    db_init()
    log.info(f"SakanferBot starting | Token={TG_TOKEN[:10]}... | Keys={len(API_KEYS)} | MaxIter={MAX_ITER}")

    app = ApplicationBuilder().token(TG_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("clear",      cmd_clear))
    app.add_handler(CommandHandler("clearmem",   cmd_clearmem))
    app.add_handler(CommandHandler("memory",     cmd_memory))
    app.add_handler(CommandHandler("schedules",  cmd_schedules))
    app.add_handler(CommandHandler("opus",       cmd_opus))
    app.add_handler(CommandHandler("buildbot",   cmd_buildbot))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Scheduler — runs every 60 seconds
    app.job_queue.run_repeating(scheduler_tick, interval=60, first=15)

    log.info("Bot ready! Polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
