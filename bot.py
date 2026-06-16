import asyncio
import json
import logging
import os
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = "8785816519:AAEZBsriAk182crzy7xZbXoJcE-ztCyeiqk"
CHANNEL_ID = -1003820751232
CHANNEL_LINK = "https://t.me/growagarden2track"
API_URL = "https://grow-a-garden-2-tracker.onrender.com/api/stock"
DATA_FILE = "users.json"
CHECK_INTERVAL = 60  # seconds between stock checks

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ─── Data helpers ────────────────────────────────────────────────────────────

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
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"subscriptions": [], "notified": {}}
        save_data(data)
    return data[uid]

def update_user(user_id: int, user_data: dict):
    data = load_data()
    data[str(user_id)] = user_data
    save_data(data)

# ─── API ─────────────────────────────────────────────────────────────────────

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
    cat_lower = cat.lower()
    if "seed" in cat_lower:    return "🌰"
    if "gear" in cat_lower:    return "⚙️"
    if "egg"  in cat_lower:    return "🥚"
    if "pet"  in cat_lower:    return "🐾"
    if "crop" in cat_lower:    return "🌾"
    if "fruit" in cat_lower:   return "🍓"
    if "tool"  in cat_lower:   return "🔧"
    if "shop"  in cat_lower:   return "🛒"
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
    """Return flat list of all item names from stock."""
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

# ─── Subscription check ───────────────────────────────────────────────────────

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception as e:
        logger.warning(f"Subscription check failed for {user_id}: {e}")
        return False

def subscription_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=CHANNEL_LINK)
    builder.button(text="✅ Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Посмотреть сток",      callback_data="view_stock")
    builder.button(text="🔔 Настроить уведомления", callback_data="manage_alerts")
    builder.button(text="📋 Мои подписки",          callback_data="my_subs")
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

# ─── Handlers ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я <b>Grow a Garden 2 Stock Bot</b>.\n\n"
        "Я слежу за стоком предметов в игре и пришлю уведомление, "
        "когда нужный тебе предмет появится в продаже.\n\n"
        "Для использования бота нужно подписаться на канал 👇"
    )
    if await is_subscribed(message.from_user.id):
        await message.answer(text + "\n\n✅ Ты уже подписан!", reply_markup=main_menu(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=subscription_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    if await is_subscribed(call.from_user.id):
        await call.message.edit_text(
            "✅ Подписка подтверждена! Добро пожаловать 🎉",
            reply_markup=main_menu()
        )
    else:
        await call.answer("❌ Ты ещё не подписался на канал!", show_alert=True)

async def require_sub(call: CallbackQuery) -> bool:
    if not await is_subscribed(call.from_user.id):
        await call.message.edit_text(
            "⚠️ Для использования бота нужно подписаться на канал:",
            reply_markup=subscription_keyboard()
        )
        return False
    return True

@dp.callback_query(F.data == "view_stock")
async def view_stock(call: CallbackQuery):
    if not await require_sub(call): return
    await call.answer()
    msg = await call.message.edit_text("⏳ Загружаю сток...")
    stock = await fetch_stock()
    if stock:
        text = format_stock(stock)
    else:
        text = "⚠️ Не удалось получить данные. Попробуй позже."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="view_stock")
    builder.button(text="🔙 Назад",    callback_data="back_menu")
    builder.adjust(2)
    await msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "manage_alerts")
async def manage_alerts(call: CallbackQuery):
    if not await require_sub(call): return
    await call.answer()
    stock = await fetch_stock()
    if not stock:
        await call.message.edit_text("⚠️ Не удалось загрузить список предметов. Попробуй позже.")
        return
    items = extract_all_items(stock)
    if not items:
        await call.message.edit_text("⚠️ Список предметов пуст.")
        return
    await call.message.edit_text(
        "🔔 <b>Настройка уведомлений</b>\n\nВыбери предметы, о появлении которых хочешь получать уведомления:",
        reply_markup=alerts_menu(call.from_user.id, items),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_alert(call: CallbackQuery):
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

    # Refresh keyboard
    stock = await fetch_stock()
    if stock:
        items = extract_all_items(stock)
        await call.message.edit_reply_markup(reply_markup=alerts_menu(call.from_user.id, items))

@dp.callback_query(F.data == "my_subs")
async def my_subs(call: CallbackQuery):
    if not await require_sub(call): return
    await call.answer()
    user = get_user(call.from_user.id)
    subs = user.get("subscriptions", [])
    if subs:
        text = "🔔 <b>Твои подписки на уведомления:</b>\n\n" + "\n".join(f"  • {s}" for s in sorted(subs))
    else:
        text = "У тебя нет активных подписок.\nНажми «Настроить уведомления», чтобы выбрать предметы."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Настроить уведомления", callback_data="manage_alerts")
    builder.button(text="🔙 Назад", callback_data="back_menu")
    builder.adjust(1)
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "🌱 <b>Grow a Garden 2 Stock Bot</b>\n\nВыбери действие:",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

# ─── Background stock watcher ────────────────────────────────────────────────

async def stock_watcher():
    """Periodically fetch stock and notify users about subscribed items."""
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

            if new_items and prev_items:  # skip first run
                data = load_data()
                for uid, user_data in data.items():
                    subs = set(user_data.get("subscriptions", []))
                    notified = user_data.get("notified", {})
                    matched = subs & new_items
                    # Only notify if not already notified for this item in this batch
                    to_notify = [m for m in matched if not notified.get(m)]
                    if to_notify:
                        try:
                            text = (
                                "🔔 <b>Появились предметы в стоке!</b>\n\n"
                                + "\n".join(f"✅ {item}" for item in sorted(to_notify))
                                + f"\n\n<i>Открой бота, чтобы посмотреть подробности.</i>"
                            )
                            await bot.send_message(int(uid), text, parse_mode="HTML", reply_markup=main_menu())
                            for item in to_notify:
                                notified[item] = True
                        except Exception as e:
                            logger.warning(f"Could not notify {uid}: {e}")

                # Reset notified flags for items that left the stock
                for uid, user_data in data.items():
                    notified = user_data.get("notified", {})
                    for item in list(notified.keys()):
                        if item not in current_items:
                            del notified[item]

                save_data(data)

            prev_items = current_items

        except Exception as e:
            logger.error(f"Watcher error: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    asyncio.create_task(stock_watcher())
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
