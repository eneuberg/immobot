"""immobot: Fredy webhook -> Telegram follow-up with Claude-drafted application messages + dashboard."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import bot, db, is24, vault, web
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
    vault.init(config.session_secret, config.env_credentials())  # before anything reads a credential
    is24.init(config.data_dir)
    is24.cleanup_old_media()  # pictures of listings older than 60 days
    bot.init_public_url(config.public_url)
    if not config.disable_bot:
        await bot.start()
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
    max_age=web.SESSION_MAX_AGE_S,  # the server also enforces it (and a 24 h idle timeout), see web.is_logged_in
    same_site="strict",
    https_only=config.cookie_secure,
)
app.include_router(router)
