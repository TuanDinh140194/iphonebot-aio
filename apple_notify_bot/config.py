import json
import os
import tempfile
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(ValueError):
    """Raised when config.json is missing or malformed."""


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not path.exists():
        raise ConfigError(
            f"Config file not found at {path}. "
            f"Copy config.json.example to config.json and fill it in."
        )

    with open(path, "r", encoding="utf-8") as file:
        config = json.load(file)

    _validate(config)
    return config


def save_config(config: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    _validate(config)
    path = Path(path)
    file_descriptor, temporary_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        Path(temporary_path).unlink(missing_ok=True)


def _validate(config: dict) -> None:
    if "telegram" not in config:
        raise ConfigError("config.json is missing a 'telegram' section.")
    for field in ("bot_token", "chat_id"):
        if not config["telegram"].get(field):
            raise ConfigError(f"config.json 'telegram' section is missing '{field}'.")

    if not config.get("zipcode"):
        raise ConfigError("config.json is missing required field 'zipcode'.")

    # Legacy fields are optional after v7 because tasks live in SQLite. They are
    # still read once on first launch to migrate the existing monitor selection.
    for field in ("models", "stores"):
        if field in config and not isinstance(config[field], list):
            raise ConfigError(f"config.json field '{field}' must be a list.")

    poll = config.get("poll_interval_seconds", [55, 65])
    if not isinstance(poll, list) or len(poll) != 2:
        raise ConfigError("poll_interval_seconds must be [minimum, maximum].")

    proxy = config.get("http_proxy")
    if proxy is not None and not isinstance(proxy, str):
        raise ConfigError("http_proxy must be one URL string or null.")

    assistant = config.get("checkout_assistant", {})
    if assistant is not None and not isinstance(assistant, dict):
        raise ConfigError("checkout_assistant must be an object if provided.")
    if isinstance(assistant, dict):
        for field in (
            "enabled",
            "auto_launch_on_stock",
            "auto_add_to_bag",
            "auto_checkout",
            "fill_checkout",
            "fill_payment",
            "stop_before_place_order",
            "headless",
            "fresh_session_per_run",
            "auto_start_cdp_chrome",
        ):
            if field in assistant and not isinstance(assistant[field], bool):
                raise ConfigError(f"checkout_assistant.{field} must be true or false.")
        mode = assistant.get("browser_mode", "incognito")
        if mode not in ("incognito", "persistent", "cdp"):
            raise ConfigError("checkout_assistant.browser_mode must be 'incognito', 'persistent', or 'cdp'.")
        cdp_url = assistant.get("cdp_url", "http://127.0.0.1:9222")
        if cdp_url is not None and not isinstance(cdp_url, str):
            raise ConfigError("checkout_assistant.cdp_url must be a URL string or null.")
        profile = assistant.get("browser_profile_dir")
        if profile is not None and not isinstance(profile, str):
            raise ConfigError("checkout_assistant.browser_profile_dir must be a path string or null.")
        chrome_executable = assistant.get("chrome_executable")
        if chrome_executable is not None and not isinstance(chrome_executable, str):
            raise ConfigError("checkout_assistant.chrome_executable must be a path string or null.")
        chrome_user_data_dir = assistant.get("chrome_user_data_dir")
        if chrome_user_data_dir is not None and not isinstance(chrome_user_data_dir, str):
            raise ConfigError("checkout_assistant.chrome_user_data_dir must be a path string or null.")
        chrome_start_timeout = assistant.get("chrome_start_timeout_seconds", 20)
        if not isinstance(chrome_start_timeout, (int, float)) or chrome_start_timeout <= 0:
            raise ConfigError("checkout_assistant.chrome_start_timeout_seconds must be a positive number.")
