import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from db import Database, MasterConfig


DATE_DAYS_AHEAD = 14
WORKING_HOURS = [f"{h:02d}:00" for h in range(10, 20)]


def parse_master_ids(raw: str) -> list[MasterConfig]:
    masters: list[MasterConfig] = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        if ":" not in part:
            raise ValueError("MASTER_IDS должен быть в формате telegram_id:Имя")
        tg_id, name = part.split(":", 1)
        masters.append(MasterConfig(tg_id=int(tg_id.strip()), name=name.strip()))
    return masters


def parse_admin_ids(raw: str) -> set[int]:
    return {int(p.strip()) for p in raw.split(",") if p.strip()}


def format_date(value: str) -> str:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return f"{d.strftime('%d.%m')} ({'пн вт ср чт пт сб вс'.split()[d.weekday()]})"


def cd(*parts: object) -> str:
    return ":".join(str(p) for p in parts)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def add_contact(markup: InlineKeyboardMarkup, url: str | None) -> InlineKeyboardMarkup:
    if url:
        markup.inline_keyboard.append([InlineKeyboardButton(text="Связаться с администратором", url=url)])
    return markup


def contact_url(admin_ids: set[int]) -> str | None:
    if u := os.getenv("ADMIN_CONTACT_URL", "").strip(): return u
    if u := os.getenv("ADMIN_USERNAME", "").strip().lstrip("@"): return f"https://t.me/{u}"
    return f"tg://user?id={next(iter(admin_ids))}" if admin_ids else None


def menu_kb(url=None):   return add_contact(kb([[("Записаться", cd("book", "start"))], [("Прайс-лист", cd("price"))], [("Мои записи", cd("my"))]]), url)
def price_kb(url=None):  return add_contact(kb([[("Записаться", cd("book", "start"))], [("В меню", cd("menu"))]]), url)
def back_kb(url=None):   return add_contact(kb([[("В меню", cd("menu"))]]), url)
def confirm_kb():        return kb([[("Записаться", cd("confirm"))], [("Изменить услугу", cd("back", "service"))], [("Изменить мастера", cd("back", "master"))], [("В меню", cd("menu"))]])
def dashboard_kb():      return kb([[("Обновить", cd("admin", "dashboard"))]])
def cancel_kb(aid, url=None): return add_contact(kb([[("Отменить запись", cd("cancel", aid))], [("В меню", cd("menu"))]]), url)

def date_kb() -> InlineKeyboardMarkup:
    today = date.today()
    pairs = [(format_date((today + timedelta(days=i)).isoformat()), cd("date", (today + timedelta(days=i)).isoformat())) for i in range(DATE_DAYS_AHEAD)]
    rows = [pairs[i:i+2] for i in range(0, len(pairs), 2)]
    rows.append([("Назад", cd("menu"))])
    return kb(rows)

def time_kb() -> InlineKeyboardMarkup:
    slots = [(s, cd("time", s.replace(":", ""))) for s in WORKING_HOURS]
    rows = [slots[i:i+3] for i in range(0, len(slots), 3)]
    rows.append([("Назад к датам", cd("book", "start"))])
    return kb(rows)

def masters_kb(masters) -> InlineKeyboardMarkup:
    rows = [[(m["name"], cd("master", m["id"]))] for m in masters]
    rows.append([("Назад ко времени", cd("back", "time"))])
    return kb(rows)

def services_kb(services) -> InlineKeyboardMarkup:
    rows = [[(f"{s['title']} — {s['price']}", cd("service", s["id"]))] for s in services]
    rows.append([("Назад к мастерам", cd("back", "master"))])
    return kb(rows)


def rv(row: sqlite3.Row, key: str, default: str = "не указано") -> str:
    v = row[key] if key in row.keys() else None
    return str(v) if v else default

def menu_text() -> str:
    return "<b>Барбершоп / парикмахерская</b>\n\nВ этом боте вы можете записаться к мастеру и ознакомиться с прайс-листом"

def prices_text(prices) -> str:
    return "<b>Прайс-лист</b>\n\n" + "\n".join(f"• {r['title']} — <b>{r['price']}</b>" for r in prices) + "\n"

def appointment_text(a) -> str:
    return (f"<b>Запись подтверждена</b>\n\nДата: <b>{format_date(a['appointment_date'])}</b>  Время: <b>{a['appointment_time']}</b>\n"
            f"Мастер: <b>{a['master_name']}</b>\n\nУслуга: <b>{rv(a,'service_title')}</b>  Стоимость: <b>{rv(a,'service_price')}</b>\n\n"
            "Если планы поменяются, запись можно отменить кнопкой ниже.")

def summary_text(draft, master, svc) -> str:
    return (f"<b>Проверьте запись</b>\n\nДата: <b>{format_date(draft['selected_date'])}</b>  Время: <b>{draft['selected_time']}</b>\n"
            f"Мастер: <b>{master['name']}</b>\n\nУслуга: <b>{svc['title']}</b>  Стоимость: <b>{svc['price']}</b>\n\nЕсли все верно, нажмите «Записаться».")

