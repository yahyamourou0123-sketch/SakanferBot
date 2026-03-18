#!/usr/bin/env python3
# ================================================================
#  🤖 CODER BOT — بوت المبرمج الذكي الكامل
#  ✅ يكتب كود + يصلح نفسه بلا حد حتى يشتغل
#  ✅ يرد بنفس لغة المستخدم (عربي، تونسي، فرنسي، إنجليزي...)
#  ✅ ينشر الكود تلقائياً على GitHub
#  ✅ يتعلم على المستخدم (ذاكرة دائمة)
#  ✅ يدردش + يبرمج
#  ✅ Fallback تلقائي من الأقوى للأضعف
#  ✅ مخصص للتشغيل في Termux على الهاتف
# ================================================================

import os, sys, subprocess, time, re, logging, json
import requests
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction

# ── CONFIG ─────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")
AUTHORIZED_USER = int(os.getenv("AUTHORIZED_USER", "0"))
EXEC_TIMEOUT    = 25

# ── 7 مفاتيح OpenRouter — rotation تلقائي لمضاعفة الحد المجاني ──
_raw_keys = [
    os.getenv("OPENROUTER_KEY_1", os.getenv("OPENROUTER_KEY", "")),
    os.getenv("OPENROUTER_KEY_2", ""),
    os.getenv("OPENROUTER_KEY_3", ""),
    os.getenv("OPENROUTER_KEY_4", ""),
    os.getenv("OPENROUTER_KEY_5", ""),
    os.getenv("OPENROUTER_KEY_6", ""),
    os.getenv("OPENROUTER_KEY_7", ""),
]
# فلتر المفاتيح الفارغة فقط
OPENROUTER_KEYS = [k.strip() for k in _raw_keys if k.strip()]
if not OPENROUTER_KEYS:
    OPENROUTER_KEYS = [""]  # placeholder يظهر خطأ واضح

# state للـ rotation
_key_index     = 0
_key_usage     = {i: 0 for i in range(len(OPENROUTER_KEYS))}  # عدد استخدام كل مفتاح
_key_exhausted = {i: False for i in range(len(OPENROUTER_KEYS))}  # مفتاح انتهى حده

def get_next_key() -> tuple[str, int]:
    """يرجع (api_key, key_index) — يدور على المفاتيح تلقائياً"""
    global _key_index
    # ابحث عن مفتاح غير منتهي
    for _ in range(len(OPENROUTER_KEYS)):
        idx = _key_index % len(OPENROUTER_KEYS)
        if not _key_exhausted.get(idx, False):
            _key_usage[idx] = _key_usage.get(idx, 0) + 1
            return OPENROUTER_KEYS[idx], idx
        _key_index += 1
    # لو كلهم انتهوا — reset وابدأ من أول
    for i in range(len(OPENROUTER_KEYS)):
        _key_exhausted[i] = False
    _key_index = 0
    return OPENROUTER_KEYS[0], 0

def mark_key_exhausted(idx: int):
    """علّم مفتاح كمنتهي الحد"""
    _key_exhausted[idx] = True
    log.warning(f"⚠️ المفتاح #{idx+1} انتهى حده — ينتقل للتالي")

# ── نماذج مرتبة الأقوى → الأضعف في البرمجة (SWE-bench) ─────────
MODELS = [
    {"id": "anthropic/claude-opus-4-5",         "name": "Claude Opus 4.5",   "tier": "🔥"},
    {"id": "anthropic/claude-sonnet-4-5",        "name": "Claude Sonnet 4.5", "tier": "💪"},
    {"id": "google/gemini-2.5-pro-preview-05-06","name": "Gemini 2.5 Pro",    "tier": "🌐"},
    {"id": "google/gemini-2.0-flash-001",        "name": "Gemini Flash 2.0",  "tier": "⚡"},
    {"id": "deepseek/deepseek-chat-v3-0324",     "name": "DeepSeek V3",       "tier": "🧠"},
    {"id": "qwen/qwen3-235b-a22b",               "name": "Qwen3 235B",        "tier": "🔓"},
    {"id": "meta-llama/llama-3.3-70b-instruct",  "name": "Llama 3.3 70B",     "tier": "🔄"},
]

