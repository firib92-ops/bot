import asyncio
import logging
import os
import json
import aiohttp
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8785816519:AAEZBsriAk182crzy7xZbXoJcE-ztCyeiqk")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003820751232"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/growagarden2track")
API_URL = os.getenv("API_URL", "https://grow-a-garden-2-tracker.onrender.com/api/stock")
ADMIN_CODE = os.getenv("ADMIN_CODE", "GrehI07")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            is_banned INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            seed_name TEXT,
            UNIQUE(user_id, seed_name)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stock_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed_name TEXT,
            quantity INTEGER,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            seed_name TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            send_at TEXT,
            target TEXT DEFAULT 'all'
        )
    """)
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect("bot.db")

def add_user(user_id, username, full_name):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
            (user_id, username, full_name)
        )

def is_banned(user_id):
    with get_conn() as conn:
        r = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return r and r[0] == 1

def is_admin(user_id):
    with get_conn() as conn:
        r = conn.execute("SELECT is_admin FROM users WHERE user_id=?", (user_id,)).fetchone()
        return r and r[0] == 1

def get_all_users():
    with get_conn() as conn:
        return conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()

def get_subscriber_users():
    """Get users who are subscribed to at least one seed"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT DISTINCT user_id FROM subscriptions"
        ).fetchall()

def ban_user(user_id):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))

def unban_user(user_id):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

def get_user_subs(user_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT seed_name FROM subscriptions WHERE user_id=?", (user_id,)).fetchall()
        return [r[0] for r in rows]

def add_sub(user_id, seed_name):
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO subscriptions (user_id, seed_name) VALUES (?,?)", (user_id, seed_name))
        except:
            pass

def remove_sub(user_id, seed_name):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE user_id=? AND seed_name=?", (user_id, seed_name))

def get_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        with_subs = conn.execute("SELECT COUNT(DISTINCT user_id) FROM subscriptions").fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        new_today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (today+"%",)
        ).fetchone()[0]
        top_seeds = conn.execute(
            "SELECT seed_name, COUNT(*) as cnt FROM subscriptions GROUP BY seed_name ORDER BY cnt DESC LIMIT 5"
        ).fetchall()
        notifs_today = conn.execute(
            "SELECT COUNT(*) FROM notifications_sent WHERE sent_at LIKE ?", (today+"%",)
        ).fetchone()[0]
        return {
            "total": total, "banned": banned, "with_subs": with_subs,
            "new_today": new_today, "top_seeds": top_seeds, "notifs_today": notifs_today
        }

