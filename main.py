"""Digital Shop Bot — aiogram 3 + SQLite + Telegram Stars.

Магазин цифровых товаров: каталог, корзина, промокоды, оплата звёздами,
мгновенная выдача доступов, история заказов, админка со статистикой.
"""

import asyncio
import logging
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta
from html import escape

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # свой ID узнать у @userinfobot
DB_PATH = "shop.db"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

SEED_PRODUCTS = [
    ("Курсы", "Python с нуля до бота",
     "12 модулей, 40 часов видео, домашки с проверкой. От переменных до деплоя на сервер.", 1,
     "🎓 Доступ к курсу: https://example.com/course-python\nЛогин придёт на почту в течение 5 минут."),
    ("Курсы", "Telegram-боты на aiogram 3",
     "Практический курс: 5 ботов от идеи до продакшена. Платежи, БД, деплой.", 1,
     "🎓 Доступ к курсу: https://example.com/course-aiogram\nБонус: исходники всех 5 ботов."),
    ("Курсы", "SQL для разработчиков",
     "Запросы, индексы, оптимизация. 20 часов практики на реальных базах.", 1,
     "🎓 Доступ к курсу: https://example.com/course-sql"),
    ("Подписки", "PRO-доступ · 1 месяц",
     "Закрытый чат, разбор кода, ревью проектов, ответы на вопросы.", 1,
     "🔑 Приглашение в закрытый чат: https://t.me/+example\nДействует 30 дней."),
    ("Подписки", "PRO-доступ · 1 год",
     "Всё то же самое, но на год. Плюс личные консультации раз в месяц.", 1,
     "🔑 Приглашение в закрытый чат: https://t.me/+example\nДействует 365 дней."),
    ("Шаблоны", "Набор шаблонов ботов",
     "6 готовых проектов с исходниками: магазин, запись, рассылки, квиз, поддержка, погода.", 1,
     "📦 Архив с исходниками: https://example.com/templates.zip"),
    ("Шаблоны", "Стартовый шаблон aiogram",
     "Каркас проекта: структура, middlewares, БД, конфиг, Docker. Экономит день работы.", 1,
     "📦 Репозиторий: https://github.com/example/aiogram-template"),
    ("Инструменты", "Гайд по деплою на VPS",
     "Пошагово: от покупки сервера до systemd и автозапуска. С готовыми конфигами.", 1,
     "📄 PDF-гайд: https://example.com/vps-guide.pdf"),
]


