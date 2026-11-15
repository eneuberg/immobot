"""immobot: Fredy webhook -> Telegram follow-up with Claude-drafted application messages + dashboard."""

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import bot, db, editor, flow, fredy_alive, housekeeping, media, vault, web
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
    media.schedule_pending_pictures()
    dismissing = asyncio.create_task(housekeeping.loop())  # automatically aussortiert: uninteressant / veraltet
    # Fredy's liveness check (its MCP list_listings): ads gone from the portal are aussortiert "offline"
    alive = asyncio.create_task(fredy_alive.loop(housekeeping.apply_fredy_states, db.fredy_listing_ids))
    yield
    alive.cancel()
    dismissing.cancel()
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
    """Stylesheet and script of the dashboard: public, no secrets (CSP allows 'self'). Cacheable for a day, but
    only under this version's content hash (base.html adds ?v=<hash> to the URLs): during a deploy the old and the
    new container run side by side, and the old one may answer a request for the new hash - a browser must not
    keep that old file for a day under the new address. The security middleware keeps this Cache-Control and adds
    its other headers."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        version = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [""])[0]
        current = version == web.templates.env.globals["STATIC_VERSION"]
        response.headers["Cache-Control"] = "public, max-age=86400" if current else "no-store"
        return response


app.include_router(router)
app.include_router(editor.router)
app.include_router(flow.router)  # Bewerbungen, Stil
app.mount("/static", StaticAssets(directory=web.STATIC_DIR), name="static")
