"""Claude Code adapter regression: claude_hook.py over the shared core.

Events are shaped like Claude Code hook input (prompt_id instead of turn_id,
last_assistant_message on Stop, agent_id inside subagents). Fixtures live in a
short temporary directory so Windows MAX_PATH limits do not interfere.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "project-handoff"
ADAPTER = SOURCE / "scripts" / "claude_hook.py"
CORE = SOURCE / "scripts" / "compaction_reminder.py"
NOTICE = "交接建议：模拟阶段已收拢，后续进入下一段工作，适合在这里交接。确认后我会保存材料并交付开场白。"


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.state = pathlib.Path(tempfile.mkdtemp(prefix="ph-claude-"))
        self.session = str(uuid.uuid4())
        self.prompt = str(uuid.uuid4())

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def event(self, name, **extra):
        base = {"session_id": self.session, "transcript_path": str(self.state / "missing.jsonl"),
                "cwd": str(self.state), "permission_mode": "default", "hook_event_name": name}
        base.update(extra)
        return base

    def hook(self, name, raw=None, **extra):
        payload = raw if raw is not None else json.dumps(self.event(name, **extra))
        run = subprocess.run([sys.executable, "-X", "utf8", str(ADAPTER), "--state-dir", str(self.state)],
                             input=payload, text=True, encoding="utf-8", capture_output=True, timeout=30)
        out = run.stdout.strip()
        return run.returncode, (json.loads(out) if out else {})

    def core(self, *args):
        run = subprocess.run([sys.executable, "-X", "utf8", str(CORE), "--state-dir", str(self.state),
                              "--session-id", self.session, *args],
                             text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def compact(self, times=3):
        for _ in range(times):
            self.assertEqual(self.hook("PostCompact", trigger="auto", prompt_id=self.prompt), (0, {}))

    def context(self):
        rc, out = self.hook("UserPromptSubmit", prompt_id=self.prompt, prompt="继续")
        self.assertEqual(rc, 0)
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_prompt_id_maps_to_turn_id_and_context_names_session(self):
        self.compact()
        ctx = self.context()
        self.assertTrue(ctx.startswith(f"当前session_id={self.session}；"))
        self.assertIn(f"当前turn_id={self.prompt}", ctx)
        self.assertIn("待评估", ctx)
        self.assertEqual(self.core("--action", "status")["active_turn_id"], self.prompt)

    def test_manual_compaction_and_fresh_sessions_stay_silent(self):
        self.assertEqual(self.hook("UserPromptSubmit", prompt_id=self.prompt, prompt="开始"), (0, {}))
        self.assertEqual(self.hook("PostCompact", trigger="manual", prompt_id=self.prompt), (0, {}))
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_subagent_events_are_ignored(self):
        self.hook("PostCompact", trigger="auto", prompt_id=self.prompt, agent_id="sub-1", agent_type="Explore")
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_stop_without_prompt_id_is_silent(self):
        self.compact()
        self.assertEqual(self.hook("Stop", last_assistant_message="完成", stop_hook_active=False), (0, {}))

    def test_stop_repairs_missing_evaluation_once(self):
        self.compact()
        self.context()
        rc, first = self.hook("Stop", prompt_id=self.prompt, last_assistant_message="本轮完成了 X。",
                              stop_hook_active=False)
        self.assertEqual((rc, first.get("decision")), (0, "block"))
        self.assertIn("评估", first["reason"])
        rc, second = self.hook("Stop", prompt_id=self.prompt, last_assistant_message="本轮完成了 X。",
                               stop_hook_active=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("decision", second)
        self.assertIn("systemMessage", second)
        audit = self.core("--action", "status")["stop_audit"]
        self.assertEqual((audit["turn_id"], audit["missed"], audit["issue"]),
                         (self.prompt, True, "evaluation_missing"))

    def test_skip_evaluation_silences_stop_and_next_prompt(self):
        self.compact()
        self.context()
        self.core("--action", "evaluate", "--turn-id", self.prompt, "--outcome", "skip",
                  "--note", "讨论本 Skill", "--next-check", "next_compaction")
        self.assertEqual(self.hook("Stop", prompt_id=self.prompt, last_assistant_message="完成",
                                   stop_hook_active=False), (0, {}))
        self.prompt = str(uuid.uuid4())
        self.assertNotIn("待评估", self.context())

    def prepare(self):
        self.compact()
        self.context()
        result = self.core("--action", "prepare", "--turn-id", self.prompt, "--reason", "count",
                           "--stage-key", "simulated-stage", "--safe", "--has-next", "--notice", NOTICE)
        self.assertTrue(result["prepared"])
        return result["proposal"]

    def test_final_notice_in_first_paragraph_counts_once(self):
        self.prepare()
        message = NOTICE + "\n\n下面是本轮成果列表。"
        self.assertEqual(self.hook("Stop", prompt_id=self.prompt, last_assistant_message=message,
                                   stop_hook_active=False), (0, {}))
        state = self.core("--action", "status")
        self.assertEqual(state["tracked_reminder_count"], 1)
        self.assertTrue(state["proposal"]["final_delivered"])
        self.assertEqual(state["next_reminder_at"], 6)
        self.hook("Stop", prompt_id=self.prompt, last_assistant_message=message, stop_hook_active=True)
        self.assertEqual(self.core("--action", "status")["tracked_reminder_count"], 1)

    def test_notice_after_other_text_is_repaired_once_then_marked_missed(self):
        self.prepare()
        message = "先汇报成果。\n\n" + NOTICE
        rc, first = self.hook("Stop", prompt_id=self.prompt, last_assistant_message=message,
                              stop_hook_active=False)
        self.assertEqual((rc, first.get("decision")), (0, "block"))
        self.assertIn(NOTICE, first["reason"])
        rc, second = self.hook("Stop", prompt_id=self.prompt, last_assistant_message=message,
                               stop_hook_active=True)
        self.assertIn("systemMessage", second)
        state = self.core("--action", "status")
        self.assertEqual(state["tracked_reminder_count"], 0)
        self.assertTrue(state["proposal"]["final_missed"])

    def test_bad_input_fails_open_with_system_message(self):
        rc, out = self.hook("PostCompact", raw="not json")
        self.assertEqual(rc, 0)
        self.assertIn("计数不可用", out["systemMessage"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClaudeHookTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
