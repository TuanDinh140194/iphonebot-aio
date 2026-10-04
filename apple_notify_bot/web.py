from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .catalog import MODEL_CATALOG, build_checkout_start_url
from .config import load_config, save_config
from .payment_vault import PaymentVaultUnavailable, delete_payment, get_payment, save_payment

log = logging.getLogger(__name__)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


class PollIntervalInput(BaseModel):
    minimum_seconds: int
    maximum_seconds: int


class TaskCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    mode: Literal["monitor", "instore_checkout"] = "monitor"
    model_code: str = Field(min_length=1, max_length=64)
    product_url: str = ""
    zipcode: str = Field(min_length=3, max_length=16)
    stores: list[str] = []
    poll_min: int = 55
    poll_max: int = 65
    proxy_url: str | None = None
    profile_id: int | None = None
    watch_task_id: int | None = None
    auto_prepare_browser: bool = False
    auto_add_to_bag: bool = False
    auto_checkout: bool = False
    enabled: bool = True


class TaskPairInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    checkout_name: str | None = None
    model_code: str = Field(min_length=1, max_length=64)
    product_url: str = ""
    zipcode: str = Field(min_length=3, max_length=16)
    stores: list[str] = []
    poll_min: int = 55
    poll_max: int = 65
    proxy_url: str | None = None
    profile_id: int | None = None
    auto_prepare_browser: bool = True
    auto_add_to_bag: bool = True
    auto_checkout: bool = True
    enabled: bool = True


class TaskUpdateInput(BaseModel):
    name: str | None = None
    model_code: str | None = None
    product_url: str | None = None
    zipcode: str | None = None
    stores: list[str] | None = None
    poll_min: int | None = None
    poll_max: int | None = None
    proxy_url: str | None = None
    profile_id: int | None = None
    watch_task_id: int | None = None
    auto_prepare_browser: bool | None = None
    auto_add_to_bag: bool | None = None
    auto_checkout: bool | None = None


class ProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    pickup_first_name: str = ""
    pickup_last_name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"


class PaymentInput(BaseModel):
    card_number: str = Field(min_length=12, max_length=32)
    exp_month: str = Field(min_length=1, max_length=2)
    exp_year: str = Field(min_length=2, max_length=4)
    cvv: str = Field(min_length=3, max_length=4)
    name_on_card: str = Field(default="", max_length=120)


def _validate_poll(low: int, high: int) -> None:
    if not 5 <= low <= 3600 or not 5 <= high <= 3600:
        raise HTTPException(422, "Poll interval must be between 5 and 3600 seconds")
    if low > high:
        raise HTTPException(422, "Minimum cannot be greater than maximum")


