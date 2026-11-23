"""tools_free — the keyless pack: weather, time, wiki, news, fx, define.

Hermetic: no test here touches the network. Every handler's httpx client is
replaced with a canned-response fake, and the rule under test is the same as
everywhere else in the bot: a failed lookup must come back as an honest
sentence naming what blocked it, never as an invented answer.
"""

import asyncio
import unittest
from unittest import mock

import httpx

import tool_registry
import tools_free


def run(coro):
    return asyncio.run(coro)


class FakeResp:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeClient:
    """AsyncClient stand-in that answers gets from a SHARED script.

    `script` is a list of FakeResp (or Exceptions) popped in call order —
    shared across every client the handler opens, so a fallback path that
    opens a second client continues the same script instead of replaying it.
    """

    def __init__(self, script):
        self.script = script          # shared, popped in place — NOT copied

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kw):
        item = self.script.pop(0) if self.script else FakeResp(status_code=500)
        if isinstance(item, Exception):
            raise item
        return item


def patch_http(script):
    """Patch tools_free's httpx for one canned script (shared across every
    client the handler opens — copies made per-client would replay the
    script from the top on a fallback path)."""
    queue = list(script)               # copy once, share across clients
    return mock.patch.object(tools_free.httpx, "AsyncClient",
                             lambda **kw: FakeClient(queue))


class WeatherTests(unittest.TestCase):
    GEO = {"results": [{"name": "Berlin", "country": "Germany",
                        "latitude": 52.52, "longitude": 13.41}]}
    FORECAST = {
        "current": {"time": "2026-11-23T15:00", "temperature_2m": 4.1,
                    "weather_code": 3, "wind_speed_10m": 12,
                    "relative_humidity_2m": 87},
        "daily": {"time": ["2026-11-23", "2026-11-24"],
                  "temperature_2m_max": [6.0, 5.0],
                  "temperature_2m_min": [2.0, 1.0],
                  "precipitation_sum": [0.4, 0.0]}}

    def test_happy_path_names_place_conditions_and_source(self):
        with patch_http([FakeResp(self.GEO), FakeResp(self.FORECAST)]):
            out = run(tools_free.weather("berlin"))
        self.assertIn("Berlin, Germany", out)
        self.assertIn("4.1°C", out)
        self.assertIn("overcast", out)
        self.assertIn("today: high 6.0° / low 2.0°", out)
        self.assertIn("0.4mm rain", out)
        self.assertIn("source: open-meteo.com", out)

    def test_unknown_place_is_honest(self):
        with patch_http([FakeResp({"results": []})]):
            out = run(tools_free.weather("xkcdville"))
        self.assertIn("no place named", out)
        self.assertIn("xkcdville", out)
        self.assertNotIn("°", out)

    def test_unreachable_api_is_honest(self):
        # the handler lets the exception fly; the REGISTRY is what wraps it
        # into an honest sentence for the model
        with patch_http([Exception("dns is on fire")]):
            out = run(tool_registry.run(None, "weather", {"place": "berlin"}))
        self.assertIn("failed", out)
        self.assertIn("dns is on fire", out)


class TimeTests(unittest.TestCase):
    def test_known_zone(self):
        out = run(tools_free.time("Asia/Tokyo"))
        self.assertIn("in Asia/Tokyo", out)
        self.assertIn("UTC", out)
        self.assertIn("source", out)

    def test_unknown_zone_is_honest(self):
        out = run(tools_free.time("Mars/Olympus_Mons"))
        self.assertIn("don't know the timezone", out)
        self.assertIn("Europe/Berlin", out)   # shows a valid example

    def test_default_is_utc(self):
        out = run(tools_free.time())
        self.assertIn("UTC", out)

    def test_lowercased_utc_alias(self):
        out = run(tools_free.time("utc"))
        self.assertIn("UTC", out)
        self.assertNotIn("don't know", out)


