# 计数、评估与最终核验

仅在宿主注入 project-handoff 状态、需要登记评估／建议／回应，或配置、排查计数器时读取。什么时候该提醒以 [SKILL.md](../SKILL.md) 为准，本页只讲登记命令、核验方式和部署。

## 计数与检查点

- `PostCompact(auto)` 每次加一。手动压缩、恢复会话、注入次数、聊天轮数都不计数；同一回合可以发生多次自动压缩。带子 Agent 标识的事件跳过。计数只覆盖 Hook 启用后观察到的事件，旧任务不回填。
- 压缩数到达提醒点、且没有用户静默或指定节点时，需要一次评估。`next_compaction` 让同一压缩点不在每轮重查；`next_turn` 下一回合重评。新的真实压缩会让旧评估过期。业务阶段和用户指定节点由 Agent 识别，Hook 不扫描历史、不猜阶段。
- `SessionStart` 和 `UserPromptSubmit` 注入状态，并登记宿主真实的 turn_id。本轮无需登记时只注入一行压缩数和冷却点；到期待评估、有未展示的建议、用户静默或指定节点时才注入完整规则。`Stop` 检查本轮待展示的建议和到期未评估的压缩点，缺一样就请求补漏一次，两类缺失共用这一次；本轮已补过或宿主标记 `stop_hook_active` 时不再发起。旧回合的最终答复或评估不能冒充本轮的。

## 登记命令

session_id、turn_id 只用宿主给的，也就是注入文本里的“当前session_id”“当前turn_id”（Codex 的 turn_id、Claude Code 的 prompt_id）。没有标识就不能登记，更不能猜。

Windows 用 PowerShell 7（`py` 是 Python 启动器；没有它就写 `& '<PYTHON_EXE>'` 绝对路径）：

```powershell
py -3 -X utf8 '<SKILL_DIRECTORY>/scripts/compaction_reminder.py' --state-dir '<STATE_DIR>' --session-id '<当前session_id>' --action status
```

macOS／Linux：

```bash
python3 -X utf8 '<SKILL_DIRECTORY>/scripts/compaction_reminder.py' --state-dir '<STATE_DIR>' --session-id '<当前session_id>' --action status
```

`<SKILL_DIRECTORY>` 是本 Skill 的实际安装目录，宿主加载 Skill 或注入文本时会给出。Windows 上不要把 `python` 当成可用解释器，它可能只是商店占位程序。`<STATE_DIR>`：Codex 为 `<CODEX_HOME>/state/project-handoff`（CODEX_HOME 未设置时是用户目录下 `.codex`），Claude Code 为 `<CLAUDE_HOME>/state/project-handoff`（通常是用户目录下 `.claude`）。`status` 只读，不建锁、不写回。其他动作把 `--action status` 换成下表参数。

常用动作：

| 场景 | 参数 |
| --- | --- |
| 暂缓 | `--action evaluate --turn-id '<当前turn_id>' --outcome defer --note '<具体缺项>' --next-check next_turn`；同阶段没有新收益时改 `next_compaction` |
| 不适用 | `--action evaluate --turn-id '<当前turn_id>' --outcome skip --note '<纯问答、无后续或讨论本 Skill>' --next-check next_compaction` |
| 首次次数提醒 | `--action prepare --turn-id '<当前turn_id>' --reason count --stage-key '<稳定的业务阶段>' --safe --has-next --notice '交接建议：<原因、第一步、确认后的动作>'` |
| 阶段提醒 | 同上，`--reason stage`，加 `--benefit` |
| 用户确认完整交接 | `--action respond --turn-id '<回应turn_id>' --proposal-id '<建议id>' --response handoff`。只停止提醒，业务交接仍按 handoff.md 的授权和核验执行 |
| 用户说先不交接 | 同上，`--response continue` |

少用动作：

| 场景 | 参数 |
| --- | --- |
| 已核实的新混淆 | 按首次提醒的参数 `prepare`，改为 `--reason confusion --benefit --cause-key '<已纠正问题的标识>'` |
| 用户指定节点 | `--action respond --turn-id '<回应turn_id>' --proposal-id '<建议id>' --response defer --checkpoint-key '<节点>'`；节点到达时按首次提醒的参数 `prepare`，改为 `--reason checkpoint --checkpoint-key '<节点>'`，到达与否由 Agent 核实 |
| 静默／恢复 | `--action respond --turn-id '<回应turn_id>' --response mute` 或 `resume`，不需要 proposal-id，仅用户明确要求时使用 |
| 建议失效 | `--action cancel --turn-id '<当前turn_id>' --proposal-id '<建议id>'`，记为本次跳过，不冒充用户拒绝 |

