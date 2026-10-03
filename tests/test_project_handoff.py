"""Behavioral checks in retained, isolated fixtures. No live session writes."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
QA = REPO / ".test-output" / "project-handoff"
QA.mkdir(parents=True, exist_ok=True)
SCRIPT = REPO / "project-handoff" / "scripts" / "compaction_reminder.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("reminder", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# Short temp root keeps lock paths under the Windows 260-character limit
# regardless of where the repository is checked out.
RUN = Path(tempfile.mkdtemp(prefix="ph-"))
NOTICE = "交接建议：设计已经确认，后续进入样片；确认后保存材料，在同一项目新建并打开任务接续。"


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.root = RUN / hashlib.sha256(self._testMethodName.encode()).hexdigest()[:12]
        self.sid = "main-session"
        self.turn = "turn-a"

    def event(self, name, **extra):
        return dict(session_id=self.sid, turn_id=self.turn, hook_event_name=name, **extra)

    def hook(self, name, **extra):
        return m.process(self.event(name, **extra), self.root)

    def compact(self, n=1):
        for _ in range(n):
            self.hook("PostCompact", trigger="auto")

    def status(self):
        return m.process(self.event(""), self.root, "status")

    def prep(self, reason="count", stage="design", **kw):
        args = dict(stage_key=stage, safe=True, has_next=True, notice=NOTICE)
        args.update(kw)
        return m.process(self.event(""), self.root, "prepare", reason=reason, **args)

    def pid(self):
        return self.status()["proposal"]["id"]

    def reply(self, value, turn="response-a", **kw):
        args = dict(proposal_id=self.pid(), **kw)
        return m.process(dict(session_id=self.sid, turn_id=turn), self.root,
                         "respond", response=value, **args)

    def delivered(self):
        self.compact(3)
        self.assertTrue(self.prep()["prepared"])
        self.hook("Stop", last_assistant_message=NOTICE + "\n\n已完成交付。")

    def test_status_is_readonly_even_for_missing_state(self):
        self.assertEqual(self.status()["auto_count"], 0)
        self.assertFalse(self.root.exists())

    def test_initial_threshold_and_prepare_not_delivery(self):
        self.compact(2)
        self.assertFalse(self.prep()["prepared"])
        self.compact()
        self.assertTrue(self.prep()["prepared"])
        self.assertEqual(self.status()["reminder_count"], 0)

    def test_manual_resume_and_prompt_never_increment(self):
        self.compact()
        for _ in range(3):
            self.hook("PostCompact", trigger="manual")
            self.hook("SessionStart", source="resume")
            self.hook("UserPromptSubmit", prompt="继续，压缩")
        self.assertEqual(self.status()["auto_count"], 1)

    def test_many_compactions_in_one_turn_are_distinct(self):
        self.compact(6)
        self.assertEqual(self.status()["auto_count"], 6)

    def test_no_proposal_still_checks_missing_evaluation(self):
        self.compact(9)
        self.assertEqual(self.hook("Stop", last_assistant_message="普通任务已完成")["decision"], "block")
        self.assertEqual(self.status()["reminder_count"], 0)

    def test_unsafe_or_finished_work_cannot_prepare(self):
        self.compact(3)
        self.assertFalse(self.prep(safe=False)["prepared"])
        self.assertFalse(self.prep(has_next=False)["prepared"])

    def test_host_turn_identity_cannot_be_guessed(self):
        self.compact(3)
        self.turn = "wrong-turn"
        with self.assertRaises(ValueError):
            self.prep()

    def test_final_counts_once_and_keeps_cooldown(self):
        self.compact(3)
        self.prep()
        self.compact()
        self.hook("Stop", last_assistant_message="**" + NOTICE + "**\n\n成果如下。")
        s = self.status()
        self.assertEqual((s["reminder_count"], s["last_notified_at"], s["next_reminder_at"]), (1, 4, 7))
        self.assertTrue(s["proposal"]["final_delivered"])

    def test_final_only_counts_after_stop_evidence(self):
        self.compact(3)
        self.prep()
        self.assertEqual(self.status()["reminder_count"], 0)
        self.hook("Stop", last_assistant_message=NOTICE)
        self.assertEqual(self.status()["reminder_count"], 1)

    def test_no_manual_receipt_action(self):
        self.compact(3)
        self.prep()
        before = m.state_path(self.root, self.sid).read_bytes()
        with self.assertRaises(ValueError):
            m.process(self.event(""), self.root, "notified", proposal_id=self.pid(), channel="final")
        self.assertEqual(m.state_path(self.root, self.sid).read_bytes(), before)
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--state-dir", str(self.root),
                   "--session-id", self.sid, "--turn-id", self.turn, "--action", "notified"]
        result = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()["reminder_count"], 0)

    def test_missing_final_repairs_once_then_records_failure(self):
        self.compact(3)
        self.prep()
        first = self.hook("Stop", last_assistant_message="已完成")
        self.assertEqual(first["decision"], "block")
        second = self.hook("Stop", last_assistant_message="还是漏了", stop_hook_active=True)
        self.assertIn("systemMessage", second)
        self.assertEqual(self.hook("Stop", last_assistant_message="还是漏了"), {})
        self.assertEqual(self.status()["reminder_count"], 0)
        self.assertTrue(self.status()["proposal"]["final_missed"])

    def test_repair_success_counts_once(self):
        self.compact(3)
        self.prep()
        self.hook("Stop", last_assistant_message="已完成")
        self.hook("Stop", last_assistant_message=NOTICE + "\n\n已完成", stop_hook_active=True)
        self.hook("Stop", last_assistant_message=NOTICE)
        self.assertEqual(self.status()["reminder_count"], 1)

    def test_other_stop_hook_continuation_does_not_start_a_retry_loop(self):
        self.compact(3)
        self.prep()
        self.assertNotIn("decision", self.hook("Stop", last_assistant_message="结果", stop_hook_active=True))

    def test_stale_turn_cannot_check_or_repeat_previous_notice(self):
        self.compact(3)
        self.prep()
        self.turn = "unrelated-turn"
        self.assertEqual(self.hook("Stop", last_assistant_message="普通答复"), {})
        self.assertFalse(self.status()["proposal"]["retry_used"])

    def test_examples_are_not_delivery(self):
        for text in ("> " + NOTICE, "```text\n" + NOTICE + "\n```", "示例：" + NOTICE):
            self.assertFalse(m.final_contains_notice(text, NOTICE))
        self.assertTrue(m.final_contains_notice(NOTICE + "\n\n结果\n\n<oai-mem-citation>meta", NOTICE))

    def test_cooldown_alone_does_not_trigger_repeat(self):
        self.delivered()
        self.compact(30)
        self.assertFalse(self.prep(reason="count", stage="full")["prepared"])
        self.assertFalse(self.prep(reason="stage", stage="full")["prepared"])

    def test_new_stage_cannot_bypass_cooldown(self):
        self.delivered()
        self.compact(2)
        self.assertFalse(self.prep(reason="stage", stage="sample", benefit=True)["prepared"])
        self.compact()
        self.assertTrue(self.prep(reason="stage", stage="sample", benefit=True)["prepared"])

    def test_same_stage_versions_and_revisit_are_deduplicated(self):
        self.delivered()
        self.compact(3)
        self.assertFalse(self.prep(reason="stage", stage="design", benefit=True)["prepared"])
        self.prep(reason="stage", stage="sample", benefit=True)
        self.hook("Stop", last_assistant_message=NOTICE)
        self.compact(3)
        self.assertFalse(self.prep(reason="stage", stage="design", benefit=True)["prepared"])

    def test_user_continue_cools_from_response_and_is_not_approval(self):
        self.delivered()
        self.compact(2)
        self.reply("continue")
        s = self.status()
        self.assertEqual(s["next_reminder_at"], 8)
        self.assertFalse(s["muted"])
        self.assertEqual(s["response"], "continue")
        self.compact(2)
        self.reply("continue")
        self.assertEqual(self.status()["next_reminder_at"], 8)

    def test_continue_before_receipt_still_suppresses_first_count_route(self):
        self.compact(3)
        self.prep()
        self.reply("continue")
        self.compact(3)
        self.assertFalse(self.prep(reason="count", stage="sample")["prepared"])
        self.assertTrue(self.prep(reason="stage", stage="sample", benefit=True)["prepared"])

    def test_independent_hook_processes_do_not_lose_increments(self):
        from concurrent.futures import ThreadPoolExecutor
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--state-dir", str(self.root)]
        event = json.dumps(self.event("PostCompact", trigger="auto"))
        def invoke(_):
            return subprocess.run(command, input=event, text=True, encoding="utf-8",
                                  capture_output=True, timeout=8)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(invoke, range(12)))
        self.assertTrue(all(r.returncode == 0 and not r.stdout.strip() for r in results))
        self.assertEqual(self.status()["auto_count"], 12)

    def test_ordinary_continue_prompt_is_not_a_response(self):
        self.delivered()
        self.turn = "turn-next"
        self.hook("UserPromptSubmit", prompt="同意修改这个 Skill，继续")
        self.assertEqual(self.status()["response"], "pending")
        self.assertFalse(self.status()["muted"])

    def test_wrong_proposal_response_preserves_state(self):
        self.delivered()
        before = m.state_path(self.root, self.sid).read_bytes()
        with self.assertRaises(ValueError):
            m.process(dict(session_id=self.sid, turn_id="response-b"), self.root,
                      "respond", response="continue", proposal_id="wrong")
        self.assertEqual(before, m.state_path(self.root, self.sid).read_bytes())

    def test_defer_until_checkpoint_ignores_count_and_other_stages(self):
        self.delivered()
        self.reply("defer", checkpoint_key="sample-ready")
        self.compact(30)
        self.assertFalse(self.prep(reason="stage", stage="full", benefit=True)["prepared"])
        self.assertFalse(self.prep(reason="checkpoint", checkpoint_key="wrong")["prepared"])
        self.assertTrue(self.prep(reason="checkpoint", checkpoint_key="sample-ready")["prepared"])

    def test_user_checkpoint_does_not_require_extra_compactions(self):
        self.delivered()
        self.reply("defer", checkpoint_key="sample-ready")
        self.assertTrue(self.prep(reason="checkpoint", stage="sample", checkpoint_key="sample-ready")["prepared"])

    def test_verified_new_confusion_bypasses_cooldown_but_not_mute(self):
        self.delivered()
        self.assertTrue(self.prep(reason="confusion", cause_key="wrong-master", benefit=True)["prepared"])
        self.hook("Stop", last_assistant_message=NOTICE)
        self.compact(5)
        self.assertFalse(self.prep(reason="confusion", cause_key="wrong-master", benefit=True)["prepared"])
        self.reply("mute")
        self.assertFalse(self.prep(reason="confusion", cause_key="new-error", benefit=True)["prepared"])

    def test_mute_resume_and_handoff_only_change_reminder_state(self):
        self.delivered()
        self.reply("mute")
        self.compact(10)
        self.assertFalse(self.prep(reason="stage", stage="sample", benefit=True)["prepared"])
        self.reply("resume", turn="response-b")
        self.assertTrue(self.prep(reason="stage", stage="sample", benefit=True)["prepared"])
        self.reply("handoff", turn="response-c")
        self.assertTrue(self.status()["muted"])
        self.assertEqual({p.suffix for p in self.root.iterdir()}, {".json", ".lock"})

    def test_cancel_prevents_stale_final_repair(self):
        self.compact(3)
        self.prep()
        m.process(self.event(""), self.root, "cancel", proposal_id=self.pid())
        self.assertEqual(self.hook("Stop", last_assistant_message="任务已结束"), {})
        self.assertEqual(self.status()["reminder_count"], 0)

    def test_repeat_prepare_and_interruption_rearm_keep_one_identity(self):
        self.compact(3)
        self.prep()
        identity = self.pid()
        self.prep()
        self.turn = "resumed-turn"
        self.hook("UserPromptSubmit", prompt="处理下一步")
        self.prep()
        self.assertEqual(self.pid(), identity)
        self.hook("Stop", last_assistant_message=NOTICE)
        self.assertEqual(self.status()["reminder_count"], 1)

    def test_parent_child_isolation_and_distinct_sessions(self):
        self.hook("PostCompact", trigger="auto", agent_id="child")
        self.assertFalse(self.root.exists())
        self.compact(3)
        other = m.process(dict(session_id="other"), self.root, "status")
        self.assertEqual(other["auto_count"], 0)
        self.assertEqual(self.status()["auto_count"], 3)

    def test_transcript_metadata_subagent_isolation(self):
        self.root.mkdir(parents=True)
        path = self.root / "child.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "child", "source": {"subagent": {}}}}), encoding="utf-8")
        self.hook("PostCompact", trigger="auto", transcript_path=str(path))
        self.assertFalse(m.state_path(self.root, self.sid).exists())

    def test_legacy_ten_is_not_ten_notifications_and_status_preserves_bytes(self):
        self.root.mkdir(parents=True)
        legacy = dict(schema=1, session_id=self.sid, auto_count=10, next_reminder_at=13,
                      muted=False, last_notified_at=10, last_reason="count", response="pending")
        path = m.state_path(self.root, self.sid)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        s = self.status()
        self.assertIsNone(s["reminder_count"])
        self.assertEqual(s["tracked_reminder_count"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix(".lock").exists())
        self.compact(3)
        self.prep(reason="stage", stage="full", benefit=True)
        self.hook("Stop", last_assistant_message=NOTICE)
        s = self.status()
        self.assertIsNone(s["reminder_count"])
        self.assertEqual((s["tracked_reminder_count"], s["auto_count"]), (1, 13))

    def test_corrupt_state_is_preserved(self):
        self.root.mkdir(parents=True)
        path = m.state_path(self.root, self.sid)
        path.write_text('{"schema":999}', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.compact()
        self.assertEqual(path.read_bytes(), before)

    def test_cli_stop_json_and_fail_open_error(self):
        self.compact(3)
        self.prep()
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--state-dir", str(self.root)]
        result = subprocess.run(command, input=json.dumps(self.event("Stop", last_assistant_message="成果")),
                                text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        result = subprocess.run(command, input="{}", text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        self.assertIn("systemMessage", json.loads(result.stdout))

    def evaluation(self, outcome="defer", next_check="next_compaction", note="仍在同一阶段，没有新的切换收益"):
        return m.process(self.event(""), self.root, "evaluate", outcome=outcome,
                         next_check=next_check, note=note)

    def test_prepare_alone_is_not_delivery_or_cooldown(self):
        self.compact(3); self.prep()
        s=self.status()
        self.assertEqual(s["reminder_count"],0)
        self.assertEqual(s["next_reminder_at"],3)
        self.assertIsNone(s["last_notified_at"])
        self.assertEqual(s["stage_keys"],[])

    def test_evaluation_defers_until_next_compaction_not_every_turn(self):
        self.compact(6); self.evaluation()
        self.assertEqual(self.hook("Stop",last_assistant_message="成果"),{})
        self.turn="later";self.hook("UserPromptSubmit")
        self.assertEqual(self.hook("Stop",last_assistant_message="问答"),{})
        self.compact()
        self.assertEqual(self.hook("Stop",last_assistant_message="成果")["decision"],"block")

    def test_unsafe_rechecks_next_turn(self):
        self.compact(3);self.evaluation(next_check="next_turn",note="文件写入尚未安全收拢，下轮完成后重评")
        self.assertEqual(self.hook("Stop",last_assistant_message="进展"),{})
        self.turn="next";self.hook("UserPromptSubmit")
        self.assertEqual(self.hook("Stop",last_assistant_message="成果")["decision"],"block")

    def test_negative_decision_requires_reason_and_next_check(self):
        self.compact(3)
        for opts in [dict(note=""),dict(next_check="never"),dict(outcome="remind")]:
            with self.assertRaises(ValueError):self.evaluation(**opts)
        self.assertIsNone(self.status()["evaluation"])

    def test_one_stop_retry_shared_by_missing_evaluation_and_final(self):
        self.compact(3)
        self.assertEqual(self.hook("Stop",last_assistant_message="成果")["decision"],"block")
        self.prep()
        self.assertNotIn("decision",self.hook("Stop",last_assistant_message="遗漏",stop_hook_active=True))
        self.assertEqual(self.status()["reminder_count"],0)
        self.assertEqual(self.hook("Stop",last_assistant_message="遗漏"),{})

    def test_skip_discussion_repairs_without_reminding(self):
        self.compact(6)
        self.hook("Stop",last_assistant_message="Skill 审查完成")
        self.evaluation(outcome="skip",note="本轮只讨论交接 Skill，不执行交接")
        self.assertEqual(self.hook("Stop",last_assistant_message="Skill 审查完成",stop_hook_active=True),{})
        self.assertEqual(self.status()["reminder_count"],0)

    def test_delivered_old_proposal_does_not_hide_due_evaluation(self):
        self.delivered();self.compact(3)
        self.turn="next";self.hook("UserPromptSubmit")
        self.assertEqual(self.hook("Stop",last_assistant_message="成果")["decision"],"block")

    def test_v2_commentary_only_migration_preserves_counter_and_uncertainty(self):
        self.compact(3);self.prep()
        path=m.state_path(self.root,self.sid);s=self.status()
        s.update(tracking_version=2,reminder_count=1,tracked_reminder_count=1,
                 last_notified_at=3,next_reminder_at=6,stage_keys=["design"])
        s["proposal"].update(counted=True,commentary_delivered=True,final_delivered=False)
        path.write_text(json.dumps(s),encoding="utf-8")
        original=path.read_bytes();s=self.status()
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(s["auto_count"],3)
        self.assertEqual(s["reminder_count"],0)
        self.assertEqual(s["next_reminder_at"],3)
        self.assertEqual(s["legacy_delivery_counts"]["reminder_count"],1)

    def test_v2_multiple_reminders_do_not_invent_final_total(self):
        self.compact(9);p=m.state_path(self.root,self.sid);s=self.status()
        s.update(tracking_version=2,reminder_count=4,tracked_reminder_count=4,last_notified_at=6)
        p.write_text(json.dumps(s),encoding="utf-8")
        self.assertIsNone(self.status()["reminder_count"])
        self.assertIn("历史最终总数未知",json.dumps(self.hook("UserPromptSubmit"),ensure_ascii=False))

    def test_ready_evaluation_cannot_replace_notice_delivery(self):
        self.compact(3);self.prep()
        self.assertEqual(self.status()["evaluation"]["decision"],"remind")
        self.assertEqual(self.hook("Stop",last_assistant_message="已评估")["decision"],"block")

    def test_old_turn_evaluation_cannot_mask_current_checkpoint(self):
        self.compact(3);self.turn="wrong"
        with self.assertRaises(ValueError):self.evaluation()


    def test_notice_after_cards_is_not_accepted_as_front_notice(self):
        self.compact(3);self.prep()
        self.assertEqual(self.hook("Stop",last_assistant_message="::created-thread{threadId=x}\n\n"+NOTICE)["decision"],"block")
        self.assertEqual(self.status()["reminder_count"],0)
        self.hook("Stop",last_assistant_message=NOTICE+"\n\n成果",stop_hook_active=True)
        self.assertEqual(self.status()["reminder_count"],1)

    def test_failed_prepare_still_requires_negative_evaluation(self):
        self.compact(3);self.assertFalse(self.prep(safe=False)["prepared"])
        self.assertEqual(self.hook("Stop",last_assistant_message="未收拢")["decision"],"block")

    def test_user_defer_suppresses_automatic_evaluation_until_checkpoint(self):
        self.delivered();self.reply("defer",checkpoint_key="ready");self.compact(20)
        self.turn="new";self.hook("UserPromptSubmit")
        self.assertEqual(self.hook("Stop",last_assistant_message="进展"),{})

    def test_cancel_rejects_stale_turn_without_writing(self):
        self.compact(3);self.prep();self.turn="wrong"
        before=m.state_path(self.root,self.sid).read_bytes()
        with self.assertRaises(ValueError):
            m.process(self.event(""),self.root,"cancel",proposal_id=self.pid())
        self.assertEqual(m.state_path(self.root,self.sid).read_bytes(),before)


    def test_prompt_body_is_not_saved_in_state(self):
        self.compact()
        self.hook("UserPromptSubmit", prompt="PRIVATE-SENTINEL-DO-NOT-SAVE")
        saved = m.state_path(self.root, self.sid).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE-SENTINEL", saved)

    def test_same_session_subagent_source_is_ignored(self):
        self.root.mkdir(parents=True)
        path = self.root / "subagent.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.sid, "source": {"subagent": {"spawn": {}}}}}), encoding="utf-8")
        self.hook("PostCompact", trigger="auto", transcript_path=str(path))
        self.assertEqual(self.status()["auto_count"], 0)

    def test_malformed_json_cli_preserves_original_file(self):
        self.compact()
        path = m.state_path(self.root, self.sid)
        path.write_text("broken-json", encoding="utf-8")
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--state-dir", str(self.root)]
        result = subprocess.run(command, input=json.dumps(self.event("PostCompact", trigger="auto")),
                                text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        self.assertIn("systemMessage", json.loads(result.stdout))
        self.assertEqual(path.read_text(encoding="utf-8"), "broken-json")

    def test_cli_prompt_context_does_not_request_stop_continuation(self):
        self.compact(3)
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--state-dir", str(self.root)]
        result = subprocess.run(command, input=json.dumps(self.event("UserPromptSubmit")),
                                text=True, encoding="utf-8", capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("首次", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("decision", output)
        self.assertNotIn("continue", output)

    def test_windows_hook_template_runs_under_powershell(self):
        template = json.loads((REPO / "project-handoff" / "hooks" / "codex-hooks.example.json").read_text(encoding="utf-8"))
        handlers = {name: groups[0]["hooks"][0] for name, groups in template["hooks"].items()}
        self.assertEqual(set(handlers), {"SessionStart", "UserPromptSubmit", "PostCompact", "Stop"})
        for handler in handlers.values():
            self.assertEqual(handler["commandWindows"], "& " + handler["command"])
        pwsh = shutil.which("pwsh")
        if os.name != "nt" or not pwsh:
            self.skipTest("needs Windows and PowerShell 7")
        command = (handlers["PostCompact"]["commandWindows"].replace("<PYTHON_EXE>", sys.executable)
                   .replace("<SKILL_DIRECTORY>", SCRIPT.parents[1].as_posix())
                   .replace("<CODEX_HOME>", self.root.as_posix()))
        result = subprocess.run([pwsh, "-NoProfile", "-Command", command],
                                input=json.dumps(self.event("PostCompact", trigger="auto")),
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = m.process(self.event(""), self.root / "state" / "project-handoff", "status")
        self.assertEqual(state["auto_count"], 1)

    def age(self, path, days):
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))

    def test_auto_compaction_sweeps_only_stale_own_state(self):
        self.compact()
        stale = m.state_path(self.root, "old-session")
        fresh = m.state_path(self.root, "recent-session")
        for p in (stale, stale.with_suffix(".lock"), fresh, fresh.with_suffix(".lock")):
            p.write_text("{}", encoding="utf-8")
        foreign = self.root / "notes.json"
        foreign.write_text("{}", encoding="utf-8")
        for p in (stale, stale.with_suffix(".lock"), foreign):
            self.age(p, 61)
        self.age(fresh, 59)
        self.age(fresh.with_suffix(".lock"), 90)  # Lock age follows its .json.
        own = m.state_path(self.root, self.sid)
        self.age(own, 90)  # The calling session is never swept.
        self.compact()
        self.assertFalse(stale.exists())
        self.assertFalse(stale.with_suffix(".lock").exists())
        self.assertTrue(fresh.exists() and fresh.with_suffix(".lock").exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(self.status()["auto_count"], 2)

    def test_prompts_and_manual_compaction_do_not_sweep(self):
        self.compact()
        stale = m.state_path(self.root, "old-session")
        stale.write_text("{}", encoding="utf-8")
        self.age(stale, 61)
        self.hook("UserPromptSubmit")
        self.hook("PostCompact", trigger="manual")
        self.assertTrue(stale.exists())

    def test_not_due_context_is_one_short_line(self):
        self.compact()
        ctx = self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, "project-handoff：自动压缩1次；当前turn_id=turn-a；冷却至第3次压缩，期间不提醒。")
        self.compact(5)
        self.evaluation()
        ctx = self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("本压缩点已评估", ctx)
        self.assertNotIn("执行前读取", ctx)
        self.compact()
        self.assertIn("执行前读取", self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"])

    def test_relative_links_in_skill_docs_resolve(self):
        skill = REPO / "project-handoff"
        docs = [skill / "SKILL.md", *sorted((skill / "references").glob("*.md"))]
        for doc in docs:
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text(encoding="utf-8")):
                if "://" in target:
                    continue
                self.assertTrue((doc.parent / target).resolve().exists(), f"{doc.name} -> {target}")

    def test_state_template_has_markers_and_handoff_fields(self):
        text = (REPO / "project-handoff" / "assets" / "PROJECT_STATE.template.md").read_text(encoding="utf-8")
        begin = re.search(r"<!-- (\S+)-HANDOFF-BEGIN (\S+) -->", text)
        self.assertIsNotNone(begin)
        self.assertIn(f"<!-- {begin.group(1)}-HANDOFF-END {begin.group(2)} -->", text)
        for section in ("交接区", "任务与版本", "权威资料", "决定与限制", "进展与运行状态", "接续动作"):
            self.assertIn(f"### {section}", text)
        for status in ("材料已核对", "等待接手核验", "准备接续", "已接手", "受阻"):
            self.assertIn(status, text)

    def test_new_state_is_v3_without_legacy_snapshot(self):
        self.compact()
        saved = json.loads(m.state_path(self.root, self.sid).read_text(encoding="utf-8"))
        self.assertEqual((saved["tracking_version"], saved["reminder_count"]), (3, 0))
        self.assertNotIn("legacy_delivery_counts", saved)
        self.assertNotIn("commentary_delivered", json.dumps(saved))


if __name__ == "__main__":
    started = time.time()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReminderTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    record = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "seconds": round(time.time()-started, 3), "fixture_dir": str(RUN),
              "failure_details": [{"test": str(t), "traceback": detail}
                                  for t, detail in result.failures + result.errors]}
    (QA / "test-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    raise SystemExit(not result.wasSuccessful())
