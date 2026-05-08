# Codex Responses 请求捕获设计

日期：2026-05-08

## 目标

在 `agentd` 启动 Codex 时，统一保存 Codex 最终发给模型供应商的原始 HTTP `POST /v1/responses` 请求。

这里的“原始请求”指 Codex 已经完成模型选择、instructions/input/tools 组装、metadata 注入、鉴权 header 注入之后，真正要发往 Responses API 的请求。目标不是只记录 app-server JSON-RPC，也不是只记录抽象 messages。

需要保存：

- 请求 path、method、headers、body 原始字节。
- body 的可读 JSON 副本。如果 Codex 启用 `Content-Encoding`，proxy 需要额外保存解压后的 JSON。
- 上游响应的 status、headers、SSE/JSON body，便于回放和排障。
- 与 `agentd` session、Codex thread、Codex turn 的关联信息。

## 结论

最合适的方式是做一个本地 Responses API capture proxy，而不是 fork Codex 或在 `agentd` 的 app-server JSON-RPC 层拦截。

推荐链路：

```text
agentd
  -> codex app-server --listen stdio://
    -> http://127.0.0.1:<capture-port>/v1/responses
      -> agentd capture proxy
        -> real upstream /responses
```

这样抓到的是 Codex 发送模型请求时的最终 HTTP 请求体，同时不需要改 Codex 源码。

## 为什么 JSON-RPC 层不够

当前 `agentd` 通过 `CodexAppServer` 启动 Codex app-server，并用 stdio JSON-RPC 与它交互：

- `src/agentd/codex_app_server.py`
- `CodexAppServer.run_turn()` 里构造 `argv = [*shlex.split(self.config.command), "app-server", ..., "--listen", "stdio://"]`

这个层面只能看到：

- `initialize`
- `thread/start` / `thread/resume`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- app-server 返回的事件

但 Codex 最终发给模型的 `ResponsesApiRequest` 是在 Codex Rust core 内部组装的，包含最终 instructions、input、tools、stream、client_metadata 等内容。仅拦截 JSON-RPC 会漏掉 Codex 内部补齐、压缩、metadata、header、provider 差异。

## Codex 源码依据

OpenAI Codex 源码路径：

```text
/home/sine/code/github/openai/codex
```

关键点：

- provider URL 拼接在 `codex-rs/codex-api/src/provider.rs:53`，`url_for_path()` 会用 `base_url + path` 生成请求 URL。
- provider 配置结构在 `codex-rs/model-provider-info/src/lib.rs:80`，字段包括 `base_url`、`env_key`、`wire_api`、`requires_openai_auth`、`supports_websockets`。
- 默认 provider base URL 在 `codex-rs/model-provider-info/src/lib.rs:232` 附近决定：ChatGPT auth 默认走 `https://chatgpt.com/backend-api/codex`，API key 默认走 `https://api.openai.com/v1`，但显式 `base_url` 会覆盖它。
- Responses HTTP 请求发送在 `codex-rs/codex-api/src/endpoint/responses.rs:70`，path 是 `"responses"`。
- request body 在 `codex-rs/codex-api/src/endpoint/responses.rs:84` 由 `ResponsesApiRequest` 序列化成 JSON value 后发送。
- request compression 在 `codex-rs/core/src/client.rs:1143` 附近判断：开启 compression、ChatGPT backend auth、且 provider `name` 是 OpenAI 时，可能使用 Zstd。

## ChatGPT Pro 功能影响

正常不会影响 ChatGPT Pro 的账号、剩余额度、rate limit 查询，前提是只代理模型 provider 请求，不代理全部网络流量。

源码依据：

- `GetAccountRateLimits` 在 `codex-rs/app-server/src/message_processor.rs:1185` 进入 account processor。
- 额度读取在 `codex-rs/app-server/src/request_processors/account_processor.rs:919` 使用 `BackendClient::from_auth(self.config.chatgpt_base_url.clone(), &auth)`。
- 也就是说额度查询走 `chatgpt_base_url` 对应的 ChatGPT backend，不走 `model_provider.base_url`。

必须遵守：

- 不修改 `chatgpt_base_url`。
- 不设置全局 `HTTPS_PROXY` / MITM 去拦截 Codex 所有请求。
- capture proxy 只作为 `model_provider.base_url`。
- proxy 透明转发 response headers、status、SSE body，不能吞掉 rate-limit header 或 SSE event。
- 如果用户使用 ChatGPT Pro 登录态，模型 upstream 应保持 Codex 默认的 ChatGPT backend，而不是强行转到 `https://api.openai.com/v1`，否则就会变成 API key 计费/鉴权语义，影响 Pro 使用。