HOME         = Path.home()
MEMORY_FILE  = HOME / "coderbot_memory.json"
PROJECTS_DIR = HOME / "coderbot_projects"
PROJECTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(HOME / "coderbot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── ذاكرة المستخدم ──────────────────────────────────────────────
def load_memory():
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except:
            pass
    return {
        "user_name": "",
        "projects": [],
        "conversation_history": [],
        "stats": {"requests": 0, "fixes": 0, "successes": 0},
    }

def save_memory(mem):
    MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2))

memory = load_memory()

# ── كشف اللغة ───────────────────────────────────────────────────
def detect_language(text):
    arabic_chars  = len(re.findall(r'[\u0600-\u06FF]', text))
    latin_chars   = len(re.findall(r'[a-zA-Z]', text))
    tunisian_words = ['كيفاش','لاباس','برشا','يزي','توا','نحب','واش','هكا','مزيان','نكمل','بش']
    french_words   = ['bonjour','merci','je ','tu ','nous','avec','pour','dans','faire','créer']

    if sum(1 for w in tunisian_words if w in text) >= 2:
        return "tunisian"
    if arabic_chars > latin_chars:
        return "arabic"
    if sum(1 for w in french_words if w in text.lower()) >= 2:
        return "french"
    return "english"

def lang_system(lang):
    return {
        "tunisian": "رد بالدارجة التونسية (Franco-Arabic): واش نجمت، يزي، نكملو، هيا بيا، برشا...",
        "arabic":   "رد بالعربية الواضحة والطبيعية.",
        "french":   "Réponds en français naturel.",
        "english":  "Reply in natural English.",
    }.get(lang, "رد بالعربية.")

# ── AI Call مع Fallback على النماذج + rotation على المفاتيح ──────
def ask_ai(messages, start_idx=0):
    for i in range(start_idx, len(MODELS)):
        m = MODELS[i]
        tried_keys = set()
        while True:
            api_key, key_idx = get_next_key()
            if key_idx in tried_keys:
                break
            tried_keys.add(key_idx)
            try:
                r = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "termux://coderbot",
                        "X-Title": "CoderBot",
                    },
                    json={"model": m["id"], "messages": messages,
                          "max_tokens": 8000, "temperature": 0.1},
                    timeout=90
                )
                data = r.json()
                if "choices" in data and data["choices"]:
                    log.info(f"✅ {m['name']} | مفتاح #{key_idx+1}")
                    return data["choices"][0]["message"]["content"], i
                err = data.get("error", {}).get("message", "?")
                if any(x in err.lower() for x in ["rate", "quota", "limit", "insufficient", "credit"]):
                    mark_key_exhausted(key_idx)
                    continue
                log.warning(f"⚠️ {m['name']} مفتاح#{key_idx+1}: {err}")
            except Exception as e:
                log.warning(f"⚠️ {m['name']} مفتاح#{key_idx+1}: {e}")
            time.sleep(0.5)
        time.sleep(0.5)
    return "❌ كل النماذج والمفاتيح فشلت — تحقق من OPENROUTER_KEYS في .env", 0

# ── استخراج الكود ───────────────────────────────────────────────
def extract_code(text):
    for pat, lang in [
        (r"```(?:python|py)\n?(.*?)```", "python"),
        (r"```(?:javascript|js)\n?(.*?)```", "javascript"),
        (r"```(?:bash|sh)\n?(.*?)```", "bash"),
        (r"```(?:html)\n?(.*?)```", "html"),
        (r"```\w*\n?(.*?)```", "python"),
    ]:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip(), lang
    return text.strip(), "python"

# ── تشغيل الكود ────────────────────────────────────────────────
def run_code(code, lang):
    if lang == "html":
        return True, "✅ ملف HTML جاهز"
    suffix = {"python": ".py", "javascript": ".js", "bash": ".sh"}.get(lang, ".py")
    fpath = PROJECTS_DIR / f"test_{int(time.time())}{suffix}"
    fpath.write_text(code)
    cmd_map = {
        "python":     ["python3", str(fpath)],
        "javascript": ["node",    str(fpath)],
        "bash":       ["bash",    str(fpath)],
    }
    try:
        res = subprocess.run(cmd_map.get(lang, ["python3", str(fpath)]),
                             capture_output=True, text=True, timeout=EXEC_TIMEOUT)
        out = (res.stdout + res.stderr).strip()[:2000]
        return res.returncode == 0, out or "(لا output)"
    except subprocess.TimeoutExpired:
        return False, f"❌ انتهى الوقت ({EXEC_TIMEOUT}s)"
    except FileNotFoundError:
        return False, f"❌ {lang} غير مثبت — شغّل: pkg install python nodejs"
    except Exception as e:
        return False, f"❌ {e}"

