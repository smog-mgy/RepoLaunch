# RepoLaunch

**从仓库，到运行。** 一个本地优先的 Python 项目启动与故障诊断助手。

输入公开 GitHub 仓库地址 → 固定源码提交 → 检索诊断知识 → 生成受控方案 → 人工确认 → Docker构建与运行 → HTTP就绪验证 → 导出运行报告。

> 当前为 v0.1 MVP。支持范围是受信任、CPU运行、入口清晰的Python Web项目，不是“任意仓库一键运行”。没有Docker时仍可分析仓库和体验明确标注的模拟演示。尚未验证任意项目安装成功率，不提供虚构的基准成绩。

## 快速开始

需要 Python 3.11+；真实项目运行还需要已启动的 Docker Engine/Desktop。首次安装会从软件包源下载项目自身的依赖。

### Windows

在项目目录打开 PowerShell：

```powershell
.\setup.ps1 -Python "你的Python3.11或更高版本的python.exe路径"
.\start.ps1
```

已经准备好 `.venv` 的这台电脑可以直接双击 `start.bat`。启动器会自动寻找8787—8806之间的可用端口，通过健康检查后打开默认浏览器；请保持启动窗口运行。实际地址显示在窗口和 `startup.log`，不应始终假定为8787。若系统执行策略限制脚本，可在终端执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`。

### Linux / macOS

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m repolaunch
```

Web页面点击“体验演示”可以走完分析、确认、模拟执行与报告导出，不需要API Key或Docker。演示不会下载输入仓库，不会实际构建容器，结果没有真实应用链接。

首次检查真实 Docker 环境时，点击首页“验证 Docker”，再审阅并确认内置方案。此功能使用随应用发布的 `repolaunch/fixtures/docker-check` 标准库 HTTP 服务，不下载 GitHub 仓库、不需要模型密钥或 PyPI 依赖；会真实构建镜像、启动受限容器、映射本机端口并检查 `/health`。首次可能需要下载 `python:3.11-slim` 基础镜像。成功后可打开真实容器页面、导出复现包，最后点击“停止容器”。这验证的是 Docker 执行链路，不代表第三方项目已安装成功。

如果页面显示 Docker 引擎访问被拒绝，请确认从普通 Windows 会话启动 RepoLaunch，并检查当前账户的 Docker 访问权限；这与 Docker 未安装不同，不应因此反复重装。安装或启动 Docker 后，可打开“模型与环境设置”刷新状态。更新应用源码后需要重启 RepoLaunch 才能使用新增入口。

## 一次启动完整本地集成

这台已准备好Python环境的Windows电脑，可双击 **start-integrations.bat**。保持Docker Desktop运行，脚本会下载固定版本镜像，部署MySQL、Milvus和Langfuse及其依赖，执行连接检查，再运行真实向量检索、模型修复和追踪验收，最后打开网页。首次需要下载多个镜像，建议为Docker准备12GB内存；本套服务只用于本地开发。

- 数据保存在项目内`.repolaunch/services`，容器镜像在Docker Desktop设置的磁盘位置。本机两者均位于F盘。
- ClickHouse使用Docker管理的Linux卷，避免Windows重命名问题，本机物理数据仍在F盘Docker虚拟磁盘中；旧目录保留。所有发布端口只绑定127.0.0.1；Langfuse默认地址为http://127.0.0.1:30300。本地登录信息在`.repolaunch/services/credentials.txt`，不应提交或分享。
- 生成的数据库、追踪服务凭证只保存在被忽略的本机配置中。规划密钥通过可选DPAPI加密保存；嵌入密钥需明确单独配置或勾选授权复用。
- 切换MySQL时，仅当目标历史表为空才复制原SQLite历史，原文件保留不变。
- 重复运行会保留凭证和数据。发生失败不会自动删除服务或清空卷，具体配置及维护限制见[infra/README.md](infra/README.md)。

