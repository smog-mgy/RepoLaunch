"""Read-only RepoLaunch MCP task history server (separately launched stdio).

No TaskManager is created: connecting an MCP client cannot resume jobs, approve
plans, install packages, create containers or execute repository commands.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from .storage import TaskStore

ROOT = Path(__file__).resolve().parent.parent
_TASK_ID = re.compile(r"[a-f0-9]{16,32}\Z")
_SUMMARY_FIELDS = ("id", "repository_url", "name", "mode", "status", "attempt", "created_at", "updated_at")
_PLAN_FIELDS = ("python_version", "install_mode", "requirements_file", "start_kind", "entrypoint", "port", "verify_path", "source")


def load_data_environment(path: Path | None = None) -> None:
    """Load only data-store settings. Never import model or tracing credentials.

    No interpolation is allowed: a database value cannot expand another secret
    from the .env file or the MCP client's process environment.
    """
    if all(name in os.environ for name in ("DATABASE_URL", "REPOLAUNCH_DATA_DIR")):
        return
    from dotenv import dotenv_values
    values = dotenv_values(path or ROOT / ".env", interpolate=False)
    for name in ("DATABASE_URL", "REPOLAUNCH_DATA_DIR"):
        value = values.get(name)
        if isinstance(value, str):
            os.environ.setdefault(name, value)


def _backend(store: Any) -> str:
    value = getattr(store, "backend", "unknown")
    return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{1,20}", value) else "unknown"


def _identifier(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("任务编号必须是 16 至 32 位十六进制字符串。")
    return task_id


def _summary(task: dict) -> dict:
    return {key: task.get(key) for key in _SUMMARY_FIELDS}


class ReadOnlyTaskTools:
    def __init__(self, store: Any):
        self.store = store

    def list_tasks(self, limit: int = 20) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit 必须为 1 至 50 的整数。")
        try:
            return [_summary(task) for task in self.store.list()[:limit]]
        except SQLAlchemyError:
            raise ValueError("无法读取任务数据库，请检查服务和只读账户权限。") from None

    def task_status(self, task_id: str) -> dict:
        try:
            task = self.store.get(_identifier(task_id))
        except KeyError:
            raise ValueError("找不到该任务。") from None
        except SQLAlchemyError:
            raise ValueError("无法读取任务数据库，请检查服务和只读账户权限。") from None
        result = _summary(task)
        plan = task.get("plan") or {}
        result["plan"] = {key: plan.get(key) for key in _PLAN_FIELDS}
        result["has_error"] = bool(task.get("error"))
        result["required_environment_names"] = list(plan.get("needs_env", []))
        verification = (task.get("result") or {}).get("verification") or {}
        result["verification"] = {key: verification.get(key) for key in ("passed", "status_code")}
        result["verification_scope"] = "模拟流程" if task.get("mode") == "demo" else "HTTP 就绪检查，不代表完整业务验证"
        result["details"] = "完整代码、日志、错误正文和本地路径不会通过 MCP 返回，请在本地网页查看。"
        return result

    def export_metadata(self, task_id: str) -> dict:
        status = self.task_status(task_id)
        return {
            "task_id": status["id"], "mode": status["mode"],
            "api_path": f"/api/tasks/{status['id']}/export",
            "format": "zip", "contains": ["report.json", "plan.json", "REPRODUCE.md", "Dockerfile", ".dockerignore"],
            "optional_files": [],
            "note": "仅返回导出入口元数据，不读取或传送报告正文。下载需使用本地 RepoLaunch 网页服务。",
        }


def create_server(store: Any):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError:
        raise RuntimeError("MCP 可选依赖未安装；请运行 pip install -e '.[integrations]'。") from None
    server = FastMCP(f"RepoLaunch — read-only task history ({_backend(store)})")
    tools = ReadOnlyTaskTools(store)
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=annotations)
    def list_tasks(limit: int = 20) -> list[dict]:
        """List recent RepoLaunch task summaries. Does not analyze or execute projects."""
        return tools.list_tasks(limit)

    @server.tool(annotations=annotations)
    def task_status(task_id: str) -> dict:
        """Read task status and sanitized plan fields; excludes code, logs and credentials."""
        return tools.task_status(task_id)

    @server.tool(annotations=annotations)
    def export_metadata(task_id: str) -> dict:
        """Read a report's local download route and file list, without report contents."""
        return tools.export_metadata(task_id)

    return server


def main() -> None:
    load_data_environment()
    data_dir = Path(os.environ.get("REPOLAUNCH_DATA_DIR", str(ROOT / ".repolaunch")))
    store = TaskStore(data_dir, os.environ.get("DATABASE_URL", ""), initialize=False)
    try:
        server = create_server(store)
        server.run(transport="stdio")
    finally:
        store.close()


if __name__ == "__main__":
    main()