def master_notify_text(a) -> str:
    u = f"@{a['client_username']}" if a["client_username"] else "username не указан"
    return (f"<b>Новая запись</b>\n\nКлиент: <b>{a['client_name']}</b>  Telegram: {u}\n"
            f"Дата: <b>{format_date(a['appointment_date'])}</b>  Время: <b>{a['appointment_time']}</b>\n"
            f"Мастер: <b>{a['master_name']}</b>  Услуга: <b>{rv(a,'service_title')}</b>  Стоимость: <b>{rv(a,'service_price')}</b>")

def cancel_notify_text(a) -> str:
    return (f"<b>Запись отменена</b>\n\nКлиент: <b>{a['client_name']}</b>\n"
            f"Дата: <b>{format_date(a['appointment_date'])}</b>  Время: <b>{a['appointment_time']}</b>\n"
            f"Мастер: <b>{a['master_name']}</b>  Услуга: <b>{rv(a,'service_title')}</b>")

def dashboard_text(title: str, appts) -> str:
    if not appts:
        return f"<b>{title}</b>\n\nБлижайших записей пока нет."
    lines = [f"<b>{title}</b>", "", "Все ближайшие записи сгруппированы по датам.", ""]
    cur = ""
    for a in appts:
        if a["appointment_date"] != cur:
            cur = a["appointment_date"]
            if len(lines) > 4: lines.append("")
            lines.append(f"<b>{format_date(cur)}</b>")
        u = f" @{a['client_username']}" if a["client_username"] else ""
        lines.append(f"{a['appointment_time']} — <b>{a['client_name']}</b>{u}\nМастер: {a['master_name']}  Услуга: {rv(a,'service_title')} ({rv(a,'service_price')})")
    return "\n".join(lines)

def my_appts_text(appts) -> str:
    if not appts:
        return "<b>Мои записи</b>\n\nАктивных записей пока нет."
    lines = ["<b>Мои записи</b>", ""]
    for a in appts:
        lines.append(f"• {format_date(a['appointment_date'])} {a['appointment_time']} — мастер {a['master_name']}, {rv(a,'service_title')}")
    return "\n".join(lines)