## Provider 注入设计

agentd 启动 capture proxy 后，给 Codex app-server 注入临时 provider override：

```toml
model_provider = "agentd-capture"

[model_providers.agentd-capture]
name = "OpenAI"
base_url = "http://127.0.0.1:<capture-port>/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

说明：

- `base_url` 指向本地 proxy 后，Codex 会请求 `http://127.0.0.1:<capture-port>/v1/responses`。
- `wire_api = "responses"` 保持 Responses API。
- `requires_openai_auth = true` 保持 OpenAI/ChatGPT 登录态语义。
- `supports_websockets = false` 强制走 HTTP streaming，便于稳定捕获原始 `/v1/responses` 请求。
- `name = "OpenAI"` 可以保留 Codex 对 OpenAI provider 的部分行为，但要注意这可能触发 request compression；proxy 需要同时保存 raw bytes 和 decoded JSON。

不要直接覆盖用户全局 `~/.codex/config.toml`。更稳妥的方式是在 `agentd` 启动 Codex app-server 时通过 `-c` 临时 override 注入，进程结束后不污染用户配置。

## Upstream 选择

capture proxy 需要知道真实 upstream。建议支持两种模式：

```toml
[codex.capture]
enabled = true
upstream_mode = "codex-default"
```

`codex-default` 的含义：

- 如果当前 Codex 使用 ChatGPT auth / Agent Identity：转发到 `https://chatgpt.com/backend-api/codex/responses`。
- 如果当前 Codex 使用 API key：转发到 `https://api.openai.com/v1/responses`。

也可以支持显式配置：

```toml
[codex.capture]
enabled = true
upstream_url = "https://chatgpt.com/backend-api/codex/responses"
```

注意：如果当前用户计划换成普通 `codex`，这是好事。agentd 应该从当前的 `bin/acodex` shell wrapper 迁移到直接启动普通 `codex app-server`，再通过 `-c` 注入 capture provider。这样比依赖 `zsh -ic 'acodex "$@"'` 更可控。

## Proxy 行为

proxy 对入站请求：

- 只接受 `POST /v1/responses`，其他 path 默认透传或拒绝，取决于后续是否要记录 `/models` 等辅助请求。
- 读取并保存原始 request body bytes。
- 保存 request headers 的原始副本，但默认在索引和普通日志里 redact `Authorization`、`Cookie`、`Set-Cookie`、`ChatGPT-Account-ID` 等敏感值。
- 根据 `Content-Encoding` 生成 decoded body 副本，便于搜索和分析。
- 不重新构造 body，直接用原始 bytes 转发到 upstream，避免改变 Codex 真实请求。

proxy 对上游响应：

- 立即流式转发 SSE chunk，不能等完整响应结束再返回。
- 同时 tee 一份 response bytes 到文件。
- 保存 status 和 headers。
- 如果响应中有 rate limit 相关 header 或 SSE event，原样转发。
- 上游断流时保留 partial response 和错误信息，方便排查。

## 存储设计

建议用 SQLite 做索引，raw body 落文件。

表名可以叫 `model_http_exchanges`：

```text
id
created_at
completed_at
session_id
codex_thread_id
codex_turn_id
method
request_path
upstream_url
provider_id
model
stream
status_code
request_headers_path
request_body_raw_path
request_body_decoded_path
response_headers_path
response_body_raw_path
error
```

raw 文件：

```text
<state_dir>/captures/responses/<yyyy-mm-dd>/<exchange-id>-request.headers.json
<state_dir>/captures/responses/<yyyy-mm-dd>/<exchange-id>-request.raw
<state_dir>/captures/responses/<yyyy-mm-dd>/<exchange-id>-request.json
<state_dir>/captures/responses/<yyyy-mm-dd>/<exchange-id>-response.headers.json
<state_dir>/captures/responses/<yyyy-mm-dd>/<exchange-id>-response.sse
```

权限建议：

- capture 目录默认 `0700`。
- 普通日志不打印 prompt/body。
- 原始 headers 如果包含凭据，单独开关控制是否保存未脱敏版本。

## 关联 turn/session

agentd 当前已经知道：

- `session.id`
- Codex `threadId`
- Codex `turnId`

