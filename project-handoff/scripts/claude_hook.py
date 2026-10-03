"""Claude Code lifecycle adapter for the shared project-handoff core.

Only maps Claude hook fields onto the Codex-shaped event the core expects:
prompt_id becomes turn_id, and injected context names the session_id so the
agent can call the core CLI. Counting, evaluation and Stop checks stay in
compaction_reminder.py, which is kept byte-identical to the Codex copy.
No network, model calls, or business-file writes.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compaction_reminder as core  # noqa: E402


def adapt(event):
    if not event.get("turn_id") and event.get("prompt_id"):
        event["turn_id"] = event["prompt_id"]
    return event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = adapt(json.load(sys.stdin))
        result = core.process(event, args.state_dir)
        extra = (result or {}).get("hookSpecificOutput")
        if extra and extra.get("additionalContext"):
            extra["additionalContext"] = (f"当前session_id={event['session_id']}；"
                                          + extra["additionalContext"])
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Same contract as the core hook path: keep state, never block the task.
        print(json.dumps({"systemMessage":
            f"project-handoff 计数不可用（{type(error).__name__}）；未重置计数，任务继续。"},
            ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    raise SystemExit(main())
