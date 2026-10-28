# 可选集成

核心安装不需要外部数据库、向量服务或追踪账号。默认使用 SQLite、本地关键词检索和本地事件日志。这些默认模式会在结果中明确标注，不会被称作 MySQL、向量检索或外部追踪。

在项目根目录安装可选依赖：

```shell
python -m pip install -r requirements-integrations.txt
```

网页服务会在启动时读取项目根目录 `.env`，已有进程环境变量优先。索引命令支持显式--env-file并读取单独加密保存的嵌入配置；MCP只从.env补充DATABASE_URL与REPOLAUNCH_DATA_DIR，已有进程变量优先，禁用变量插值，不读取模型或追踪密钥。也可以在网页中设置规划模型；不同用途的密钥必须分别提供。

## MySQL：持久化任务历史

SQLAlchemy `TaskStore` 已支持 MySQL，Python驱动为 PyMySQL：

```text
DATABASE_URL=mysql+pymysql://USERNAME:PASSWORD@127.0.0.1:3306/repolaunch?charset=utf8mb4
REPOLAUNCH_DATA_DIR=F:/RepoLaunchData
```

先创建 `repolaunch` 数据库，并给专用账号授予所需表权限。启动时创建 `repolaunch_tasks` 表。账号密码中的特殊字符须按 URL 编码，不要将真实连接串提交进 Git。应用和 MCP 服务应使用相同的 `DATABASE_URL` 与 `REPOLAUNCH_DATA_DIR`。

`REPOLAUNCH_DATA_DIR` 仍用于仓库快照和导出文件；切换数据库不会自动迁移原有 SQLite 历史。默认 SQLite 已覆盖本地演示使用；可用项目根目录start-integrations.bat部署并验收本地MySQL。

## Milvus：诊断案例向量检索

启用条件是明确配置 Milvus 地址，同时提供独立的嵌入服务配置。不会自动借用网页中的规划模型密钥或 `OPENAI_API_KEY`。

```text
REPOLAUNCH_MILVUS_URI=http://127.0.0.1:19530
REPOLAUNCH_MILVUS_TOKEN=
REPOLAUNCH_EMBEDDING_BASE_URL=https://api.openai.com/v1
REPOLAUNCH_EMBEDDING_MODEL=text-embedding-3-small
REPOLAUNCH_EMBEDDING_API_KEY=YOUR_EMBEDDING_KEY
```

本地 HTTP 嵌入服务只允许显式回环地址，例如 `http://127.0.0.1:11434/v1`；远程嵌入服务要求 HTTPS。先部署 Milvus 或使用兼容的托管服务。完成环境配置后，在项目目录执行：

```shell
python -c "from repolaunch.knowledge import index_diagnostic_cases; print(index_diagnostic_cases())"
```

Windows加密配置可使用命令`python -m repolaunch.knowledge --env-file .env --index --smoke --status`，不会把规划密钥自动当作嵌入密钥。索引操作将仓库内置的六条诊断案例嵌入并写入专用集合 `repolaunch_diagnostics`。不会索引用户仓库或日志，不会删除其他集合。已有集合必须与所选嵌入模型维度兼容；更换模型时应使用匹配的新环境或手工管理专用集合。

查询只抽取诊断词表中的关键词后请求嵌入，避免上传完整代码、日志或其中的密钥。向量命中只用于选择内置案例编号，案例正文及来源仍来自本地受控语料。未配置、未建立集合或服务失败时回退本地关键词检索，在方案的 `retrieval.backend` 与 `retrieval.warning` 中说明真实状态。

## Langfuse：图执行追踪

配置以下环境变量后启用：

```text
LANGFUSE_PUBLIC_KEY=YOUR_PUBLIC_KEY
LANGFUSE_SECRET_KEY=YOUR_SECRET_KEY
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

支持自建 Langfuse；远程地址要求 HTTPS，本地回环服务可用 HTTP。初始化官方 Python SDK 客户端，给实际 LangGraph 调用挂载 `langfuse.langchain.CallbackHandler`，结束后刷新缓冲。客户端对输入、输出及其元数据统一遮蔽，只保留执行结构、时序和 SDK 支持的统计信息；不会把规划 API 密钥加入图状态。

方案的 `tracing.status` 为 `disabled`、`enabled` 或 `unavailable`。`enabled` 表示已启用 SDK 回调，不表示服务端已经确认接收；需要在你自己的 Langfuse 项目中检查是否收到追踪。凭证缺失或初始化失败时仍可使用本地事件日志。

## MCP：只读任务历史

MCP 是单独启动的 stdio 服务，不是网页后端接口。它直接只读访问同一SQLAlchemy数据存储，不创建任务管理器，不创建数据库表，也不会恢复或启动任何旧任务。先让网页服务初始化表结构；读取失败会返回脱敏错误。

```shell
python -m repolaunch.mcp_server
```

为支持 MCP 的客户端配置启动命令，例如：

```json
{
  "mcpServers": {
    "repolaunch-history": {
      "command": "F:/CodexData/RepoLaunch/.venv/Scripts/python.exe",
      "args": ["F:/CodexData/RepoLaunch/tools/mcp_history.py"]
    }
  }
}
```

请把命令与入口改为实际项目路径。此入口会固定加载该源码目录，避免MCP客户端的工作目录导致读取旧的已安装包；数据库配置从项目.env读取，无需把真实密码复制到客户端示例中。显式进程环境变量仍优先。

只提供三个工具：

- `list_tasks(limit=20)`：最近任务的摘要，最多50条。
- `task_status(task_id)`：状态、受控计划字段、必需配置名称及 HTTP 验证摘要。
- `export_metadata(task_id)`：报告下载 API 路径和文件清单，不读取报告正文。

执行`python tools/verify_mcp.py --expected-backend mysql`可通过真实MCP SDK验证stdio握手、工具声明、历史列表和任务状态。

工具不返回完整源码、原始日志、错误正文或宿主机文件路径。没有批准、执行、修复、停止、写文件或任意命令工具；任务的实际操作仍在本地网页中进行。

## 已验证范围

自动化测试覆盖 SQLite任务历史、规则/模型方案验证、受控修复、Milvus适配器调用与回退、Langfuse回调挂载与遮蔽、MCP只读工具和字段裁剪。适配器仍保留可控模拟测试；本机另外完成了真实MySQL提交读回、Milvus索引与向量查询、SiliconFlow模型修复、Langfuse服务端追踪读回和真实MCP SDK握手。固定样例与完整限制见VALIDATION.md。

本机缺少 Docker 时，演示与仓库分析可用，但不能声称真实项目已经完成 Docker 构建和运行。HTTP检查也只确认就绪状态，不代替目标项目的完整业务测试。
