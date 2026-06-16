import asyncio
import logging
import json
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.exceptions import TelegramBadRequest
import aiohttp
import aiosqlite

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = "8785816519:AAEZBsriAk182crzy7xZbXoJcE-ztCyeiqk"
CHANNEL_ID = -1003820751232
CHANNEL_LINK = "https://t.me/growagarden2track"
STOCK_API = "https://grow-a-garden-2-tracker.onrender.com/api/stock"
ADMIN_CODE = "GrehI07"
DB_PATH = "garden_bot.db"
POLL_INTERVAL = 60  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ── States ───────────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_admin_code = State()
    broadcast_text = State()
    broadcast_scheduled = State()
    search_user = State()
    ban_user = State()
    unban_user = State()

# ── Database ─────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT,
                is_banned INTEGER DEFAULT 0,
                last_active TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER,
                seed_name TEXT,
                PRIMARY KEY (user_id, seed_name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                seed_name TEXT,
                sent_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_cache (
                key TEXT PRIMARY KEY,
                data TEXT,
                updated_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                user_id INTEGER PRIMARY KEY,
                authed_at TEXT
            )
        """)
        await db.commit()

async def upsert_user(user: types.User):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, joined_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_active=excluded.last_active
        """, (user.id, user.username, user.full_name, now, now))
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])

async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admin_sessions WHERE user_id=?", (user_id,)) as cur:
            return bool(await cur.fetchone())

async def set_admin(user_id: int):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO admin_sessions VALUES (?,?)", (user_id, now))
        await db.commit()

