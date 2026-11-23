"""tool_registry — the tool surface as a declarative registry.

The invariant this file guards: the prompt the model sees is GENERATED from
the registry, so the persona can never advertise a capability that isn't
installed (or hide one that is). Plus the schema coercer and the tier gate.
"""

import asyncio
import unittest
from unittest import mock

import tool_registry
import tools_server
from tool_registry import RegistryError, ToolSpec, coerce


def run(coro):
    return asyncio.run(coro)


class RegistrationTests(unittest.TestCase):
    def test_all_seven_tools_are_registered(self):
        self.assertEqual(
            tool_registry.names(),
            ["web_search", "fetch_page", "channel_history", "discord_search",
             "member_profile", "docs_search", "computer"])

    def test_computer_is_admin_tier_the_rest_member(self):
        for spec in tool_registry.specs():
            if spec.name == "computer":
                self.assertEqual(spec.tier, "admin")
            else:
                self.assertEqual(spec.tier, "member",
                                 f"{spec.name} should default to member tier")

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaises(RegistryError):
            @tool_registry.tool(name="web_search", description="dup",
                                args={})
            async def dup():
                return ""

    def test_sync_handler_is_rejected(self):
        with self.assertRaises(RegistryError):
            @tool_registry.tool(name="_sync_test", description="x", args={})
            def sync_fn():
                return ""

    def test_bad_tier_is_rejected(self):
        with self.assertRaises(RegistryError):
            @tool_registry.tool(name="_tier_test", description="x", args={},
                                tier="supreme")
            async def fn():
                return ""

    def test_bad_arg_rule_is_rejected_at_registration(self):
        with self.assertRaises(RegistryError):
            @tool_registry.tool(name="_rule_test", description="x",
                                args={"q": "float:1.5"})
            async def fn(q):
                return ""


class DriftTests(unittest.TestCase):
    """The prompt is generated from the registry — keep it that way."""

    def test_module_prompt_equals_registry_member_view(self):
        self.assertEqual(tools_server.TOOLS_PROMPT,
                         tool_registry.tools_prompt(admin=False))

    def test_every_member_tool_is_advertised(self):
        prompt = tool_registry.tools_prompt(admin=False)
        for spec in tool_registry.specs():
            if spec.tier == "member":
                self.assertIn(f'"tool": "{spec.name}"', prompt)

    def test_admin_only_tool_is_hidden_from_members_shown_to_admins(self):
        member_view = tool_registry.tools_prompt(admin=False)
        admin_view = tool_registry.tools_prompt(admin=True)
        self.assertNotIn('"computer"', member_view)
        self.assertIn('"computer"', admin_view)

    def test_tier_override_hides_a_tool_from_its_spec_default(self):
        # config.json permissions.tool_tiers can raise a tool's tier
        fake = {"web_search": "admin"}
        with mock.patch("tool_registry.permissions.tool_tier",
                        side_effect=lambda name, default:
                        fake.get(name, default)):
            view = tool_registry.tools_prompt(admin=False)
        self.assertNotIn('"web_search"', view)


class CoerceTests(unittest.TestCase):
    def _spec(self, args):
        return ToolSpec(name="t", description="d", args=args)

    def test_str_is_clamped(self):
        spec = self._spec({"q": "str:5"})
        out, err = coerce(spec, {"q": "abcdefghij"})
        self.assertEqual(out, {"q": "abcde"})
        self.assertIsNone(err)

    def test_int_is_bounded(self):
        spec = self._spec({"n": "int:1..25"})
        out, err = coerce(spec, {"n": 999})
        self.assertEqual(out, {"n": 25})
        out, _ = coerce(spec, {"n": "0"})
        self.assertEqual(out, {"n": 1})

    def test_bad_int_is_an_error_not_a_crash(self):
        spec = self._spec({"n": "int:1..25"})
        out, err = coerce(spec, {"n": "soon"})
        self.assertEqual(out, {})
        self.assertIn("whole number", err)

    def test_choice_must_match(self):
        spec = self._spec({"c": "choice:a|b"})
        out, err = coerce(spec, {"c": "b"})
        self.assertEqual(out, {"c": "b"})
        out, err = coerce(spec, {"c": "z"})
        self.assertIn("must be one of", err)

    def test_optional_missing_leaves_the_default(self):
        spec = self._spec({"q": "str:10", "n": "?int:1..25"})
        out, err = coerce(spec, {"q": "hi"})
        self.assertEqual(out, {"q": "hi"})
        self.assertIsNone(err)

    def test_required_missing_is_an_error(self):
        spec = self._spec({"q": "str:10"})
        out, err = coerce(spec, {})
        self.assertIn("missing required arg 'q'", err)

    def test_non_dict_args_come_back_as_missing(self):
        spec = self._spec({"q": "str:10"})
        out, err = coerce(spec, "nope")
        self.assertIn("missing required", err)


