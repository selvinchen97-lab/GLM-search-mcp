# selvin-search-mcp

Selvin Search MCP 是一个面向 Codex 的本地联网搜索 MCP。它支持三种搜索链路：

| 模式 | 配置 | 搜索是谁做的 | Sources 从哪里来 |
| --- | --- | --- | --- |
| 并行搜索模式 | `SELVIN_SEARCH_MODE=parallel` | 智谱 Web Search API + 支持联网能力的模型同时搜索 | 两条链路返回的来源合并去重 |
| API 搜索模式 | `SELVIN_SEARCH_MODE=api` | 智谱 Web Search API | 智谱 `/web_search` 返回的 `search_result` |
| 模型联网模式 | `SELVIN_SEARCH_MODE=model_online` | 支持联网能力的模型自己搜索 | 模型回答中的 `## Sources` 链接，由 MCP 解析 |

默认推荐 `parallel` 模式：MCP 会同时启动 API 搜索链路和模型内置联网链路；如果模型内置联网先返回有效内容，MCP 会取消尚未完成的 API 搜索。随后 MCP 会抓取模型内置联网返回的来源页面，解析正文并交给模型做最终整理。`api` 模式更可控，`model_online` 模式更依赖模型/平台自己的联网能力。

## 敏感配置

项目不在代码里保存 API Key，也不在代码里写死具体模型名。运行前创建本地 `.env`：

```bash
cd selvin-search-mcp
cp .env.example .env
```

`.env` 已被 `.gitignore` 忽略，不应提交。仓库只保留 `.env.example` 作为模板。

配置优先级：

1. 系统环境变量
2. 项目根目录 `.env`
3. 代码内非敏感默认值，例如 provider、search mode 和搜索参数

`SELVIN_MODEL` 是必填项；不配置模型名时 MCP 会返回配置错误。

## 模式 A：并行搜索模式

这个模式同时启动两条搜索路线，但模型内置联网链路优先：

```text
用户问题
  -> 路线 1：智谱 Web Search API /web_search
  -> 路线 2：支持联网的模型 /chat/completions
  -> 如果路线 2 先返回有效内容，MCP 取消尚未完成的路线 1
  -> 如果路线 1 已经完成并有内容，MCP 同时保留两边结果
  -> MCP 解析模型联网回答中的来源链接
  -> MCP 抓取这些来源页面，抽取正文并归档
  -> MCP 合并 API 来源、模型来源和归档正文
  -> 用户配置的模型基于归档内容和两边结果做最终整理
  -> MCP web_search 返回最终 answer + session_id
  -> MCP get_sources 返回合并后的来源列表和抓取状态
```

`.env` 示例：

```bash
SELVIN_PROVIDER=zhipu
SELVIN_SEARCH_MODE=parallel
SELVIN_API_URL=https://open.bigmodel.cn/api/paas/v4

SELVIN_API_KEY=<your-api-key>
# 或：
# ZHIPU_API_KEY=<your-api-key>

SELVIN_MODEL=<your-model-name>

ZHIPU_SEARCH_ENGINE=search_pro
ZHIPU_SEARCH_COUNT=5
ZHIPU_CONTENT_SIZE=high
ZHIPU_SEARCH_RECENCY_FILTER=noLimit
SELVIN_MAX_TOKENS=2600

SELVIN_API_CANCEL_GRACE_SECONDS=0.5
SELVIN_FETCH_ONLINE_SOURCES=true
SELVIN_FETCH_ONLINE_SOURCE_COUNT=5
SELVIN_FETCH_ONLINE_SOURCE_CHARS=3000
SELVIN_ONLINE_USE_SEARCH_TOOL=true
```

如果模型内置联网链路使用的是另一个 OpenAI-compatible 平台，可以单独覆盖：

```bash
SELVIN_ONLINE_API_URL=<openai-compatible-api-base-url>
SELVIN_ONLINE_API_KEY=<your-online-model-api-key>
SELVIN_ONLINE_MODEL=<online-capable-model-name>
```

判断是否真的联网：

- API 路线：来源来自智谱 `/web_search` 的 `search_result`
- 模型内置路线：智谱/BigModel 默认通过 `chat/completions` 的 `tools.web_search` 触发搜索；其他 OpenAI-compatible 平台默认不传该工具，除非显式设置 `SELVIN_ONLINE_USE_SEARCH_TOOL=true`
- 模型内置路线会在提示词中强制要求输出原始页面的 exact URLs；如果上游模型或搜索工具不暴露 URL，MCP 会把这次结果视为缺少可验证来源
- 模型内置路线的来源来自模型回答里的 `## Sources`、Markdown 链接或 URL
- 模型内置路线返回的 URL 会被 MCP 再访问一次，成功时会写入 `archive_status=fetched` 和 `archive_content`
- `get_sources(session_id)` 返回的是两边来源合并去重后的列表

## 模式 B：API 搜索模式

这个模式先调用智谱 `/web_search`，拿到真实 `search_result`，再把搜索结果交给你配置的模型总结。

```text
用户问题
  -> 智谱 Web Search API /web_search
  -> search_result 真实搜索结果
  -> 用户配置的模型总结
  -> MCP web_search 返回 answer + session_id
  -> MCP get_sources 返回 search_result 来源列表
```

`.env` 示例：

```bash
SELVIN_PROVIDER=zhipu
SELVIN_SEARCH_MODE=api
SELVIN_API_URL=https://open.bigmodel.cn/api/paas/v4

SELVIN_API_KEY=<your-api-key>
# 或：
# ZHIPU_API_KEY=<your-api-key>

SELVIN_MODEL=<your-model-name>

ZHIPU_SEARCH_ENGINE=search_pro
ZHIPU_SEARCH_COUNT=5
ZHIPU_CONTENT_SIZE=high
ZHIPU_SEARCH_RECENCY_FILTER=noLimit
SELVIN_MAX_TOKENS=2600
```

