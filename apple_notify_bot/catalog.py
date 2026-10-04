"""Built-in iPhone 18 Pro / Pro Max catalog and Apple deep-link helpers.

The monitor still uses Apple part numbers. v8 also stores structured model details so
checkout watchers can build the canonical Apple product page for the exact size,
finish and storage before Browser Assist clicks the real Apple controls.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _item(code: str, family: str, storage: str, color: str) -> dict:
    display = "6.9-inch-display" if family == "Pro Max" else "6.3-inch-display"
    label = f"{family} {storage} {color}"
    return {
        "code": code,
        "label": label,
        "family": family,
        "storage": storage,
        "color": color,
        "display_slug": display,
    }


MODEL_CATALOG = [
    # iPhone 18 Pro - 256GB
    _item("MJQ34LL/A", "Pro", "256GB", "Black"),
    _item("MJQ44LL/A", "Pro", "256GB", "Silver"),
    _item("MJQ54LL/A", "Pro", "256GB", "Burgundy"),
    _item("MJQ64LL/A", "Pro", "256GB", "Glacier"),
    # 512GB
    _item("MJQ74LL/A", "Pro", "512GB", "Black"),
    _item("MJQ84LL/A", "Pro", "512GB", "Silver"),
    _item("MJQ94LL/A", "Pro", "512GB", "Burgundy"),
    _item("MJQA4LL/A", "Pro", "512GB", "Glacier"),
    # 1TB
    _item("MJQC4LL/A", "Pro", "1TB", "Black"),
    _item("MJQD4LL/A", "Pro", "1TB", "Silver"),
    _item("MJQE4LL/A", "Pro", "1TB", "Burgundy"),
    _item("MJQF4LL/A", "Pro", "1TB", "Glacier"),
    # 2TB
    _item("MJQG4LL/A", "Pro", "2TB", "Black"),
    _item("MJQH4LL/A", "Pro", "2TB", "Silver"),
    _item("MJQJ4LL/A", "Pro", "2TB", "Burgundy"),
    _item("MJQK4LL/A", "Pro", "2TB", "Glacier"),
    # iPhone 18 Pro Max - 256GB
    _item("MJW44LL/A", "Pro Max", "256GB", "Black"),
    _item("MJW54LL/A", "Pro Max", "256GB", "Silver"),
    _item("MJW64LL/A", "Pro Max", "256GB", "Burgundy"),
    _item("MJW74LL/A", "Pro Max", "256GB", "Glacier"),
    # 512GB
    _item("MJW84LL/A", "Pro Max", "512GB", "Black"),
    _item("MJW94LL/A", "Pro Max", "512GB", "Silver"),
    _item("MJWA4LL/A", "Pro Max", "512GB", "Burgundy"),
    _item("MJWC4LL/A", "Pro Max", "512GB", "Glacier"),
    # 1TB
    _item("MJWD4LL/A", "Pro Max", "1TB", "Black"),
    _item("MJWE4LL/A", "Pro Max", "1TB", "Silver"),
    _item("MJWF4LL/A", "Pro Max", "1TB", "Burgundy"),
    _item("MJWG4LL/A", "Pro Max", "1TB", "Glacier"),
    # 2TB
    _item("MJWH4LL/A", "Pro Max", "2TB", "Black"),
    _item("MJWJ4LL/A", "Pro Max", "2TB", "Silver"),
    _item("MJWK4LL/A", "Pro Max", "2TB", "Burgundy"),
    _item("MJWL4LL/A", "Pro Max", "2TB", "Glacier"),
]

MODEL_BY_CODE = {item["code"]: item for item in MODEL_CATALOG}
MODEL_LABELS = {item["code"]: item["label"] for item in MODEL_CATALOG}

APPLE_IPHONE_BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro"


def _family_base_url(product_url: str | None = None) -> str:
    """Normalize any Apple iPhone 18 Pro URL back to the family route.

    Apple selection/query parameters are only reliable on the family route.
    Starting an automated flow from the pretty configured slug can cause Apple
    to append its selection query to that slug and return Page Not Found.
    """
    base = (product_url or APPLE_IPHONE_BUY_URL).strip() or APPLE_IPHONE_BUY_URL
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    marker = "/shop/buy-iphone/iphone-18-pro"
    if marker in path:
        prefix = path.split(marker, 1)[0]
        family_path = f"{prefix}{marker}"
    else:
        family_path = marker
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.apple.com", family_path, "", ""))


def build_fast_checkout_url(code: str, product_url: str | None = None) -> str:
    """Backward-compatible alias for the canonical configured product URL.

    v8.2 intentionally stopped using Apple's internal ``?product=...&step=attach``
    configurator URL because it can render the right-looking product page without
    creating the cart/session state Apple expects.  Older callers can still use
    this function name, but it now returns the same safe canonical URL as
    :func:`build_preconfigured_url`.
    """
    return build_preconfigured_url(code, product_url)


def model_label(code: str) -> str:
    return MODEL_LABELS.get(code, code)


def model_details(code: str) -> dict | None:
    item = MODEL_BY_CODE.get(code)
    return dict(item) if item else None


def build_preconfigured_url(code: str, product_url: str | None = None) -> str:
    """Return Apple's configured-product route for a known v8 catalog SKU.

    The URL selects model size, storage, finish and the unlocked / connect-later
    variant. It intentionally does not choose trade-in, AppleCare, financing,
    payment, or submit an order.
    """
    details = model_details(code)
    base = (product_url or APPLE_IPHONE_BUY_URL).strip() or APPLE_IPHONE_BUY_URL
    if not details:
        return base

    parts = urlsplit(base)
    # Preserve storefront prefixes such as /us-edu/ if the user supplied one,
    # but normalize the family route before appending the configuration slug.
    path = parts.path.rstrip("/")
    marker = "/shop/buy-iphone/iphone-18-pro"
    if marker in path:
        prefix = path.split(marker, 1)[0]
        family_path = f"{prefix}{marker}"
    else:
        family_path = marker

    storage = details["storage"].lower()
    color = details["color"].lower()
    slug = f"{details['display_slug']}-{storage}-{color}-unlocked"
    configured_path = f"{family_path}/{slug}"
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.apple.com", configured_path, "", ""))


def build_checkout_start_url(code: str, product_url: str | None = None) -> str:
    """Return the canonical Apple page Browser Assist should open.

    This URL selects the exact model/size/storage/color/unlocked variant using
    Apple's public product route.  Browser Assist then clicks Apple's real form
    controls and real Add to Bag button; it never synthesizes an internal cart URL.
    """
    return build_preconfigured_url(code, product_url)
