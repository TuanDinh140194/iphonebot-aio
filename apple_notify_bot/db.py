from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import APPLE_IPHONE_BUY_URL, model_label

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "iphone_aio.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class Database:
    def __init__(self, path: Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS task_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            retailer TEXT NOT NULL DEFAULT 'apple',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            pickup_first_name TEXT NOT NULL DEFAULT '',
            pickup_last_name TEXT NOT NULL DEFAULT '',
            address1 TEXT NOT NULL DEFAULT '',
            address2 TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '',
            postal_code TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT 'US',
            payment_last4 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER REFERENCES task_groups(id) ON DELETE SET NULL,
            profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
            watch_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('monitor', 'instore_checkout')),
            model_code TEXT NOT NULL,
            product_url TEXT NOT NULL DEFAULT '',
            zipcode TEXT NOT NULL,
            stores_json TEXT NOT NULL DEFAULT '[]',
            poll_min INTEGER NOT NULL DEFAULT 55,
            poll_max INTEGER NOT NULL DEFAULT 65,
            proxy_url TEXT,
            auto_prepare_browser INTEGER NOT NULL DEFAULT 0,
            auto_add_to_bag INTEGER NOT NULL DEFAULT 0,
            auto_checkout INTEGER NOT NULL DEFAULT 0,
            prepared_url TEXT,
            prepared_store_id TEXT,
            prepared_store_name TEXT,
            last_checkout_run_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'STOPPED',
            status_message TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT,
            last_stock_at TEXT,
            stock_hits INTEGER NOT NULL DEFAULT 0,
            carted_count INTEGER NOT NULL DEFAULT 0,
            checkout_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS checkout_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            monitor_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            model_code TEXT NOT NULL,
            store_id TEXT,
            store_name TEXT,
            checkout_url TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            level TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_enabled_mode ON tasks(enabled, mode);
        CREATE INDEX IF NOT EXISTS idx_tasks_watch_task_id ON tasks(watch_task_id);
        CREATE INDEX IF NOT EXISTS idx_checkout_runs_task_id ON checkout_runs(task_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(id DESC);
        """
        with self._lock:
            self._connection.executescript(schema)
            self._migrate_task_columns()
            self._migrate_profile_columns()
            self._connection.commit()

    def _migrate_task_columns(self) -> None:
        """Add v8 columns to an existing v7 SQLite database in place."""
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        additions = {
            "auto_prepare_browser": "INTEGER NOT NULL DEFAULT 0",
            "auto_add_to_bag": "INTEGER NOT NULL DEFAULT 0",
            "auto_checkout": "INTEGER NOT NULL DEFAULT 0",
            "prepared_url": "TEXT",
            "prepared_store_id": "TEXT",
            "prepared_store_name": "TEXT",
            "last_checkout_run_id": "INTEGER",
        }
        for column, ddl in additions.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")

    def _migrate_profile_columns(self) -> None:
        """Add v8.1 checkout/contact columns to existing profile rows."""
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(profiles)").fetchall()
        }
        additions = {
            "pickup_first_name": "TEXT NOT NULL DEFAULT ''",
            "pickup_last_name": "TEXT NOT NULL DEFAULT ''",
            "address1": "TEXT NOT NULL DEFAULT ''",
            "address2": "TEXT NOT NULL DEFAULT ''",
            "city": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT ''",
            "postal_code": "TEXT NOT NULL DEFAULT ''",
            "country": "TEXT NOT NULL DEFAULT 'US'",
            "payment_last4": "TEXT NOT NULL DEFAULT ''",
        }
        for column, ddl in additions.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE profiles ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _task_from_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["stores"] = _decode_json(item.pop("stores_json", "[]"), [])
        item["enabled"] = bool(item["enabled"])
        item["auto_prepare_browser"] = bool(item.get("auto_prepare_browser", 0))
        item["auto_add_to_bag"] = bool(item.get("auto_add_to_bag", 0))
        item["auto_checkout"] = bool(item.get("auto_checkout", 0))
        return item

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["metadata"] = _decode_json(item.pop("metadata_json", "{}"), {})
        return item

    @staticmethod
    def _checkout_run_from_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _decode_json(item.pop("metadata_json", "{}"), {})
        return item

    def ensure_default_group(self, name: str = "Apple iPhone") -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM task_groups ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                return int(row["id"])
            cur = self._connection.execute(
                "INSERT INTO task_groups(name, retailer, created_at) VALUES (?, 'apple', ?)",
                (name, utc_now()),
            )
            self._connection.commit()
            return int(cur.lastrowid)

    def list_groups(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_groups ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_profiles(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM profiles ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_profile(self, payload: dict) -> dict:
        now = utc_now()
        with self._lock:
            cur = self._connection.execute(
                """
                INSERT INTO profiles(
                    name, first_name, last_name, email, phone,
                    pickup_first_name, pickup_last_name,
                    address1, address2, city, state, postal_code, country,
                    payment_last4, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    payload["name"].strip(),
                    payload.get("first_name", "").strip(),
                    payload.get("last_name", "").strip(),
                    payload.get("email", "").strip(),
                    payload.get("phone", "").strip(),
                    payload.get("pickup_first_name", "").strip(),
                    payload.get("pickup_last_name", "").strip(),
                    payload.get("address1", "").strip(),
                    payload.get("address2", "").strip(),
                    payload.get("city", "").strip(),
                    payload.get("state", "").strip(),
                    payload.get("postal_code", "").strip(),
                    (payload.get("country") or "US").strip(),
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return self.get_profile(int(cur.lastrowid))

    def get_profile(self, profile_id: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_profile(self, profile_id: int, changes: dict) -> dict | None:
        allowed = {
            "name", "first_name", "last_name", "email", "phone",
            "pickup_first_name", "pickup_last_name", "address1", "address2",
            "city", "state", "postal_code", "country", "payment_last4",
        }
        assignments = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            assignments.append(f"{key}=?")
            values.append((value or "").strip() if isinstance(value, str) else value)
        if not assignments:
            return self.get_profile(profile_id)
        assignments.append("updated_at=?")
        values.append(utc_now())
        values.append(profile_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE profiles SET {', '.join(assignments)} WHERE id=?",
                tuple(values),
            )
            self._connection.commit()
        return self.get_profile(profile_id)

    def set_profile_payment_last4(self, profile_id: int, last4: str) -> dict | None:
        return self.update_profile(profile_id, {"payment_last4": last4})

    def delete_profile(self, profile_id: int) -> bool:
        with self._lock:
            cur = self._connection.execute(
                "DELETE FROM profiles WHERE id=?", (profile_id,)
            )
            self._connection.commit()
            return cur.rowcount > 0

    def create_task(self, payload: dict) -> dict:
        now = utc_now()
        group_id = payload.get("group_id") or self.ensure_default_group()
        status = (
            "MONITORING"
            if payload.get("enabled", True) and payload["mode"] == "monitor"
            else (
                "WAITING_FOR_MONITOR_PING"
                if payload.get("enabled", True) and payload["mode"] == "instore_checkout"
                else "STOPPED"
            )
        )
        with self._lock:
            cur = self._connection.execute(
                """
                INSERT INTO tasks(
                    group_id, profile_id, watch_task_id, name, mode, model_code,
                    product_url, zipcode, stores_json, poll_min, poll_max, proxy_url,
                    auto_prepare_browser, auto_add_to_bag, auto_checkout,
                    enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    payload.get("profile_id"),
                    payload.get("watch_task_id"),
                    payload["name"].strip(),
                    payload["mode"],
                    payload["model_code"].strip(),
                    payload.get("product_url", "").strip(),
                    payload["zipcode"].strip(),
                    _json(payload.get("stores", [])),
                    int(payload.get("poll_min", 55)),
                    int(payload.get("poll_max", 65)),
                    payload.get("proxy_url") or None,
                    1 if payload.get("auto_prepare_browser", False) else 0,
                    1 if payload.get("auto_add_to_bag", False) else 0,
                    1 if payload.get("auto_checkout", False) else 0,
                    1 if payload.get("enabled", True) else 0,
                    status,
                    now,
                    now,
                ),
            )
            task_id = int(cur.lastrowid)
            self._connection.commit()
        self.add_event(task_id, "INFO", "TASK_CREATED", f"Task created: {payload['name']}")
        return self.get_task(task_id)

    def create_pair(self, payload: dict) -> dict:
        # Browser checkout flags only make sense on the checkout watcher.
        monitor_payload = {
            **payload,
            "mode": "monitor",
            "auto_prepare_browser": False,
            "auto_add_to_bag": False,
            "auto_checkout": False,
        }
        monitor = self.create_task(monitor_payload)
        checkout = self.create_task(
            {
                **payload,
                "name": payload.get("checkout_name") or f"{payload['name']} Checkout",
                "mode": "instore_checkout",
                "watch_task_id": monitor["id"],
            }
        )
        return {"monitor": monitor, "checkout": checkout}

    def get_task(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT t.*, p.name AS profile_name
                FROM tasks t
                LEFT JOIN profiles p ON p.id=t.profile_id
                WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
            return self._task_from_row(row)

    def list_tasks(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT t.*, p.name AS profile_name
                FROM tasks t
                LEFT JOIN profiles p ON p.id=t.profile_id
                ORDER BY t.id DESC
                """
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def list_enabled_monitor_tasks(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT t.*, p.name AS profile_name
                FROM tasks t
                LEFT JOIN profiles p ON p.id=t.profile_id
                WHERE t.enabled=1 AND t.mode='monitor'
                ORDER BY t.id
                """
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def list_watchers(self, monitor_task_id: int) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT t.*, p.name AS profile_name
                FROM tasks t
                LEFT JOIN profiles p ON p.id=t.profile_id
                WHERE t.enabled=1 AND t.mode='instore_checkout' AND t.watch_task_id=?
                ORDER BY t.id
                """,
                (monitor_task_id,),
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def update_task(self, task_id: int, changes: dict) -> dict | None:
        allowed = {
            "group_id", "profile_id", "watch_task_id", "name", "model_code",
            "product_url", "zipcode", "poll_min", "poll_max", "proxy_url",
            "auto_prepare_browser", "auto_add_to_bag", "auto_checkout", "prepared_url",
            "prepared_store_id", "prepared_store_name", "last_checkout_run_id",
            "enabled", "status", "status_message", "last_checked_at",
            "last_stock_at", "stock_hits", "carted_count", "checkout_count",
        }
        values = []
        assignments = []
        if "stores" in changes:
            assignments.append("stores_json=?")
            values.append(_json(changes["stores"]))
        for key, value in changes.items():
            if key == "stores" or key not in allowed:
                continue
            if key in {"enabled", "auto_prepare_browser", "auto_add_to_bag", "auto_checkout"}:
                value = 1 if value else 0
            assignments.append(f"{key}=?")
            values.append(value)
        if not assignments:
            return self.get_task(task_id)
        assignments.append("updated_at=?")
        values.append(utc_now())
        values.append(task_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?",
                tuple(values),
            )
            self._connection.commit()
        return self.get_task(task_id)

    def set_task_enabled(self, task_id: int, enabled: bool) -> dict | None:
        task = self.get_task(task_id)
        if not task:
            return None
        status = "STOPPED"
        if enabled:
            status = "MONITORING" if task["mode"] == "monitor" else "WAITING_FOR_MONITOR_PING"
        updated = self.update_task(
            task_id,
            {"enabled": enabled, "status": status, "status_message": ""},
        )
        self.add_event(task_id, "INFO", "TASK_STARTED" if enabled else "TASK_STOPPED", status)
        return updated

    def enable_full_checkout_for_existing_watchers(self) -> int:
        """Upgrade v8 watchers that already had Browser Assist + Add to Bag enabled."""
        with self._lock:
            cur = self._connection.execute(
                """
                UPDATE tasks
                SET auto_checkout=1, updated_at=?
                WHERE mode='instore_checkout'
                  AND auto_prepare_browser=1
                  AND auto_add_to_bag=1
                  AND profile_id IS NOT NULL
                  AND auto_checkout=0
                """,
                (utc_now(),),
            )
            self._connection.commit()
            return int(cur.rowcount or 0)

    def set_all_enabled(self, enabled: bool) -> None:
        with self._lock:
            rows = self._connection.execute("SELECT id, mode FROM tasks").fetchall()
            now = utc_now()
            for row in rows:
                status = "STOPPED"
                if enabled:
                    status = "MONITORING" if row["mode"] == "monitor" else "WAITING_FOR_MONITOR_PING"
                self._connection.execute(
                    "UPDATE tasks SET enabled=?, status=?, status_message='', updated_at=? WHERE id=?",
                    (1 if enabled else 0, status, now, row["id"]),
                )
            self._connection.commit()

    def delete_task(self, task_id: int) -> bool:
        with self._lock:
            cur = self._connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._connection.commit()
            return cur.rowcount > 0

    def increment(self, task_id: int, field: str, amount: int = 1) -> dict | None:
        if field not in {"stock_hits", "carted_count", "checkout_count"}:
            raise ValueError("Unsupported counter")
        with self._lock:
            self._connection.execute(
                f"UPDATE tasks SET {field}={field}+?, updated_at=? WHERE id=?",
                (amount, utc_now(), task_id),
            )
            self._connection.commit()
        return self.get_task(task_id)

    # ---- Checkout runs -------------------------------------------------
    def create_checkout_run(
        self,
        task_id: int,
        monitor_task_id: int | None,
        model_code: str,
        checkout_url: str,
        store: dict | None,
        status: str = "CHECKOUT_READY",
        metadata: dict | None = None,
    ) -> dict:
        now = utc_now()
        store = store or {}
        with self._lock:
            cur = self._connection.execute(
                """
                INSERT INTO checkout_runs(
                    task_id, monitor_task_id, status, model_code,
                    store_id, store_name, checkout_url, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    monitor_task_id,
                    status,
                    model_code,
                    store.get("store_id"),
                    store.get("store_name"),
                    checkout_url,
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )
            run_id = int(cur.lastrowid)
            self._connection.commit()
        return self.get_checkout_run(run_id)

    def get_checkout_run(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM checkout_runs WHERE id=?", (run_id,)
            ).fetchone()
            return self._checkout_run_from_row(row)

    def latest_checkout_run(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM checkout_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._checkout_run_from_row(row)

    def update_checkout_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        current = self.get_checkout_run(run_id)
        if not current:
            return None
        assignments = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if metadata is not None:
            merged = {**current.get("metadata", {}), **metadata}
            assignments.append("metadata_json=?")
            values.append(_json(merged))
        if not assignments:
            return current
        assignments.append("updated_at=?")
        values.append(utc_now())
        values.append(run_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE checkout_runs SET {', '.join(assignments)} WHERE id=?",
                tuple(values),
            )
            self._connection.commit()
        return self.get_checkout_run(run_id)

    def list_checkout_runs(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT r.*, t.name AS task_name
                FROM checkout_runs r
                LEFT JOIN tasks t ON t.id=r.task_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._checkout_run_from_row(row) for row in rows]

    # ---- Event log -----------------------------------------------------
    def add_event(
        self,
        task_id: int | None,
        level: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> dict:
        created_at = utc_now()
        with self._lock:
            cur = self._connection.execute(
                """
                INSERT INTO events(task_id, level, event_type, message, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, level, event_type, message, _json(metadata or {}), created_at),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM events WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return self._event_from_row(row)

    def list_events(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT e.*, t.name AS task_name
                FROM events e
                LEFT JOIN tasks t ON t.id=e.task_id
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def overview(self) -> dict:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS total_tasks,
                    SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS running_tasks,
                    COALESCE(SUM(stock_hits), 0) AS stock_hits,
                    COALESCE(SUM(carted_count), 0) AS carted,
                    COALESCE(SUM(checkout_count), 0) AS checkouts,
                    SUM(CASE WHEN status IN ('ACTION_REQUIRED','CHECKOUT_READY') THEN 1 ELSE 0 END) AS action_required,
                    SUM(CASE WHEN status='CHECKOUT_READY' THEN 1 ELSE 0 END) AS checkout_ready,
                    SUM(CASE WHEN status='READY_TO_PLACE_ORDER' THEN 1 ELSE 0 END) AS ready_to_place_order
                FROM tasks
                """
            ).fetchone()
            return {key: int(row[key] or 0) for key in row.keys()}

    def migrate_legacy_config(self, config: dict) -> None:
        """Turn the old flat model list into task pairs on the first launch."""
        with self._lock:
            existing = self._connection.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        if existing:
            return

        models = config.get("models", [])
        if not models:
            return

        group_id = self.ensure_default_group("Apple iPhone 18")
        poll_min, poll_max = config.get("poll_interval_seconds", [55, 65])
        zipcode = config.get("zipcode", "97220")
        stores = config.get("stores", [])
        proxy_url = config.get("http_proxy")
        assistant = config.get("checkout_assistant", {})

        for code in models:
            name = model_label(code)
            self.create_pair(
                {
                    "group_id": group_id,
                    "name": name,
                    "model_code": code,
                    "product_url": APPLE_IPHONE_BUY_URL,
                    "zipcode": zipcode,
                    "stores": stores,
                    "poll_min": poll_min,
                    "poll_max": poll_max,
                    "proxy_url": proxy_url,
                    "auto_prepare_browser": bool(assistant.get("auto_launch_on_stock", False)),
                    "auto_add_to_bag": bool(assistant.get("auto_add_to_bag", False)),
                    "auto_checkout": bool(assistant.get("auto_checkout", True)),
                    "enabled": True,
                }
            )

        self.add_event(None, "INFO", "MIGRATION", f"Imported {len(models)} legacy models into task pairs")
