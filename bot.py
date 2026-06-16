import asyncio
import json
import logging
import os
import time
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN    = "8785816519:AAEZBsriAk182crzy7xZbXoJcE-ztCyeiqk"
CHANNEL_ID   = -1002820751232          # исправлен: -100 + 2820751232
CHANNEL_LINK = "https://t.me/growagarden2track"
API_URL      = "https://grow-a-garden-2-tracker.onrender.com/api/stock"
DATA_FILE    = "users.json"
CHECK_INTERVAL = 60          # секунд между проверками стока
ADMIN_CODE   = "grehI07"     # код для входа в админ-панель

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── онлайн-трекер (последнее время активности) ───────────────────────────────
online: dict[int, float] = {}   # user_id → unix timestamp последнего действия
ONLINE_TTL = 300                 # считаем онлайн 5 минут после последнего действия

def touch_online(uid: int):
    online[uid] = time.time()

def is_online(uid: int) -> bool:
    return time.time() - online.get(uid, 0) < ONLINE_TTL

# ── FSM states ────────────────────────────────────────────────────────────────
class AdminState(StatesGroup):
    waiting_code    = State()
    in_panel        = State()
    writing_user    = State()   # uid хранится в data["target_uid"]

# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(user_id: int) -> dict:
    data = load_data()
    uid  = str(user_id)
    if uid not in data:
        data[uid] = {"subscriptions": [], "notified": {}, "history": [], "name": ""}
        save_data(data)
    return data[uid]

def update_user(user_id: int, user_data: dict):
    data = load_data()
    data[str(user_id)] = user_data
    save_data(data)

def log_message(user_id: int, text: str):
    """Сохраняем историю сообщений пользователя (последние 50)."""
    data = load_data()
    uid  = str(user_id)
    if uid not in data:
        data[uid] = {"subscriptions": [], "notified": {}, "history": [], "name": ""}
    history = data[uid].setdefault("history", [])
    history.append({"ts": int(time.time()), "text": text})
    data[uid]["history"] = history[-50:]
    save_data(data)

# ── API ───────────────────────────────────────────────────────────────────────
async def fetch_stock() -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.error(f"API error: {e}")
    return None

def format_stock(stock: dict) -> str:
    lines = ["🌱 <b>Grow a Garden 2 — текущий сток</b>\n"]
    if not stock:
        return "⚠️ Не удалось получить данные стока."
    for category, items in stock.items():
        if not items:
            continue
        emoji = category_emoji(category)
        lines.append(f"\n{emoji} <b>{category}</b>")
        if isinstance(items, list):
            for item in items:
                lines.append(item_line(item))
        elif isinstance(items, dict):
            for name, info in items.items():
                lines.append(item_line(info, name))
    return "\n".join(lines)

def category_emoji(cat: str) -> str:
    c = cat.lower()
    if "seed"  in c: return "🌰"
    if "gear"  in c: return "⚙️"
    if "egg"   in c: return "🥚"
    if "pet"   in c: return "🐾"
    if "crop"  in c: return "🌾"
    if "fruit" in c: return "🍓"
    if "tool"  in c: return "🔧"
    if "shop"  in c: return "🛒"
    return "📦"

def item_line(item, fallback_name: str = "") -> str:
    if isinstance(item, dict):
        name  = item.get("name") or item.get("item") or fallback_name
        qty   = item.get("quantity") or item.get("stock") or item.get("amount") or ""
        price = item.get("price") or item.get("cost") or ""
        parts = [f"  • {name}"]
        if qty:   parts.append(f"x{qty}")
        if price: parts.append(f"💰{price}")
        return " ".join(parts)
    return f"  • {item}"

def extract_all_items(stock: dict) -> list[str]:
    items = set()
    for category, contents in stock.items():
        if isinstance(contents, list):
            for item in contents:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("item") or ""
                    if name: items.add(name)
                elif isinstance(item, str):
                    items.add(item)
        elif isinstance(contents, dict):
            for name in contents.keys():
                items.add(name)
    return sorted(items)

# ── Subscription ──────────────────────────────────────────────────────────────
async def is_subscribed(user_id: int) -> bool:
    """
    Проверяем подписку через get_chat_member.
    Если бот НЕ администратор канала — всегда возвращает False.
    Убедись, что бот добавлен как администратор в канал!
    """
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        status = member.status
        logger.info(f"User {user_id} channel status: {status}")
        return status not in ("left", "kicked", "banned", "restricted")
    except Exception as e:
        logger.warning(f"Subscription check error for {user_id}: {e}")
        # Если не можем проверить — пропускаем (чтобы не блокировать всех)
        return True

