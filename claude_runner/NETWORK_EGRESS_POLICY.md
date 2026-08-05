# Claude Turn 容器网络许可制度

## 目标与强制边界

每个 Claude Turn 容器必须以 `network_mode=none` 启动。容器内模型、Bash、
Python、Node、浏览器和子代理都不得拥有直连网卡、宿主网络或 Docker socket。
唯一的出站路径是容器内 `127.0.0.1` 上的短生命周期桥接器；桥接器只能连接受信的
Skill Egress Proxy Unix socket。提示词、Skill 脚本和模型输出都不能扩大该边界。

不存在“临时全网放开”或通配域名兜底。需要新增网络能力时，必须形成下面定义的精确
许可并通过回归测试后才能部署。

## 每 Turn 许可的唯一来源

许可由 Supervisor 在启动 Turn 前编译并冻结，只允许以下三类来源：

1. 部署所有者配置的模型 Provider profile。只授予该 profile 的精确
   `POST /v1/messages`；调用方提交的 profile、模型、Backend 协议和 Backend URL
   必须与部署配置完全一致。
2. 当前已授权且内容寻址、只读的 Session Skill 视图。只有 Skill 资源中明确声明的
   HTTP(S) URL 前缀和方法可以成为许可；不会根据工具名、模型猜测或相似域名推导。
3. 当前用户本 Turn 文本中明确出现的 URL。此类许可严格限制为该 URL 的
   `GET/HEAD`，路径和查询串都必须完全一致，不能追加查询数据，也不能产生写入
   方法、同源全站或其他路径权限。

所有规则均规范化为“scheme + host + port + path + query match + methods”。Skill/MCP
协议可以声明有界前缀规则；用户 URL 与 Provider endpoint 强制使用 exact-query。
规则集合、
预算作用域和调用 ID 被摘要绑定并签名发送给代理。重定向后的新目标也必须命中规则，
否则拒绝。

## 私网、DNS 与凭据

- 回环、链路本地、组播、未指定地址以及云元数据地址始终禁止。
- 私网目标必须同时满足：部署级 `BROWSER_PRIVATE_ORIGIN_ALLOWLIST`、当前 Skill
  的精确规则，以及当前用户本 Turn 明确给出的 URL。三者缺一不可。
- 域名在受信代理侧重新解析并固定，不能用 DNS rebinding 绕过私网判断。
- Provider 与 MCP 凭据只能来自受限部署配置；不得写入 Skill、workspace、调试事件
  或 Git。终端审计只保留规则摘要、计数和预算，不记录密钥。

## 数据方向与预算

网络安全不能只靠“禁止上传”的方向判断：HTTP 请求、URL 查询、DNS 名称以及模型
Provider 调用本身都可能携带数据。因此采用目的地与方法白名单作为主边界，并叠加：

- 每 Turn 最大连接/请求数；
- client-to-proxy 总字节上限；
- proxy-to-client 总字节上限；
- 超限立即拒绝，关闭时等待所有处理器排空；
- 终端事件携带不可变、内容摘要化的 egress receipt。

这些限制同时作用于 Bash、代码运行、浏览器、MCP 和 Claude 自身，不允许工具选择
不同运行环境来绕过策略。

## 变更与发布门禁

任何新增 Provider、私网 origin、HTTP 方法或 URL 编译规则都必须：

1. 在受限 `.local_secrets`/部署环境中配置凭据，源码和日志中不得出现凭据；
2. 增加允许案例、相邻路径拒绝、错误方法拒绝、私网三方交集、篡改 Skill 视图拒绝、
   预算超限和重定向逃逸测试；
3. 运行 Runner、Backend、代理和前端基础回归；
4. 构建固定版本镜像并确认 Turn 容器仍为 `network_mode=none`；
5. 仅在审计 receipt 可生成且终端状态可持久化后部署。

Supervisor、Turn Runner、Session Sandbox 与 Egress Proxy 必须共同声明
`signed-exact-query-v1` 策略运行时版本。镜像构建会实际导入规则解析器验证该版本，
Supervisor 启动时再次校验 Runner 镜像标签；不能用一个新版策略编译器搭配旧版执行镜像。

紧急撤销通过删除部署 profile/private-origin 配置或禁用
`CLAUDE_CODE_ENGINE_ENABLED` 完成；不得通过修改模型提示词代替基础设施撤销。

当前生产宿主的 runc/kernel 组合无法同时加载 seccomp BPF 与
`no-new-privileges`（容器初始化返回 `errno 524`）。Turn 镜像因此采用显式的
`seccomp_stripped_setid` 模式：构建阶段移除全部 setuid/setgid 位和文件 capability，
Supervisor 在启动前校验镜像 attestation，运行时保留 cap-drop、精确 cap-add、只读根、
单 Session 挂载、紧凑 seccomp denylist 与 `network_mode=none`。这是可观察的部署兼容
模式，不会放宽网络许可；宿主修复后可切换到更严格的
`seccomp_no_new_privileges`。

Egress Proxy 本身以非 root、cap-drop、只读根和 Docker 默认 seccomp 运行；其镜像同样
在构建阶段移除全部 setuid/setgid 位与文件 capability。这样可在该宿主上保留 seccomp，
而不采用会触发 `errno 524` 的 NNP 组合。它仍是唯一有外网接口的组件，且不拥有通配
转发权限。
