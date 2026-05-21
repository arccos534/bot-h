import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from aiogram.types import Message


@dataclass(frozen=True)
class MasterConfig:
    tg_id: int
    name: str


DEFAULT_PRICES = [
    ("Мужская стрижка", "900 ₽"),
    ("Женская стрижка", "1 500 ₽"),
    ("Детская стрижка", "700 ₽"),
    ("Стрижка + борода", "1 300 ₽"),
    ("Укладка", "1 200 ₽"),
    ("Окрашивание", "2 500 ₽"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
    username TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS masters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE,
    name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE, price TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_drafts (
    user_tg_id INTEGER PRIMARY KEY, selected_date TEXT, selected_time TEXT,
    selected_master_id INTEGER, selected_service_id INTEGER, message_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_tg_id INTEGER NOT NULL, client_name TEXT NOT NULL, client_username TEXT,
    master_id INTEGER NOT NULL, service_id INTEGER, service_title TEXT, service_price TEXT,
    appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, cancelled_at TEXT,
    FOREIGN KEY(master_id) REFERENCES masters(id),
    FOREIGN KEY(service_id) REFERENCES services(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_slot
ON appointments(master_id, appointment_date, appointment_time) WHERE status = 'confirmed';
"""

_APPT_JOIN = (
    "SELECT appointments.*, masters.name AS master_name, masters.tg_id AS master_tg_id "
    "FROM appointments JOIN masters ON masters.id = appointments.master_id "
)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with closing(self.connect()) as c:
            return c.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(self.connect()) as c:
            return list(c.execute(sql, params))

    def _init(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(_SCHEMA)
            for table, col, defn in [
                ("booking_drafts", "selected_service_id", "INTEGER"),
                ("appointments", "service_id", "INTEGER"),
                ("appointments", "service_title", "TEXT"),
                ("appointments", "service_price", "TEXT"),
            ]:
                cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            conn.commit()

    def seed(self, masters: Iterable[MasterConfig]) -> None:
        with closing(self.connect()) as conn:
            for m in masters:
                conn.execute(
                    "INSERT INTO masters(tg_id, name) VALUES(?,?) "
                    "ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name, active=1",
                    (m.tg_id, m.name),
                )
            if not list(conn.execute("SELECT id FROM masters LIMIT 1")):
                for name in ("Анна", "Мария", "Егор"):
                    conn.execute("INSERT INTO masters(name) VALUES(?)", (name,))
            for title, price in DEFAULT_PRICES:
                conn.execute(
                    "INSERT INTO services(title, price) VALUES(?,?) "
                    "ON CONFLICT(title) DO UPDATE SET price=excluded.price",
                    (title, price),
                )
            conn.commit()

    def upsert_user(self, message: Message) -> None:
        u = message.from_user
        if u is None:
            return
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO users(tg_id, full_name, username) VALUES(?,?,?) "
                "ON CONFLICT(tg_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username",
                (u.id, u.full_name, u.username),
            )
            conn.commit()

    def reset_draft(self, user_tg_id: int, message_id: int | None = None) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO booking_drafts(user_tg_id, message_id) VALUES(?,?) "
                "ON CONFLICT(user_tg_id) DO UPDATE SET "
                "selected_date=NULL, selected_time=NULL, selected_master_id=NULL, "
                "selected_service_id=NULL, "
                "message_id=COALESCE(excluded.message_id, booking_drafts.message_id), "
                "updated_at=CURRENT_TIMESTAMP",
                (user_tg_id, message_id),
            )
            conn.commit()

    def update_draft(self, user_tg_id: int, **values: object) -> None:
        allowed = {"selected_date", "selected_time", "selected_master_id", "selected_service_id", "message_id"}
        cols = [k for k in values if k in allowed]
        if not cols:
            return
        params = [values[k] for k in cols] + [user_tg_id]
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO booking_drafts(user_tg_id) VALUES(?) ON CONFLICT(user_tg_id) DO NOTHING", (user_tg_id,))
            conn.execute(f"UPDATE booking_drafts SET {', '.join(f'{k}=?' for k in cols)}, updated_at=CURRENT_TIMESTAMP WHERE user_tg_id=?", params)
            conn.commit()

    def get_draft(self, uid: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM booking_drafts WHERE user_tg_id=?", (uid,))

    def get_prices(self) -> list[sqlite3.Row]:              return self._all("SELECT id,title,price FROM services ORDER BY id")
    def get_service(self, sid: int) -> sqlite3.Row | None:  return self._one("SELECT id,title,price FROM services WHERE id=?", (sid,))
    def get_masters(self) -> list[sqlite3.Row]:             return self._all("SELECT * FROM masters WHERE active=1 ORDER BY id")
    def get_master(self, mid: int) -> sqlite3.Row | None:   return self._one("SELECT * FROM masters WHERE id=?", (mid,))
    def get_master_by_tg_id(self, tg: int) -> sqlite3.Row | None: return self._one("SELECT * FROM masters WHERE tg_id=?", (tg,))

    def get_available_masters(self, appt_date: str, appt_time: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT masters.* FROM masters WHERE active=1 AND id NOT IN ("
            "SELECT master_id FROM appointments WHERE appointment_date=? AND appointment_time=? AND status='confirmed'"
            ") ORDER BY id",
            (appt_date, appt_time),
        )

    def get_appointment(self, aid: int) -> sqlite3.Row | None:
        return self._one(_APPT_JOIN + "WHERE appointments.id=?", (aid,))

    def create_appointment(self, user_tg_id: int, client_name: str, client_username: str | None) -> sqlite3.Row:
        with closing(self.connect()) as conn:
            draft = conn.execute("SELECT * FROM booking_drafts WHERE user_tg_id=?", (user_tg_id,)).fetchone()
            if draft is None or not all([draft["selected_date"], draft["selected_time"], draft["selected_master_id"], draft["selected_service_id"]]):
                raise ValueError("Черновик записи не заполнен.")
            svc = conn.execute("SELECT id,title,price FROM services WHERE id=?", (draft["selected_service_id"],)).fetchone()
            if svc is None:
                raise ValueError("Выбранная услуга не найдена.")
            cur = conn.execute(
                "INSERT INTO appointments(user_tg_id,client_name,client_username,master_id,service_id,service_title,service_price,appointment_date,appointment_time) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (user_tg_id, client_name, client_username, draft["selected_master_id"], svc["id"], svc["title"], svc["price"], draft["selected_date"], draft["selected_time"]),
            )
            aid = cur.lastrowid
            conn.commit()
            return conn.execute(_APPT_JOIN + "WHERE appointments.id=?", (aid,)).fetchone()

    def cancel_appointment(self, aid: int, user_tg_id: int | None = None) -> sqlite3.Row | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT user_tg_id FROM appointments WHERE id=? AND status='confirmed'", (aid,)).fetchone()
            if row is None or (user_tg_id is not None and row["user_tg_id"] != user_tg_id):
                return None
            conn.execute("UPDATE appointments SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
            conn.commit()
        return self.get_appointment(aid)

    def upcoming_appointments(self, *, master_id: int | None = None, user_tg_id: int | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        today = date.today().isoformat()
        where = ["appointment_date>=?", "status='confirmed'"]
        params: list = [today]
        if master_id is not None:
            where.append("master_id=?"); params.append(master_id)
        if user_tg_id is not None:
            where.append("user_tg_id=?"); params.append(user_tg_id)
        sql = (
            "SELECT appointments.*, masters.name AS master_name "
            f"FROM appointments JOIN masters ON masters.id=appointments.master_id "
            f"WHERE {' AND '.join(where)} ORDER BY appointment_date, appointment_time"
        )
        if limit is not None:
            sql += " LIMIT ?"; params.append(limit)
        return self._all(sql, tuple(params))