def get_user_info(user_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

# ─── API ──────────────────────────────────────────────────────────────────────

async def fetch_stock():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.error(f"API error: {e}")
    return None

def format_stock(data):
    if not data:
        return "❌ Не удалось получить данные о стоке."
    
    lines = ["🌱 <b>Grow a Garden 2 — Текущий сток</b>\n"]
    
    if isinstance(data, dict):
        for category, items in data.items():
            lines.append(f"\n📦 <b>{category}</b>")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", item.get("seed", "?"))
                        qty = item.get("quantity", item.get("stock", item.get("amount", "?")))
                        lines.append(f"  🌿 {name} — {qty} шт.")
                    else:
                        lines.append(f"  • {item}")
            elif isinstance(items, dict):
                for k, v in items.items():
                    lines.append(f"  🌿 {k} — {v} шт.")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name", item.get("seed", "?"))
                qty = item.get("quantity", item.get("stock", item.get("amount", "?")))
                lines.append(f"🌿 {name} — {qty} шт.")
    
    lines.append(f"\n🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
    return "\n".join(lines)

def extract_seeds(data):
    """Extract all seed names from stock data"""
    seeds = []
    if not data:
        return seeds
    if isinstance(data, dict):
        for category, items in data.items():
            if "seed" in category.lower() or "seeds" in category.lower():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            name = item.get("name", item.get("seed", ""))
                            if name:
                                seeds.append(name)
                        elif isinstance(item, str):
                            seeds.append(item)
                elif isinstance(items, dict):
                    seeds.extend(items.keys())
            else:
                # Try to get seeds from all categories
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            name = item.get("name", item.get("seed", ""))
                            if name:
                                seeds.append(name)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name", item.get("seed", ""))
                if name:
                    seeds.append(name)
    return list(set(seeds))

def get_in_stock_seeds(data):
    """Return dict of seed_name -> quantity for seeds that are in stock"""
    in_stock = {}
    if not data:
        return in_stock
    if isinstance(data, dict):
        for category, items in data.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", item.get("seed", ""))
                        qty = item.get("quantity", item.get("stock", item.get("amount", 0)))
                        try:
                            qty_int = int(qty)
                        except:
                            qty_int = 1
                        if name and qty_int > 0:
                            in_stock[name] = qty_int
            elif isinstance(items, dict):
                for k, v in items.items():
                    try:
                        qty_int = int(v)
                    except:
                        qty_int = 1
                    if qty_int > 0:
                        in_stock[k] = qty_int
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name", item.get("seed", ""))
                qty = item.get("quantity", item.get("stock", item.get("amount", 0)))
                try:
                    qty_int = int(qty)
                except:
                    qty_int = 1
                if name and qty_int > 0:
                    in_stock[name] = qty_int
    return in_stock

# ─── CHANNEL CHECK ────────────────────────────────────────────────────────────

async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────

def main_menu_kb(is_adm=False):
    buttons = [
        [InlineKeyboardButton(text="📊 Посмотреть сток", callback_data="view_stock")],
        [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notif_settings")],
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
    ]
    if is_adm:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка всем", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📢 Рассылка подписчикам", callback_data="broadcast_subs")],
        [InlineKeyboardButton(text="📨 Тестовое сообщение", callback_data="broadcast_test")],
        [InlineKeyboardButton(text="📅 Запланировать рассылку", callback_data="schedule_broadcast")],
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="find_user")],
        [InlineKeyboardButton(text="🚫 Забанить пользователя", callback_data="ban_user_admin")],
        [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="unban_user_admin")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])

async def seeds_notif_kb(user_id, data):
    user_subs = get_user_subs(user_id)
    seeds = extract_seeds(data) if data else []
    
    if not seeds:
        # Fallback seeds list if API doesn't have clear seed category
        seeds = [
            "Carrot Seed", "Strawberry Seed", "Blueberry Seed", "Tomato Seed",
            "Corn Seed", "Watermelon Seed", "Pumpkin Seed", "Apple Seed",
            "Mango Seed", "Grape Seed", "Bamboo Seed", "Cactus Seed",
            "Rose Seed", "Sunflower Seed", "Tulip Seed"
        ]
    
    buttons = []
    for seed in sorted(seeds):
        icon = "✅" if seed in user_subs else "☑️"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {seed}",
            callback_data=f"toggle_seed:{seed}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── FSM STATES ──────────────────────────────────────────────────────────────

class AdminStates(StatesGroup):
    waiting_admin_code = State()
    waiting_broadcast_msg = State()
    waiting_schedule_msg = State()
    waiting_schedule_time = State()
    waiting_user_id = State()
    waiting_ban_id = State()
    waiting_unban_id = State()

# ─── HANDLERS ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def start_handler(msg: types.Message, state: FSMContext):
    await state.clear()
    user = msg.from_user
    add_user(user.id, user.username, user.full_name)
    
    if is_banned(user.id):
        await msg.answer("🚫 Вы заблокированы.")
        return
    
    text = (
        f"👋 Привет, <b>{user.full_name}</b>!\n\n"
        "🌱 Я бот <b>Grow a Garden 2</b> — слежу за стоком и пришлю уведомление, "
        "когда нужный тебе seed появится!\n\n"
        "📢 Для просмотра стока нужно подписаться на канал."
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=main_menu_kb(is_admin(user.id)))

@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message, state: FSMContext):
    if is_admin(msg.from_user.id):
        await msg.answer("⚙️ Админ панель:", reply_markup=admin_kb())
        return
    await msg.answer("🔐 Введите код администратора:")
    await state.set_state(AdminStates.waiting_admin_code)

@dp.message(AdminStates.waiting_admin_code)
async def check_admin_code(msg: types.Message, state: FSMContext):
    if msg.text.strip() == ADMIN_CODE:
        with get_conn() as conn:
            conn.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (msg.from_user.id,))
        await state.clear()
        await msg.answer("✅ Доступ получен!", reply_markup=admin_kb())
    else:
        await state.clear()
        await msg.answer("❌ Неверный код.")

