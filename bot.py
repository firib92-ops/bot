import asyncio
import logging
import json
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.exceptions import TelegramBadRequest
import aiohttp
import aiosqlite

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = "8785816519:AAEZBsriAk182crzy7xZbXoJcE-ztCyeiqk"
CHANNEL_ID   = -1003820751232
CHANNEL_LINK = "https://t.me/growagarden2track"
STOCK_API    = "https://grow-a-garden-2-tracker.onrender.com/api/stock"
ADMIN_CODE   = "GrehI07"
DB_PATH      = "bot.db"
POLL_SEC     = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── STATES ──────────────────────────────────────────────────────────────────
class Admin(StatesGroup):
    code        = State()
    bcast_type  = State()
    bcast_text  = State()
    bcast_sched = State()
    find_user   = State()
    ban_id      = State()
    unban_id    = State()

# ─── DB ──────────────────────────────────────────────────────────────────────
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY,
                username TEXT, name TEXT,
                joined   TEXT, banned INTEGER DEFAULT 0,
                active   TEXT
            );
            CREATE TABLE IF NOT EXISTS subs (
                uid  INTEGER,
                seed TEXT,
                PRIMARY KEY (uid, seed)
            );
            CREATE TABLE IF NOT EXISTS notif_log (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                uid  INTEGER, seed TEXT, at TEXT
            );
            CREATE TABLE IF NOT EXISTS admins (
                uid INTEGER PRIMARY KEY,
                at  TEXT
            );
        """)
        await db.commit()

async def save_user(u: types.User):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (id,username,name,joined,active)
            VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              username=excluded.username,
              name=excluded.name,
              active=excluded.active
        """, (u.id, u.username, u.full_name, now, now))
        await db.commit()

