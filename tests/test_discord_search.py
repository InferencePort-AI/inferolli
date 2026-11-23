"""tools_server.discord_search — long-reach history search.

Discord indexes every message the bot can read, so searching it reaches back
years without storing anything. The risk that comes with that power is scope:
a search that returns channels the bot shouldn't quote is worse than no search
at all. These tests are mostly about the boundary.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import tools_server
from tests.fakes import FakePermissions

GUILD_ID = 1565792206642483200


class FakeSearchChannel:
    def __init__(self, cid, name, readable=True):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self._readable = readable

    def permissions_for(self, member):
        return FakePermissions(read_message_history=self._readable)


class FakeSearchGuild:
    def __init__(self, channels, members=()):
        self.id = GUILD_ID
        self.text_channels = channels
        self.members = list(members)
        self.me = SimpleNamespace(id=555)


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild] if guild else []


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def http_response(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json = lambda: payload or {}
    return r


class SearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.public = FakeSearchChannel(10, "general")
        self.secret = FakeSearchChannel(20, "staff-chat", readable=False)
        self.guild = FakeSearchGuild([self.public, self.secret])
        self.bot = FakeBot(self.guild)
        self._env = mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "tok"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _search(self, payload, **kw):
        with mock.patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = mock.AsyncMock(
                return_value=http_response(200, payload))
            return run(tools_server.discord_search(self.bot, "astra", **kw))

    def _hit(self, cid, content="hello", ts="2026-11-17T18:55:00", user="cj"):
        return [{"channel_id": str(cid), "content": content, "timestamp": ts,
                 "author": {"username": user}}]

    def test_returns_formatted_hits(self):
        out = self._search({"total_results": 1, "messages": [self._hit(10)]})
        self.assertIn("astra", out)
        self.assertIn("#general", out)
        self.assertIn("2026-11-17", out)

    def test_unreadable_channel_is_filtered_out(self):
        """A hit in a channel the bot cannot read must never be quoted."""
        out = self._search({"total_results": 2,
                            "messages": [self._hit(20, "secret staff talk"),
                                         self._hit(10, "public talk")]})
        self.assertIn("public talk", out)
        self.assertNotIn("secret staff talk", out)

    def test_empty_result_is_explicit(self):
        out = self._search({"total_results": 0, "messages": []})
        self.assertIn("nothing found", out)

    def test_http_failure_is_reported_not_raised(self):
        with mock.patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = mock.AsyncMock(
                return_value=http_response(500, {}))
            out = run(tools_server.discord_search(self.bot, "x"))
        self.assertIn("HTTP 500", out)

    def test_forbidden_is_explained(self):
        with mock.patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = mock.AsyncMock(
                return_value=http_response(403, {}))
            out = run(tools_server.discord_search(self.bot, "x"))
        self.assertIn("refused", out)

    def test_network_error_is_contained(self):
        with mock.patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = mock.AsyncMock(
                side_effect=RuntimeError("boom"))
            out = run(tools_server.discord_search(self.bot, "x"))
        self.assertIn("search failed", out)

    def test_missing_token_degrades(self):
        with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": ""}, clear=False):
            out = run(tools_server.discord_search(self.bot, "x"))
        self.assertIn("no Discord token", out)

    def test_no_guild_degrades(self):
        out = run(tools_server.discord_search(FakeBot(None), "x"))
        self.assertIn("no guild", out)

    def test_limit_is_bounded(self):
        """Ask for 500, get at most 25 — the API cap, and a runaway guard."""
        with mock.patch("httpx.AsyncClient") as client:
            getter = mock.AsyncMock(return_value=http_response(200, {"messages": []}))
            client.return_value.__aenter__.return_value.get = getter
            run(tools_server.discord_search(self.bot, "x", n=500))
        params = getter.await_args.kwargs["params"]
        self.assertLessEqual(params["limit"], 25)

    def test_channel_filter_keeps_requested_room_only(self):
        with mock.patch("httpx.AsyncClient") as client:
            getter = mock.AsyncMock(return_value=http_response(200, {"messages": []}))
            client.return_value.__aenter__.return_value.get = getter
            run(tools_server.discord_search(self.bot, "x", channel="#general"))
        params = getter.await_args.kwargs["params"]
        self.assertEqual(params["channel_id"], "10")

    def test_channel_filter_refuses_unreadable_room(self):
        out = run(tools_server.discord_search(self.bot, "x", channel="#staff-chat"))
        self.assertIn("can't read", out)

    def test_blank_messages_are_skipped(self):
        out = self._search({"total_results": 2,
                            "messages": [self._hit(10, ""), self._hit(10, "real text")]})
        self.assertIn("real text", out)
        self.assertNotIn("- 2026-11-17T18:55:00 #general cj: \n", out)

    def test_bot_message_shape_with_grouped_messages(self):
        """Discord returns messages grouped as [[m], [m]] for replies."""
        grouped = {"total_results": 1, "messages": [[self._hit(10, "grouped one")[0]]]}
        out = self._search(grouped)
        self.assertIn("grouped one", out)


class ToolRegistrationTests(unittest.TestCase):
    def test_tool_is_advertised_to_the_model(self):
        self.assertIn("discord_search", tools_server.TOOLS_PROMPT)

    def test_tool_is_routed(self):
        # routing is registry-driven now: the invariant is that the tool is
        # registered (so run_tool can reach it), not that its name is spelled
        # out in run_tool's source
        self.assertIsNotNone(tools_server.tool_registry.get("discord_search"))
        # and it actually dispatches through run_tool
        out = asyncio.run(tools_server.run_tool(
            FakeBot(None), "discord_search", {"query": "zzz_nope"}))
        self.assertNotIn("unknown tool", out)


if __name__ == "__main__":
    unittest.main()
