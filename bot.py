"""
ربات اعتراف ناشناس - چندگپی (Multi-tenant)
aiogram 3.x + aiosqlite
- بدون نیاز به آیدی عددی در هیچ جا
- هر گپ/کانال: مالک خودش، ادمین‌های خودش (با نام دلخواه + لینک دعوت)، لینک اعتراف اختصاصی خودش
"""
import asyncio
import hashlib
import hmac
import html
import logging
import os
import secrets
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_KEY = os.environ["OWNER_KEY"]          # کلید یک‌بار مصرف برای تبدیل شدن به «مالک ربات»
SALT = os.environ["SECRET_SALT"].encode()    # برای هش کردن هویت فرستنده
DB_PATH = os.getenv("DB_PATH", "confess.db")
INVITE_TTL = 72 * 3600
MAX_REVIEWERS = 20
BACKUP_EVERY = 24 * 3600
BASE_DIR = Path(__file__).resolve().parent

r = Router()
db: aiosqlite.Connection
bot: Bot
BOT_USERNAME = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS spaces(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE NOT NULL, title TEXT, kind TEXT,
  owner_id INTEGER NOT NULL, code TEXT UNIQUE NOT NULL, active INTEGER DEFAULT 1, banned INTEGER DEFAULT 0,
  counter INTEGER DEFAULT 0, cooldown INTEGER DEFAULT 60, max_len INTEGER DEFAULT 1500,
  tag TEXT DEFAULT '#اعتراف', created_at INTEGER);
CREATE INDEX IF NOT EXISTS ix_sp_owner ON spaces(owner_id);
CREATE TABLE IF NOT EXISTS reviewers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  space_id INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  user_id INTEGER, name TEXT NOT NULL, token TEXT UNIQUE, token_exp INTEGER,
  active INTEGER DEFAULT 1, UNIQUE(space_id, user_id));
CREATE TABLE IF NOT EXISTS confessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  space_id INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
  text TEXT, sender_hash TEXT, notify_uid INTEGER, status TEXT DEFAULT 'pending',
  decided_by TEXT, created_at INTEGER);
CREATE INDEX IF NOT EXISTS ix_cf ON confessions(space_id, sender_hash, created_at);
CREATE TABLE IF NOT EXISTS review_msgs(
  confession_id INTEGER REFERENCES confessions(id) ON DELETE CASCADE, user_id INTEGER, message_id INTEGER);
CREATE TABLE IF NOT EXISTS blocks(
  space_id INTEGER REFERENCES spaces(id) ON DELETE CASCADE, sender_hash TEXT,
  PRIMARY KEY(space_id, sender_hash));