async def safe_edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if cb.message is None:
        return
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def build_router(db: Database, bot: Bot, admin_ids: set[int]) -> Router:
    router = Router()
    url = contact_url(admin_ids)

    def dash_text(uid: int) -> str | None:
        master = db.get_master_by_tg_id(uid)
        if uid in admin_ids:
            return dashboard_text("Админка: все ближайшие записи", db.upcoming_appointments(limit=50))
        if master is not None:
            return dashboard_text(f"Админка мастера: {master['name']}", db.upcoming_appointments(master_id=master["id"], limit=30))
        return None

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        db.upsert_user(message)
        sent = await message.answer(menu_text(), reply_markup=menu_kb(url))
        if message.from_user:
            db.update_draft(message.from_user.id, message_id=sent.message_id)

    @router.message(Command("master"))
    async def master_cmd(message: Message) -> None:
        if not message.from_user: return
        text = dash_text(message.from_user.id)
        if text is None: await message.answer("Эта команда доступна только мастерам."); return
        await message.answer(text, reply_markup=dashboard_kb())

    @router.callback_query(F.data == "menu")
    async def show_menu(cb: CallbackQuery) -> None:
        await cb.answer(); await safe_edit(cb, menu_text(), menu_kb(url))

    @router.callback_query(F.data == "price")
    async def show_prices(cb: CallbackQuery) -> None:
        await cb.answer(); await safe_edit(cb, prices_text(db.get_prices()), price_kb(url))

    @router.callback_query(F.data == "my")
    async def show_my(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user: return
        await safe_edit(cb, my_appts_text(db.upcoming_appointments(user_tg_id=cb.from_user.id)), back_kb(url))

    @router.callback_query(F.data == cd("book", "start"))
    async def choose_date(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user: return
        db.reset_draft(cb.from_user.id, cb.message.message_id if cb.message else None)
        await safe_edit(cb, "<b>Выберите дату</b>", date_kb())

    @router.callback_query(F.data.startswith("date:"))
    async def choose_time(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user or not cb.data: return
        _, sel_date = cb.data.split(":", 1)
        db.update_draft(cb.from_user.id, selected_date=sel_date, selected_time=None, selected_master_id=None, selected_service_id=None)
        await safe_edit(cb, f"<b>Дата: {format_date(sel_date)}</b>\n\nВыберите время.", time_kb())

    @router.callback_query(F.data.startswith("time:"))
    async def choose_master(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user or not cb.data: return
        _, raw = cb.data.split(":", 1)
        t = f"{raw[:2]}:{raw[2:]}"
        draft = db.get_draft(cb.from_user.id)
        if draft is None or not draft["selected_date"]:
            await safe_edit(cb, "Давайте начнем заново и выберем дату.", date_kb()); return
        db.update_draft(cb.from_user.id, selected_time=t, selected_master_id=None, selected_service_id=None)
        masters = db.get_available_masters(draft["selected_date"], t)
        if not masters:
            await safe_edit(cb, "На это время все мастера заняты. Выберите другое время.", time_kb()); return
        await safe_edit(cb, f"<b>{format_date(draft['selected_date'])}, {t}</b>\n\nВыберите мастера.", masters_kb(masters))

    @router.callback_query(F.data == cd("back", "time"))
    async def back_time(cb: CallbackQuery) -> None:
        await cb.answer()
        draft = db.get_draft(cb.from_user.id)
        dt = format_date(draft["selected_date"]) if draft and draft["selected_date"] else "выбранная дата"
        await safe_edit(cb, f"<b>Дата: {dt}</b>\n\nВыберите время.", time_kb())

    @router.callback_query(F.data == cd("back", "master"))
    async def back_master(cb: CallbackQuery) -> None:
        await cb.answer()
        draft = db.get_draft(cb.from_user.id)
        if not draft or not draft["selected_date"] or not draft["selected_time"]:
            await safe_edit(cb, "Давайте начнем заново и выберем дату.", date_kb()); return
        masters = db.get_available_masters(draft["selected_date"], draft["selected_time"])
        await safe_edit(cb, f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\n\nВыберите мастера.", masters_kb(masters))

    @router.callback_query(F.data.startswith("master:"))
    async def choose_service(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user or not cb.data: return
        mid = int(cb.data.split(":", 1)[1])
        db.update_draft(cb.from_user.id, selected_master_id=mid, selected_service_id=None)
        draft, master = db.get_draft(cb.from_user.id), db.get_master(mid)
        if not draft or not master:
            await safe_edit(cb, "Не удалось собрать запись. Попробуйте еще раз.", menu_kb(url)); return
        await safe_edit(cb, f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\nМастер: <b>{master['name']}</b>\n\nВыберите услугу.", services_kb(db.get_prices()))

    @router.callback_query(F.data == cd("back", "service"))
    async def back_service(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user: return
        draft = db.get_draft(cb.from_user.id)
        if not draft or not draft["selected_date"] or not draft["selected_time"] or not draft["selected_master_id"]:
            await safe_edit(cb, "Давайте начнем заново и выберем дату.", date_kb()); return
        master = db.get_master(draft["selected_master_id"])
        if not master:
            await safe_edit(cb, "Мастер не найден. Выберите мастера заново.", masters_kb(db.get_masters())); return
        await safe_edit(cb, f"<b>{format_date(draft['selected_date'])}, {draft['selected_time']}</b>\nМастер: <b>{master['name']}</b>\n\nВыберите услугу.", services_kb(db.get_prices()))

    @router.callback_query(F.data.startswith("service:"))
    async def show_confirm(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user or not cb.data: return
        sid = int(cb.data.split(":", 1)[1])
        db.update_draft(cb.from_user.id, selected_service_id=sid)
        draft, svc = db.get_draft(cb.from_user.id), db.get_service(sid)
        if not draft or not svc or not draft["selected_master_id"]:
            await safe_edit(cb, "Не удалось собрать запись. Попробуйте еще раз.", menu_kb(url)); return
        master = db.get_master(draft["selected_master_id"])
        if not master:
            await safe_edit(cb, "Мастер не найден. Выберите мастера заново.", masters_kb(db.get_masters())); return
        await safe_edit(cb, summary_text(draft, master, svc), confirm_kb())

    @router.callback_query(F.data == "confirm")
    async def confirm_booking(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user: return
        try:
            appt = db.create_appointment(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
        except sqlite3.IntegrityError:
            await safe_edit(cb, "Этот слот только что заняли. Выберите другое время или мастера.", time_kb()); return
        except ValueError:
            await safe_edit(cb, "Давайте начнем заново и выберем дату.", date_kb()); return
        await safe_edit(cb, appointment_text(appt), cancel_kb(appt["id"], url))
        if appt["master_tg_id"]:
            await bot.send_message(appt["master_tg_id"], master_notify_text(appt), reply_markup=dashboard_kb())

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_booking(cb: CallbackQuery) -> None:
        await cb.answer()
        if not cb.from_user or not cb.data: return
        appt = db.cancel_appointment(int(cb.data.split(":", 1)[1]), cb.from_user.id)
        if appt is None:
            await safe_edit(cb, "Не нашел активную запись для отмены.", menu_kb(url)); return
        await safe_edit(cb, f"<b>Запись отменена</b>\n\n{format_date(appt['appointment_date'])} в {appt['appointment_time']}, мастер {appt['master_name']}.", menu_kb(url))
        if appt["master_tg_id"]:
            await bot.send_message(appt["master_tg_id"], cancel_notify_text(appt))

    @router.callback_query(F.data == cd("admin", "dashboard"))
    async def refresh_dash(cb: CallbackQuery) -> None:
        await cb.answer("Обновлено")
        if not cb.from_user: return
        text = dash_text(cb.from_user.id)
        if text is None: await safe_edit(cb, "Эта админка доступна только мастерам."); return
        await safe_edit(cb, text, dashboard_kb())

    return router


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    db = Database(os.getenv("DB_PATH", "salon.sqlite3"))
    db.seed(parse_master_ids(os.getenv("MASTER_IDS", "")))
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(db, bot, parse_admin_ids(os.getenv("ADMIN_IDS", ""))))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
