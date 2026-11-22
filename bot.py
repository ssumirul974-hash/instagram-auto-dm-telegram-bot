"""Developed by Rohan 04
Instagram Auto DM Bot (Telegram) - fully button based, multi-user, admin panel.
Features: multi-account, auto DM (list / followers), single DM, auto-reply,
welcome DM for new followers, scheduled DM, spintax {hi|hello}, admin panel + broadcast."""
import asyncio, csv, html, io, json, logging, os, random, re, threading, time, uuid
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from instagrapi import Client
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as Kb, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMINS = {int(x) for x in (os.environ.get("ADMIN_IDS") or os.environ.get("OWNER_IDS", "")).replace(" ", "").split(",") if x}
fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "40"))
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "3"))
MIN_DELAY, MAX_DELAY = int(os.environ.get("MIN_DELAY", "45")), int(os.environ.get("MAX_DELAY", "120"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
DATA = Path(os.environ.get("DATA_FILE", "data.json"))
PORT = int(os.environ.get("PORT", "10000"))
DEV = os.environ.get("DEVELOPER_NAME", "Rohan 04")
COMMENT_LIMIT = int(os.environ.get("COMMENT_LIMIT", "60"))
FILES = DATA.parent / "files"

db = json.loads(DATA.read_text()) if DATA.exists() else {}
db.setdefault("users", {}); db.setdefault("schedules", [])
db.setdefault("settings", {"approval": os.environ.get("REQUIRE_APPROVAL") == "1", "maintenance": False})
clients, locks, jobs = {}, {}, {}
errlog = deque(maxlen=60)
FRESH = {"autoreply": False, "welcome": False, "list": [], "sent": {}, "daily": {}, "replied": {}, "fol_seen": [],
         "msg": "", "reply": "Thanks for your message! Main jaldi reply karunga 🙏", "wmsg": "Hi {username}, follow karne ke liye thanks! 🙌",
         "story": False, "sreply": "Story reply karne ke liye thanks! 🙌 Jaldi jawab dunga.",
         "deliver": False, "dtext": "", "dfiles": [], "dfollow": True, "dkw": [],
         "nofollow": "Pehle mujhe follow karo 🙏 phir dobara DM karo, tumhe turant mil jayega!",
         "cr": False, "crtext": "Thanks {username}! 🙌 Check your DM 📩", "crkw": [], "crdm": False,
         "c_done": [], "c_base": False, "cdaily": {}}


# ---------------- helpers ----------------
def save():
    tmp = DATA.with_suffix(".tmp"); tmp.write_text(json.dumps(db)); tmp.replace(DATA)


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def sent_today(a):
    return a["daily"].get(today(), 0)


def spin(t):
    while True:
        n = re.sub(r"\{([^{}|]*\|[^{}]*)\}", lambda m: random.choice(m.group(1).split("|")), t)
        if n == t:
            return t
        t = n


def render(tpl, username):
    return spin(tpl.replace("{username}", username).replace("{name}", username))


def A(key):
    uid, name = key.split(":", 1)
    acc = db["users"][uid]["accounts"][name]
    if "cr" not in acc:   # older saved accounts: add new fields
        for k, v in FRESH.items():
            acc.setdefault(k, json.loads(json.dumps(v)))
    return acc


def limit_for(key):
    return db["users"][key.split(":", 1)[0]].get("limit") or DAILY_LIMIT


def cur(u):
    uid = str(u.effective_user.id)
    x = db["users"][uid]
    return f"{uid}:{x['active']}" if x["active"] in x["accounts"] else None


def esc(t):
    return html.escape(str(t))


def short(t, n=60):
    return (t[:n] + "…") if len(t) > n else t


# ---------------- Instagram (blocking, run in thread) ----------------
def get_client(key):
    if key not in clients:
        cl = Client(); cl.delay_range = [2, 5]
        cl.login_by_sessionid(fernet.decrypt(A(key)["sid"].encode()).decode())
        clients[key] = cl
    return clients[key]


def lock(key):
    return locks.setdefault(key, threading.RLock())


def ig_send(key, username, text):
    try:
        with lock(key):
            cl = get_client(key)
            uid = cl.user_id_from_username(username.lstrip("@"))
            cl.direct_send(text, user_ids=[int(uid)])
    except Exception as e:
        if "login" in str(e).lower():
            clients.pop(key, None)
        raise
    a = A(key)
    a["sent"][username.lower()] = time.time()
    a["daily"] = {today(): sent_today(a) + 1}
    save()


def ig_followers(key, amount):
    with lock(key):
        cl = get_client(key)
        return [u.username for u in cl.user_followers(cl.user_id, amount=amount).values()]


def ig_new_followers(key):
    a = A(key)
    cur_f = ig_followers(key, 50)
    seen = a.get("fol_seen") or []
    a["fol_seen"] = list(dict.fromkeys(cur_f + seen))[:2000]
    save()
    return [x for x in cur_f if x not in seen] if seen else []   # first run = baseline only


def _follows_me(cl, pk):
    for fn in ("user_friendship_v1", "user_friendship"):
        try:
            return bool(getattr(cl, fn)(int(pk)).followed_by)
        except Exception as e:
            errlog.append(f"follow check {fn}: {e}")
    return True   # check not possible -> don't block delivery


def _deliver(cl, a, pk, username):
    if a["dtext"]:
        cl.direct_send(render(a["dtext"], username), user_ids=[int(pk)])
    for f in a["dfiles"]:
        p = Path(f["path"])
        if not p.exists():
            errlog.append(f"media missing {p}")
            continue
        (cl.direct_send_photo if f["type"] == "photo" else cl.direct_send_video)(p, user_ids=[int(pk)])


def ig_check_inbox(key):
    a, out = A(key), []
    with lock(key):
        cl = get_client(key)
        me = str(cl.user_id)
        threads = list(cl.direct_threads(amount=15, selected_filter="unread"))
        try:
            threads += list(cl.direct_pending_inbox(amount=15))   # message requests
        except Exception as e:
            errlog.append(f"{key} pending inbox: {e}")
        cool = lambda k, secs: time.time() - a["replied"].get(k, 0) >= secs
        mark = lambda k: a["replied"].__setitem__(k, time.time())
        for t in threads:
            if not t.messages or len(t.users) != 1:
                continue
            m, u = t.messages[0], t.users[0]
            if str(m.user_id) == me:
                continue
            pk, name, text = str(u.pk), u.username, (getattr(m, "text", "") or "").lower()
            story = getattr(m, "item_type", "") == "reel_share" or getattr(m, "reel_share", None) is not None
            try:
                if story and a["story"]:
                    if cool("s" + pk, 21600):
                        cl.direct_answer(t.id, render(a["sreply"], name)); mark("s" + pk); out.append(name)
                elif a["deliver"] and (not a["dkw"] or any(k in text for k in a["dkw"])):
                    if cool("d" + pk, 86400):
                        if a["dfollow"] and not _follows_me(cl, pk):
                            if cool("n" + pk, 600):
                                cl.direct_answer(t.id, render(a["nofollow"], name)); mark("n" + pk)
                        else:
                            _deliver(cl, a, pk, name); mark("d" + pk); out.append(name)
                elif a["autoreply"] and cool(pk, 86400):
                    cl.direct_answer(t.id, render(a["reply"], name)); mark(pk); out.append(name)
            except Exception as e:
                errlog.append(f"{key} inbox @{name}: {e}")
                if any(w in str(e).lower() for w in BLOCK_WORDS):
                    raise
    save()
    return out


def ig_comments(key):
    a, out, new_done, replies = A(key), [], [], 0
    with lock(key):
        cl = get_client(key)
        me, done, first = str(cl.user_id), set(a["c_done"]), not a["c_base"]
        try:
            for md in cl.user_medias(cl.user_id, amount=5):
                for cm in cl.media_comments(md.id, amount=30):
                    cid = str(cm.pk)
                    if cid in done:
                        continue
                    if first or str(cm.user.pk) == me:
                        new_done.append(cid); continue
                    if replies >= 5 or a["cdaily"].get(today(), 0) >= COMMENT_LIMIT:
                        continue   # leave for next round
                    new_done.append(cid)
                    if a["crkw"] and not any(k in (cm.text or "").lower() for k in a["crkw"]):
                        continue
                    name = cm.user.username
                    try:
                        cl.media_comment(md.id, render(a["crtext"], name), replied_to_comment_id=int(cm.pk))
                        if a["crdm"] and (a["dtext"] or a["dfiles"]):
                            _deliver(cl, a, cm.user.pk, name)
                    except Exception as e:
                        errlog.append(f"{key} comment @{name}: {e}")
                        if any(w in str(e).lower() for w in BLOCK_WORDS):
                            raise
                        continue
                    replies += 1
                    a["cdaily"] = {today(): a["cdaily"].get(today(), 0) + 1}
                    out.append(name)
                    time.sleep(random.uniform(5, 12))
            a["c_base"] = True
        finally:
            a["c_done"] = (a["c_done"] + new_done)[-3000:]
            save()
    return out


# ---------------- UI plumbing ----------------
def kb(*rows):
    return Kb([[B(t, callback_data=d) for t, d in r] for r in rows])


BACK = [("🏠 Main Menu", "m:main")]


async def show(u, c, text, markup=None):
    q = u.callback_query
    if q:
        try:
            return await q.edit_message_text(text, reply_markup=markup)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            pass
    await c.bot.send_message(u.effective_chat.id, text, reply_markup=markup)


async def say(c, uid, text, markup=None):
    try:
        await c.bot.send_message(int(uid), text, reply_markup=markup)
    except Exception as e:
        errlog.append(f"send {uid}: {e}")


async def register(u, c):
    tu, uid = u.effective_user, str(u.effective_user.id)
    new = uid not in db["users"]
    if new:
        db["users"][uid] = {"name": tu.full_name, "username": tu.username or "", "joined": time.time(), "banned": False,
                            "approved": not db["settings"]["approval"] or tu.id in ADMINS, "limit": 0, "active": "", "blocked": False, "accounts": {}}
    x = db["users"][uid]
    x.update(name=tu.full_name, username=tu.username or "", last_seen=time.time(), blocked=False)
    if new:
        save()
        mk = kb([("✅ Approve", f"ap:{uid}:1"), ("🚫 Ban", f"ap:{uid}:0")]) if not x["approved"] else None
        for a in ADMINS:
            await say(c, a, f"🆕 Naya user: {tu.full_name} @{tu.username or '-'} ({uid})", mk)
    return x


async def check(u, c, admin=False):
    x = await register(u, c)
    isadm = u.effective_user.id in ADMINS
    if admin and not isadm:
        return False
    if not isadm:
        m = u.effective_message
        if x["banned"]:
            await m.reply_text("🚫 Tum ban ho. Admin se baat karo."); return False
        if db["settings"]["maintenance"]:
            await m.reply_text("🛠 Bot maintenance me hai, thodi der baad aao."); return False
        if not x["approved"]:
            await m.reply_text("⏳ Admin approval ka wait karo."); return False
    return True


async def main_menu(u, c, note=""):
    c.user_data.pop("await", None)
    k = cur(u)
    isadm = u.effective_user.id in ADMINS
    if k:
        a = A(k)
        head = (f"📱 Active: @{a.get('ig', '?')}  (aaj {sent_today(a)}/{limit_for(k)} DM)\n"
                f"✉️ DM msg: {short(a['msg']) if a['msg'] else '❌ set nahi hai'}\n👥 Targets: {len(a['list'])}   "
                f"🤖 Auto-reply: {'ON' if a['autoreply'] else 'off'}   🎉 Welcome: {'ON' if a['welcome'] else 'off'}")
        rows = [[("📱 Accounts", "acc:menu"), ("✉️ DM Message", "msg:menu")],
                [("👥 Target List", "ls:menu"), ("🚀 Auto DM", "ad:menu")],
                [("💬 Single DM", "dm:one"), ("🤖 Auto Reply", "ar:menu")],
                [("🎁 Delivery (Link/File)", "dl:menu")],
                [("💭 Comment Reply", "cr:menu"), ("📖 Story Reply", "sr:menu")],
                [("🎉 Welcome DM", "wl:menu"), ("⏰ Schedule", "sc:menu")],
                [("📊 Status", "st:main"), ("❓ Help", "help:main")]]
    else:
        head = "👋 Swagat hai! Sabse pehle apna Instagram account add karo 👇"
        rows = [[("➕ Add Account", "acc:add")], [("❓ Help", "help:main")]]
    if isadm:
        rows.append([("👑 Admin Panel", "adm:main")])
    await show(u, c, (note + "\n\n" if note else "") + "🏠 MAIN MENU\n\n" + head + f"\n\n👨‍💻 Developed by {DEV}", kb(*rows))


async def need_acc(u, c):
    if not cur(u):
        await show(u, c, "Pehle Instagram account add karo 👇", kb([("➕ Add Account", "acc:add")], BACK))
        return None
    return cur(u)


def ask(c, kind, **data):
    c.user_data["await"] = {"k": kind, **data}


CANCEL = kb([("❌ Cancel", "m:main")])


# ---------------- command entry points ----------------
async def cmd_start(u, c):
    if await check(u, c):
        await main_menu(u, c)


async def cmd_admin(u, c):
    if await check(u, c, admin=True):
        await r_adm(u, c, ["main"])


# ---------------- accounts ----------------
async def r_acc(u, c, d):
    x = db["users"][str(u.effective_user.id)]
    if d[0] == "add":
        if len(x["accounts"]) >= MAX_ACCOUNTS:
            return await show(u, c, f"❌ Max {MAX_ACCOUNTS} accounts hi add ho sakte hain.", kb([("📱 Accounts", "acc:menu")], BACK))
        ask(c, "add_acc")
        return await show(u, c, "➕ ACCOUNT ADD\n\nApni Instagram sessionid yahan bhejo.\n"
                          "(Instagram.com login → F12 → Application → Cookies → sessionid ki value copy karo)\n\n"
                          "🔒 Msg bhejte hi main use delete kar dunga.", CANCEL)
    if d[0] == "sel" and d[1] in x["accounts"]:
        x["active"] = d[1]; save()
    if d[0] == "del":
        return await show(u, c, f"🗑 @{x['accounts'].get(d[1], {}).get('ig', d[1])} hata dun?", kb([("✅ Haan hata do", f"acc:delok:{d[1]}"), ("❌ Nahi", "acc:menu")]))
    if d[0] == "delok" and d[1] in x["accounts"]:
        x["accounts"].pop(d[1]); clients.pop(f"{u.effective_user.id}:{d[1]}", None)
        if x["active"] == d[1]:
            x["active"] = next(iter(x["accounts"]), "")
        save()
    rows = [[(f"{'✅ ' if n == x['active'] else ''}@{a.get('ig', n)}", f"acc:sel:{n}"), ("🗑", f"acc:del:{n}")] for n, a in x["accounts"].items()]
    rows += [[("➕ Add Account", "acc:add")], BACK]
    await show(u, c, "📱 MY ACCOUNTS\n\nAccount pe dabao = active karo, 🗑 = hatao.\n✅ = abhi active", kb(*rows))


async def in_add_acc(u, c, txt, st):
    try:
        await u.message.delete()
    except Exception:
        pass
    sid = txt.strip().strip("\"'")
    uid, tmp = str(u.effective_user.id), "tmp_login"
    x = db["users"][uid]
    x["accounts"][tmp] = {"sid": fernet.encrypt(sid.encode()).decode(), **json.loads(json.dumps(FRESH))}
    clients.pop(f"{uid}:{tmp}", None)
    wait = await c_send(u, c, "⏳ Login ho raha hai…")
    try:
        cl = await asyncio.to_thread(get_client, f"{uid}:{tmp}")
        info = await asyncio.to_thread(cl.account_info)
    except Exception as e:
        x["accounts"].pop(tmp, None); clients.pop(f"{uid}:{tmp}", None)
        errlog.append(f"login {uid}: {e}")
        return await show_new(u, c, f"❌ Login fail: {short(str(e), 150)}\n\nFresh sessionid lekar dobara try karo.", kb([("🔁 Dobara try", "acc:add")], BACK))
    name = info.username.lower()
    acc = x["accounts"].pop(tmp)
    clients[f"{uid}:{name}"] = clients.pop(f"{uid}:{tmp}")
    acc["ig"] = info.username
    x["accounts"][name] = acc
    x["active"] = name
    c.user_data.pop("await", None)
    save()
    await show_new(u, c, f"✅ @{info.username} add ho gaya! (Sessionid wala msg delete kar diya)\n\nAb DM message set karo 👇",
                   kb([("✉️ DM Message set karo", "msg:menu")], BACK))


async def c_send(u, c, text, markup=None):
    return await c.bot.send_message(u.effective_chat.id, text, reply_markup=markup)


show_new = c_send   # always a fresh message (used after text input)


# ---------------- DM message ----------------
async def r_msg(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "set":
        ask(c, "set_msg")
        return await show(u, c, "✉️ Wo message bhejo jo DM me jana chahiye.\n\nTips:\n• {username} = samne wale ka naam\n• {hi|hello|hey} = random ek word (ban se bachata hai)\n\nExample:\nHi {username}! {Check|Dekho} meri profile 🔥", CANCEL)
    await show(u, c, f"✉️ DM MESSAGE\n\nAbhi ka message:\n{a['msg'] or '❌ set nahi hai'}\n\n"
               "Auto DM / Single DM me yahi message jayega.", kb([("✏️ Message set/change karo", "msg:set")], BACK))


async def in_set_msg(u, c, txt, st):
    k = cur(u); A(k)["msg"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, f"✅ DM message set ho gaya:\n\n{txt}", kb([("👥 Target add karo", "ls:menu"), ("🚀 Auto DM", "ad:menu")], BACK))


# ---------------- target list ----------------
async def r_ls(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "add":
        ask(c, "add_list")
        return await show(u, c, "👥 Usernames bhejo (space / comma / new line se alag).\nYa .txt file bhej do.\n\nExample:\nrahul_01 priya.k amit_99", CANCEL)
    if d[0] == "show":
        txt = ", ".join(a["list"][:150]) or "Khaali hai"
        return await show(u, c, f"📋 List ({len(a['list'])}):\n\n{txt}" + ("…" if len(a["list"]) > 150 else ""), kb([("⬅️ Back", "ls:menu")]))
    if d[0] == "clear":
        a["list"] = []; save()
    await show(u, c, f"👥 TARGET LIST\n\nAbhi {len(a['list'])} users hain.\nAuto DM inhi ko jayega.",
               kb([("➕ Add usernames", "ls:add"), ("📋 Dekho", "ls:show")], [("🧹 Clear list", "ls:clear")], BACK))


def parse_names(t):
    return [x.strip().lstrip("@").lower() for x in re.split(r"[\s,;]+", t) if x.strip()]


async def in_add_list(u, c, txt, st):
    a = A(cur(u))
    names = parse_names(txt)
    a["list"].extend(x for x in dict.fromkeys(names) if x not in a["list"]); save(); c.user_data.pop("await", None)
    await show_new(u, c, f"✅ {len(names)} username add hue. List me ab {len(a['list'])} users.", kb([("🚀 Auto DM start karo", "ad:menu")], BACK))


# ---------------- auto DM ----------------
def job_text(k):
    return "▶️ Chal raha hai" if k in jobs else "⏹ Band"


async def r_ad(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] in ("menu", "reset"):
        if d[0] == "reset":
            a["sent"] = {}; save()
        return await show(u, c, f"🚀 AUTO DM\n\nStatus: {job_text(k)}\nAaj: {sent_today(a)}/{limit_for(k)}\nMsg: {short(a['msg']) or '❌ set nahi'}\n"
                          f"Targets: {len(a['list'])}   Pehle DM kiye: {len(a['sent'])}\n\n"
                          "Pehle DM kiye hue log dobara skip honge.",
                          kb([("📋 Target List ko DM", "ad:list")], [("👥 Mere Followers ko DM", "ad:fol")], [("⏹ Stop", "ad:stop"), ("♻️ History reset", "ad:reset")],
                             [("✉️ Message badlo", "msg:menu")], BACK))
    if d[0] == "stop":
        if k in jobs:
            jobs[k]["stop"].set()
        return await show(u, c, "🛑 Auto DM rok diya." if k in jobs else "Koi job nahi chal raha.", kb([("🚀 Auto DM", "ad:menu")], BACK))
    if not a["msg"]:
        return await show(u, c, "❌ Pehle DM message set karo.", kb([("✉️ Message set karo", "msg:set")], BACK))
    if k in jobs:
        return await show(u, c, "Pehle se chal raha hai.", kb([("⏹ Stop", "ad:stop")], BACK))
    if d[0] == "list":
        if not a["list"]:
            return await show(u, c, "❌ Target list khaali hai.", kb([("➕ Usernames add karo", "ls:add")], BACK))
        return await show(u, c, f"✅ CONFIRM\n\n{len(a['list'])} users ko ye DM jayega:\n\n{a['msg']}\n\nDelay: {MIN_DELAY}-{MAX_DELAY}s, limit {limit_for(k)}/din.",
                          kb([("▶️ START", "ad:go:list")], [("❌ Cancel", "ad:menu")]))
    if d[0] == "fol":
        return await show(u, c, "👥 Kitne followers ko DM karna hai?", kb([("10", "ad:fn:10"), ("25", "ad:fn:25"), ("50", "ad:fn:50"), ("100", "ad:fn:100")], [("✏️ Custom number", "ad:fc")], [("⬅️ Back", "ad:menu")]))
    if d[0] == "fc":
        ask(c, "fol_n")
        return await show(u, c, "Number bhejo (max 500):", CANCEL)
    if d[0] == "fn":
        return await show(u, c, f"✅ CONFIRM\n\nApne {d[1]} followers ko ye DM jayega:\n\n{a['msg']}\n\nDelay: {MIN_DELAY}-{MAX_DELAY}s, limit {limit_for(k)}/din.",
                          kb([("▶️ START", f"ad:go:fol:{d[1]}")], [("❌ Cancel", "ad:menu")]))
    if d[0] == "go":
        if d[1] == "list":
            targets = list(a["list"])
        else:
            await show(u, c, "⏳ Followers la raha hu…")
            try:
                targets = await asyncio.to_thread(ig_followers, k, min(int(d[2]), 500))
            except Exception as e:
                errlog.append(f"{k} followers: {e}")
                return await show_new(u, c, f"❌ {short(str(e), 150)}", kb([("🚀 Auto DM", "ad:menu")], BACK))
        stop = asyncio.Event()
        jobs[k] = {"stop": stop, "task": asyncio.create_task(run_autodm(c, str(u.effective_user.id), k, targets, stop))}
        await show(u, c, f"🚀 Auto DM START!\n{len(targets)} targets. Progress yahin milega.", kb([("⏹ Stop", "ad:stop")], BACK))


async def in_fol_n(u, c, txt, st):
    if not txt.isdigit() or not 0 < int(txt) <= 500:
        return await show_new(u, c, "1 se 500 ke beech number bhejo.", CANCEL)
    c.user_data.pop("await", None)
    n = int(txt); a = A(cur(u))
    await show_new(u, c, f"✅ CONFIRM\n\nApne {n} followers ko ye DM jayega:\n\n{a['msg']}", kb([("▶️ START", f"ad:go:fol:{n}")], [("❌ Cancel", "ad:menu")]))


BLOCK_WORDS = ("login_required", "challenge", "feedback", "spam", "429", "rate", "checkpoint")


async def run_autodm(c, uid, k, targets, stop):
    a, ok, bad = A(k), 0, 0
    try:
        for user in targets:
            if stop.is_set():
                break
            if user.lower() in a["sent"]:
                continue
            if sent_today(a) >= limit_for(k):
                await say(c, uid, f"⛔ Aaj ki limit ({limit_for(k)}) poori. Kal dobara Auto DM chalao.")
                break
            try:
                await asyncio.to_thread(ig_send, k, user, render(a["msg"], user))
                ok += 1
                if ok % 5 == 0:
                    await say(c, uid, f"📨 {ok} DM ho gaye… (fail: {bad})")
            except Exception as e:
                bad += 1
                errlog.append(f"{k} autodm @{user}: {e}")
                if any(w in str(e).lower() for w in BLOCK_WORDS):
                    await say(c, uid, f"🛑 Instagram ne rok lagayi ({short(str(e), 80)}). Auto DM ruk gaya, kuch ghante ruko.")
                    break
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    finally:
        jobs.pop(k, None)
    await say(c, uid, f"✅ Auto DM khatam!\nBheje: {ok} | Fail: {bad}", kb([("🚀 Auto DM", "ad:menu")], BACK))


# ---------------- single DM ----------------
async def r_dm(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    if d[0] == "one":
        ask(c, "dm_user")
        return await show(u, c, "💬 SINGLE DM\n\nKis Instagram username ko DM karna hai?", CANCEL)
    st = c.user_data.get("dm_pending")
    if d[0] == "saved" and st:
        await do_single(u, c, k, st, A(k)["msg"])


async def do_single(u, c, k, user, text):
    c.user_data.pop("await", None)
    a = A(k)
    if not text:
        return await show_new(u, c, "❌ Message khaali hai.", kb([("✉️ Message set karo", "msg:set")], BACK))
    if sent_today(a) >= limit_for(k):
        return await show_new(u, c, "⛔ Aaj ki limit poori.", kb(BACK))
    try:
        await asyncio.to_thread(ig_send, k, user, render(text, user))
        await show_new(u, c, f"✅ @{user} ko DM chala gaya!", kb([("💬 Aur DM", "dm:one")], BACK))
    except Exception as e:
        errlog.append(f"{k} dm: {e}")
        await show_new(u, c, f"❌ DM fail: {short(str(e), 150)}", kb([("💬 Dobara", "dm:one")], BACK))


async def in_dm_user(u, c, txt, st):
    user = txt.strip().lstrip("@").split()[0].lower()
    c.user_data["dm_pending"] = user
    ask(c, "dm_text", user=user)
    await show_new(u, c, f"@{user} ko kya bhejna hai? Message type karo,\nya saved message use karo 👇", kb([("✉️ Saved message bhejo", "dm:saved")], [("❌ Cancel", "m:main")]))


async def in_dm_text(u, c, txt, st):
    await do_single(u, c, cur(u), st["user"], txt)


# ---------------- auto reply / welcome ----------------
async def r_ar(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "tog":
        a["autoreply"] = not a["autoreply"]; save()
    if d[0] == "set":
        ask(c, "set_reply")
        return await show(u, c, "🤖 Auto-reply ka text bhejo ({username} / {hi|hello} chalega):", CANCEL)
    await show(u, c, f"🤖 AUTO REPLY\n\nStatus: {'✅ ON' if a['autoreply'] else '❌ OFF'}\nReply text:\n{a['reply']}\n\n"
               "Koi tumhe DM kare to bot khud reply karega (har 1-1.5 min me check, ek bande ko 24h me 1 baar).",
               kb([(("🔴 OFF karo" if a["autoreply"] else "🟢 ON karo"), "ar:tog")], [("✏️ Reply text badlo", "ar:set")], BACK))


async def in_set_reply(u, c, txt, st):
    A(cur(u))["reply"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Reply text set.", kb([("🤖 Auto Reply", "ar:menu")], BACK))


async def r_wl(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "tog":
        a["welcome"] = not a["welcome"]; a["fol_seen"] = []; save()
    if d[0] == "set":
        ask(c, "set_wmsg")
        return await show(u, c, "🎉 Welcome DM ka text bhejo:", CANCEL)
    await show(u, c, f"🎉 WELCOME DM\n\nStatus: {'✅ ON' if a['welcome'] else '❌ OFF'}\nText:\n{a['wmsg']}\n\n"
               "Koi naya follower aaye to use ye DM chala jayega (har ~10 min me check). ON karne ke baad ke naye followers ko hi jayega.",
               kb([(("🔴 OFF karo" if a["welcome"] else "🟢 ON karo"), "wl:tog")], [("✏️ Text badlo", "wl:set")], BACK))


async def in_set_wmsg(u, c, txt, st):
    A(cur(u))["wmsg"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Welcome text set.", kb([("🎉 Welcome DM", "wl:menu")], BACK))


# ---------------- schedule ----------------
async def r_sc(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    uid = str(u.effective_user.id)
    if d[0] == "new":
        ask(c, "sc_user")
        return await show(u, c, "⏰ NAYA SCHEDULE\n\nKis username ko DM bhejna hai?", CANCEL)
    if d[0] == "del":
        db["schedules"] = [s for s in db["schedules"] if not (s["id"] == d[1] and s["key"].startswith(uid + ":"))]; save()
    if d[0] == "t":
        p = c.user_data.get("sc")
        if not p:
            return await main_menu(u, c)
        now = datetime.now(TZ)
        when = now + timedelta(minutes=int(d[1])) if d[1] != "tm" else (now + timedelta(days=1)).replace(hour=9, minute=0, second=0)
        return await add_sched(u, c, k, p, when)
    mine = [s for s in db["schedules"] if s["key"].startswith(uid + ":")]
    rows = [[(f"🗑 @{s['user']} {datetime.fromtimestamp(s['ts'], TZ):%d %b %H:%M}", f"sc:del:{s['id']}")] for s in mine[:15]]
    await show(u, c, f"⏰ SCHEDULE\n\n{len(mine)} pending. 🗑 dabao = cancel.", kb(*rows, [("➕ Naya schedule", "sc:new")], BACK))


async def add_sched(u, c, k, p, when):
    c.user_data.pop("await", None); c.user_data.pop("sc", None)
    db["schedules"].append({"id": uuid.uuid4().hex[:6], "key": k, "user": p["user"], "text": p["text"], "ts": when.timestamp(), "chat": u.effective_chat.id}); save()
    await show_new(u, c, f"✅ Schedule ho gaya!\n@{p['user']} ko {when:%d %b %Y %H:%M} pe DM jayega.", kb([("⏰ Schedules", "sc:menu")], BACK))


async def in_sc_user(u, c, txt, st):
    ask(c, "sc_text", user=txt.strip().lstrip("@").split()[0].lower())
    await show_new(u, c, "Kya message bhejna hai? Type karo (ya saved message):", kb([("✉️ Saved message", "sc:saved")], [("❌ Cancel", "m:main")]))


async def in_sc_text(u, c, txt, st):
    c.user_data["sc"] = {"user": st["user"], "text": txt}
    ask(c, "sc_time")
    await show_new(u, c, "Kab bhejna hai? Button dabao, ya type karo:\n`HH:MM` (aaj/kal) ya `YYYY-MM-DD HH:MM`".replace("`", ""),
                   kb([("30 min baad", "sc:t:30"), ("1 ghanta baad", "sc:t:60")], [("3 ghante baad", "sc:t:180"), ("Kal subah 9", "sc:t:tm")], [("❌ Cancel", "m:main")]))


async def in_sc_time(u, c, txt, st):
    p, now = c.user_data.get("sc"), datetime.now(TZ)
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", txt):
            h, m = map(int, txt.split(":"))
            when = now.replace(hour=h, minute=m, second=0)
            when = when if when > now else when + timedelta(days=1)
        else:
            when = datetime.strptime(txt, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        return await show_new(u, c, "❌ Time samajh nahi aaya. Example: 18:30 ya 2026-11-20 18:30", CANCEL)
    await add_sched(u, c, cur(u), p, when)


# saved-message shortcut for schedule
async def sc_saved(u, c):
    k = cur(u); st = c.user_data.get("await") or {}
    await in_sc_text(u, c, A(k)["msg"] or "Hi {username}!", st)


# ---------------- status / help ----------------
async def r_st(u, c, d):
    k = await need_acc(u, c)
    if k:
        a = A(k)
        await show(u, c, f"📊 STATUS — @{a.get('ig', '?')}\n\nAaj DM: {sent_today(a)}/{limit_for(k)}\nTotal DM: {len(a['sent'])}\nTargets: {len(a['list'])}\n"
                   f"Auto DM: {job_text(k)}\nAuto-reply: {'ON' if a['autoreply'] else 'off'}\nDelivery: {'ON' if a['deliver'] else 'off'}\nComment reply: {'ON' if a['cr'] else 'off'}\n"
                   f"Story reply: {'ON' if a['story'] else 'off'}\nWelcome DM: {'ON' if a['welcome'] else 'off'}\n"
                   f"Schedules: {sum(1 for s in db['schedules'] if s['key'] == k)}", kb([("🔄 Refresh", "st:main")], BACK))


async def r_help(u, c, d):
    await show(u, c, "❓ KAISE USE KARE\n\n1️⃣ Add Account → sessionid bhejo\n2️⃣ DM Message → apna message set karo\n3️⃣ Target List → usernames add karo\n"
               "4️⃣ Auto DM → START dabao\n\nExtra:\n• 💬 Single DM = ek bande ko\n• 🤖 Auto Reply = DM aane pe auto jawab\n• 🎉 Welcome DM = naye follower ko DM\n• 🎁 Delivery = DM/follow karne walon ko tumhara link/photo/video auto milna\n• 💭 Comment Reply = post ke comments pe auto reply\n• 📖 Story Reply = story reply pe auto jawab\n• ⏰ Schedule = baad me bhejna\n\n"
               "Message tips: {username} = naam, {hi|hello} = random word.\n⚠️ Zyada DM = Instagram ban. Limit/delay mat badhao.\n\n👨‍💻 Developed by " + DEV, kb(BACK))


# ---------------- admin ----------------
def stats_text():
    us = list(db["users"].values()); day = time.time() - 86400
    accs = [a for x in us for a in x["accounts"].values()]
    return (f"👥 Users: {len(us)} (24h active: {sum(1 for x in us if x.get('last_seen', 0) > day)})\n🚫 Banned: {sum(x['banned'] for x in us)} | ⏳ Pending: {sum(not x['approved'] and not x['banned'] for x in us)}\n"
            f"📱 IG accounts: {len(accs)} | 🤖 Auto-reply ON: {sum(a['autoreply'] for a in accs)}\n📨 DM aaj: {sum(sent_today(a) for a in accs)} | total: {sum(len(a['sent']) for a in accs)}\n"
            f"🚀 Jobs: {len(jobs)} | ⏰ Schedules: {len(db['schedules'])}")


async def r_adm(u, c, d):
    s = db["settings"]
    if d[0] == "appr":
        s["approval"] = not s["approval"]; save()
    if d[0] == "maint":
        s["maintenance"] = not s["maintenance"]; save()
    if d[0] == "u":
        page = int(d[1]) if len(d) > 1 else 0
        us = sorted(db["users"].items(), key=lambda kv: -kv[1]["joined"])
        tot = max(1, (len(us) + 7) // 8); page = max(0, min(page, tot - 1))
        rows = [[(f"{'🚫' if x['banned'] else ('✅' if x['approved'] else '⏳')} {short(x['name'], 18)} @{x['username'] or '-'}", f"au:{i}")] for i, x in us[page * 8:(page + 1) * 8]]
        nav = [("⬅️", f"adm:u:{page - 1}")] if page else []
        nav += [("➡️", f"adm:u:{page + 1}")] if page < tot - 1 else []
        return await show(u, c, f"👥 USERS — page {page + 1}/{tot} (total {len(us)})", kb(*rows, nav, [("🔍 Find", "adm:find"), ("⬅️ Panel", "adm:main")]))
    if d[0] == "find":
        ask(c, "adm_find")
        return await show(u, c, "🔍 User ka naam / @username / ID bhejo:", kb([("❌ Cancel", "adm:main")]))
    if d[0] == "bc":
        ask(c, "bc")
        return await show(u, c, "📣 BROADCAST\n\nJo bhi sabko bhejna hai wo bhejo (text / photo / video / kuch bhi).", kb([("❌ Cancel", "adm:main")]))
    if d[0] == "logs":
        return await show(u, c, ("🧾 LOGS\n\n" + "\n".join(list(errlog)[-25:]))[-3900:] if errlog else "🧾 Koi error nahi 🎉", kb([("⬅️ Panel", "adm:main")]))
    if d[0] == "export":
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["id", "name", "username", "status", "accounts", "total_dms", "joined"])
        for i, x in db["users"].items():
            w.writerow([i, x["name"], x["username"], "banned" if x["banned"] else ("ok" if x["approved"] else "pending"), len(x["accounts"]),
                        sum(len(a["sent"]) for a in x["accounts"].values()), datetime.fromtimestamp(x["joined"], TZ).strftime("%Y-%m-%d")])
        await c.bot.send_document(u.effective_chat.id, io.BytesIO(buf.getvalue().encode()), filename="users.csv")
        return
    await show(u, c, "👑 ADMIN PANEL\n\n" + stats_text(),
               kb([("👥 Users", "adm:u:0"), ("📣 Broadcast", "adm:bc")],
                  [(f"🔒 Approval: {'ON' if s['approval'] else 'OFF'}", "adm:appr"), (f"🛠 Maintenance: {'ON' if s['maintenance'] else 'OFF'}", "adm:maint")],
                  [("🧾 Logs", "adm:logs"), ("📤 Export CSV", "adm:export")], [("🔄 Refresh", "adm:main")], BACK))


def user_card(uid):
    x = db["users"][uid]
    accs = "\n".join(f"  • @{a.get('ig', n)} aaj {sent_today(a)}, total {len(a['sent'])}" for n, a in x["accounts"].items()) or "  –"
    seen = datetime.fromtimestamp(x.get("last_seen", x["joined"]), TZ)
    return (f"👤 {x['name']} @{x['username'] or '-'}\nID: {uid}\nStatus: {'banned' if x['banned'] else ('approved' if x['approved'] else 'pending')}\n"
            f"Daily limit: {x.get('limit') or DAILY_LIMIT}\nLast seen: {seen:%d %b %H:%M}\nAccounts:\n{accs}")


async def r_au(u, c, d):
    uid = d[0]
    if uid not in db["users"]:
        return await r_adm(u, c, ["u", "0"])
    x, act = db["users"][uid], d[1] if len(d) > 1 else ""
    if act == "ban" and int(uid) not in ADMINS:
        x["banned"] = True
        for k in [k for k in jobs if k.startswith(uid + ":")]:
            jobs[k]["stop"].set()
        db["schedules"] = [s for s in db["schedules"] if not s["key"].startswith(uid + ":")]
        for a in x["accounts"].values():
            a["autoreply"] = a["welcome"] = a["story"] = a["deliver"] = a["cr"] = False
        save()
    elif act == "unban":
        x["banned"] = False; save()
    elif act == "appr":
        x["approved"], x["banned"] = True, False; save()
        await say(c, uid, "✅ Admin ne approve kar diya! /start dabao.")
    elif act == "lim":
        ask(c, "adm_limit", uid=uid)
        return await show(u, c, f"Naya daily limit number bhejo (0 = default {DAILY_LIMIT}):", kb([("❌ Cancel", f"au:{uid}")]))
    elif act == "msg":
        ask(c, "adm_msg", uid=uid)
        return await show(u, c, "Is user ko kya msg bhejna hai? Type karo:", kb([("❌ Cancel", f"au:{uid}")]))
    await show(u, c, user_card(uid), kb([("🚫 Ban", f"au:{uid}:ban"), ("♻️ Unban", f"au:{uid}:unban")],
                                       [("✅ Approve", f"au:{uid}:appr"), ("📊 Limit", f"au:{uid}:lim")], [("💬 Message", f"au:{uid}:msg")], [("⬅️ Users", "adm:u:0")]))


async def r_ap(u, c, d):
    uid, yes = d[0], d[1] == "1"
    if uid in db["users"]:
        db["users"][uid]["approved"], db["users"][uid]["banned"] = yes, not yes; save()
        if yes:
            await say(c, uid, "✅ Admin ne approve kar diya! /start dabao.")
    await show(u, c, f"{u.callback_query.message.text}\n\n{'✅ Approved' if yes else '🚫 Banned'}")


async def r_bc(u, c, d):
    if d[0] == "no":
        c.user_data.pop("bc", None)
        return await r_adm(u, c, ["main"])
    src = c.user_data.pop("bc", None)
    if not src:
        return await r_adm(u, c, ["main"])
    targets = [i for i, x in db["users"].items() if not x["banned"] and not x.get("blocked")]
    asyncio.create_task(do_broadcast(c, u.effective_chat.id, targets, src))
    await show(u, c, f"📣 {len(targets)} users ko bhej raha hu… (khatam hone pe report aayegi)", kb([("⬅️ Panel", "adm:main")]))


async def do_broadcast(c, admin_chat, targets, src):
    ok = fail = 0
    for uid in targets:
        try:
            await c.bot.copy_message(int(uid), src[0], src[1]); ok += 1
        except Forbidden:
            db["users"][uid]["blocked"] = True; fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.07)
    save()
    await say(c, admin_chat, f"📣 Broadcast done!\n✅ Gaya: {ok}\n❌ Fail/blocked: {fail}", kb([("⬅️ Panel", "adm:main")]))


async def in_bc(u, c, txt, st):
    m = u.effective_message
    c.user_data["bc"] = (m.chat_id, m.message_id); c.user_data.pop("await", None)
    n = sum(1 for x in db["users"].values() if not x["banned"] and not x.get("blocked"))
    await show_new(u, c, f"☝️ Ye message {n} users ko jayega. Bhejun?", kb([(f"✅ Haan, {n} ko bhejo", "bc:yes")], [("❌ Cancel", "bc:no")]))


async def in_adm_find(u, c, txt, st):
    q = txt.lower().lstrip("@"); c.user_data.pop("await", None)
    hits = [(i, x) for i, x in db["users"].items() if q == i or q in x["name"].lower() or q in x["username"].lower()][:10]
    rows = [[(f"{short(x['name'], 18)} @{x['username'] or '-'}", f"au:{i}")] for i, x in hits] + [[("⬅️ Panel", "adm:main")]]
    await show_new(u, c, f"🔍 {len(hits)} mile" if hits else "Kuch nahi mila.", kb(*rows))


async def in_adm_limit(u, c, txt, st):
    c.user_data.pop("await", None)
    if txt.isdigit() and st["uid"] in db["users"]:
        db["users"][st["uid"]]["limit"] = int(txt); save()
    await show_new(u, c, user_card(st["uid"]), kb([("⬅️ User", f"au:{st['uid']}")]))


async def in_adm_msg(u, c, txt, st):
    c.user_data.pop("await", None)
    await say(c, st["uid"], "📩 Admin: " + txt)
    await show_new(u, c, "✅ Bhej diya.", kb([("⬅️ User", f"au:{st['uid']}")]))


# ---------------- delivery / story / comment ----------------
async def r_dl(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "tog":
        if not a["dtext"] and not a["dfiles"]:
            return await show(u, c, "❌ Pehle link/text ya photo/video set karo.", kb([("✏️ Text/Link set karo", "dl:text")], [("🎁 Delivery", "dl:menu")]))
        a["deliver"] = not a["deliver"]; save()
    elif d[0] == "text":
        ask(c, "d_text")
        return await show(u, c, "🎁 Wo text/link bhejo jo logon ko milna chahiye.\n\nExample:\nYe lo free guide 👉 https://drive.google.com/...\n\n({username} aur {hi|hello} bhi chalega)", CANCEL)
    elif d[0] == "media":
        ask(c, "d_media")
        return await show(u, c, f"📎 Photo ya video bhejo (abhi {len(a['dfiles'])}/3).\n\n⚠️ PDF/ZIP/APK jaisi files Instagram DM me direct nahi ja sakti — unka link (Drive/Mega/Telegram) text me daalo.", CANCEL)
    elif d[0] == "clr":
        for f in a["dfiles"]:
            Path(f["path"]).unlink(missing_ok=True)
        a["dfiles"] = []; save()
    elif d[0] == "fol":
        a["dfollow"] = not a["dfollow"]; save()
    elif d[0] == "kw":
        ask(c, "d_kw")
        return await show(u, c, "🔑 Trigger words bhejo (comma se), jaise: link, guide, free\nSirf in words wale DM pe delivery hogi.\n\n'-' bhejo = kisi bhi DM pe.", CANCEL)
    elif d[0] == "nf":
        ask(c, "d_nf")
        return await show(u, c, "Jo follow nahi karta use kya reply jaye? Text bhejo:", CANCEL)
    await show(u, c, f"🎁 DELIVERY (Link/File)\n\nStatus: {'✅ ON' if a['deliver'] else '❌ OFF'}\nText/Link: {short(a['dtext'], 80) or '❌ set nahi'}\nPhoto/Video: {len(a['dfiles'])}/3\n"
               f"Follow zaroori: {'Haan' if a['dfollow'] else 'Nahi'}\nTrigger: {', '.join(a['dkw']) or 'kisi bhi DM pe'}\n\n"
               "Kaise: koi tumhe DM kare to use ye mil jata hai." + (" Follow nahi kiya to 'pehle follow karo' msg jata hai; follow karke dobara DM kare to mil jata hai." if a["dfollow"] else "") +
               "\n(Har banda 24h me 1 baar, har ~1-1.5 min me check)",
               kb([(("🔴 OFF karo" if a["deliver"] else "🟢 ON karo"), "dl:tog")], [("✏️ Text/Link", "dl:text"), ("📎 Photo/Video", "dl:media")],
                  [(f"👥 Follow zaroori: {'ON' if a['dfollow'] else 'OFF'}", "dl:fol"), ("🔑 Trigger words", "dl:kw")], [("✏️ Non-follower msg", "dl:nf"), ("🗑 Media hatao", "dl:clr")], BACK))


async def in_d_text(u, c, txt, st):
    A(cur(u))["dtext"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Delivery text/link set.", kb([("🎁 Delivery", "dl:menu")], BACK))


async def in_d_kw(u, c, txt, st):
    a = A(cur(u))
    a["dkw"] = [] if txt.strip() == "-" else [w.strip().lower() for w in re.split(r"[,\n]+", txt) if w.strip()]
    save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Trigger set: " + (", ".join(a["dkw"]) or "kisi bhi DM pe"), kb([("🎁 Delivery", "dl:menu")], BACK))


async def in_d_nf(u, c, txt, st):
    A(cur(u))["nofollow"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Non-follower msg set.", kb([("🎁 Delivery", "dl:menu")], BACK))


async def in_d_media(u, c, m):
    a = A(cur(u))
    if len(a["dfiles"]) >= 3:
        return await show_new(u, c, "❌ Max 3 media. Pehle 🗑 Media hatao.", kb([("🎁 Delivery", "dl:menu")]))
    mime = (m.document.mime_type or "") if m.document else ""
    if m.photo:
        tf, ext, typ = await m.photo[-1].get_file(), "jpg", "photo"
    elif m.video:
        tf, ext, typ = await m.video.get_file(), "mp4", "video"
    elif mime.startswith("image/"):
        tf, ext, typ = await m.document.get_file(), mime.split("/")[1], "photo"
    elif mime.startswith("video/"):
        tf, ext, typ = await m.document.get_file(), "mp4", "video"
    else:
        return await show_new(u, c, "❌ Sirf photo ya video chalega. PDF/ZIP ka link text me daalo.", CANCEL)
    FILES.mkdir(parents=True, exist_ok=True)
    p = FILES / f"{u.effective_user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    await tf.download_to_drive(p)
    a["dfiles"].append({"type": typ, "path": str(p)}); save(); c.user_data.pop("await", None)
    await show_new(u, c, f"✅ {typ} add hua ({len(a['dfiles'])}/3).", kb([("📎 Aur media", "dl:media"), ("🎁 Delivery", "dl:menu")], BACK))


async def r_sr(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "tog":
        a["story"] = not a["story"]; save()
    if d[0] == "set":
        ask(c, "sr_text")
        return await show(u, c, "📖 Story reply ka jawab bhejo ({username} / {hi|hello} chalega):", CANCEL)
    await show(u, c, f"📖 STORY REPLY\n\nStatus: {'✅ ON' if a['story'] else '❌ OFF'}\nJawab:\n{a['sreply']}\n\n"
               "Koi tumhari story pe reply kare to bot khud jawab dega (ek bande ko 6 ghante me 1 baar, har ~1-1.5 min me check).",
               kb([(("🔴 OFF karo" if a["story"] else "🟢 ON karo"), "sr:tog")], [("✏️ Jawab badlo", "sr:set")], BACK))


async def in_sr_text(u, c, txt, st):
    A(cur(u))["sreply"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Story reply text set.", kb([("📖 Story Reply", "sr:menu")], BACK))


async def r_cr(u, c, d):
    k = await need_acc(u, c)
    if not k:
        return
    a = A(k)
    if d[0] == "tog":
        a["cr"] = not a["cr"]; a["c_base"] = False; save()
    elif d[0] == "dm":
        a["crdm"] = not a["crdm"]; save()
    elif d[0] == "set":
        ask(c, "cr_text")
        return await show(u, c, "💭 Comment ke neeche kya reply jaye? Text bhejo:\n({username} aur {Thanks|Shukriya} chalega)", CANCEL)
    elif d[0] == "kw":
        ask(c, "cr_kw")
        return await show(u, c, "🔑 Keywords bhejo (comma se), jaise: link, price, info\nSirf in words wale comment pe reply hoga.\n\n'-' bhejo = har comment pe.", CANCEL)
    await show(u, c, f"💭 COMMENT REPLY\n\nStatus: {'✅ ON' if a['cr'] else '❌ OFF'}\nReply: {short(a['crtext'], 70)}\nKeywords: {', '.join(a['crkw']) or 'har comment'}\n"
               f"Commenter ko DM bhi (Delivery wala link/file): {'ON' if a['crdm'] else 'OFF'}\n\n"
               f"Tumhari last 5 posts ke comments check hote hain (har ~4 min). ON karne ke baad ke naye comments hi reply paate hain. Max {COMMENT_LIMIT} reply/din.",
               kb([(("🔴 OFF karo" if a["cr"] else "🟢 ON karo"), "cr:tog")], [("✏️ Reply text", "cr:set"), ("🔑 Keywords", "cr:kw")],
                  [(f"📩 DM bhi bhejo: {'ON' if a['crdm'] else 'OFF'}", "cr:dm"), ("🎁 Delivery set", "dl:menu")], BACK))


async def in_cr_text(u, c, txt, st):
    A(cur(u))["crtext"] = txt; save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Comment reply text set.", kb([("💭 Comment Reply", "cr:menu")], BACK))


async def in_cr_kw(u, c, txt, st):
    a = A(cur(u))
    a["crkw"] = [] if txt.strip() == "-" else [w.strip().lower() for w in re.split(r"[,\n]+", txt) if w.strip()]
    save(); c.user_data.pop("await", None)
    await show_new(u, c, "✅ Keywords: " + (", ".join(a["crkw"]) or "har comment"), kb([("💭 Comment Reply", "cr:menu")], BACK))


# ---------------- routers ----------------
ROUTES = {"acc": r_acc, "msg": r_msg, "ls": r_ls, "ad": r_ad, "dm": r_dm, "ar": r_ar, "wl": r_wl, "sc": r_sc, "st": r_st, "help": r_help,
          "adm": r_adm, "au": r_au, "ap": r_ap, "bc": r_bc, "dl": r_dl, "sr": r_sr, "cr": r_cr}
ADMIN_ROUTES = {"adm", "au", "ap", "bc"}
INPUTS = {"add_acc": in_add_acc, "set_msg": in_set_msg, "add_list": in_add_list, "fol_n": in_fol_n, "dm_user": in_dm_user, "dm_text": in_dm_text,
          "set_reply": in_set_reply, "set_wmsg": in_set_wmsg, "sc_user": in_sc_user, "sc_text": in_sc_text, "sc_time": in_sc_time,
          "bc": in_bc, "d_text": in_d_text, "d_kw": in_d_kw, "d_nf": in_d_nf, "sr_text": in_sr_text, "cr_text": in_cr_text, "cr_kw": in_cr_kw, "adm_find": in_adm_find, "adm_limit": in_adm_limit, "adm_msg": in_adm_msg}
ADMIN_INPUTS = {"bc", "adm_find", "adm_limit", "adm_msg"}


async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    try:
        await q.answer()
        d = q.data.split(":")
        if not await check(u, c, admin=d[0] in ADMIN_ROUTES):
            return
        if d[0] == "m":
            return await main_menu(u, c)
        if d[0] == "sc" and d[1] == "saved":
            return await sc_saved(u, c)
        c.user_data.pop("await", None)
        await ROUTES[d[0]](u, c, d[1:])
    except Exception as e:
        logging.exception("callback error")
        errlog.append(f"cb {q.data}: {e}")
        try:
            await c.bot.send_message(u.effective_chat.id, f"⚠️ Error: {short(str(e), 120)}", reply_markup=kb(BACK))
        except Exception:
            pass


async def on_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        if not await check(u, c):
            return
        st = c.user_data.get("await")
        m = u.message
        if not st:
            return await main_menu(u, c, "Menu se button dabao 👇")
        kind = st["k"]
        if kind in ADMIN_INPUTS and u.effective_user.id not in ADMINS:
            return
        if kind == "bc":
            return await in_bc(u, c, "", st)
        if kind == "d_media":
            return await in_d_media(u, c, m)
        if kind == "add_list" and m.document:
            data = await (await m.document.get_file()).download_as_bytearray()
            return await in_add_list(u, c, data.decode(errors="ignore"), st)
        if not m.text:
            return await show_new(u, c, "Text bhejo please.", CANCEL)
        await INPUTS[kind](u, c, m.text.strip(), st)
    except Exception as e:
        logging.exception("message error")
        errlog.append(f"msg: {e}")
        await c.bot.send_message(u.effective_chat.id, f"⚠️ Error: {short(str(e), 120)}", reply_markup=kb(BACK))


async def on_error(u, c):
    errlog.append(f"update error: {c.error}")
    logging.error("PTB error: %s", c.error)


# ---------------- background loops ----------------
async def scheduler(app):
    while True:
        await asyncio.sleep(30)
        for s in [x for x in db["schedules"] if x["ts"] <= time.time()]:
            db["schedules"].remove(s); save()
            uid, name = s["key"].split(":", 1)
            try:
                if db["users"][uid]["banned"] or name not in db["users"][uid]["accounts"]:
                    raise RuntimeError("user ban / account nahi")
                if sent_today(A(s["key"])) >= limit_for(s["key"]):
                    raise RuntimeError("daily limit poori")
                await asyncio.to_thread(ig_send, s["key"], s["user"], render(s["text"], s["user"]))
                await say(app, s["chat"], f"⏰✅ Scheduled DM gaya → @{s['user']}")
            except Exception as e:
                errlog.append(f"schedule {s['id']}: {e}")
                await say(app, s["chat"], f"⏰❌ Schedule fail (@{s['user']}): {short(str(e), 100)}")


async def inbox_loop(app):
    while True:
        await asyncio.sleep(random.uniform(60, 90))
        for uid, x in list(db["users"].items()):
            if x["banned"]:
                continue
            for n in list(x["accounts"]):
                a = A(f"{uid}:{n}")
                if a["autoreply"] or a["story"] or a["deliver"]:
                    try:
                        await asyncio.to_thread(ig_check_inbox, f"{uid}:{n}")
                    except Exception as e:
                        errlog.append(f"inbox {uid}:{n}: {e}")
                    await asyncio.sleep(random.uniform(3, 8))


async def welcome_loop(app):
    while True:
        await asyncio.sleep(random.uniform(540, 660))
        for uid, x in list(db["users"].items()):
            if x["banned"]:
                continue
            for n, a in list(x["accounts"].items()):
                if not a["welcome"]:
                    continue
                key = f"{uid}:{n}"
                try:
                    for user in await asyncio.to_thread(ig_new_followers, key):
                        if user.lower() in a["sent"] or sent_today(a) >= limit_for(key):
                            continue
                        await asyncio.to_thread(ig_send, key, user, render(a["wmsg"], user))
                        await asyncio.sleep(random.uniform(20, 45))
                except Exception as e:
                    errlog.append(f"welcome {key}: {e}")
                await asyncio.sleep(random.uniform(3, 8))


async def comment_loop(app):
    while True:
        await asyncio.sleep(random.uniform(200, 280))
        for uid, x in list(db["users"].items()):
            if x["banned"]:
                continue
            for n in list(x["accounts"]):
                if A(f"{uid}:{n}")["cr"]:
                    try:
                        await asyncio.to_thread(ig_comments, f"{uid}:{n}")
                    except Exception as e:
                        errlog.append(f"comments {uid}:{n}: {e}")
                    await asyncio.sleep(random.uniform(3, 8))


def keep_alive():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def do_HEAD(self):   # UptimeRobot free plan uses HEAD requests
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        def log_message(self, *a): pass
    threading.Thread(target=HTTPServer(("0.0.0.0", PORT), H).serve_forever, daemon=True).start()
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        def ping():
            while True:
                time.sleep(240)
                try: urlopen(url, timeout=15).read()
                except Exception: pass
        threading.Thread(target=ping, daemon=True).start()


async def post_init(app):
    for f in (scheduler, inbox_loop, welcome_loop, comment_loop):
        asyncio.create_task(f(app))


def main():
    keep_alive()
    app = Application.builder().token(TOKEN).post_init(post_init).concurrent_updates(True).build()
    for n, f in (("start", cmd_start), ("menu", cmd_start), ("help", cmd_start), ("admin", cmd_admin)):
        app.add_handler(CommandHandler(n, f))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