def subscription_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=CHANNEL_LINK)
    builder.button(text="✅ Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Посмотреть сток",       callback_data="view_stock")
    builder.button(text="🔔 Настроить уведомления", callback_data="manage_alerts")
    builder.button(text="📋 Мои подписки",           callback_data="my_subs")
    builder.adjust(1)
    return builder.as_markup()

def alerts_menu(user_id: int, stock_items: list[str]) -> InlineKeyboardMarkup:
    user = get_user(user_id)
    subs = set(user.get("subscriptions", []))
    builder = InlineKeyboardBuilder()
    for item in stock_items:
        tick = "✅" if item in subs else "☑️"
        builder.button(text=f"{tick} {item}", callback_data=f"toggle_{item}")
    builder.button(text="🔙 Назад", callback_data="back_menu")
    builder.adjust(2)
    return builder.as_markup()

# ── Admin keyboards ───────────────────────────────────────────────────────────
def admin_main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👥 Онлайн / Пользователи", callback_data="adm_users")
    b.button(text="📊 Статистика",             callback_data="adm_stats")
    b.button(text="🚪 Выйти",                  callback_data="adm_exit")
    b.adjust(1)
    return b.as_markup()

def admin_users_kb(page: int = 0) -> InlineKeyboardMarkup:
    data  = load_data()
    uids  = list(data.keys())
    PER   = 10
    chunk = uids[page*PER:(page+1)*PER]
    b     = InlineKeyboardBuilder()
    for uid in chunk:
        u    = data[uid]
        name = u.get("name") or uid
        dot  = "🟢" if is_online(int(uid)) else "⚫"
        b.button(text=f"{dot} {name} ({uid})", callback_data=f"adm_user_{uid}")
    # пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_page_{page-1}"))
    if (page+1)*PER < len(uids):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_page_{page+1}"))
    if nav:
        b.row(*nav)
    b.button(text="🔙 Назад", callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()

def admin_user_kb(uid: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Уведомления",     callback_data=f"adm_subs_{uid}")
    b.button(text="📜 История сообщ.",  callback_data=f"adm_hist_{uid}")
    b.button(text="✉️ Написать",        callback_data=f"adm_write_{uid}")
    b.button(text="🔙 Назад",           callback_data="adm_users")
    b.adjust(1)
    return b.as_markup()

# ── User handlers ─────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    touch_online(message.from_user.id)
    user = get_user(message.from_user.id)
    name = message.from_user.full_name or ""
    if user.get("name") != name:
        user["name"] = name
        update_user(message.from_user.id, user)
    log_message(message.from_user.id, "/start")

    text = (
        "👋 Привет! Я <b>Grow a Garden 2 Stock Bot</b>.\n\n"
        "Я слежу за стоком предметов в игре и пришлю уведомление, "
        "когда нужный тебе предмет появится в продаже.\n\n"
        "Для использования бота нужно подписаться на канал 👇"
    )
    if await is_subscribed(message.from_user.id):
        await message.answer(
            text + "\n\n✅ Ты уже подписан! Выбери действие:",
            reply_markup=main_menu(), parse_mode="HTML"
        )
    else:
        await message.answer(text, reply_markup=subscription_keyboard(), parse_mode="HTML")

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    touch_online(message.from_user.id)
    await state.set_state(AdminState.waiting_code)
    await message.answer("🔐 Введи код доступа к админ-панели:")

@dp.message(AdminState.waiting_code)
async def admin_code_input(message: Message, state: FSMContext):
    if message.text == ADMIN_CODE:
        await state.set_state(AdminState.in_panel)
        await message.answer("✅ Добро пожаловать в админ-панель!", reply_markup=admin_main_kb())
    else:
        await state.clear()
        await message.answer("❌ Неверный код.")

# Все текстовые сообщения пользователей — логируем
@dp.message(F.text)
async def any_text(message: Message, state: FSMContext):
    touch_online(message.from_user.id)
    current = await state.get_state()

    # Если ждём сообщение от админа пользователю
    if current == AdminState.writing_user.state:
        data_state = await state.get_data()
        target = data_state.get("target_uid")
        if target:
            try:
                await bot.send_message(
                    int(target),
                    f"📩 <b>Сообщение от администратора:</b>\n\n{message.text}",
                    parse_mode="HTML"
                )
                await message.answer("✅ Сообщение отправлено!", reply_markup=admin_main_kb())
            except Exception as e:
                await message.answer(f"❌ Ошибка: {e}", reply_markup=admin_main_kb())
        await state.set_state(AdminState.in_panel)
        return

    # Обычный пользователь — логируем
    if current not in (AdminState.waiting_code.state, AdminState.in_panel.state):
        log_message(message.from_user.id, message.text or "")

# ── Callback: проверка подписки ───────────────────────────────────────────────
@dp.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    touch_online(call.from_user.id)
    if await is_subscribed(call.from_user.id):
        await call.message.edit_text(
            "✅ Подписка подтверждена! Добро пожаловать 🎉\n\nВыбери действие:",
            reply_markup=main_menu()
        )
    else:
        await call.answer(
            "❌ Подписка не найдена.\n\nУбедись, что ты подписался на канал и попробуй снова.",
            show_alert=True
        )

async def require_sub(call: CallbackQuery) -> bool:
    if not await is_subscribed(call.from_user.id):
        await call.message.edit_text(
            "⚠️ Для использования бота нужно подписаться на канал:",
            reply_markup=subscription_keyboard()
        )
        return False
    return True

# ── Callback: сток ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "view_stock")
async def view_stock(call: CallbackQuery):
    touch_online(call.from_user.id)
    if not await require_sub(call): return
    await call.answer()
    msg   = await call.message.edit_text("⏳ Загружаю сток...")
    stock = await fetch_stock()
    text  = format_stock(stock) if stock else "⚠️ Не удалось получить данные. Попробуй позже."
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="view_stock")
    b.button(text="🔙 Назад",    callback_data="back_menu")
    b.adjust(2)
    await msg.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "manage_alerts")