# ── نشر على GitHub ──────────────────────────────────────────────
def publish_github(code, lang, name):
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        return False, "⚠️ أضف GITHUB_TOKEN و GITHUB_USERNAME في .env"
    import base64
    suffix = {"python":".py","javascript":".js","bash":".sh","html":".html"}.get(lang,".py")
    repo   = re.sub(r'[^a-z0-9-]', '-', name.lower())[:40] or "coderbot-project"
    hdrs   = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    r = requests.post("https://api.github.com/user/repos", headers=hdrs,
                      json={"name": repo, "description": "🤖 CoderBot", "private": False}, timeout=30)
    if r.status_code not in (201, 422):
        return False, f"❌ {r.json().get('message', r.status_code)}"

    r2 = requests.put(
        f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo}/contents/main{suffix}",
        headers=hdrs,
        json={"message": "🤖 CoderBot auto-commit", "content": base64.b64encode(code.encode()).decode()},
        timeout=30
    )
    if r2.status_code in (200, 201):
        return True, f"https://github.com/{GITHUB_USERNAME}/{repo}"
    return False, f"❌ {r2.json().get('message', r2.status_code)}"

# ── AGENT: يكتب → يجرب → يصلح بلا حد ──────────────────────────
async def coding_agent(update, context, request, lang):
    chat_id   = update.effective_chat.id
    model_idx = 0
    attempt   = 0
    messages  = [
        {"role": "system", "content": f"أنت مبرمج خبير. {lang_system(lang)}\n"
         "اكتب كود مكتمل داخل ```language...``` فقط. لا تتردد في الإصلاح."},
        {"role": "user",   "content": f"اكتب:\n{request}"}
    ]

    status = await context.bot.send_message(chat_id, "⚙️ أبدأ...")

    while True:
        attempt += 1
        m_name = f"{MODELS[model_idx]['tier']} {MODELS[model_idx]['name']}"
        await status.edit_text(
            f"{'✍️ يكتب' if attempt==1 else '🔧 يصلح'} | محاولة #{attempt} | {m_name}"
        )

        response, model_idx = ask_ai(messages, model_idx)
        if "❌ كل النماذج" in response:
            await status.edit_text(response)
            return

        code, lang_code = extract_code(response)

        await status.edit_text(f"🧪 يجرّب الكود | محاولة #{attempt}")
        success, output = run_code(code, lang_code)

        if success:
            memory["stats"]["successes"] += 1
            memory["projects"].append({
                "name": request[:50], "lang": lang_code,
                "date": datetime.now().strftime("%Y-%m-%d"), "attempts": attempt
            })
            save_memory(memory)

            github_line = ""
            if GITHUB_TOKEN:
                await status.edit_text("📤 ينشر على GitHub...")
                ok, result = publish_github(code, lang_code, request[:40])
                github_line = f"\n🔗 GitHub: {result}" if ok else f"\n⚠️ {result}"

            await status.delete()

            result_text = (
                f"✅ نجح في {attempt} {'محاولة' if attempt==1 else 'محاولات'}!\n"
                f"🤖 {m_name}\n"
                f"▶️ `{output[:250]}`{github_line}"
            )
            try:
                await context.bot.send_message(chat_id, f"```{lang_code}\n{code}\n```",
                                               parse_mode=ParseMode.MARKDOWN_V2)
            except:
                await context.bot.send_message(chat_id, f"الكود:\n{code}")
            await context.bot.send_message(chat_id, result_text, parse_mode=ParseMode.MARKDOWN)
            return

        else:
            memory["stats"]["fixes"] += 1
            save_memory(memory)
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user",
                              "content": f"فشل. الخطأ:\n```\n{output[:1200]}\n```\nصلّح الكود كاملاً."})
            if len(messages) > 22:
                messages = messages[:2] + messages[-12:]

