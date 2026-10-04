# 🧰 Table Skills

[抖音 · 一只桌子](https://v.douyin.com/7jbgafVeA4U/) · [YouTube · 一只桌子](https://www.youtube.com/@%E4%B8%80%E5%8F%AA%E6%A1%8C%E5%AD%90) · [小红书 · 一只桌桌桌子](https://xhslink.cn/o/2iZQ3Yc2j4E) · [bilibili · 一只桌子_table](https://b23.tv/7Y34qaP) · [X · 一只桌子](https://x.com/D_uoduo)

整理日常使用中的 AI 技能与实验思路，按实际效果持续调整。当前维护「项目交接」；「网页替身」旧版实现已弃用，保留设计思路供后续探索。

当前主要面向 **Windows 上的 Codex 桌面端**，项目交接另提供 Claude Code 适配。安装 Skill 不会自动提供宿主缺失的工具、接口或 Hook 权限。

https://github.com/user-attachments/assets/94c0a4d6-0bee-405e-9fe9-dc398cb7faa0

## 📋 目录

| 技能 | 一句话 | 使用入口 |
|---|---|---|
| 🔄 [project-handoff（项目交接）](project-handoff/) | 保存有效进度和决定，让下一次对话能接着做 | [SKILL.md](project-handoff/SKILL.md) |
| 🌐 [web-stand-in（网页替身）](web-stand-in/) | Astra上线后token利用率提高，实测省token效果堪忧，优化中。 | [思路.md](web-stand-in/思路.md) |

## 📦 安装方式

把技能链接发给支持安装 Skills 的 Agent，例如：

```text
帮我安装项目交接技能：https://github.com/duoduoler-ops/Table-skills/tree/main/project-handoff
```

让 Agent 先检查当前宿主是否具备所需工具，再安装到对应的 Skills 目录。保留整个技能文件夹；只复制 `SKILL.md` 不能替代配套脚本和说明。

网页替身目前只保留思路文档，不提供旧版 Skill 安装入口。

## 🔄 project-handoff（项目交接）

长任务换对话时，最容易丢失的是定稿位置、已经否决的方案、真实进度和下一步。项目交接只整理影响接续的信息，让接手对话先核对，再继续。

**主要功能**

- 保存目标、有效决定、关键文件、真实进度和下一步，避免整段复制聊天记录。
- 只要求保存时，交付材料和可复制的开场白。
- 明确批准新建并继续后，在宿主能力允许时完成保存、新建、核对和接续。
- 在压缩检查点、实质阶段切换或已核实的上下文混淆时评估是否交接；暂不提醒时记录原因和下次检查时机。
- 需要提醒时，把建议放在最终答复正文最前；可选 Hook 核验最终文本后才计为正式提醒，并检查漏评估或漏展示。

**怎么用**

```text
用项目交接保存当前进度，先不要新建任务。

整理交接材料，在同一项目目录新建任务并继续下一步。

读取这个项目的 PROJECT_STATE.md，核对后继续未完成工作。

本任务不再主动提醒交接。
```

**提醒与授权**：首次到第三次自动压缩后，在安全且有明确后续的位置提醒。之后还需满足新阶段、具体切换收益和冷却条件，不是每三次压缩机械提醒。普通“继续”不等于同意新建任务；提醒不会自动执行交接。

**可选 Hook**：Codex 版仅在 Windows 上验证，模板中 `commandWindows` 供 Windows 使用（Codex 通过 PowerShell 执行，需以 `&` 调用），`command` 供其他系统使用。Claude Code 用同一份计数脚本加适配器 `scripts/claude_hook.py`，模板见 `hooks/claude-settings.example.json`；计数和状态注入已在 Claude Code 上观察到；完整提醒链已用模拟事件走通，尚未在真实 Claude Code 会话里到达第 3 次自动压缩。安装 Skill 不等于启用 Hook；配置前需替换示例路径、合并已有配置，并完成宿主原生信任。见 [计数器说明](project-handoff/references/compaction-reminder.md)、[Codex Hook 模板](project-handoff/hooks/codex-hooks.example.json) 和 [Claude Code 模板](project-handoff/hooks/claude-settings.example.json)。

Hook 能检查压缩节点和记录是否缺失，不能独立判断所有业务阶段，也不能证明用户已经看到提醒。没有真实压缩事件时不猜次数；没有接续工具时提供手动入口。

## 🌐 web-stand-in（网页替身）

![Codex 快速聊天入口](docs/images/web-stand-in-quick-chat.png)

![快速聊天添加到 Codex](docs/images/web-stand-in-add-to-codex.png)

Astra 上线后 token 利用率提高，但网页外包的实测省 token 效果仍不理想，目前继续优化。

**优先思路**：把快速聊天作为半自动入口。结果回收已有接口，但自动创建全新快速聊天仍缺少正式接口。

**其他思路**：结果外包、MCP 任务服务、模型后端桥接和原生子任务。详见 [思路与设计框架](web-stand-in/思路.md)。

## 🛠️ 反馈

报告问题时，请说明宿主、系统、出问题的步骤和脱敏后的报错。不要公开账号信息、密钥或私人聊天全文。

功能跑通、自动触发、上下文隔离和实际省 token 是不同的验证项；未覆盖的环境与组合需要单独测试。

## 📄 许可证

本仓库采用 [MIT License](LICENSE)，允许复制、修改、商用和再发布，须保留版权和许可证声明。

## 📬 联系我

欢迎交流实际使用中的问题与改进建议。

| 渠道 | 联系方式 |
|---|---|
| 邮箱 | [duoduoler@gmail.com](mailto:duoduoler@gmail.com) |
| X | [一只桌子 · @D_uoduo](https://x.com/D_uoduo) |

Made by [@duoduoler-ops](https://github.com/duoduoler-ops)
