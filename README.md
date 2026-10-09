# immobot

Kleiner Begleitdienst zu [Fredy](https://github.com/orangecoding/fredy). Unter jedes Fredy-Inserat in Telegram setzt immobot eine
Nachricht mit **✍️ Anschreiben** / **❌ Uninteressant**. Auf Knopfdruck schreibt Claude einen Anschreiben-Entwurf, mit
**Neu / Kürzer / Förmlicher / Anpassen**. Ein Dashboard (mit Passwort) zeigt Inserate, alle Entwürfe und die API-Kosten
und enthält Profil, System-Prompt, Modell und Budget.

Gesendet wird **nie automatisch**. Du kopierst den Entwurf und schickst ihn im Portal selbst ab.

```
Fredy ──Telegram-Adapter──▶ Telegram  (Inserat wie bisher)
  └────HTTP-Adapter──▶ immobot /hook/fredy ──▶ Telegram (Buttons, Entwurf)  ◀── Dashboard
                              └──▶ Claude API
```

## Setup in Coolify

1. **Neue Application** aus dem GitHub-Repo, Build Pack **Dockerfile**, Port **8000** (Coolify übernimmt ihn aus `EXPOSE`), Domain z. B. `https://immobot.fentreactor.de`.
2. **Persistent Storage:** Volume `immobot-data` → `/data` (SQLite-Datenbank).
3. **Environment Variables** (alle als Secret markieren):

   | Variable | Wert |
   |---|---|
   | `ANTHROPIC_API_KEY` | API-Key von console.anthropic.com |
   | `TELEGRAM_BOT_TOKEN` | **derselbe** Token wie in Fredys Telegram-Adapter |
   | `TELEGRAM_CHAT_ID` | **dieselbe** Chat-ID wie in Fredys Telegram-Adapter |
   | `FREDY_WEBHOOK_TOKEN` | zufällig, `openssl rand -hex 32` |
   | `DASHBOARD_PASSWORD` | dein Dashboard-Passwort (lang) |
   | `SESSION_SECRET` | zufällig, `openssl rand -hex 32` |

4. Deployen. `https://immobot.<domain>/status` sollte „Telegram läuft“ zeigen.

## Fredy verbinden

Im Fredy-Job unter *Notification Adapter* **zusätzlich** zum Telegram-Adapter den **HTTP**-Adapter hinzufügen:

- Endpoint URL: `https://immobot.<domain>/hook/fredy`
- Auth Token: der Wert von `FREDY_WEBHOOK_TOKEN`

Empfohlen: In Fredys Einstellungen die Detailseiten (`provider_details`) für deine Portale aktivieren. Sonst bekommt Claude nur den
kurzen Text aus der Suchliste. Nachteil: Fredy wird etwas leichter als Bot erkannt.

**Wichtig:** Ab jetzt nicht mehr manuell `getUpdates` mit dem Bot-Token aufrufen (Fredys Doku schlägt das für die Chat-ID vor).
Nur immobot darf Updates abholen.

## Sicherheit

- Die Secrets existieren nur als Env-Variablen. Sie werden nie in der DB gespeichert, nie im Dashboard angezeigt und nie geloggt.
- Das Dashboard ist komplett hinter Login (Session-Cookie `HttpOnly`, `Secure`, `SameSite=Strict`, Login-Rate-Limit).
- `/hook/fredy` verlangt den Bearer-Token. `/healthz` ist offen, gibt aber nichts preis.
- Der Bot reagiert nur auf `TELEGRAM_CHAT_ID`.

## Lokal entwickeln

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
IMMOBOT_DISABLE_BOT=1 COOKIE_SECURE=0 DATA_DIR=./data ANTHROPIC_API_KEY=... FREDY_WEBHOOK_TOKEN=dev \
  DASHBOARD_PASSWORD=dev SESSION_SECRET=$(openssl rand -hex 32) .venv/bin/uvicorn app.main:app --reload
```
