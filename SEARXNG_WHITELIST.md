# SearXNG 搜索服务白名单

> 当前建议：SearXNG 部署在本机 `10.10.132.126`，`chat_ds` 远端 `10.10.130.178` 只访问本机 SearXNG HTTP 服务。仅当显式配置 Harness 的可选 `ddg` fallback 时，远端才需要 DuckDuckGo 公网出站。

## 当前本机部署地址

- 服务容器：`chat_acits_searxng`
- 本机监听：`10.10.132.126:8088 -> container:8080`
- Harness 内部 Docker 地址：`http://searxng:8080`
- 远端若调用本机 SearXNG：`http://10.10.132.126:8088`
- 同 Compose 搜索网络：`172.29.250.0/24`（只接入 Harness、SearXNG、Valkey，并由 limiter 放行）
- 镜像固定到官方 `2026.7.9-8456831a0` 的 digest，干净 checkout 不依赖未跟踪的上游源码树

2026-07-21 部署后，从 Harness 容器经 limiter 查询 `Galectin-3 阿尔茨海默病` 返回 HTTP 200、44 条聚合结果；当次有效来源包含 Baidu、360、Bing、Mojeek、Sogou，Yahoo 报告一次 HTTP protocol error。上游抓取引擎会因 CAPTCHA、协议变化或出口 IP 声誉动态降级，不能把任一免费引擎视为永久容量保证；本方案通过低并发、退避、熔断、缓存和多引擎聚合提高可用性，不尝试绕过对方访问控制。

## 推荐最小白名单

### 1. 远端 chat_ds 主机 `10.10.130.178` 出站

如果 SearXNG 放在本机，只需要允许：

| 源 | 目的 | 协议/端口 | 用途 |
|---|---|---:|---|
| `10.10.130.178` | `10.10.132.126` | TCP `8088` | 远端 harness 调用本机 SearXNG JSON API |

远端不需要直接访问 Bing / DuckDuckGo / Brave 等公网搜索站点。

### 2. 本机 SearXNG 主机 `10.10.132.126` 出站

当前 SearXNG 配置以 Baidu 为首选，并启用 360、Sogou、Yahoo、Mojeek 和低权重 Bing：

| 源 | 目的域名 | 协议/端口 | 用途 | 当前本机探测 |
|---|---|---:|---|---|
| `10.10.132.126` | `www.baidu.com`、`www.so.com`、`www.sogou.com` | HTTPS `443` | 中文通用搜索池 | 需部署后持续健康检查 |
| `10.10.132.126` | `search.yahoo.com`、`www.mojeek.com` | HTTPS `443` | 通用搜索补充池 | 需部署后持续健康检查 |
| `10.10.132.126` | `www.bing.com` | HTTPS `443` | 低权重补充；污染结果由 Harness 质量门过滤 | 可达 |
| `10.10.132.126` | `cn.bing.com` | HTTPS `443` | 国内 Bing 入口，会跳转/回落到 `www.bing.com` | 可达 |

### 3. SearXNG 可选规则源

SearXNG 启动时会尝试拉 ClearURLs 规则；不是搜索必需，但放开可减少日志 warning：

| 源 | 目的域名 | 协议/端口 | 用途 | 当前本机探测 |
|---|---|---:|---|---|
| `10.10.132.126` | `rules1.clearurls.xyz` | HTTPS `443` | ClearURLs 规则 | 可达 |
| `10.10.132.126` | `rules2.clearurls.xyz` | HTTPS `443` | ClearURLs 规则备用 | 未单测 |
| `10.10.132.126` | `raw.githubusercontent.com` | HTTPS `443` | ClearURLs 规则备用 | 可达 |

## 备用搜索引擎白名单

这些不是当前最小运行必需；只有以后在 `searxng/settings.yml` 启用对应 engine 时才需要。

| 搜索引擎 | 目的域名 | 协议/端口 | 本机探测结论 |
|---|---|---:|---|
| DuckDuckGo | `duckduckgo.com` | HTTPS `443` | 可达，但 SearXNG DDG engine 当前触发 CAPTCHA |
| DuckDuckGo HTML | `html.duckduckgo.com` | HTTPS `443` | 可达；代码级 DDG fallback 可用 |
| DuckDuckGo Lite | `lite.duckduckgo.com` | HTTPS `443` | 可达；代码级 DDG fallback 可用 |
| Brave Search | `search.brave.com` | HTTPS `443` | 可达，可作为后续候选 |
| Startpage | `www.startpage.com` | HTTPS `443` | 可达但返回 captcha-block |
| Mojeek | `www.mojeek.com` | HTTPS `443` | 当前 HTTP 403 |
| Qwant | `api.qwant.com` | HTTPS `443` | 当前 HTTP 403 |

## 构建镜像时需要的临时白名单

如果在某台机器上从源码重建 SearXNG 镜像，需要额外放开：

| 目的域名 | 协议/端口 | 用途 |
|---|---:|---|
| `registry-1.docker.io` | HTTPS `443` | 拉 Docker Hub 镜像 manifest/layer |
| `auth.docker.io` | HTTPS `443` | Docker Hub token |
| `production.cloudflare.docker.com` | HTTPS `443` | Docker Hub layer CDN |
| `pypi.org` | HTTPS `443` | Python 包索引备用 |
| `files.pythonhosted.org` | HTTPS `443` | Python wheel/sdist 下载备用 |
| `pypi.tuna.tsinghua.edu.cn` | HTTPS `443` | 本项目默认 pip 镜像 |

## 当前配置建议

- 本机 SearXNG：继续监听 `10.10.132.126:8088`，只在内网开放。
- 远端 `10.10.130.178`：设置 `SEARXNG_BASE_URL=http://10.10.132.126:8088`，默认 `WEB_SEARCH_PROVIDERS=searxng`。只有确认远端可直接访问 DuckDuckGo 时才追加 `ddg`。
- 如果以后要把 SearXNG 也部署在远端，则把上面的 Bing/ClearURLs 公网白名单从 `10.10.132.126` 改为 `10.10.130.178`。
