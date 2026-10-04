from __future__ import annotations

from collections.abc import Sequence
import asyncio
from ipaddress import ip_address
import logging
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, body: str, retry_after: str | None = None):
        self.status = status
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"Apple API returned status {status}")


async def log_proxy_details(session: aiohttp.ClientSession, proxy: str | None) -> None:
    if not proxy:
        log.info("Apple requests use a direct connection: no proxy configured.")
        return

    try:
        endpoint = urlsplit(proxy)
        log.info("Configured proxy: host=%s port=%s", endpoint.hostname, endpoint.port or 80)
        async with session.get(
            "https://api.ipify.org?format=json",
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            data = await response.json()
            exit_ip = ip_address(data["ip"])
        log.info("Proxy diagnostic exit IP: %s", exit_ip)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as error:
        log.warning("Proxy diagnostic failed: %s. Continuing.", type(error).__name__)


async def fetch_availability(
    session: aiohttp.ClientSession,
    models: Sequence[str],
    zipcode: str,
    proxy: str | None = None,
) -> dict:
    if isinstance(models, str):
        models = [models]

    url = "https://www.apple.com/shop/retail/pickup-message"
    params: list[tuple[str, str]] = [
        ("pl", "true"),
        ("mts.0", "regular"),
        ("location", zipcode),
    ]
    params.extend((f"parts.{index}", model) for index, model in enumerate(models))

    # Use a transparent application identity. This monitor intentionally does not
    # spoof browser fingerprints or attempt to bypass Apple's access controls.
    headers = {
        "User-Agent": "IphoneAIO-Monitor/2.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.8",
    }

    timeout = aiohttp.ClientTimeout(total=20)
    async with session.get(
        url,
        params=params,
        headers=headers,
        proxy=proxy,
        timeout=timeout,
    ) as response:
        if response.status != 200:
            body = await response.text()
            log.warning(
                "Apple request failed: status=%s models=%s body=%r",
                response.status,
                list(models),
                body[:200],
            )
            raise ApiError(
                response.status,
                body,
                retry_after=response.headers.get("Retry-After"),
            )
        return await response.json()


def extract_available_stores(data: dict, model: str, wanted_store_ids: list[str]) -> list[dict]:
    store_list = data.get("body", {}).get("stores", [])
    wanted = set(wanted_store_ids or [])
    relevant_stores = [
        store for store in store_list
        if not wanted or store.get("storeNumber") in wanted
    ]

    available: list[dict] = []
    for store_info in relevant_stores:
        model_status = store_info.get("partsAvailability", {}).get(model, {})
        if model_status.get("pickupDisplay") != "available":
            continue
        regular = model_status.get("messageTypes", {}).get("regular", {})
        available.append(
            {
                "store_id": store_info.get("storeNumber", ""),
                "store_name": store_info.get("storeName", "Unknown store"),
                "store_location": regular.get("storePickupQuote", ""),
                "product_title": regular.get("storePickupProductTitle", model),
            }
        )
    return available
