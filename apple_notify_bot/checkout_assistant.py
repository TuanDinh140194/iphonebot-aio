from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from typing import Awaitable, Callable

from .catalog import build_checkout_start_url, model_details

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BROWSER_PROFILE = ROOT_DIR / "data" / "browser-profile"

StatusCallback = Callable[[str, str], Awaitable[None]]


class CheckoutAssistantUnavailable(RuntimeError):
    pass


class CheckoutAssistant:
    """Headed Playwright checkout helper that stops before final purchase.

    v10 opens the canonical configured Apple product page, prepares pickup, adds the item to
    the bag, fill contact/billing details, and fill card details supplied by the
    OS credential vault. It has a hard guard against clicking final-purchase
    controls such as Place Order / Pay Now / Buy Now.

    It does not bypass CAPTCHA, identity/payment verification, Apple purchase
    limits, access controls, or anti-bot checks. Any such step becomes
    ACTION_REQUIRED and the browser remains open for the user.
    """

    FINAL_PURCHASE_RE = re.compile(
        r"\b(place(?:\s+your)?\s+order|pay\s+now|buy\s+now|submit\s+order|complete\s+purchase|order\s+now|confirm\s+purchase)\b",
        re.I,
    )

    def __init__(self, config: dict):
        settings = config.get("checkout_assistant", {})
        self.enabled = bool(settings.get("enabled", True))
        self.headless = bool(settings.get("headless", False))
        self.fill_checkout = bool(settings.get("fill_checkout", True))
        self.fill_payment = bool(settings.get("fill_payment", True))
        self.stop_before_place_order = True
        if settings.get("stop_before_place_order") is False:
            log.warning("stop_before_place_order=false is ignored; v10 always stops before final purchase")

        mode = str(settings.get("browser_mode", "incognito") or "incognito").strip().lower()
        if mode not in {"incognito", "persistent", "cdp"}:
            log.warning("Unknown browser_mode=%s; using incognito", mode)
            mode = "incognito"
        self.browser_mode = mode
        self.cdp_url = str(settings.get("cdp_url", "http://127.0.0.1:9222") or "http://127.0.0.1:9222").strip()
        self.fresh_session_per_run = bool(settings.get("fresh_session_per_run", True))
        self.auto_start_cdp_chrome = bool(settings.get("auto_start_cdp_chrome", True))
        self.chrome_executable = str(settings.get("chrome_executable") or "").strip() or None
        self.chrome_start_timeout_seconds = float(settings.get("chrome_start_timeout_seconds", 20) or 20)

        chrome_user_data_dir = settings.get("chrome_user_data_dir")
        if chrome_user_data_dir:
            self.chrome_user_data_dir = Path(os.path.expandvars(str(chrome_user_data_dir))).expanduser()
        elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            # Match start_chrome_bot.bat so manual and automatic startup share the
            # same dedicated Chrome profile without touching the user's default profile.
            self.chrome_user_data_dir = Path(os.environ["LOCALAPPDATA"]) / "iPhoneBot-v10-Chrome"
        else:
            self.chrome_user_data_dir = DEFAULT_BROWSER_PROFILE

        profile = settings.get("browser_profile_dir")
        self.profile_dir = Path(profile).expanduser() if profile else DEFAULT_BROWSER_PROFILE
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = None
        self._browser = None
        self._persistent_context = None
        self._contexts: dict[int, object] = {}
        self._pages: dict[int, object] = {}
        self._active_run_ids: dict[int, int | None] = {}
        self._lock = asyncio.Lock()

    async def _emit(self, callback: StatusCallback | None, status: str, message: str) -> None:
        if callback is None:
            return
        try:
            await callback(status, message)
        except Exception:
            log.debug("Status callback failed", exc_info=True)

    async def _reset_browser_state(self) -> None:
        self._pages.clear()
        self._active_run_ids.clear()

        contexts = list(self._contexts.values())
        self._contexts.clear()
        persistent_context = self._persistent_context
        browser = self._browser
        playwright = self._playwright

        self._persistent_context = None
        self._browser = None
        self._playwright = None

        for context in contexts:
            try:
                await context.close()
            except Exception:
                pass
        if persistent_context is not None:
            try:
                await persistent_context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                # A CDP browser belongs to the user. Disconnect Playwright but
                # never close the Chrome window that the user opened manually.
                if self.browser_mode != "cdp":
                    await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    def _cdp_endpoint_ready_sync(self) -> bool:
        endpoint = self.cdp_url.rstrip("/") + "/json/version"
        try:
            with urlopen(endpoint, timeout=0.6) as response:
                return 200 <= int(getattr(response, "status", 200)) < 300
        except Exception:
            return False

    async def _cdp_endpoint_ready(self) -> bool:
        return await asyncio.to_thread(self._cdp_endpoint_ready_sync)

    def _find_windows_chrome(self) -> Path | None:
        if self.chrome_executable:
            candidate = Path(os.path.expandvars(self.chrome_executable)).expanduser()
            return candidate if candidate.is_file() else None

        candidates: list[Path] = []
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _cdp_port(self) -> int:
        try:
            parsed = urlparse(self.cdp_url)
            return int(parsed.port or 9222)
        except Exception:
            return 9222

    async def _ensure_windows_chrome_for_cdp(self) -> None:
        if await self._cdp_endpoint_ready():
            return

        if not self.auto_start_cdp_chrome:
            raise CheckoutAssistantUnavailable(
                f"Chrome is not listening at {self.cdp_url}. Start Chrome with start_chrome_bot.bat, then retry Browser Assist."
            )
        if os.name != "nt":
            raise CheckoutAssistantUnavailable(
                f"Automatic Chrome startup is only enabled on Windows. Start Chrome with remote debugging at {self.cdp_url}."
            )

        chrome = self._find_windows_chrome()
        if chrome is None:
            raise CheckoutAssistantUnavailable(
                "Google Chrome was not found in the standard Windows locations. "
                "Set checkout_assistant.chrome_executable in config.json to chrome.exe."
            )

        self.chrome_user_data_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(chrome),
            f"--remote-debugging-port={self._cdp_port()}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.chrome_user_data_dir}",
            "--new-window",
            "about:blank",
        ]
        log.info(
            "Starting Windows Chrome directly (not through Playwright): %s profile=%s",
            chrome,
            self.chrome_user_data_dir,
        )
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as error:
            raise CheckoutAssistantUnavailable(f"Windows could not start Google Chrome: {error}") from error

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(5.0, self.chrome_start_timeout_seconds)
        while loop.time() < deadline:
            if await self._cdp_endpoint_ready():
                log.info("Windows Chrome is ready for CDP at %s", self.cdp_url)
                return
            await asyncio.sleep(0.25)

        raise CheckoutAssistantUnavailable(
            f"Windows opened Chrome, but the remote-debugging endpoint did not become ready at {self.cdp_url}."
        )

    def _runtime_healthy(self) -> bool:
        try:
            if self.browser_mode == "persistent":
                if self._persistent_context is None:
                    return False
                browser = self._persistent_context.browser
                if browser is not None and not browser.is_connected():
                    return False
                _ = self._persistent_context.pages
                return True
            return self._browser is not None and self._browser.is_connected()
        except Exception:
            return False

    async def _ensure_runtime(self, *, force_relaunch: bool = False) -> None:
        if not self.enabled:
            raise CheckoutAssistantUnavailable("Browser checkout assistant is disabled in config.json")

        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise CheckoutAssistantUnavailable(
                "Playwright is not installed. Run: pip install -r requirements.txt"
            ) from error

        if force_relaunch or not self._runtime_healthy():
            await self._reset_browser_state()
        else:
            return

        # In CDP mode, Chrome is started by Windows/Python first. Playwright is
        # started only after the real Chrome window exposes its debugging port.
        if self.browser_mode == "cdp":
            await self._ensure_windows_chrome_for_cdp()

        self._playwright = await async_playwright().start()
        try:
            if self.browser_mode == "cdp":
                log.info("Attaching Playwright to Windows Chrome at %s", self.cdp_url)
                self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
            elif self.browser_mode == "persistent":
                self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                )
            else:
                # Backwards-compatible fallback: launch a bot-owned Chromium and
                # create isolated contexts for checkout tasks.
                self._browser = await self._playwright.chromium.launch(headless=self.headless)
        except Exception as error:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._browser = None
            self._persistent_context = None
            message = str(error).lower()
            if self.browser_mode == "cdp":
                raise CheckoutAssistantUnavailable(
                    f"Could not attach Playwright to the Windows Chrome window at {self.cdp_url}. "
                    "The bot can auto-start Chrome; check chrome_executable/CDP settings if this persists."
                ) from error
            if self.browser_mode == "persistent" and any(
                token in message for token in ("profile", "lock", "singleton", "user data")
            ):
                raise CheckoutAssistantUnavailable(
                    "Chromium profile is locked or already in use. Close any Chrome/Chromium window using the bot profile, then retry."
                ) from error
            raise CheckoutAssistantUnavailable(
                "Chromium could not start. Run 'python -m playwright install chromium' and retry."
            ) from error

    @staticmethod
    def _context_healthy(context) -> bool:
        if context is None:
            return False
        try:
            browser = context.browser
            if browser is not None and not browser.is_connected():
                return False
            _ = context.pages
            return True
        except Exception:
            return False

    async def _drop_task_context(self, task_id: int) -> None:
        page = self._pages.pop(task_id, None)
        self._active_run_ids.pop(task_id, None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

        if self.browser_mode != "incognito":
            return
        context = self._contexts.pop(task_id, None)
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def _get_task_context(self, task_id: int):
        await self._ensure_runtime()
        if self.browser_mode == "cdp":
            contexts = self._browser.contexts
            if not contexts:
                raise CheckoutAssistantUnavailable(
                    "Connected to Chrome, but Chrome did not expose a browser context."
                )
            return contexts[0]
        if self.browser_mode == "persistent":
            return self._persistent_context

        context = self._contexts.get(task_id)
        if self._context_healthy(context):
            return context
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.pop(task_id, None)

        try:
            context = await self._browser.new_context(viewport={"width": 1440, "height": 1000})
        except Exception as error:
            if type(error).__name__ != "TargetClosedError":
                raise
            log.warning("Browser closed while creating an incognito context; relaunching Chromium once")
            await self._ensure_runtime(force_relaunch=True)
            context = await self._browser.new_context(viewport={"width": 1440, "height": 1000})
        self._contexts[task_id] = context
        return context

    async def _prepare_session_for_run(self, task_id: int, run_id: int | None) -> None:
        if self.browser_mode != "incognito" or not self.fresh_session_per_run:
            self._active_run_ids[task_id] = run_id
            return
        previous = self._active_run_ids.get(task_id)
        if previous != run_id:
            await self._drop_task_context(task_id)
        self._active_run_ids[task_id] = run_id

    async def _get_page(self, task_id: int):
        context = await self._get_task_context(task_id)
        old_page = self._pages.get(task_id)
        try:
            if old_page is not None and not old_page.is_closed() and old_page.context is context:
                return old_page
        except Exception:
            self._pages.pop(task_id, None)

        # When Windows just auto-started the dedicated CDP Chrome, reuse the
        # blank tab that Chrome itself opened. This keeps the visible window/tab
        # OS-owned; Playwright only attaches and navigates it.
        if self.browser_mode == "cdp":
            try:
                for candidate in reversed(context.pages):
                    if candidate.is_closed():
                        continue
                    current_url = str(candidate.url or "").lower()
                    if current_url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                        self._pages[task_id] = candidate
                        return candidate
            except Exception:
                pass

        try:
            page = await context.new_page()
        except Exception as error:
            if type(error).__name__ != "TargetClosedError":
                raise
            log.warning("Browser context closed while creating a page; rebuilding the checkout browser once")
            if self.browser_mode == "incognito":
                await self._drop_task_context(task_id)
                context = await self._get_task_context(task_id)
            else:
                await self._ensure_runtime(force_relaunch=True)
                context = await self._get_task_context(task_id)
            page = await context.new_page()

        self._pages[task_id] = page
        return page

    async def close(self) -> None:
        async with self._lock:
            await self._reset_browser_state()

    async def _body_text(self, page) -> str:
        try:
            return (await page.locator("body").inner_text(timeout=5000)).lower()
        except Exception:
            return ""

    async def _is_page_not_found(self, page) -> bool:
        """Detect Apple's Page Not Found screen after a bad configurator navigation."""
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        text = await self._body_text(page)
        return (
            "page not found" in title
            or "the page you're looking for can't be found" in text
            or "the page you’re looking for can’t be found" in text
        )

    async def _product_matches_expected(self, page, model_code: str) -> bool:
        """Verify the canonical page still represents the expected exact iPhone.

        The page title on Apple's configured route normally includes the family,
        storage and finish.  The URL slug is also accepted as a fallback because
        it is the public configured-product route we generated ourselves.
        """
        details = model_details(model_code) or {}
        if not details:
            return True
        try:
            title = re.sub(r"\s+", " ", (await page.title()).lower()).strip()
        except Exception:
            title = ""
        url = str(getattr(page, "url", "") or "").lower()

        family = str(details.get("family", "")).lower()
        storage = str(details.get("storage", "")).lower()
        color = str(details.get("color", "")).lower()
        display_slug = str(details.get("display_slug", "")).lower()
        expected_family = f"iphone 18 {family}" if family else "iphone 18"
        title_family_ok = expected_family in title and not (family == "pro" and "iphone 18 pro max" in title)
        title_ok = title_family_ok and (not storage or storage in title) and (not color or color in title)

        slug = f"{display_slug}-{storage}-{color}-unlocked" if display_slug and storage and color else ""
        url_ok = bool(slug and slug in url)
        return title_ok or url_ok

    async def _has_verification(self, page) -> bool:
        text = await self._body_text(page)
        markers = (
            "captcha",
            "verify you are human",
            "verify you're human",
            "verification required",
            "unusual traffic",
            "access denied",
            "security check",
            "confirm it's you",
            "confirm it’s you",
            "verify your identity",
            "3-d secure",
            "3d secure",
        )
        return any(marker in text for marker in markers)

    async def _final_purchase_visible(self, page) -> bool:
        # HAR-confirmed final Apple control. This selector is detection-only and is
        # never clicked anywhere in the assistant.
        try:
            final_button = page.locator(
                "#checkout-review-continue,#checkout-review-placeOrder-continue"
            ).first
            if await final_button.is_visible(timeout=180):
                return True
        except Exception:
            pass
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=self.FINAL_PURCHASE_RE)
                if await locator.count() and await locator.first.is_visible(timeout=250):
                    return True
            except Exception:
                continue
        return False

    async def _click_named(self, page, names: list[str], *, timeout: int = 1400) -> bool:
        for name in names:
            if self.FINAL_PURCHASE_RE.search(name):
                log.warning("Refusing to click final-purchase control candidate: %s", name)
                continue
            pattern = re.compile(re.escape(name), re.I)
            for role in ("button", "radio", "link"):
                try:
                    locator = page.get_by_role(role, name=pattern).first
                    if await locator.is_visible(timeout=timeout):
                        # Re-read the accessible text before click when possible.
                        try:
                            label = await locator.inner_text(timeout=500)
                            if self.FINAL_PURCHASE_RE.search(label or ""):
                                continue
                        except Exception:
                            pass
                        await locator.click(timeout=5000)
                        return True
                except Exception:
                    continue
        return False

    async def _select_choice(self, page, names: list[str], *, timeout: int = 1400) -> bool:
        """Select an option without following configurator navigation links.

        Apple often renders option cards with both a radio/button and an anchor.
        Clicking the anchor from a deep pretty URL can append selection query
        parameters to the wrong path and produce Page Not Found. Prefer the
        actual radio/button/label and never use a link for these choices.
        """
        for name in names:
            pattern = re.compile(re.escape(name), re.I)
            try:
                radio = page.get_by_role("radio", name=pattern).first
                if await radio.is_visible(timeout=timeout):
                    try:
                        await radio.check(timeout=5000)
                    except Exception:
                        await radio.click(timeout=5000)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass

            try:
                button = page.get_by_role("button", name=pattern).first
                if await button.is_visible(timeout=timeout):
                    await button.click(timeout=5000)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass

            try:
                label = page.locator("label").filter(has_text=pattern).first
                if await label.is_visible(timeout=timeout):
                    await label.click(timeout=5000)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass
        return False

    async def _fill_first_visible(self, candidates: list[object], value: str, *, timeout: int = 700) -> bool:
        if not value:
            return False
        for field in candidates:
            try:
                if await field.is_visible(timeout=timeout):
                    await field.fill(str(value))
                    return True
            except Exception:
                continue
        return False

    async def _fill_zip(self, page, zipcode: str) -> bool:
        candidates = [
            page.get_by_role("textbox", name=re.compile(r"(zip|postal|location)", re.I)).first,
            page.locator("input[autocomplete='postal-code']").first,
            page.locator("input[name*='postal' i]").first,
            page.locator("input[placeholder*='ZIP' i]").first,
            page.locator("input[aria-label*='ZIP' i]").first,
        ]
        if await self._fill_first_visible(candidates, zipcode, timeout=900):
            for field in candidates:
                try:
                    if await field.is_visible(timeout=400):
                        await field.press("Enter")
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(1200)
            return True
        return False

    async def _select_store(self, page, store_name: str | None, store_id: str | None) -> bool:
        needles = [value for value in (store_name, store_id) if value]
        for needle in needles:
            try:
                pattern = re.compile(re.escape(needle), re.I)
                # Best case: Apple exposes the store name directly in the radio
                # or button accessible name. This avoids guessing an ancestor.
                try:
                    radio = page.get_by_role("radio", name=pattern).first
                    if await radio.is_visible(timeout=700):
                        try:
                            await radio.check(timeout=4000)
                        except Exception:
                            await radio.click(timeout=4000)
                        await page.wait_for_timeout(700)
                        return True
                except Exception:
                    pass
                try:
                    named_button = page.get_by_role("button", name=re.compile(rf"(select|choose|pick up|pickup).*{re.escape(needle)}|{re.escape(needle)}.*(select|choose|pick up|pickup)", re.I)).first
                    if await named_button.is_visible(timeout=700):
                        await named_button.click(timeout=4000)
                        await page.wait_for_timeout(700)
                        return True
                except Exception:
                    pass

                text = page.get_by_text(pattern).first
                if not await text.is_visible(timeout=1200):
                    continue

                # Prefer a real selector control inside the store row/card instead
                # of clicking store-name anchors that can navigate away from the
                # configured product route.
                parent = text.locator("xpath=ancestor::*[self::li or self::div or self::section][1]")
                for role, label in (
                    ("radio", None),
                    ("button", "Select"),
                    ("button", "Choose"),
                    ("button", "Pick up here"),
                    ("button", "Continue"),
                ):
                    try:
                        if role == "radio":
                            control = parent.get_by_role("radio").first
                        else:
                            control = parent.get_by_role("button", name=re.compile(label, re.I)).first
                        if await control.is_visible(timeout=700):
                            try:
                                await control.check(timeout=4000)
                            except Exception:
                                await control.click(timeout=4000)
                            await page.wait_for_timeout(700)
                            return True
                    except Exception:
                        continue

                # Last resort: click only non-anchor text. Never follow an <a>
                # store link because it can discard the product configurator state.
                try:
                    tag = (await text.evaluate("el => el.tagName")).lower()
                except Exception:
                    tag = ""
                if tag != "a":
                    try:
                        await text.click(timeout=4000)
                        await page.wait_for_timeout(700)
                        return True
                    except Exception:
                        pass
            except Exception:
                continue
        return False

    async def _select_connect_later(self, page) -> bool:
        return await self._select_choice(
            page,
            [
                "Connect on your own later",
                "Connect to a carrier later",
                "Connect later",
                "Unlocked",
            ],
        )

    async def _select_full_price(self, page) -> bool:
        return await self._select_choice(
            page,
            [
                "Pay in Full",
                "Pay in full",
                "Full Price",
                "Full price",
            ],
        )

    async def _select_no_trade_in(self, page) -> bool:
        return await self._select_choice(
            page,
            ["No trade-in", "No Trade In", "No thanks, no trade-in", "Buy without trade-in"],
        )

    async def _select_no_applecare(self, page) -> bool:
        return await self._select_choice(
            page,
            [
                "No AppleCare+ Coverage",
                "No AppleCare Coverage",
                "No AppleCare+ coverage",
                "No AppleCare coverage",
                "No AppleCare Cover",
                "No AppleCare+",
            ],
        )

    async def _select_pickup(self, page) -> bool:
        return await self._select_choice(
            page,
            ["Pick up from Store", "Pickup", "Pick Up", "Check availability"],
        )

    async def _prepare_product_options(self, page, task: dict, run: dict) -> dict:
        """Prepare the visible Apple options without synthesizing any cart URL."""
        # The canonical configured URL already selects model/size/storage/finish
        # and the unlocked route. These calls only reinforce visible choices when
        # Apple presents them as real form controls; no anchor/link is followed.
        connect_later = await self._select_connect_later(page)
        full_price = await self._select_full_price(page)
        no_trade_in = await self._select_no_trade_in(page)
        if await self._is_page_not_found(page):
            return {
                "page_not_found": True,
                "connect_later": connect_later,
                "full_price": full_price,
                "no_trade_in": no_trade_in,
            }

        no_applecare = await self._select_no_applecare(page)
        if await self._is_page_not_found(page):
            return {
                "page_not_found": True,
                "connect_later": connect_later,
                "full_price": full_price,
                "no_trade_in": no_trade_in,
                "no_applecare": no_applecare,
            }
        await page.wait_for_timeout(350)

        pickup_clicked = await self._select_pickup(page)
        if pickup_clicked:
            await page.wait_for_timeout(700)

        zip_filled = await self._fill_zip(page, task.get("zipcode", ""))
        store_selected = await self._select_store(
            page,
            run.get("store_name") or task.get("prepared_store_name"),
            run.get("store_id") or task.get("prepared_store_id"),
        )
        return {
            "page_not_found": await self._is_page_not_found(page),
            "connect_later": connect_later,
            "full_price": full_price,
            "no_trade_in": no_trade_in,
            "no_applecare": no_applecare,
            "pickup_clicked": pickup_clicked,
            "zip_filled": zip_filled,
            "store_selected": store_selected,
        }

    async def _click_add_to_bag_button(self, page) -> tuple[bool, str]:
        """Click the real, visible, enabled Add to Bag control.

        Apple can render more than one responsive copy of the purchase control and
        can leave the visible copy disabled briefly while pickup/store state settles.
        v8.2 used ``.first`` with a short visibility timeout, which could select a
        hidden/disabled copy and incorrectly report that nothing was clickable.
        v10 keeps the v8.2.1 behavior and scans every matching control and waits for a visible + enabled one.
        """
        pattern = re.compile(r"add\s+to\s+(bag|cart)", re.I)
        candidates = [
            ("role=button", page.get_by_role("button", name=pattern)),
            ("button text", page.locator("button").filter(has_text=pattern)),
            ("submit input", page.locator("input[type='submit'][value*='Add to Bag' i]")),
            ("button input", page.locator("input[type='button'][value*='Add to Bag' i]")),
            ("button aria-label", page.locator("button[aria-label*='Add to Bag' i]")),
            ("role button text", page.locator("[role='button']").filter(has_text=pattern)),
        ]

        # Give Apple's configurator time to finish enabling the purchase control
        # after pickup/store/AppleCare selections. Do not force-click disabled UI.
        deadline = asyncio.get_running_loop().time() + 12.0
        last_state: list[str] = []
        while asyncio.get_running_loop().time() < deadline:
            last_state.clear()
            for label, group in candidates:
                try:
                    count = min(await group.count(), 12)
                except Exception:
                    continue
                for index in range(count):
                    locator = group.nth(index)
                    try:
                        if not await locator.is_visible(timeout=250):
                            continue
                        enabled = await locator.is_enabled(timeout=250)
                        last_state.append(f"{label}[{index}] visible enabled={enabled}")
                        if not enabled:
                            continue
                        try:
                            await locator.scroll_into_view_if_needed(timeout=1500)
                        except Exception:
                            pass
                        await locator.click(timeout=7000)
                        log.info("Clicked Add to Bag via %s[%s]", label, index)
                        return True, f"clicked via {label}[{index}]"
                    except Exception as exc:
                        last_state.append(f"{label}[{index}] click failed: {type(exc).__name__}")
                        continue
            await page.wait_for_timeout(400)

        # Capture a small diagnostic snapshot so the dashboard/log makes the
        # difference between 'not found' and 'found but disabled' obvious.
        try:
            controls = page.locator("button, input[type='submit'], input[type='button'], [role='button']")
            count = min(await controls.count(), 40)
            relevant: list[str] = []
            for index in range(count):
                control = controls.nth(index)
                try:
                    text = (await control.inner_text(timeout=250)).strip()
                except Exception:
                    try:
                        text = (await control.get_attribute("value")) or (await control.get_attribute("aria-label")) or ""
                    except Exception:
                        text = ""
                if re.search(r"bag|cart|add", text or "", re.I):
                    try:
                        visible = await control.is_visible(timeout=200)
                        enabled = await control.is_enabled(timeout=200)
                    except Exception:
                        visible = enabled = False
                    relevant.append(f"{index}:{text[:80]!r} visible={visible} enabled={enabled}")
            if relevant:
                detail = "; ".join(relevant[:8])
                log.warning("Add to Bag controls were not clickable: %s", detail)
                return False, detail
        except Exception:
            pass

        detail = "; ".join(last_state[:8]) if last_state else "no matching Add to Bag control found"
        log.warning("Add to Bag control was not clickable: %s", detail)
        return False, detail

    async def _attempt_add_to_bag(self, page, model_code: str) -> tuple[bool, str]:
        """Click Apple's real Add to Bag control, then verify the exact cart item.

        A button click alone is never treated as success in v10. The assistant
        always opens Apple's real /shop/bag route and confirms the expected exact
        iPhone configuration before checkout continues.
        """
        if await self._has_verification(page):
            return False, "Apple verification is visible; user action is required"
        if await self._is_page_not_found(page):
            return False, "Apple returned Page Not Found before Add to Bag"

        clicked, click_note = await self._click_add_to_bag_button(page)
        if not clicked:
            return False, f"Add to Bag was not clickable after selecting the product options ({click_note})"

        await page.wait_for_timeout(1800)
        verified = await self._open_bag_and_verify(page, model_code)
        if verified:
            return True, "The exact expected iPhone is present in Apple's real bag"

        if await self._has_verification(page):
            return False, "Apple verification appeared after Add to Bag; review the open browser"
        return False, "Add to Bag was clicked, but the exact expected iPhone was not found in Apple's real bag"

    async def _open_bag_and_verify(self, page, model_code: str) -> bool:
        """Open Apple's real bag and require an exact family/storage/finish match."""
        try:
            await page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1100)
        except Exception:
            return False
        if await self._is_page_not_found(page):
            return False

        text = re.sub(r"\s+", " ", await self._body_text(page)).strip()
        if any(marker in text for marker in ("your bag is empty", "bag is empty", "there are no items in your bag")):
            return False

        normalized_text = re.sub(r"[^a-z0-9]", "", text.lower())
        normalized_code = re.sub(r"[^a-z0-9]", "", model_code.lower())
        if normalized_code and normalized_code in normalized_text:
            return True

        details = model_details(model_code) or {}
        family = str(details.get("family", "")).lower()
        storage = str(details.get("storage", "")).lower()
        color = str(details.get("color", "")).lower()
        if not (family and storage and color):
            return False

        expected_family = f"iphone 18 {family}"
        family_ok = expected_family in text
        if family == "pro":
            family_ok = family_ok and "iphone 18 pro max" not in text
        return family_ok and storage in text and color in text

    async def _is_selector_visible(self, page, selector: str, *, timeout: int = 500) -> bool:
        try:
            return await page.locator(selector).first.is_visible(timeout=timeout)
        except Exception:
            return False

    async def _pickup_contact_visible(self, page, *, timeout: int = 350) -> bool:
        """Recognize Pickup Details quickly from the first live contact control.

        Use one combined DOM query instead of waiting on several selectors in
        series.  This materially reduces the pause between Fulfillment and
        Profile autofill when Apple's old Fulfillment markup is still mounted.
        """
        combined = (
            "#checkout-pickupContact-selfPickupContact-selfContact-address-firstName,"
            "#checkout-pickupContact-selfPickupContact-selfContact-address-lastName,"
            "#checkout-pickupContact-selfPickupContact-selfContact-address-emailAddress,"
            "#checkout-pickupContact-selfPickupContact-selfContact-address-fullDaytimePhone,"
            "#checkout-pickupContact-continue"
        )
        try:
            if await page.locator(combined).first.is_visible(timeout=timeout):
                return True
        except Exception:
            pass
        try:
            heading = page.get_by_text(
                re.compile(r"Now\s+fill\s+out\s+your\s+pickup\s+information", re.I)
            ).first
            return await heading.is_visible(timeout=min(timeout, 250))
        except Exception:
            return False

    async def _billing_option_visible(self, page, *, timeout: int = 350) -> bool:
        """Recognize Billing before card fields exist, without serial waits."""
        combined = (
            "input[name='checkout.billing.billingOptions.selectBillingOption'][value='CREDIT'],"
            "input[value='CREDIT'],"
            "#checkout-billing-options-selectBillingOption"
        )
        try:
            if await page.locator(combined).first.is_visible(timeout=min(timeout, 180)):
                return True
        except Exception:
            pass
        try:
            exact = page.get_by_text(re.compile(r"^\s*Credit\s+or\s+Debit\s+Card\s*$", re.I)).first
            return await exact.is_visible(timeout=min(timeout, 220))
        except Exception:
            return False

    async def _wait_for_post_fulfillment_stage(self, page, *, timeout_ms: int = 20000) -> str:
        """Wait for Apple to leave Fulfillment after Continue was clicked.

        Do not return ``fulfillment`` just because React has not unmounted the old
        fulfillment controls yet.  We only accept the next real stage.
        """
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            if await self._pickup_contact_visible(page, timeout=250):
                return "pickup_contact"
            if await self._billing_option_visible(page, timeout=180) or await self._is_selector_visible(page, "#checkout-billing-continue", timeout=180) or await self._payment_field_visible(page):
                return "billing"
            if await self._is_selector_visible(page, "#checkout-review-placeOrder-continue", timeout=250) or await self._final_purchase_visible(page):
                return "review"
            if await self._has_verification(page):
                return "verification"
            await page.wait_for_timeout(250)
        if await self._pickup_contact_visible(page, timeout=500):
            return "pickup_contact"
        return await self._checkout_stage(page)

    async def _wait_for_post_pickup_contact_stage(self, page, *, timeout_ms: int = 20000) -> str:
        """Wait for Billing after Continue to Payment.

        Pickup Details can remain mounted for a short time after Apple accepts the
        contact form. Ignore that stale stage and wait for the real next screen.
        """
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            if await self._billing_option_visible(page, timeout=180) or await self._is_selector_visible(page, "#checkout-billing-continue", timeout=180) or await self._payment_field_visible(page):
                return "billing"
            if await self._is_selector_visible(page, "#checkout-review-placeOrder-continue", timeout=250) or await self._final_purchase_visible(page):
                return "review"
            if await self._has_verification(page):
                return "verification"
            await page.wait_for_timeout(250)
        return await self._checkout_stage(page)

    async def _checkout_stage(self, page) -> str:
        """Return Apple's current checkout stage using HAR-confirmed controls.

        Important: the Fulfillment screen exists *before*
        ``#checkout-fulfillment-continue`` is necessarily rendered/enabled.  The
        first thing Apple mounts is the delivery/pickup segmented control.  Older
        builds therefore returned ``unknown`` and never invoked the RETAIL click
        routine even though the user could already see "I'll pick it up".
        """
        if await self._is_selector_visible(page, "#checkout-review-placeOrder-continue", timeout=350):
            return "review"
        if await self._billing_option_visible(page, timeout=250) or await self._is_selector_visible(page, "#checkout-billing-continue", timeout=250):
            return "billing"
        if await self._pickup_contact_visible(page, timeout=350):
            return "pickup_contact"

        # Fulfillment can be recognized before the Continue button appears.
        # These selectors are grounded in Apple's checkout.js from the supplied
        # HAR: Ik renders .rs-fulfillment-options containing a two-option
        # rc-segmented-control, while RETAIL later mounts checkout-fulfillment-pickupTab.
        for selector in (
            ".rs-fulfillment-options",
            ".rs-fulfillment-options button[role='radio']",
            "#checkout-fulfillment-pickupTab",
            "#checkout-fulfillment-continue",
        ):
            if await self._is_selector_visible(page, selector, timeout=350):
                return "fulfillment"

        # Accessible-text fallback in case Apple changes only CSS class names.
        try:
            pickup_choice = page.get_by_role(
                "radio", name=re.compile(r"I[’']ll\s+pick\s+it\s+up", re.I)
            ).first
            if await pickup_choice.is_visible(timeout=350):
                return "fulfillment"
        except Exception:
            pass

        if await self._payment_field_visible(page):
            return "billing"
        return "unknown"

    async def _wait_for_checkout_stage(self, page, *, timeout_ms: int = 12000) -> str:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            stage = await self._checkout_stage(page)
            if stage != "unknown":
                return stage
            if await self._has_verification(page):
                return "verification"
            await page.wait_for_timeout(250)
        return await self._checkout_stage(page)

    async def _open_checkout(self, page) -> bool:
        """Open Apple's *standard* checkout, never the Apple Pay checkout.

        The supplied HAR maps the regular checkout button to ``#cart-actions-checkout``
        (StandardCheckout -> /shop/bagx/checkout_now?_a=checkout), while Apple Pay
        uses separate ``...apw...`` controls. Prefer the HAR-confirmed regular
        checkout control and use an exact-text fallback only.
        """
        await self._click_named(page, ["Review Bag", "Go to Bag", "View Bag"])
        await page.wait_for_timeout(600)
        if "/shop/bag" not in page.url:
            try:
                await page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        await page.wait_for_timeout(700)

        clicked = False
        # HAR-confirmed standard checkout controls. Explicitly exclude the
        # Apple Pay path (cart-actions-apwCheckout/navApwCheckout).
        for selector in (
            "#cart-actions-checkout",
            "#cart-actions-navCheckout",
            "#cart-actions-checkoutOtherPayments",
            "#cart-actions-navCheckoutOtherPayments",
        ):
            try:
                control = page.locator(selector).first
                if not await control.is_visible(timeout=700):
                    continue
                if not await control.is_enabled(timeout=700):
                    continue
                label = ""
                try:
                    label = (await control.inner_text(timeout=300)) or ""
                except Exception:
                    try:
                        label = (await control.get_attribute("aria-label")) or ""
                    except Exception:
                        pass
                if "apple pay" in label.lower() or "\uf8ffpay" in label.lower():
                    log.warning("Skipping Apple Pay checkout control %s", selector)
                    continue
                await control.click(timeout=6000)
                log.info("Opened standard Apple checkout via %s", selector)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            # Exact label fallback: anchored regex cannot match
            # "Check out with Apple Pay".
            exact_checkout = re.compile(r"^\s*(check\s*out|checkout|proceed\s+to\s+checkout)\s*$", re.I)
            for role in ("button", "link"):
                try:
                    group = page.get_by_role(role, name=exact_checkout)
                    count = min(await group.count(), 8)
                except Exception:
                    count = 0
                for index in range(count):
                    control = group.nth(index)
                    try:
                        if await control.is_visible(timeout=500) and await control.is_enabled(timeout=500):
                            await control.click(timeout=6000)
                            log.info("Opened standard Apple checkout via exact %s label", role)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break

        if clicked:
            await page.wait_for_timeout(900)

        # HAR-confirmed Guest Checkout control.
        guest = False
        try:
            guest_control = page.locator("#signIn-guestLogin").first
            if await guest_control.is_visible(timeout=1800) and await guest_control.is_enabled(timeout=800):
                await guest_control.click(timeout=6000)
                log.info("Continuing checkout as guest via #signIn-guestLogin")
                guest = True
        except Exception:
            pass

        if not guest:
            exact_guest = re.compile(r"^\s*continue\s+as\s+guest\s*$", re.I)
            for role in ("button", "link"):
                try:
                    control = page.get_by_role(role, name=exact_guest).first
                    if await control.is_visible(timeout=1200) and await control.is_enabled(timeout=700):
                        await control.click(timeout=6000)
                        log.info("Continuing checkout as guest via exact label")
                        guest = True
                        break
                except Exception:
                    continue

        if guest:
            # Guest login submits into the secure checkout app.  Wait for the
            # checkout route and for React to mount a recognizable checkout stage
            # instead of racing the first fulfillment render.
            try:
                await page.wait_for_url(re.compile(r"/shop/checkout(?:$|[/?#])", re.I), timeout=30000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(500)

        stage = await self._wait_for_checkout_stage(page, timeout_ms=30000)
        log.info("Apple checkout stage after standard/guest checkout: %s url=%s", stage, page.url)
        return clicked or guest or stage in {"fulfillment", "pickup_contact", "billing", "review"}

    async def _select_first_pickup_window(self, page) -> bool:
        """Choose the first available Apple pickup check-in window.

        v10.3.8 uses the exact live Apple element reported from checkout:

            <select
              id="checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue"
              data-autom="pickup-availablewindow-dropdown">

        Earlier builds returned early when an older wrapper/container selector was
        missing, so the code never reached this real select even though it was on
        screen.  The select itself is now the primary source of truth.
        """
        exact_selector = (
            'select[id="checkout.fulfillment.pickupTab.pickup.timeSlot.'
            'dateTimeSlots.timeSlotValue"]'
        )
        generic_selector = "select[data-autom='pickup-availablewindow-dropdown']"

        async def get_dropdown():
            # Prefer the exact live ID supplied from Apple checkout.
            exact = page.locator(exact_selector).first
            try:
                if await exact.count():
                    return exact
            except Exception:
                pass
            return page.locator(generic_selector).first

        async def wait_for_dropdown(timeout_ms: int = 25000):
            deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
            while asyncio.get_running_loop().time() < deadline:
                dropdown = await get_dropdown()
                try:
                    if (
                        await dropdown.is_visible(timeout=400)
                        and await dropdown.is_enabled(timeout=400)
                    ):
                        return dropdown
                except Exception:
                    pass
                if await self._has_verification(page):
                    return None
                await page.wait_for_timeout(250)
            return None

        dropdown = await wait_for_dropdown(12000)

        # If the time selector has not rendered yet, Apple may still need a
        # pickup date. Select the first enabled date and then wait again.
        if dropdown is None:
            date_radios = page.locator(
                "input[name='bartPickupDateSelectorButtonGroup'][type='radio']:not([disabled])"
            )
            try:
                count = min(await date_radios.count(), 14)
            except Exception:
                count = 0

            already_checked = False
            for index in range(count):
                radio = date_radios.nth(index)
                try:
                    if await radio.is_checked():
                        already_checked = True
                        break
                except Exception:
                    continue

            if not already_checked:
                for index in range(count):
                    radio = date_radios.nth(index)
                    try:
                        if not await radio.is_visible(timeout=300):
                            continue
                        if not await radio.is_enabled(timeout=300):
                            continue
                        await radio.scroll_into_view_if_needed(timeout=3000)
                        await radio.check(timeout=5000)
                        await page.wait_for_timeout(700)
                        log.info(
                            "Selected Apple pickup date day=%s",
                            (await radio.get_attribute("value")) or "",
                        )
                        break
                    except Exception:
                        continue

            dropdown = await wait_for_dropdown(20000)

        if dropdown is None:
            log.warning(
                "Apple Available Time Window select did not become visible/enabled. "
                "Expected id=checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue"
            )
            return False

        # The user-visible behavior requested for the native select: actually
        # scroll the page to it, focus it, and click it before selecting a time.
        try:
            await dropdown.evaluate(
                "el => el.scrollIntoView({behavior: 'instant', block: 'center', inline: 'nearest'})"
            )
        except Exception:
            pass
        try:
            await dropdown.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(250)

        try:
            await dropdown.focus(timeout=3000)
        except Exception:
            pass

        clicked = False
        try:
            # force=True is scoped to this exact select.  It avoids Apple overlay
            # timing from preventing the click while still targeting no other UI.
            await dropdown.click(timeout=5000, force=True)
            clicked = True
            log.info(
                "Clicked Apple Available Time Window select id=checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue"
            )
        except Exception as exc:
            log.debug("Native Available Time Window click failed: %s", exc)

        if not clicked:
            try:
                await dropdown.evaluate("el => { el.focus(); el.click(); }")
                clicked = True
                log.info("Clicked Apple Available Time Window select via exact DOM fallback")
            except Exception:
                pass

        await page.wait_for_timeout(500)

        # Reacquire after click because Apple's React checkout may re-render the
        # select while the native control is opening.
        dropdown = await get_dropdown()

        # If a slot is already selected, do not disturb it.
        try:
            current = (await dropdown.input_value()).strip()
            if current:
                log.info("Apple pickup time window already selected value=%s", current)
                return True
        except Exception:
            pass

        # Read the real <option> list.  The first option has empty value and is
        # disabled ("Available Windows"); choose the first enabled non-empty
        # option exactly as requested.
        options = dropdown.locator("option")
        try:
            option_count = await options.count()
        except Exception:
            option_count = 0

        available = []
        for index in range(option_count):
            option = options.nth(index)
            try:
                value = ((await option.get_attribute("value")) or "").strip()
                disabled = await option.is_disabled()
                text = re.sub(r"\s+", " ", ((await option.inner_text()) or "")).strip()
                if value and not disabled:
                    available.append((value, text))
            except Exception:
                continue

        if not available:
            # Give Apple a short period after the click to populate time windows.
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline and not available:
                await page.wait_for_timeout(350)
                dropdown = await get_dropdown()
                options = dropdown.locator("option")
                try:
                    option_count = await options.count()
                except Exception:
                    option_count = 0
                for index in range(option_count):
                    option = options.nth(index)
                    try:
                        value = ((await option.get_attribute("value")) or "").strip()
                        disabled = await option.is_disabled()
                        text = re.sub(r"\s+", " ", ((await option.inner_text()) or "")).strip()
                        if value and not disabled:
                            available.append((value, text))
                    except Exception:
                        continue

        if not available:
            log.warning("Available Time Window opened but Apple returned no enabled time options")
            return False

        value, label = available[0]

        # Playwright select_option is the reliable native-select operation and
        # dispatches the input/change events Apple's checkout listens to.  It is
        # intentionally performed only after the visible scroll/focus/click.
        try:
            await dropdown.select_option(value=value, timeout=6000)
            await page.wait_for_timeout(900)
        except Exception as exc:
            log.warning("Apple time-window select_option failed for %s: %s", value, exc)
            return False

        # Reacquire once more because selecting a slot can cause React to render.
        dropdown = await get_dropdown()
        try:
            selected_value = (await dropdown.input_value()).strip()
        except Exception:
            selected_value = ""

        if selected_value == value:
            log.info(
                "Selected Apple pickup time window value=%s label=%s",
                value,
                label,
            )
            return True

        log.warning(
            "Apple time window did not stay selected. expected=%s actual=%s",
            value,
            selected_value,
        )
        return False

    async def _select_retail_fulfillment(self, page, *, timeout_ms: int = 25000) -> bool:
        """Select Apple's Fulfillment option ``RETAIL`` ("I'll pick it up").

        Apple's checkout.js renders the delivery/pickup choice as a custom
        segmented control, not a native ``input[value=RETAIL]``.  The HAR-backed
        DOM shape is approximately::

            .rs-fulfillment-options
              button.rc-segmented-control-button[role="radio"]
                .rc-segmented-control-text  # "I'd like it delivered"
              button.rc-segmented-control-button[role="radio"]
                .rc-segmented-control-text  # "I'll pick it up"

        Prefer that concrete structure first.  Semantic/native fallbacks remain
        for future Apple markup changes.  A click is only considered successful
        after the pickup choice becomes aria-checked or the pickup module renders.
        """
        pickup_tab = page.locator("#checkout-fulfillment-pickupTab").first
        search = page.locator("#checkout-fulfillment-pickupTab-pickup-storeLocator-searchInput").first

        # Apple can restore a prior RETAIL choice when checkout is resumed.
        try:
            if await pickup_tab.is_visible(timeout=250) or await search.is_visible(timeout=250):
                return True
        except Exception:
            pass

        exact_pickup = re.compile(r"^\s*I[’']ll\s+pick\s+it\s+up\s*$", re.I)
        delivered = re.compile(r"^\s*I[’']d\s+like\s+it\s+delivered\s*$", re.I)
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)

        async def pickup_rendered() -> bool:
            try:
                return await pickup_tab.is_visible(timeout=200) or await search.is_visible(timeout=200)
            except Exception:
                return False

        async def wait_after_click(control=None) -> bool:
            # React flips aria-checked as soon as the RETAIL segmented control is
            # accepted.  That state is enough to continue.  Older builds waited
            # for #checkout-fulfillment-pickupTab/search selectors as a second
            # requirement; on Apple's current DOM those selectors may never match,
            # which left the bot stuck immediately after a visibly successful click.
            render_deadline = asyncio.get_running_loop().time() + 8.0
            while asyncio.get_running_loop().time() < render_deadline:
                if await pickup_rendered():
                    return True
                if control is not None:
                    try:
                        if (await control.get_attribute("aria-checked")) == "true":
                            log.info("Apple RETAIL fulfillment confirmed by aria-checked=true")
                            await page.wait_for_timeout(600)
                            return True
                    except Exception:
                        pass
                if await self._has_verification(page):
                    return False
                await page.wait_for_timeout(200)

            # If the exact control was re-rendered and the original handle became
            # stale, re-scan the scoped fulfillment buttons before declaring failure.
            try:
                selected = page.locator(
                    ".rs-fulfillment-options button[role='radio'][aria-checked='true']"
                )
                count = await selected.count()
                for index in range(min(count, 4)):
                    candidate = selected.nth(index)
                    text = re.sub(r"\s+", " ", (await candidate.inner_text()) or "").strip()
                    if exact_pickup.search(text):
                        log.info("Apple RETAIL fulfillment confirmed after React re-render")
                        return True
            except Exception:
                pass
            return False

        last_controls: list[str] = []
        while asyncio.get_running_loop().time() < deadline:
            if await pickup_rendered():
                return True

            # HAR-confirmed React segmented control.  Do not rely on accessible
            # name calculation because Apple nests the visible title in a child div.
            group = page.locator(".rs-fulfillment-options").first
            # The supplied checkout.js renders rc-segmented-control-button today,
            # but role=radio is the stable semantic contract.  Scope it to the
            # fulfillment container so an unrelated checkout radio can never be
            # selected.
            buttons = group.locator("button[role='radio']")
            try:
                count = min(await buttons.count(), 6)
            except Exception:
                count = 0

            last_controls = []
            pickup_button = None
            delivered_button = None
            visible_buttons = []
            for index in range(count):
                button = buttons.nth(index)
                try:
                    if not await button.is_visible(timeout=300):
                        continue
                    visible_buttons.append(button)
                    text = re.sub(r"\s+", " ", (await button.inner_text(timeout=600)) or "").strip()
                    checked = await button.get_attribute("aria-checked")
                    last_controls.append(f"segmented[{index}] text={text!r} checked={checked}")
                    if exact_pickup.search(text):
                        pickup_button = button
                    elif delivered.search(text):
                        delivered_button = button
                except Exception as exc:
                    last_controls.append(f"segmented[{index}] inspect failed: {type(exc).__name__}")

            # HAR order is HOME then RETAIL. Use the second button only when the
            # group has exactly the expected two visible choices and the first is
            # recognizably Delivery; this avoids clicking an unrelated radio.
            if pickup_button is None and len(visible_buttons) == 2 and delivered_button is visible_buttons[0]:
                pickup_button = visible_buttons[1]
                log.info("Using HAR-confirmed second segmented option for Apple RETAIL fulfillment")

            if pickup_button is not None:
                try:
                    if (await pickup_button.get_attribute("aria-checked")) == "true":
                        if await wait_after_click(pickup_button):
                            return True
                    else:
                        try:
                            await pickup_button.scroll_into_view_if_needed(timeout=1500)
                        except Exception:
                            pass
                        try:
                            await pickup_button.click(timeout=6000)
                        except Exception as click_exc:
                            # If Apple's transient loading layer blocks Playwright's
                            # actionability checks, dispatch the click on the exact,
                            # visible HAR-matched RETAIL button.  This still invokes
                            # Apple's normal React onClick handler.
                            log.warning(
                                "Normal RETAIL segmented click failed (%s); retrying exact matched control via DOM click",
                                type(click_exc).__name__,
                            )
                            await pickup_button.evaluate("el => el.click()")
                        log.info("Clicked Apple fulfillment segmented control: I'll pick it up")
                        if await wait_after_click(pickup_button):
                            return True
                except Exception as exc:
                    log.warning("Apple RETAIL segmented-control click failed: %s", type(exc).__name__)

            # Final DOM fallback scoped strictly to the fulfillment module.
            # This is useful when React has mounted the control but Playwright's
            # actionability/accessibility tree is briefly stale.
            try:
                dom_clicked = await group.evaluate(
                    r"""root => {
                        const normalize = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const targets = Array.from(root.querySelectorAll('button[role=\"radio\"], button'));
                        const target = targets.find(el => {
                            const text = normalize(el.innerText || el.textContent);
                            return text === "i’ll pick it up" || text === "i'll pick it up";
                        });
                        if (!target) return false;
                        target.scrollIntoView({block: 'center', inline: 'center'});
                        target.click();
                        return true;
                    }"""
                )
                if dom_clicked:
                    log.info("Dispatched scoped DOM click on Apple's I'll pick it up fulfillment control")
                    if await wait_after_click():
                        return True
            except Exception:
                pass

            # Future-markup fallbacks. These are deliberately secondary to the
            # concrete HAR-observed segmented control above.
            candidates = [
                ("role radio", page.get_by_role("radio", name=re.compile(r"I[’']ll\s+pick\s+it\s+up", re.I)).first, "click"),
                ("RETAIL radio", page.locator("input[type='radio'][value='RETAIL']").first, "check"),
                ("RETAIL input", page.locator("input[value='RETAIL']").first, "check"),
                ("RETAIL button", page.locator("button[value='RETAIL']").first, "click"),
                ("visible text", page.get_by_text(re.compile(r"I[’']ll\s+pick\s+it\s+up", re.I), exact=True).first, "click"),
            ]
            for label, control, action in candidates:
                try:
                    if not await control.is_visible(timeout=250) or not await control.is_enabled(timeout=250):
                        continue
                    if action == "check":
                        try:
                            await control.check(timeout=4000)
                        except Exception:
                            await control.click(timeout=4000)
                    else:
                        await control.click(timeout=4000)
                    log.info("Selected Apple fulfillment RETAIL via fallback %s", label)
                    if await wait_after_click(control):
                        return True
                except Exception:
                    continue

            if await self._has_verification(page):
                return False
            await page.wait_for_timeout(300)

        if last_controls:
            log.warning(
                "Could not select Apple's RETAIL fulfillment option. Visible segmented controls: %s",
                "; ".join(last_controls[:6]),
            )
        else:
            log.warning("Could not find Apple's fulfillment segmented controls for I'll pick it up")
        return False

    async def _prepare_checkout_fulfillment(self, page, task: dict, run: dict) -> dict:
        """Complete Apple's HAR-observed Fulfillment -> Pickup Details step."""
        stage = await self._checkout_stage(page)
        if stage in {"pickup_contact", "billing", "review"}:
            return {"ok": True, "stage": stage, "store_selected": True, "time_slot_selected": True}
        if stage != "fulfillment":
            return {"ok": False, "stage": stage, "store_selected": False, "time_slot_selected": False}

        retail_selected = await self._select_retail_fulfillment(page, timeout_ms=20000)
        if not retail_selected:
            return {
                "ok": False,
                "stage": "fulfillment",
                "store_selected": False,
                "time_slot_selected": False,
                "message": "Apple pickup fulfillment (I'll pick it up) could not be selected.",
            }

        # Apple's current checkout can carry the store selection from the product
        # page and render Available Time Window immediately after RETAIL is chosen.
        # Check for that exact select first so the bot visibly scrolls to it instead
        # of spending time waiting for an optional/legacy store-search selector.
        exact_time_select = page.locator(
            'select[id="checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue"]'
        ).first
        generic_time_select = page.locator(
            "select[data-autom='pickup-availablewindow-dropdown']"
        ).first

        time_select_ready = False
        quick_deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < quick_deadline:
            for candidate in (exact_time_select, generic_time_select):
                try:
                    if await candidate.count() and await candidate.is_enabled(timeout=250):
                        time_select_ready = True
                        break
                except Exception:
                    continue
            if time_select_ready:
                break
            if await self._has_verification(page):
                break
            await page.wait_for_timeout(200)

        time_slot_selected = False
        store_selected = False
        if time_select_ready:
            log.info("Available Time Window is already present after RETAIL; selecting it before store-search fallback")
            time_slot_selected = await self._select_first_pickup_window(page)

        # Only use the store search when the time-window UI is not already ready.
        # The store may have been carried into checkout, so absence of this input is
        # not an error and must not block the time-slot routine.
        if not time_slot_selected:
            search = page.locator("#checkout-fulfillment-pickupTab-pickup-storeLocator-searchInput").first
            try:
                if await search.is_visible(timeout=1500) and await search.is_enabled(timeout=500):
                    zipcode = str(task.get("zipcode") or "").strip()
                    if zipcode:
                        await search.fill(zipcode)
                        await search.press("Enter")
                        await page.wait_for_timeout(1200)
            except Exception:
                log.info("Pickup store search is not visible; continuing because Apple may have carried the store into checkout")

            expected_name = run.get("store_name") or task.get("prepared_store_name")
            expected_id = run.get("store_id") or task.get("prepared_store_id")
            store_selected = await self._select_store(page, expected_name, expected_id)
            if not store_selected and (expected_name or expected_id):
                # If Apple carried the prepared store into checkout, there may be no
                # selectable control left. Treat visible expected store text as enough.
                for needle in (expected_name, expected_id):
                    if not needle:
                        continue
                    try:
                        text = page.get_by_text(re.compile(re.escape(str(needle)), re.I)).first
                        if await text.is_visible(timeout=600):
                            store_selected = True
                            break
                    except Exception:
                        continue

            # This is the authoritative time-window step.  It waits for the exact
            # live select supplied by the user, scrolls it into view, opens it,
            # and chooses the first enabled non-empty option.
            time_slot_selected = await self._select_first_pickup_window(page)

        if not time_slot_selected:
            log.warning("No explicit pickup time window could be selected; checking whether Apple already enabled Continue")

        continue_button = page.locator("#checkout-fulfillment-continue").first
        continued = False
        try:
            if await continue_button.is_visible(timeout=900) and await continue_button.is_enabled(timeout=900):
                await continue_button.click(timeout=5000)
                continued = True
        except Exception:
            pass
        if not continued:
            continued = await self._click_named(page, ["Continue to Pickup Details"], timeout=1200)
        if continued:
            await page.wait_for_timeout(900)

        next_stage = await self._wait_for_post_fulfillment_stage(page, timeout_ms=10000)
        log.info("Apple checkout stage after Continue to Pickup Details: %s", next_stage)
        return {
            "ok": next_stage in {"pickup_contact", "billing", "review"},
            "stage": next_stage,
            "store_selected": store_selected,
            "time_slot_selected": time_slot_selected,
        }

    async def _fill_apple_pickup_phone(self, page, value: str) -> bool:
        """Fill Apple's pickup phone field through its live formatter.

        Apple's checkout model marks this field as text/number with autofill=tel,
        maxLen=14 and validates the formatted value ``(###) ###-####``.  Feeding a
        whole raw string with ``fill()`` can be rejected/reset by Apple's React
        formatter.  Type the ten digits sequentially so Apple's own input handler
        creates the punctuation, then verify the DOM retained the same ten digits.
        """
        raw = re.sub(r"\D", "", str(value or ""))
        if len(raw) == 11 and raw.startswith("1"):
            raw = raw[1:]
        if len(raw) != 10:
            log.warning("Pickup phone is not a valid 10-digit US number; skipping phone autofill")
            return False

        candidates = [
            page.locator("#checkout-pickupContact-selfPickupContact-selfContact-address-fullDaytimePhone").first,
            page.locator("input[autocomplete='tel']").first,
            page.get_by_label(re.compile(r"phone", re.I)).first,
        ]

        for field in candidates:
            try:
                if not await field.is_visible(timeout=900):
                    continue
                if not await field.is_enabled(timeout=1000):
                    continue

                try:
                    await field.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                await field.click(timeout=3000)
                try:
                    await field.fill("")
                except Exception:
                    try:
                        await field.press("Control+A")
                        await field.press("Backspace")
                    except Exception:
                        pass

                # Use real key/input events so Apple's formatter turns ten digits
                # into the required '(###) ###-####' representation.
                try:
                    await field.press_sequentially(raw, delay=25)
                except Exception:
                    for digit in raw:
                        await field.press(digit)
                        await page.wait_for_timeout(20)

                await page.wait_for_timeout(120)
                try:
                    await field.press("Tab")
                except Exception:
                    pass
                await page.wait_for_timeout(120)

                current = ""
                try:
                    current = await field.input_value(timeout=1200)
                except Exception:
                    try:
                        current = await field.get_attribute("value") or ""
                    except Exception:
                        current = ""

                current_digits = re.sub(r"\D", "", current)
                if len(current_digits) == 11 and current_digits.startswith("1"):
                    current_digits = current_digits[1:]
                if current_digits == raw:
                    log.info("Apple pickup phone autofill retained a valid formatted 10-digit value")
                    return True

                log.warning(
                    "Apple pickup phone field did not retain the expected 10 digits after typing; current_length=%s",
                    len(current_digits),
                )
            except Exception as exc:
                log.debug("Pickup phone candidate failed: %s", exc)
                continue
        return False

    async def _fill_pickup_contact(self, page, profile: dict) -> dict:
        """Fill Apple's HAR-confirmed self-pickup contact fields."""
        results = {}

        # Wait for the actual Pickup Details form, not merely the previous
        # fulfillment module disappearing. Apple can mount these asynchronously.
        first_name_field = page.locator(
            "#checkout-pickupContact-selfPickupContact-selfContact-address-firstName"
        ).first
        try:
            await first_name_field.wait_for(state="visible", timeout=6000)
        except Exception:
            log.warning("Pickup Details stage was detected, but the self-pickup contact form did not become visible")

        # HAR defaults to SELF; explicitly select it if Apple exposes the radio.
        for candidate in (
            page.get_by_role("radio", name=re.compile(r"I[’']ll pick it up", re.I)).first,
            page.locator("input[type='radio'][value='SELF']").first,
        ):
            try:
                if await candidate.is_visible(timeout=400):
                    try:
                        await candidate.check(timeout=3000)
                    except Exception:
                        await candidate.click(timeout=3000)
                    break
            except Exception:
                continue

        results["first_name"] = await self._fill_first_visible(
            [
                page.locator("#checkout-pickupContact-selfPickupContact-selfContact-address-firstName").first,
                page.get_by_label(re.compile(r"first\s*name", re.I)).first,
            ],
            profile.get("pickup_first_name") or profile.get("first_name", ""),
            timeout=700,
        )
        results["last_name"] = await self._fill_first_visible(
            [
                page.locator("#checkout-pickupContact-selfPickupContact-selfContact-address-lastName").first,
                page.get_by_label(re.compile(r"last\s*name|surname", re.I)).first,
            ],
            profile.get("pickup_last_name") or profile.get("last_name", ""),
            timeout=700,
        )
        results["email"] = await self._fill_first_visible(
            [
                page.locator("#checkout-pickupContact-selfPickupContact-selfContact-address-emailAddress").first,
                page.get_by_label(re.compile(r"email", re.I)).first,
            ],
            profile.get("email", ""),
            timeout=700,
        )
        results["phone"] = await self._fill_apple_pickup_phone(
            page,
            profile.get("phone", ""),
        )
        log.info(
            "Pickup contact autofill results first_name=%s last_name=%s email=%s phone=%s",
            bool(results.get("first_name")),
            bool(results.get("last_name")),
            bool(results.get("email")),
            bool(results.get("phone")),
        )
        return results

    async def _continue_pickup_contact(self, page) -> bool:
        button = page.locator("#checkout-pickupContact-continue").first
        try:
            if await button.is_visible(timeout=800) and await button.is_enabled(timeout=800):
                await button.click(timeout=5000)
                await page.wait_for_timeout(250)
                return True
        except Exception:
            pass
        clicked = await self._click_named(page, ["Continue to Payment"], timeout=1200)
        if clicked:
            await page.wait_for_timeout(250)
        return clicked

    async def _select_credit_card_option(self, page, *, timeout_ms: int = 15000) -> bool:
        """Select Apple's HAR-confirmed CREDIT payment option and wait for card UI.

        The initial Billing screen only shows payment choices.  The card fields
        are mounted after ``selectBillingOption=CREDIT``.  Target both the native
        input name implied by the HAR POST body and Apple's custom label/control
        rendering, then verify success by waiting for the real card-number field.
        """
        if await self._payment_field_visible(page):
            return True

        exact_text = re.compile(r"^\s*Credit\s+or\s+Debit\s+Card\s*$", re.I)
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        clicked = False

        while asyncio.get_running_loop().time() < deadline:
            if await self._payment_field_visible(page):
                return True

            candidates = [
                page.locator("input[name='checkout.billing.billingOptions.selectBillingOption'][value='CREDIT']").first,
                page.locator("input[type='radio'][value='CREDIT']").first,
                page.get_by_role("radio", name=exact_text).first,
                page.locator("button[role='radio']").filter(has_text=exact_text).first,
                page.locator("label").filter(has_text=exact_text).first,
                page.locator("button").filter(has_text=exact_text).first,
            ]
            for control in candidates:
                try:
                    if not await control.is_visible(timeout=180):
                        continue
                    if not await control.is_enabled(timeout=180):
                        continue
                    try:
                        tag = (await control.evaluate("el => el.tagName.toLowerCase()")) or ""
                    except Exception:
                        tag = ""
                    try:
                        if tag == "input":
                            await control.check(timeout=3500)
                        else:
                            await control.click(timeout=3500)
                    except Exception:
                        try:
                            await control.evaluate("el => el.click()")
                        except Exception:
                            continue
                    clicked = True
                    log.info("Selected Apple billing option: Credit or Debit Card (CREDIT)")
                    break
                except Exception:
                    continue

            if not clicked:
                # Last scoped fallback: find the exact visible text and click the
                # nearest interactive ancestor instead of a generic page text hit.
                try:
                    text = page.get_by_text(exact_text).first
                    if await text.is_visible(timeout=250):
                        ancestor = text.locator(
                            "xpath=ancestor::*[self::label or self::button or @role='radio'][1]"
                        )
                        if await ancestor.count() and await ancestor.is_visible(timeout=200):
                            await ancestor.click(timeout=3500)
                            clicked = True
                            log.info("Selected Apple CREDIT option through its exact-text interactive ancestor")
                        else:
                            await text.click(timeout=3500)
                            clicked = True
                            log.info("Selected Apple CREDIT option through exact visible text")
                except Exception:
                    pass

            if clicked:
                # Prefer the exact HAR-observed field. Waiting on this single
                # selector is much faster than repeatedly probing every generic
                # fallback candidate while Apple's React model is mounting.
                exact_card = page.locator(
                    "#checkout-billing-options-selectedBillingOptions-creditCard-inputs-cardInput-0-cardNumber"
                ).first
                try:
                    await exact_card.wait_for(state="visible", timeout=5000)
                    log.info("Apple credit-card module is visible after CREDIT selection")
                    return True
                except Exception:
                    pass

                field_deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < field_deadline:
                    if await self._payment_field_visible(page):
                        log.info("Apple credit-card module is visible after CREDIT selection")
                        return True
                    if await self._has_verification(page):
                        return False
                    await page.wait_for_timeout(100)
                clicked = False

            await page.wait_for_timeout(150)

        log.warning("Could not activate Apple's Credit or Debit Card (CREDIT) billing option")
        return await self._payment_field_visible(page)

    async def _select_city_state_option(self, page, profile: dict) -> bool:
        select = page.locator("#checkout-billing-options-selectedBillingOptions-creditCard-billingAddress-zipLookup-cityState").first
        try:
            if not await select.is_visible(timeout=500):
                return False
            options = select.locator("option")
            count = await options.count()
            city = str(profile.get("city") or "").strip().lower()
            state = str(profile.get("state") or "").strip().lower()
            fallback_value = None
            for index in range(count):
                option = options.nth(index)
                label = (await option.inner_text()).strip()
                value = await option.get_attribute("value")
                if not value:
                    continue
                if fallback_value is None:
                    fallback_value = value
                low = label.lower()
                if (not city or city in low) and (not state or state in low):
                    await select.select_option(value=value)
                    await page.wait_for_timeout(120)
                    return True
            if fallback_value:
                await select.select_option(value=fallback_value)
                await page.wait_for_timeout(120)
                return True
        except Exception:
            return False
        return False

    async def _finish_billing_city_state(self, page, profile: dict) -> dict:
        """Finish City/State after ZIP without delaying card-number autofill.

        Apple often populates the City/State control asynchronously after ZIP blur.
        Let card autofill start immediately, then come back to this small address
        tail once Apple has had time to resolve the ZIP in parallel.
        """
        results = {}
        exact = "#checkout-billing-options-selectedBillingOptions-creditCard-billingAddress-"
        city_state_selected = await self._select_city_state_option(page, profile)
        results["city_state"] = city_state_selected
        if not city_state_selected:
            results["city"] = await self._fill_first_visible(
                [page.locator(exact + "zipLookup-city").first, page.locator("input[autocomplete='address-level2']").first],
                profile.get("city", ""),
                timeout=180,
            )
            results["state"] = await self._fill_first_visible(
                [page.locator(exact + "zipLookup-state").first, page.locator("input[autocomplete='address-level1']").first],
                profile.get("state", ""),
                timeout=180,
            )
        return results

    async def _fill_billing_address(self, page, profile: dict, *, defer_city_state: bool = False) -> dict:
        """Fill Apple's billing address, optionally deferring City/State.

        In the fast path we fill through ZIP, blur ZIP, and return immediately so
        Card Number can start while Apple's ZIP lookup resolves City/State.
        """
        results = {}
        exact = "#checkout-billing-options-selectedBillingOptions-creditCard-billingAddress-"
        results["first_name"] = await self._fill_first_visible(
            [page.locator(exact + "firstName").first, page.get_by_label(re.compile(r"first\s*name", re.I)).first],
            profile.get("first_name", ""),
            timeout=220,
        )
        results["last_name"] = await self._fill_first_visible(
            [page.locator(exact + "lastName").first, page.get_by_label(re.compile(r"last\s*name", re.I)).first],
            profile.get("last_name", ""),
            timeout=220,
        )
        results["address1"] = await self._fill_first_visible(
            [page.locator(exact + "street").first, page.locator("input[autocomplete='address-line1']").first],
            profile.get("address1", ""),
            timeout=220,
        )
        results["address2"] = await self._fill_first_visible(
            [page.locator(exact + "street2").first, page.locator("input[autocomplete='address-line2']").first],
            profile.get("address2", ""),
            timeout=160,
        )

        postal = page.locator(exact + "zipLookup-postalCode").first
        postal_value = profile.get("postal_code") or profile.get("zipcode", "")
        results["postal_code"] = await self._fill_first_visible(
            [postal, page.locator("input[autocomplete='postal-code']").first], postal_value, timeout=220
        )
        if results["postal_code"]:
            try:
                await postal.press("Tab")
            except Exception:
                pass
            # One short event-loop turn is enough to dispatch ZIP blur/change.
            # Do not wait for City/State here in the fast path.
            await page.wait_for_timeout(60 if defer_city_state else 180)

        if defer_city_state:
            results["city_state_deferred"] = True
            return results

        results.update(await self._finish_billing_city_state(page, profile))
        return results

    async def _payment_field_visible(self, page) -> bool:
        # One combined query avoids stacking several 500ms selector timeouts
        # before payment autofill can begin. The first selector is the exact
        # card-number id observed in the supplied Apple HAR.
        combined = (
            "#checkout-billing-options-selectedBillingOptions-creditCard-inputs-cardInput-0-cardNumber,"
            "input[autocomplete='cc-number'],"
            "input[name*='card' i][name*='number' i],"
            "input[id*='card' i][id*='number' i]"
        )
        for frame in page.frames:
            try:
                if await frame.locator(combined).first.is_visible(timeout=150):
                    return True
            except Exception:
                pass
            try:
                if await frame.get_by_label(re.compile(r"card\s*number|credit.*number", re.I)).first.is_visible(timeout=120):
                    return True
            except Exception:
                pass
        return False

    async def _fill_payment(self, page, payment: dict, profile: dict) -> dict:
        """Fill Apple's card controls with a main-page fast path.

        The supplied HAR shows Apple's card controls in the main checkout
        document.  Older builds scanned every frame and several fallback
        selectors before reaching those exact controls, which could add a few
        seconds after Billing ZIP even though the card fields were already
        visible.  Try the exact HAR-observed fields immediately; only fall back
        to frame/label discovery if one of them is missing.
        """
        result = {"card_number": False, "expiry": False, "cvv": False, "name_on_card": False}
        if not payment:
            return result

        number = re.sub(r"\D", "", str(payment.get("card_number") or ""))
        month = re.sub(r"\D", "", str(payment.get("exp_month") or "")).zfill(2)[-2:]
        year = re.sub(r"\D", "", str(payment.get("exp_year") or ""))
        cvv = re.sub(r"\D", "", str(payment.get("cvv") or ""))
        name = payment.get("name_on_card") or " ".join(
            value for value in (profile.get("first_name"), profile.get("last_name")) if value
        )
        expiry_short = f"{month}/{year[-2:]}" if month and year else ""

        exact_base = "#checkout-billing-options-selectedBillingOptions-creditCard-inputs-cardInput-0-"

        async def type_one(field, value: str, *, digits_only: bool = False, visible_timeout: int = 140) -> bool:
            if not value:
                return False
            try:
                if not await field.is_visible(timeout=visible_timeout):
                    return False
                if not await field.is_enabled(timeout=visible_timeout):
                    return False
                try:
                    await field.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                await field.click(timeout=1800)
                try:
                    await field.press("Control+A")
                    await field.press("Backspace")
                except Exception:
                    try:
                        await field.fill("")
                    except Exception:
                        pass
                # Keep real keyboard/input events for Apple's formatter and card
                # bindings, but do not artificially slow the sequence.
                await field.type(str(value), delay=8)
                await field.press("Tab")
                await page.wait_for_timeout(45)
                try:
                    retained = await field.input_value()
                    if digits_only:
                        if re.sub(r"\D", "", retained) != re.sub(r"\D", "", str(value)):
                            return False
                    elif not retained:
                        return False
                except Exception:
                    pass
                return True
            except Exception:
                return False

        # Fast path: these exact ids are present in the user's Apple checkout HAR
        # and are normally already mounted before ZIP is filled.  This avoids
        # frame-by-frame probing after ZIP.
        result["card_number"] = await type_one(
            page.locator(exact_base + "cardNumber").first, number, digits_only=True, visible_timeout=180
        )
        result["expiry"] = await type_one(
            page.locator(exact_base + "expiration").first, expiry_short, digits_only=True, visible_timeout=180
        )
        result["cvv"] = await type_one(
            page.locator(exact_base + "securityCode").first, cvv, digits_only=True, visible_timeout=180
        )

        # Name-on-card is optional in Apple's current UI.  Do this after the
        # required card fields so it never delays Card Number.
        result["name_on_card"] = await self._fill_first_visible(
            [
                page.get_by_label(re.compile(r"name.*card|cardholder", re.I)).first,
                page.locator("input[autocomplete='cc-name']").first,
            ],
            name,
            timeout=90,
        )

        # Fallback only for fields the exact main-page fast path could not fill.
        # Keep timeouts short so an unrelated iframe cannot add multi-second
        # stalls.
        if not (result["card_number"] and result["expiry"] and result["cvv"]):
            for frame in page.frames:
                if not result["card_number"]:
                    for field in (
                        frame.get_by_label(re.compile(r"card\s*number|credit.*number", re.I)).first,
                        frame.locator("input[autocomplete='cc-number']").first,
                    ):
                        if await type_one(field, number, digits_only=True, visible_timeout=90):
                            result["card_number"] = True
                            break

                if not result["expiry"]:
                    for field in (
                        frame.get_by_label(re.compile(r"expir|mm\s*/\s*yy|expiration date", re.I)).first,
                        frame.locator("input[autocomplete='cc-exp']").first,
                    ):
                        if await type_one(field, expiry_short, digits_only=True, visible_timeout=90):
                            result["expiry"] = True
                            break

                if not result["cvv"]:
                    for field in (
                        frame.get_by_label(re.compile(r"security\s*code|cvv|cvc", re.I)).first,
                        frame.locator("input[autocomplete='cc-csc']").first,
                    ):
                        if await type_one(field, cvv, digits_only=True, visible_timeout=90):
                            result["cvv"] = True
                            break

                if result["card_number"] and result["expiry"] and result["cvv"]:
                    break

        # HAR shows Apple submits validCardNumber=true before Billing -> Review.
        # Keep the existing short settle check after the fields are visibly
        # filled; it does not delay the start of Card Number entry.
        valid_binding = page.locator(
            "#checkout-billing-options-selectedBillingOptions-creditCard-inputs-cardInput-0-validCardNumber"
        ).first
        try:
            if await valid_binding.count():
                deadline = asyncio.get_running_loop().time() + 3.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        value = ((await valid_binding.input_value()) or "").strip().lower()
                    except Exception:
                        value = ((await valid_binding.get_attribute("value")) or "").strip().lower()
                    if value in {"true", "1"}:
                        break
                    await page.wait_for_timeout(80)
        except Exception:
            pass

        await page.wait_for_timeout(80)
        return result

    async def _advance_billing_to_review(self, page, profile: dict, payment: dict) -> bool:
        """Click only Apple's Billing Continue and stop once Review appears.

        Do not require Playwright's ``is_enabled`` probe to become true before
        attempting the safe Billing -> Review action.  Apple's checkout can lag
        in the accessibility/actionability state even after its own card bindings
        have committed.  We first try the HAR-confirmed id, then an exact
        accessible-name ``Continue`` fallback while *still on Billing*.
        """
        exact_continue = page.locator("#checkout-billing-continue").first
        exact_label = re.compile(r"^\s*Continue\s*$", re.I)

        async def get_continue_control():
            try:
                if await exact_continue.is_visible(timeout=350):
                    return exact_continue
            except Exception:
                pass
            # Fallback is deliberately exact text and Billing-stage-only.
            for role in ("button", "link"):
                try:
                    control = page.get_by_role(role, name=exact_label).first
                    if await control.is_visible(timeout=250):
                        return control
                except Exception:
                    continue
            try:
                control = page.locator("input[type='submit'][value='Continue']").first
                if await control.is_visible(timeout=250):
                    return control
            except Exception:
                pass
            return None

        async def click_billing_continue() -> bool:
            if await self._checkout_stage(page) != "billing":
                return False
            control = await get_continue_control()
            if control is None:
                return False
            try:
                await control.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            # Normal click first. Playwright itself waits for actionability.
            try:
                await control.click(timeout=5000)
                log.info("Clicked Apple Billing Continue; advancing to Order Review")
                return True
            except Exception as exc:
                log.debug("Normal Apple Billing Continue click did not fire yet: %s", exc)

            # Exact Billing control only: a forced click can overcome transient
            # overlay/actionability timing, but never targets Place Your Order.
            try:
                if control is exact_continue or await exact_continue.is_visible(timeout=120):
                    await exact_continue.click(timeout=2500, force=True)
                    log.info("Force-clicked Apple's exact Billing Continue control")
                    return True
            except Exception:
                pass

            # DOM click is the final safe fallback, again on the exact Billing id.
            try:
                if await exact_continue.is_visible(timeout=120):
                    disabled = await exact_continue.evaluate(
                        "el => !!el.disabled || el.getAttribute('aria-disabled') === 'true'"
                    )
                    if not disabled:
                        await exact_continue.evaluate("el => el.click()")
                        log.info("Dispatched DOM click on Apple's exact Billing Continue control")
                        return True
            except Exception:
                pass
            return False

        for attempt in range(2):
            if await self._final_purchase_visible(page) or await self._checkout_stage(page) == "review":
                return True

            # Wait for the control itself, not for a separate enabled-state probe.
            deadline = asyncio.get_running_loop().time() + 10.0
            control = None
            while asyncio.get_running_loop().time() < deadline:
                if await self._final_purchase_visible(page) or await self._checkout_stage(page) == "review":
                    return True
                if await self._has_verification(page):
                    return False
                control = await get_continue_control()
                if control is not None:
                    break
                await page.wait_for_timeout(100)

            if control is None:
                log.warning("Apple Billing Continue control did not become visible")
                return False

            # Give Apple one short event-loop turn after the final Tab/blur, then
            # attempt the safe Continue immediately.
            await page.wait_for_timeout(180)
            clicked = await click_billing_continue()

            if not clicked and attempt == 0:
                log.info("Billing Continue was not actionable; re-committing card fields once")
                await self._fill_payment(page, payment, profile)
                await page.wait_for_timeout(250)
                continue
            if not clicked:
                log.warning("Apple Billing Continue could not be clicked after payment autofill")
                return False

            # Wait for Review or a Billing validation response.
            transition_deadline = asyncio.get_running_loop().time() + 12.0
            while asyncio.get_running_loop().time() < transition_deadline:
                if await self._has_verification(page):
                    return False
                if await self._final_purchase_visible(page) or await self._checkout_stage(page) == "review":
                    return True
                await page.wait_for_timeout(120)

            if await self._checkout_stage(page) == "billing" and attempt == 0:
                log.info("Apple stayed on Billing after Continue; re-committing payment once and retrying")
                await self._fill_billing_address(page, profile)
                await self._fill_payment(page, payment, profile)
                continue
            break

        return await self._final_purchase_visible(page) or await self._checkout_stage(page) == "review"

    async def _complete_checkout_to_review(
        self,
        page,
        task: dict,
        run: dict,
        profile: dict,
        payment: dict,
        on_status: StatusCallback | None,
    ) -> dict:
        """HAR-grounded Apple checkout state machine ending before Place Your Order."""
        stage = await self._wait_for_checkout_stage(page, timeout_ms=30000)
        log.info("Checkout state machine starting at stage=%s url=%s", stage, page.url)
        if stage == "verification":
            return {"status": "ACTION_REQUIRED", "message": "Apple verification requires your action.", "carted": True, "browser_url": page.url}

        fulfillment_state = {"ok": True, "stage": stage}
        if stage == "fulfillment":
            await self._emit(on_status, "SELECTING_FULFILLMENT", "Selecting Apple Store pickup and the first available check-in window")
            fulfillment_state = await self._prepare_checkout_fulfillment(page, task, run)
            stage = fulfillment_state.get("stage", "unknown")
            if not fulfillment_state.get("ok"):
                return {
                    "status": "ACTION_REQUIRED",
                    "message": "Apple checkout opened, but pickup fulfillment/time-slot selection could not reach Pickup Details.",
                    "carted": True,
                    "browser_url": page.url,
                    "fulfillment": fulfillment_state,
                }

        pickup_fields = {}
        if stage != "pickup_contact" and await self._pickup_contact_visible(page, timeout=700):
            stage = "pickup_contact"
            log.info("Pickup Details form is visible; forcing checkout stage to pickup_contact")

        if stage == "pickup_contact":
            await self._emit(on_status, "FILLING_CHECKOUT", "Filling Apple pickup contact information from Profile")
            pickup_fields = await self._fill_pickup_contact(page, profile)
            required_ok = all(pickup_fields.get(k) for k in ("first_name", "last_name", "email", "phone"))
            if not required_ok:
                return {
                    "status": "ACTION_REQUIRED",
                    "message": "Pickup Details opened, but one or more required contact fields could not be filled.",
                    "carted": True,
                    "browser_url": page.url,
                    "contact_fields": pickup_fields,
                }
            if not await self._continue_pickup_contact(page):
                return {
                    "status": "ACTION_REQUIRED",
                    "message": "Pickup contact was filled, but Apple could not continue to Payment.",
                    "carted": True,
                    "browser_url": page.url,
                    "contact_fields": pickup_fields,
                }
            stage = await self._wait_for_post_pickup_contact_stage(page, timeout_ms=12000)
            log.info("Apple checkout stage after Continue to Payment: %s", stage)

        if await self._has_verification(page):
            return {
                "status": "ACTION_REQUIRED",
                "message": "Apple verification appeared before payment. Complete it manually; the bot will not bypass verification.",
                "carted": True,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
            }

        if stage == "review" or await self._final_purchase_visible(page):
            return {
                "status": "READY_TO_PLACE_ORDER",
                "message": "Checkout is already at Order Review. Place Your Order has NOT been clicked.",
                "carted": True,
                "payment_filled": False,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
            }

        if stage != "billing":
            return {
                "status": "ACTION_REQUIRED",
                "message": f"Expected Apple Billing after pickup contact, but checkout stage is {stage}.",
                "carted": True,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
            }

        if not self.fill_payment:
            return {
                "status": "ACTION_REQUIRED",
                "message": "Checkout reached Payment, but payment autofill is disabled in config.json.",
                "carted": True,
                "browser_url": page.url,
            }

        # Always activate Apple's CREDIT option first. This makes the real card
        # form visible even when the local secure vault is missing, so the
        # browser is left at the correct manual-recovery point instead of
        # stopping on the billing-option chooser.
        await self._emit(on_status, "SELECTING_PAYMENT", "Selecting Credit or Debit Card")
        card_option_ready = await self._select_credit_card_option(page)
        if not card_option_ready:
            return {
                "status": "ACTION_REQUIRED",
                "message": "Payment page opened, but Apple Credit or Debit Card could not be activated.",
                "carted": True,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
            }

        if not payment:
            return {
                "status": "ACTION_REQUIRED",
                "message": (
                    "Credit or Debit Card is selected, but no card secret is available in Windows Credential Manager for this Profile. "
                    "Open Profiles, choose Add/Replace card, Save Payment again, then rerun checkout."
                ),
                "carted": True,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
            }

        await self._emit(on_status, "FILLING_PAYMENT", "Credit or Debit Card selected; filling billing/payment from the secure Profile")
        # Fast path: fill the address through ZIP, then start Card Number
        # immediately while Apple resolves City/State from the ZIP in parallel.
        billing_fields = await self._fill_billing_address(page, profile, defer_city_state=True)
        payment_fields = await self._fill_payment(page, payment, profile)
        billing_fields.update(await self._finish_billing_city_state(page, profile))
        billing_fields.pop("city_state_deferred", None)
        if not (payment_fields.get("card_number") and payment_fields.get("expiry") and payment_fields.get("cvv")):
            return {
                "status": "ACTION_REQUIRED",
                "message": "Payment page opened, but one or more card fields could not be filled.",
                "carted": True,
                "browser_url": page.url,
                "billing_fields": billing_fields,
                "payment_fields": payment_fields,
            }

        await self._emit(on_status, "PAYMENT_FILLED", "Payment filled; using Apple's Billing → Review Continue control")
        ready = await self._advance_billing_to_review(page, profile, payment)
        if await self._has_verification(page):
            return {
                "status": "ACTION_REQUIRED",
                "message": "Bank/Apple verification requires your action. The bot will not bypass it or place the order.",
                "carted": True,
                "browser_url": page.url,
                "billing_fields": billing_fields,
                "payment_fields": payment_fields,
            }
        if ready:
            await page.bring_to_front()
            return {
                "status": "READY_TO_PLACE_ORDER",
                "message": "Order Review is ready. Contact, billing and saved-card payment are filled. Place Your Order has NOT been clicked.",
                "carted": True,
                "payment_filled": True,
                "browser_url": page.url,
                "contact_fields": pickup_fields,
                "billing_fields": billing_fields,
                "payment_fields": payment_fields,
            }
        return {
            "status": "ACTION_REQUIRED",
            "message": "Payment was filled, but Apple did not reach Order Review. The final Place Your Order control was not clicked.",
            "carted": True,
            "payment_filled": True,
            "browser_url": page.url,
            "contact_fields": pickup_fields,
            "billing_fields": billing_fields,
            "payment_fields": payment_fields,
        }

    async def prepare(
        self,
        task: dict,
        run: dict,
        *,
        profile: dict | None = None,
        payment: dict | None = None,
        on_status: StatusCallback | None = None,
    ) -> dict:
        """Prepare checkout from Apple's canonical configured page and stop at Place Order."""
        async with self._lock:
            try:
                task_id = int(task["id"])
                run_id = run.get("id")
                await self._prepare_session_for_run(task_id, int(run_id) if run_id is not None else None)
                page = await self._get_page(task_id)
                await self._emit(on_status, "PREPARING_CHECKOUT", "Opening canonical Apple product page")
                await page.bring_to_front()

                # v10 never uses Apple's internal ?product=...&step=attach URL.
                # Always rebuild the public configured-product route from the SKU,
                # even when an older v8.1 checkout run stored an internal URL.
                start_url = build_checkout_start_url(task["model_code"], task.get("product_url"))
                await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1400)

                if await self._is_page_not_found(page):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Apple returned Page Not Found on the canonical configured product page. Verify the product URL/SKU before carting.",
                        "carted": False,
                        "browser_url": page.url,
                    }

                if await self._has_verification(page):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Apple verification/CAPTCHA detected. Complete it manually in the open browser, then launch Browser Assist again.",
                        "carted": False,
                        "browser_url": page.url,
                    }

                if not await self._product_matches_expected(page, task["model_code"]):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "The Apple page did not match the expected exact iPhone configuration. The bot stopped before Add to Bag.",
                        "carted": False,
                        "browser_url": page.url,
                    }

                await self._emit(
                    on_status,
                    "SELECTING_OPTIONS",
                    "Selecting visible unlocked/full-price, no trade-in, no AppleCare, and pickup options",
                )
                option_state = await self._prepare_product_options(page, task, run)

                # A UI option can occasionally leave the configurator in a bad
                # route. Retry only the same canonical public product page once;
                # never synthesize an internal Apple cart/configurator URL.
                if option_state.get("page_not_found"):
                    await self._emit(
                        on_status,
                        "RECOVERING_CART",
                        "Apple returned Page Not Found while selecting options; reopening the canonical product page once",
                    )
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(1200)
                    if await self._is_page_not_found(page) or not await self._product_matches_expected(page, task["model_code"]):
                        return {
                            "status": "ACTION_REQUIRED",
                            "message": "Apple could not restore the expected canonical product page. The bot stopped before Add to Bag.",
                            "carted": False,
                            "browser_url": page.url,
                        }
                    option_state = await self._prepare_product_options(page, task, run)
                    if option_state.get("page_not_found"):
                        return {
                            "status": "ACTION_REQUIRED",
                            "message": "Apple still returned Page Not Found while selecting options. Nothing was added to the bag.",
                            "carted": False,
                            "browser_url": page.url,
                            **{k: v for k, v in option_state.items() if k != "page_not_found"},
                        }

                if await self._has_verification(page):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Apple verification appeared while preparing pickup. Complete it manually, then launch Browser Assist again.",
                        "carted": False,
                        "browser_url": page.url,
                        **{k: v for k, v in option_state.items() if k != "page_not_found"},
                    }

                expected_store = run.get("store_id") or task.get("prepared_store_id")
                if expected_store and not option_state.get("store_selected"):
                    log.warning(
                        "Expected pickup store %s could not be confirmed; continuing to Add to Bag because store confirmation is non-blocking in v10.",
                        expected_store,
                    )

                if not task.get("auto_add_to_bag"):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Product/pickup options prepared. Enable Attempt Add to Bag for automatic checkout progression.",
                        "carted": False,
                        "browser_url": page.url,
                        **{k: v for k, v in option_state.items() if k != "page_not_found"},
                    }

                await self._emit(on_status, "CARTING", "Clicking Apple's real Add to Bag button and verifying the exact bag item")
                carted, cart_message = await self._attempt_add_to_bag(page, task["model_code"])
                if not carted:
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": cart_message,
                        "carted": False,
                        "browser_url": page.url,
                    }

                if not task.get("auto_checkout"):
                    return {
                        "status": "CARTED",
                        "message": f"{cart_message}. Full checkout assist is disabled for this task.",
                        "carted": True,
                        "browser_url": page.url,
                    }

                await self._emit(on_status, "FILLING_CHECKOUT", "Opening the verified Apple bag and checkout")
                checkout_opened = await self._open_checkout(page)
                if await self._has_verification(page):
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Verification/sign-in step requires your action. Complete it manually; the bot will not bypass verification.",
                        "carted": True,
                        "browser_url": page.url,
                    }

                if not checkout_opened:
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "The exact iPhone is in the bag, but Apple checkout could not be opened automatically.",
                        "carted": True,
                        "browser_url": page.url,
                    }

                if not profile:
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Item is carted, but this checkout task has no Profile. Assign a Profile with contact, billing and saved payment.",
                        "carted": True,
                        "browser_url": page.url,
                    }

                if not self.fill_checkout:
                    return {
                        "status": "ACTION_REQUIRED",
                        "message": "Apple checkout opened, but checkout autofill is disabled in config.json.",
                        "carted": True,
                        "browser_url": page.url,
                    }

                return await self._complete_checkout_to_review(
                    page,
                    task,
                    run,
                    profile,
                    payment,
                    on_status,
                )
            except CheckoutAssistantUnavailable:
                raise
            except Exception as error:
                log.exception("Checkout browser assistant failed")
                return {
                    "status": "ACTION_REQUIRED",
                    "message": f"Browser assistant stopped: {type(error).__name__}. The canonical Apple product link remains available.",
                    "carted": False,
                }