# ─── CALLBACKS ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "back_main")
async def back_main(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(
        "🌱 Главное меню:",
        reply_markup=main_menu_kb(is_admin(cq.from_user.id))
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(cq: CallbackQuery):
    subscribed = await check_subscription(cq.from_user.id)
    if subscribed:
        await cq.message.edit_text(
            "✅ Отлично! Теперь ты можешь смотреть сток и настраивать уведомления.",
            reply_markup=main_menu_kb(is_admin(cq.from_user.id))
        )
    else:
        await cq.answer("❌ Вы ещё не подписаны на канал!", show_alert=True)

@dp.callback_query(F.data == "view_stock")
async def view_stock_cb(cq: CallbackQuery):
    subscribed = await check_subscription(cq.from_user.id)
    if not subscribed:
        await cq.message.edit_text(
            "📢 Для просмотра стока нужно подписаться на наш канал!\n\n"
            f"👉 {CHANNEL_LINK}",
            reply_markup=sub_required_kb()
        )
        return
    
    await cq.answer("⏳ Загружаю сток...")
    data = await fetch_stock()
    text = format_stock(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="view_stock")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        await cq.message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "notif_settings")
async def notif_settings_cb(cq: CallbackQuery):
    subscribed = await check_subscription(cq.from_user.id)
    if not subscribed:
        await cq.message.edit_text(
            "📢 Для настройки уведомлений нужно подписаться на канал!\n\n"
            f"👉 {CHANNEL_LINK}",
            reply_markup=sub_required_kb()
        )
        return
    
    await cq.answer("⏳ Загружаю список seeds...")
    data = await fetch_stock()
    kb = await seeds_notif_kb(cq.from_user.id, data)
    user_subs = get_user_subs(cq.from_user.id)
    sub_text = f"Активных уведомлений: {len(user_subs)}"
    if user_subs:
        sub_text += f"\n✅ {', '.join(user_subs[:5])}"
        if len(user_subs) > 5:
            sub_text += f" и ещё {len(user_subs)-5}..."
    
    try:
        await cq.message.edit_text(
            f"🔔 <b>Настройки уведомлений о seeds</b>\n\n"
            f"Выбери seeds, о которых хочешь получать уведомления когда они появятся в стоке:\n\n"
            f"{sub_text}",
            parse_mode="HTML",
            reply_markup=kb
        )
    except:
        await cq.message.answer(
            "🔔 Настройки уведомлений:", reply_markup=kb
        )

@dp.callback_query(F.data.startswith("toggle_seed:"))
async def toggle_seed_cb(cq: CallbackQuery):
    seed = cq.data.split(":", 1)[1]
    user_id = cq.from_user.id
    subs = get_user_subs(user_id)
    
    if seed in subs:
        remove_sub(user_id, seed)
        await cq.answer(f"❌ Уведомления для «{seed}» отключены")
    else:
        add_sub(user_id, seed)
        await cq.answer(f"✅ Уведомления для «{seed}» включены")
    
    data = await fetch_stock()
    kb = await seeds_notif_kb(user_id, data)
    user_subs = get_user_subs(user_id)
    sub_text = f"Активных уведомлений: {len(user_subs)}"
    if user_subs:
        sub_text += f"\n✅ {', '.join(user_subs[:5])}"
        if len(user_subs) > 5:
            sub_text += f" и ещё {len(user_subs)-5}..."
    
    try:
        await cq.message.edit_text(
            f"🔔 <b>Настройки уведомлений о seeds</b>\n\n"
            f"Выбери seeds, о которых хочешь получать уведомления:\n\n"
            f"{sub_text}",
            parse_mode="HTML",
            reply_markup=kb
        )
    except:
        pass

# ─── ADMIN CALLBACKS ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "admin_panel")
async def admin_panel_cb(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Нет доступа!", show_alert=True)
        return
    await cq.message.edit_text("⚙️ <b>Админ панель</b>", parse_mode="HTML", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_cb(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Нет доступа!", show_alert=True)
        return
    
    stats = get_stats()
    top_seeds_text = "\n".join(
        [f"  {i+1}. {s[0]} — {s[1]} подписчиков" for i, s in enumerate(stats["top_seeds"])]
    ) or "  Нет данных"
    
    # Activity by hour
    with get_conn() as conn:
        hour_stats = conn.execute("""
            SELECT strftime('%H', sent_at) as hour, COUNT(*) as cnt
            FROM notifications_sent
            WHERE sent_at >= datetime('now', '-1 day')
            GROUP BY hour ORDER BY hour
        """).fetchall()
    
    hour_text = ""
    for h, cnt in hour_stats:
        bar = "█" * min(cnt, 20)
        hour_text += f"  {h}:00 {bar} {cnt}\n"
    if not hour_text:
        hour_text = "  Нет данных за последние 24ч"
    
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"🚫 Забанено: <b>{stats['banned']}</b>\n"
        f"🔔 Используют уведомления: <b>{stats['with_subs']}</b>\n"
        f"🆕 Новых сегодня: <b>{stats['new_today']}</b>\n"
        f"📨 Уведомлений отправлено сегодня: <b>{stats['notifs_today']}</b>\n\n"
        f"🌱 <b>Популярные seeds:</b>\n{top_seeds_text}\n\n"
        f"⏰ <b>Активность по часам (24ч):</b>\n{hour_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ])
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        await cq.message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "broadcast_all")
async def broadcast_all_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast_msg)
    await state.update_data(broadcast_target="all")
    await cq.message.edit_text(
        "📢 Введите сообщение для рассылки ВСЕМ пользователям:\n\n"
        "Поддерживается HTML форматирование.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
    )

@dp.callback_query(F.data == "broadcast_subs")
async def broadcast_subs_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast_msg)
    await state.update_data(broadcast_target="subs")
    await cq.message.edit_text(
        "📢 Введите сообщение для рассылки подписчикам канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
    )

@dp.callback_query(F.data == "broadcast_test")
async def broadcast_test_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast_msg)
    await state.update_data(broadcast_target="test", test_user=cq.from_user.id)
    await cq.message.edit_text(
        "📨 Введите тестовое сообщение (придёт только вам):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
    )