async def banned(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT banned FROM users WHERE id=?", (uid,)) as c:
            r = await c.fetchone()
            return bool(r and r[0])

async def is_admin(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE uid=?", (uid,)) as c:
            return bool(await c.fetchone())

async def set_admin(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO admins VALUES (?,?)",
                         (uid, datetime.now().isoformat()))
        await db.commit()

async def get_subs(uid: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT seed FROM subs WHERE uid=?", (uid,)) as c:
            return [r[0] for r in await c.fetchall()]

async def toggle(uid: int, seed: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM subs WHERE uid=? AND seed=?", (uid, seed)) as c:
            exists = await c.fetchone()
        if exists:
            await db.execute("DELETE FROM subs WHERE uid=? AND seed=?", (uid, seed))
            await db.commit()
            return False
        await db.execute("INSERT INTO subs VALUES (?,?)", (uid, seed))
        await db.commit()
        return True

async def all_users() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users WHERE banned=0") as c:
            return [r[0] for r in await c.fetchall()]

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned=0") as c:
            active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT uid) FROM subs") as c:
            notif = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM subs") as c:
            total_subs = (await c.fetchone())[0]
        today = datetime.now().date().isoformat()
        async with db.execute("SELECT COUNT(*) FROM users WHERE joined LIKE ?", (f"{today}%",)) as c:
            new_today = (await c.fetchone())[0]
        async with db.execute("""
            SELECT seed, COUNT(*) cnt FROM subs
            GROUP BY seed ORDER BY cnt DESC LIMIT 10
        """) as c:
            top = await c.fetchall()
        async with db.execute("""
            SELECT strftime('%H',at) hr, COUNT(*) FROM notif_log
            GROUP BY hr ORDER BY hr
        """) as c:
            hourly = await c.fetchall()
        async with db.execute("""
            SELECT date(joined) d, COUNT(*) FROM users
            WHERE joined >= date('now','-7 days')
            GROUP BY d ORDER BY d
        """) as c:
            daily = await c.fetchall()
    return dict(total=total, active=active, banned=total-active,
                notif=notif, total_subs=total_subs,
                new_today=new_today, top=top,
                hourly=hourly, daily=daily)

# ─── STOCK ───────────────────────────────────────────────────────────────────
_stock_cache: dict = {}
_stock_time: float = 0

async def fetch_stock() -> dict:
    global _stock_cache, _stock_time
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(STOCK_API, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    _stock_cache = await r.json()
                    _stock_time  = time.time()
    except Exception as e:
        log.warning(f"stock fetch: {e}")
    return _stock_cache

def _all_items(stock: dict) -> list[dict]:
    """Flatten stock to list of {name, qty, price, category}"""
    items = []
    for cat, val in stock.items():
        if isinstance(val, dict):
            for name, info in val.items():
                if isinstance(info, dict):
                    items.append(dict(
                        name=name, cat=cat,
                        qty=info.get("quantity", info.get("stock", info.get("amount", "?"))),
                        price=info.get("price", info.get("cost", ""))
                    ))
                else:
                    items.append(dict(name=name, cat=cat, qty=info, price=""))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    n = item.get("name", item.get("id", "?"))
                    items.append(dict(
                        name=n, cat=cat,
                        qty=item.get("quantity", item.get("stock", item.get("amount", "?"))),
                        price=item.get("price", item.get("cost", ""))
                    ))
    return items

def format_stock(stock: dict) -> str:
    if not stock:
        return "⚠️ Не удалось получить данные стока."
    items = _all_items(stock)
    if not items:
        return "⚠️ Сток пуст или неизвестный формат API."

    lines = [f"🌱 <b>Grow a Garden 2 — Сток</b>",
             f"🕐 {datetime.now().strftime('%H:%M:%S  %d.%m.%Y')}\n"]

    by_cat: dict[str, list] = {}
    for it in items:
        by_cat.setdefault(it["cat"], []).append(it)

    for cat, its in by_cat.items():
        lines.append(f"\n<b>📦 {cat}</b>")
        for it in its:
            p = f"  💰{it['price']}" if it["price"] else ""
            lines.append(f"  • <b>{it['name']}</b>: {it['qty']}{p}")
    return "\n".join(lines)

def all_seeds(stock: dict) -> list:
    items = _all_items(stock)
    seeds = []
    for it in items:
        n, c = it["name"], it["cat"]
        if any(w in (n+c).lower() for w in ("seed","plant","crop","flower","tree","bush")):
            seeds.append(n)
    if not seeds:
        seeds = [it["name"] for it in items]
    return sorted(set(seeds))

def in_stock(stock: dict, seed: str) -> bool:
    for it in _all_items(stock):
        if it["name"] == seed:
            try:
                return int(it["qty"]) > 0
            except:
                return bool(it["qty"]) and it["qty"] not in ("0","","?")
    return False

# ─── CHANNEL CHECK ───────────────────────────────────────────────────────────
async def check_sub(uid: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        return m.status not in ("left","kicked")
    except:
        return False

def kb_sub():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_LINK),
        InlineKeyboardButton(text="✅ Проверить", callback_data="chk_sub")
    ]])

def kb_main():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📦 Сток"),
         KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="ℹ️ О боте")]
    ], resize_keyboard=True)

async def need_sub(msg: types.Message) -> bool:
    if not await check_sub(msg.from_user.id):
        await msg.answer("❗ Сначала подпишись на канал:", reply_markup=kb_sub())
        return False
    return True

# ─── HANDLERS ────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def h_start(msg: types.Message):
    await save_user(msg.from_user)
    if await banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")
    await msg.answer(
        "🌱 <b>Grow a Garden 2 Stock Bot</b>\n\n"
        "📦 Смотри текущий сток\n"
        "🔔 Настраивай уведомления на нужные seeds\n\n"
        "Для работы нужна подписка на канал 👇",
        parse_mode="HTML", reply_markup=kb_sub()
    )

@dp.callback_query(F.data == "chk_sub")
async def h_chk(call: types.CallbackQuery):
    await save_user(call.from_user)
    if await check_sub(call.from_user.id):
        await call.message.edit_text("✅ <b>Подписка подтверждена!</b>", parse_mode="HTML")
        await call.message.answer("Выбери действие:", reply_markup=kb_main())
    else:
        await call.answer("❌ Ты ещё не подписался!", show_alert=True)