# ═══════════════════════════ БАЗА ДАННЫХ ═══════════════════════════

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT    NOT NULL,
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL,
                price       INTEGER NOT NULL,
                content     TEXT    NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS cart (
                user_id    INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                qty        INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                email      TEXT    NOT NULL,
                total      INTEGER NOT NULL,
                discount   INTEGER NOT NULL DEFAULT 0,
                promo_code TEXT,
                status     TEXT    NOT NULL DEFAULT 'paid',
                created_at TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id   INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                title      TEXT    NOT NULL,
                price      INTEGER NOT NULL,
                qty        INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promocodes (
                code             TEXT PRIMARY KEY,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                discount_fixed   INTEGER NOT NULL DEFAULT 0,
                max_uses         INTEGER NOT NULL DEFAULT 0,
                used             INTEGER NOT NULL DEFAULT 0,
                is_active        INTEGER NOT NULL DEFAULT 1
            );
        """)
        cursor = await db.execute("SELECT COUNT(*) FROM products")
        (count,) = await cursor.fetchone()
        if count == 0:
            await db.executemany(
                "INSERT INTO products (category, title, description, price, content) "
                "VALUES (?, ?, ?, ?, ?)",
                SEED_PRODUCTS,
            )
            logger.info("Каталог заполнен: %d товаров", len(SEED_PRODUCTS))
        await db.commit()


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        return [dict(row) for row in await cursor.fetchall()]


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    rows = await fetch_all(query, params)
    return rows[0] if rows else None


async def execute(query: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        await db.commit()
        return cursor.lastrowid


async def get_categories() -> list[str]:
    rows = await fetch_all(
        "SELECT DISTINCT category FROM products WHERE is_active = 1 ORDER BY category"
    )
    return [row["category"] for row in rows]


async def get_cart(user_id: int) -> list[dict]:
    return await fetch_all(
        """SELECT p.id, p.title, p.price, p.content, c.qty
           FROM cart c JOIN products p ON p.id = c.product_id
           WHERE c.user_id = ? ORDER BY p.title""",
        (user_id,),
    )


def cart_total(items: list[dict]) -> int:
    return sum(item["price"] * item["qty"] for item in items)


# ═══════════════════════════ ПРОМОКОДЫ ═══════════════════════════

async def get_promo(code: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM promocodes WHERE code = ? AND is_active = 1", (code.upper(),)
    )


def promo_error(promo: dict) -> str | None:
    """None — промокод годен, иначе текст проблемы."""
    if promo["max_uses"] and promo["used"] >= promo["max_uses"]:
        return "у промокода закончились применения"
    return None


def apply_discount(total: int, promo: dict | None) -> tuple[int, int]:
    """Возвращает (к оплате, скидка). Минимум 1 ⭐ — таково требование Telegram Stars."""
    if not promo:
        return total, 0
    discount = total * promo["discount_percent"] // 100 + promo["discount_fixed"]
    discount = max(0, min(discount, total - 1))
    return total - discount, discount


# ═══════════════════════════ КЛАВИАТУРЫ ═══════════════════════════

def main_menu_kb(is_admin: bool = False):
    builder = InlineKeyboardBuilder()
    builder.button(text="🛍 Каталог", callback_data="catalog")
    builder.button(text="🛒 Корзина", callback_data="cart")
    builder.button(text="📦 Мои заказы", callback_data="orders")
    if is_admin:
        builder.button(text="⚙️ Админка", callback_data="admin")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


async def catalog_kb():
    builder = InlineKeyboardBuilder()
    for category in await get_categories():
        builder.button(text=category, callback_data=f"cat:{category}")
    builder.button(text="⬅️ Назад", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


async def category_kb(category: str):
    builder = InlineKeyboardBuilder()
    products = await fetch_all(
        "SELECT id, title, price FROM products WHERE category = ? AND is_active = 1 ORDER BY id",
        (category,),
    )
    for product in products:
        builder.button(
            text=f"{product['title']} — {product['price']} ⭐",
            callback_data=f"prod:{product['id']}",
        )
    builder.button(text="⬅️ К категориям", callback_data="catalog")
    builder.adjust(1)
    return builder.as_markup()


def product_kb(product_id: int, category: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ В корзину", callback_data=f"add:{product_id}")
    builder.button(text="🛒 Корзина", callback_data="cart")
    builder.button(text="⬅️ Назад", callback_data=f"cat:{category}")
    builder.adjust(2, 1)
    return builder.as_markup()


def cart_kb(items: list[dict]):
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.row(
            *InlineKeyboardBuilder()
            .button(text="➖", callback_data=f"qty:{item['id']}:-1")
            .button(text=f"{item['title'][:18]} ×{item['qty']}", callback_data="noop")
            .button(text="➕", callback_data=f"qty:{item['id']}:1")
            .buttons
        )
    builder.row(
        *InlineKeyboardBuilder()
        .button(text="💳 Оформить заказ", callback_data="checkout")
        .buttons
    )
    builder.row(
        *InlineKeyboardBuilder()
        .button(text="🗑 Очистить", callback_data="clear")
        .button(text="⬅️ В меню", callback_data="menu")
        .buttons
    )
    return builder.as_markup()


def admin_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="➕ Добавить товар", callback_data="admin:add")
    builder.button(text="🎟 Создать промокод", callback_data="admin:promo")
    builder.button(text="📦 Все заказы", callback_data="admin:orders")
    builder.button(text="⬅️ В меню", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


# ═══════════════════════════ СОСТОЯНИЯ ═══════════════════════════

class Checkout(StatesGroup):
    email = State()
    promo = State()


class AddProduct(StatesGroup):
    category = State()
    title = State()
    description = State()
    price = State()
    content = State()


class AddPromo(StatesGroup):
    code = State()
    discount = State()
    max_uses = State()


router = Router()


# ═══════════════════════════ КАТАЛОГ ═══════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"👋 Привет, {escape(message.from_user.first_name)}!\n\n"
        "<b>Digital Shop</b> — курсы, подписки и шаблоны.\n\n"
        "<blockquote>"
        "⭐️ Оплата через Telegram Stars\n"
        "⚡️ Доступы приходят мгновенно\n"
        "🎟 Есть промокоды"
        "</blockquote>",
        reply_markup=main_menu_kb(message.from_user.id == ADMIN_ID),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "Главное меню:", reply_markup=main_menu_kb(callback.from_user.id == ADMIN_ID)
    )


@router.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("🛍 Выбери категорию:", reply_markup=await catalog_kb())


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery) -> None:
    category = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.answer(
        f"<b>{escape(category)}</b>", reply_markup=await category_kb(category)
    )


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(callback: CallbackQuery) -> None:
    product_id = int(callback.data.split(":")[1])
    product = await fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))
    await callback.answer()
    if not product:
        await callback.message.answer("Товар не найден 🤷")
        return

    await callback.message.answer(
        f"<b>{escape(product['title'])}</b>\n\n"
        f"{escape(product['description'])}\n\n"
        f"💰 Цена: <b>{product['price']} ⭐</b>",
        reply_markup=product_kb(product["id"], product["category"]),
    )


# ═══════════════════════════ КОРЗИНА ═══════════════════════════

@router.callback_query(F.data.startswith("add:"))
async def cb_add_to_cart(callback: CallbackQuery) -> None:
    product_id = int(callback.data.split(":")[1])
    await execute(
        """INSERT INTO cart (user_id, product_id, qty) VALUES (?, ?, 1)
           ON CONFLICT(user_id, product_id) DO UPDATE SET qty = qty + 1""",
        (callback.from_user.id, product_id),
    )
    await callback.answer("Добавлено в корзину ✅")


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


async def show_cart(message: Message, user_id: int, edit: bool = False) -> None:
    items = await get_cart(user_id)
    if not items:
        text = "🛒 Корзина пуста.\nЗагляни в каталог!"
        keyboard = main_menu_kb(user_id == ADMIN_ID)
    else:
        lines = ["🛒 <b>Твоя корзина</b>\n"]
        for item in items:
            lines.append(
                f"• {escape(item['title'])}\n"
                f"  {item['price']} ⭐ × {item['qty']} = <b>{item['price'] * item['qty']} ⭐</b>"
            )
        lines.append(f"\n<b>Итого: {cart_total(items)} ⭐</b>")
        text = "\n".join(lines)
        keyboard = cart_kb(items)

    if edit:
        with suppress(Exception):  # текст мог не измениться — Telegram ругнётся, нам всё равно
            await message.edit_text(text, reply_markup=keyboard)
        return
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "cart")
async def cb_cart(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_cart(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("qty:"))
async def cb_change_qty(callback: CallbackQuery) -> None:
    _, product_id, delta = callback.data.split(":")
    user_id = callback.from_user.id
    await execute(
        "UPDATE cart SET qty = qty + ? WHERE user_id = ? AND product_id = ?",
        (int(delta), user_id, int(product_id)),
    )
    await execute("DELETE FROM cart WHERE user_id = ? AND qty <= 0", (user_id,))
    await callback.answer()
    await show_cart(callback.message, user_id, edit=True)


@router.callback_query(F.data == "clear")
async def cb_clear(callback: CallbackQuery) -> None:
    await execute("DELETE FROM cart WHERE user_id = ?", (callback.from_user.id,))
    await callback.answer("Корзина очищена")
    with suppress(Exception):
        await callback.message.edit_text(
            "🛒 Корзина пуста.", reply_markup=main_menu_kb(callback.from_user.id == ADMIN_ID)
        )


# ═══════════════════════════ ОФОРМЛЕНИЕ ЗАКАЗА (FSM) ═══════════════════════════

@router.callback_query(F.data == "checkout")
async def cb_checkout(callback: CallbackQuery, state: FSMContext) -> None:
    items = await get_cart(callback.from_user.id)
    await callback.answer()
    if not items:
        await callback.message.answer("Корзина пуста 🛒")
        return

    await state.set_state(Checkout.email)
    await callback.message.answer(
        "📧 <b>На какой e-mail отправить доступы?</b>\n\n"
        "Напиши адрес одним сообщением. Отменить — /cancel"
    )


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb(message.from_user.id == ADMIN_ID))


@router.message(Checkout.email, F.text)
async def process_email(message: Message, state: FSMContext) -> None:
    email = message.text.strip()
    if not EMAIL_RE.match(email):
        await message.answer("Это не похоже на e-mail 🤔 Попробуй ещё раз или /cancel")
        return

    await state.update_data(email=email)
    await state.set_state(Checkout.promo)

    builder = InlineKeyboardBuilder()
    builder.button(text="Без промокода →", callback_data="promo:skip")
    await message.answer(
        "🎟 <b>Есть промокод?</b>\nВведи его сообщением — или пропусти этот шаг.",
        reply_markup=builder.as_markup(),
    )


async def send_order_invoice(
    message: Message, bot: Bot, state: FSMContext, user_id: int, promo_code: str = ""
) -> None:
    data = await state.get_data()
    email = data.get("email", "")
    items = await get_cart(user_id)
    if not items:
        await state.clear()
        await message.answer("Корзина опустела 🛒")
        return

    total = cart_total(items)
    promo = await get_promo(promo_code) if promo_code else None
    payable, discount = apply_discount(total, promo)
    await state.clear()

    label = f"Заказ (скидка {discount} ⭐)" if discount else "Заказ"
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Заказ в Digital Shop",
        description=", ".join(f"{i['title']} ×{i['qty']}" for i in items)[:255],
        payload=f"{email}|{promo_code}",
        provider_token="",   # для Stars провайдер не нужен
        currency="XTR",      # Telegram Stars
        prices=[LabeledPrice(label=label, amount=payable)],
    )


@router.message(Checkout.promo, F.text)
async def process_promo(message: Message, state: FSMContext, bot: Bot) -> None:
    code = message.text.strip().upper()
    promo = await get_promo(code)
    if not promo:
        await message.answer("Такого промокода нет 🤔 Введи другой или жми «Без промокода».")
        return

    error = promo_error(promo)
    if error:
        await message.answer(f"Не сработало — {error}.")
        return

    await message.answer(f"✅ Промокод <b>{escape(code)}</b> применён")
    await send_order_invoice(message, bot, state, message.from_user.id, code)


@router.callback_query(Checkout.promo, F.data == "promo:skip")
async def cb_skip_promo(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await send_order_invoice(callback.message, bot, state, callback.from_user.id)


@router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery) -> None:
    """Последний шанс отменить платёж — например, если товар кончился."""
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    parts = payment.invoice_payload.split("|", 1)
    email = parts[0]
    promo_code = parts[1] if len(parts) > 1 else ""
    user_id = message.from_user.id

    items = await get_cart(user_id)
    paid = payment.total_amount
    discount = max(cart_total(items) - paid, 0)

    order_id = await execute(
        "INSERT INTO orders (user_id, username, email, total, discount, promo_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, message.from_user.username, email, paid, discount, promo_code or None,
         datetime.now().isoformat(timespec="seconds")),
    )
    for item in items:
        await execute(
            "INSERT INTO order_items (order_id, product_id, title, price, qty) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_id, item["id"], item["title"], item["price"], item["qty"]),
        )
    if promo_code:
        await execute("UPDATE promocodes SET used = used + 1 WHERE code = ?", (promo_code,))
    await execute("DELETE FROM cart WHERE user_id = ?", (user_id,))

    lines = [f"✅ <b>Оплачено! Заказ №{order_id}</b>\n", "🔑 <b>Твои доступы:</b>\n"]
    for item in items:
        lines.append(f"• <b>{escape(item['title'])}</b>\n{escape(item['content'])}\n")
    if discount:
        lines.append(f"🎟 Скидка по промокоду: <b>{discount} ⭐</b>")
    lines.append(f"📧 Копия отправлена на {escape(email)}")
    lines.append("\n<i>История заказов: /orders</i>")

    await message.answer("\n".join(lines), reply_markup=main_menu_kb(user_id == ADMIN_ID))

    if ADMIN_ID:
        with suppress(Exception):
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Новый заказ №{order_id}</b>\n"
                f"От: @{message.from_user.username or user_id}\n"
                f"E-mail: {escape(email)}\n"
                f"Сумма: <b>{paid} ⭐</b>" + (f" (скидка {discount} ⭐)" if discount else ""),
            )


# ═══════════════════════════ ИСТОРИЯ ЗАКАЗОВ ═══════════════════════════

async def show_orders(message: Message, user_id: int) -> None:
    orders = await fetch_all(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,)
    )
    if not orders:
        await message.answer("📭 У тебя пока нет заказов.")
        return

    builder = InlineKeyboardBuilder()
    lines = ["📦 <b>Твои заказы</b>\n"]
    for order in orders:
        lines.append(
            f"<b>№{order['id']}</b> — {order['total']} ⭐ — <i>{order['created_at'][:10]}</i>"
        )
        builder.button(text=f"🔑 Доступы к №{order['id']}", callback_data=f"reorder:{order['id']}")
    builder.adjust(1)
    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    await show_orders(message, message.from_user.id)


@router.callback_query(F.data == "orders")
async def cb_orders(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_orders(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("reorder:"))
async def cb_reorder(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    # Проверка владельца: чужой заказ не отдадим, даже если подделать callback_data
    order = await fetch_one(
        "SELECT id FROM orders WHERE id = ? AND user_id = ?", (order_id, callback.from_user.id)
    )
    await callback.answer()
    if not order:
        await callback.message.answer("Заказ не найден 🤷")
        return

    items = await fetch_all(
        """SELECT oi.title, p.content FROM order_items oi
           LEFT JOIN products p ON p.id = oi.product_id
           WHERE oi.order_id = ?""",
        (order_id,),
    )
    lines = [f"🔑 <b>Доступы — заказ №{order_id}</b>\n"]
    for item in items:
        content = item["content"] or "товар больше недоступен"
        lines.append(f"• <b>{escape(item['title'])}</b>\n{escape(content)}\n")
    await callback.message.answer("\n".join(lines))


# ═══════════════════════════ АДМИНКА ═══════════════════════════

@router.callback_query(F.data.startswith("admin"))
async def cb_admin_guard(callback: CallbackQuery, state: FSMContext) -> None:
    """Единая точка входа в админку: сначала проверяем права, потом разводим по действиям."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    action = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    await callback.answer()

    if action == "":
        await callback.message.answer("⚙️ <b>Панель администратора</b>", reply_markup=admin_kb())
    elif action == "stats":
        await admin_stats(callback.message, callback.bot)
    elif action == "orders":
        await admin_orders(callback.message)
    elif action == "add":
        await state.set_state(AddProduct.category)
        await callback.message.answer("Категория товара? (/cancel — отмена)")
    elif action == "promo":
        await state.set_state(AddPromo.code)
        await callback.message.answer(
            "Код промокода? Например <code>SALE20</code>  (/cancel — отмена)"
        )


async def admin_stats(message: Message, bot: Bot) -> None:
    total = await fetch_one(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS sum, "
        "COUNT(DISTINCT user_id) AS buyers FROM orders"
    )
    today = await fetch_one(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS sum FROM orders WHERE created_at >= ?",
        (datetime.now().strftime("%Y-%m-%d"),),
    )
    week = await fetch_one(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS sum FROM orders WHERE created_at >= ?",
        ((datetime.now() - timedelta(days=7)).isoformat(timespec="seconds"),),
    )
    top = await fetch_all(
        "SELECT title, SUM(qty) AS qty FROM order_items GROUP BY title ORDER BY qty DESC LIMIT 5"
    )

    balance = "недоступен"
    with suppress(Exception):
        balance = f"{(await bot.get_my_star_balance()).amount} ⭐"

    lines = [
        "📊 <b>Статистика</b>\n",
        "<pre>"
        f"Сегодня   {today['cnt']:>3} зак.  {today['sum']:>5} ⭐\n"
        f"7 дней    {week['cnt']:>3} зак.  {week['sum']:>5} ⭐\n"
        f"Всего     {total['cnt']:>3} зак.  {total['sum']:>5} ⭐\n"
        f"Покупателей: {total['buyers']}"
        "</pre>",
        f"💰 Баланс бота: <b>{balance}</b>",
    ]
    if top:
        lines.append("\n🔥 <b>Топ товаров</b>")
        for i, item in enumerate(top, 1):
            lines.append(f"{i}. {escape(item['title'])} — {item['qty']} шт.")
    await message.answer("\n".join(lines), reply_markup=admin_kb())


async def admin_orders(message: Message) -> None:
    orders = await fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 10")
    if not orders:
        await message.answer("Заказов пока нет.", reply_markup=admin_kb())
        return

    lines = ["📦 <b>Последние заказы</b>\n"]
    for order in orders:
        promo = f" · 🎟 {escape(order['promo_code'])}" if order["promo_code"] else ""
        lines.append(
            f"<b>№{order['id']}</b> — {order['total']} ⭐{promo}\n"
            f"{escape(order['email'])} · <i>{order['created_at'][:16]}</i>"
        )
    await message.answer("\n\n".join(lines), reply_markup=admin_kb())


# ─── Добавление товара ───

@router.message(AddProduct.category, F.text)
async def add_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=message.text.strip())
    await state.set_state(AddProduct.title)
    await message.answer("Название товара?")