@dp.callback_query(F.data == "schedule_broadcast")
async def schedule_broadcast_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_schedule_msg)
    await cq.message.edit_text(
        "📅 Введите сообщение для запланированной рассылки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
    )

@dp.message(AdminStates.waiting_schedule_msg)
async def schedule_msg_handler(msg: types.Message, state: FSMContext):
    await state.update_data(schedule_msg=msg.text)
    await state.set_state(AdminStates.waiting_schedule_time)
    await msg.answer(
        "🕐 Введите время отправки в формате:\n"
        "<code>YYYY-MM-DD HH:MM</code>\n\n"
        "Например: <code>2025-06-20 18:00</code>",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_schedule_time)
async def schedule_time_handler(msg: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(msg.text.strip(), "%Y-%m-%d %H:%M")
        data = await state.get_data()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO scheduled_broadcasts (message, send_at) VALUES (?,?)",
                (data.get("schedule_msg", ""), dt.isoformat())
            )
        await state.clear()
        await msg.answer(
            f"✅ Рассылка запланирована на {dt.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=admin_kb()
        )
    except ValueError:
        await msg.answer("❌ Неверный формат. Используйте: YYYY-MM-DD HH:MM")

@dp.message(AdminStates.waiting_broadcast_msg)
async def broadcast_msg_handler(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("broadcast_target", "all")
    await state.clear()
    
    text = msg.text
    sent = 0
    failed = 0
    
    if target == "test":
        try:
            await bot.send_message(data.get("test_user"), f"📨 Тест:\n\n{text}", parse_mode="HTML")
            sent = 1
        except:
            failed = 1
    elif target == "all":
        users = get_all_users()
        for (uid,) in users:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1
    elif target == "subs":
        users = get_subscriber_users()
        for (uid,) in users:
            try:
                # Check if they're still in channel
                if await check_subscription(uid):
                    await bot.send_message(uid, text, parse_mode="HTML")
                    sent += 1
                    await asyncio.sleep(0.05)
            except:
                failed += 1
    
    await msg.answer(
        f"✅ Рассылка завершена!\n📤 Отправлено: {sent}\n❌ Ошибок: {failed}",
        reply_markup=admin_kb()
    )

@dp.callback_query(F.data == "find_user")
async def find_user_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_user_id)
    await cq.message.edit_text(
        "🔍 Введите ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
    )

