"""Sends a real ImmoScout24 listing to immobot the way Fredy does (HTTP webhook), to try Telegram and the editor.

    .venv/bin/python scripts/test_listing.py https://immobot.example.com [--env-file FILE] [--expose URL]

Title, rent, size, address and description come from the exposé (IS24 app API, one request, like Fredy). The listing
keeps the exposé URL, so immobot itself loads all pictures and the full exposé text (setting "Bilder & Details selbst
von ImmoScout24 laden"), at the latest when the editor or a draft needs them. Each run is a new listing ("TEST" in
the title). The webhook token comes from FREDY_WEBHOOK_TOKEN or the env file and is never printed.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import is24  # noqa: E402

DEFAULT_EXPOSE = "https://www.immobilienscout24.de/expose/171725784"


def token_from(env_file: str | None) -> str:
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "FREDY_WEBHOOK_TOKEN":
                return value.strip().strip("'\"")
    return os.environ.get("FREDY_WEBHOOK_TOKEN", "")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def listing_from_expose(url: str, data: dict) -> dict:
    """The fields Fredy would send, read from the exposé sections."""
    fields = {"title": "", "price": "", "size": "", "address": "", "description": ""}
    for section in data.get("sections") or []:
        kind = section.get("type")
        if kind == "TITLE":
            fields["title"] = _clean(section.get("title"))
        elif kind == "TOP_ATTRIBUTES":
            for item in section.get("attributes") or []:
                label = _clean(item.get("label"))
                if label.startswith("Kaltmiete") and not fields["price"]:
                    fields["price"] = _clean(item.get("text"))
                elif label.startswith("Wohnfläche"):
                    fields["size"] = _clean(item.get("text"))
        elif kind == "MAP":
            fields["address"] = ", ".join(filter(None, [_clean(section.get("addressLine1")),
                                                        _clean(section.get("addressLine2"))]))
        elif kind == "TEXT_AREA" and not fields["description"]:
            fields["description"] = str(section.get("text") or "").replace("\xa0", " ").strip()[:500]
    fields["title"] = f"TEST {time.strftime('%d.%m. %H:%M')}: {fields['title'] or 'Inserat'}"
    return {"id": f"test-{is24.expose_id(url)}-{int(time.time())}", "url": url, **fields}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="immobot address, e.g. https://immobot.example.com")
    parser.add_argument("--env-file", help="file with a line FREDY_WEBHOOK_TOKEN=...")
    parser.add_argument("--expose", default=DEFAULT_EXPOSE, help="ImmoScout24 exposé URL")
    args = parser.parse_args()

    token = token_from(args.env_file)
    if not token:
        sys.exit("FREDY_WEBHOOK_TOKEN missing (env var or --env-file)")
    expose_id = is24.expose_id(args.expose)
    if expose_id is None:
        sys.exit("not an ImmoScout24 exposé URL: " + args.expose)
    try:
        data = asyncio.run(is24.fetch_expose(expose_id))
    except is24.Is24Error as exc:
        sys.exit(f"exposé: {exc}")
    item = listing_from_expose(args.expose, data)
    print(json.dumps({k: item[k] for k in ("title", "price", "size", "address")}, ensure_ascii=False))

    response = httpx.post(args.base.rstrip("/") + "/hook/fredy", timeout=30,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"provider": "immoscout", "jobId": None, "listings": [item]})
    print(response.status_code, response.text)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
