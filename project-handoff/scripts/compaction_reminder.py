"""Codex lifecycle adapter. No network, model calls, or business-file writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from contextlib import contextmanager

if os.name == "nt":
    import msvcrt
else:
    import fcntl

INTERVAL = 3
RETENTION_DAYS = 60
SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
STATE_NAME = re.compile(r"[0-9a-f]{64}")


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 2
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock timed out")
                time.sleep(0.025)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_subagent(event):
    if event.get("agent_id") or event.get("agent_transcript_path"):
        return True
    # Read only the supplied transcript's metadata, never its conversation body.
    transcript = event.get("transcript_path")
    if transcript:
        try:
            with Path(transcript).open(encoding="utf-8-sig") as handle:
                record = json.loads(handle.readline(65536))
            if record.get("type") == "session_meta":
                meta = record.get("payload", {})
                source = meta.get("source")
                return (meta.get("id") != event.get("session_id") or
                        (isinstance(source, dict) and "subagent" in source))
        except (OSError, ValueError):
            pass  # Metadata is optional; lifecycle fields remain authoritative.
    return False


def state_path(root, session):
    return root / (hashlib.sha256(session.encode()).hexdigest() + ".json")


def fresh_state(session):
    return {
        "schema": 1, "session_id": session, "auto_count": 0,
        "next_reminder_at": INTERVAL, "muted": False,
        "last_notified_at": None, "last_reason": None,
        "response": None, "coverage": "observed_since_hook_enabled",
        "tracking_version": 3, "reminder_count": 0, "tracked_reminder_count": 0,
        "stage_keys": [], "confusion_keys": [], "proposal": None, "defer_until": None,
        "last_response_turn_id": None, "active_turn_id": None,
        "evaluation": None, "stop_audit": None,
    }


def sweep(root, keep):
    """Remove this skill's own state files untouched for RETENTION_DAYS.

    Runs on automatic compaction only. Staleness is judged by the .json file so
    an active session's lock file is never removed from under it. Only names
    this script creates are touched. Failures are ignored: housekeeping must
    not affect counting.
    """
    cutoff = time.time() - RETENTION_DAYS * 86400
    groups = {}
    try:
        for p in root.iterdir():
            if (STATE_NAME.fullmatch(p.stem) and p.suffix in {".json", ".lock", ".tmp"}
                    and p.stem != keep.stem):
                groups.setdefault(p.stem, []).append(p)
    except OSError:
        return
    # Decide per session before deleting, so a lock never outlives its state file.
    for files in groups.values():
        anchors = [p for p in files if p.suffix == ".json"] or files
        try:
            if max(p.stat().st_mtime for p in anchors) >= cutoff:
                continue
        except OSError:
            continue
        for entry in sorted(files, key=lambda p: p.suffix != ".json"):
            try:
                entry.unlink()
            except OSError:
                pass  # In use or already gone; retry at a later compaction.


def read_state(path, session):
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else fresh_state(session)
    if (state.get("schema") != 1 or state.get("session_id") != session or
            type(state.get("auto_count")) is not int or state["auto_count"] < 0 or
            type(state.get("next_reminder_at")) is not int or
            state["next_reminder_at"] < INTERVAL or
            type(state.get("muted")) is not bool):
        raise ValueError("invalid state; original file preserved")
    # Additive extension: old adapters can still read the original fields.
    # A legacy receipt contains a compaction index, not a notification total.
    if "tracking_version" not in state:
        state.update(tracking_version=2,
                     reminder_count=0 if state["last_notified_at"] is None else None,
                     tracked_reminder_count=0, stage_keys=[], confusion_keys=[],
                     proposal=None, defer_until=None, last_response_turn_id=None,
                     active_turn_id=None)
    if (state.get("tracking_version") not in {2, 3} or
            type(state.get("tracked_reminder_count")) is not int or
            state["tracked_reminder_count"] < 0 or
            (state.get("reminder_count") is not None and
             (type(state["reminder_count"]) is not int or state["reminder_count"] < 0)) or
            not isinstance(state.get("stage_keys"), list) or
            not isinstance(state.get("confusion_keys"), list) or
            (state.get("proposal") is not None and not isinstance(state["proposal"], dict))):
        raise ValueError("invalid tracking fields; original file preserved")
    if state["tracking_version"] == 2:
        # Old counts included commentary. Preserve evidence; never infer old
        # final delivery totals from a compaction index or a discarded proposal.
        state["legacy_delivery_counts"] = {k: state.get(k) for k in
            ("reminder_count", "tracked_reminder_count", "last_notified_at")}
        proposal = state["proposal"]
        total = state["reminder_count"]
        known_single = total == 1 and proposal and proposal.get("counted")
        if total == 0:
            pass
        elif known_single:
            state["reminder_count"] = int(bool(proposal.get("final_delivered")))
            if not proposal.get("final_delivered"):
                proposal["counted"] = False
                if not proposal.get("closed"):
                    state["stage_keys"] = [k for k in state["stage_keys"] if k != proposal["stage_key"]]
                state["last_notified_at"] = None
                if state["response"] == "pending":
                    state["next_reminder_at"] = INTERVAL
        else:
            state["reminder_count"] = None
        state["tracked_reminder_count"] = 0
        state.update(tracking_version=3, evaluation=None, stop_audit=None)
    if (state.get("evaluation") is not None and not isinstance(state["evaluation"], dict)) or (
            state.get("stop_audit") is not None and not isinstance(state["stop_audit"], dict)):
        raise ValueError("invalid evaluation state")
    return state


def evaluation_due(state, turn):
    if state["muted"] or state["defer_until"] or state["auto_count"] < state["next_reminder_at"]:
        return False
    evaluation = state.get("evaluation")
    if not evaluation or evaluation["auto_count"] < state["auto_count"]:
        return True
    return evaluation["next_check"] == "next_turn" and evaluation["turn_id"] != turn


def evaluate(state, event, opts):
    turn = key_arg(event.get("turn_id"), "turn_id")
    if state.get("active_turn_id") and turn != state["active_turn_id"]:
        raise ValueError("evaluation must use current host turn")
    decision = opts.get("outcome")
    next_check = opts.get("next_check")
    note = key_arg(opts.get("note"), "evaluation note")
    if decision not in {"defer", "skip"} or next_check not in {"next_turn", "next_compaction"}:
        raise ValueError("choose defer/skip and next_turn/next_compaction")
    state["evaluation"] = dict(turn_id=turn, auto_count=state["auto_count"],
        decision=decision, note=note, next_check=next_check)
    # A current negative decision withdraws an unfulfilled proposal, not a user response.
    proposal = state["proposal"]
    if proposal and not proposal["closed"] and not proposal["final_delivered"]:
        proposal["closed"] = True
    return {"evaluated": True, "evaluation": state["evaluation"]}


def key_arg(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(f"missing or invalid {label}")
    return value.strip()


def prepare(state, event, reason, opts):
    turn = key_arg(event.get("turn_id"), "turn_id")
    if state.get("active_turn_id") and turn != state["active_turn_id"]:
        raise ValueError("turn_id differs from the current host event")
    stage = key_arg(opts.get("stage_key"), "stage_key")
    notice = opts.get("notice", "").strip()
    if not notice.startswith("交接建议：") or not 20 <= len(notice) <= 600 or "\n" in notice:
        raise ValueError("notice must be one bounded handoff suggestion paragraph")
    if reason not in {"count", "stage", "confusion", "checkpoint"}:
        raise ValueError("invalid reminder reason")
    if state["muted"] or not opts.get("safe") or not opts.get("has_next"):
        return {"prepared": False, "why": "muted_or_not_ready"}
    old = state["proposal"]
    # Re-arm an interrupted/missed delivery, keeping the same ID and count.
    if (old and not old["closed"] and not old["final_delivered"] and
            old["stage_key"] == stage and old["reason"] == reason and
            old["cause_key"] == opts.get("cause_key")):
        old.update(turn_id=turn, notice=notice)
        record_ready(state, turn)
        return {"prepared": True, "proposal": old}
    if state["defer_until"] and reason not in {"checkpoint", "confusion"}:
        return {"prepared": False, "why": "waiting_for_user_checkpoint"}
    cause = opts.get("cause_key")
    if reason == "confusion":
        cause = key_arg(cause, "cause_key")
        if cause in state["confusion_keys"] or not opts.get("benefit"):
            return {"prepared": False, "why": "same_confusion_or_no_benefit"}
    elif reason == "checkpoint":
        if not state["defer_until"] or opts.get("checkpoint_key") != state["defer_until"]:
            return {"prepared": False, "why": "checkpoint_not_matched"}
    else:
        has_prior = state["last_notified_at"] is not None or bool(state["stage_keys"])
        if stage in state["stage_keys"]:
            return {"prepared": False, "why": "same_work_stage"}
        if reason == "count":
            if (has_prior or
                    state["auto_count"] < state["next_reminder_at"]):
                return {"prepared": False, "why": "count_only_is_first_reminder"}
        elif not opts.get("benefit"):
            return {"prepared": False, "why": "no_switching_benefit"}
        if (has_prior and
                state["auto_count"] < state["next_reminder_at"]):
            return {"prepared": False, "why": "cooldown"}
    identity = json.dumps([state["session_id"], turn, reason, stage, cause], ensure_ascii=False)
    proposal = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                "turn_id": turn, "reason": reason, "stage_key": stage,
                "cause_key": cause, "notice": notice, "counted": False,
                "final_delivered": False,
                "retry_used": False, "final_missed": False, "closed": False}
    state.update(proposal=proposal, response="pending")
    record_ready(state, turn)
    if reason == "checkpoint":
        state["defer_until"] = None
    return {"prepared": True, "proposal": proposal}


def record_ready(state, turn):
    state["evaluation"] = dict(turn_id=turn, auto_count=state["auto_count"],
        decision="remind", note="已核实安全位置、后续及适用触发条件",
        next_check="next_compaction")


def receipt(state, proposal):
    """Count a final-text delivery once; the only evidence is the host Stop event."""
    if proposal["closed"]:
        raise ValueError("proposal already closed")
    proposal["final_delivered"] = True
    if not proposal["counted"]:
        proposal["counted"] = True
        state["tracked_reminder_count"] += 1
        if state["reminder_count"] is not None:
            state["reminder_count"] += 1
        state["last_notified_at"] = state["auto_count"]
        state["last_reason"] = proposal["reason"]
        state["next_reminder_at"] = max(state["next_reminder_at"], state["auto_count"] + INTERVAL)
        if proposal["stage_key"] not in state["stage_keys"]:
            state["stage_keys"].append(proposal["stage_key"])
        if proposal["reason"] == "confusion":
            state["confusion_keys"].append(proposal["cause_key"])


def respond(state, event, response, opts):
    turn = key_arg(event.get("turn_id"), "response turn_id")
    if response not in {"continue", "defer", "mute", "handoff", "resume"}:
        raise ValueError("invalid response")
    if state["last_response_turn_id"] == turn:
        return  # Re-reading the same response must not slide the cooldown.
    proposal = state["proposal"]
    if response not in {"mute", "resume"}:
        if not proposal or opts.get("proposal_id") != proposal["id"] or proposal["closed"]:
            raise ValueError("response must identify the current open proposal")
    if response == "defer":
        state["defer_until"] = key_arg(opts.get("checkpoint_key"), "checkpoint_key")
    elif response != "resume":
        state["defer_until"] = None
    state["last_response_turn_id"] = turn
    state["response"] = response
    if response in {"mute", "handoff"}:
        state["muted"] = True
    elif response == "resume":
        state["muted"] = False
    else:
        state["next_reminder_at"] = max(state["next_reminder_at"], state["auto_count"] + INTERVAL)
    if proposal:
        proposal["closed"] = True
        if proposal["stage_key"] not in state["stage_keys"]:
            state["stage_keys"].append(proposal["stage_key"])


def final_contains_notice(message, notice):
    if not isinstance(message, str):
        return False
    # Only inspect the host-supplied final text, excluding quoted/code examples.
    message = message.split("<oai-mem-citation>", 1)[0]
    lines, fenced = [], False
    for line in message.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and not line.lstrip().startswith(">"):
            lines.append(line)
    normalize = lambda value: re.sub(r"[\s*`_]", "", value)
    paragraphs = [p for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
    return bool(paragraphs) and normalize(paragraphs[0]) == normalize(notice)


def check_stop(state, event):
    turn = event.get("turn_id")
    if state["muted"] or not turn or (state.get("active_turn_id") and turn != state["active_turn_id"]):
        return {}
    proposal = state["proposal"]
    current = (proposal and not proposal["closed"] and not proposal["final_delivered"]
               and turn == proposal["turn_id"])
    if current and final_contains_notice(event.get("last_assistant_message"), proposal["notice"]):
        receipt(state, proposal)
        proposal["final_missed"] = False
        return {}
    if current:
        issue = "final_missing"
        reason = ("本轮已准备的交接建议漏出最终答复。保留成果，在正文最前单独补上此段；"
                  "不重做任务、不保存交接材料、不新建任务：\n" + proposal["notice"])
    elif evaluation_due(state, turn):
        issue = "evaluation_missing"
        reason = ("project-handoff 到达压缩检查点，但没有本次评估记录。读取 Skill，"
                  "只做一次评估：符合提醒条件则 prepare 并把建议放在最终正文最前；"
                  "否则 evaluate，写明暂不提醒的具体原因和 next_check。"
                  "纯问答、无后续或讨论本 Skill 应记录 skip，不主动提醒。"
                  "保留已有成果；不新建任务、不执行交接。本次最多补漏一次。")
    else:
        return {}
    audit = state.get("stop_audit")
    if not audit or audit["turn_id"] != turn:
        audit = dict(turn_id=turn, retry_used=False, missed=False)
        state["stop_audit"] = audit
    if audit["missed"]:
        return {}
    if not audit["retry_used"] and not event.get("stop_hook_active"):
        audit["retry_used"] = True
        if current:
            proposal["retry_used"] = True
        return {"decision": "block", "reason": reason}
    audit.update(missed=True, issue=issue)
    if current:
        proposal["final_missed"] = True
    return {"systemMessage": "project-handoff：评估或最终提醒仍未核验，本轮停止补漏；未计为正式提醒。"}


def context_output(state, name):
    count = state["auto_count"]
    if count == 0 and not state["proposal"] and not state["muted"]:
        return {}
    turn = state.get("active_turn_id")
    turn_part = f"当前turn_id={turn}；" if turn else ""
    open_proposal = (state["proposal"] and not state["proposal"]["closed"]
                     and not state["proposal"]["final_delivered"])
    due = evaluation_due(state, turn)
    if not (state["muted"] or state["defer_until"] or open_proposal or due):
        # Nothing to register this turn: one line instead of the full rule text.
        if count < state["next_reminder_at"]:
            line = f"冷却至第{state['next_reminder_at']}次压缩，期间不提醒。"
        else:
            line = "本压缩点已评估；进入新的实质阶段时再按 project-handoff 评估。"
        return {"hookSpecificOutput": {"hookEventName": name,
                "additionalContext": f"project-handoff：自动压缩{count}次；{turn_part}{line}"}}
    total = (str(state["reminder_count"]) + "次" if state["reminder_count"] is not None else
             f"历史最终总数未知，新版已核验{state['tracked_reminder_count']}次")
    base = f"project-handoff：自动压缩{count}次；最终文本提醒{total}；{turn_part}"
    if state["last_notified_at"] is not None:
        base += f"提醒冷却参考第{state['last_notified_at']}次压缩。"
    if state["muted"]:
        context = "本任务主动提醒已关闭；显式交接或恢复提醒请求仍执行。"
    elif state["defer_until"]:
        context = f"等待用户指定节点{state['defer_until']}；节点到达才评估，普通次数不催促。"
    elif open_proposal:
        context = ("存在尚未最终展示的建议。先核对最新回应：仍适用则按原标识重新 prepare，"
                   "放在最终正文最前；不适用则 evaluate 或 cancel。进度消息不算展示。")
    else:
        context = ("本次压缩检查点待评估：最终答复前执行 prepare 或 evaluate。"
                   "prepare 成功才提醒；不提醒须记录原因及下次检查时机。"
                   "首次到三次需安全位置和后续；再次提醒还需新阶段与具体收益。")
    context += (f"执行前读取{SKILL}及references/compaction-reminder.md。"
                "新且已核实的混淆先纠正，可提前提醒；同一问题去重，静默优先。"
                "纯问答、无后续或讨论/修改本Skill不提醒。注入不算送达，普通继续不算交接批准。")
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": base + context}}


def process(event, root, action="hook", response=None, reason=None, **opts):
    session = event.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 256:
        raise ValueError("missing or invalid session_id")
    name = event.get("hook_event_name")
    if action == "hook":
        if name not in {"PostCompact", "SessionStart", "UserPromptSubmit", "Stop"}:
            return {}
        if is_subagent(event):
            return {}
        if name == "PostCompact" and event.get("trigger") != "auto":
            return {}
    path = state_path(root, session)
    if action == "status":
        return read_state(path, session)  # No mkdir, lock file, migration or write.
    if action == "hook" and name != "PostCompact" and not path.exists():
        return {}
    with locked(path.with_suffix(".lock")):
        state = read_state(path, session)
        before = json.dumps(state, sort_keys=True, ensure_ascii=False)
        result = None
        if action == "hook" and name != "Stop" and event.get("turn_id"):
            state["active_turn_id"] = key_arg(event["turn_id"], "turn_id")
        if action == "hook" and name == "PostCompact":
            # One callback per successful automatic compaction. Do not deduplicate
            # by turn_id: a single turn can legitimately compact several times.
            state["auto_count"] += 1
            sweep(root, path)
        elif action == "evaluate":
            result = evaluate(state, event, opts)
        elif action == "prepare":
            result = prepare(state, event, reason, opts)
        elif action == "cancel":
            turn = key_arg(event.get("turn_id"), "turn_id")
            if state.get("active_turn_id") and turn != state["active_turn_id"]:
                raise ValueError("cancel must use current host turn")
            proposal = state["proposal"]
            if not proposal or opts.get("proposal_id") != proposal["id"]:
                raise ValueError("cancel requires the current proposal ID")
            proposal["closed"] = True
            state["response"] = "cancelled"
            state["evaluation"] = dict(turn_id=event.get("turn_id"), auto_count=state["auto_count"],
                decision="skip", note="当前建议已撤销", next_check="next_compaction")
        elif action == "respond":
            respond(state, event, response, opts)
        elif action == "hook" and name == "Stop":
            result = check_stop(state, event)
        elif action != "hook":
            raise ValueError("invalid action")
        if json.dumps(state, sort_keys=True, ensure_ascii=False) != before:
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            os.replace(temp, path)
        if action != "hook":
            return result if result is not None else state
        if name == "Stop":
            return result
        if name == "PostCompact":
            return {}
        return context_output(state, name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--action", choices=["hook", "status", "evaluate", "prepare", "respond", "cancel"],
                        default="hook")
    parser.add_argument("--session-id")
    parser.add_argument("--turn-id")
    parser.add_argument("--response", choices=["continue", "defer", "mute", "handoff", "resume"])
    parser.add_argument("--reason", choices=["count", "confusion", "stage", "checkpoint"])
    parser.add_argument("--stage-key")
    parser.add_argument("--cause-key")
    parser.add_argument("--checkpoint-key")
    parser.add_argument("--proposal-id")
    parser.add_argument("--notice")
    parser.add_argument("--outcome", choices=["defer", "skip"])
    parser.add_argument("--note")
    parser.add_argument("--next-check", choices=["next_turn", "next_compaction"])
    parser.add_argument("--safe", action="store_true")
    parser.add_argument("--has-next", action="store_true")
    parser.add_argument("--benefit", action="store_true")
    args = parser.parse_args()
    try:
        event = (json.load(sys.stdin) if args.action == "hook"
                 else {"session_id": args.session_id, "turn_id": args.turn_id})
        options = {k: v for k, v in vars(args).items()
                   if k not in {"session_id", "turn_id", "state_dir", "action", "response", "reason"}}
        result = process(event, args.state_dir, args.action, args.response, args.reason, **options)
        if result:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Preserve corrupt state and continue work; make tracking failure visible.
        if args.action == "hook":
            print(json.dumps({"systemMessage":
                f"project-handoff 计数不可用（{type(error).__name__}）；未重置计数，任务继续。"},
                ensure_ascii=False))
            return 0
        print(f"project-handoff: {type(error).__name__}; state preserved", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    raise SystemExit(main())