Codex app-server 的 `turn/start` 协议支持 `responsesapiClientMetadata`，可用于把 agentd 的 session/turn 信息塞进 Codex 最终 Responses 请求的 `client_metadata` 或相关 metadata header 中。

可参考源码：

- `codex-rs/app-server-protocol/src/protocol/v2/turn.rs`
- `codex-rs/core/src/turn_metadata.rs`
- `codex-rs/core/src/client.rs` 中 `x-codex-turn-metadata` 的生成逻辑

设计上应在 `turn/start` 时传入类似：

```json
{
  "agentd_session_id": "...",
  "agentd_request_id": "...",
  "codex_thread_id": "...",
  "codex_turn_id": "..."
}
```

如果某些字段只有 `turn/start` 返回后才知道，则 proxy 可以先按时间窗口和 thread metadata 关联，再在 app-server event 到达后补写索引。

## Nexus chat_tracker 可参考点

参考路径：

```text
/home/sine/code/tencent/nexus-agent/nexus/nexus/trace/processors/chat_tracker.py
```

`ChatTrackerProcessor` 的思路是：

- 注册为 processor：`@span_processor_registry.register("chat_tracker")`
- 只关心 `openai_completion` span。
- 从 span attributes 里取 `inputs` / `outputs`。
- 把 messages 和 LLM 输出组织成一棵 trajectory tree。
- 最后 `get_result()` 输出可分析/训练的数据结构。

可借鉴的点：

- capture 逻辑做成独立组件，不侵入主业务流程。
- 保存 inputs/outputs 的完整 generation detail，而不是只保存最终文本。
- 归一化 volatile 字段，便于比较和去重。
- 提供可回放/可导出的结构化结果。

但本项目目标不同：这里要记录的是 Codex 最终 HTTP `/v1/responses` 请求，所以应放在 HTTP provider proxy 层，而不是只记录 SDK/OpenAI completion span。

## 当前 agentd 启动方式的问题

当前默认命令在 `src/agentd/config.py` 中会落到 repo 内 `bin/acodex`。

`bin/acodex` 实际上执行：

```text
zsh -ic 'acodex "$@"'
```

这依赖用户 shell 中的 `acodex` 函数/alias，行为不透明，也不利于注入稳定的 provider override。

后续建议：

- `codex.command` 默认改为普通 `codex` 或绝对路径。
- `agentd` 负责拼 `codex app-server --listen stdio://`。
- capture provider 通过 `config_overrides` 临时注入。
- 保留用户显式配置 command 的能力。

## 实施顺序建议

1. 新增 `codex.capture` 配置结构，默认关闭。
2. 实现一个最小 HTTP streaming proxy：只支持 `POST /v1/responses`，raw request/response 落文件。
3. 在 `CodexAppServer.run_turn()` 启动 Codex 前启动 proxy，拿到本地端口。
4. 通过 `config_overrides` 注入 `agentd-capture` provider 和 `model_provider = "agentd-capture"`。
5. 在 `turn/start` 里加入 correlation metadata。
6. 增加 SQLite 索引和查询命令。
7. 再处理压缩、敏感字段脱敏、失败重试、cleanup、测试覆盖。

## 测试建议

单元测试：

- provider override 生成正确。
- proxy 保存 raw body，不修改 body。
- `Authorization` 等敏感 header 在索引中脱敏。
- SSE response 能边转发边落盘。
- upstream 断流时保留 partial capture。

集成测试：

- 用本地 fake upstream 模拟 `/v1/responses` SSE。
- 启动 `agentd` -> `codex app-server` -> capture proxy -> fake upstream。
- 验证 Codex turn 能正常完成。
- 验证 capture 文件包含最终 Responses request body。
- 验证 `account/rateLimits/read` 不经过 capture proxy。

## 主要风险

- 错把全部 Codex 网络流量都代理了，会影响 ChatGPT Pro 账号和额度查询。
- ChatGPT auth 下 upstream 选错到 `api.openai.com/v1`，会改变鉴权/计费语义。
- WebSocket transport 会绕过 HTTP `/v1/responses` 捕获，所以 capture provider 要禁用 `supports_websockets`。
- Request compression 可能导致 body 不是直接可读 JSON，proxy 必须保存 raw bytes 并额外解码。
- 原始请求体包含完整 prompt、上下文、工具定义和可能的敏感信息，存储权限和脱敏策略必须明确。

