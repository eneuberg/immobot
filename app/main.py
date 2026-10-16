"""immobot: Fredy webhook -> Telegram follow-up with Claude-drafted application messages + dashboard."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import bot, db, editor, media, vault, web
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
    media.init(config.data_dir)
    media.cleanup_old_media()  # pictures of listings older than 60 days
    bot.init_public_url(config.public_url)
    if not config.disable_bot:
        await bot.start()
    # older ImmoScout24 listings without exposé text (warm rent): fetched slowly, their messages updated
    media.schedule_backfill(on_done=None if config.disable_bot else bot.schedule_refresh)
    yield
    if not config.disable_bot:
        await bot.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.config = config
app.middleware("http")(security_headers)
app.middleware("http")(editor.editor_headers)  # added later = runs outside security_headers, so its headers win
app.add_middleware(
    SessionMiddleware,
    secret_key=config.session_secret,
    session_cookie="immobot_session",
    max_age=web.SESSION_MAX_AGE_S,  # the server also enforces it (and a 24 h idle timeout), see web.is_logged_in
    same_site="strict",
    https_only=config.cookie_secure,
)


class StaticAssets(StaticFiles):
    """Stylesheet and script of the dashboard: public, no secrets (CSP allows 'self'). Cacheable for a day;
    base.html adds a content hash to the URLs, so a deploy is picked up at once. The security middleware
    keeps this Cache-Control and adds its other headers."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response


app.include_router(router)
app.include_router(editor.router)
app.mount("/static", StaticAssets(directory=web.STATIC_DIR), name="static")
