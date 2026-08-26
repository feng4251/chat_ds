# Claude Turn 容器网络许可制度

## 目标与强制边界

每个 Claude Turn 容器必须以 `network_mode=none` 启动。容器内模型、Bash、
Python、Node、浏览器和子代理都不得拥有直连网卡、宿主网络或 Docker socket。
唯一的出站路径是容器内 `127.0.0.1` 上的短生命周期桥接器；桥接器只能连接受信的
Skill Egress Proxy Unix socket。提示词、Skill 脚本和模型输出都不能扩大该边界。

不存在“临时全网放开”、原始 TCP 或通配写权限。部署可以显式启用固定的
`public-read` profile：任意公网域名仅允许 HTTP GET/HEAD 的标准 80/443 端口；每个
请求仍由代理解密检查、DNS 分类、固定 IP、净化请求头、计量并审计。该 profile 是
部署配置，不来自提示词、Skill 或模型输出。

## 每 Turn 许可的唯一来源

许可由 Supervisor 在启动 Turn 前编译并冻结，只允许以下四类来源：

1. 部署所有者配置的模型 Provider profile。只授予该 profile 的精确
   `POST /v1/messages`；调用方提交的 profile、模型、Backend 协议和 Backend URL
   必须与部署配置完全一致。
2. 当前已授权且内容寻址、只读的 Session Skill 视图。只有 Skill 资源中明确声明的
   HTTP(S) URL 前缀和方法可以成为许可；不会根据工具名、模型猜测或相似域名推导。
3. 当前用户本 Turn 文本中明确出现的 URL。此类许可严格限制为该 URL 的
   `GET/HEAD`，路径和查询串都必须完全一致，不能追加查询数据，也不能产生写入
   方法、同源全站或其他路径权限。
4. 部署所有者启用的固定公网只读 profile。它只表达
   `methods=[GET,HEAD], ports=[80,443]`，不产生 wildcard URL rule，不允许私网、
   请求 body、自定义/认证/Cookie header、WebSocket、QUIC、嵌套 CONNECT 或非标准
   端口。重定向会作为新的代理连接重新经过同一检查。

所有规则均规范化为“scheme + host + port + path + query match + methods”。Skill/MCP
协议可以声明有界前缀规则；用户 URL 与 Provider endpoint 强制使用 exact-query。
精确规则、public-read profile、预算作用域和调用 ID 被一并摘要绑定并签名发送给代理。

## 私网、DNS 与凭据

- public-read 下回环、私网、链路本地、组播、未指定、保留、IPv4-in-IPv6、
  transition/NAT64 与云元数据地址始终禁止；混合公网/私网 DNS 响应整体拒绝。
- 私网目标必须同时满足：部署级 `BROWSER_PRIVATE_ORIGIN_ALLOWLIST`、当前 Skill
  的精确规则，以及当前用户本 Turn 明确给出的 URL。三者缺一不可。
- 域名在受信代理侧重新解析并固定，不能用 DNS rebinding 绕过私网判断。
- public-read 会把请求头重建为固定 Host/User-Agent/Accept/Accept-Encoding/
  Connection 集合；Authorization、Cookie 和任意调用方 header 都不会上送。精确
  Provider/MCP/Skill 规则优先匹配并保留协议所需 header。
- Provider 与 MCP 凭据只能来自受限部署配置；不得写入 Skill、workspace、调试事件
  或 Git。终端审计只保留规则摘要、计数和预算，不记录密钥。

## 数据方向与预算

网络安全不能只靠“禁止上传”的方向判断：即使 GET/HEAD 没有 body，hostname、path、
query 与 DNS 仍可能携带少量数据。因此 public-read 是“大幅压缩出站通道”的实用边界，
不是数学意义的零外传；严格零外传仍只能使用固定参数 schema 的 typed broker。
在此基础上继续叠加：

- 每 Turn 最大连接/请求数；
- client-to-proxy 总字节上限；
- proxy-to-client 总字节上限；
- 仅精确 Provider POST 规则可签名响应空闲预算，使原生引擎的 stream watchdog
  保持权威；Skill、MCP 与公共读取仍使用代理的短空闲上限；
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
`signed-route-idle-v2` 策略运行时版本。镜像构建会实际导入规则解析器验证该版本，
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
而不采用会触发 `errno 524` 的 NNP 组合。它仍是唯一有外网接口的执行组件；每条连接
仍必须携带短时 HMAC policy，代理自身不拥有无条件通配转发权限。
