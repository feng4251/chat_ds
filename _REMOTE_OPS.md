---
name: remote-ops-quick-ref
description: 远端 10.10.130.178 操作速查表
metadata:
  node_type: memory
  type: reference
  originSessionId: b9335375-0822-4c4e-b007-603a72b57b97
---

> **历史文档（2026-07-08）**：当前操作状态与安全要求请先读 `SESSION_HANDOFF.md`。下列命令仅作历史参考。

## SSH 连接

```bash
set -a
source .local_secrets/remote_10.10.130.178.env
set +a
export SSHPASS="$CHATDS_REMOTE_PASSWORD"
SSH_CMD="sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/chat_ds_known_hosts -o ConnectTimeout=10 $CHATDS_REMOTE_USER@$CHATDS_REMOTE_HOST"
$SSH_CMD
```

## 容器管理

```bash
# 查看容器状态
$SSH_CMD docker compose ps

# 查看 harness 日志（最近 50 行）
$SSH_CMD docker logs -n 50 chat_acits_harness

# 查看 backend 日志
$SSH_CMD docker logs -n 50 chat_acits_backend

# 重启服务
$SSH_CMD docker compose restart harness
$SSH_CMD docker compose restart backend
```

## 数据库查询

```bash
# Session 对话
$SSH_CMD docker exec chat_acits_backend python3 << 'EOF'
import sqlite3
db = sqlite3.connect('/app/db/chat_ds.db')
db.row_factory = sqlite3.Row
cur = db.cursor()
cur.execute("SELECT conversation_id, user_id, enabled_user_skills, enabled_tools FROM conversations WHERE conversation_id='4588be7c...' LIMIT 1")
row = cur.fetchone()
if row:
    print(f"user_id: {row['user_id']}")
    print(f"enabled_user_skills: {row['enabled_user_skills']}")
    print(f"enabled_tools: {row['enabled_tools']}")
EOF
```

```bash
# Agent runs 和 tool_events
$SSH_CMD docker exec chat_acits_backend python3 << 'EOF'
import sqlite3
import json
db = sqlite3.connect('/app/db/chat_ds.db')
db.row_factory = sqlite3.Row
cur = db.cursor()
cur.execute("SELECT status, finish_reason, error, tool_events FROM agent_runs WHERE conversation_id='4588be7c...' ORDER BY created_at DESC LIMIT 5")
for row in cur.fetchall():
    print(f"Status: {row['status']}, Finish: {row['finish_reason']}")
    if row['tool_events']:
        events = json.loads(row['tool_events'])
        tools_used = {}
        for evt in events:
            name = evt.get('tool_name', 'unknown')
            tools_used[name] = tools_used.get(name, 0) + 1
        print(f"  Tools: {tools_used}")
        # 检查 mcp_ 和 web_search
        has_mcp = any(name.startswith('mcp_') for name in tools_used.keys())
        has_web = 'web_search' in tools_used
        print(f"  MCP: {has_mcp}, web_search: {has_web}")
EOF
```

## 文件操作

```bash
# 查看 session workspace
$SSH_CMD ls -lh /app/data/workspaces/*/4588be7c.../ 2>/dev/null | head -20

# 查看生成的 MD 文件
$SSH_CMD find /app/data/workspaces -name "*.md" -path "*4588be7c*" -exec ls -lh {} \;

# 下载 MD 文件到本地
sshpass -e scp -o StrictHostKeyChecking=no -r root@10.10.130.178:/app/data/workspaces/*/4588be7c.../*.md /tmp/

# 查看监控日志
$SSH_CMD tail -30 /tmp/chat_ds_session_60a128_monitor.log
```

## Executor Socket 测试

```bash
# Smoke test
$SSH_CMD docker exec chat_acits_executor python3 << 'EOF'
import numpy as np
import scipy.sparse
import pandas as pd
import matplotlib
import os

print(f"OpenBLAS threads: {os.environ.get('OPENBLAS_NUM_THREADS')}")
print(f"BLAS num threads: {os.environ.get('BLAS_NUM_THREADS')}")

m = np.random.randn(100, 100)
result = m @ m
print(f"Matrix mult OK: {result.shape}")

import resource
soft, hard = resource.getrlimit(resource.RLIMIT_AS)
print(f"RLIMIT_AS: soft={soft}, hard={hard}")
EOF
```

## 代码部署（热修补）

```bash
# 打包本地修改的 Python 文件
cd /nfs/yangbb/codes/chat_ds
tar czf /tmp/harness_fix.tar.gz harness/

# 复制到远端
sshpass -e scp -o StrictHostKeyChecking=no /tmp/harness_fix.tar.gz root@10.10.130.178:/tmp/

# 在容器中解压并更新
$SSH_CMD bash << 'EOF'
docker cp /tmp/harness_fix.tar.gz chat_acits_harness:/tmp/
docker exec chat_acits_harness bash << 'INNER'
cd /app && tar xzf /tmp/harness_fix.tar.gz
python3 -m py_compile harness/*.py harness/tools/*.py harness/skills/*.py
echo "Compilation OK"
INNER
docker restart chat_acits_harness
sleep 3
docker logs chat_acits_harness | head -20
EOF
```

## 监控日志追加

```bash
# 在下次运行时执行（作为 standing task）
$SSH_CMD bash << 'EOF'
echo "=== $(date) ===" >> /tmp/chat_ds_session_60a128_monitor.log

# 1) Container status
docker compose ps >> /tmp/chat_ds_session_60a128_monitor.log 2>&1

# 2) Tool events grep
docker exec chat_acits_backend python3 << 'PYEOF' >> /tmp/chat_ds_session_60a128_monitor.log 2>&1
import sqlite3
import json
db = sqlite3.connect('/app/db/chat_ds.db')
cur = db.cursor()
cur.execute("SELECT tool_events FROM agent_runs WHERE conversation_id='60a128d8516949b487d7aa0411ccff43' ORDER BY created_at DESC LIMIT 1")
row = cur.fetchone()
if row and row[0]:
    events = json.loads(row[0])
    for evt in events:
        name = evt.get('tool_name', '')
        if any(x in name for x in ['execute_code', 'OpenBLAS', 'SyntaxError', 'timeout', 'RemoteProtocol', 'ToolContext', 'ModuleNotFound']):
            print(f"Found: {name} - {evt.get('status')}")
PYEOF

# 3) Executor socket test
docker exec chat_acits_executor python3 << 'PYEOF' >> /tmp/chat_ds_session_60a128_monitor.log 2>&1
import numpy as np
import os
m = np.random.randn(50, 50)
result = m @ m
threads = os.environ.get('OPENBLAS_NUM_THREADS', 'not set')
print(f"Executor OK: {threads} threads, shape={result.shape}")
PYEOF

echo "" >> /tmp/chat_ds_session_60a128_monitor.log
EOF
```

## 常见问题排查

| 症状 | 检查命令 |
|------|--------|
| Harness 启动失败 | `docker logs chat_acits_harness \| tail -30` |
| DB 连接失败 | `docker exec chat_acits_backend sqlite3 /app/db/chat_ds.db ".tables"` |
| Executor 不可用 | `docker ps \| grep executor` & `docker logs chat_acits_executor` |
| Tool 调用失败 | DB 查询 tool_events 看错误详情 |
