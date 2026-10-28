# 在 Windows 上启动 RepoLaunch

## 已完成安装的电脑

双击项目目录中的 **start.bat**。启动窗口必须保持打开；关闭它或按 Ctrl+C 会停止网页服务。

启动器会检查本机端口，加载应用并验证 /api/health，确认响应属于 RepoLaunch 后才请求默认浏览器打开页面。若浏览器未出现，可手动访问：

http://127.0.0.1:8787

默认启动遇到端口占用时，会自动尝试 8788 至 8806，不会结束已有程序。请以窗口或 startup.log 中「服务已通过就绪检查」后面的实际地址为准，浏览器也会打开该实际地址。指定 -Port 时固定使用该端口，不自动更换。

访问本机地址不要求已安装 Docker。Docker 只用于真实项目的构建与运行；缺少 Docker 时，仍可使用网页、内置演示和仓库分析。

## 使用其他端口

在普通 PowerShell 窗口中运行：

    powershell -NoProfile -ExecutionPolicy Bypass -File F:\CodexData\RepoLaunch\start.ps1 -Port 8788

只启动服务，不自动打开浏览器：

    powershell -NoProfile -ExecutionPolicy Bypass -File F:\CodexData\RepoLaunch\start.ps1 -NoBrowser

也可在已激活的项目虚拟环境中运行：

    python -m repolaunch --port 8787 --open-browser

## 无法打开页面时

1. 检查启动窗口是否仍然打开。RepoLaunch 是前台本地应用，不安装 Windows 服务或开机启动项。
2. 查看项目根目录的 **startup.log**。日志记录 Python 版本、启动错误、端口占用、应用就绪和浏览器唤起结果，不打印环境变量或模型密钥。日志自动轮换，最多保留三个约 1MB 文件。
3. 若端口由 RepoLaunch 占用，启动器会复用已经通过健康检查的服务。若由其他程序占用，启动器不会终止该程序或自动打开它；默认启动会自动选择下一个可用端口。
4. 若提示未找到 .venv\Scripts\python.exe，先运行 setup.ps1。
5. 若应用依赖、数据库或项目目录权限导致启动失败，startup.log 会保留具体错误。修复后重新双击 start.bat。
6. 在受限的自动化执行环境中启动的进程，可能无法从桌面浏览器访问，或者会随执行会话结束。请在自己的 Windows 会话中双击 start.bat；工具内的进程存活不代表桌面浏览器已可访问。

浏览器被请求打开不代表页面一定已经呈现；请以实际浏览器页面和日志中的就绪检查结果为准。


## Docker安装后验证

Docker Desktop显示Engine running后，重启RepoLaunch，使进程读取新的安装位置。网页点击“验证Docker”，检查内置方案并确认运行。它会真实构建容器、启动标准库HTTP服务并访问/health，不需要GitHub或模型密钥，首次运行可能下载Python基础镜像。成功后打开应用链接，再点击停止容器。

“体验演示”是模拟流程，不代表Docker已可用。若提示权限不足，应从普通Windows会话启动RepoLaunch；不要为解决工具环境限制而开放无加密的Docker TCP接口。


## 已启用完整集成后的日常运行

本机已使用MySQL保存任务历史，运行网页前请先启动Docker Desktop，等待服务就绪。双击start.bat仅启动网页；start-integrations.bat会部署/启动服务、验证连接并补跑包含模型请求的完整验收。模型密钥由同一Windows账户解密恢复。

服务后台会继续运行。需要停止本项目服务而保留数据，可在项目目录使用Docker Compose的stop命令，指定项目repolaunch-integrations、infra/compose.yaml及.repolaunch/services/services.env；不需要删除数据卷。具体服务端口与数据说明见infra/README.md。