判断是否真的联网：

- `web_search.sources_count > 0`
- `get_sources(session_id).sources_count` 与 `web_search.sources_count` 一致
- 每条来源来自智谱 `/web_search` 的 `search_result`

## 模式 C：模型联网模式

这个模式不先调用搜索 API，而是直接调用一个支持联网能力的聊天模型。模型自己搜索、自己写答案和来源，MCP 再从模型回答里解析来源链接。

```text
用户问题
  -> 支持联网的模型 /chat/completions
  -> 模型自己搜索并回答
  -> 模型在 ## Sources 写来源链接
  -> MCP 从模型回答中解析 links
  -> MCP get_sources 返回解析后的来源列表
```

`.env` 示例：

```bash
SELVIN_PROVIDER=custom
SELVIN_SEARCH_MODE=model_online
SELVIN_API_URL=<openai-compatible-api-base-url>
SELVIN_API_KEY=<your-api-key>
SELVIN_MODEL=<online-capable-model-name>
SELVIN_MAX_TOKENS=2600
```

注意：

- 这个模式要求你配置的模型/平台真的支持联网搜索。
- 如果模型没有联网能力，它可能只能回答“无法访问实时网络”，或凭记忆回答。
- MCP 会解析模型回答里的 `## Sources`、Markdown 链接或 URL，但无法像 API 搜索模式那样证明这些来源一定来自结构化搜索接口。

## 本地运行

```bash
uv run --project . selvin-search
```

## Codex 配置模板

当前 README 只提供模板，不会自动修改你的 Codex 项目配置。测试通过后，可以把下面配置加入项目级 Codex 配置：

```toml
[mcp_servers.selvin-search]
command = "uv"
args = [
  "run",
  "--project",
  ".",
  "selvin-search"
]
```

如果你希望 Codex 不依赖 `.env` 文件，也可以把环境变量写入 Codex 配置，但不要把真实配置提交到公开仓库：

```toml
[mcp_servers.selvin-search.env]
SELVIN_PROVIDER = "zhipu"
SELVIN_SEARCH_MODE = "parallel"
SELVIN_API_URL = "https://open.bigmodel.cn/api/paas/v4"
SELVIN_API_KEY = "<your-api-key>"
SELVIN_MODEL = "<your-model-name>"
ZHIPU_SEARCH_ENGINE = "search_pro"
ZHIPU_SEARCH_COUNT = "5"
ZHIPU_CONTENT_SIZE = "high"
ZHIPU_SEARCH_RECENCY_FILTER = "noLimit"
SELVIN_MAX_TOKENS = "2600"
```

## MCP 工具

### `web_search`

根据 `SELVIN_SEARCH_MODE` 执行联网搜索。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `query` | 要搜索的问题 |
| `platform` | 可选，追加到搜索词中用于聚焦平台或来源 |
| `model` | 可选，单次请求覆盖 `SELVIN_MODEL` |
| `plan_session_id` | 可选，配合规划工具使用；留空表示直接搜索 |

返回字段：

| 字段 | 说明 |
| --- | --- |
| `session_id` | 后续传给 `get_sources` |
| `content` | 基于搜索结果或模型联网搜索生成的回答 |
| `sources_count` | MCP 解析出的来源数量 |
| `cached` | 是否命中本进程缓存 |

### `get_sources`

使用 `web_search` 返回的 `session_id` 获取来源列表。

### `get_config_info`

返回当前配置，并做一次连通性测试：

- `parallel` 模式：同时请求 `{SELVIN_API_URL}/web_search` 和在线模型接口的 `/models`
- `api` 模式：请求 `{SELVIN_API_URL}/web_search`
- `model_online` 模式：请求 `{SELVIN_API_URL}/models`

API Key 会被遮罩显示。

### `switch_model`

持久化切换总结模型。更推荐通过 `.env` 管理模型名；只有需要运行时临时切换时才使用这个工具。

## 文件与缓存位置

| 类型 | 路径 |
| --- | --- |
| 项目目录 | 当前仓库根目录 |
| Python 包 | `src/selvin_search` |
| 本地环境变量 | `.env` |
| 环境变量模板 | `.env.example` |
| 配置文件 | `~/.config/selvin-search/config.json` |
| 日志目录 | `~/.config/selvin-search/logs` |
| 规划会话 | `~/.config/selvin-search/sessions` |

## 快速验证

编译检查：

```bash
uv run --project . python -m py_compile \
  src/selvin_search/config.py \
  src/selvin_search/server.py \
  src/selvin_search/providers/zhipu.py \
  src/selvin_search/providers/model_online.py
```

配置读取检查：

```bash
uv run --project . python -c \
'from selvin_search.config import config; print(config.provider); print(config.search_mode); print(config.api_url); print(config.model)'
```

预期输出应显示：

```text
<your-provider>
<parallel|api|model_online>
<your-api-url>
<your-model-name>
```

如果 `.env` 未配置 API Key 或模型名，会看到明确的配置错误。

## 注意事项

- `parallel` 模式会合并 API 搜索和模型内置搜索的来源，最终回答由模型再次整理。
- `api` 模式的 sources 更可控，因为它们来自搜索接口的结构化结果。
- `model_online` 模式依赖模型和平台自己的联网能力。
- 如果中文查询召回为 0，可以换成中英混合查询。
- `sources_count = 0` 时，表示本次没有独立联网来源，不应把回答当作已验证结论。
