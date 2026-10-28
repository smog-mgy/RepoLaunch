# 内置 Docker 验证样例

此样例随 RepoLaunch 发布，使用 Python 标准库，不下载 GitHub 仓库，不需要模型 API Key，不安装 PyPI 包。
确认方案后由真实 Docker 引擎构建并运行。首次可能下载 python:3.11-slim 基础镜像。
启动入口：python main.py。容器端口：8000。GET /health 返回 status=ok JSON；GET / 提供说明页面。
本样例验证容器构建、启动、端口映射和 HTTP 就绪，不代表任意第三方项目安装成功。
