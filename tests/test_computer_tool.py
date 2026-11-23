"""The `computer` tool must never narrate a capability the box does not have.

/workspace exists only inside the E2B sandbox. The tool used to run
`cd /workspace && opencode run ...` regardless, so on any other host sh
answered "can't cd to /workspace" and the model told the person its
"computer tool keeps dying" — a hardware-sounding failure for what is simply
"there is no sandbox right now". The tool must say that, and must not spawn
anything: a shell that runs in a place the bot does not own is how a promise
turns into a lie.
"""
import asyncio
import shlex
import unittest
from unittest import mock

import tools_server


def run_tool(name, args):
    # admin=True: these tests exercise the handler's honesty, not the tier
    # gate (which has its own tests in test_tool_registry)
    return asyncio.run(tools_server.run_tool(mock.Mock(), name, args, admin=True))


class NoWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.sub = mock.Mock(side_effect=lambda *a, **k: self.calls.append(a) or
                             mock.Mock(stdout="never", stderr="", returncode=0))
        self.patches = [
            mock.patch.dict(tools_server.os.environ, {}, clear=False),
            mock.patch.object(tools_server.shutil, "which", mock.Mock(return_value="/usr/bin/opencode")),
            mock.patch.object(tools_server.os.path, "isdir", mock.Mock(return_value=False)),
            mock.patch("subprocess.run", self.sub),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_it_says_there_is_no_sandbox_and_runs_nothing(self):
        out = run_tool("computer", {"prompt": "play get lucky"})
        self.assertIn("no sandbox", out)
        self.assertIn("/workspace", out)
        self.assertEqual(self.calls, [], "the tool must not spawn a shell it cannot honour")

    def test_it_does_not_pretend_it_tried(self):
        out = run_tool("computer", {"prompt": "ls"})
        for wording in ("can't cd", "keeps dying", "traceback", "Exit code"):
            self.assertNotIn(wording, out)


class WorkspaceTests(unittest.TestCase):
    """With a workspace present the tool runs, and it quotes the prompt."""

    def setUp(self):
        self.sub = mock.Mock(return_value=mock.Mock(stdout="  it played\n", stderr="", returncode=0))
        for p in [
            mock.patch.object(tools_server.os.path, "isdir", mock.Mock(return_value=True)),
            mock.patch.object(tools_server.shutil, "which", mock.Mock(return_value="/usr/bin/opencode")),
            mock.patch("subprocess.run", self.sub),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def test_a_prompt_with_quotes_cannot_break_out_of_the_command(self):
        evil = "play it's a sin; rm -rf /workspace"
        run_tool("computer", {"prompt": evil})
        argv = self.sub.call_args[0][0]
        self.assertEqual(argv[:2], ["bash", "-lc"])
        # the prompt must survive as ONE argument, with no control operator
        # escaping into the command line
        tokens = shlex.split(argv[2])
        self.assertEqual(tokens[-1], evil)
        self.assertNotIn("rm", tokens)
        self.assertNotIn("&&", tokens[3:])

    def test_stdout_is_what_the_person_gets(self):
        self.assertEqual(run_tool("computer", {"prompt": "np"}), "it played")

    def test_an_empty_run_is_explained(self):
        self.sub.return_value = mock.Mock(stdout="", stderr="opencode: not authenticated", returncode=1)
        out = run_tool("computer", {"prompt": "np"})
        self.assertIn("ran nothing", out)
        self.assertIn("not authenticated", out)


if __name__ == "__main__":
    unittest.main()
