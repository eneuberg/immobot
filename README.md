# immobot

Kleiner Begleitdienst zu [Fredy](https://github.com/orangecoding/fredy). Unter jedes Fredy-Inserat in Telegram setzt immobot eine
Nachricht mit **✍️ Anschreiben** / **❌ Uninteressant**. Auf Knopfdruck schreibt Claude einen Anschreiben-Entwurf, den du
mit **📝 Bearbeiten** im Entwurfs-Editor fertig machst (ohne Editor-Button: **Neu / Kürzer / Förmlicher / Anpassen** direkt
in Telegram). Ein Dashboard (mit Passwort) zeigt Inserate, alle Entwürfe und die API-Kosten
und die Einstellungen in drei Reitern: **Profil** (Über mich, Wünsche, Stil, System-Prompt, Verlauf), **Verhalten**
(Denkaufwand, Budget, Fotos, Bot, Lernen) und **Zugänge** (Anbieter Anthropic oder DeutschlandGPT samt API-Key und Modell,
Telegram, Fredy).

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

4. Deployen, im Dashboard anmelden und unter **Einstellungen** eintragen:

   | Zugang (alle unter **Zugänge**) | Wert |
   |---|---|
   | Anthropic- **oder** DeutschlandGPT-API-Key | Pflicht ist nur der Key des gewählten Anbieters |
   | Telegram-Bot-Token | Pflicht: **derselbe** Token wie in Fredys Telegram-Adapter |
   | Telegram-Chat-ID | Pflicht: **dieselbe** Chat-ID wie in Fredys Telegram-Adapter – oder per Code verbinden (siehe unten) |
   | Erlaubte Telegram-User-IDs | Pflicht: wer im Chat die Buttons drücken und den 📝 Editor aus Telegram öffnen darf (kommagetrennt); trägt das Pairing automatisch ein. Leer = niemand |
   | Fredy-Webhook-Token | Pflicht: **Neu erzeugen** klicken, den einmal angezeigten Token in Fredys HTTP-Adapter eintragen |

   Jede Änderung verlangt dein Dashboard-Passwort. Danach mit **testen** prüfen. Fehlt noch etwas Pflichtiges, zeigt
   „Zugänge“ in der Navigation einen roten Punkt. Für den Button „📝 Bearbeiten“ einmal bei @BotFather `/setdomain` senden, den
   Bot wählen und die Domain des Dashboards eintragen (Anleitung unter Zugänge → Telegram).
5. `https://immobot.<domain>/status` sollte „Telegram-Bot läuft“ zeigen.

### Telegram-Chat per Code verbinden

Statt die Chat-ID herauszusuchen: unter Zugänge **Chat per Code verbinden** klicken (Bot-Token muss gespeichert sein und der
Bot laufen). Dann im gewünschten Chat genau den angezeigten Befehl `/pair@<dein_bot> CODE` schicken – mit `@<dein_bot>`, weil der
Bot in Gruppen sonst (Privacy-Modus) nichts sieht. Der Code gilt 10 Minuten und nach 5 falschen Versuchen nicht mehr. Gespeichert
wird nur ein HMAC-SHA256 des Codes (Schlüssel aus `SESSION_SECRET`).

Wer den Code schickt, landet auf der Liste der erlaubten Telegram-User. Das muss ein normales Konto sein (nicht anonymer
Gruppen-Admin, Kanal oder Bot), sonst lehnt der Bot ab und der Code bleibt gültig. Den Code also nicht in einer Gruppe stehen
lassen, in der andere mitlesen. Nach dem Pairing meldet der Bot im neuen Chat (und im bisherigen, falls es einen gab), welcher
Chat und welcher User verbunden wurde; dasselbe steht unter Zugänge und im Protokoll (Status).

### Umstieg von Env-Variablen (bestehende Installation)

Alte Installationen mit `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` und `FREDY_WEBHOOK_TOKEN` laufen ohne
Änderung weiter: Diese Variablen gelten als Fallback (ebenso `TELEGRAM_ALLOWED_USER_IDS`). In den Einstellungen steht dann „aus Env-Variable“. Den Wert dort eintragen
(der Dashboard-Wert hat Vorrang), mit „Verbindung testen“ prüfen und danach die Variable in Coolify löschen und neu deployen.
Beim Webhook-Token entweder den bisherigen Wert eintragen oder „Neu erzeugen“ und den neuen Token in Fredy hinterlegen.
Danach einmal **Chat per Code verbinden** ausführen (im bisherigen Chat), damit deine Telegram-User-ID auf die Liste der
erlaubten User kommt. Solange die Liste leer ist, kann niemand die Buttons nutzen (Hinweis unter Status); das Pairing
geht trotzdem.
Nach dem ersten Deploy mit Session-Timeouts musst du dich einmal neu anmelden.

### SESSION_SECRET nicht rotieren

Die Zugänge liegen AES-256-GCM-verschlüsselt in der SQLite-Datenbank; der Schlüssel wird per HKDF aus `SESSION_SECRET`
abgeleitet. Wird `SESSION_SECRET` geändert, sind alle im Dashboard gespeicherten Zugänge **nicht mehr entschlüsselbar**
(die Einstellungen zeigen „⚠ nicht entschlüsselbar – neu eintragen“) und müssen neu eingetragen werden. Außerdem werden alle Sessions
ungültig.

## Weitere Instanzen (Docker Compose)

Für eine zweite Person oder Suche: in Coolify **+ New → Public Repository** → dieses Repo, Build Pack **Docker Compose**
(Datei `docker-compose.yaml`). Coolify erzeugt `DASHBOARD_PASSWORD` und `SESSION_SECRET` beim ersten Deploy selbst und zeigt
sie unter *Environment Variables*. Unter *Domains* für den Dienst `immobot2` z. B. `https://immobot2.example.com:8000`
eintragen, deployen, einloggen und unter **Einstellungen** einen **eigenen** Telegram-Bot (nicht den von Instanz 1) und einen
Key eintragen. Weitere Instanz: den Block in `docker-compose.yaml` kopieren und `immobot2` → `immobot3` umbenennen.

## Einstellungen

- **Profil:** Über mich, Wünsche, Schreibstil mit Ziellänge, System-Prompt (eingeklappt) und ein Test-Entwurf. Jede
  Änderung dieser Felder wird eine **Version** (Speichern, „Standard wiederherstellen“, Übernahmen aus Rückmeldungen); die
  Meldung nennt die Nummer. Unter **Verlauf** zeigt „Ansehen“ den Unterschied einer Version zum aktuellen Stand pro Feld,
  „Wiederherstellen“ legt eine neue Version an. Versionen werden nie gelöscht.
- **Aus Rückmeldungen verbessern** (oben im Profil; möglich ab 3 Rückmeldungen oder 3 aktiven Regeln, bis etwa 10
  Rückmeldungen oder 8 Regeln nur eine unauffällige Zeile, danach eine Karte): Claude liest die gesammelte Kritik, die
  Endversionen aus dem Editor und die gelernten Regeln und schlägt kleine
  Änderungen am Profil vor. Das läuft im Hintergrund (bis zu ein paar Minuten, die Seite aktualisiert sich selbst). Jede
  Änderung erscheint als Diff in ihrem Feld mit Begründung und Belegen; du hakst an, was übernommen wird. Änderungen an
  „Über mich“ (Fakten) sind nie vorausgewählt. Verworfenes schlägt Claude nicht wieder vor. Ändert sich das Profil
  zwischendurch, ist der Vorschlag veraltet und wird neu erstellt.
- **Verhalten:** Denkaufwand, Monatsbudget und Wechselkurs, Fotos & Exposé, Verzögerung und Pause des Bots und die Karte
  **Lernen**: die Schalter „Endversionen als Stil-Beispiele nutzen“ und „Gelernte Regeln nutzen“, darunter die gelernten
  Regeln (an-/abschalten, bearbeiten, löschen, offene Vorschläge annehmen oder verwerfen, Verlauf; höchstens 25 aktive,
  ab 15 der Hinweis, sie ins Profil einzuarbeiten) und die genutzten Beispiele mit „Nicht mehr als Beispiel nutzen“
  (rückgängig zu machen; entfernte Beispiele stehen eingeklappt darunter mit „Wieder als Beispiel nutzen“).
- **Zugänge:** KI-Anbieter (Key, Modell, Test, Modellliste), Telegram (Token, Chat, erlaubte User, Links und
  `/setdomain`-Anleitung für den 📝 Editor), Fredy (Webhook-Token, Fredy-Adresse). Die alten Adressen `/settings/ki`,
  `/settings/telegram` und `/credentials` leiten hierher weiter.

Lern-Aufrufe (Regeln, Umschreiben) zählen wie Entwürfe zum Monatsbudget und zu den Kosten-Summen.

Begriffe, wie sie überall in immobot heißen:

- **Entwurf:** ein Anschreiben, das Claude schreibt (in Telegram, auf der Inseratsseite, im Editor).
- **Fassung:** ein Zwischenstand im 📝 Editor (Fassung 1 = Claudes Entwurf, jede Überarbeitung eine neue Fassung).
- **Endversion:** der Text nach „Fertig“ im Editor; er wird zum **Beispiel** für Claudes Stil.
- **Rückmeldung:** alles, woraus immobot lernt: „Kürzer“, „Förmlicher“, „Neu“, „Anpassen“, Kritik im Editor und
  geänderte Endversionen.
- **Regel:** ein kurzer Satz an Claude, den du nach „Fertig“ übernommen hast; gilt für jeden neuen Entwurf.
- **Version:** ein gespeicherter Stand deines Profils (Verlauf im Reiter Profil).

## KI-Anbieter: Anthropic oder DeutschlandGPT

Unter **Einstellungen → Zugänge** wählst du, wer die Entwürfe schreibt (den Denkaufwand unter **Verhalten**):

- **Anthropic** (Abrechnung in Dollar): direkt über die Claude API, mit Structured Outputs (JSON-Schema) und dem
  serverseitigen Refusal-Fallback (Opus 5.5, Opus 5, Sonnet 5.5, Fable 5.1).
- **DeutschlandGPT** (Abrechnung in Euro, netto): über dessen Anthropic-kompatiblen Endpunkt
  `https://api.deutschlandgpt.de/anthropic`. Dort wird das JSON-Schema von den meisten Modellen ignoriert. immobot bittet
  deshalb im System-Prompt um ein festes Format (`<<<ENTWURF>>>` / `<<<HINWEIS>>>`) und liest die Antwort tolerant: zuerst
  dieses Format, dann JSON, dann Markdown („**draft**“ …), zuletzt den ganzen Text als Entwurf. Fehlerhaft ist nur eine leere
  Antwort.

Wählst du im Feld „Anbieter“ einen Anbieter aus, zeigt der Abschnitt sofort dessen Key-Status (mit Kurzform wie
`sk-ant-…A1B2`), eine Warnung falls noch kein Key hinterlegt ist, das Formular zum Setzen oder Ersetzen des Keys („Key
speichern & Modelle laden“ bzw. „Entfernen“, mit Dashboard-Passwort), die Anleitung, einen Verbindungstest und die
Modellliste dieses Anbieters. Übernommen werden Anbieter und Modell mit „Anbieter & Modell speichern“. Ohne JavaScript stehen beide Anbieter untereinander. Angeboten werden die Modelle, die der Anbieter für deinen Key
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
   `*.immobilienscout24.de`. Abschaltbar unter Einstellungen → Verhalten → „Bilder & Details selbst von ImmoScout24 laden“; dann
   geht von keiner Stelle (Entwurf, „🖼 Bilder“, Dashboard) eine Anfrage an ImmoScout24. Ist Fredys Text schon lang
   (Detailseiten in Fredy aktiviert, über 500 Zeichen), wird das Exposé nicht nur für den Text abgerufen.
3. **Sonst** gibt es keine Galerie; Entwürfe bekommen wie bisher Fredys Einzelbild.

Heruntergeladen wird höflich: über eine Verbindung, ein Bild nach dem anderen mit kurzer Pause, keine Weiterleitungen,
höchstens 10 MB pro Bild; bei der ersten Ablehnung (403/429/kein Bild) ist Schluss, was da ist, bleibt. Gespeichert
wird als JPEG (max. 1024 px) unter `/data/media/<Inserat>/`; nach 60 Tagen löscht immobot die Bilder beim Start wieder.
Unter Status steht, ob der letzte Webhook Bilder enthielt („Fredy schickt Bilder mit: ja/nein“) und der letzte
Bilder-Fehler.

- **Entwürfe** bekommen den vollen Text, die Ansprechperson (ImmoScout24) und so viele Fotos, wie unter „Fotos für
  Claude pro Entwurf“ eingestellt (Einstellungen → Verhalten, 0–20, Standard 10). Jedes Foto kostet ca. 1.000 Tokens (≈ 0,1–4 ct je
  nach Modell) und ist beschriftet („Bild 1: Grundriss“); Grundrisse kommen zuerst.
- **Telegram:** „🖼 Bilder“ (bei Inseraten mit Galerie) schickt bis zu 10 Fotos als Album unter die immobot-Nachricht
  (einmal pro Inserat). Das ist Telegrams Grenze pro Album und hat mit der Einstellung für Claude nichts zu tun.
  „🔗 Dashboard“ öffnet die Inseratseite.
- **Dashboard:** Die Inseratseite zeigt alle Bilder mit ihrer Quelle („Bilder von Fredy“ / „von ImmoScout24 geladen“),
  Details und Ansprechperson. Solange nichts geladen ist oder etwas schiefging, gibt es „Bilder laden“ bzw.
  „Erneut versuchen“. Fehlversuche wiederholt immobot nicht selbst.

Die Adresse für „🔗 Dashboard“ merkt sich immobot aus deinen Dashboard-Aufrufen und aus Fredys Webhook. Fest einstellen
lässt sie sich mit der optionalen Env-Variable `IMMOBOT_PUBLIC_URL` (z. B. `https://immobot.example.com`). Adressen wie
`localhost` oder reine IP-Adressen nutzt immobot nicht, weil Telegram solche Links ablehnt.

## Fredy verbinden

Im Fredy-Job unter *Notification Adapter* **zusätzlich** zum Telegram-Adapter den **HTTP**-Adapter hinzufügen:

- Endpoint URL: `https://immobot.<domain>/hook/fredy`
- Auth Token: der Fredy-Webhook-Token (Einstellungen → Zugänge → **Neu erzeugen**; wird nur einmal angezeigt)

Optional: Mit der **Adresse deiner Fredy-Instanz** (Einstellungen → Zugänge, z. B. `https://fredy.example.com`) baut
immobot die Links „In Fredy öffnen“ selbst, etwa wenn Fredy umgezogen ist. Leer = die Links, die Fredy mitschickt.

Empfohlen: In Fredys Einstellungen die Detailseiten (`provider_details`) für deine Portale aktivieren. Sonst bekommt Claude nur den
kurzen Text aus der Suchliste. Nachteil: Fredy wird etwas leichter als Bot erkannt.

**Wichtig:** Ab jetzt nicht mehr manuell `getUpdates` mit dem Bot-Token aufrufen (Fredys Doku schlägt das für die Chat-ID vor).
Nur immobot darf Updates abholen.

## Entwurfs-Editor

Eine Seite pro Inserat (`/editor/<id>`), auf dem Handy gut bedienbar, zum Fertigmachen eines Entwurfs:

- Text von Hand ändern, mit „Kopieren“ und „Speichern“; „Änderungen zum Original“ zeigt den Wort-Diff zu Claudes Entwurf.
- „Was soll Claude ändern?“: Claude überarbeitet den aktuellen Text samt deiner Änderungen (beliebig oft, mit Verlauf).
  Ohne Tippen gehen „Kürzer“ und „Förmlicher“ (wie die Telegram-Buttons) und „Ganz neu schreiben“: ein neuer Entwurf,
  mit dem eine neue Runde beginnt (fragt vorher, wenn du den Text geändert hast).
- „Fertig“ speichert die Endversion. Sie dient künftig als Stil-Beispiel, und Claude schlägt (wenn „Gelernte Regeln
  nutzen“ an ist) bis zu 3 Regeln für künftige Entwürfe vor, die du übernimmst, umformulierst oder verwirfst. Steht beim
  „Fertig“ noch eine nicht geschickte Kritik im Feld, wird sie als Rückmeldung gespeichert und fließt in die
  Regelvorschläge ein. Fehlen Vorschläge (z. B. nach einem Neustart), gibt es „Regeln vorschlagen lassen“.
- Danach: Kopieren, „Inserat öffnen“, „Als gesendet markieren“ (ändert auch die Telegram-Nachricht), „Weiter bearbeiten“.
- Monatsbudget und höchstens 30 Claude-Aufrufe pro Inserat und Tag gelten auch hier.

Aufrufen lässt er sich über „📝 Im Editor bearbeiten“ auf der Inseratsseite (mit Dashboard-Login) oder über den Button
„📝 Bearbeiten“ unter einem Entwurf in Telegram, ohne Dashboard-Passwort:

1. **Einmalig:** Bei [@BotFather](https://t.me/BotFather) `/setdomain` senden, den Bot wählen und die Domain des
   Dashboards eintragen (z. B. `immobot.example.com`, ohne `https://`). Das ist je Bot nötig, also auch für jede weitere
   Instanz mit eigenem Bot. Außerdem müssen unter Einstellungen → Zugänge die „Erlaubten Telegram-User-IDs“ gesetzt sein
   (das Pairing trägt dich ein).
2. Der Button ist ein Telegram-Login-Button: Telegram bestätigt beim Tippen, wer du bist. immobot prüft den signierten
   Link (7 Tage gültig), die Telegram-Anmeldung (höchstens 10 Minuten alt, nur einmal verwendbar) und die erlaubten
   User-IDs. Kopierte oder weitergeleitete Links funktionieren deshalb nicht.
3. Danach gilt der Editor **in diesem Browser für dieses eine Inserat 24 Stunden** ab dem ersten Tippen (Cookie
   `immobot_editor`). Einstellungen, andere Inserate und die Regel-Liste bleiben hinter dem Dashboard-Login. Ist die Zeit
   um, kommt „Sitzung abgelaufen – bitte anmelden“; mit dem Dashboard-Login geht es auf derselben Seite weiter.

Die Telegram-Nachricht unter einem Entwurf hat zwei Formen:

- **Editor zuerst** (sobald der Button geht): „📝 Bearbeiten“ und „📨 Inserat öffnen“, „✅ Gesendet“ / „❌ Uninteressant“,
  „🖼 Bilder“ / „🔗 Dashboard“. Kürzer, förmlicher, neu und eigene Wünsche gibt es im Editor.
- **Ohne Editor** (Rückfall): „🔁 Neu“ / „✂️ Kürzer“ / „🎩 Förmlicher“, „✏️ Anpassen“ (Wunsch als Antwort schreiben) und
  „📨 Inserat öffnen“, dazu Gesendet / Uninteressant und Bilder / Dashboard. Die Buttons älterer Nachrichten gehen weiter.

„✍️ Anschreiben“ schreibt nur, wenn es noch keinen Entwurf gibt; sonst zeigt die Nachricht den vorhandenen (oder die
Endversion aus dem Editor). Ein Entwurf von der Inseratsseite oder aus dem Editor aktualisiert die Nachricht gleich mit.

Lehnt Telegram den Button ab (meist: Domain nicht per `/setdomain` verknüpft), schickt immobot die Nachricht ohne Editor
und zeigt auf der Status- und der Zugänge-Seite einen Hinweis. immobot versucht den Button weiter; sobald die Domain
verknüpft ist, gilt wieder „Editor zuerst“. Ohne bekannte öffentliche Adresse (`IMMOBOT_PUBLIC_URL` oder ein Aufruf
des Dashboards über die Domain) gibt es den Button nicht.

## Sicherheit

- Env-Variablen sind nur noch `DASHBOARD_PASSWORD` und `SESSION_SECRET`.
- API-Keys und Tokens werden unter Einstellungen → Zugänge eingetragen und nur verschlüsselt
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
  Jede Änderung landet im Protokoll (Status) und wird in Telegram gemeldet, und zwar an den Chat, der **vor**
  der Änderung eingestellt war.
- Fehlgeschlagene Logins (Login-Seite und Passwort-Abfrage in den Einstellungen) werden pro IP gezählt, IPv6 pro /64. Nach 3
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
- Der Bot reagiert nur auf den eingestellten Chat. Dort dürfen nur die erlaubten Telegram-User Buttons drücken und
  antworten. Andere bekommen beim Button „Nur für den Besitzer dieses Bots.“, alles andere wird ignoriert. Ist die Liste
  leer, darf niemand (ein Button-Druck sagt, wie man sie füllt); ist sie nicht entschlüsselbar (`SESSION_SECRET`
  geändert), ebenso. Das gilt, bis sie eingetragen oder ein Pairing gemacht wurde.
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
# API-Key usw. dann unter http://127.0.0.1:8000/settings/zugaenge eintragen.
```
