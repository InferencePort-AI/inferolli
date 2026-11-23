"""tools_server.py — the tool loop envelope, hops, action stripping, admin gating."""

import asyncio
import unittest
from unittest import mock

import tools_server


class RunLoopTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def _script(self, *responses):
        """chat_for stub that yields the given responses in order.

        run_loop now asks for raw text (json_out=False) and parses it itself, so
        dicts are serialised here — that keeps these tests exercising the real
        parsing path instead of handing it a pre-parsed object.
        """
        import json as _json
        calls = []
        queue = [r if isinstance(r, str) else _json.dumps(r) for r in responses]

        async def fake_chat_for(job, messages, **kw):
            calls.append([m["content"] for m in messages])
            return queue.pop(0)

        return fake_chat_for, calls

    def test_plain_reply(self):
        fake, _ = self._script({"reply": "hello there", "actions": []})
        with mock.patch.object(tools_server.ai, "chat_for", fake):
            out = self.run_async(tools_server.run_loop(None, "SYS", "hi"))
        self.assertEqual(out, {"reply": "hello there", "actions": []})

    def test_tool_hop_then_reply(self):
        fake, calls = self._script(
            {"tool": "web_search", "args": {"query": "tonearm"}},
            {"reply": "found it", "actions": []},
        )
        ran = []

        async def fake_run_tool(bot, name, args, **kw):
            ran.append((name, args))
            return "SEARCH RESULTS"

        with mock.patch.object(tools_server.ai, "chat_for", fake), \
             mock.patch.object(tools_server, "run_tool", fake_run_tool):
            out = self.run_async(tools_server.run_loop(None, "SYS", "hi"))

        self.assertEqual(out["reply"], "found it")
        self.assertEqual(ran, [("web_search", {"query": "tonearm"})])
        # the second hop must carry the tool result back
        self.assertTrue(any("SEARCH RESULTS" in c for c in calls[1]))

    def test_actions_stripped_when_not_allowed(self):
        fake, _ = self._script({"reply": "ok",
                                "actions": [{"type": "create_channel", "name": "x"}]})
        with mock.patch.object(tools_server.ai, "chat_for", fake):
            out = self.run_async(tools_server.run_loop(None, "SYS", "hi",
                                                       allow_actions=False))
        self.assertEqual(out["actions"], [])

    def test_actions_pass_through_when_allowed(self):
        act = [{"type": "create_channel", "name": "x"}]
        fake, _ = self._script({"reply": "ok", "actions": act})
        with mock.patch.object(tools_server.ai, "chat_for", fake):
            out = self.run_async(tools_server.run_loop(None, "SYS", "hi"))
        self.assertEqual(out["actions"], act)

    def test_computer_line_only_for_admins(self):
        fake, calls = self._script({"reply": "ok", "actions": []},
                                   {"reply": "ok", "actions": []})
        with mock.patch.object(tools_server.ai, "chat_for", fake):
            self.run_async(tools_server.run_loop(None, "SYS", "hi", admin=False))
            self.run_async(tools_server.run_loop(None, "SYS", "hi", admin=True))
        member_sys, admin_sys = calls[0][0], calls[1][0]
        self.assertNotIn("its live desktop", member_sys)
        self.assertIn("its live desktop", admin_sys)

    def test_hop_exhaustion_returns_empty(self):
        fake, _ = self._script(
            {"tool": "web_search", "args": {"query": "x"}},
            {"tool": "web_search", "args": {"query": "y"}},
        )

        async def fake_run_tool(bot, name, args, **kw):
            return "r"

        with mock.patch.object(tools_server.ai, "chat_for", fake), \
             mock.patch.object(tools_server, "run_tool", fake_run_tool), \
             mock.patch.object(tools_server, "MAX_HOPS", 2):
            out = self.run_async(tools_server.run_loop(None, "SYS", "hi"))
        self.assertEqual(out, {"reply": "", "actions": []})

    def test_vision_payload_rides_on_first_user_message(self):
        fake, calls = self._script({"reply": "seen", "actions": []})
        payload = [{"type": "text", "text": "what is this"},
                   {"type": "image_url", "image_url": {"url": "https://cdn/x.png"}}]
        with mock.patch.object(tools_server.ai, "chat_for", fake):
            self.run_async(tools_server.run_loop(None, "SYS", "what is this",
                                                 content_payload=payload))
        self.assertEqual(calls[0][1], payload)


if __name__ == "__main__":
    unittest.main()
