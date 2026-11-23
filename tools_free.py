"""tools_free — the keyless lookup pack (Phase 1 of the capabilities plan).

Six tools, no keys, no accounts, no secrets: weather (open-meteo), time
(stdlib zoneinfo — zero network), wiki (Wikipedia REST), news (Hacker News
Firebase API), fx (ECB daily rates via frankfurter.dev, er-api fallback),
define (dictionaryapi.dev).

Each is a registry entry like any other — schema, tier, timeout — and each
result carries a one-line source attribution, because "I looked it up" must
always be traceable to *where*. Registration happens on import; tools_server
imports this module after its own tools, so the prompt order is: server
tools, then this pack.

Rule from the plan: keyless only. Anything needing a key waits for Phase 2
so the secrets story stays in one place.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from tool_registry import tool

log = logging.getLogger("tools")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; Inferolli/1.0)"}

# open-meteo's WMO weather codes, as words
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


def _code_desc(code) -> str:
    try:
        return _WMO.get(int(code)) or f"weather code {code}"
    except (TypeError, ValueError):
        return "unknown conditions"


@tool(name="weather",
      description="current conditions + short forecast for a place (city name)",
      args={"place": "str:80", "days": "?int:1..5"}, timeout=25)
async def weather(place: str, days: int = 2) -> str:
    async with httpx.AsyncClient(timeout=10, headers=_UA, follow_redirects=True) as c:
        r = await c.get("https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": place.strip(), "count": 1})
        r.raise_for_status()
        hits = r.json().get("results") or []
        if not hits:
            return f"(weather: no place named {place.strip()!r} in the geocoder)"
        h = hits[0]
        lat, lon = h["latitude"], h["longitude"]
        r = await c.get("https://api.open-meteo.com/v1/forecast",
                        params={"latitude": lat, "longitude": lon,
                                "current": "temperature_2m,relative_humidity_2m,"
                                           "weather_code,wind_speed_10m",
                                "daily": "temperature_2m_max,temperature_2m_min,"
                                         "precipitation_sum",
                                "forecast_days": max(1, min(days, 5)),
                                "timezone": "auto"})
        r.raise_for_status()
        d = r.json()

    cur = d.get("current") or {}
    where = h.get("name", place)
    if h.get("country"):
        where += f", {h['country']}"
    lines = [f"{where} — local time {cur.get('time', '?')}: "
             f"{cur.get('temperature_2m', '?')}°C, "
             f"{_code_desc(cur.get('weather_code'))}, "
             f"wind {cur.get('wind_speed_10m', '?')} km/h, "
             f"humidity {cur.get('relative_humidity_2m', '?')}%"]
    daily = d.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_sum") or []
    dates = daily.get("time") or []
    for i in range(min(len(highs), max(1, min(days, 5)))):
        tag = "today" if i == 0 else str(dates[i]) if i < len(dates) else f"+{i}"
        mm = f", {rain[i]:.1f}mm rain" if i < len(rain) and rain[i] else ""
        lines.append(f"{tag}: high {highs[i]}° / low {lows[i]}°{mm}")
    lines.append("(source: open-meteo.com)")
    return "\n".join(lines)


@tool(name="time",
      description="current time in an IANA timezone (e.g. Europe/Berlin, America/New_York); "
                  "also answers 'what time is it' with UTC if no zone is given",
      args={"zone": "?str:60"})
async def time(zone: str = "UTC") -> str:
    zone = (zone or "UTC").strip()
    if zone.lower() in ("utc", "gmt", "z"):
        zone = "UTC"
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        return (f"(time: i don't know the timezone {zone!r} — try an IANA name "
                "like Europe/Berlin or America/New_York)")
    now_utc = datetime.now(timezone.utc)
    now_here = now_utc.astimezone(tz)
    off = now_here.strftime("%z")[:3] + ":" + now_here.strftime("%z")[3:]
    return (f"{now_here.strftime('%A %H:%M')} in {zone} (UTC{off}); "
            f"it's {now_utc.strftime('%H:%M')} UTC"
            "\n(source: the server clock, no network needed)")


@tool(name="wiki",
      description="one-paragraph Wikipedia summary of a topic, with a link",
      args={"topic": "str:120"}, timeout=15)
async def wiki(topic: str) -> str:
    title = quote(topic.strip().replace(" ", "_"))
    async with httpx.AsyncClient(timeout=10, headers=_UA, follow_redirects=True) as c:
        r = await c.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
    if r.status_code == 404:
        return f"(wiki: no article named {topic.strip()!r} — it may exist under a different spelling)"
    r.raise_for_status()
    d = r.json()
    if d.get("type") == "disambiguation":
        return (f"(wiki: {topic.strip()!r} is a disambiguation page — "
                "tell me which of the things you mean)")
    extract = (d.get("extract") or "").strip()
    if not extract:
        return f"(wiki: the article for {topic.strip()!r} has no summary text)"
    url = ((d.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    out = f"{d.get('title', topic)}: {extract[:600]}"
    if url:
        out += f"\n{url}"
    out += "\n(source: Wikipedia)"
    return out


@tool(name="news",
      description="current top stories from Hacker News (feeds: top, new, ask)",
      args={"feed": "?choice:top|new|ask", "n": "?int:1..8"}, timeout=25)
async def news(feed: str = "top", n: int = 5) -> str:
    feed = (feed or "top").strip().lower()
    n = max(1, min(n, 8))
    async with httpx.AsyncClient(timeout=10, headers=_UA) as c:
        r = await c.get(f"https://hacker-news.firebaseio.com/v0/{feed}stories.json")
        r.raise_for_status()
        ids = (r.json() or [])[:n]
        if not ids:
            return f"(news: the {feed} feed came back empty)"
        items = await asyncio.gather(
            *(c.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
              for i in ids), return_exceptions=True)
    lines = []
    for it in items:
        if isinstance(it, Exception) or it.status_code != 200:
            continue
        d = it.json()
        if not d:
            continue
        host = ""
        link = d.get("url") or ""
        if link:
            host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
        comments = d.get("descendants") or 0
        lines.append(f"- [{d.get('score', 0)} pts, {comments} comments] "
                     f"{d.get('title', '?')}{' — ' + host if host else ''}")
    if not lines:
        return f"(news: couldn't read any stories off the {feed} feed)"
    return "\n".join(lines) + f"\n({feed} stories, source: news.ycombinator.com)"


@tool(name="fx",
      description="convert between currencies using yesterday's ECB reference rates "
                  "(e.g. from USD to EUR, amount 250)",
      args={"from": "str:8", "to": "str:8", "amount": "?int:1..1000000"}, timeout=15)
async def fx(**kw) -> str:
    # "from" is a Python keyword, so this handler is **kw-only and reads the
    # schema args by hand. No silent clamping of currency codes: a wrong code
    # must come back as an error, not as a confidently wrong conversion.
    frm = str(kw.get("from", "")).strip().upper()
    to = str(kw.get("to", "")).strip().upper()
    try:
        amount = int(kw.get("amount", 1) or 1)
    except (TypeError, ValueError):
        return "(fx: amount must be a whole number)"
    for code in (frm, to):
        if not re.fullmatch(r"[A-Z]{3}", code):
            return (f"(fx: {code!r} isn't a 3-letter ISO currency code "
                    "like USD or EUR)")
    if frm == to:
        return f"{amount} {frm} is {amount} {to} (same currency)"

    rate, date, src = await _ecb_rate(frm, to)
    if rate is None:
        rate, date, src = await _erapi_rate(frm, to)
    if rate is None:
        return (f"(fx: no rate found for {frm} → {to} — the rate services "
                "don't know one of those codes)")

    out = amount * rate
    shown = f"{out:,.2f}" if out >= 1 else f"{out:.4f}"
    return (f"{amount:,} {frm} = {shown} {to} (rate {rate}, {src} daily, {date})"
            f"\n(source: {src})")


async def _ecb_rate(frm: str, to: str):
    try:
        async with httpx.AsyncClient(timeout=8, headers=_UA, follow_redirects=True) as c:
            r = await c.get("https://api.frankfurter.dev/v1/latest",
                            params={"base": frm, "symbols": to})
            r.raise_for_status()
            rates = r.json().get("rates") or {}
        return float(rates[to]), r.json().get("date", "recent"), "ECB"
    except Exception as e:
        log.info("fx: ECB lookup failed (%s) — trying fallback", str(e)[:60])
        return None, None, None


async def _erapi_rate(frm: str, to: str):
    try:
        async with httpx.AsyncClient(timeout=8, headers=_UA, follow_redirects=True) as c:
            r = await c.get(f"https://open.er-api.com/v6/latest/{frm}")
            r.raise_for_status()
            rates = r.json().get("rates") or {}
        return float(rates[to]), "recent", "exchangerate-api"
    except Exception:
        return None, None, None


@tool(name="define",
      description="dictionary definition and pronunciation of an English word",
      args={"word": "str:40"}, timeout=15)
async def define(word: str) -> str:
    w = quote(word.strip().lower())
    async with httpx.AsyncClient(timeout=10, headers=_UA, follow_redirects=True) as c:
        r = await c.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{w}")
    if r.status_code == 404:
        return f"(define: no dictionary entry for {word.strip()!r})"
    r.raise_for_status()
    entries = r.json() or []
    if not entries:
        return f"(define: the dictionary came back empty for {word.strip()!r})"
    e = entries[0]
    out = []
    phon = e.get("phonetic") or next(
        (p.get("text") for p in e.get("phonetics") or [] if p.get("text")), "")
    head = e.get("word", word.strip()) + (f" {phon}" if phon else "")
    out.append(head)
    for meaning in (e.get("meanings") or [])[:3]:
        pos = meaning.get("partOfSpeech", "?")
        defs = meaning.get("definitions") or []
        if not defs:
            continue
        text = (defs[0].get("definition") or "").strip()
        line = f"{pos}: {text}"
        ex = (defs[0].get("example") or "").strip()
        if ex:
            line += f' — e.g. "{ex[:120]}"'
        out.append(line)
    if e.get("origin"):
        out.append(f"origin: {str(e['origin'])[:200]}")
    out.append("(source: dictionaryapi.dev)")
    return "\n".join(out)