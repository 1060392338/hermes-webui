# Hermes WebUI

基于 FastAPI + ACP stdio bridge 的浏览器对话界面，通过 WebSocket 让浏览器与 Hermes Agent 对话。

## 功能特性

- 🌐 **浏览器对话** — 实时流式输出（SSE），支持多会话
- 🔌 **多 Provider 切换** — Hermes（默认）、OpenAI 兼容 API、MCP
- 🛠️ **技能系统** — 动态加载/开关/上传技能，持久化状态
- 💾 **记忆与会话** — 自动保存对话历史，支持上下文续接
- 🎨 **玻璃拟态 UI** — 深色渐变 + blur 效果
- 🤖 **Hermes 主动引导** — 技能 `webui-launcher` 让 Hermes 知道 WebUI 存在，可主动引导用户访问

## 目录结构

```
web_chat/
├── app.py        # FastAPI 后端（Bridge 调度、API 端点、会话管理）
└── index.html    # 单页前端（技能面板、模型配置、聊天 UI）
```

## 依赖

| 包 | 用途 |
|---|---|
| `fastapi` | Web 框架 |
| `uvicorn` | ASGI 服务器 |
| `httpx` | OpenAI 兼容 API 调用 |
| `pydantic` | 数据模型 |

> 这些依赖已在 Hermes Agent 的虚拟环境 `~/.hermes/hermes-agent/venv/` 中安装，**不需要单独安装**。

## 快速启动

### 一键启动（推荐）

```bash
~/.hermes/web_chat/start-all.sh
```

这会同时启动：
- **Hermes Agent** — 运行在终端前台
- **web_chat** — 运行在 http://localhost:8080

启动后直接访问 http://localhost:8080 使用 WebUI。

### 其他用法

```bash
# 只启动 Hermes（不启动 web_chat）
~/.hermes/web_chat/start-all.sh --hermes-only

# 只启动 web_chat（后台）
~/.hermes/web_chat/start-all.sh --web-only

# 停止 web_chat
~/.hermes/web_chat/start-all.sh --stop
```

### 手动启动（不推荐）

```bash
# 终端 1：启动 web_chat
cd ~/.hermes/web_chat
~/.hermes/hermes-agent/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080

# 终端 2：启动 Hermes
~/.hermes/hermes-agent/venv/bin/python -m hermes
```

启动后访问 **http://localhost:8080**

## 配置文件

### 1. `~/.hermes/.env`（必需）

WebUI 通过 Hermes Agent 的 `.env` 读取 API 密钥和渠道配置。**不需要为 WebUI 新建任何配置文件**，只需确保以下变量存在：

```env
# API 密钥（必需）
MINIMAX_API_KEY=your_minimax_api_key
MINIMAX_BASE_URL=https://api.minimax.chat/v1  # 或你的 MiniMax 部署地址

# LLM 模型
HERMES_MAX_ITERATIONS=90

# 如果使用 OpenAI Provider，API Key 可在 WebUI 模型配置面板中填写
# 无需写在 .env 中
```

### 2. 模型切换（WebUI 内配置）

在浏览器界面点击 **「模型」** 面板，可切换 Provider：

| Provider | 说明 |
|---|---|
| `hermes`（默认）| 通过 ACP subprocess 调用 Hermes Agent（使用 `.env` 中的 `MINIMAX_API_KEY`）|
| `openai` | 直接 HTTP 调用 OpenAI 兼容 API（需填写 API URL、Key、模型名）|
| `mcp` | 通过 MCP JSON-RPC stdio 调用 MCP 服务器 |

### 3. 技能系统

技能目录位于 `~/.hermes/skills/`，由 Hermes Agent 管理。WebUI 动态扫描该目录生成技能列表。

```
~/.hermes/skills/
├── official/    # 官方技能（只读）
└── custom/     # 自定义技能（可上传）
```

- 技能开关状态保存在 `~/.hermes/web_chat_history/skills_state.json`
- 上传新技能：在 WebUI 「技能」面板填写名称和 SKILL.md 内容，提交后存入 `~/.hermes/skills/custom/`

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | WebUI 主页 |
| `POST` | `/chat` | 普通对话 |
| `POST` | `/chat/stream` | 流式对话（SSE）|
| `GET` | `/api/sessions` | 会话列表 |
| `POST` | `/api/sessions` | 创建会话 |
| `GET` | `/api/sessions/<id>` | 获取会话 |
| `POST` | `/api/sessions/<id>/messages` | 追加消息 |
| `GET` | `/api/memory` | 记忆内容 |
| `POST` | `/api/memory` | 更新记忆 |
| `GET` | `/api/skills/list` | 技能列表（分类）|
| `POST` | `/api/skills/toggle` | 开关技能 |
| `POST` | `/api/skills/upload` | 上传自定义技能 |
| `GET` | `/api/skills/<name>` | 技能详情 |
| `GET` | `/api/model/config` | 模型配置 |
| `POST` | `/api/model/config` | 更新模型配置 |

## 常见问题

**Q: 技能列表一直显示"加载中"**
检查浏览器控制台是否有 `ReferenceError`，如 `statusDot is not defined`。确保代码已更新到最新版本后强制刷新（`Cmd+Shift+R`）。

**Q: 切换到 OpenAI Provider 后报 401 错误**
确认 API Key 正确，且账户有对应模型的访问权限。

**Q: 启动报 `Module not found: uvicorn`**
使用 Hermes Agent 的虚拟环境 Python：`~/.hermes/hermes-agent/venv/bin/python`