async def manage_alerts(call: CallbackQuery):
    touch_online(call.from_user.id)
    if not await require_sub(call): return
    await call.answer()
    stock = await fetch_stock()
    if not stock:
        await call.message.edit_text("⚠️ Не удалось загрузить список предметов. Попробуй позже.")
        return
    items = extract_all_items(stock)
    if not items:
        await call.message.edit_text("⚠️ Список предметов пуст — сток сейчас пустой.")
        return
    await call.message.edit_text(
        "🔔 <b>Настройка уведомлений</b>\n\nВыбери предметы — получишь уведомление, когда они появятся в стоке:",
        reply_markup=alerts_menu(call.from_user.id, items),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_alert(call: CallbackQuery):
    touch_online(call.from_user.id)
    if not await require_sub(call): return
    item_name = call.data[len("toggle_"):]
    user = get_user(call.from_user.id)
    subs = set(user.get("subscriptions", []))
    if item_name in subs:
        subs.discard(item_name)
        await call.answer(f"🔕 Уведомления для «{item_name}» отключены")
    else:
        subs.add(item_name)
        await call.answer(f"🔔 Уведомления для «{item_name}» включены")
    user["subscriptions"] = list(subs)
    update_user(call.from_user.id, user)
    stock = await fetch_stock()
    if stock:
        await call.message.edit_reply_markup(reply_markup=alerts_menu(call.from_user.id, extract_all_items(stock)))

@dp.callback_query(F.data == "my_subs")
async def my_subs(call: CallbackQuery):
    touch_online(call.from_user.id)
    if not await require_sub(call): return
    await call.answer()
    user = get_user(call.from_user.id)
    subs = user.get("subscriptions", [])
    text = (
        "🔔 <b>Твои подписки на уведомления:</b>\n\n" + "\n".join(f"  • {s}" for s in sorted(subs))
        if subs else
        "У тебя нет активных подписок.\nНажми «Настроить уведомления», чтобы выбрать предметы."
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Настроить уведомления", callback_data="manage_alerts")
    b.button(text="🔙 Назад", callback_data="back_menu")
    b.adjust(1)
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery):
    touch_online(call.from_user.id)
    await call.answer()
    await call.message.edit_text(
        "🌱 <b>Grow a Garden 2 Stock Bot</b>\n\nВыбери действие:",
        reply_markup=main_menu(), parse_mode="HTML"
    )

# ── Admin callbacks ───────────────────────────────────────────────────────────
async def admin_guard(call: CallbackQuery, state: FSMContext) -> bool:
    current = await state.get_state()
    if current not in (AdminState.in_panel.state, AdminState.writing_user.state):
        await call.answer("❌ Нет доступа. Введи /admin", show_alert=True)
        return False
    return True

@dp.callback_query(F.data == "adm_back")
async def adm_back(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    await call.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_exit")
async def adm_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.edit_text("🚪 Вышел из админ-панели.")

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    data     = load_data()
    total    = len(data)
    now_on   = sum(1 for uid in data if is_online(int(uid)))
    with_sub = sum(1 for u in data.values() if u.get("subscriptions"))
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🟢 Сейчас онлайн: <b>{now_on}</b>\n"
        f"🔔 Настроили уведомления: <b>{with_sub}</b>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data="adm_back")
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_users")
async def adm_users(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    data   = load_data()
    now_on = sum(1 for uid in data if is_online(int(uid)))
    await call.message.edit_text(
        f"👥 <b>Пользователи</b>\n🟢 Онлайн: {now_on} / {len(data)}\n\nВыбери пользователя:",
        reply_markup=admin_users_kb(0), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_page_"))
async def adm_page(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    page = int(call.data.split("_")[-1])
    data = load_data()
    now_on = sum(1 for uid in data if is_online(int(uid)))
    await call.message.edit_text(
        f"👥 <b>Пользователи</b>\n🟢 Онлайн: {now_on} / {len(data)}\n\nВыбери пользователя:",
        reply_markup=admin_users_kb(page), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_user_"))
async def adm_user(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    uid  = call.data[len("adm_user_"):]
    data = load_data()
    u    = data.get(uid, {})
    name = u.get("name") or "—"
    dot  = "🟢 онлайн" if is_online(int(uid)) else "⚫ офлайн"
    subs = len(u.get("subscriptions", []))
    hist = len(u.get("history", []))
    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Имя: {name}\n"
        f"Статус: {dot}\n"
        f"Уведомлений: {subs}\n"
        f"Сообщений в истории: {hist}"
    )
    await call.message.edit_text(text, reply_markup=admin_user_kb(uid), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_subs_"))
async def adm_subs(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    uid  = call.data[len("adm_subs_"):]
    data = load_data()
    subs = data.get(uid, {}).get("subscriptions", [])
    text = (
        f"🔔 <b>Уведомления пользователя {uid}:</b>\n\n" +
        ("\n".join(f"  • {s}" for s in sorted(subs)) if subs else "— нет активных подписок —")
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data=f"adm_user_{uid}")
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_hist_"))
async def adm_hist(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    uid  = call.data[len("adm_hist_"):]
    data = load_data()
    hist = data.get(uid, {}).get("history", [])
    if hist:
        lines = []
        for h in hist[-20:]:
            ts   = h.get("ts", 0)
            from datetime import datetime
            dt   = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
            lines.append(f"[{dt}] {h.get('text','')}")
        body = "\n".join(lines)
    else:
        body = "— история пуста —"
    text = f"📜 <b>История сообщений {uid}:</b>\n\n<code>{body}</code>"
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data=f"adm_user_{uid}")
    await call.message.edit_text(text[:4000], reply_markup=b.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_write_"))
async def adm_write(call: CallbackQuery, state: FSMContext):
    if not await admin_guard(call, state): return
    await call.answer()
    uid = call.data[len("adm_write_"):]
    await state.set_state(AdminState.writing_user)
    await state.update_data(target_uid=uid)
    await call.message.edit_text(
        f"✉️ Введи сообщение для пользователя <code>{uid}</code>\n\n(оно придёт от лица бота)",
        parse_mode="HTML"
    )

# ── Background stock watcher ──────────────────────────────────────────────────
async def stock_watcher():
    prev_items: set[str] = set()
    logger.info("Stock watcher started")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            stock = await fetch_stock()
            if not stock:
                continue
            current_items = set(extract_all_items(stock))
            new_items = current_items - prev_items

            if new_items and prev_items:
                data = load_data()
                for uid, user_data in data.items():
                    subs     = set(user_data.get("subscriptions", []))
                    notified = user_data.get("notified", {})
                    matched  = subs & new_items
                    to_notify = [m for m in matched if not notified.get(m)]
                    if to_notify:
                        try:
                            text = (
                                "🔔 <b>Появились предметы в стоке!</b>\n\n"
                                + "\n".join(f"✅ {item}" for item in sorted(to_notify))
                                + "\n\n<i>Открой бота, чтобы посмотреть подробности.</i>"
                            )
                            await bot.send_message(int(uid), text, parse_mode="HTML", reply_markup=main_menu())
                            for item in to_notify:
                                notified[item] = True
                        except Exception as e:
                            logger.warning(f"Could not notify {uid}: {e}")

                # Сбрасываем флаг уведомления для предметов которых уже нет
                for uid, user_data in data.items():
                    notified = user_data.get("notified", {})
                    for item in list(notified.keys()):
                        if item not in current_items:
                            del notified[item]
                save_data(data)

            prev_items = current_items
        except Exception as e:
            logger.error(f"Watcher error: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(stock_watcher())
    logger.info("Bot started. Make sure the bot is an ADMIN in the channel!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