模型和嵌入验收需要先配置相应密钥；未配置时报告未通过，网页仍会打开供配置。服务连接检查不等于业务验收。完整验收报告在`.repolaunch/full-stack-verification.json`，只有检查通过的项目才记为成功。

## 接入模型

在网页左下角“模型与环境设置”填写兼容 Chat Completions 的 Base URL、完整模型名称和 API Key。默认只存在当前进程内存中；Windows可勾选“在此Windows账户下加密记住配置”，用当前用户DPAPI保存到数据目录的credentials.enc，不写入明文文件。重启由同一Windows用户解密恢复，其他系统仍可使用内存配置。空密钥保留当前配置。模型服务由用户自行选择，按其实际规则计费。

保存配置不会自动计费探测；“测试已保存的连接”会发送一次短请求。方案记录实际耗时、请求次数和服务商返回的Token用量。SiliconFlow使用其兼容的max_tokens参数与JSON模式，DeepSeek规划请求关闭思考输出；模型不存在、鉴权、额度、限流和网络错误分别给出安全提示，不记录原始服务端错误正文。

也可以复制 `.env.example` 为 `.env`，设置 `REPOLAUNCH_BASE_URL`、`REPOLAUNCH_MODEL` 和 `REPOLAUNCH_API_KEY`；网页服务在启动时读取项目根目录 `.env`（已有环境变量优先）。不要提交真实密钥。显式环境变量优先于加密保存的值，包括空值；使用加密配置时，应从.env删除模板中的空REPOLAUNCH_API_KEY项。

未接入模型时明确使用**规则模式**，识别直接声明的FastAPI/Flask入口；这不是LLM推理。无论哪种模式都经过同一方案校验，不接受任意shell脚本。模型不会直接获得宿主机命令执行能力。

接入外部规划模型后，所选仓库的README、有限源文件及失败日志会发送到用户配置的模型服务。API Key不进入任务状态、不注入目标容器。日志做基础脱敏，但不能保证识别源码中所有秘密；请只分析公开且可分享的项目。

## 使用真实仓库

1. 输入 `https://github.com/owner/repository` 格式的公开仓库地址。
2. 查看固定提交、安装依赖、启动入口、HTTP验证路径和诊断依据。
3. 核实仓库可信、外部条件满足后确认执行。分析不会自动安装目标依赖。
4. Docker构建成功后，目标服务只向 `127.0.0.1` 发布随机端口。
5. HTTP路径返回2xx才标记就绪；这不代表业务功能完整通过。
6. 失败后可请求修复，最多2次；每次新方案都需要再次确认。真实模型修复只输出python_version、add_packages、summary、warnings、evidence五个补丁字段，由程序保留原方案；仅允许有依据地补充依赖或调整受支持的Python版本，不修改业务代码、不绕过测试。
7. 完成后可停止容器、导出方案与运行报告。构建镜像与快照会保留用于排查，可在Docker Desktop及本地数据目录手工管理。

GitHub API限流时会使用只读的 `git ls-remote` 获取固定提交（需要Git），再下载该提交的源码压缩包，不克隆子模块或执行Git钩子。

## 支持范围与边界

- Python 3.10—3.13目标运行环境，CPU，Uvicorn / Flask / Python文件入口。
- requirements文件或pyproject项目安装；仅使用公开PyPI包名形式的附加依赖。
- Python文件入口必须自行监听配置的HOST/PORT；脚本若不提供HTTP服务将无法通过就绪验证。
- 暂不支持GPU、系统包自动安装、Docker Compose多服务、私有仓库、子模块、任意启动shell、目标项目密钥注入。
- 缺必填环境变量时暂停，不能通过编辑方案清除已检测的约束。静态检测不是完整依赖分析，仍可能漏掉动态环境需求，运行失败会保留证据。
- 下载压缩包32MB、解压128MB、5000文件上限；拒绝路径穿越、符号链接和特殊文件。最多读取35个关键文件，内容总量有限。
- Docker运行非root、移除能力、限制CPU/内存/进程数和日志大小，不挂载宿主机目录或Docker socket，不传入规划模型凭证。
- **Docker不是运行恶意代码的强隔离承诺。** 构建和应用可访问网络；仅用于可信项目。构建阶段不保证具备与运行阶段相同的资源限制。首版只绑定本机，未设计为多用户公网服务。
- 默认SQLite持久化；重启会把执行中任务标记中断，不自动重试。成功容器可继续运行，在历史任务中停止；Docker引擎不可用时不能声称已清理。
- 固定的是仓库提交和运行配方，基础镜像及部分依赖版本仍可能浮动，不声称字节级环境复现。

