"""immobot: Fredy webhook -> Telegram follow-up with Claude-drafted application messages + dashboard."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import bot, db, llm
from .config import load_config
from .web import router, security_headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO; Telegram URLs contain the bot token.
for noisy in ("httpx", "httpcore", "httpx2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

config = load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(config.data_dir)
    llm.configure(config.anthropic_api_key)
    if not config.disable_bot:
        await bot.start(config)
    yield
    if not config.disable_bot:
        await bot.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.config = config
app.middleware("http")(security_headers)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.session_secret,
    session_cookie="immobot_session",
    max_age=60 * 60 * 24 * 30,
    same_site="strict",
    https_only=config.cookie_secure,
)
app.include_router(router)
