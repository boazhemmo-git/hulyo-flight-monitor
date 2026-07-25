"""Hülyo flight-availability monitor.

Polls hulyo.co.il's public flight catalog JSON (no login) and pushes a
Telegram alert when flights to a watched destination (Amsterdam / Nice)
become available — with dates, times, prices, and a direct deal link.

The site serves static per-category catalogs at
``/dynamic/client/flights-*.json``. Each contains:

* ``destinations``    IATA → {destination (Hebrew name), countryName, ...}
* ``catalogItems``    the destinations that currently HAVE active deals,
                      each with ``relatedProducts`` (date group → productIds)
* ``productOverrides`` productId → {fromDate, sellingPrice, availableSeats,
                      legRefs → segmentIds, ...}
* ``segmentMap``      segmentId → {departureIATA, arrivalIATA, date, depTime,
                      arrTime, airline, flightNumber, ...}

A destination missing from ``catalogItems`` has no active deals — so a
watched destination appearing there is exactly the "became available" event.

Runs as a one-shot cycle (``--once``, the default when invoked from CI) —
designed to be triggered on a schedule (Windows Task Scheduler locally, or a
GitHub Actions cron job) rather than looping forever. Telegram credentials
come from the ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` environment
variables if set, else from ``config.json`` (kept out of the public repo).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
SEEN_PATH = PROJECT_DIR / "seen_deals.json"
LOG_PATH = PROJECT_DIR / "hulyo_monitor.log"
STATUS_PATH = PROJECT_DIR / "status.json"
OFFSET_PATH = PROJECT_DIR / "telegram_offset.json"

# Text that triggers a reply with the current offering (not just new deals).
LIST_COMMANDS = {"list", "status", "current"}

log = logging.getLogger("hulyo")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Deal:
    """One bookable flight product for a watched destination."""

    product_id: str
    iata: str
    dest_name: str
    from_date: str  # e.g. "2026-07-03T04:45"
    price: Optional[int]
    currency: str
    seats: Optional[int]
    deal_type: str
    segments: tuple[str, ...]
    url: str

    @property
    def price_key(self) -> str:
        """Identity for dedup: alert again if price or date changes."""
        return f"{self.product_id}|{self.price}"

    def format(self) -> str:
        price = f"${self.price}" if self.price is not None else "price N/A"
        seats = f", {self.seats} seats" if self.seats else ""
        head = f"• {_fmt_date(self.from_date)} — from {price}{seats}"
        legs = "\n".join(f"    {s}" for s in self.segments)
        return f"{head}\n{legs}\n    🔗 {self.url}"


# --------------------------------------------------------------------------- #
# Telegram (send + a light poll for the "list current offering" command)
# --------------------------------------------------------------------------- #

class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session = requests.Session()

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        # Telegram hard-caps messages at 4096 chars.
        for chunk in _chunk(text, 4000):
            try:
                resp = self._session.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException:
                log.exception("Telegram send failed")

    def get_updates(self, offset: Optional[int]) -> list[dict[str, Any]]:
        """Non-blocking poll (timeout=0) — this bot is dedicated to Hulyo, so
        there's no other consumer of its inbox to race with."""
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        params: dict[str, Any] = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json().get("result", [])
        except (requests.RequestException, ValueError):
            log.warning("Telegram getUpdates failed", exc_info=True)
            return []


# --------------------------------------------------------------------------- #
# Catalog fetching & resolution
# --------------------------------------------------------------------------- #