# ── معالج الرسائل ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHORIZED_USER and update.effective_user.id != AUTHORIZED_USER:
        await update.message.reply_text("⛔ غير مصرح")
        return

    text = update.message.text.strip()
    lang = detect_language(text)
    memory["stats"]["requests"] += 1

    code_kw = ["اكتب","ابني","اعمل","انشئ","برمج","صمم","اصنع",
               "write","build","create","make","code","script",
               "écris","crée","نكتب","نعمل","نبني","يكتب","يعمل","بناء"]

    if any(kw in text.lower() for kw in code_kw):
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        await coding_agent(update, context, text, lang)
    else:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        user_ctx = f"المستخدم اسمه {memory['user_name']}. " if memory["user_name"] else ""
        if memory["projects"]:
            recent = ", ".join(p["name"] for p in memory["projects"][-3:])
            user_ctx += f"مشاريعه الأخيرة: {recent}."

        msgs = [{"role": "system", "content": f"أنت مساعد ذكي ومبرمج خبير. {lang_system(lang)} {user_ctx}"}]
        msgs += memory["conversation_history"][-8:]
        msgs.append({"role": "user", "content": text})

        resp, _ = ask_ai(msgs)

        memory["conversation_history"].append({"role": "user",      "content": text})
        memory["conversation_history"].append({"role": "assistant", "content": resp})
        if len(memory["conversation_history"]) > 50:
            memory["conversation_history"] = memory["conversation_history"][-40:]
        save_memory(memory)

        await update.message.reply_text(resp)

# ── أوامر ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "صاحبي"
    memory["user_name"] = name
    save_memory(memory)
    models_list = "\n".join(f"{i+1}. {m['tier']} {m['name']}" for i,m in enumerate(MODELS))
    await update.message.reply_text(
        f"🤖 *CoderBot جاهز يا {name}!*\n\n"
        "قولي شنو تبي أبني وأنا أكتب الكود، أجربه، وأصلحه بلا حد!\n\n"
        f"*النماذج (الأقوى → الأضعف):*\n{models_list}\n\n"
        "الأوامر: /status /projects /models /clear /keys /resetkeys",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = memory["stats"]
    await update.message.reply_text(
        f"📊 *إحصائياتك:*\n"
        f"• الطلبات: {s['requests']}\n"
        f"• مرات الإصلاح: {s['fixes']}\n"
        f"• النجاحات: {s['successes']}\n"
        f"• المشاريع المحفوظة: {len(memory['projects'])}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not memory["projects"]:
        await update.message.reply_text("📭 ما فيه مشاريع بعد")
        return
    lines = ["📁 *مشاريعك:*\n"]
    for p in memory["projects"][-10:]:
        lines.append(f"• {p['name']} ({p['lang']}) — {p['attempts']} محاولة")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🤖 *ترتيب النماذج:*\n"]
    for i, m in enumerate(MODELS, 1):
        lines.append(f"{i}. {m['tier']} {m['name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory["conversation_history"] = []
    save_memory(memory)
    await update.message.reply_text("🗑️ تم مسح سجل المحادثة")

async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"🔑 *حالة المفاتيح ({len(OPENROUTER_KEYS)} مفتاح):*\n"]
    for i, key in enumerate(OPENROUTER_KEYS):
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "غير محدد"
        status = "❌ منتهي" if _key_exhausted.get(i) else "✅ نشط"
        usage  = _key_usage.get(i, 0)
        lines.append(f"{i+1}. `{masked}` — {status} | استُخدم {usage}x")
    total = sum(_key_usage.values())
    lines.append(f"\n📊 إجمالي الطلبات: {total}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_resetkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for i in range(len(OPENROUTER_KEYS)):
        _key_exhausted[i] = False
        _key_usage[i] = 0
    await update.message.reply_text("🔄 تم reset كل المفاتيح!")

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ── MAIN ────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not OPENROUTER_KEY:
        print("❌ تحقق من TELEGRAM_TOKEN و OPENROUTER_KEY في ~/.env")
        sys.exit(1)
    print(f"🤖 CoderBot يبدأ | {len(MODELS)} نماذج | الذاكرة: {MEMORY_FILE}")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("models",   cmd_models))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("keys",      cmd_keys))
    app.add_handler(CommandHandler("resetkeys", cmd_resetkeys))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ جاهز!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