def create_app(bot) -> FastAPI:
    app = FastAPI(title="iPhoneBot v10.3.18 AIO", version="10.3.18")

    def _profile_with_vault_state(profile: dict) -> dict:
        enriched = dict(profile)
        try:
            saved = bool(get_payment(int(profile["id"])))
            enriched["payment_vault_saved"] = saved
            enriched["payment_vault_error"] = ""
        except PaymentVaultUnavailable as error:
            enriched["payment_vault_saved"] = False
            enriched["payment_vault_error"] = str(error)
        return enriched

    def _profiles_with_vault_state() -> list[dict]:
        return [_profile_with_vault_state(profile) for profile in bot.db.list_profiles()]

    def _require_vault_payment(profile_id: int) -> dict:
        try:
            payment = get_payment(profile_id)
        except PaymentVaultUnavailable as error:
            raise HTTPException(503, str(error)) from error
        if not payment:
            raise HTTPException(
                422,
                "Full checkout assist requires a card secret saved in Windows Credential Manager. Open Profiles, choose Add/Replace card, and Save Payment again.",
            )
        return payment
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health_check(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "message": "It's running!",
                "service": os.environ.get("K_SERVICE", "Local"),
                "revision": os.environ.get("K_REVISION", "dev"),
            },
        )

    @app.get("/api/dashboard")
    async def dashboard():
        return {
            "version": "10.3.18",
            "engine": {
                "running": bot.running.is_set(),
                "last_checked_at": bot.last_checked_at,
                "last_status": bot.last_status,
            },
            "overview": bot.db.overview(),
            "tasks": bot.db.list_tasks(),
            "profiles": _profiles_with_vault_state(),
            "groups": bot.db.list_groups(),
            "events": bot.db.list_events(80),
            "checkout_runs": bot.db.list_checkout_runs(30),
            "catalog": MODEL_CATALOG,
        }

    @app.get("/api/tasks")
    async def list_tasks():
        return {"tasks": bot.db.list_tasks()}

    @app.post("/api/tasks")
    async def create_task(item: TaskCreateInput):
        _validate_poll(item.poll_min, item.poll_max)
        if item.mode == "instore_checkout" and item.watch_task_id is None:
            raise HTTPException(422, "Checkout tasks need a watch_task_id")
        task = bot.db.create_task(item.model_dump())
        return {"task": task}

    @app.post("/api/task-pairs")
    async def create_task_pair(item: TaskPairInput):
        _validate_poll(item.poll_min, item.poll_max)
        if item.auto_add_to_bag and not item.auto_prepare_browser:
            raise HTTPException(422, "Auto Add to Bag requires Browser Assist")
        if item.auto_checkout and not (item.auto_prepare_browser and item.auto_add_to_bag):
            raise HTTPException(422, "Full checkout assist requires Browser Assist and Add to Bag")
        if item.auto_checkout and item.profile_id is None:
            raise HTTPException(422, "Full checkout assist requires a checkout profile")
        if item.auto_checkout:
            profile = bot.db.get_profile(item.profile_id)
            if not profile:
                raise HTTPException(422, "Checkout profile not found")
            _require_vault_payment(item.profile_id)
        pair = bot.db.create_pair(item.model_dump())
        return pair

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: int, item: TaskUpdateInput):
        existing = bot.db.get_task(task_id)
        if not existing:
            raise HTTPException(404, "Task not found")
        changes = item.model_dump(exclude_unset=True)
        low = int(changes.get("poll_min", existing["poll_min"]))
        high = int(changes.get("poll_max", existing["poll_max"]))
        _validate_poll(low, high)
        auto_browser = bool(changes.get("auto_prepare_browser", existing.get("auto_prepare_browser")))
        auto_bag = bool(changes.get("auto_add_to_bag", existing.get("auto_add_to_bag")))
        auto_checkout = bool(changes.get("auto_checkout", existing.get("auto_checkout")))
        if auto_bag and not auto_browser:
            raise HTTPException(422, "Auto Add to Bag requires Browser Assist")
        if auto_checkout and not (auto_browser and auto_bag):
            raise HTTPException(422, "Full checkout assist requires Browser Assist and Add to Bag")
        profile_id = changes.get("profile_id", existing.get("profile_id"))
        if auto_checkout and profile_id is None:
            raise HTTPException(422, "Full checkout assist requires a checkout profile")
        if auto_checkout:
            profile = bot.db.get_profile(profile_id)
            if not profile:
                raise HTTPException(422, "Checkout profile not found")
            _require_vault_payment(profile_id)
        task = bot.db.update_task(task_id, changes)
        return {"task": task}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: int):
        if not bot.db.delete_task(task_id):
            raise HTTPException(404, "Task not found")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: int):
        task = bot.db.set_task_enabled(task_id, True)
        if not task:
            raise HTTPException(404, "Task not found")
        return {"task": task}

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: int):
        task = bot.db.set_task_enabled(task_id, False)
        if not task:
            raise HTTPException(404, "Task not found")
        return {"task": task}

    @app.post("/api/tasks/start-all")
    async def start_all_tasks():
        bot.db.set_all_enabled(True)
        return {"ok": True}

    @app.post("/api/tasks/stop-all")
    async def stop_all_tasks():
        bot.db.set_all_enabled(False)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/reset-checkout")
    async def reset_checkout(task_id: int):
        task = bot.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["mode"] != "instore_checkout":
            raise HTTPException(422, "Only checkout tasks can be reset")
        task = bot.db.update_task(
            task_id,
            {
                "status": "WAITING_FOR_MONITOR_PING",
                "status_message": "",
                "prepared_url": None,
                "prepared_store_id": None,
                "prepared_store_name": None,
                "last_checkout_run_id": None,
            },
        )
        bot.db.add_event(task_id, "INFO", "CHECKOUT_RESET", "Checkout watcher reset")
        return {"task": task}

    @app.post("/api/tasks/{task_id}/prepare-link")
    async def prepare_link(task_id: int):
        task = bot.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["mode"] != "instore_checkout":
            raise HTTPException(422, "Only checkout tasks have prepared links")
        url = build_checkout_start_url(task["model_code"], task.get("product_url"))
        task = bot.db.update_task(
            task_id,
            {
                "prepared_url": url,
                "status": "CHECKOUT_READY",
                "status_message": "Canonical Apple product link prepared",
            },
        )
        bot.db.add_event(task_id, "INFO", "LINK_PREPARED", "Prepared canonical Apple product link", {"url": url})
        return {"task": task, "url": url}

    @app.post("/api/tasks/{task_id}/launch-assistant")
    async def launch_assistant(task_id: int):
        task = bot.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["mode"] != "instore_checkout":
            raise HTTPException(422, "Only checkout tasks can launch Browser Assist")
        # Run in the background so the dashboard request returns immediately.
        bot.launch_checkout_assistant_background(task_id, task.get("last_checkout_run_id"))
        return {"ok": True, "task": bot.db.get_task(task_id)}

    @app.post("/api/tasks/{task_id}/mark-carted")
    async def mark_carted(task_id: int):
        task = bot.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        bot.db.increment(task_id, "carted_count")
        task = bot.db.update_task(task_id, {"status": "CARTED", "status_message": "Marked carted by user"})
        run = bot.db.latest_checkout_run(task_id)
        if run:
            bot.db.update_checkout_run(run["id"], status="CARTED")
        bot.db.add_event(task_id, "SUCCESS", "CARTED", "User marked item as added to cart")
        return {"task": task}

    @app.post("/api/tasks/{task_id}/mark-checkout")
    async def mark_checkout(task_id: int):
        task = bot.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        bot.db.increment(task_id, "checkout_count")
        task = bot.db.update_task(task_id, {"status": "SUCCESS", "status_message": "Checkout completed"})
        run = bot.db.latest_checkout_run(task_id)
        if run:
            bot.db.update_checkout_run(run["id"], status="SUCCESS")
        bot.db.add_event(task_id, "SUCCESS", "CHECKOUT_SUCCESS", "User marked checkout as completed")
        return {"task": task}

    @app.get("/api/checkout-runs")
    async def list_checkout_runs(limit: int = 50):
        return {"checkout_runs": bot.db.list_checkout_runs(limit)}

    @app.get("/api/events")
    async def list_events(limit: int = 100):
        return {"events": bot.db.list_events(limit)}

    @app.get("/api/profiles")
    async def list_profiles():
        return {"profiles": _profiles_with_vault_state()}

    @app.post("/api/profiles")
    async def create_profile(item: ProfileInput):
        profile = bot.db.create_profile(item.model_dump())
        return {"profile": profile}

    @app.put("/api/profiles/{profile_id}")
    async def update_profile(profile_id: int, item: ProfileInput):
        if not bot.db.get_profile(profile_id):
            raise HTTPException(404, "Profile not found")
        profile = bot.db.update_profile(profile_id, item.model_dump())
        return {"profile": profile}

    @app.put("/api/profiles/{profile_id}/payment")
    async def save_profile_payment(profile_id: int, item: PaymentInput):
        if not bot.db.get_profile(profile_id):
            raise HTTPException(404, "Profile not found")
        try:
            summary = save_payment(profile_id, **item.model_dump())
            verified = get_payment(profile_id)
            if not verified:
                raise PaymentVaultUnavailable(
                    "Payment was submitted but could not be verified in Windows Credential Manager. Save the card again while running the bot under your normal Windows account."
                )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except PaymentVaultUnavailable as error:
            raise HTTPException(503, str(error)) from error
        profile = bot.db.set_profile_payment_last4(profile_id, summary.last4)
        return {
            "profile": _profile_with_vault_state(profile),
            "payment": {"last4": summary.last4, "label": summary.label, "vault_saved": True},
        }

    @app.delete("/api/profiles/{profile_id}/payment")
    async def delete_profile_payment(profile_id: int):
        if not bot.db.get_profile(profile_id):
            raise HTTPException(404, "Profile not found")
        try:
            delete_payment(profile_id)
        except PaymentVaultUnavailable as error:
            raise HTTPException(503, str(error)) from error
        profile = bot.db.set_profile_payment_last4(profile_id, "")
        return {"ok": True, "profile": profile}

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: int):
        if not bot.db.get_profile(profile_id):
            raise HTTPException(404, "Profile not found")
        try:
            delete_payment(profile_id)
        except PaymentVaultUnavailable:
            # Profile deletion should still work when the OS vault is unavailable.
            pass
        if not bot.db.delete_profile(profile_id):
            raise HTTPException(404, "Profile not found")
        return {"ok": True}

    @app.post("/api/engine/pause")
    async def pause_engine():
        bot.pause()
        return {"running": False}

    @app.post("/api/engine/resume")
    async def resume_engine():
        bot.resume()
        return {"running": True}

    # ---- Backwards-compatible endpoints --------------------------------
    @app.get("/status")
    async def get_status():
        return bot.status()

    @app.post("/bot/pause")
    async def pause_bot():
        bot.pause()
        return bot.status()

    @app.post("/bot/resume")
    async def resume_bot():
        bot.resume()
        return bot.status()

    @app.post("/settings/poll-interval")
    async def update_poll_interval(item: PollIntervalInput):
        _validate_poll(item.minimum_seconds, item.maximum_seconds)
        config = load_config()
        config["poll_interval_seconds"] = [item.minimum_seconds, item.maximum_seconds]
        save_config(config)
        bot.apply_config(config)
        return {"poll_interval_seconds": config["poll_interval_seconds"]}

    frontend_dist = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    )
    if os.path.isdir(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