@dp.message(F.text == "📦 Сток")
async def h_stock(msg: types.Message):
    await save_user(msg.from_user)
    if await banned(msg.from_user.id): return
    if not await need_sub(msg): return
    w = await msg.answer("⏳ Загружаю...")
    stock = await fetch_stock()
    text  = format_stock(stock)
    kb    = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="ref_stock")
    ]])
    try:
        await w.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await w.delete()
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "ref_stock")
async def h_ref(call: types.CallbackQuery):
    if not await check_sub(call.from_user.id):
        return await call.answer("❗ Подпишись на канал!", show_alert=True)
    stock = await fetch_stock()
    try:
        await call.message.edit_text(
            format_stock(stock), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Обновить", callback_data="ref_stock")
            ]])
        )
        await call.answer("✅")
    except TelegramBadRequest:
        await call.answer("Уже актуально!")

@dp.message(F.text == "🔔 Уведомления")
async def h_notif(msg: types.Message):
    await save_user(msg.from_user)
    if await banned(msg.from_user.id): return
    if not await need_sub(msg): return
    stock = await fetch_stock()
    seeds = all_seeds(stock)
    my    = await get_subs(msg.from_user.id)
    if not seeds:
        return await msg.answer("⚠️ Не удалось загрузить seeds. Попробуй позже.")
    await msg.answer(
        "🔔 <b>Уведомления о seeds</b>\n\n"
        "Тапни на seed чтобы включить/выключить уведомление.\n"
        "✅ = включено  ⬜ = выключено",
        parse_mode="HTML",
        reply_markup=seeds_kb(seeds, my)
    )