## 技术实现

| 层 | 实现 |
|---|---|
| 网页与API | 原生HTML/CSS/JS、FastAPI、轮询任务事件，无CDN依赖 |
| Agent规划 | 真正的LangGraph检索→提案→校验图；LangChain ChatOpenAI适配器 |
| 工具执行 | Python受控工具、Docker CLI、下载及执行超时、取消与清理 |
| 知识检索 | 6条带官方来源的诊断案例；默认关键词检索，可选Milvus向量检索 |
| 持久化 | SQLAlchemy，默认SQLite，可选MySQL/PyMySQL |
| 可观测性 | 本地任务事件；可选Langfuse图回调与载荷遮蔽 |
| MCP | 独立stdio只读服务，查询任务状态和报告元数据 |

可选集成的真实配置与验证范围见 [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)。集成是可用代码入口，不代表已经连接用户的外部服务。

## 项目结构

```text
repolaunch/
  app.py          本机API、审批、任务生命周期、报告导出
  engine.py       仓库读取、方案校验、Docker执行与验证
  planner.py      LangGraph与模型/规则规划、受控修复
  knowledge.py    本地诊断案例与可选Milvus
  storage.py      SQLite/MySQL任务持久化
  mcp_server.py   独立只读MCP服务
  static/         中文网页
  fixtures/       演示、真实Docker检查及受控修复验证样例与 docker-check 真实环境验证样例
examples/hello-web/  可独立启动的最小FastAPI样例
infra/              固定版本本地集成服务及说明
tools/              部署、真实服务验收、MCP和小样本仓库验证
tests/              API、规划、执行、权限与MCP测试
```

## 测试

```sh
python -m pip install -e '.[dev]'
python -m pytest tests -q
```

Docker执行测试使用替身验证命令、权限和生命周期，不等于真实Docker联调；模型、MySQL、Milvus和Langfuse外部服务同样需要实际配置后验证。详细实测记录见[docs/VALIDATION.md](docs/VALIDATION.md)，三个选定公开仓库的固定提交和结果见[docs/BENCHMARK.md](docs/BENCHMARK.md)。

## 实际验收命令

```powershell
# 普通Windows会话，验证MySQL、Milvus、Langfuse与一次真实模型修复
.\.venv\Scripts\python.exe -m tools.verify_full_stack
# 真实MCP SDK握手并读取MySQL历史
.\.venv\Scripts\python.exe tools/verify_mcp.py --expected-backend mysql
# 三个指定公开仓库的真实模型规划、构建与业务探针，产生调用和下载
.\.venv\Scripts\python.exe tools/benchmark_repositories.py --execute --url http://127.0.0.1:8787
```

请把端口换为实际网页地址。这是三个简单项目的小样本验证，不代表任意仓库的安装成功率。

## 后续路线

- 在固定、可公开复现的仓库集合上建立启动基准，记录失败类别、耗时和调用成本。
- 支持由用户声明的业务冒烟测试，区分“端口可访问”和“实际功能可用”。
- 增加版本化镜像与依赖锁，完善资源回收与磁盘配额。
- 以实测需求扩展框架支持范围，不以“任意仓库”作宣传。

MIT License。仅用于受信任项目的本地开发与验证。
