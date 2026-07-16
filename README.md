# selvin-search-mcp

Selvin Search MCP 是一个面向 Codex 的本地联网搜索 MCP。它不会让模型凭记忆回答，而是先通过智谱 Web Search API 搜索，再把真实搜索结果交给用户配置的模型总结。

```text
用户问题
  -> 智谱 Web Search API /web_search
  -> search_result 真实搜索结果
  -> 用户配置的模型总结
  -> MCP web_search 返回 answer + session_id
  -> MCP get_sources 返回真实来源列表
```

## 敏感配置

项目不在代码里保存 API Key，也不在代码里写死具体模型名。运行前需要创建本地 `.env`：

```bash
cd selvin-search-mcp
cp .env.example .env
```

然后编辑 `.env`，填入自己的值：

```bash
SELVIN_PROVIDER=zhipu
SELVIN_API_URL=https://open.bigmodel.cn/api/paas/v4

SELVIN_API_KEY=<your-api-key>
# 或使用：
# ZHIPU_API_KEY=<your-api-key>

SELVIN_MODEL=<your-model-name>

ZHIPU_SEARCH_ENGINE=search_pro
ZHIPU_SEARCH_COUNT=5
ZHIPU_CONTENT_SIZE=high
ZHIPU_SEARCH_RECENCY_FILTER=noLimit
SELVIN_MAX_TOKENS=2600
```

`.env` 已被 `.gitignore` 忽略，不应提交。仓库只保留 `.env.example` 作为模板。

配置优先级：

1. 系统环境变量
2. 项目根目录 `.env`
3. 代码内非敏感默认值，例如 provider 和搜索参数

`SELVIN_MODEL` 是必填项；不配置模型名时 MCP 会返回配置错误。

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

通过智谱 `/web_search` 执行联网搜索，再用 `SELVIN_MODEL` 指定的模型基于搜索结果总结。

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
| `content` | 基于搜索结果生成的回答 |
| `sources_count` | 真实来源数量 |
| `cached` | 是否命中本进程缓存 |

如果智谱 `/web_search` 没有返回可用 `search_result`，工具会返回“未独立联网验证”，不会让模型凭空编来源。

### `get_sources`

使用 `web_search` 返回的 `session_id` 获取来源列表。

判断是否真的联网：

- `web_search.sources_count > 0`
- `get_sources(session_id).sources_count` 与 `web_search.sources_count` 一致
- 每条来源来自智谱 `/web_search` 的 `search_result`
- 来源里不会把模型正文中的 Markdown 链接当作可靠来源

### `get_config_info`

返回当前配置，并通过智谱 `/web_search` 做一次连通性测试。API Key 会被遮罩显示。

### `switch_model`

持久化切换总结模型。更推荐通过 `.env` 管理模型名；只有需要运行时临时切换时才使用这个工具。

## 实现说明

智谱模式使用两段式流程：

1. 调用 `POST {SELVIN_API_URL}/web_search`
2. 解析响应里的 `search_result`
3. 将 `search_result` 格式化为上下文
4. 调用 `POST {SELVIN_API_URL}/chat/completions`
5. 在回答末尾追加由真实 `search_result` 生成的 `## Sources`
6. MCP 内部缓存来源，供 `get_sources(session_id)` 查询

这样可以区分两件事：

- 搜索来源：来自智谱 Web Search API
- 回答文字：由用户配置的模型基于搜索结果总结

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
  src/selvin_search/providers/zhipu.py
```

配置读取检查：

```bash
uv run --project . python -c \
'from selvin_search.config import config; print(config.provider); print(config.api_url); print(config.model)'
```

预期输出应显示：

```text
zhipu
<your-api-url>
<your-model-name>
```

如果 `.env` 未配置 API Key 或模型名，会看到明确的配置错误。

## 注意事项

- 当前搜索质量取决于 `ZHIPU_SEARCH_ENGINE` 对应的召回结果。
- 如果中文查询召回为 0，可以换成中英混合查询。
- `sources_count = 0` 时，表示本次没有独立联网来源，不应把回答当作已验证结论。
