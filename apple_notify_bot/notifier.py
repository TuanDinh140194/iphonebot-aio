import logging

import aiohttp

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        log.info("NOTIFY: %s", text.replace("\n", " | "))
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status >= 400:
                        body = await response.text()
                        log.error("Telegram send failed: HTTP %s %s", response.status, body[:160])
        except (aiohttp.ClientError, TimeoutError) as error:
            log.error("Telegram send failed: %s", type(error).__name__)