"""


# ───────────────────────── helpers ─────────────────────────
async def one(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchone()


async def many(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchall()


async def run(sql, *a):
    c = await db.execute(sql, a)
    await db.commit()
    return c


async def get_setting(k):
    row = await one("SELECT value FROM settings WHERE key=?", k)
    return row["value"] if row else None


def esc(s):
    return html.escape(s or "")


def now():
    return int(time.time())


def sender_hash(uid, sid):
    return hmac.new(SALT, f"{uid}:{sid}".encode(), hashlib.sha256).hexdigest()[:24]


def conf_link(code):
    return f"https://t.me/{BOT_USERNAME}?start=c_{code}"


def inv_link(token):
    return f"https://t.me/{BOT_USERNAME}?start=inv_{token}"


def fmt_sec(s):
    return "بدون محدودیت" if s == 0 else (f"{s} ثانیه" if s < 60 else f"{s // 60} دقیقه")


async def safe(fn, *a, **k):
    for i in range(3):
        try:
            return await fn(*a, **k)
        except TelegramRetryAfter as e:
            if i == 2:
                raise
            await asyncio.sleep(e.retry_after + 1)


async def show(cb: CallbackQuery, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


def ikb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


async def is_bot_owner(uid):
    return str(uid) == await get_setting("owner_id")


async def main_kb(uid):
    rows = [
        [KeyboardButton(text="➕ افزودن گروه", request_chat=KeyboardButtonRequestChat(
            request_id=1, chat_is_channel=False, chat_is_created=True)),
         KeyboardButton(text="➕ افزودن کانال", request_chat=KeyboardButtonRequestChat(
             request_id=2, chat_is_channel=True, chat_is_created=True))],
        [KeyboardButton(text="📂 گپ‌های من")],
    ]
    if await is_bot_owner(uid):
        rows.append([KeyboardButton(text="👑 پنل مالک ربات")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ───────────────────────── states ─────────────────────────
class Conf(StatesGroup):
    text = State()


class Inv(StatesGroup):
    name = State()


class Ren(StatesGroup):
    name = State()


class Tag(StatesGroup):
    tag = State()


class Bc(StatesGroup):
    text = State()


class Ban(StatesGroup):
    link = State()


# ───────────────────────── /start ─────────────────────────
WELCOME = (
    "سلام 👋\n"
    "با این ربات می‌تونی برای گروه یا کانال خودت «اعتراف ناشناس» راه بندازی.\n\n"
    "۱) با دکمه‌های پایین گروه/کانالت رو اضافه کن (ربات رو ادمین کن)\n"
    "۲) لینک اعتراف اختصاصی گپت رو بگیر و بین اعضا پخش کن\n"
    "۳) ادمین‌هات رو با اسم دلخواه و لینک دعوت اضافه کن\n\n"
    "هر اعتراف اول توسط ادمین‌های همون گپ بررسی می‌شه و بعد از تایید منتشر می‌شه."
)


@r.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("c_"):
        return await start_confess(m, state, arg[2:])
    if arg.startswith("inv_"):
        return await show_invite(m, arg[4:])
    await m.answer(WELCOME, reply_markup=await main_kb(m.from_user.id))


@r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("لغو شد.", reply_markup=await main_kb(m.from_user.id))


# ───────────────────────── ارسال اعتراف (کاربر عادی) ─────────────────────────
async def start_confess(m: Message, state: FSMContext, code: str):
    sp = await one("SELECT * FROM spaces WHERE code=?", code)
    if not sp or sp["banned"]:
        return await m.answer("این لینک معتبر نیست.")
    if not sp["active"]:
        return await m.answer("دریافت اعتراف برای این گپ فعلاً متوقف شده.")
    if await one("SELECT 1 FROM blocks WHERE space_id=? AND sender_hash=?",
                 sp["id"], sender_hash(m.from_user.id, sp["id"])):
        return await m.answer("امکان ارسال برای شما در این گپ وجود نداره.")
    await state.set_state(Conf.text)
    await state.update_data(sid=sp["id"])
    await m.answer(
        f"✍️ اعتراف خودت رو برای «<b>{esc(sp['title'])}</b>» بنویس.\n"
        f"هویتت برای همه (حتی ادمین‌ها) کاملاً مخفی می‌مونه.\n"
        f"حداکثر {sp['max_len']} کاراکتر • برای لغو: /cancel",
        reply_markup=ReplyKeyboardRemove())


@r.message(Conf.text, F.text)
async def got_confession(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    sp = await one("SELECT * FROM spaces WHERE id=?", sid)
    if not sp or sp["banned"] or not sp["active"]:
        await state.clear()
        return await m.answer("این گپ دیگه اعتراف دریافت نمی‌کنه.", reply_markup=await main_kb(m.from_user.id))
    text = m.text.strip()
    if len(text) < 5:
        return await m.answer("متن خیلی کوتاهه، کمی بیشتر بنویس.")
    if len(text) > sp["max_len"]:
        return await m.answer(f"متن طولانیه؛ حداکثر {sp['max_len']} کاراکتر. (الان {len(text)})")
    h = sender_hash(m.from_user.id, sid)
    if await one("SELECT 1 FROM blocks WHERE space_id=? AND sender_hash=?", sid, h):
        await state.clear()
        return await m.answer("امکان ارسال برای شما در این گپ وجود نداره.")
    last = await one("SELECT MAX(created_at) t FROM confessions WHERE space_id=? AND sender_hash=?", sid, h)
    if last and last["t"] and now() - last["t"] < sp["cooldown"]:
        wait = sp["cooldown"] - (now() - last["t"])
        return await m.answer(f"⏳ {wait} ثانیه دیگه می‌تونی اعتراف بعدی رو بفرستی.")
    cur = await run(
        "INSERT INTO confessions(space_id,text,sender_hash,notify_uid,created_at) VALUES(?,?,?,?,?)",
        sid, text, h, m.from_user.id, now())
    await state.clear()
    await m.answer("✅ اعتراف شما برای بررسی ادمین‌ها ارسال شد. نتیجه رو همینجا بهت خبر می‌دم.",
                   reply_markup=await main_kb(m.from_user.id))
    await dispatch_to_reviewers(sp, cur.lastrowid, text)


@r.message(Conf.text)
async def confession_not_text(m: Message):
    await m.answer("فعلاً فقط متن پشتیبانی می‌شه. متن اعتراف رو بفرست یا /cancel بزن.")


async def dispatch_to_reviewers(sp, cid, text):
    revs = await many(
        "SELECT user_id FROM reviewers WHERE space_id=? AND active=1 AND user_id IS NOT NULL", sp["id"])
    targets = [x["user_id"] for x in revs] or [sp["owner_id"]]
    kb = ikb([("✅ تایید", f"d:a:{cid}"), ("❌ رد", f"d:r:{cid}")],
             [("🚫 رد و بلاک فرستنده", f"d:b:{cid}")])
    for uid in targets:
        try:
            msg = await safe(bot.send_message, uid, review_text(sp["title"], text),
                             reply_markup=kb, parse_mode=None)
            await run("INSERT INTO review_msgs VALUES(?,?,?)", cid, uid, msg.message_id)
        except TelegramAPIError:
            pass


def review_text(title, text):
    return f"📨 اعتراف جدید برای «{title}»\n\n{text}"


@r.callback_query(F.data.startswith("d:"))
async def decide(cb: CallbackQuery):
    _, act, cid = cb.data.split(":")
    cid = int(cid)
    c = await one("SELECT * FROM confessions WHERE id=?", cid)
    if not c:
        return await cb.answer("پیدا نشد.", show_alert=True)
    rv = await one("SELECT * FROM reviewers WHERE space_id=? AND user_id=? AND active=1",
                   c["space_id"], cb.from_user.id)
    if not rv:
        return await cb.answer("دسترسی نداری.", show_alert=True)
    cur = await run("UPDATE confessions SET status=?, decided_by=? WHERE id=? AND status='pending'",
                    "approved" if act == "a" else "rejected", rv["name"], cid)
    if cur.rowcount == 0:
        return await cb.answer("این اعتراف قبلاً بررسی شده.", show_alert=True)
    sp = await one("SELECT * FROM spaces WHERE id=?", c["space_id"])
    notify = c["notify_uid"]
    try:
        if act == "a":
            async with db.execute("UPDATE spaces SET counter=counter+1 WHERE id=? RETURNING counter",
                                  (sp["id"],)) as q:
                n = (await q.fetchone())[0]
            await db.commit()
            try:
                await safe(bot.send_message, sp["chat_id"], f"{sp['tag']} #{n}\n\n{c['text']}",
                           parse_mode=None,
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                               text="✍️ ارسال اعتراف ناشناس", url=conf_link(sp["code"]))]]))
            except TelegramAPIError:
                await run("UPDATE confessions SET status='failed' WHERE id=?", cid)
                return await cb.answer("❌ ارسال به گپ ناموفق بود. ربات هنوز ادمین/عضو هست؟", show_alert=True)
            label = "✅ تایید و منتشر شد"
            await notify_user(notify, "✅ اعتراف شما تایید و منتشر شد.")
        elif act == "r":
            label = "❌ رد شد"
            await notify_user(notify, "❌ اعتراف شما تایید نشد.")
        else:
            label = "🚫 رد شد و فرستنده بلاک شد"
            await run("INSERT OR IGNORE INTO blocks VALUES(?,?)", sp["id"], c["sender_hash"])
    finally:
        # حریم خصوصی: بعد از تصمیم، شناسه‌ی فرستنده پاک می‌شود (و متن اعتراف‌های ردشده هم)
        await run("UPDATE confessions SET notify_uid=NULL, text=CASE WHEN status='approved' THEN text ELSE '' END WHERE id=?", cid)
    for mm in await many("SELECT user_id, message_id FROM review_msgs WHERE confession_id=?", cid):
        try:
            await bot.edit_message_text(
                f"{review_text(sp['title'], c['text'])}\n\n— {label} توسط «{rv['name']}»",
                chat_id=mm["user_id"], message_id=mm["message_id"], parse_mode=None, reply_markup=None)
        except TelegramAPIError:
            pass
    await run("DELETE FROM review_msgs WHERE confession_id=?", cid)
    await cb.answer("انجام شد.")


async def notify_user(uid, text):
    if uid:
        try:
            await safe(bot.send_message, uid, text)
        except TelegramAPIError:
            pass


# ───────────────────────── ثبت گروه/کانال (مالک گپ) ─────────────────────────
def add_bot_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن ربات به گروه",
                              url=f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=post_messages")],
        [InlineKeyboardButton(text="➕ افزودن ربات به کانال",
                              url=f"https://t.me/{BOT_USERNAME}?startchannel=true&admin=post_messages")]])


@r.message(F.chat_shared)
async def chat_shared(m: Message):
    cid = m.chat_shared.chat_id
    uid = m.from_user.id
    try:
        chat = await bot.get_chat(cid)
        me = await bot.get_chat_member(cid, bot.id)
        you = await bot.get_chat_member(cid, uid)
    except TelegramAPIError:
        return await m.answer("ربات هنوز داخل این گپ نیست. اول ربات رو اضافه و ادمین کن، بعد دوباره انتخابش کن 👇",
                              reply_markup=add_bot_kb())
    if you.status != ChatMemberStatus.CREATOR:
        return await m.answer("فقط «سازنده/مالک» گپ می‌تونه اون رو ثبت کنه.")
    kind = "channel" if chat.type == "channel" else "group"
    ok = me.status == ChatMemberStatus.ADMINISTRATOR if kind == "channel" else \
        me.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
    if ok and kind == "channel" and not getattr(me, "can_post_messages", False):
        ok = False
    if not ok:
        return await m.answer("ربات باید در کانال ادمین با دسترسی «ارسال پیام» باشه (در گروه هم عضو باشه). "
                              "درستش کن و دوباره انتخاب کن 👇", reply_markup=add_bot_kb())
    ex = await one("SELECT * FROM spaces WHERE chat_id=?", cid)
    if ex:
        sid = ex["id"]
        await run("UPDATE spaces SET owner_id=?, title=?, active=1 WHERE id=?", uid, chat.title, sid)
    else:
        cur = await run("INSERT INTO spaces(chat_id,title,kind,owner_id,code,created_at) VALUES(?,?,?,?,?,?)",
                        cid, chat.title, kind, uid, secrets.token_urlsafe(6), now())
        sid = cur.lastrowid
    await run("INSERT OR IGNORE INTO reviewers(space_id,user_id,name) VALUES(?,?,?)", sid, uid, "مالک")
    await m.answer("✅ ثبت شد. تو به‌صورت پیش‌فرض ادمین بررسی‌کننده‌ی این گپ هستی.",
                   reply_markup=await main_kb(uid))
    sp = await one("SELECT * FROM spaces WHERE id=?", sid)
    t, k = await panel(sp)
    await m.answer(t, reply_markup=k)


@r.my_chat_member()
async def on_member(ev: ChatMemberUpdated):
    if ev.chat.type == "private":
        return
    if ev.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        sp = await one("SELECT * FROM spaces WHERE chat_id=?", ev.chat.id)
        if sp:
            await run("UPDATE spaces SET active=0 WHERE id=?", sp["id"])
            await notify_user(sp["owner_id"], f"⚠️ ربات از «{sp['title']}» حذف شد و دریافت اعتراف متوقف شد.")


# ───────────────────────── پنل مالک گپ ─────────────────────────
async def panel(sp):
    nrev = (await one("SELECT COUNT(*) c FROM reviewers WHERE space_id=? AND user_id IS NOT NULL AND active=1",
                      sp["id"]))["c"]
    npend = (await one("SELECT COUNT(*) c FROM confessions WHERE space_id=? AND status='pending'", sp["id"]))["c"]
    icon = "📢" if sp["kind"] == "channel" else "👥"
    t = (f"{icon} <b>{esc(sp['title'])}</b>\n\n"
         f"🔗 لینک اعتراف (بین اعضا پخش کن):\n<code>{conf_link(sp['code'])}</code>\n\n"
         f"👤 ادمین‌های فعال: {nrev}\n⏳ در انتظار بررسی: {npend}\n"
         f"📊 منتشرشده: {sp['counter']}\n"
         f"وضعیت: {'فعال ✅' if sp['active'] else 'متوقف ⏸'}")
    s = sp["id"]
    k = ikb([("👥 ادمین‌ها", f"rv:{s}"), ("⚙️ تنظیمات", f"st:{s}")],
            [("⏸ توقف" if sp["active"] else "▶️ فعال‌سازی", f"tg:{s}"), ("♻️ لینک جدید", f"nl:{s}")],
            [("📌 ارسال دکمه در گپ", f"pn:{s}"), ("🚫 بلاک‌ها", f"bl:{s}")],
            [("🗑 حذف گپ", f"dl:{s}")])
    return t, k


async def owned(cb: CallbackQuery, sid):
    sp = await one("SELECT * FROM spaces WHERE id=? AND owner_id=?", sid, cb.from_user.id)
    if not sp:
        await cb.answer("دسترسی نداری.", show_alert=True)
    return sp


@r.message(StateFilter(None), F.text == "📂 گپ‌های من")
async def my_spaces(m: Message):
    rows = await many("SELECT id,title,kind FROM spaces WHERE owner_id=? ORDER BY id DESC LIMIT 90", m.from_user.id)
    if not rows:
        return await m.answer("هنوز گپی ثبت نکردی. از دکمه‌های «➕ افزودن» استفاده کن.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=("📢 " if x["kind"] == "channel" else "👥 ") + (x["title"] or "؟")[:40],
        callback_data=f"sp:{x['id']}")] for x in rows])
    await m.answer("گپ‌های تو:", reply_markup=kb)


@r.callback_query(F.data.startswith("sp:"))
async def cb_space(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        t, k = await panel(sp)
        await show(cb, t, k)


@r.callback_query(F.data.startswith("tg:"))
async def cb_toggle(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await run("UPDATE spaces SET active=1-active WHERE id=?", sp["id"])
        t, k = await panel(await one("SELECT * FROM spaces WHERE id=?", sp["id"]))
        await show(cb, t, k)


@r.callback_query(F.data.startswith("nl:"))
async def cb_newlink(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await run("UPDATE spaces SET code=? WHERE id=?", secrets.token_urlsafe(6), sp["id"])
        t, k = await panel(await one("SELECT * FROM spaces WHERE id=?", sp["id"]))
        await show(cb, "♻️ لینک قبلی باطل شد.\n\n" + t, k)


@r.callback_query(F.data.startswith("pn:"))
async def cb_post_button(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if not sp:
        return
    try:
        await bot.send_message(sp["chat_id"], "🤫 اعتراف ناشناس بده؛ هویتت مخفی می‌مونه.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                   text="✍️ ارسال اعتراف ناشناس", url=conf_link(sp["code"]))]]))
        await cb.answer("ارسال شد؛ می‌تونی پینش کنی 📌", show_alert=True)
    except TelegramAPIError:
        await cb.answer("ارسال نشد. دسترسی ربات رو چک کن.", show_alert=True)


@r.callback_query(F.data.startswith("dl:"))
async def cb_delete(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await show(cb, f"مطمئنی «{esc(sp['title'])}» و همه‌ی داده‌هاش حذف بشه؟",
                   ikb([("✅ بله، حذف کن", f"dy:{sp['id']}"), ("🔙 نه", f"sp:{sp['id']}")]))


@r.callback_query(F.data.startswith("dy:"))
async def cb_delete_yes(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await run("DELETE FROM spaces WHERE id=?", sp["id"])
        await show(cb, "🗑 حذف شد.")


@r.callback_query(F.data.startswith("bl:"))
async def cb_blocks(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        n = (await one("SELECT COUNT(*) c FROM blocks WHERE space_id=?", sp["id"]))["c"]
        await show(cb, f"🚫 تعداد فرستنده‌های بلاک‌شده: {n}\n(هویت‌ها ناشناس‌اند و قابل مشاهده نیستند)",
                   ikb([("🧹 پاک کردن همه‌ی بلاک‌ها", f"bc:{sp['id']}")], [("🔙", f"sp:{sp['id']}")]))


@r.callback_query(F.data.startswith("bc:"))
async def cb_blocks_clear(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await run("DELETE FROM blocks WHERE space_id=?", sp["id"])
        await show(cb, "🧹 همه‌ی بلاک‌ها پاک شد.", ikb([("🔙", f"sp:{sp['id']}")]))


# ───────────────────────── تنظیمات گپ ─────────────────────────
CD = [0, 30, 60, 300, 1800]
ML = [500, 1000, 1500, 3000]


async def settings_view(cb, sid):
    sp = await one("SELECT * FROM spaces WHERE id=?", sid)
    await show(cb, (f"⚙️ تنظیمات «{esc(sp['title'])}»\n\n"
                    f"⏱ فاصله‌ی بین دو اعتراف هر نفر: {fmt_sec(sp['cooldown'])}\n"
                    f"📏 حداکثر طول: {sp['max_len']} کاراکتر\n"
                    f"🏷 تگ بالای پست: {esc(sp['tag'])}"),
               ikb([("⏱ تغییر فاصله", f"sc:{sid}"), ("📏 تغییر طول", f"sm:{sid}")],
                   [("🏷 تغییر تگ", f"stg:{sid}")], [("🔙", f"sp:{sid}")]))


@r.callback_query(F.data.startswith("st:"))
async def cb_settings(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await settings_view(cb, sp["id"])


@r.callback_query(F.data.startswith("sc:"))
async def cb_cd(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        nxt = CD[(CD.index(sp["cooldown"]) + 1) % len(CD)] if sp["cooldown"] in CD else CD[0]
        await run("UPDATE spaces SET cooldown=? WHERE id=?", nxt, sp["id"])
        await settings_view(cb, sp["id"])


@r.callback_query(F.data.startswith("sm:"))
async def cb_ml(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        nxt = ML[(ML.index(sp["max_len"]) + 1) % len(ML)] if sp["max_len"] in ML else ML[0]
        await run("UPDATE spaces SET max_len=? WHERE id=?", nxt, sp["id"])
        await settings_view(cb, sp["id"])


@r.callback_query(F.data.startswith("stg:"))
async def cb_tag(cb: CallbackQuery, state: FSMContext):
    sp = await owned(cb, int(cb.data[4:]))
    if sp:
        await state.set_state(Tag.tag)
        await state.update_data(sid=sp["id"])
        await cb.message.answer("تگ جدید رو بفرست (مثلاً #اعتراف یا 🤫 اعتراف). حداکثر ۳۰ کاراکتر. /cancel")
        await cb.answer()


@r.message(Tag.tag, F.text)
async def got_tag(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    if len(m.text) > 30:
        return await m.answer("خیلی طولانیه (حداکثر ۳۰).")
    await run("UPDATE spaces SET tag=? WHERE id=? AND owner_id=?", m.text.strip(), sid, m.from_user.id)
    await state.clear()
    await m.answer("✅ تگ تغییر کرد.", reply_markup=await main_kb(m.from_user.id))


# ───────────────────────── ادمین‌ها (نام دلخواه + لینک دعوت) ─────────────────────────
async def own_rev(cb: CallbackQuery, rid):
    rv = await one("SELECT r.* FROM reviewers r JOIN spaces s ON s.id=r.space_id WHERE r.id=? AND s.owner_id=?",
                   rid, cb.from_user.id)
    if not rv:
        await cb.answer("دسترسی نداری.", show_alert=True)
    return rv


async def reviewers_view(cb, sid):
    rows = await many("SELECT * FROM reviewers WHERE space_id=? ORDER BY id", sid)
    kb = [[InlineKeyboardButton(
        text=f"{'⏳' if not x['user_id'] else ('✅' if x['active'] else '⏸')} {x['name']}",
        callback_data=f"rx:{x['id']}")] for x in rows]
    kb.append([InlineKeyboardButton(text="➕ افزودن ادمین جدید", callback_data=f"ra:{sid}")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data=f"sp:{sid}")])
    await show(cb, "👥 ادمین‌های بررسی‌کننده\n✅ فعال  ⏸ غیرفعال  ⏳ منتظر قبول دعوت",
               InlineKeyboardMarkup(inline_keyboard=kb))


@r.callback_query(F.data.startswith("rv:"))
async def cb_reviewers(cb: CallbackQuery):
    sp = await owned(cb, int(cb.data[3:]))
    if sp:
        await reviewers_view(cb, sp["id"])


@r.callback_query(F.data.startswith("ra:"))
async def cb_add_reviewer(cb: CallbackQuery, state: FSMContext):
    sp = await owned(cb, int(cb.data[3:]))
    if not sp:
        return
    n = (await one("SELECT COUNT(*) c FROM reviewers WHERE space_id=?", sp["id"]))["c"]
    if n >= MAX_REVIEWERS:
        return await cb.answer(f"حداکثر {MAX_REVIEWERS} ادمین.", show_alert=True)
    await state.set_state(Inv.name)
    await state.update_data(sid=sp["id"])
    await cb.message.answer("یه اسم دلخواه برای این ادمین بنویس (فقط برای خودته، مثلاً «علی» یا «مدیر شب»). /cancel")
    await cb.answer()


@r.message(Inv.name, F.text)
async def got_inv_name(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    name = m.text.strip()[:30]
    token = secrets.token_urlsafe(8)
    await run("INSERT INTO reviewers(space_id,name,token,token_exp) VALUES(?,?,?,?)",
              sid, name, token, now() + INVITE_TTL)
    await state.clear()
    await m.answer(f"✅ دعوت‌نامه برای «{esc(name)}» ساخته شد.\n\nاین لینک رو براش بفرست "
                   f"(یک‌بار مصرف، ۷۲ ساعت اعتبار):\n<code>{inv_link(token)}</code>",
                   reply_markup=await main_kb(m.from_user.id))


async def show_invite(m: Message, token):
    rv = await one("SELECT r.*, s.title FROM reviewers r JOIN spaces s ON s.id=r.space_id "
                   "WHERE r.token=? AND r.user_id IS NULL AND r.token_exp>?", token, now())
    if not rv:
        return await m.answer("این دعوت‌نامه نامعتبر یا منقضی شده.")
    await m.answer(f"به عنوان ادمین بررسی‌کننده‌ی «<b>{esc(rv['title'])}</b>» دعوت شدی.\nقبول می‌کنی؟",
                   reply_markup=ikb([("✅ قبول می‌کنم", f"ia:{token}"), ("❌ نه", "ix")]))


@r.callback_query(F.data == "ix")
async def cb_invite_no(cb: CallbackQuery):
    await show(cb, "باشه، دعوت نادیده گرفته شد.")


@r.callback_query(F.data.startswith("ia:"))
async def cb_invite_yes(cb: CallbackQuery):
    token = cb.data[3:]
    rv = await one("SELECT r.*, s.title, s.owner_id FROM reviewers r JOIN spaces s ON s.id=r.space_id "
                   "WHERE r.token=? AND r.user_id IS NULL AND r.token_exp>?", token, now())
    if not rv:
        return await show(cb, "این دعوت‌نامه نامعتبر یا منقضی شده.")
    try:
        await run("UPDATE reviewers SET user_id=?, token=NULL WHERE id=?", cb.from_user.id, rv["id"])
    except sqlite3.IntegrityError:
        return await show(cb, "تو قبلاً ادمین همین گپ هستی.")
    await show(cb, f"✅ از الان ادمین «{esc(rv['title'])}» هستی. اعتراف‌ها همینجا برات میاد.")
    await bot.send_message(cb.from_user.id, "منوی ربات:", reply_markup=await main_kb(cb.from_user.id))
    await notify_user(rv["owner_id"], f"✅ «{rv['name']}» دعوتت رو برای «{rv['title']}» قبول کرد.")


@r.callback_query(F.data.startswith("rx:"))
async def cb_rev_detail(cb: CallbackQuery):
    rv = await own_rev(cb, int(cb.data[3:]))
    if not rv:
        return
    st = "⏳ منتظر قبول دعوت" if not rv["user_id"] else ("✅ فعال" if rv["active"] else "⏸ غیرفعال")
    rows = [[("✏️ تغییر نام", f"rn:{rv['id']}")]]
    if not rv["user_id"]:
        rows.append([("🔗 لینک دعوت جدید", f"ri:{rv['id']}")])
    else:
        rows.append([("⏯ فعال/غیرفعال", f"rt:{rv['id']}")])
    rows.append([("🗑 حذف", f"rd:{rv['id']}"), ("🔙", f"rv:{rv['space_id']}")])
    await show(cb, f"👤 <b>{esc(rv['name'])}</b>\nوضعیت: {st}", ikb(*rows))


@r.callback_query(F.data.startswith("ri:"))
async def cb_rev_reinvite(cb: CallbackQuery):
    rv = await own_rev(cb, int(cb.data[3:]))
    if rv and not rv["user_id"]:
        token = secrets.token_urlsafe(8)
        await run("UPDATE reviewers SET token=?, token_exp=? WHERE id=?", token, now() + INVITE_TTL, rv["id"])
        await cb.message.answer(f"🔗 لینک جدید (۷۲ ساعت):\n<code>{inv_link(token)}</code>")
        await cb.answer()


@r.callback_query(F.data.startswith("rt:"))
async def cb_rev_toggle(cb: CallbackQuery):
    rv = await own_rev(cb, int(cb.data[3:]))
    if rv:
        await run("UPDATE reviewers SET active=1-active WHERE id=?", rv["id"])
        await reviewers_view(cb, rv["space_id"])


@r.callback_query(F.data.startswith("rd:"))
async def cb_rev_delete(cb: CallbackQuery):
    rv = await own_rev(cb, int(cb.data[3:]))
    if rv:
        await run("DELETE FROM reviewers WHERE id=?", rv["id"])
        await reviewers_view(cb, rv["space_id"])


@r.callback_query(F.data.startswith("rn:"))
async def cb_rev_rename(cb: CallbackQuery, state: FSMContext):
    rv = await own_rev(cb, int(cb.data[3:]))
    if rv:
        await state.set_state(Ren.name)
        await state.update_data(rid=rv["id"])
        await cb.message.answer("اسم جدید رو بفرست. /cancel")
        await cb.answer()


@r.message(Ren.name, F.text)
async def got_rename(m: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    await run("UPDATE reviewers SET name=? WHERE id=? AND space_id IN (SELECT id FROM spaces WHERE owner_id=?)",
              m.text.strip()[:30], rid, m.from_user.id)
    await state.clear()
    await m.answer("✅ تغییر کرد.", reply_markup=await main_kb(m.from_user.id))


# ───────────────────────── پنل مالک ربات ─────────────────────────
@r.message(Command("owner"))
async def claim_owner(m: Message, command: CommandObject):
    if await get_setting("owner_id") or not command.args:
        return
    if hmac.compare_digest(command.args.strip().encode(), OWNER_KEY.encode()):
        await run("INSERT OR REPLACE INTO settings VALUES('owner_id',?)", str(m.from_user.id))
        await m.answer("👑 تو مالک ربات شدی.", reply_markup=await main_kb(m.from_user.id))


@r.message(StateFilter(None), F.text == "👑 پنل مالک ربات")
async def owner_panel(m: Message):
    if await is_bot_owner(m.from_user.id):
        await m.answer("👑 پنل مالک", reply_markup=await owner_kb())


async def owner_kb():
    auto = await get_setting("auto_backup") != "0"
    return ikb([("📊 آمار", "o:stats")], [("📢 پیام همگانی", "o:bc")],
               [("🚫 مسدود/آزادسازی گپ", "o:ban")],
               [("💾 دریافت بکاپ", "o:backup")],
               [(f"🔄 بکاپ خودکار ۲۴ساعته: {'روشن ✅' if auto else 'خاموش ⏸'}", "o:abk")])


# ───────────────────────── بکاپ ─────────────────────────
README_BK = (
    "بازیابی بکاپ:\n"
    "1) ربات رو متوقف کن.\n"
    "2) فایل confess.db داخل این زیپ رو جای دیتابیس فعلی بذار (DB_PATH در .env).\n"
    "3) مقدار SECRET_SALT رو از env_backup.txt در .env بذار (همون قبلی باید باشه وگرنه بلاک‌ها بی‌اثر می‌شن).\n"
    "4) BOT_TOKEN رو از BotFather بگیر و در .env بذار (عمداً داخل بکاپ نیست).\n"
    "5) pip install -r requirements.txt و بعد python bot.py\n")


def _make_zip(db_copy: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy, "confess.db")
        z.writestr("env_backup.txt", f"SECRET_SALT={SALT.decode()}\nOWNER_KEY={OWNER_KEY}\nDB_PATH={DB_PATH}\n")
        z.writestr("README.txt", README_BK)
        for name in ("bot.py", "requirements.txt"):
            f = BASE_DIR / name
            if f.exists():
                z.write(f, name)


async def send_backup(chat_id: int, reason: str) -> bool:
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "confess.db"
        await db.commit()
        await db.execute("VACUUM INTO ?", (str(copy),))   # کپی سازگار از دیتابیس، بدون توقف ربات
        zp = Path(d) / f"backup_{time.strftime('%Y%m%d_%H%M')}.zip"
        await asyncio.to_thread(_make_zip, copy, zp)
        size = zp.stat().st_size
        if size > 49 * 1024 * 1024:
            await bot.send_message(chat_id, f"⚠️ حجم بکاپ {size // 1048576}MB است و از سقف ارسال تلگرام (۵۰MB) بیشتره. "
                                            "فایل confess.db رو دستی از سرور بردار.")
            return False
        await safe(bot.send_document, chat_id, FSInputFile(zp),
                   caption=f"💾 بکاپ {reason}\n📦 {size // 1024} KB\nراهنمای بازیابی داخل زیپ هست.")
    await run("INSERT OR REPLACE INTO settings VALUES('last_backup',?)", str(now()))
    return True


async def backup_loop():
    while True:
        try:
            last = int(await get_setting("last_backup") or 0)
            await asyncio.sleep(max(60, last + BACKUP_EVERY - now()))
            owner = await get_setting("owner_id")
            if owner and await get_setting("auto_backup") != "0":
                await send_backup(int(owner), "خودکار")
            else:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("backup failed")
            await asyncio.sleep(600)


@r.callback_query(F.data == "o:backup")
async def o_backup(cb: CallbackQuery):
    if not await is_bot_owner(cb.from_user.id):
        return await cb.answer()
    await cb.answer("در حال ساخت بکاپ…")
    try:
        await send_backup(cb.from_user.id, "دستی")
    except Exception:
        logging.exception("manual backup failed")
        await cb.message.answer("❌ ساخت بکاپ ناموفق بود. لاگ ربات رو ببین.")


@r.callback_query(F.data == "o:abk")
async def o_auto_backup(cb: CallbackQuery):
    if not await is_bot_owner(cb.from_user.id):
        return await cb.answer()
    cur = await get_setting("auto_backup") != "0"
    await run("INSERT OR REPLACE INTO settings VALUES('auto_backup',?)", "0" if cur else "1")
    try:
        await cb.message.edit_reply_markup(reply_markup=await owner_kb())
    except TelegramBadRequest:
        pass
    await cb.answer()


@r.callback_query(F.data == "o:stats")
async def o_stats(cb: CallbackQuery):
    if not await is_bot_owner(cb.from_user.id):
        return await cb.answer()
    sp = await one("SELECT COUNT(*) c, SUM(active) a FROM spaces")
    cf = await one("SELECT COUNT(*) c, SUM(status='approved') a, SUM(status='pending') p FROM confessions")
    ow = (await one("SELECT COUNT(DISTINCT owner_id) c FROM spaces"))["c"]
    await show(cb, f"📊 گپ‌ها: {sp['c']} (فعال: {sp['a'] or 0})\n👤 مالک‌ها: {ow}\n"
                   f"📨 اعتراف‌ها: {cf['c']} | منتشرشده: {cf['a'] or 0} | در انتظار: {cf['p'] or 0}")


@r.callback_query(F.data == "o:bc")
async def o_bc(cb: CallbackQuery, state: FSMContext):
    if await is_bot_owner(cb.from_user.id):
        await state.set_state(Bc.text)
        await cb.message.answer("متن پیام همگانی (برای مالک‌های گپ‌ها). /cancel")
        await cb.answer()


@r.message(Bc.text, F.text)
async def do_bc(m: Message, state: FSMContext):
    await state.clear()
    ok = bad = 0
    for x in await many("SELECT DISTINCT owner_id FROM spaces"):
        try:
            await safe(bot.send_message, x["owner_id"], m.text, parse_mode=None)
            ok += 1
        except TelegramAPIError:
            bad += 1
        await asyncio.sleep(0.05)
    await m.answer(f"ارسال شد: {ok} | ناموفق: {bad}")


@r.callback_query(F.data == "o:ban")
async def o_ban(cb: CallbackQuery, state: FSMContext):
    if await is_bot_owner(cb.from_user.id):
        await state.set_state(Ban.link)
        await cb.message.answer("لینک اعتراف همون گپ رو بفرست تا مسدود/آزاد بشه. /cancel")
        await cb.answer()


@r.message(Ban.link, F.text)
async def do_ban(m: Message, state: FSMContext):
    code = m.text.strip().split("start=c_")[-1]
    sp = await one("SELECT * FROM spaces WHERE code=?", code)
    await state.clear()
    if not sp:
        return await m.answer("گپی با این لینک پیدا نشد.")
    await run("UPDATE spaces SET banned=1-banned WHERE id=?", sp["id"])
    await m.answer(f"«{esc(sp['title'])}»: {'🚫 مسدود شد' if not sp['banned'] else '✅ آزاد شد'}")


# ───────────────────────── اجرا ─────────────────────────
async def main():
    global db, bot, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(SCHEMA)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    asyncio.create_task(backup_loop())
    dp = Dispatcher()
    dp.include_router(r)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