class WikiTests(unittest.TestCase):
    SUMMARY = {"type": "standard", "title": "Aspirin",
               "extract": "Aspirin is a medicine.",
               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Aspirin"}}}

    def test_happy_path(self):
        with patch_http([FakeResp(self.SUMMARY)]):
            out = run(tools_free.wiki("aspirin"))
        self.assertIn("Aspirin: Aspirin is a medicine.", out)
        self.assertIn("en.wikipedia.org/wiki/Aspirin", out)
        self.assertIn("source: Wikipedia", out)

    def test_no_article_is_honest(self):
        with patch_http([FakeResp(status_code=404)]):
            out = run(tools_free.wiki("zzzznotatopic"))
        self.assertIn("no article named", out)
        self.assertNotIn("Aspirin", out)

    def test_disambiguation_is_not_a_summary(self):
        with patch_http([FakeResp({"type": "disambiguation"})]):
            out = run(tools_free.wiki("Mercury"))
        self.assertIn("disambiguation", out)

    def test_empty_extract_is_honest(self):
        with patch_http([FakeResp({"type": "standard", "title": "X",
                                    "extract": ""})]):
            out = run(tools_free.wiki("x"))
        self.assertIn("no summary text", out)


class NewsTests(unittest.TestCase):
    STORIES = [1, 2]
    ITEM = {"title": "Show HN: Inferolli", "score": 42, "descendants": 7,
            "url": "https://example.com/post"}

    def test_happy_path(self):
        script = [FakeResp(self.STORIES),
                  FakeResp(self.ITEM),
                  FakeResp(dict(self.ITEM, title="Second", url=None))]
        with patch_http(script):
            out = run(tools_free.news("top", 2))
        self.assertIn("[42 pts, 7 comments] Show HN: Inferolli — example.com", out)
        self.assertIn("Second", out)
        self.assertIn("source: news.ycombinator.com", out)

    def test_empty_feed_is_honest(self):
        with patch_http([FakeResp([])]):
            out = run(tools_free.news("new", 5))
        self.assertIn("came back empty", out)

    def test_all_items_unreadable_is_honest(self):
        script = [FakeResp(self.STORIES),
                  Exception("503"), Exception("503")]
        with patch_http(script):
            out = run(tools_free.news("top", 2))
        self.assertIn("couldn't read any stories", out)


class FxTests(unittest.TestCase):
    def test_happy_path_with_amount(self):
        with patch_http([FakeResp({"rates": {"EUR": 0.91416},
                                    "date": "2026-11-23"})]):
            out = run(tools_free.fx(**{"from": "USD", "to": "EUR",
                                       "amount": 250}))
        self.assertIn("250 USD = 228.54 EUR", out)
        self.assertIn("ECB", out)
        self.assertIn("2026-11-23", out)

    def test_default_amount_is_one(self):
        with patch_http([FakeResp({"rates": {"EUR": 0.91416}, "date": "d"})]):
            out = run(tools_free.fx(**{"from": "USD", "to": "EUR"}))
        self.assertIn("1 USD = 0.9142 EUR", out)   # sub-1 results keep 4 decimals

    def test_bad_code_is_an_error_not_a_wrong_conversion(self):
        out = run(tools_free.fx(**{"from": "USDT", "to": "EUR"}))
        self.assertIn("isn't a 3-letter ISO currency code", out)
        self.assertNotIn("=", out)

    def test_fallback_source_is_used_when_ecb_is_down(self):
        script = [Exception("ecb down"),
                  FakeResp({"rates": {"JPY": 155.0}})]
        with patch_http(script):
            out = run(tools_free.fx(**{"from": "USD", "to": "JPY",
                                       "amount": 100}))
        self.assertIn("15,500.00 JPY", out)
        self.assertIn("exchangerate-api", out)

    def test_both_sources_down_is_honest(self):
        script = [Exception("ecb down"), Exception("erapi down")]
        with patch_http(script):
            out = run(tools_free.fx(**{"from": "USD", "to": "EUR"}))
        self.assertIn("no rate found", out)

    def test_same_currency_short_circuits(self):
        out = run(tools_free.fx(**{"from": "USD", "to": "USD", "amount": 5}))
        self.assertIn("same currency", out)


class DefineTests(unittest.TestCase):
    ENTRY = [{"word": "hello", "phonetic": "/həˈloʊ/",
              "meanings": [{"partOfSpeech": "exclamation",
                            "definitions": [
                                {"definition": "used as a greeting",
                                 "example": "hello there"}]}],
              "origin": "early 19th century"}]

    def test_happy_path(self):
        with patch_http([FakeResp(self.ENTRY)]):
            out = run(tools_free.define("hello"))
        self.assertIn("hello /həˈloʊ/", out)
        self.assertIn("exclamation: used as a greeting", out)
        self.assertIn('e.g. "hello there"', out)
        self.assertIn("origin: early 19th century", out)
        self.assertIn("source: dictionaryapi.dev", out)

    def test_unknown_word_is_honest(self):
        with patch_http([FakeResp(status_code=404)]):
            out = run(tools_free.define("qqqqzzz"))
        self.assertIn("no dictionary entry", out)


class RegistryIntegrationTests(unittest.TestCase):
    def test_six_pack_is_registered_after_the_server_tools(self):
        names = tool_registry.names()
        self.assertEqual(names,
                         ["web_search", "fetch_page", "channel_history",
                          "discord_search", "member_profile", "docs_search",
                          "computer",
                          "weather", "time", "wiki", "news", "fx", "define"])

    def test_every_pack_tool_is_member_tier_and_free(self):
        for name in ("weather", "time", "wiki", "news", "fx", "define"):
            spec = tool_registry.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.tier, "member", name)
            self.assertEqual(spec.cost, "free", name)

    def test_pack_is_advertised_in_the_member_prompt(self):
        prompt = tool_registry.tools_prompt(admin=False)
        for name in ("weather", "time", "wiki", "news", "fx", "define"):
            self.assertIn(f'"tool": "{name}"', prompt)

    def test_dispatch_through_the_registry(self):
        # end to end: the registry routes fx, coerces args (including the
        # keyword-named "from"), and returns the handler's text
        with patch_http([FakeResp({"rates": {"EUR": 0.91416},
                                    "date": "2026-11-23"})]):
            out = run(tool_registry.run(None, "fx",
                                        {"from": "usd", "to": "eur",
                                         "amount": 10}))
        self.assertIn("10 USD = 9.14 EUR", out)

    def test_fx_missing_required_arg_is_an_error(self):
        out = run(tool_registry.run(None, "fx", {"to": "EUR"}))
        self.assertIn("missing required arg 'from'", out)


if __name__ == "__main__":
    unittest.main()