def fetch_catalog(base_url: str, fname: str) -> Optional[dict[str, Any]]:
    try:
        resp = requests.get(base_url + fname, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        log.warning("Failed to fetch %s", fname, exc_info=True)
        return None


def resolve_deals(
    data: dict[str, Any],
    iata: str,
    dest_name_override: str,
    url_template: str,
) -> list[Deal]:
    """Extract all active deals for `iata` from one catalog file."""
    catalog_items = data.get("catalogItems", [])
    overrides = data.get("productOverrides", {})
    segment_map = data.get("segmentMap", {})
    destinations = data.get("destinations", {})

    item = next((c for c in catalog_items if c.get("destinationIata") == iata), None)
    if item is None:
        return []

    dest_name = (
        destinations.get(iata, {}).get("destination") or dest_name_override or iata
    )
    product_ids: list[str] = []
    for group in item.get("relatedProducts", []):
        product_ids.extend(group.get("productsIds", []))

    deals: list[Deal] = []
    seen_ids: set[str] = set()
    for pid in product_ids:
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        ov = overrides.get(pid)
        if not ov:
            continue
        segments = _resolve_segments(ov, segment_map)
        price_obj = ov.get("sellingPrice") or {}
        deals.append(Deal(
            product_id=pid,
            iata=iata,
            dest_name=f"{dest_name_override or dest_name}",
            from_date=ov.get("fromDate", ""),
            price=price_obj.get("adult"),
            currency=price_obj.get("currency", "USD"),
            seats=ov.get("availableSeats"),
            deal_type=item.get("dealType", "Flights"),
            segments=tuple(segments),
            url=url_template.format(product_id=pid),
        ))
    return deals


def _resolve_segments(ov: dict[str, Any], segment_map: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for leg in ov.get("legRefs", []):
        for sid in leg.get("segmentIds", []):
            s = segment_map.get(sid)
            if not s:
                continue
            out.append(
                f"{s.get('departureIATA')}→{s.get('arrivalIATA')} "
                f"{_fmt_day(s.get('date'))} {s.get('depTime')}–{s.get('arrTime')} "
                f"({s.get('airline')}{s.get('flightNumber')})"
            )
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fmt_date(iso: str) -> str:
    """'2026-07-03T04:45' -> '03.07.2026 04:45'."""
    if not iso:
        return "?"
    date, _, tm = iso.partition("T")
    y, m, d = (date.split("-") + ["", "", ""])[:3]
    return f"{d}.{m}.{y}" + (f" {tm}" if tm else "")


def _fmt_day(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    parts = iso.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else iso


def _chunk(text: str, size: int) -> Iterable[str]:
    lines = text.split("\n")
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > size and buf:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf:
        yield buf


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def load_offset() -> Optional[int]:
    if OFFSET_PATH.exists():
        try:
            return json.loads(OFFSET_PATH.read_text(encoding="utf-8")).get("offset")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_offset(offset: int) -> None:
    OFFSET_PATH.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def format_offering(cfg: dict[str, Any], deals_by_dest: dict[str, list[Deal]]) -> str:
    """Current active deals per watched destination, regardless of dedup state."""
    cap = cfg["monitor"].get("max_deals_per_alert", 8)
    lines = ["📋 Current Hulyo offering:"]
    for iata, deals in deals_by_dest.items():
        name = cfg.get("destination_names", {}).get(iata, iata)
        if not deals:
            lines.append(f"\n{name}: no active deals right now.")
            continue
        ordered = sorted(deals, key=lambda d: (d.price is None, d.price or 0))
        shown = ordered[:cap]
        lines.append(
            f"\n{name}: {len(deals)} active deal(s)"
            + (f", cheapest {len(shown)}:" if len(deals) > len(shown) else ":")
        )
        lines.extend(d.format() for d in shown)
    return "\n".join(lines)


def handle_list_command(
    cfg: dict[str, Any], tg: Telegram, deals_by_dest: dict[str, list[Deal]]
) -> None:
    """Reply with the current offering if the chat asked for it (e.g. "List")
    since the last poll. Non-blocking — fits inside one cron cycle."""
    updates = tg.get_updates(load_offset())
    if not updates:
        return
    log.info("Polled %d Telegram update(s)", len(updates))
    save_offset(updates[-1]["update_id"] + 1)

    wants_list = False
    for update in updates:
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(cfg["telegram"]["chat_id"]):
            continue
        text = (msg.get("text") or "").strip().lower().lstrip("/")
        if text in LIST_COMMANDS:
            log.info("List command received (text=%r) - sending current offering", text)
            wants_list = True

    if wants_list:
        tg.send(format_offering(cfg, deals_by_dest))
        log.info("Sent current offering in reply to list command")


def write_status(counts: Optional[dict[str, int]], error: Optional[str]) -> None:
    """Small machine-readable heartbeat other tools (e.g. a /status command
    on a sibling monitor) can read without parsing the log."""
    payload = {
        "monitor": "hulyo",
        "last_check": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": error is None,
        "counts": counts,
        "error": error,
    }
    try:
        STATUS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        log.warning("Could not write status.json", exc_info=True)


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
    ]
    if sys.stdout is not None:
        # The Windows console may be a legacy codepage (cp1255); don't let a
        # non-encodable char kill a log call.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Env vars (GitHub Actions secrets) take priority over config.json so the
    # public repo's config.json never needs to hold real credentials.
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg["telegram"].get("bot_token")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"].get("chat_id")
    cfg["telegram"]["bot_token"] = token
    cfg["telegram"]["chat_id"] = chat_id
    return cfg


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #

def collect_deals(cfg: dict[str, Any]) -> dict[str, list[Deal]]:
    """Return {iata: [deals]} across all flight catalogs for watched dests."""
    catalog = cfg["catalog"]
    names = cfg.get("destination_names", {})
    max_price = cfg["monitor"].get("max_price_usd", 0)
    result: dict[str, list[Deal]] = {iata: [] for iata in cfg["watch_destinations"]}

    for fname in catalog["flight_files"]:
        data = fetch_catalog(catalog["base_url"], fname)
        if not data:
            continue
        for iata in cfg["watch_destinations"]:
            for deal in resolve_deals(
                data, iata, names.get(iata, iata), catalog["product_url_template"]
            ):
                if max_price and deal.price and deal.price > max_price:
                    continue
                result[iata].append(deal)
    return result


def alert_new_deals(
    cfg: dict[str, Any], tg: Telegram, seen: set[str], deals_by_dest: dict[str, list[Deal]]
) -> None:
    cap = cfg["monitor"].get("max_deals_per_alert", 8)

    for iata, deals in deals_by_dest.items():
        name = cfg.get("destination_names", {}).get(iata, iata)
        if not deals:
            # Destination has no active deals; drop its seen entries so a
            # future re-appearance triggers a fresh "became available" alert.
            stale = {k for k in seen if k.startswith(f"{iata}|")}
            if stale:
                seen -= stale
                log.info("%s no longer has deals; reset dedup", iata)
            continue

        new_deals = [d for d in deals if f"{iata}|{d.price_key}" not in seen]
        log.info("%s: %d active deal(s), %d new", iata, len(deals), len(new_deals))
        if not new_deals:
            continue

        new_deals.sort(key=lambda d: (d.price is None, d.price or 0))
        shown = new_deals[:cap]
        header = (
            f"✈️ Flights to {name} are available!\n"
            f"{len(deals)} deal(s) found"
            + (f", showing the {len(shown)} cheapest new ones:" if len(new_deals) > len(shown)
               else f", {len(new_deals)} new:")
            + "\n"
        )
        body = "\n".join(d.format() for d in shown)
        tg.send(header + "\n" + body)
        seen.update(f"{iata}|{d.price_key}" for d in new_deals)


def run_once(cfg: dict[str, Any], tg: Telegram, seen: set[str]) -> None:
    deals_by_dest = collect_deals(cfg)
    alert_new_deals(cfg, tg, seen, deals_by_dest)
    handle_list_command(cfg, tg, deals_by_dest)
    save_seen(seen)
    write_status({iata: len(deals) for iata, deals in deals_by_dest.items()}, error=None)


def main() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    setup_logging()
    cfg = load_config()
    # Default to a single cycle (CI trigger). Pass --loop for the old
    # continuous local behavior.
    once = "--loop" not in sys.argv

    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    seen = load_seen()
    log.info(
        "Hulyo monitor starting - watching %s (once=%s)",
        ", ".join(cfg["watch_destinations"]), once,
    )

    while True:
        try:
            run_once(cfg, tg, seen)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.exception("Cycle failed")
            write_status(None, error=str(exc)[:300])
        if once:
            break
        delay = random.uniform(
            cfg["monitor"]["min_delay_seconds"], cfg["monitor"]["max_delay_seconds"]
        )
        log.info("Sleeping %.0f seconds", delay)
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