@dp.message(AdminStates.waiting_user_id)
async def find_user_handler(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        user = get_user_info(uid)
        if user:
            subs = get_user_subs(uid)
            await msg.answer(
                f"👤 <b>Пользователь #{uid}</b>\n"
                f"Имя: {user[2]}\n"
                f"Username: @{user[1] or 'нет'}\n"
                f"Заблокирован: {'да' if user[3] else 'нет'}\n"
                f"Администратор: {'да' if user[4] else 'нет'}\n"
                f"Дата: {user[5]}\n"
                f"🌱 Подписки ({len(subs)}): {', '.join(subs) or 'нет'}",
                parse_mode="HTML",
                reply_markup=admin_kb()
            )
        else:
            await msg.answer("❌ Пользователь не найден.", reply_markup=admin_kb())
    except ValueError:
        await msg.answer("❌ Введите числовой ID.", reply_markup=admin_kb())

@dp.callback_query(F.data == "ban_user_admin")
async def ban_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_ban_id)
    await cq.message.edit_text("🚫 Введите ID пользователя для бана:")

@dp.message(AdminStates.waiting_ban_id)
async def ban_handler(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        ban_user(uid)
        await msg.answer(f"✅ Пользователь {uid} заблокирован.", reply_markup=admin_kb())
    except ValueError:
        await msg.answer("❌ Неверный ID.", reply_markup=admin_kb())

@dp.callback_query(F.data == "unban_user_admin")
async def unban_cb(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.waiting_unban_id)
    await cq.message.edit_text("✅ Введите ID пользователя для разбана:")

@dp.message(AdminStates.waiting_unban_id)
async def unban_handler(msg: types.Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(msg.text.strip())
        unban_user(uid)
        await msg.answer(f"✅ Пользователь {uid} разблокирован.", reply_markup=admin_kb())
    except ValueError:
        await msg.answer("❌ Неверный ID.", reply_markup=admin_kb())

# ─── STOCK MONITOR ────────────────────────────────────────────────────────────

last_stock_state = {}

async def check_and_notify():
    global last_stock_state
    
    data = await fetch_stock()
    if not data:
        return
    
    current_in_stock = get_in_stock_seeds(data)
    
    # Find seeds that just appeared in stock
    newly_available = {}
    for seed_name, qty in current_in_stock.items():
        if seed_name not in last_stock_state or last_stock_state.get(seed_name, 0) == 0:
            newly_available[seed_name] = qty
    
    last_stock_state = current_in_stock
    
    if not newly_available:
        return
    
    # Find all users subscribed to these seeds
    with get_conn() as conn:
        for seed_name, qty in newly_available.items():
            rows = conn.execute(
                "SELECT user_id FROM subscriptions WHERE seed_name=?", (seed_name,)
            ).fetchall()
            
            for (uid,) in rows:
                if is_banned(uid):
                    continue
                try:
                    await bot.send_message(
                        uid,
                        f"🌱 <b>{seed_name}</b> появился в стоке!\n"
                        f"📦 Количество: <b>{qty}</b> шт.\n\n"
                        f"🛒 Быстрее, пока не раскупили!",
                        parse_mode="HTML"
                    )
                    conn.execute(
                        "INSERT INTO notifications_sent (user_id, seed_name) VALUES (?,?)",
                        (uid, seed_name)
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Failed to notify {uid}: {e}")

async def check_scheduled_broadcasts():
    with get_conn() as conn:
        now = datetime.now().isoformat()
        rows = conn.execute(
            "SELECT id, message FROM scheduled_broadcasts WHERE send_at <= ?", (now,)
        ).fetchall()
        
        for (bid, message) in rows:
            users = get_all_users()
            for (uid,) in users:
                try:
                    await bot.send_message(uid, message, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                except:
                    pass
            conn.execute("DELETE FROM scheduled_broadcasts WHERE id=?", (bid,))

async def stock_monitor_loop():
    logger.info("Stock monitor started")
    while True:
        try:
            await check_and_notify()
            await check_scheduled_broadcasts()
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    logger.info("Bot starting...")
    asyncio.create_task(stock_monitor_loop())
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
