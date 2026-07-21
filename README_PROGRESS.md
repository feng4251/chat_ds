# 项目进度与续接入口

最后更新：2026-07-21

当前唯一权威交接文档是 [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md)。Codex、Claude Code 或人工维护者重新打开本目录后，应先完整阅读该文件。

当前摘要：

- 通用 Skill Harness 修复已提交到本地分支 `fix/generic-skill-harness-20260717`，实现提交为 `4dd69e0a`。
- 最新 Harness 完整回归为 1190 项通过、5 项跳过；Backend 24 项、Browser Sidecar 6 项通过。
- 最新代码已部署到 `10.10.130.178` / `172.30.100.145`，Browser、Harness、Backend、Executor 正常。
- 流式 productive hard cap 已由 1500 秒提高到 2400 秒；无进展 lease 仍独立生效。
- Harness 搜索使用 `10.10.132.126:8088`；旧生产机 8080 SearXNG 已停止但未删除。
- 下一步等待用户手工运行新的 V2.3 Skill E2E，再依据 session debug 做证据驱动诊断。
- 不要批量 stage 当前工作树；不要打印或持久化凭据。

历史文件仅供追溯：`_SESSION_STATUS.md`、`_HARNESS_CHANGES.md`、`_REMOTE_OPS.md`、`harness/HARNESS_V23_CONTEXT_2026-07-16.md`。