@router.message(AddProduct.title, F.text)
async def add_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(AddProduct.description)
    await message.answer("Описание?")


@router.message(AddProduct.description, F.text)
async def add_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text.strip())
    await state.set_state(AddProduct.price)
    await message.answer("Цена в звёздах? (целое число)")


@router.message(AddProduct.price, F.text)
async def add_price(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
        await message.answer("Нужно целое число не меньше 1, например: 150")
        return
    await state.update_data(price=int(message.text.strip()))
    await state.set_state(AddProduct.content)
    await message.answer("Что выдавать после оплаты? (ссылка, ключ, инструкция)")


@router.message(AddProduct.content, F.text)
async def add_content(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    product_id = await execute(
        "INSERT INTO products (category, title, description, price, content) VALUES (?, ?, ?, ?, ?)",
        (data["category"], data["title"], data["description"], data["price"], message.text.strip()),
    )
    await message.answer(
        f"✅ Товар добавлен (id={product_id})\n"
        f"<b>{escape(data['title'])}</b> — {data['price']} ⭐",
        reply_markup=admin_kb(),
    )


# ─── Создание промокода ───

@router.message(AddPromo.code, F.text)
async def promo_code_step(message: Message, state: FSMContext) -> None:
    await state.update_data(code=message.text.strip().upper())
    await state.set_state(AddPromo.discount)
    await message.answer(
        "Размер скидки?\n\n"
        "<blockquote>"
        "<code>20%</code> — двадцать процентов\n"
        "<code>50</code> — фиксированно 50 ⭐"
        "</blockquote>"
    )


@router.message(AddPromo.discount, F.text)
async def promo_discount_step(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if raw.endswith("%") and raw[:-1].isdigit():
        await state.update_data(percent=int(raw[:-1]), fixed=0)
    elif raw.isdigit():
        await state.update_data(percent=0, fixed=int(raw))
    else:
        await message.answer("Не понял. Введи <code>20%</code> или <code>50</code>")
        return
    await state.set_state(AddPromo.max_uses)
    await message.answer("Сколько раз можно применить? <code>0</code> — без ограничений")


@router.message(AddPromo.max_uses, F.text)
async def promo_uses_step(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Нужно целое число")
        return
    data = await state.get_data()
    await state.clear()

    await execute(
        "INSERT OR REPLACE INTO promocodes (code, discount_percent, discount_fixed, max_uses) "
        "VALUES (?, ?, ?, ?)",
        (data["code"], data["percent"], data["fixed"], int(message.text.strip())),
    )
    size = f"{data['percent']}%" if data["percent"] else f"{data['fixed']} ⭐"
    uses = int(message.text.strip()) or "без ограничений"
    await message.answer(
        f"🎟 Промокод <b>{escape(data['code'])}</b> создан\n"
        f"Скидка: <b>{size}</b>\nПрименений: {uses}",
        reply_markup=admin_kb(),
    )


# ═══════════════════════════ ЗАПУСК ═══════════════════════════

async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="orders", description="📦 Мои заказы"),
        BotCommand(command="cancel", description="❌ Отменить действие"),
    ])


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь его в Secrets.")
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан — админка недоступна")

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Магазин запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Магазин остановлен")


if __name__ == "__main__":
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())