def seeds_kb(seeds: list, my: list) -> InlineKeyboardMarkup:
    rows = []
    for s in seeds:
        icon = "✅" if s in my else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {s}", callback_data=f"ts:{s}")])
    rows.append([InlineKeyboardButton(text="❌ Отключить все", callback_data="unsub_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith("ts:"))
async def h_toggle(call: types.CallbackQuery):
    seed = call.data[3:]
    subbed = await toggle(call.from_user.id, seed)
    await call.answer(f"{'✅ Включено' if subbed else '⬜ Выключено'}: {seed}")
    stock = await fetch_stock()
    my    = await get_subs(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=seeds_kb(all_seeds(stock), my))
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data == "unsub_all")
async def h_unsub_all(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subs WHERE uid=?", (call.from_user.id,))
        await db.commit()
    await call.answer("❌ Все уведомления отключены")
    stock = await fetch_stock()
    try:
        await call.message.edit_reply_markup(reply_markup=seeds_kb(all_seeds(stock), []))
    except TelegramBadRequest:
        pass

@dp.message(F.text == "ℹ️ О боте")
async def h_about(msg: types.Message):
    await msg.answer(
        "🌱 <b>Grow a Garden 2 Stock Bot</b>\n\n"
        "Отслеживает сток игры в реальном времени.\n"
        "Уведомляет когда нужный seed появляется в продаже.\n\n"
        "🔄 Обновление каждую минуту\n"
        f"📢 Канал: {CHANNEL_LINK}",
        parse_mode="HTML"
    )

# ─── ADMIN ───────────────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def h_admin(msg: types.Message, state: FSMContext):
    if await is_admin(msg.from_user.id):
        return await show_panel(msg)
    await msg.answer("🔐 Введи код администратора:")
    await state.set_state(Admin.code)

@dp.message(Admin.code)
async def h_code(msg: types.Message, state: FSMContext):
    await state.clear()
    if msg.text.strip() == ADMIN_CODE:
        await set_admin(msg.from_user.id)
        await msg.answer("✅ Доступ открыт!")
        await show_panel(msg)
    else:
        await msg.answer("❌ Неверный код.")

async def panel_text() -> str:
    s = await get_stats()
    return (
        f"🛠 <b>Панель администратора</b>\n\n"
        f"👥 Всего: <b>{s['total']}</b>\n"
        f"✅ Активных: <b>{s['active']}</b>\n"
        f"🚫 Забанено: <b>{s['banned']}</b>\n"
        f"🔔 С уведомлениями: <b>{s['notif']}</b>\n"
        f"📌 Подписок на seeds: <b>{s['total_subs']}</b>\n"
        f"🆕 Новых сегодня: <b>{s['new_today']}</b>"
    )

def panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка",    callback_data="adm:bcast")],
        [InlineKeyboardButton(text="👤 Пользователи",callback_data="adm:users")],
        [InlineKeyboardButton(text="📊 Статистика",  callback_data="adm:stats")],
        [InlineKeyboardButton(text="🔄 Обновить",    callback_data="adm:refresh")]
    ])

async def show_panel(msg: types.Message):
    await msg.answer(await panel_text(), parse_mode="HTML", reply_markup=panel_kb())

@dp.callback_query(F.data == "adm:refresh")
async def adm_refresh(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    try:
        await call.message.edit_text(await panel_text(), parse_mode="HTML", reply_markup=panel_kb())
    except TelegramBadRequest:
        pass
    await call.answer("✅")

@dp.callback_query(F.data == "adm:bcast")
async def adm_bcast(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Всем",               callback_data="bcast:all")],
        [InlineKeyboardButton(text="📢 Подписчикам канала", callback_data="bcast:channel")],
        [InlineKeyboardButton(text="🧪 Тест (себе)",        callback_data="bcast:test")],
        [InlineKeyboardButton(text="⏰ Запланировать",      callback_data="bcast:sched")],
        [InlineKeyboardButton(text="◀️ Назад",              callback_data="adm:back")]
    ])
    await call.message.edit_text("📢 <b>Рассылка</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("bcast:"))
async def adm_bcast_type(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    btype = call.data.split(":")[1]
    await state.update_data(btype=btype)
    if btype == "sched":
        await call.message.edit_text(
            "⏰ Введи в формате:\n<code>ДД.ММ ЧЧ:ММ\nТекст сообщения</code>",
            parse_mode="HTML"
        )
        await state.set_state(Admin.bcast_sched)
    else:
        await call.message.edit_text("✏️ Введи текст сообщения:")
        await state.set_state(Admin.bcast_text)

@dp.message(Admin.bcast_text)
async def adm_do_bcast(msg: types.Message, state: FSMContext):
    data  = await state.get_data()
    btype = data.get("btype","all")
    await state.clear()
    users = await all_users()
    sent, failed = 0, 0
    st = await msg.answer("📤 Рассылаю...")
    if btype == "test":
        try:
            await bot.send_message(msg.from_user.id,
                f"🧪 <b>Тест:</b>\n\n{msg.text}", parse_mode="HTML")
            sent = 1
        except: failed = 1
    else:
        for uid in users:
            if btype == "channel" and not await check_sub(uid): continue
            try:
                await bot.send_message(uid, msg.text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except: failed += 1
    await st.edit_text(f"✅ Готово!\n✉️ Отправлено: {sent}\n❌ Ошибок: {failed}")

@dp.message(Admin.bcast_sched)
async def adm_sched(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        lines  = msg.text.strip().split("\n", 1)
        dt     = datetime.strptime(f"{datetime.now().year} {lines[0].strip()}", "%Y %d.%m %H:%M")
        text   = lines[1].strip() if len(lines) > 1 else ""
        delay  = (dt - datetime.now()).total_seconds()
        if delay < 0: return await msg.answer("❌ Время уже прошло!")
        asyncio.create_task(_delayed(delay, text, await all_users()))
        await msg.answer(f"✅ Запланировано на {dt.strftime('%d.%m %H:%M')}")
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")

async def _delayed(delay: float, text: str, users: list):
    await asyncio.sleep(delay)
    for uid in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except: pass

@dp.callback_query(F.data == "adm:users")
async def adm_users(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск по ID",callback_data="usr:find")],
        [InlineKeyboardButton(text="🚫 Забанить",   callback_data="usr:ban")],
        [InlineKeyboardButton(text="✅ Разбанить",  callback_data="usr:unban")],
        [InlineKeyboardButton(text="◀️ Назад",      callback_data="adm:back")]
    ])
    await call.message.edit_text("👤 <b>Пользователи</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "usr:find")
async def usr_find(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("🔍 Введи ID пользователя:")
    await state.set_state(Admin.find_user)

@dp.message(Admin.find_user)
async def do_find(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
                row = await c.fetchone()
            async with db.execute("SELECT seed FROM subs WHERE uid=?", (uid,)) as c:
                subs = [r[0] for r in await c.fetchall()]
        if not row:
            return await msg.answer("❌ Не найден.")
        bn = "🚫 Да" if row[4] else "✅ Нет"
        await msg.answer(
            f"👤 ID: <b>{uid}</b>\n"
            f"Имя: {row[2]}\n"
            f"@{row[1] or '—'}\n"
            f"Зарег: {str(row[3])[:10]}\n"
            f"Бан: {bn}\n"
            f"Seeds: {', '.join(subs) or 'нет'}",
            parse_mode="HTML"
        )
    except ValueError:
        await msg.answer("❌ Введи числовой ID")

@dp.callback_query(F.data == "usr:ban")
async def usr_ban(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("🚫 Введи ID для бана:")
    await state.set_state(Admin.ban_id)

@dp.message(Admin.ban_id)
async def do_ban(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
            await db.commit()
        await msg.answer(f"🚫 {uid} забанен.")
    except ValueError:
        await msg.answer("❌ Введи числовой ID")

@dp.callback_query(F.data == "usr:unban")
async def usr_unban(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("✅ Введи ID для разбана:")
    await state.set_state(Admin.unban_id)

@dp.message(Admin.unban_id)
async def do_unban(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET banned=0 WHERE id=?", (uid,))
            await db.commit()
        await msg.answer(f"✅ {uid} разбанен.")
    except ValueError:
        await msg.answer("❌ Введи числовой ID")

@dp.callback_query(F.data == "adm:stats")
async def adm_stats(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    s = await get_stats()
    top = "\n".join(f"  {i}. {sd}: {cnt}" for i,(sd,cnt) in enumerate(s["top"],1)) or "  —"
    daily = "\n".join(f"  {d}: {'█'*min(n,15)} {n}" for d,n in s["daily"]) or "  —"
    hourly = "\n".join(f"  {h}:00 {'▪'*min(n,10)} {n}" for h,n in s["hourly"]) or "  —"
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"🌱 <b>Топ seeds:</b>\n{top}\n\n"
        f"📅 <b>Новые (7 дней):</b>\n{daily}\n\n"
        f"⏱ <b>Уведомления по часам:</b>\n{hourly}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")
    ]])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await call.answer("Без изменений")

@dp.callback_query(F.data == "adm:back")
async def adm_back(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    try:
        await call.message.edit_text(await panel_text(), parse_mode="HTML", reply_markup=panel_kb())
    except TelegramBadRequest:
        pass

# ─── NOTIFICATION WORKER ─────────────────────────────────────────────────────
_prev_stock: set = set()

async def worker():
    global _prev_stock
    await asyncio.sleep(5)
    log.info("Worker started")
    while True:
        try:
            stock = await fetch_stock()
            if stock:
                seeds     = all_seeds(stock)
                now_in    = {s for s in seeds if in_stock(stock, s)}
                new_items = now_in - _prev_stock
                if new_items:
                    log.info(f"New in stock: {new_items}")
                    async with aiosqlite.connect(DB_PATH) as db:
                        for seed in new_items:
                            async with db.execute(
                                "SELECT u.id FROM subs s JOIN users u ON s.uid=u.id "
                                "WHERE s.seed=? AND u.banned=0", (seed,)
                            ) as c:
                                uids = [r[0] for r in await c.fetchall()]
                            for uid in uids:
                                try:
                                    await bot.send_message(
                                        uid,
                                        f"🚨 <b>{seed}</b> появился в стоке!\n\n"
                                        f"🌱 Спеши купить!\n"
                                        f"📦 Нажми «Сток» чтобы посмотреть всё",
                                        parse_mode="HTML"
                                    )
                                    await db.execute(
                                        "INSERT INTO notif_log (uid,seed,at) VALUES (?,?,?)",
                                        (uid, seed, datetime.now().isoformat())
                                    )
                                    await asyncio.sleep(0.05)
                                except Exception as e:
                                    log.warning(f"notify {uid}: {e}")
                        await db.commit()
                _prev_stock = now_in
        except Exception as e:
            log.error(f"worker error: {e}")
        await asyncio.sleep(POLL_SEC)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    await db_init()
    asyncio.create_task(worker())
    log.info("Bot polling...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