约束：`note` 不超过 160 字，不存用户原文或敏感信息；`notice` 是 20 到 600 字的一段话，以“交接建议：”开头，不能换行；`--safe`、`--has-next`、`--benefit` 必须有事实依据。`prepare` 返回 `prepared: true` 才能展示，被拒绝时核实返回的原因，再登记暂缓或不适用。登记暂缓或不适用会关闭尚未最终展示的旧建议，避免恢复后误补。

## 最终核验

展示没有人工回执入口，进度消息里出现过建议也不登记。`Stop` 从宿主给的最终文本里取正文第一段（跳过引用和代码块），与当前建议原文比对，一致才计一次正式提醒并开始冷却；文本核验不等于用户已经看到。同一建议的重复结束事件不重复计数。没有可用 Stop 的宿主只做人工自检，并如实说明最终核验未知，不补写“已核验”。

## 状态与成本

每个 session_id 对应 state-dir 里一个 SHA-256 命名的文件，只存压缩计数、最近评估、当前建议、用户回应、去重键和本轮补漏状态，不存聊天正文。版本 3 把“提醒次数”限定为最终文本核验次数；新会话直接按版本 3 建档。从旧版迁移时才保留原计数快照，无法从单条记录确定的历史总数标为未知，真实压缩数不回滚。单次 `status` 的兼容转换只在内存中进行。

每次自动压缩时，脚本顺手删除同目录里 60 天未更新的其他会话状态：只认本脚本生成的 64 位十六进制文件名，按 `.json` 的修改时间整组删除对应的锁和临时文件，当前会话不删；删除失败不影响计数。

脚本不联网、不调模型、不新建业务任务。正常检查不增加模型轮次；只有漏评估或漏展示时，Stop 会让模型多回复一次。Hook 没运行、执行失败或业务判断有误时，程序不能保证提醒发生，所以不能宣称零额外用量或零漏报。

## 部署与回退

| 宿主 | 事件入口 | 模板 |
| --- | --- | --- |
| Codex | 用户级 `<CODEX_HOME>/hooks.json` 中 SessionStart、UserPromptSubmit、PostCompact(auto)、Stop 四条，直接调用 `scripts/compaction_reminder.py`。Windows 走 `commandWindows`（Codex 用 PowerShell 执行，带引号的程序路径前要有 `&`），其他系统走 `command` | [codex-hooks.example.json](../hooks/codex-hooks.example.json) |
| Claude Code | 用户级 `settings.json` 中同样四条，exec 形式调用 `scripts/claude_hook.py`。适配器只把 `prompt_id` 映射为 turn_id、在注入文本前补 session_id，其余逻辑复用同一份 `compaction_reminder.py` | [claude-settings.example.json](../hooks/claude-settings.example.json) |

- 先把模板里的 `<PYTHON_EXE>`、`<SKILL_DIRECTORY>`、`<CODEX_HOME>` 或 `<CLAUDE_HOME>` 换成本机已核实的绝对路径，再合并进已有配置并保留其他 Hook。Codex 需要在原生 `/hooks` 中审阅并信任；不能写信任库、加托管策略或用绕过参数伪装启用。安装 Skill 不等于启用 Hook。
- 回退时删除对应四条即可，状态目录保留。换回不识别版本 3 的旧脚本前，先备份状态并核对字段兼容，不能只改版本号，也不能用旧快照覆盖新计数。
- 文件存在、模拟事件通过、真实宿主事件、最终文本核验、用户界面可见，是五个不同的证据层级，报告时分开说。
- 官方接口：[Codex Hooks](https://learn.chatgpt.com/docs/hooks) 的 `Stop` 提供 `turn_id`、`last_assistant_message`、`stop_hook_active`；[Claude Code Hooks](https://code.claude.com/docs/en/hooks) 的 `PostCompact` 提供 `trigger`，`Stop` 提供 `prompt_id` 和 `last_assistant_message`，`SessionStart`／`UserPromptSubmit` 支持 `additionalContext`。Claude Code 文档未列出 `stop_hook_active`，脚本自身保证每回合只补漏一次。两边的 `decision: block` 都只是请求继续一次，不是永久阻断。
