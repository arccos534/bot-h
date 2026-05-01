import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


DATE_DAYS_AHEAD = 14
WORKING_HOURS = [f"{hour:02d}:00" for hour in range(10, 20)]

DEFAULT_PRICES = [
    ("Мужская стрижка", "900 ₽"),
    ("Женская стрижка", "1 500 ₽"),
    ("Детская стрижка", "700 ₽"),
    ("Стрижка + борода", "1 300 ₽"),
    ("Укладка", "1 200 ₽"),
    ("Окрашивание", "2 500 ₽"),
]


@dataclass(frozen=True)
class MasterConfig:
    tg_id: int
    name: str


def parse_master_ids(raw_value: str) -> list[MasterConfig]:
    masters: list[MasterConfig] = []
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue

        if ":" not in part:
            raise ValueError("MASTER_IDS должен быть в формате telegram_id:Имя")

        tg_id, name = part.split(":", 1)
        masters.append(MasterConfig(tg_id=int(tg_id.strip()), name=name.strip()))

    return masters


def parse_admin_ids(raw_value: str) -> set[int]:
    return {int(part.strip()) for part in raw_value.split(",") if part.strip()}


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def format_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return f"{parsed.strftime('%d.%m')} ({weekdays[parsed.weekday()]})"


def callback_data(*parts: object) -> str:
    return ":".join(str(part) for part in parts)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    tg_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS masters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER UNIQUE,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL UNIQUE,
                    price TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS booking_drafts (
                    user_tg_id INTEGER PRIMARY KEY,
                    selected_date TEXT,
                    selected_time TEXT,
                    selected_master_id INTEGER,
                    selected_service_id INTEGER,
                    message_id INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_tg_id INTEGER NOT NULL,
                    client_name TEXT NOT NULL,
                    client_username TEXT,
                    master_id INTEGER NOT NULL,
                    service_id INTEGER,
                    service_title TEXT,
                    service_price TEXT,
                    appointment_date TEXT NOT NULL,
                    appointment_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    cancelled_at TEXT,
                    FOREIGN KEY(master_id) REFERENCES masters(id),
                    FOREIGN KEY(service_id) REFERENCES services(id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_slot
                ON appointments(master_id, appointment_date, appointment_time)
                WHERE status = 'confirmed';
                """
            )
            self._ensure_column(connection, "booking_drafts", "selected_service_id", "INTEGER")
            self._ensure_column(connection, "appointments", "service_id", "INTEGER")
            self._ensure_column(connection, "appointments", "service_title", "TEXT")
            self._ensure_column(connection, "appointments", "service_price", "TEXT")
            connection.commit()

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed(self, masters: Iterable[MasterConfig]) -> None:
        with closing(self.connect()) as connection:
            for master in masters:
                connection.execute(
                    """
                    INSERT INTO masters(tg_id, name)
                    VALUES(?, ?)
                    ON CONFLICT(tg_id) DO UPDATE SET
                        name = excluded.name,
                        active = 1
                    """,
                    (master.tg_id, master.name),
                )

            if not list(connection.execute("SELECT id FROM masters LIMIT 1")):
                for name in ("Анна", "Мария", "Егор"):
                    connection.execute("INSERT INTO masters(name) VALUES(?)", (name,))

            for title, price in DEFAULT_PRICES:
                connection.execute(
                    """
                    INSERT INTO services(title, price)
                    VALUES(?, ?)
                    ON CONFLICT(title) DO UPDATE SET price = excluded.price
                    """,
                    (title, price),
                )

            connection.commit()

    def upsert_user(self, message: Message) -> None:
        user = message.from_user
        if user is None:
            return

        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO users(tg_id, full_name, username)
                VALUES(?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username
                """,
                (user.id, user.full_name, user.username),
            )
            connection.commit()

    def reset_draft(self, user_tg_id: int, message_id: int | None = None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO booking_drafts(user_tg_id, message_id)
                VALUES(?, ?)
                ON CONFLICT(user_tg_id) DO UPDATE SET
                    selected_date = NULL,
                    selected_time = NULL,
                    selected_master_id = NULL,
                    selected_service_id = NULL,
                    message_id = COALESCE(excluded.message_id, booking_drafts.message_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_tg_id, message_id),
            )
            connection.commit()

    def update_draft(self, user_tg_id: int, **values: object) -> None:
        allowed = {"selected_date", "selected_time", "selected_master_id", "selected_service_id", "message_id"}
        assignments = [f"{key} = ?" for key in values if key in allowed]
        params = [values[key] for key in values if key in allowed]
        if not assignments:
            return

        params.append(user_tg_id)
        with closing(self.connect()) as connection:
            connection.execute(
                f"""
                INSERT INTO booking_drafts(user_tg_id)
                VALUES(?)
                ON CONFLICT(user_tg_id) DO NOTHING
                """,
                (user_tg_id,),
            )
            connection.execute(
                f"""
                UPDATE booking_drafts
                SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                WHERE user_tg_id = ?
                """,
                params,
            )
            connection.commit()

    def get_draft(self, user_tg_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute(
                "SELECT * FROM booking_drafts WHERE user_tg_id = ?",
                (user_tg_id,),
            ).fetchone()

    def get_prices(self) -> list[sqlite3.Row]:
        with closing(self.connect()) as connection:
            return list(connection.execute("SELECT id, title, price FROM services ORDER BY id"))

    def get_service(self, service_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT id, title, price FROM services WHERE id = ?", (service_id,)).fetchone()

    def get_masters(self) -> list[sqlite3.Row]:
        with closing(self.connect()) as connection:
            return list(connection.execute("SELECT * FROM masters WHERE active = 1 ORDER BY id"))

    def get_master(self, master_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT * FROM masters WHERE id = ?", (master_id,)).fetchone()

    def get_master_by_tg_id(self, tg_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT * FROM masters WHERE tg_id = ?", (tg_id,)).fetchone()

    def get_available_masters(self, appointment_date: str, appointment_time: str) -> list[sqlite3.Row]:
        with closing(self.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT masters.*
                    FROM masters
                    WHERE active = 1
                      AND id NOT IN (
                        SELECT master_id
                        FROM appointments
                        WHERE appointment_date = ?
                          AND appointment_time = ?
                          AND status = 'confirmed'
                      )
                    ORDER BY id
                    """,
                    (appointment_date, appointment_time),
                )
            )

    def create_appointment(
        self,
        user_tg_id: int,
        client_name: str,
        client_username: str | None,
    ) -> sqlite3.Row:
        with closing(self.connect()) as connection:
            draft = connection.execute(
                "SELECT * FROM booking_drafts WHERE user_tg_id = ?",
                (user_tg_id,),
            ).fetchone()
            if draft is None or not all(
                [
                    draft["selected_date"],
                    draft["selected_time"],
                    draft["selected_master_id"],
                    draft["selected_service_id"],
                ]
            ):
                raise ValueError("Черновик записи не заполнен.")

            service = connection.execute(
                "SELECT id, title, price FROM services WHERE id = ?",
                (draft["selected_service_id"],),
            ).fetchone()
            if service is None:
                raise ValueError("Выбранная услуга не найдена.")

            cursor = connection.execute(
                """
                INSERT INTO appointments(
                    user_tg_id,
                    client_name,
                    client_username,
                    master_id,
                    service_id,
                    service_title,
                    service_price,
                    appointment_date,
                    appointment_time
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_tg_id,
                    client_name,
                    client_username,
                    draft["selected_master_id"],
                    service["id"],
                    service["title"],
                    service["price"],
                    draft["selected_date"],
                    draft["selected_time"],
                ),
            )
            appointment_id = cursor.lastrowid
            connection.commit()

            return connection.execute(
                """
                SELECT appointments.*, masters.name AS master_name, masters.tg_id AS master_tg_id
                FROM appointments
                JOIN masters ON masters.id = appointments.master_id
                WHERE appointments.id = ?
                """,
                (appointment_id,),
            ).fetchone()

    def get_appointment(self, appointment_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT appointments.*, masters.name AS master_name, masters.tg_id AS master_tg_id
                FROM appointments
                JOIN masters ON masters.id = appointments.master_id
                WHERE appointments.id = ?
                """,
                (appointment_id,),
            ).fetchone()

    def cancel_appointment(self, appointment_id: int, user_tg_id: int | None = None) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            appointment = connection.execute(
                """
                SELECT appointments.*, masters.name AS master_name, masters.tg_id AS master_tg_id
                FROM appointments
                JOIN masters ON masters.id = appointments.master_id
                WHERE appointments.id = ?
                """,
                (appointment_id,),
            ).fetchone()
            if appointment is None:
                return None

            if user_tg_id is not None and appointment["user_tg_id"] != user_tg_id:
                return None

            connection.execute(
                """
                UPDATE appointments
                SET status = 'cancelled', cancelled_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'confirmed'
                """,
                (appointment_id,),
            )
            connection.commit()
            return self.get_appointment(appointment_id)

    def upcoming_for_master(self, master_id: int, limit: int = 30) -> list[sqlite3.Row]:
        today = date.today().isoformat()
        with closing(self.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT appointments.*, masters.name AS master_name
                    FROM appointments
                    JOIN masters ON masters.id = appointments.master_id
                    WHERE master_id = ?
                      AND appointment_date >= ?
                      AND status = 'confirmed'
                    ORDER BY appointment_date, appointment_time
                    LIMIT ?
                    """,
                    (master_id, today, limit),
                )
            )

    def upcoming_all(self, limit: int = 50) -> list[sqlite3.Row]:
        today = date.today().isoformat()
        with closing(self.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT appointments.*, masters.name AS master_name
                    FROM appointments
                    JOIN masters ON masters.id = appointments.master_id
                    WHERE appointment_date >= ?
                      AND status = 'confirmed'
                    ORDER BY appointment_date, appointment_time
                    LIMIT ?
                    """,
                    (today, limit),
                )
            )

    def upcoming_for_user(self, user_tg_id: int) -> list[sqlite3.Row]:
        today = date.today().isoformat()
        with closing(self.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT appointments.*, masters.name AS master_name
                    FROM appointments
                    JOIN masters ON masters.id = appointments.master_id
                    WHERE user_tg_id = ?
                      AND appointment_date >= ?
                      AND status = 'confirmed'
                    ORDER BY appointment_date, appointment_time
                    """,
                    (user_tg_id, today),
                )
            )


def make_keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def admin_contact_url(admin_ids: set[int]) -> str | None:
    explicit_url = os.getenv("ADMIN_CONTACT_URL", "").strip()
    if explicit_url:
        return explicit_url

    username = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"

    if admin_ids:
        return f"tg://user?id={next(iter(admin_ids))}"

    return None


def add_admin_contact(markup: InlineKeyboardMarkup, contact_url: str | None) -> InlineKeyboardMarkup:
    if contact_url:
        markup.inline_keyboard.append([InlineKeyboardButton(text="Связаться с администратором", url=contact_url)])
    return markup


def main_menu_keyboard(contact_url: str | None = None) -> InlineKeyboardMarkup:
    return add_admin_contact(
        make_keyboard(
            [
                [("Записаться", callback_data("book", "start"))],
                [("Прайс-лист", callback_data("price"))],
                [("Мои записи", callback_data("my"))],
            ]
        ),
        contact_url,
    )


def price_keyboard(contact_url: str | None = None) -> InlineKeyboardMarkup:
    return add_admin_contact(
        make_keyboard([[("Записаться", callback_data("book", "start"))], [("В меню", callback_data("menu"))]]),
        contact_url,
    )


def user_back_keyboard(contact_url: str | None = None) -> InlineKeyboardMarkup:
    return add_admin_contact(make_keyboard([[("В меню", callback_data("menu"))]]), contact_url)


def service_keyboard(services: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [(f"{service['title']} — {service['price']}", callback_data("service", service["id"]))]
        for service in services
    ]
    rows.append([("Назад к мастерам", callback_data("back", "master"))])
    return make_keyboard(rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return make_keyboard(
        [
            [("Записаться", callback_data("confirm"))],
            [("Изменить услугу", callback_data("back", "service"))],
            [("Изменить мастера", callback_data("back", "master"))],
            [("В меню", callback_data("menu"))],
        ]
    )


def date_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    current_row: list[tuple[str, str]] = []
    today = date.today()

    for day_offset in range(DATE_DAYS_AHEAD):
        value = today + timedelta(days=day_offset)
        current_row.append((format_date(value.isoformat()), callback_data("date", value.isoformat())))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([("Назад", callback_data("menu"))])
    return make_keyboard(rows)


def time_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    current_row: list[tuple[str, str]] = []

    for slot in WORKING_HOURS:
        current_row.append((slot, callback_data("time", slot.replace(":", ""))))
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([("Назад к датам", callback_data("book", "start"))])
    return make_keyboard(rows)


def masters_keyboard(masters: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [[(master["name"], callback_data("master", master["id"]))] for master in masters]
    rows.append([("Назад ко времени", callback_data("back", "time"))])
    return make_keyboard(rows)


def cancel_keyboard(appointment_id: int, contact_url: str | None = None) -> InlineKeyboardMarkup:
    return add_admin_contact(
        make_keyboard(
            [
                [("Отменить запись", callback_data("cancel", appointment_id))],
                [("В меню", callback_data("menu"))],
            ]
        ),
        contact_url,
    )


def master_dashboard_keyboard() -> InlineKeyboardMarkup:
    return make_keyboard([[("Обновить", callback_data("admin", "dashboard"))]])


def menu_text() -> str:
    return (
        "<b>Барбершоп / парикмахерская</b>\n\n"
        "В этом боте вы можете записать к мастеру и ознакомиться с прайс листом"
    )


def row_value(row: sqlite3.Row, key: str, default: str = "не указано") -> str:
    value = row[key] if key in row.keys() else None
    return str(value) if value else default


def prices_text(prices: list[sqlite3.Row]) -> str:
    lines = ["<b>Прайс-лист</b>", ""]
    lines.extend(f"• {row['title']} — <b>{row['price']}</b>" for row in prices)
    lines.append("")
    return "\n".join(lines)


def appointment_text(appointment: sqlite3.Row) -> str:
    return (
        "<b>Запись подтверждена</b>\n\n"
        f"Дата: <b>{format_date(appointment['appointment_date'])}</b>\n"
        f"Время: <b>{appointment['appointment_time']}</b>\n"
        f"Мастер: <b>{appointment['master_name']}</b>\n\n"
        f"Услуга: <b>{row_value(appointment, 'service_title')}</b>\n"
        f"Стоимость: <b>{row_value(appointment, 'service_price')}</b>\n\n"
        "Если планы поменяются, запись можно отменить кнопкой ниже."
    )


def booking_summary_text(draft: sqlite3.Row, master: sqlite3.Row, service: sqlite3.Row) -> str:
    return (
        "<b>Проверьте запись</b>\n\n"
        f"Дата: <b>{format_date(draft['selected_date'])}</b>\n"
        f"Время: <b>{draft['selected_time']}</b>\n"
        f"Мастер: <b>{master['name']}</b>\n\n"
        f"Услуга: <b>{service['title']}</b>\n"
        f"Стоимость: <b>{service['price']}</b>\n\n"
        "Если все верно, нажмите «Записаться»."
    )


def master_notification_text(appointment: sqlite3.Row) -> str:
    username = appointment["client_username"]
    username_text = f"@{username}" if username else "username не указан"
    return (
        "<b>Новая запись</b>\n\n"
        f"Клиент: <b>{appointment['client_name']}</b>\n"
        f"Telegram: {username_text}\n"
        f"Дата: <b>{format_date(appointment['appointment_date'])}</b>\n"
        f"Время: <b>{appointment['appointment_time']}</b>\n"
        f"Мастер: <b>{appointment['master_name']}</b>\n"
        f"Услуга: <b>{row_value(appointment, 'service_title')}</b>\n"
        f"Стоимость: <b>{row_value(appointment, 'service_price')}</b>"
    )


def cancellation_notification_text(appointment: sqlite3.Row) -> str:
    return (
        "<b>Запись отменена</b>\n\n"
        f"Клиент: <b>{appointment['client_name']}</b>\n"
        f"Дата: <b>{format_date(appointment['appointment_date'])}</b>\n"
        f"Время: <b>{appointment['appointment_time']}</b>\n"
        f"Мастер: <b>{appointment['master_name']}</b>\n"
        f"Услуга: <b>{row_value(appointment, 'service_title')}</b>"
    )


def dashboard_text(title: str, appointments: list[sqlite3.Row]) -> str:
    if not appointments:
        return f"<b>{title}</b>\n\nБлижайших записей пока нет."

    lines = [f"<b>{title}</b>", "", "Все ближайшие записи сгруппированы по датам.", ""]
    current_date = ""
    for item in appointments:
        if item["appointment_date"] != current_date:
            current_date = item["appointment_date"]
            if len(lines) > 4:
                lines.append("")
            lines.append(f"<b>{format_date(current_date)}</b>")

        username = item["client_username"]
        username_text = f" @{username}" if username else ""
        lines.append(
            f"{item['appointment_time']} — <b>{item['client_name']}</b>{username_text}\n"
            f"Мастер: {item['master_name']}\n"
            f"Услуга: {row_value(item, 'service_title')} ({row_value(item, 'service_price')})"
        )
    return "\n".join(lines)


def user_appointments_text(appointments: list[sqlite3.Row]) -> str:
    if not appointments:
        return "<b>Мои записи</b>\n\nАктивных записей пока нет."

    lines = ["<b>Мои записи</b>", ""]
    for item in appointments:
        lines.append(
            f"• {format_date(item['appointment_date'])} {item['appointment_time']} — "
            f"мастер {item['master_name']}, {row_value(item, 'service_title')}"
        )
    return "\n".join(lines)


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is None:
        return

    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


def build_router(db: Database, bot: Bot, admin_ids: set[int]) -> Router:
    router = Router()
    contact_url = admin_contact_url(admin_ids)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        db.upsert_user(message)
        sent = await message.answer(menu_text(), reply_markup=main_menu_keyboard(contact_url))
        if message.from_user is not None:
            db.update_draft(message.from_user.id, message_id=sent.message_id)

    @router.message(Command("master"))
    async def master_dashboard(message: Message) -> None:
        if message.from_user is None:
            return

        master = db.get_master_by_tg_id(message.from_user.id)
        if master is None and message.from_user.id not in admin_ids:
            await message.answer("Эта команда доступна только мастерам.")
            return

        if message.from_user.id in admin_ids:
            appointments = db.upcoming_all()
            text = dashboard_text("Админка: все ближайшие записи", appointments)
        elif master is not None:
            appointments = db.upcoming_for_master(master["id"])
            text = dashboard_text(f"Админка мастера: {master['name']}", appointments)

        await message.answer(text, reply_markup=master_dashboard_keyboard())

    @router.callback_query(F.data == "menu")
    async def show_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        await safe_edit(callback, menu_text(), main_menu_keyboard(contact_url))

    @router.callback_query(F.data == "price")
    async def show_prices(callback: CallbackQuery) -> None:
        await callback.answer()
        await safe_edit(
            callback,
            prices_text(db.get_prices()),
            price_keyboard(contact_url),
        )

    @router.callback_query(F.data == "my")
    async def show_my_appointments(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None:
            return

        appointments = db.upcoming_for_user(callback.from_user.id)
        await safe_edit(callback, user_appointments_text(appointments), user_back_keyboard(contact_url))

    @router.callback_query(F.data == callback_data("book", "start"))
    async def choose_date(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None:
            return

        message_id = callback.message.message_id if callback.message else None
        db.reset_draft(callback.from_user.id, message_id)
        await safe_edit(callback, "<b>Выберите дату</b>", date_keyboard())

    @router.callback_query(F.data.startswith("date:"))
    async def choose_time(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None or callback.data is None:
            return

        _, selected_date = callback.data.split(":", 1)
        db.update_draft(
            callback.from_user.id,
            selected_date=selected_date,
            selected_time=None,
            selected_master_id=None,
            selected_service_id=None,
        )
        await safe_edit(callback, f"<b>Дата: {format_date(selected_date)}</b>\n\nВыберите время.", time_keyboard())

    @router.callback_query(F.data.startswith("time:"))
    async def choose_master(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None or callback.data is None:
            return

        _, selected_time = callback.data.split(":", 1)
        normalized_time = f"{selected_time[:2]}:{selected_time[2:]}"
        draft = db.get_draft(callback.from_user.id)
        if draft is None or draft["selected_date"] is None:
            await safe_edit(callback, "Давайте начнем заново и выберем дату.", date_keyboard())
            return

        db.update_draft(callback.from_user.id, selected_time=normalized_time, selected_master_id=None, selected_service_id=None)
        masters = db.get_available_masters(draft["selected_date"], normalized_time)
        if not masters:
            await safe_edit(
                callback,
                "На это время все мастера заняты. Выберите другое время.",
                time_keyboard(),
            )
            return

        await safe_edit(
            callback,
            (
                f"<b>{format_date(draft['selected_date'])}, {normalized_time}</b>\n\n"
                "Выберите мастера."
            ),
            masters_keyboard(masters),
        )

    @router.callback_query(F.data == callback_data("back", "time"))
    async def back_to_time(callback: CallbackQuery) -> None:
        await callback.answer()
        draft = db.get_draft(callback.from_user.id)
        date_text = format_date(draft["selected_date"]) if draft and draft["selected_date"] else "выбранная дата"
        await safe_edit(callback, f"<b>Дата: {date_text}</b>\n\nВыберите время.", time_keyboard())

    @router.callback_query(F.data == callback_data("back", "master"))
    async def back_to_master(callback: CallbackQuery) -> None:
        await callback.answer()
        draft = db.get_draft(callback.from_user.id)
        if draft is None or not draft["selected_date"] or not draft["selected_time"]:
            await safe_edit(callback, "Давайте начнем заново и выберем дату.", date_keyboard())
            return

        masters = db.get_available_masters(draft["selected_date"], draft["selected_time"])
        await safe_edit(
            callback,
            (
                f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\n\n"
                "Выберите мастера."
            ),
            masters_keyboard(masters),
        )

    @router.callback_query(F.data.startswith("master:"))
    async def choose_service(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None or callback.data is None:
            return

        _, raw_master_id = callback.data.split(":", 1)
        master_id = int(raw_master_id)
        db.update_draft(callback.from_user.id, selected_master_id=master_id, selected_service_id=None)
        draft = db.get_draft(callback.from_user.id)
        master = db.get_master(master_id)
        if draft is None or master is None:
            await safe_edit(callback, "Не удалось собрать запись. Попробуйте еще раз.", main_menu_keyboard(contact_url))
            return

        await safe_edit(
            callback,
            (
                f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\n"
                f"Мастер: <b>{master['name']}</b>\n\n"
                "Выберите услугу."
            ),
            service_keyboard(db.get_prices()),
        )

    @router.callback_query(F.data == callback_data("back", "service"))
    async def back_to_service(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None:
            return

        draft = db.get_draft(callback.from_user.id)
        if draft is None or not draft["selected_date"] or not draft["selected_time"] or not draft["selected_master_id"]:
            await safe_edit(callback, "Давайте начнем заново и выберем дату.", date_keyboard())
            return

        master = db.get_master(draft["selected_master_id"])
        if master is None:
            await safe_edit(callback, "Мастер не найден. Выберите мастера заново.", masters_keyboard(db.get_masters()))
            return

        await safe_edit(
            callback,
            (
                f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\n"
                f"Мастер: <b>{master['name']}</b>\n\n"
                "Выберите услугу."
            ),
            service_keyboard(db.get_prices()),
        )

    @router.callback_query(F.data.startswith("service:"))
    async def show_confirmation(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None or callback.data is None:
            return

        _, raw_service_id = callback.data.split(":", 1)
        service_id = int(raw_service_id)
        db.update_draft(callback.from_user.id, selected_service_id=service_id)
        draft = db.get_draft(callback.from_user.id)
        service = db.get_service(service_id)
        if draft is None or service is None or not draft["selected_master_id"]:
            await safe_edit(callback, "Не удалось собрать запись. Попробуйте еще раз.", main_menu_keyboard(contact_url))
            return

        master = db.get_master(draft["selected_master_id"])
        if master is None:
            await safe_edit(callback, "Мастер не найден. Выберите мастера заново.", masters_keyboard(db.get_masters()))
            return

        await safe_edit(callback, booking_summary_text(draft, master, service), confirm_keyboard())

    @router.callback_query(F.data == "confirm")
    async def confirm_booking(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None:
            return

        try:
            appointment = db.create_appointment(
                user_tg_id=callback.from_user.id,
                client_name=callback.from_user.full_name,
                client_username=callback.from_user.username,
            )
        except sqlite3.IntegrityError:
            await safe_edit(
                callback,
                "Этот слот только что заняли. Выберите другое время или мастера.",
                time_keyboard(),
            )
            return
        except ValueError:
            await safe_edit(callback, "Давайте начнем заново и выберем дату.", date_keyboard())
            return

        await safe_edit(callback, appointment_text(appointment), cancel_keyboard(appointment["id"], contact_url))

        master_tg_id = appointment["master_tg_id"]
        if master_tg_id:
            await bot.send_message(
                master_tg_id,
                master_notification_text(appointment),
                reply_markup=master_dashboard_keyboard(),
            )

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_booking(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.from_user is None or callback.data is None:
            return

        _, raw_appointment_id = callback.data.split(":", 1)
        appointment = db.cancel_appointment(int(raw_appointment_id), callback.from_user.id)
        if appointment is None:
            await safe_edit(callback, "Не нашел активную запись для отмены.", main_menu_keyboard(contact_url))
            return

        await safe_edit(
            callback,
            (
                "<b>Запись отменена</b>\n\n"
                f"{format_date(appointment['appointment_date'])} в {appointment['appointment_time']}, "
                f"мастер {appointment['master_name']}."
            ),
            main_menu_keyboard(contact_url),
        )

        master_tg_id = appointment["master_tg_id"]
        if master_tg_id:
            await bot.send_message(master_tg_id, cancellation_notification_text(appointment))

    @router.callback_query(F.data == callback_data("admin", "dashboard"))
    async def refresh_dashboard(callback: CallbackQuery) -> None:
        await callback.answer("Обновлено")
        if callback.from_user is None:
            return

        master = db.get_master_by_tg_id(callback.from_user.id)
        if master is None and callback.from_user.id not in admin_ids:
            await safe_edit(callback, "Эта админка доступна только мастерам.")
            return

        if callback.from_user.id in admin_ids:
            appointments = db.upcoming_all()
            text = dashboard_text("Админка: все ближайшие записи", appointments)
        elif master is not None:
            appointments = db.upcoming_for_master(master["id"])
            text = dashboard_text(f"Админка мастера: {master['name']}", appointments)

        await safe_edit(callback, text, master_dashboard_keyboard())

    return router


async def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO)

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Заполните BOT_TOKEN в .env")

    masters = parse_master_ids(os.getenv("MASTER_IDS", ""))
    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

    db = Database(os.getenv("DB_PATH", "salon.sqlite3"))
    db.seed(masters)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(db, bot, admin_ids))

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
