# immobot

Kleiner Begleitdienst zu [Fredy](https://github.com/orangecoding/fredy). Unter jedes Fredy-Inserat in Telegram setzt immobot eine
Nachricht mit **✍️ Anschreiben** / **❌ Uninteressant**. Auf Knopfdruck schreibt Claude einen Anschreiben-Entwurf, mit
**Neu / Kürzer / Förmlicher / Anpassen**. Ein Dashboard (mit Passwort) zeigt Inserate, alle Entwürfe und die API-Kosten
und enthält Profil, System-Prompt, KI-Anbieter (Anthropic oder DeutschlandGPT), Modell, Budget und die Zugänge (API-Keys,
Bot-Token, Chat-ID, Webhook-Token).

Gesendet wird **nie automatisch**. Du kopierst den Entwurf und schickst ihn im Portal selbst ab.

```
Fredy ──Telegram-Adapter──▶ Telegram  (Inserat wie bisher)
  └────HTTP-Adapter──▶ immobot /hook/fredy ──▶ Telegram (Buttons, Entwurf)  ◀── Dashboard
                              └──▶ Claude API
```

## Setup in Coolify

1. **Neue Application** aus dem GitHub-Repo, Build Pack **Dockerfile**, Port **8000** (Coolify übernimmt ihn aus `EXPOSE`), Domain z. B. `https://immobot.example.com`.
2. **Persistent Storage:** Volume `immobot-data` → `/data` (SQLite-Datenbank).
3. **Environment Variables** – nur noch zwei, beide als Secret markieren:

   | Variable | Wert |
   |---|---|
   | `DASHBOARD_PASSWORD` | dein Dashboard-Passwort (lang) |
   | `SESSION_SECRET` | zufällig, `openssl rand -hex 32` (mind. 32 Zeichen). **Nicht mehr ändern**, siehe unten. |

4. Deployen, im Dashboard anmelden und unter **Zugänge** (`/credentials`) eintragen:

   | Zugang | Wert |
   |---|---|
   | Anthropic-API-Key | von console.anthropic.com (`sk-ant-…`) – oder stattdessen: |
   | DeutschlandGPT-API-Key | aus dem DeutschlandGPT-Dashboard; Anbieter unter Einstellungen → KI-Anbieter wählen |
   | Telegram-Bot-Token | **derselbe** Token wie in Fredys Telegram-Adapter |
   | Telegram-Chat-ID | **dieselbe** Chat-ID wie in Fredys Telegram-Adapter – oder per Code verbinden (siehe unten) |
   | Erlaubte Telegram-User-IDs | wer im Chat Buttons drücken darf (kommagetrennt); trägt das Pairing automatisch ein |
   | Fredy-Webhook-Token | **Neu erzeugen** klicken, den einmal angezeigten Token in Fredys HTTP-Adapter eintragen |

   Jede Änderung verlangt dein Dashboard-Passwort. Danach mit **Verbindung testen** prüfen.
5. `https://immobot.<domain>/status` sollte „Telegram-Bot läuft“ zeigen.

### Telegram-Chat per Code verbinden

Statt die Chat-ID herauszusuchen: unter Zugänge **Chat per Code verbinden** klicken (Bot-Token muss gespeichert sein und der
Bot laufen). Dann im gewünschten Chat genau den angezeigten Befehl `/pair@<dein_bot> CODE` schicken – mit `@<dein_bot>`, weil der
Bot in Gruppen sonst (Privacy-Modus) nichts sieht. Der Code gilt 10 Minuten und nach 5 falschen Versuchen nicht mehr. Gespeichert
wird nur ein HMAC-SHA256 des Codes (Schlüssel aus `SESSION_SECRET`).

Wer den Code schickt, landet auf der Liste der erlaubten Telegram-User. Das muss ein normales Konto sein (nicht anonymer
Gruppen-Admin, Kanal oder Bot), sonst lehnt der Bot ab und der Code bleibt gültig. Den Code also nicht in einer Gruppe stehen
lassen, in der andere mitlesen. Nach dem Pairing meldet der Bot im neuen Chat (und im bisherigen, falls es einen gab), welcher
Chat und welcher User verbunden wurde; dasselbe steht unter Zugänge und im Protokoll.

### Umstieg von Env-Variablen (bestehende Installation)

Alte Installationen mit `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` und `FREDY_WEBHOOK_TOKEN` laufen ohne
Änderung weiter: Diese Variablen gelten als Fallback (ebenso das optionale `TELEGRAM_ALLOWED_USER_IDS`). Unter Zugänge steht dann „aus Env-Variable“. Den Wert dort eintragen
(der Dashboard-Wert hat Vorrang), mit „Verbindung testen“ prüfen und danach die Variable in Coolify löschen und neu deployen.
Beim Webhook-Token entweder den bisherigen Wert eintragen oder „Neu erzeugen“ und den neuen Token in Fredy hinterlegen.
Danach einmal **Chat per Code verbinden** ausführen (im bisherigen Chat), damit deine Telegram-User-ID auf die Liste der
erlaubten User kommt. Solange die Liste leer ist, darf jedes Mitglied des Chats Buttons drücken (Warnung unter Zugänge und Status).
Nach dem ersten Deploy mit Session-Timeouts musst du dich einmal neu anmelden.

### SESSION_SECRET nicht rotieren

Die Zugänge liegen AES-256-GCM-verschlüsselt in der SQLite-Datenbank; der Schlüssel wird per HKDF aus `SESSION_SECRET`
abgeleitet. Wird `SESSION_SECRET` geändert, sind alle im Dashboard gespeicherten Zugänge **nicht mehr entschlüsselbar**
(Zugänge zeigt „⚠ nicht entschlüsselbar – neu eintragen“) und müssen neu eingetragen werden. Außerdem werden alle Sessions
ungültig.

## KI-Anbieter: Anthropic oder DeutschlandGPT

Unter **Einstellungen → KI-Anbieter** wählst du, wer die Entwürfe schreibt:

- **Anthropic** (Abrechnung in Dollar): direkt über die Claude API, mit Structured Outputs (JSON-Schema) und dem
  serverseitigen Refusal-Fallback (Opus 5.5, Opus 5, Sonnet 5.5, Fable 5.1).
- **DeutschlandGPT** (Abrechnung in Euro, netto): über dessen Anthropic-kompatiblen Endpunkt
  `https://api.deutschlandgpt.de/anthropic`. Dort wird das JSON-Schema von den meisten Modellen ignoriert. immobot bittet
  deshalb im System-Prompt um ein festes Format (`<<<ENTWURF>>>` / `<<<HINWEIS>>>`) und liest die Antwort tolerant: zuerst
  dieses Format, dann JSON, dann Markdown („**draft**“ …), zuletzt den ganzen Text als Entwurf. Fehlerhaft ist nur eine leere
  Antwort.

Wählst du im Feld „Anbieter“ einen Anbieter aus, zeigt der Abschnitt sofort dessen Key-Status (mit Kurzform wie
`sk-ant-…A1B2`), eine Warnung falls noch kein Key hinterlegt ist, das Formular zum Setzen oder Ersetzen des Keys („Key
speichern & Modelle laden“, mit Dashboard-Passwort, wie unter Zugänge), die Anleitung und die Modellliste dieses
Anbieters. Übernommen werden Anbieter und Modell mit einem Klick auf „Anbieter & Modell speichern“ (oder dem normalen
„Speichern“ unten). Ohne JavaScript stehen beide Anbieter untereinander. Angeboten werden die Modelle, die der Anbieter für deinen Key
live auflistet **und** für die immobot einen Preis kennt. Die Preistabellen stehen fest in `app/settings.py` (Anthropic:
Listenpreise, DeutschlandGPT: Preise laut API, Stand 2026-10-12). Weitere Modelle des Anbieters werden nur als „verfügbar,
aber nicht unterstützt“ aufgeführt. Konnte die Liste nicht geladen werden, zeigt die Seite alle Modelle mit Preis und
„Modellliste nicht geprüft“. „Modelle neu laden“ holt die Liste erneut. Die Preise in der Modellauswahl stehen in der
Währung des Anbieters ($ bzw. € pro 1 Mio. Tokens). Ohne Auswahl gilt das Standardmodell des Anbieters (Anthropic: Opus 5.5,
DeutschlandGPT: Sonnet 5.5).

Kosten: Jeder Entwurf speichert Anbieter, Modell, Tokens und die Kosten in der Währung des Anbieters. Angezeigt und
gerechnet wird in **Euro**: Monatsbudget (Einstellungen, in €), Summen, Inserate, Entwürfe, Telegram und Status.
Dollar-Kosten (Anthropic) werden mit dem „Wechselkurs: 1 € = x $“ (Standard 1,10) umgerechnet; beim Entwurf im Dashboard
steht dann zusätzlich der Dollarbetrag, z. B. „3,8 ct (≈ 0,042 $)“. DeutschlandGPT-Kosten sind bereits in Euro.

## Bildquellen

Fredys normaler Webhook liefert pro Inserat nur ein Bild (`imageUrl`) und einen kurzen Text. immobot nimmt für die
Bildergalerie die erste passende Quelle:

1. **Fredy schickt die Bilder mit**: Mit dem Fredy-PR [#489](https://github.com/orangecoding/fredy/pull/489) (oder
   einem so gepatchten Fredy) enthält jedes Inserat im Webhook optional `images: [{url, caption?, floorPlan?}]`.
   immobot speichert diese Adressen beim Eingang und lädt die Bilder erst bei Bedarf. ImmoScout24 wird dann **nie**
   zusätzlich abgefragt. Angenommen werden nur https-Adressen mit echtem Domainnamen (keine IP-Adressen, kein
   `localhost`, `.local` oder `.internal`, keine Zugangsdaten in der URL, höchstens 2000 Zeichen); Bildunterschriften
   werden auf 200 Zeichen gekürzt, ungültige Einträge stillschweigend verworfen (der Webhook schlägt deshalb nie fehl).
   Kommt ein Inserat erneut, bleiben die gespeicherten Bilder; bringt ein späterer Webhook Bilder für ein Inserat ohne
   Bilder, werden sie übernommen.
2. **immobot holt sie selbst von ImmoScout24**: Ohne Fredy-Bilder holt immobot bei ImmoScout24-Inseraten
   (nicht Österreich) beim ersten Bedarf einmal das Exposé (dieselbe App-API, die Fredy nutzt): Details-Text,
   Ansprechperson und alle Bilder, nur in der Größe, die ImmoScout24 selbst ausliefert, nur von
   `*.immobilienscout24.de`. Abschaltbar unter Einstellungen → „Bilder & Details selbst von ImmoScout24 laden“; dann
   geht von keiner Stelle (Entwurf, „🖼 Bilder“, Dashboard) eine Anfrage an ImmoScout24. Ist Fredys Text schon lang
   (Detailseiten in Fredy aktiviert, über 500 Zeichen), wird das Exposé nicht nur für den Text abgerufen.
3. **Sonst** gibt es keine Galerie; Entwürfe bekommen wie bisher Fredys Einzelbild.

Heruntergeladen wird höflich: über eine Verbindung, ein Bild nach dem anderen mit kurzer Pause, keine Weiterleitungen,
höchstens 10 MB pro Bild; bei der ersten Ablehnung (403/429/kein Bild) ist Schluss, was da ist, bleibt. Gespeichert
wird als JPEG (max. 1024 px) unter `/data/media/<Inserat>/`; nach 60 Tagen löscht immobot die Bilder beim Start wieder.
Unter Status steht, ob der letzte Webhook Bilder enthielt („Fredy schickt Bilder mit: ja/nein“) und der letzte
Bilder-Fehler.

- **Entwürfe** bekommen den vollen Text, die Ansprechperson (ImmoScout24) und so viele Fotos, wie unter „Fotos für
  Claude pro Entwurf“ eingestellt (Einstellungen, 0–20, Standard 10). Jedes Foto kostet ca. 1.000 Tokens (≈ 0,1–4 ct je
  nach Modell) und ist beschriftet („Bild 1: Grundriss“); Grundrisse kommen zuerst.
- **Telegram:** „🖼 Bilder“ (bei Inseraten mit Galerie) schickt bis zu 10 Fotos als Album unter die immobot-Nachricht
  (einmal pro Inserat). Das ist Telegrams Grenze pro Album und hat mit der Einstellung für Claude nichts zu tun.
  „🔗 Dashboard“ öffnet die Inseratseite.
- **Dashboard:** Die Inseratseite zeigt alle Bilder mit ihrer Quelle („Bilder von Fredy“ / „von ImmoScout24 geladen“),
  Details und Ansprechperson. Solange nichts geladen ist oder etwas schiefging, gibt es „Bilder laden“ bzw.
  „Erneut versuchen“. Fehlversuche wiederholt immobot nicht selbst.

Die Adresse für „🔗 Dashboard“ merkt sich immobot aus deinen Dashboard-Aufrufen und aus Fredys Webhook. Fest einstellen
lässt sie sich mit der optionalen Env-Variable `IMMOBOT_PUBLIC_URL` (z. B. `https://immobot.example.de`). Adressen wie
`localhost` oder reine IP-Adressen nutzt immobot nicht, weil Telegram solche Links ablehnt.

## Fredy verbinden

Im Fredy-Job unter *Notification Adapter* **zusätzlich** zum Telegram-Adapter den **HTTP**-Adapter hinzufügen:

- Endpoint URL: `https://immobot.<domain>/hook/fredy`
- Auth Token: der Fredy-Webhook-Token (Zugänge → **Neu erzeugen**; wird nur einmal angezeigt)

Empfohlen: In Fredys Einstellungen die Detailseiten (`provider_details`) für deine Portale aktivieren. Sonst bekommt Claude nur den
kurzen Text aus der Suchliste. Nachteil: Fredy wird etwas leichter als Bot erkannt.

**Wichtig:** Ab jetzt nicht mehr manuell `getUpdates` mit dem Bot-Token aufrufen (Fredys Doku schlägt das für die Chat-ID vor).
Nur immobot darf Updates abholen.

## Sicherheit

- Env-Variablen sind nur noch `DASHBOARD_PASSWORD` und `SESSION_SECRET`.
- API-Keys und Tokens werden unter Zugänge (der KI-Key auch unter Einstellungen) eingetragen und nur verschlüsselt
  gespeichert (AES-256-GCM, Schlüssel aus `SESSION_SECRET`, Name als Associated Data). Eine Kopie der Datenbank allein
  verrät sie nicht.
- Zum Wiedererkennen zeigt das Dashboard nur einen kurzen Hinweis: `sk-ant-…A1B2` (Anthropic), `123456:…A1B2` (Bot-Token)
  oder die ersten und letzten 4 Zeichen (`abcd…wxyz`), bei Werten unter 16 Zeichen nur `…` und die letzten 2. Dieser
  Hinweis wird beim Speichern mit abgelegt, damit er auch nach einem `SESSION_SECRET`-Wechsel noch zeigt, welcher Key es war.
- Kein Wert wird je wieder angezeigt oder geloggt; die Eingabefelder sind immer leer (und keine Passwortfelder, damit der
  Passwort-Manager den API-Key nicht als Website-Passwort speichert). Ausnahme: ein neu erzeugter Webhook-Token erscheint
  genau einmal, auf der Seite nach der Weiterleitung. Bis dahin liegt er nur im Arbeitsspeicher, unter einer zufälligen
  Einmal-ID, höchstens 5 Minuten. Neuladen zeigt ihn nicht noch einmal und erzeugt auch keinen neuen.
- Jede Änderung braucht das Dashboard-Passwort erneut. Falsche Eingaben zählen zum Login-Limit (5 pro 5 Minuten und IP).
  Jede Änderung landet im Protokoll (Zugänge und Status) und wird in Telegram gemeldet, und zwar an den Chat, der **vor**
  der Änderung eingestellt war.
- Fehlgeschlagene Logins (Login-Seite und Passwort-Abfrage unter Zugänge) werden pro IP gezählt, IPv6 pro /64. Nach 3
  Fehlversuchen kommt eine Warnung in Telegram, nach 5 ist die IP für 5 Minuten gesperrt (ebenfalls mit Warnung). Bei 20
  Fehlversuchen von verschiedenen IPs innerhalb von 10 Minuten kommt eine Sammelwarnung; gesperrt wird dann aber niemand
  global, damit du dich weiter anmelden kannst. Jede Art von Warnung kommt höchstens einmal pro 10 Minuten, nie mit dem
  eingegebenen Passwort. Alles steht im Protokoll.
- Das Dashboard ist komplett hinter Login (Session-Cookie `HttpOnly`, `Secure`, `SameSite=Strict`, Login-Rate-Limit, HSTS).
  Eine Session endet 7 Tage nach dem Login oder nach 24 Stunden ohne Aufruf, beim Logout (überall) und wenn
  `DASHBOARD_PASSWORD` oder `SESSION_SECRET` geändert wird. Nur ein angemeldeter Logout beendet alle Sessions.
- uvicorn glaubt `X-Forwarded-For` nur von privaten Adressen (dem Reverse Proxy im Docker-Netz). So kann niemand seine IP
  fälschen, um das Login-Limit zu umgehen.
- `/hook/fredy` verlangt den Bearer-Token (ohne gespeicherten Token: 503). `/healthz` ist offen, gibt aber nichts preis.
- Der Bot reagiert nur auf den eingestellten Chat. Ist die Liste erlaubter Telegram-User gefüllt, dürfen dort nur diese
  User Buttons drücken und antworten. Andere bekommen beim Button „Nur für den Besitzer dieses Bots.“, alles andere wird
  ignoriert. Ist die gespeicherte Liste nicht entschlüsselbar (`SESSION_SECRET` geändert), darf niemand, bis sie neu
  eingetragen oder ein Pairing gemacht wurde.
- `/pair` wird komplett vorab geprüft und erreicht nie die übrigen Bot-Funktionen. Fremde Chats bekommen auf falsche Codes
  keine Antwort.
- Ein neuer Bot-Token startet den Bot im Hintergrund neu; das Speichern wartet nicht darauf. Eine neue Chat-ID gilt sofort,
  ohne Neustart.

## Lokal entwickeln

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
IMMOBOT_DISABLE_BOT=1 COOKIE_SECURE=0 DATA_DIR=./data DASHBOARD_PASSWORD=dev \
  SESSION_SECRET=<fester Wert, mind. 32 Zeichen> .venv/bin/uvicorn app.main:app --reload
# API-Key usw. dann unter http://127.0.0.1:8000/credentials eintragen.
```
