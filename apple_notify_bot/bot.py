from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import logging
import random
import time

import aiohttp

from .catalog import APPLE_IPHONE_BUY_URL, build_checkout_start_url, build_preconfigured_url, model_details
from .checkout_assistant import CheckoutAssistant, CheckoutAssistantUnavailable
from .db import Database
from .notifier import TelegramNotifier
from .payment_vault import PaymentVaultUnavailable, get_payment
from .scraper import ApiError, extract_available_stores, fetch_availability, log_proxy_details

log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppleStockBot:
    """Apple inventory task engine with the v10 checkout-assist workflow.

    Monitor tasks detect pickup stock. Linked checkout tasks are immediately
    prepared with the exact product variant and detected store. A headed
    Playwright helper can select product options, cart, fill checkout/contact,
    and fill payment from the OS credential vault, then stop at order review.

    The engine does not bypass CAPTCHA/verification, evade purchase limits, or
    click Place Order/Pay/Buy Now. Final purchase confirmation remains under the
    user's control.
    """

    def __init__(self, config: dict, db: Database | None = None):
        self.config = config
        self.db = db or Database()
        self.db.migrate_legacy_config(config)
        if bool(config.get("checkout_assistant", {}).get("auto_checkout", True)):
            upgraded = self.db.enable_full_checkout_for_existing_watchers()
            if upgraded:
                log.info("Enabled full checkout assist for %s existing v8 watcher(s)", upgraded)

        self.notifier = TelegramNotifier(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )
        self.checkout_assistant = CheckoutAssistant(config)

        self.running = asyncio.Event()
        self.running.set()
        self.last_checked_at: str | None = None
        self.last_status = "starting"

        self._next_due: dict[int, float] = {}
        self._stock_state: dict[int, bool] = {}
        self._rate_limit_attempts: dict[int, int] = defaultdict(int)
        self._assistant_jobs: dict[int, asyncio.Task] = {}

    def apply_config(self, config: dict) -> None:
        self.config = config

    async def shutdown(self) -> None:
        for job in self._assistant_jobs.values():
            if not job.done():
                job.cancel()
        await self.checkout_assistant.close()

    def pause(self) -> None:
        self.running.clear()
        self.last_status = "paused"
        self.db.add_event(None, "INFO", "ENGINE_PAUSED", "All monitor polling paused")
        log.info("Bot engine paused.")

    def resume(self) -> None:
        self.running.set()
        self.last_status = "running"
        self.db.add_event(None, "INFO", "ENGINE_RESUMED", "All enabled monitors resumed")
        log.info("Bot engine resumed.")

    def _schedule_normal(self, task: dict) -> None:
        low = max(5, int(task.get("poll_min") or 55))
        high = max(low, int(task.get("poll_max") or 65))
        self._next_due[task["id"]] = time.monotonic() + random.uniform(low, high)
        self._rate_limit_attempts[task["id"]] = 0

    def _schedule_backoff(self, task: dict, retry_after: str | None = None) -> int:
        attempt = self._rate_limit_attempts[task["id"]]
        self._rate_limit_attempts[task["id"]] = attempt + 1

        if retry_after:
            try:
                seconds = max(15, min(1800, int(retry_after)))
            except (TypeError, ValueError):
                seconds = 0
        else:
            seconds = 0

        if not seconds:
            seconds = min(900, 60 * (2 ** min(attempt, 4)))

        seconds += random.randint(0, 15)
        self._next_due[task["id"]] = time.monotonic() + seconds
        return seconds

    async def _notify_stock(self, task: dict, stores: list[dict]) -> None:
        store_lines = []
        for store in stores:
            label = store.get("store_name") or store.get("store_id") or "Apple Store"
            quote = store.get("store_location", "")
            suffix = f" — {quote}" if quote else ""
            store_lines.append(f"• {label}{suffix}")

        message = (
            f"📱 STOCK FOUND\n"
            f"{task['name']} ({task['model_code']})\n"
            + "\n".join(store_lines)
        )
        await self.notifier.send(message)

    @staticmethod
    def _choose_store(watcher: dict, stores: list[dict]) -> dict | None:
        if not stores:
            return None
        wanted = watcher.get("stores") or []
        if wanted:
            by_id = {store.get("store_id"): store for store in stores}
            for store_id in wanted:
                if store_id in by_id:
                    return by_id[store_id]
        return stores[0]

    async def _prepare_checkout_watcher(
        self,
        monitor_task: dict,
        watcher: dict,
        stores: list[dict],
    ) -> dict:
        store = self._choose_store(watcher, stores)
        product_base = watcher.get("product_url") or monitor_task.get("product_url") or APPLE_IPHONE_BUY_URL
        configured_url = build_preconfigured_url(watcher["model_code"], product_base)
        checkout_url = build_checkout_start_url(watcher["model_code"], product_base)
        details = model_details(watcher["model_code"]) or {}
        store_name = (store or {}).get("store_name") or (store or {}).get("store_id") or "detected Apple Store"

        run = self.db.create_checkout_run(
            task_id=watcher["id"],
            monitor_task_id=monitor_task["id"],
            model_code=watcher["model_code"],
            checkout_url=checkout_url,
            store=store,
            status="CHECKOUT_READY",
            metadata={"stores": stores, "model": details, "configured_url": configured_url, "checkout_start_url": checkout_url},
        )

        self.db.update_task(
            watcher["id"],
            {
                "status": "CHECKOUT_READY",
                "status_message": f"Prepared {details.get('label', watcher['model_code'])} for pickup at {store_name}",
                "last_stock_at": utc_now(),
                "prepared_url": checkout_url,
                "prepared_store_id": (store or {}).get("store_id"),
                "prepared_store_name": (store or {}).get("store_name"),
                "last_checkout_run_id": run["id"],
            },
        )
        self.db.add_event(
            watcher["id"],
            "SUCCESS",
            "CHECKOUT_PREPARED",
            f"Exact Apple configuration prepared for {store_name}",
            {"checkout_url": checkout_url, "configured_url": configured_url, "store": store, "run_id": run["id"]},
        )

        profile_text = f"Profile: {watcher['profile_name']}\n" if watcher.get("profile_name") else ""
        await self.notifier.send(
            "⚡ CHECKOUT READY\n"
            f"{watcher['name']}\n"
            f"{profile_text}"
            f"Pickup: {store_name}\n"
            f"Exact product: {details.get('label', watcher['model_code'])}\n"
            f"Open prepared Apple: {checkout_url}"
        )

        if watcher.get("auto_prepare_browser"):
            self.launch_checkout_assistant_background(watcher["id"], run["id"])
        else:
            self.db.add_event(
                watcher["id"],
                "WARNING",
                "BROWSER_ASSIST_SKIPPED",
                "Checkout prepared, but Browser Assist is disabled for this checkout task",
            )
        return run

    async def _ping_checkout_watchers(self, monitor_task: dict, stores: list[dict]) -> None:
        watchers = self.db.list_watchers(monitor_task["id"])
        for watcher in watchers:
            await self._prepare_checkout_watcher(monitor_task, watcher, stores)

    def launch_checkout_assistant_background(self, task_id: int, run_id: int | None = None) -> None:
        existing = self._assistant_jobs.get(task_id)
        if existing and not existing.done():
            return
        job = asyncio.create_task(self.launch_checkout_assistant(task_id, run_id))
        self._assistant_jobs[task_id] = job

    async def launch_checkout_assistant(self, task_id: int, run_id: int | None = None) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["mode"] != "instore_checkout":
            raise ValueError("Browser checkout assistant only supports checkout tasks")

        run = self.db.get_checkout_run(run_id) if run_id else self.db.latest_checkout_run(task_id)
        if not run:
            checkout_url = build_checkout_start_url(task["model_code"], task.get("product_url"))
            configured_url = build_preconfigured_url(task["model_code"], task.get("product_url"))
            run = self.db.create_checkout_run(
                task_id=task_id,
                monitor_task_id=task.get("watch_task_id"),
                model_code=task["model_code"],
                checkout_url=checkout_url,
                store={
                    "store_id": task.get("prepared_store_id"),
                    "store_name": task.get("prepared_store_name"),
                },
                status="PREPARING_CHECKOUT",
                metadata={"manual_launch": True, "configured_url": configured_url, "checkout_start_url": checkout_url},
            )
            self.db.update_task(
                task_id,
                {"prepared_url": checkout_url, "last_checkout_run_id": run["id"]},
            )

        self.db.update_task(
            task_id,
            {"status": "PREPARING_CHECKOUT", "status_message": "Opening Apple checkout assistant…"},
        )
        self.db.update_checkout_run(run["id"], status="PREPARING_CHECKOUT")
        self.db.add_event(task_id, "INFO", "BROWSER_ASSIST_START", "Opening Apple checkout assistant")

        profile = self.db.get_profile(task["profile_id"]) if task.get("profile_id") else None
        payment = None
        payment_warning = ""
        if task.get("auto_checkout") and task.get("profile_id"):
            try:
                payment = get_payment(task["profile_id"])
            except PaymentVaultUnavailable as error:
                payment_warning = str(error)

        async def on_status(status: str, message: str) -> None:
            self.db.update_task(task_id, {"status": status, "status_message": message})
            self.db.update_checkout_run(run["id"], status=status)

        try:
            result = await self.checkout_assistant.prepare(
                self.db.get_task(task_id),
                run,
                profile=profile,
                payment=payment,
                on_status=on_status,
            )
        except CheckoutAssistantUnavailable as error:
            result = {
                "status": "ACTION_REQUIRED",
                "message": str(error),
                "carted": False,
            }

        if payment_warning and result.get("status") not in {"READY_TO_PLACE_ORDER", "SUCCESS"}:
            result["message"] = f"{result.get('message', '')} Secure payment note: {payment_warning}".strip()

        status = result.get("status") or "ACTION_REQUIRED"
        self.db.update_task(task_id, {"status": status, "status_message": result.get("message", "")})

        already_counted = bool((run.get("metadata") or {}).get("cart_counted"))
        run_metadata = {"assistant": result}
        if result.get("carted") and not already_counted:
            self.db.increment(task_id, "carted_count")
            run_metadata["cart_counted"] = True

        self.db.update_checkout_run(
            run["id"],
            status=status,
            metadata=run_metadata,
        )

        if status == "READY_TO_PLACE_ORDER":
            self.db.add_event(task_id, "SUCCESS", "READY_TO_PLACE_ORDER", result.get("message", "Order review ready"), result)
            await self.notifier.send(
                f"✅ READY TO PLACE ORDER\n{task['name']}\n"
                f"{result.get('message', 'Checkout is ready for review.')}\n"
                "⚠️ Place Order has NOT been clicked. Review the open Apple browser and confirm manually."
            )
        elif result.get("carted"):
            self.db.add_event(task_id, "SUCCESS", "CHECKOUT_ASSIST", result.get("message", "Checkout assist progressed"), result)
            await self.notifier.send(
                f"🛒 CHECKOUT ASSIST\n{task['name']}\n{result.get('message', '')}\n"
                "The browser is left open for any required manual step."
            )
        else:
            self.db.add_event(task_id, "INFO", "BROWSER_ASSIST_STOPPED", result.get("message", "Browser assistant stopped"), result)
            await self.notifier.send(
                f"🖥️ CHECKOUT ACTION REQUIRED\n{task['name']}\n{result.get('message', '')}"
            )
        return {"task": self.db.get_task(task_id), "run": self.db.get_checkout_run(run["id"]), "assistant": result}

    async def _process_monitor_result(self, task: dict, data: dict) -> None:
        now = utc_now()
        stores = extract_available_stores(data, task["model_code"], task.get("stores", []))
        # Re-arm availability on every engine start. Database status can still be
        # STOCK_FOUND from the previous process, but the new process has not acted
        # on the current stock yet. This intentionally permits one checkout trigger
        # on the first in-stock poll after a restart.
        was_available = self._stock_state.get(task["id"], False)
        is_available = bool(stores)

        if is_available:
            store_names = ", ".join(
                store.get("store_name") or store.get("store_id") or "Apple Store"
                for store in stores
            )
            self.db.update_task(
                task["id"],
                {
                    "status": "STOCK_FOUND",
                    "status_message": f"Available at {store_names}",
                    "last_checked_at": now,
                    "last_stock_at": now,
                },
            )

            if not was_available:
                self.db.increment(task["id"], "stock_hits")
                self.db.add_event(
                    task["id"],
                    "SUCCESS",
                    "STOCK_FOUND",
                    f"Stock found at {store_names}",
                    {"stores": stores},
                )
                await self._notify_stock(task, stores)
                await self._ping_checkout_watchers(task, stores)
        else:
            self.db.update_task(
                task["id"],
                {
                    "status": "MONITORING",
                    "status_message": "No pickup stock at selected stores",
                    "last_checked_at": now,
                },
            )
            if was_available:
                self.db.add_event(
                    task["id"],
                    "INFO",
                    "STOCK_GONE",
                    "Previously detected stock is no longer available",
                )

        self._stock_state[task["id"]] = is_available
        self._schedule_normal(task)

    async def _poll_group(self, session: aiohttp.ClientSession, tasks: list[dict]) -> None:
        if not tasks:
            return

        zipcode = tasks[0]["zipcode"]
        proxy = tasks[0].get("proxy_url")
        models = [task["model_code"] for task in tasks]

        try:
            data = await fetch_availability(session, models=models, zipcode=zipcode, proxy=proxy)
        except ApiError as error:
            level = "WARNING" if error.status in (429, 541) else "ERROR"
            for task in tasks:
                if error.status in (429, 541):
                    backoff = self._schedule_backoff(task, error.retry_after)
                    self.db.update_task(
                        task["id"],
                        {
                            "status": "RATE_LIMITED",
                            "status_message": f"HTTP {error.status}; retry in about {backoff}s",
                            "last_checked_at": utc_now(),
                        },
                    )
                else:
                    self._schedule_normal(task)
                    self.db.update_task(
                        task["id"],
                        {
                            "status": "ERROR",
                            "status_message": f"Apple returned HTTP {error.status}",
                            "last_checked_at": utc_now(),
                        },
                    )
                self.db.add_event(
                    task["id"],
                    level,
                    "APPLE_HTTP_ERROR",
                    f"Apple request returned HTTP {error.status}",
                )
            self.last_status = "rate_limited" if error.status in (429, 541) else "error"
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            for task in tasks:
                self._schedule_normal(task)
                self.db.update_task(
                    task["id"],
                    {
                        "status": "ERROR",
                        "status_message": type(error).__name__,
                        "last_checked_at": utc_now(),
                    },
                )
                self.db.add_event(
                    task["id"],
                    "ERROR",
                    "NETWORK_ERROR",
                    f"Network error: {type(error).__name__}",
                )
            self.last_status = "error"
            return

        for task in tasks:
            await self._process_monitor_result(task, data)

        self.last_checked_at = utc_now()
        self.last_status = "ok"

    async def run_forever(self) -> None:
        log.info("Starting iPhoneBot v10.3.2 AIO task engine.")
        self.db.add_event(None, "INFO", "ENGINE_STARTED", "iPhoneBot v10.3.2 AIO engine started")

        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            await log_proxy_details(session, self.config.get("http_proxy"))

            while True:
                await self.running.wait()

                monitors = self.db.list_enabled_monitor_tasks()
                if not monitors:
                    self.last_status = "idle"
                    await asyncio.sleep(1.0)
                    continue

                now_mono = time.monotonic()
                due = [
                    task
                    for task in monitors
                    if self._next_due.get(task["id"], 0.0) <= now_mono
                ]

                if not due:
                    await asyncio.sleep(0.5)
                    continue

                grouped: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
                for task in due:
                    grouped[(task["zipcode"], task.get("proxy_url"))].append(task)

                for tasks in grouped.values():
                    try:
                        await self._poll_group(session, tasks)
                    except Exception as error:
                        log.exception("Unexpected monitor group failure")
                        for task in tasks:
                            self._schedule_normal(task)
                            self.db.update_task(
                                task["id"],
                                {
                                    "status": "ERROR",
                                    "status_message": type(error).__name__,
                                    "last_checked_at": utc_now(),
                                },
                            )
                            self.db.add_event(
                                task["id"],
                                "ERROR",
                                "ENGINE_ERROR",
                                f"Monitor worker failed: {type(error).__name__}",
                            )
                        self.last_status = "error"

                await asyncio.sleep(0.1)

    def status(self) -> dict:
        tasks = self.db.list_tasks()
        monitor_tasks = [task for task in tasks if task["mode"] == "monitor"]
        stores: list[str] = []
        for task in monitor_tasks:
            for store in task.get("stores", []):
                if store not in stores:
                    stores.append(store)
        poll = self.config.get("poll_interval_seconds", [55, 65])
        return {
            "running": self.running.is_set(),
            "last_checked_at": self.last_checked_at,
            "last_status": self.last_status,
            "models": [task["model_code"] for task in monitor_tasks if task["enabled"]],
            "stores": stores,
            "poll_interval_seconds": poll,
            "recent_logs": self.db.list_events(5),
        }