async def get_user_subs(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT seed_name FROM subscriptions WHERE user_id=?", (user_id,)) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def toggle_sub(user_id: int, seed_name: str) -> bool:
    """Returns True if subscribed, False if unsubscribed"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND seed_name=?", (user_id, seed_name)) as cur:
            exists = await cur.fetchone()
        if exists:
            await db.execute("DELETE FROM subscriptions WHERE user_id=? AND seed_name=?", (user_id, seed_name))
            await db.commit()
            return False
        else:
            await db.execute("INSERT INTO subscriptions VALUES (?,?)", (user_id, seed_name))
            await db.commit()
            return True

async def get_all_users() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as cur:
            return [r[0] for r in await cur.fetchall()]

async def get_channel_subs_users() -> list:
    """Users who are subscribed to channel (we track via check)"""
    return await get_all_users()

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned=0") as c:
            active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM subscriptions") as c:
            notif_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM subscriptions") as c:
            total_subs = (await c.fetchone())[0]
        today = datetime.now().date().isoformat()
        async with db.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today}%",)) as c:
            new_today = (await c.fetchone())[0]
        async with db.execute("""
            SELECT seed_name, COUNT(*) as cnt FROM subscriptions
            GROUP BY seed_name ORDER BY cnt DESC LIMIT 10
        """) as c:
            top_seeds = await c.fetchall()
        async with db.execute("""
            SELECT strftime('%H', sent_at) as hr, COUNT(*) FROM notification_log
            GROUP BY hr ORDER BY hr
        """) as c:
            hourly = await c.fetchall()
        async with db.execute("""
            SELECT date(joined_at) as d, COUNT(*) FROM users
            WHERE joined_at >= date('now', '-7 days')
            GROUP BY d ORDER BY d
        """) as c:
            daily_new = await c.fetchall()
    return {
        "total": total, "active": active, "banned": total - active,
        "notif_users": notif_users, "total_subs": total_subs,
        "new_today": new_today, "top_seeds": top_seeds,
        "hourly": hourly, "daily_new": daily_new
    }

# ── Stock API ────────────────────────────────────────────────────────────────
_last_stock = {}
_last_stock_time = 0

async def fetch_stock() -> dict:
    global _last_stock, _last_stock_time
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STOCK_API, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _last_stock = data
                    _last_stock_time = time.time()
                    # cache to db
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO stock_cache VALUES ('main', ?, ?)",
                            (json.dumps(data), datetime.now().isoformat())
                        )
                        await db.commit()
                    return data
    except Exception as e:
        log.warning(f"Stock fetch error: {e}")
    return _last_stock

def format_stock_message(stock: dict) -> str:
    if not stock:
        return "⚠️ Не удалось получить данные о стоке."

    lines = ["🌱 <b>Grow a Garden 2 — Текущий сток</b>\n"]
    lines.append(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}\n")

    # Handle both flat and nested structures
    if isinstance(stock, dict):
        for category, items in stock.items():
            if isinstance(items, dict):
                lines.append(f"\n<b>📦 {category}</b>")
                for name, info in items.items():
                    if isinstance(info, dict):
                        qty = info.get("quantity", info.get("stock", info.get("amount", "?")))
                        price = info.get("price", info.get("cost", ""))
                        price_str = f" | 💰 {price}" if price else ""
                        lines.append(f"  • <b>{name}</b>: {qty}{price_str}")
                    else:
                        lines.append(f"  • <b>{name}</b>: {info}")
            elif isinstance(items, list):
                lines.append(f"\n<b>📦 {category}</b>")
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", item.get("id", "Unknown"))
                        qty = item.get("quantity", item.get("stock", item.get("amount", "?")))
                        price = item.get("price", item.get("cost", ""))
                        price_str = f" | 💰 {price}" if price else ""
                        lines.append(f"  • <b>{name}</b>: {qty}{price_str}")
                    else:
                        lines.append(f"  • {item}")
            else:
                lines.append(f"\n• <b>{category}</b>: {items}")

    return "\n".join(lines)

def extract_seeds(stock: dict) -> list:
    """Extract all seed names from stock"""
    seeds = []
    if not stock:
        return seeds
    for category, items in stock.items():
        cat_lower = category.lower()
        if "seed" in cat_lower or "plant" in cat_lower or "crop" in cat_lower:
            if isinstance(items, dict):
                seeds.extend(items.keys())
            elif isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", item.get("id", ""))
                        if name:
                            seeds.append(name)
        elif isinstance(items, dict):
            for name in items.keys():
                if "seed" in name.lower() or "plant" in name.lower():
                    seeds.append(name)
        elif isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name", item.get("id", ""))
                    if name and ("seed" in name.lower() or "plant" in name.lower()):
                        seeds.append(name)
    # fallback - return all items
    if not seeds:
        for category, items in stock.items():
            if isinstance(items, dict):
                seeds.extend(items.keys())
            elif isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", item.get("id", ""))
                        if name:
                            seeds.append(name)
    return list(set(seeds))

def is_in_stock(stock: dict, seed_name: str) -> bool:
    for category, items in stock.items():
        if isinstance(items, dict):
            if seed_name in items:
                info = items[seed_name]
                if isinstance(info, dict):
                    qty = info.get("quantity", info.get("stock", info.get("amount", 0)))
                    try:
                        return int(qty) > 0
                    except:
                        return bool(qty)
                return bool(info)
        elif isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name", item.get("id", ""))
                    if name == seed_name:
                        qty = item.get("quantity", item.get("stock", item.get("amount", 0)))
                        try:
                            return int(qty) > 0
                        except:
                            return bool(qty)
    return False

# ── Channel check ─────────────────────────────────────────────────────────────
async def check_channel_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

def sub_required_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK),
        InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")
    ]])

# ── Main menu ─────────────────────────────────────────────────────────────────
def main_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📦 Смотреть сток"), KeyboardButton(text="🔔 Настройки уведомлений")],
        [KeyboardButton(text="ℹ️ О боте")]
    ], resize_keyboard=True)

# ── Handlers ──────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    await upsert_user(msg.from_user)
    if await is_banned(msg.from_user.id):
        await msg.answer("🚫 Вы заблокированы.")
        return

    text = (
        "🌱 <b>Добро пожаловать в Grow a Garden 2 Stock Bot!</b>\n\n"
        "Здесь ты можешь:\n"
        "📦 Смотреть текущий сток всех предметов\n"
        "🔔 Настраивать уведомления на конкретные seeds\n\n"
        "Для доступа к стоку нужно подписаться на наш канал 👇"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=sub_required_keyboard())

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery):
    await upsert_user(call.from_user)
    if await is_banned(call.from_user.id):
        await call.answer("🚫 Вы заблокированы.", show_alert=True)
        return
    if await check_channel_sub(call.from_user.id):
        await call.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n\nТеперь ты можешь пользоваться всеми функциями бота 🎉",
            parse_mode="HTML"
        )
        await call.message.answer("Выбери действие:", reply_markup=main_menu_kb())
    else:
        await call.answer("❌ Вы ещё не подписались на канал!", show_alert=True)

async def require_sub(msg: types.Message) -> bool:
    if not await check_channel_sub(msg.from_user.id):
        await msg.answer(
            "❗ Для использования бота нужно подписаться на канал:",
            reply_markup=sub_required_keyboard()
        )
        return False
    return True

@dp.message(F.text == "📦 Смотреть сток")
async def show_stock(msg: types.Message):
    await upsert_user(msg.from_user)
    if await is_banned(msg.from_user.id): return
    if not await require_sub(msg): return

    wait = await msg.answer("⏳ Загружаю сток...")
    stock = await fetch_stock()
    text = format_stock_message(stock)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_stock")
    ]])
    try:
        await wait.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await wait.delete()
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "refresh_stock")
async def refresh_stock_cb(call: types.CallbackQuery):
    if await is_banned(call.from_user.id): return
    if not await check_channel_sub(call.from_user.id):
        await call.answer("❗ Подпишитесь на канал!", show_alert=True)
        return
    stock = await fetch_stock()
    text = format_stock_message(stock)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_stock")
    ]])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer("✅ Обновлено!")
    except TelegramBadRequest:
        await call.answer("Уже актуально!")

# ── Notification settings ────────────────────────────────────────────────────
@dp.message(F.text == "🔔 Настройки уведомлений")
async def notif_settings(msg: types.Message):
    await upsert_user(msg.from_user)
    if await is_banned(msg.from_user.id): return
    if not await require_sub(msg): return

    stock = await fetch_stock()
    seeds = extract_seeds(stock)
    user_subs = await get_user_subs(msg.from_user.id)

    if not seeds:
        await msg.answer("⚠️ Не удалось загрузить список seeds. Попробуйте позже.")
        return

    text = "🔔 <b>Настройки уведомлений</b>\n\nВыбери seeds, о появлении которых хочешь получать уведомления:\n✅ — уведомления включены  |  ⬜ — выключены"
    kb = build_seeds_keyboard(seeds, user_subs)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

def build_seeds_keyboard(seeds: list, user_subs: list) -> InlineKeyboardMarkup:
    buttons = []
    for seed in sorted(seeds):
        icon = "✅" if seed in user_subs else "⬜"
        buttons.append([InlineKeyboardButton(text=f"{icon} {seed}", callback_data=f"toggle_seed:{seed}")])
    buttons.append([InlineKeyboardButton(text="❌ Отключить все", callback_data="unsub_all")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("toggle_seed:"))
async def toggle_seed_cb(call: types.CallbackQuery):
    seed = call.data.split(":", 1)[1]
    if await is_banned(call.from_user.id): return

    subscribed = await toggle_sub(call.from_user.id, seed)
    action = "включены ✅" if subscribed else "отключены ⬜"
    await call.answer(f"Уведомления для «{seed}» {action}")

    # rebuild keyboard
    stock = await fetch_stock()
    seeds = extract_seeds(stock)
    user_subs = await get_user_subs(call.from_user.id)
    kb = build_seeds_keyboard(seeds, user_subs)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data == "unsub_all")
async def unsub_all_cb(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id=?", (call.from_user.id,))
        await db.commit()
    await call.answer("❌ Все уведомления отключены")
    stock = await fetch_stock()
    seeds = extract_seeds(stock)
    kb = build_seeds_keyboard(seeds, [])
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.message(F.text == "ℹ️ О боте")
async def about_bot(msg: types.Message):
    text = (
        "🌱 <b>Grow a Garden 2 Stock Bot</b>\n\n"
        "Бот отслеживает текущий сток игры в реальном времени и отправляет уведомления, "
        "когда выбранные тобой seeds появляются в продаже.\n\n"
        "🔄 Данные обновляются каждую минуту\n"
        "📢 Канал: @growagarden2track\n\n"
        "<i>Нажми /start чтобы начать</i>"
    )
    await msg.answer(text, parse_mode="HTML")

# ── Admin ─────────────────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message, state: FSMContext):
    if await is_admin(msg.from_user.id):
        await show_admin_panel(msg)
        return
    await msg.answer("🔐 Введите код администратора:")
    await state.set_state(AdminStates.waiting_admin_code)

@dp.message(AdminStates.waiting_admin_code)
async def admin_code_input(msg: types.Message, state: FSMContext):
    if msg.text.strip() == ADMIN_CODE:
        await set_admin(msg.from_user.id)
        await state.clear()
        await msg.answer("✅ Доступ к панели администратора открыт!")
        await show_admin_panel(msg)
    else:
        await state.clear()
        await msg.answer("❌ Неверный код.")

async def show_admin_panel(msg: types.Message):
    stats = await get_stats()
    text = (
        f"🛠 <b>Панель администратора</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>\n"
        f"🚫 Забанено: <b>{stats['banned']}</b>\n"
        f"🔔 Используют уведомления: <b>{stats['notif_users']}</b>\n"
        f"📌 Всего подписок на seeds: <b>{stats['total_subs']}</b>\n"
        f"🆕 Новых сегодня: <b>{stats['new_today']}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast_menu")],
        [InlineKeyboardButton(text="👤 Пользователи", callback_data="admin:users_menu")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats_menu")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")]
    ])
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "admin:refresh")
async def admin_refresh(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    stats = await get_stats()
    text = (
        f"🛠 <b>Панель администратора</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>\n"
        f"🚫 Забанено: <b>{stats['banned']}</b>\n"
        f"🔔 Используют уведомления: <b>{stats['notif_users']}</b>\n"
        f"📌 Всего подписок на seeds: <b>{stats['total_subs']}</b>\n"
        f"🆕 Новых сегодня: <b>{stats['new_today']}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast_menu")],
        [InlineKeyboardButton(text="👤 Пользователи", callback_data="admin:users_menu")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats_menu")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")]
    ])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer("✅")

# Broadcast menu
@dp.callback_query(F.data == "admin:broadcast_menu")
async def broadcast_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Отправить всем", callback_data="admin:broadcast:all")],
        [InlineKeyboardButton(text="📢 Отправить подписчикам канала", callback_data="admin:broadcast:channel")],
        [InlineKeyboardButton(text="🧪 Тестовое сообщение", callback_data="admin:broadcast:test")],
        [InlineKeyboardButton(text="⏰ Запланировать рассылку", callback_data="admin:broadcast:schedule")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]
    ])
    await call.message.edit_text("📢 <b>Рассылка</b>\n\nВыберите тип рассылки:", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin:broadcast:"))
async def broadcast_type(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    btype = call.data.split(":")[-1]
    await state.update_data(btype=btype)

    if btype == "schedule":
        await call.message.edit_text(
            "⏰ Введите текст рассылки и время в формате:\n<code>ДД.ММ ЧЧЧЧ:ММ\nТекст сообщения</code>",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.broadcast_scheduled)
    else:
        await call.message.edit_text("✏️ Введите текст сообщения для рассылки:")
        await state.set_state(AdminStates.broadcast_text)

@dp.message(AdminStates.broadcast_text)
async def do_broadcast(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    btype = data.get("btype", "all")
    await state.clear()

    users = await get_all_users()
    sent = 0
    failed = 0
    label = {"all": "всем", "channel": "подписчикам канала", "test": "тест"}.get(btype, "всем")

    status_msg = await msg.answer(f"📤 Начинаю рассылку ({label})...")

    if btype == "test":
        try:
            await bot.send_message(msg.from_user.id, f"🧪 <b>Тест рассылки:</b>\n\n{msg.text}", parse_mode="HTML")
            sent = 1
        except: failed = 1
    else:
        for uid in users:
            if btype == "channel" and not await check_channel_sub(uid):
                continue
            try:
                await bot.send_message(uid, msg.text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1

    await status_msg.edit_text(f"✅ Рассылка завершена!\n✉️ Отправлено: {sent}\n❌ Ошибок: {failed}")

@dp.message(AdminStates.broadcast_scheduled)
async def schedule_broadcast(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        lines = msg.text.strip().split("\n", 1)
        dt_str = lines[0].strip()
        text = lines[1].strip() if len(lines) > 1 else "Тест"
        dt = datetime.strptime(f"{datetime.now().year} {dt_str}", "%Y %d.%m %H:%M")
        delay = (dt - datetime.now()).total_seconds()
        if delay < 0:
            await msg.answer("❌ Время уже прошло!")
            return
        await msg.answer(f"✅ Рассылка запланирована на {dt.strftime('%d.%m %H:%M')}")
        asyncio.create_task(delayed_broadcast(delay, text, await get_all_users()))
    except Exception as e:
        await msg.answer(f"❌ Ошибка формата: {e}")

async def delayed_broadcast(delay: float, text: str, users: list):
    await asyncio.sleep(delay)
    for uid in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except: pass

# Users menu
@dp.callback_query(F.data == "admin:users_menu")
async def users_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск по ID", callback_data="admin:search_user")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="admin:ban_user")],
        [InlineKeyboardButton(text="✅ Разбанить", callback_data="admin:unban_user")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]
    ])
    await call.message.edit_text("👤 <b>Управление пользователями</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "admin:search_user")
async def search_user(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("🔍 Введите ID пользователя:")
    await state.set_state(AdminStates.search_user)

@dp.message(AdminStates.search_user)
async def do_search_user(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
            async with db.execute("SELECT seed_name FROM subscriptions WHERE user_id=?", (uid,)) as cur:
                subs = [r[0] for r in await cur.fetchall()]
        if row:
            banned = "🚫 Да" if row[4] else "✅ Нет"
            text = (
                f"👤 <b>Пользователь {uid}</b>\n"
                f"Имя: {row[2]}\n"
                f"Username: @{row[1] or '—'}\n"
                f"Зарегистрирован: {row[3][:10]}\n"
                f"Заблокирован: {banned}\n"
                f"Подписки: {', '.join(subs) if subs else 'нет'}"
            )
        else:
            text = f"❌ Пользователь {uid} не найден."
        await msg.answer(text, parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Введите числовой ID")

@dp.callback_query(F.data == "admin:ban_user")
async def ban_user_prompt(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("🚫 Введите ID пользователя для бана:")
    await state.set_state(AdminStates.ban_user)

@dp.message(AdminStates.ban_user)
async def do_ban(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
            await db.commit()
        await msg.answer(f"🚫 Пользователь {uid} заблокирован.")
    except ValueError:
        await msg.answer("❌ Введите числовой ID")

@dp.callback_query(F.data == "admin:unban_user")
async def unban_user_prompt(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.edit_text("✅ Введите ID пользователя для разбана:")
    await state.set_state(AdminStates.unban_user)

@dp.message(AdminStates.unban_user)
async def do_unban(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
            await db.commit()
        await msg.answer(f"✅ Пользователь {uid} разблокирован.")
    except ValueError:
        await msg.answer("❌ Введите числовой ID")

# Stats menu
@dp.callback_query(F.data == "admin:stats_menu")
async def stats_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    stats = await get_stats()

    # Top seeds
    top_seeds_text = ""
    for i, (seed, cnt) in enumerate(stats["top_seeds"], 1):
        top_seeds_text += f"  {i}. {seed}: {cnt} подписок\n"

    # Daily new users (last 7 days)
    daily_text = ""
    for date, cnt in stats["daily_new"]:
        bar = "█" * min(cnt, 20)
        daily_text += f"  {date}: {bar} {cnt}\n"

    # Hourly activity
    hourly_text = ""
    for hr, cnt in stats["hourly"]:
        bar = "▪" * min(cnt // max(1, max(c for _, c in stats["hourly"]) // 10 + 1), 10)
        hourly_text += f"  {hr}:00 {bar} {cnt}\n"

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"🌱 <b>Топ-10 популярных seeds:</b>\n{top_seeds_text or '  —'}\n"
        f"📅 <b>Новые пользователи (7 дней):</b>\n{daily_text or '  —'}\n"
        f"⏱ <b>Активность по часам (уведомления):</b>\n{hourly_text or '  —'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")
    ]])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await call.answer("Без изменений")

@dp.callback_query(F.data == "admin:back")
async def admin_back(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    stats = await get_stats()
    text = (
        f"🛠 <b>Панель администратора</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>\n"
        f"🚫 Забанено: <b>{stats['banned']}</b>\n"
        f"🔔 Используют уведомления: <b>{stats['notif_users']}</b>\n"
        f"📌 Всего подписок на seeds: <b>{stats['total_subs']}</b>\n"
        f"🆕 Новых сегодня: <b>{stats['new_today']}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast_menu")],
        [InlineKeyboardButton(text="👤 Пользователи", callback_data="admin:users_menu")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats_menu")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")]
    ])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# ── Stock notification worker ─────────────────────────────────────────────────
_prev_in_stock = set()  # set of seed names that were in stock last check

async def notification_worker():
    global _prev_in_stock
    await asyncio.sleep(5)  # wait for bot to start
    log.info("Notification worker started")
    while True:
        try:
            stock = await fetch_stock()
            if not stock:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            seeds = extract_seeds(stock)
            now_in_stock = {s for s in seeds if is_in_stock(stock, s)}

            # seeds that just appeared in stock
            newly_available = now_in_stock - _prev_in_stock

            if newly_available:
                log.info(f"New in stock: {newly_available}")
                async with aiosqlite.connect(DB_PATH) as db:
                    for seed in newly_available:
                        # find all users subscribed to this seed
                        async with db.execute(
                            "SELECT u.user_id FROM subscriptions s JOIN users u ON s.user_id=u.user_id "
                            "WHERE s.seed_name=? AND u.is_banned=0",
                            (seed,)
                        ) as cur:
                            subscribers = [r[0] for r in await cur.fetchall()]

                        for uid in subscribers:
                            try:
                                await bot.send_message(
                                    uid,
                                    f"🚨 <b>{seed}</b> появился в стоке!\n\n"
                                    f"🌱 Спеши купить, пока не закончился!\n"
                                    f"📦 /stock — смотреть весь сток",
                                    parse_mode="HTML"
                                )
                                await db.execute(
                                    "INSERT INTO notification_log (user_id, seed_name, sent_at) VALUES (?,?,?)",
                                    (uid, seed, datetime.now().isoformat())
                                )
                                await asyncio.sleep(0.05)
                            except Exception as e:
                                log.warning(f"Failed to notify {uid}: {e}")
                    await db.commit()

            _prev_in_stock = now_in_stock
        except Exception as e:
            log.error(f"Worker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

@dp.message(Command("stock"))
async def stock_cmd(msg: types.Message):
    await upsert_user(msg.from_user)
    if await is_banned(msg.from_user.id): return
    if not await require_sub(msg): return
    wait = await msg.answer("⏳ Загружаю сток...")
    stock = await fetch_stock()
    text = format_stock_message(stock)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_stock")
    ]])
    try:
        await wait.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await wait.delete()
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(notification_worker())
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
