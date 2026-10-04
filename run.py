import asyncio
import logging
import os

import uvicorn

from apple_notify_bot.bot import AppleStockBot
from apple_notify_bot.config import load_config
from apple_notify_bot.web import create_app


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    bot = AppleStockBot(config)
    app = create_app(bot)

    server_config = uvicorn.Config(
        app,
        host=os.environ.get("AIO_HOST", "127.0.0.1"),
        port=int(os.environ.get("AIO_PORT", "8080")),
        log_level="info",
    )
    server = uvicorn.Server(server_config)

    try:
        await asyncio.gather(bot.run_forever(), server.serve())
    finally:
        await bot.shutdown()
        bot.db.close()


if __name__ == "__main__":
    asyncio.run(main())