class DispatchTests(unittest.TestCase):
    def _scratch(self, *, tier="member", timeout=5.0, audit=False):
        """Register a throwaway tool and unregister it after the test."""
        name = f"_scratch_{self.id()[-6:]}"
        try:
            @tool_registry.tool(name=name, description="scratch", args={},
                                tier=tier, timeout=timeout, audit=audit)
            async def handler():
                return "did it"
        except RegistryError:                      # duplicate from a re-run
            tool_registry._REGISTRY[name].handler = _scratch_handler
        self.addCleanup(tool_registry._REGISTRY.pop, name, None)
        self.addCleanup(tool_registry._ORDER.remove, name)
        return name

    def test_unknown_tool_is_honest(self):
        out = run(tool_registry.run(None, "no_such_tool", {}))
        self.assertIn("unknown tool", out)

    def test_member_cannot_run_an_admin_tool(self):
        name = self._scratch(tier="admin")
        out = run(tool_registry.run(None, name, {}, admin=False))
        self.assertIn("needs the admin tier", out)
        self.assertIn("i can't run it", out)
        out = run(tool_registry.run(None, name, {}, admin=True))
        self.assertEqual(out, "did it")

    def test_raising_handler_yields_an_honest_failure(self):
        name = self._scratch()
        spec = tool_registry.get(name)
        async def boom():
            raise RuntimeError("upstream 500")
        spec.handler = boom
        out = run(tool_registry.run(None, name, {}))
        self.assertIn("failed", out)
        self.assertIn("upstream 500", out)

    def test_timeout_yields_an_honest_failure(self):
        name = self._scratch(timeout=0.05)
        spec = tool_registry.get(name)
        async def slow():
            await asyncio.sleep(1)
            return "too late"
        spec.handler = slow
        out = run(tool_registry.run(None, name, {}))
        self.assertIn("timed out", out)

    def test_empty_result_is_not_blank_silence(self):
        name = self._scratch()
        spec = tool_registry.get(name)
        async def quiet():
            return "  "
        spec.handler = quiet
        out = run(tool_registry.run(None, name, {}))
        self.assertIn("returned nothing", out)

    def test_audit_tool_fires_the_modlog_write(self):
        name = self._scratch(audit=True)
        with mock.patch.object(tool_registry, "_audit",
                               new=mock.AsyncMock()) as aud:
            run(tool_registry.run(None, name, {}))
        aud.assert_awaited_once()

    def test_non_audit_tool_never_touches_the_modlog(self):
        name = self._scratch(audit=False)
        with mock.patch.object(tool_registry, "_audit",
                               new=mock.AsyncMock()) as aud:
            run(tool_registry.run(None, name, {}))
        aud.assert_not_awaited()

    def test_handler_gets_bot_only_when_it_declares_it(self):
        calls = {}

        name = f"_botprobe_{self.id()[-6:]}"
        try:
            @tool_registry.tool(name=name, description="probe", args={})
            async def wants_bot(bot):
                calls["bot"] = bot
                return "ok"

            @tool_registry.tool(name=name + "_x", description="probe", args={})
            async def no_bot():
                return "ok"
        finally:
            self.addCleanup(tool_registry._REGISTRY.pop, name, None)
            self.addCleanup(tool_registry._ORDER.remove, name)
            self.addCleanup(tool_registry._REGISTRY.pop, name + "_x", None)
            self.addCleanup(tool_registry._ORDER.remove, name + "_x")
        sentinel = mock.Mock()
        run(tool_registry.run(sentinel, name, {}))
        self.assertEqual(calls.get("bot"), sentinel)


class ListingTests(unittest.TestCase):
    def test_listing_marks_locked_tools(self):
        member_view = tool_registry.listing("member")
        self.assertIn("🔒 **computer**", member_view)
        self.assertIn("✅ **web_search**", member_view)

    def test_admin_listing_shows_everything_unlocked(self):
        admin_view = tool_registry.listing("admin")
        self.assertIn("✅ **computer**", admin_view)


if __name__ == "__main__":
    unittest.main()