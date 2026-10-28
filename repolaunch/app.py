from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from . import engine
from .planner import Planner, check_model_connection
from .storage import TaskStore, now

ROOT = Path(__file__).resolve().parent.parent
ACTIVE = {"analyzing", "running", "repairing"}
MAX_REPAIRS = 2


class CreateTask(BaseModel):
    repository_url: str = Field(default="", max_length=500)
    mode: str = "real"


class Approval(BaseModel):
    plan: dict | None = None


class ModelSettings(BaseModel):
    base_url: str = Field(default="https://api.openai.com/v1", max_length=500)
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=1024)
    remember: bool = False
    use_for_embeddings: bool = False
    embedding_model: str = Field(default="BAAI/bge-m3", min_length=1, max_length=200)


def validate_provider(url: str) -> str:
    parsed = urlsplit(url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("模型地址不能包含账号、密码、查询参数或片段。")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("模型服务需要 HTTPS；本机服务允许 HTTP。")
    return url.rstrip("/")


class TaskManager:
    def __init__(self, data_dir: Path, database_url: str = ""):
        self.data_dir = data_dir.resolve()
        self.store = TaskStore(self.data_dir, database_url)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="repolaunch")
        self.lock = threading.RLock()
        self.cancel_flags: dict[str, threading.Event] = {}
        self._model_task_settings: dict[str, dict] = {}
        saved = {}
        self.secure_storage = {"supported": os.name == "nt", "loaded": False, "saved": False, "warning": ""}
        try:
            from .credentials import load_credentials
            saved = load_credentials(self.data_dir)
            self.secure_storage["loaded"] = bool(saved)
            self.secure_storage["saved"] = bool(saved)
        except ValueError:
            self.secure_storage["warning"] = "已保存的加密凭证无法读取，请重新配置；未读取明文凭证。"
        self.settings = {
            "api_key": os.environ.get("REPOLAUNCH_API_KEY", saved.get("api_key", "")),
            "base_url": os.environ.get("REPOLAUNCH_BASE_URL", saved.get("base_url", "https://api.openai.com/v1")),
            "model": os.environ.get("REPOLAUNCH_MODEL", saved.get("model", "gpt-4o-mini")),
        }
        for field, variable in (("embedding_api_key", "REPOLAUNCH_EMBEDDING_API_KEY"), ("embedding_base_url", "REPOLAUNCH_EMBEDDING_BASE_URL"), ("embedding_model", "REPOLAUNCH_EMBEDDING_MODEL")):
            if variable not in os.environ and saved.get(field):
                os.environ[variable] = saved[field]
        self.embedding_settings = {"api_key": os.getenv("REPOLAUNCH_EMBEDDING_API_KEY", ""), "base_url": os.getenv("REPOLAUNCH_EMBEDDING_BASE_URL", ""), "model": os.getenv("REPOLAUNCH_EMBEDDING_MODEL", "BAAI/bge-m3")}
        self._secrets = {value for value in (self.settings["api_key"], self.embedding_settings["api_key"], os.getenv("LANGFUSE_SECRET_KEY", ""), os.getenv("REPOLAUNCH_MILVUS_TOKEN", "")) if value}
        self.model_check_lock = threading.Lock()
        self.model_connection = {"status": "not_tested", "checked_at": None, "message": "当前配置尚未测试连接。"}
        for task in self.store.list():
            if task["status"] in ACTIVE:
                self.update(task["id"], status="failed", error="应用在任务执行期间重启。请检查保留的容器，并重新分析或提出修复。")
                self.emit(task["id"], "warning", "检测到上次中断；未自动继续执行命令。")

    def redact(self, value: str) -> str:
        text = str(value)
        secrets = set(self._secrets)
        current_key = self.settings.get("api_key", "")
        if current_key:
            secrets.add(current_key)
        for key in sorted(secrets, key=len, reverse=True):
            text = text.replace(key, "[REDACTED]")
        text = re.sub(r"(?i)(api[_-]?key|authorization|token|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
        return text

    def scrub(self, value):
        """Remove configured credentials before task persistence or API/report output."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: "[REDACTED]" if str(key).lower() in {"api_key", "authorization", "password", "access_token", "refresh_token"} else self.scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value

    def configure_model(self, base_url: str, model: str, api_key: str = "", remember: bool = False, use_for_embeddings: bool = False, embedding_model: str = "BAAI/bge-m3") -> dict:
        base_url = validate_provider(base_url)
        model = model.strip()
        if not model or any(char in model for char in "\r\n"):
            raise ValueError("请填写有效模型名称；允许服务商提供的组织名/模型名格式。")
        if any(char in api_key for char in "\r\n"):
            raise ValueError("API Key格式无效。")
        embedding_model = embedding_model.strip()
        if use_for_embeddings and (not embedding_model or any(char in embedding_model for char in "\r\n")):
            raise ValueError("请填写有效的嵌入模型名称。")
        with self.lock:
            old_key = self.settings.get("api_key", "")
            new_key = api_key.strip() or old_key
            if use_for_embeddings and not new_key:
                raise ValueError("同时用于嵌入服务前，请先填写或保存 API Key。")
            self._secrets.update(key for key in (old_key, new_key) if key)
            self.settings = {"base_url": base_url, "model": model, "api_key": new_key}
            self.model_connection = {"status": "not_tested", "checked_at": None, "message": "配置已保存，尚未测试连接。"}
            if use_for_embeddings:
                self.embedding_settings = {"api_key": new_key, "base_url": base_url, "model": embedding_model}
                os.environ.update(REPOLAUNCH_EMBEDDING_API_KEY=new_key, REPOLAUNCH_EMBEDDING_BASE_URL=base_url, REPOLAUNCH_EMBEDDING_MODEL=embedding_model)
            payload = {**self.settings, **{"embedding_" + key: value for key, value in self.embedding_settings.items() if value}}
        message = "设置已应用到当前进程内存；未写入明文文件。"
        if remember:
            try:
                from .credentials import save_credentials
                save_credentials(self.data_dir, payload)
                self.secure_storage.update(saved=True, warning="")
                message = "配置已使用当前 Windows 用户的 DPAPI 加密保存，未保存明文密钥。"
            except ValueError:
                self.secure_storage["warning"] = "配置已在当前进程生效，但无法安全加密保存；没有使用明文回退。"
                raise ValueError(self.secure_storage["warning"]) from None
        elif self.secure_storage["saved"]:
            message += " 先前加密保存的配置仍保留，重启时可能恢复先前配置。"
        return self.scrub({"configured": bool(new_key), "model": model, "base_url": base_url, "connection": self.model_connection, "secure_storage": self.secure_storage, "embedding": {"configured": bool(self.embedding_settings["api_key"] and self.embedding_settings["base_url"]), "model": self.embedding_settings["model"]}, "message": message})

    def check_model(self) -> dict:
        if not self.model_check_lock.acquire(blocking=False):
            raise ValueError("已有模型连接测试正在进行，请等待结果。")
        try:
            with self.lock:
                settings = dict(self.settings)
            if not settings.get("api_key"):
                raise ValueError("请先配置模型服务和 API Key，再测试连接。")
            # A deliberate explicit probe only; no repository/task contents are sent.
            result = self.scrub(check_model_connection(settings))
            connection = {**result, "status": "connected" if result.get("ok") else "failed", "checked_at": now()}
            connection["message"] = "模型连接已验证。" if result.get("ok") else (result.get("error") or {}).get("message", "模型连接测试失败。")
            with self.lock:
                if settings == self.settings:
                    self.model_connection = connection
            return connection
        finally:
            self.model_check_lock.release()

    def cached_acceptance(self) -> tuple[dict, bool]:
        """Read only the bounded, named local acceptance cache; never probe services.

        This record proves at most a previous built-in acceptance run. It is not
        attached to the selected task and cannot mark that task as verified.
        """
        names = ("mysql", "milvus", "controlled_repair", "langfuse_trace")
        public = {"available": False, "started_at": None, "checks": {name: False for name in names}, "trace_matches_current_config": False, "scope": "最近内置验收记录；不是当前任务证明，也不是实时健康检查。"}
        path = self.data_dir / "full-stack-verification.json"
        try:
            if not path.exists():
                return public, False
            if path.is_symlink() or not path.is_file() or path.resolve().parent != self.data_dir or path.stat().st_size > 131072:
                return public, False
            with path.open("rb") as source:
                raw = source.read(131073)
            if len(raw) > 131072:
                return public, False
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict) or not isinstance(data.get("checks"), dict):
                return public, False
            started = data.get("started_at")
            if not isinstance(started, str) or not re.fullmatch(r"[0-9T:+.Z-]{10,40}", started):
                return public, False
            datetime.fromisoformat(started.replace("Z", "+00:00"))
            checks = data["checks"]
            public.update(available=True, started_at=started, checks={name: isinstance(checks.get(name), dict) and checks[name].get("ok") is True for name in names})
            trace = checks.get("langfuse_trace")
            base = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
            public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
            matches = bool(base and public_key and isinstance(trace, dict) and trace.get("ok") is True and trace.get("server_read_back") is True and trace.get("base_url") == base and trace.get("project_key_sha256") == hashlib.sha256(public_key.encode()).hexdigest())
            public["trace_matches_current_config"] = matches
            return public, matches
        except (OSError, ValueError, UnicodeError):
            return public, False

    def integration_status(self) -> dict:
        # Configuration and observations are separate: no embedding/model probe is
        # sent by GET /api/system, and an SDK being enabled is not delivery proof.
        try:
            tasks = self.store.list()
            database = {"backend": self.store.backend, "status": "ready", "message": "任务数据库读取成功。"}
        except Exception:
            tasks = []
            database = {"backend": self.store.backend, "status": "unavailable", "message": "任务数据库读取失败，请检查本地服务。"}
        real_plans = [(task, task.get("plan") or {}) for task in tasks if task.get("mode") == "real" and task.get("plan")]
        retrieval_observation = next(((task, plan["retrieval"]) for task, plan in real_plans if isinstance(plan.get("retrieval"), dict)), None)
        trace_observation = next(((task, plan["tracing"]) for task, plan in real_plans if isinstance(plan.get("tracing"), dict)), None)
        milvus_configured = bool(os.getenv("REPOLAUNCH_MILVUS_URI", "").strip())
        retrieval = {"configured_backend": "milvus" if milvus_configured else "lexical", "status": "configured" if milvus_configured else "local", "last_backend": None, "checked_at": None, "message": "已配置 Milvus；尚无真实任务检索记录。" if milvus_configured else "使用本地关键词检索，未配置 Milvus。"}
        if retrieval_observation:
            task, observation = retrieval_observation
            retrieval.update(last_backend=observation.get("backend"), checked_at=task.get("updated_at"), status="observed_success" if observation.get("backend") == "milvus" else "fallback" if milvus_configured else "local", message=observation.get("warning") or ("最近真实任务完成 Milvus 检索。" if observation.get("backend") == "milvus" else "最近真实任务使用本地关键词检索。"))
        trace_configured = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
        tracing = {"configured": trace_configured, "status": "configured" if trace_configured else "disabled", "last_status": None, "checked_at": None, "delivery_verified": False, "message": "已配置 Langfuse，尚无真实任务追踪记录。" if trace_configured else "未配置 Langfuse，使用本地任务日志。"}
        if trace_observation:
            task, observation = trace_observation
            tracing.update(last_status=observation.get("status"), checked_at=task.get("updated_at"), message="最近真实任务启用了 Langfuse SDK；远端送达需在 Langfuse 中核对。" if observation.get("status") == "enabled" else "最近真实任务未启用外部追踪，请检查任务警告。")
        acceptance, trace_proof_matches = self.cached_acceptance()
        if trace_configured and trace_proof_matches:
            tracing.update(delivery_verified=True, delivery_verified_at=acceptance["started_at"], delivery_scope="latest_builtin_acceptance", message="最近内置验收追踪已服务端确认（非所有任务）；记录时间：" + acceptance["started_at"])
        from .knowledge import retrieval_status
        vector = retrieval_status(probe=False)
        if vector.get("checked_at"):
            retrieval.update(status=vector.get("status"), last_backend=vector.get("backend"), checked_at=vector.get("checked_at"), message=vector.get("message"))
        return self.scrub({"database": self.store.backend, "database_status": database, "retrieval": retrieval["configured_backend"], "retrieval_status": retrieval, "milvus_status": vector, "langfuse": tracing, "acceptance": acceptance, "langgraph": True})

    def record_model_result(self, settings: dict, plan: dict) -> None:
        if not settings.get("api_key") or plan.get("source") != "llm":
            return
        usage = self.scrub(plan.get("model_usage") or {})
        with self.lock:
            if settings == self.settings:
                self.model_connection = {"status": "connected", "checked_at": now(), "message": "最近一次模型规划请求成功。", "model": settings["model"], "provider_host": urlsplit(settings["base_url"]).hostname, "usage": usage}

    def emit(self, task_id: str, level: str, message: str) -> None:
        def change(task):
            events = task.setdefault("events", [])
            events.append({"id": (events[-1]["id"] + 1) if events else 1, "time": now(), "level": level, "message": self.redact(message)[-4000:]})
            task["events"] = events[-500:]
        self.store.mutate(task_id, change)

    def update(self, task_id: str, **fields) -> dict:
        return self.store.mutate(task_id, lambda task: task.update(self.scrub(fields)))

    def create(self, url: str, mode: str) -> dict:
        if mode not in {"real", "demo", "example"}:
            raise ValueError("mode必须是real、demo或example。")
        if mode == "example":
            url = "builtin://docker-check"
            display_name = "内置 Docker 验证"
        else:
            owner, name = engine.validate_repository_url(url)
            display_name = f"{owner}/{name}"
        with self.lock:
            if sum(x["status"] in ACTIVE for x in self.store.list()) >= 3:
                raise ValueError("同时最多处理3个任务，请等待或取消现有任务。")
            task_id = uuid.uuid4().hex[:16]
            task = {"id": task_id, "repository_url": url, "name": display_name, "mode": mode, "status": "analyzing", "created_at": now(), "updated_at": now(), "plan": None, "analysis": None, "result": None, "error": None, "events": [], "attempt": 0}
            self.store.create(task)
            self.cancel_flags[task_id] = threading.Event()
            self.pool.submit(self.analyze, task_id)
            return task

    def check_cancel(self, task_id: str) -> None:
        if self.cancel_flags[task_id].is_set():
            raise InterruptedError("任务已取消。")

    def analyze(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
            emit = lambda level, message: self.emit(task_id, level, message)
            task_dir = self.data_dir / "tasks" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            if task["mode"] == "example":
                emit("info", "内置 Docker 验证：将真实构建容器、启动服务并检查 HTTP；不是模拟，不下载 GitHub 仓库。")
                fixture_dir = Path(__file__).parent / "fixtures" / "docker-check"
                repo_dir = task_dir / "repo"
                shutil.copytree(fixture_dir, repo_dir, dirs_exist_ok=True)
                analysis = {"repo_url": "builtin://docker-check", "origin": "builtin", "commit": "bundled-example", "repo_dir": str(repo_dir), "readme": (repo_dir / "README.md").read_text(encoding="utf-8-sig"), "files": {p.name: p.read_text(encoding="utf-8-sig") for p in repo_dir.iterdir() if p.is_file()}, "warnings": ["首次运行可能需要下载 Python 基础镜像。"]}
                plan = engine.validate_plan({"python_version": "3.11", "install_mode": "none", "requirements_file": "", "start_kind": "python", "entrypoint": "main.py", "port": 8000, "verify_path": "/health", "extra_packages": [], "needs_env": [], "summary": "内置样例真实 Docker 构建、启动和 HTTP 检查。不是模拟，不下载 GitHub 仓库。", "warnings": analysis["warnings"], "evidence": ["随 RepoLaunch 发布的 Python 标准库 HTTPServer 样例，不安装任何 PyPI 依赖。", "GET /health 返回 JSON：status=ok；服务监听容器内 8000 端口。"]}, repo_dir)
                plan["source"] = "builtin"
                with self.lock:
                    self.check_cancel(task_id)
                    self.update(task_id, analysis=analysis, status="awaiting_approval", plan=plan, error=None)
                emit("success", "已准备内置验证方案。确认后才会真实构建并运行 Docker 容器。")
                return
            if task["mode"] == "demo":
                emit("info", "演示模式：使用内置样例，不下载外部仓库、不运行Docker。")
                repo_dir = Path(__file__).parent / "fixtures"
                analysis = {"repo_url": task["repository_url"], "commit": "demo-fixture", "repo_dir": str(repo_dir), "readme": (repo_dir / "README.md").read_text(encoding="utf-8-sig"), "files": {p.name: p.read_text(encoding="utf-8-sig") for p in repo_dir.iterdir() if p.is_file()}, "warnings": ["此任务为演示；执行日志和运行结果均为模拟。"]}
                settings = {}
            else:
                emit("info", "正在获取公开仓库的固定版本并读取项目资料。")
                analysis = engine.inspect_repository(task["repository_url"], task_dir, self.cancel_flags[task_id], emit)
                settings = dict(self.settings)
            self.check_cancel(task_id)
            self.update(task_id, analysis=analysis)
            emit("info", "正在检索诊断知识并通过LangGraph生成受限的运行方案。")
            with self.lock:
                self._model_task_settings[task_id] = settings
            plan = Planner(settings).plan(analysis)
            self.record_model_result(settings, plan)
            with self.lock:
                self._model_task_settings.pop(task_id, None)
            with self.lock:
                self.check_cancel(task_id)
                self.update(task_id, status="awaiting_approval", plan=plan, error=None)
            emit("success", "方案已生成。请检查依赖、启动方式和验证路径，确认后才会执行。")
        except Exception as exc:
            self.finish_error(task_id, exc)

    def approve(self, task_id: str, plan: dict | None = None) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] != "awaiting_approval":
                raise ValueError("只有待确认的方案可以执行，不能重复提交。")
            editable = {"python_version", "install_mode", "requirements_file", "start_kind", "entrypoint", "port", "verify_path", "extra_packages"}
            proposed = task["plan"] if task["mode"] == "example" else {**task["plan"], **{k: v for k, v in (plan or {}).items() if k in editable}}
            normalized = engine.validate_plan(proposed, Path(task["analysis"]["repo_dir"]))
            normalized["needs_env"] = sorted(set(normalized.get("needs_env", [])) | set(task["plan"].get("needs_env", [])))
            if normalized.get("needs_env"):
                raise ValueError("首版不向目标项目注入密钥。此项目需要外部环境变量，请更换无需密钥的仓库。")
            if task["mode"] != "demo":
                docker = engine.docker_status()
                if not docker.get("available"):
                    detail = docker.get("message") or docker.get("reason") or "Docker 引擎暂不可用。"
                    raise ValueError(f"{detail} 现有分析结果已保留。")
            self.cancel_flags[task_id] = threading.Event()
            task = self.update(task_id, status="running", plan={**task["plan"], **normalized}, error=None)
            self.pool.submit(self.execute, task_id)
            return task

    def execute(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
            emit = lambda level, message: self.emit(task_id, level, message)
            if task["mode"] == "demo":
                for message in ["演示：创建隔离运行环境。", "演示：安装样例依赖。", "演示：启动应用并检查HTTP响应。", "演示：验证样例接口；以下结果不代表真实Docker执行。"]:
                    self.check_cancel(task_id)
                    emit("info", message)
                    time.sleep(0.35)
                result = {"simulated": True, "url": None, "container_id": None, "verification": {"passed": True, "scope": "simulated", "message": "演示流程完成；未实际启动目标应用。"}, "dockerfile": "", "logs": "DEMO: no commands executed."}
            else:
                result = engine.run_project(task_id, Path(task["analysis"]["repo_dir"]), task["plan"], emit, self.cancel_flags[task_id])
            if self.cancel_flags[task_id].is_set():
                if result.get("container_id"):
                    engine.stop_project(result["container_id"])
                raise InterruptedError("任务已取消。")
            passed = bool(result.get("verification", {}).get("passed"))
            with self.lock:
                if self.cancel_flags[task_id].is_set():
                    if result.get("container_id"):
                        engine.stop_project(result["container_id"])
                    raise InterruptedError("任务已取消。")
                self.update(task_id, result=result, status="succeeded" if passed else "failed", error=None if passed else "HTTP验证未通过，请查看日志并提出修复。")
            emit("success" if passed else "error", "演示流程完成。" if task["mode"] == "demo" else ("HTTP就绪检查通过；请打开应用验证业务功能。" if passed else "服务未通过验证；不会将启动失败标记为成功。"))
        except Exception as exc:
            self.finish_error(task_id, exc)

    def finish_error(self, task_id: str, exc: Exception) -> None:
        cancelled = isinstance(exc, InterruptedError) or self.cancel_flags.get(task_id, threading.Event()).is_set()
        message = self.redact(str(exc))[:4000]
        with self.lock:
            request_settings = self._model_task_settings.pop(task_id, None)
            if getattr(exc, "code", None) and request_settings and request_settings.get("api_key") and request_settings == self.settings:
                self.model_connection = {"status": "failed", "checked_at": now(), "message": message, "error": {"code": getattr(exc, "code", None), "message": message, "retryable": bool(getattr(exc, "retryable", False))}}
        self.update(task_id, status="cancelled" if cancelled else "failed", error=message, error_code=getattr(exc, "code", None), error_retryable=getattr(exc, "retryable", None))
        self.emit(task_id, "warning" if cancelled else "error", message)

    def cancel(self, task_id: str) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] not in ACTIVE | {"awaiting_approval"}:
                raise ValueError("该任务当前无法取消。")
            self.cancel_flags.setdefault(task_id, threading.Event()).set()
            if task["status"] == "awaiting_approval":
                return self.update(task_id, status="cancelled")
            self.emit(task_id, "warning", "已请求取消，正在终止任务并清理运行资源。")
            return self.store.get(task_id)

    def repair(self, task_id: str) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["mode"] == "demo":
                raise ValueError("演示模式不执行真实修复。")
            if task["mode"] == "example":
                raise ValueError("内置样例用于 Docker 环境验证，不调用模型修改固定方案。请根据日志处理 Docker 环境后新建验证任务。")
            if task["status"] != "failed" or not task.get("plan"):
                raise ValueError("只有已有方案的失败任务可以提出修复。")
            if task["attempt"] >= MAX_REPAIRS:
                raise ValueError("已达到2次修复上限，请根据日志人工检查。")
            if task["mode"] != "demo":
                engine.stop_project((task.get("result") or {}).get("container_id") or "repolaunch-" + task_id)
            self.cancel_flags[task_id] = threading.Event()
            task = self.update(task_id, status="repairing", attempt=task["attempt"] + 1)
            self.pool.submit(self.propose_repair, task_id)
            return task

    def propose_repair(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
            self.emit(task_id, "info", "正在分析失败证据；修复方案需要再次确认。")
            logs = "\n".join(e["message"] for e in task["events"][-100:])
            settings = dict(self.settings)
            with self.lock:
                self._model_task_settings[task_id] = settings
            plan = Planner(settings).repair(task["analysis"], task["plan"], logs)
            self.record_model_result(settings, plan)
            with self.lock:
                self._model_task_settings.pop(task_id, None)
            with self.lock:
                self.check_cancel(task_id)
                self.update(task_id, plan=plan, status="awaiting_approval", error=None, result=None)
            self.emit(task_id, "success", "修复方案已生成，请核对变更后再次确认。")
        except Exception as exc:
            self.finish_error(task_id, exc)

    def stop(self, task_id: str) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] not in {"succeeded", "failed"}:
                raise ValueError("当前没有可以停止的已完成任务。")
            result = task.get("result") or {}
            if task["mode"] != "demo":
                engine.stop_project(result.get("container_id") or "repolaunch-" + task_id)
            self.emit(task_id, "info", "演示任务已关闭。" if task["mode"] == "demo" else "已停止并移除目标容器。")
            return self.update(task_id, status="stopped", result={**result, "url": None})

    def export(self, task_id: str) -> bytes:
        task = self.scrub(self.store.get(task_id))
        if not task.get("plan"):
            raise ValueError("方案生成后才可导出。")
        public = {k: task.get(k) for k in ["id", "repository_url", "mode", "status", "created_at", "plan", "result", "attempt", "error", "error_code", "error_retryable", "events"]}
        public["commit"] = (task.get("analysis") or {}).get("commit")
        content = self.redact(json.dumps(public, ensure_ascii=False, indent=2))
        sha = public["commit"]
        pinned_url = ""
        if task["mode"] == "real":
            owner, name = engine.validate_repository_url(task["repository_url"])
            pinned_url = f"https://codeload.github.com/{owner}/{name}/zip/{sha}" if sha and re.fullmatch(r"[a-f0-9]{40}", sha) else ""
        bundled_source = "内置 Docker 验证样例，非 GitHub 仓库" if task["mode"] == "example" else "演示内置样例，非真实仓库提交"
        reproduce_source = "内置验证任务请使用源码中 repolaunch/fixtures/docker-check。" if task["mode"] == "example" else "演示任务请使用源码中 repolaunch/fixtures。"
        plan = engine.validate_plan(task["plan"], Path(task["analysis"]["repo_dir"]))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", content)
            archive.writestr("plan.json", json.dumps(task["plan"], ensure_ascii=False, indent=2))
            instructions = f"""# RepoLaunch 复现包

仓库：{task['repository_url']}
固定版本：{sha}
固定版本源码下载：{pinned_url or bundled_source}
模式：{task['mode']}

## 复现步骤

1. 下载上述固定提交的源码并解压到一个干净目录。{reproduce_source}
2. 检查 plan.json、Dockerfile 和 report.json，确认仓库及依赖可信。
3. 将本复现包的 Dockerfile、.dockerignore 放入源码目录，覆盖原文件前自行备份。
4. 在该目录执行：

```sh
docker build --tag repolaunch-repro .
docker run --detach --name repolaunch-repro --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 1 --publish 127.0.0.1:18080:{plan['port']} repolaunch-repro
```

5. 打开 http://127.0.0.1:18080{plan['verify_path']} 检查 HTTP 响应，再自行验证业务功能。
6. 停止时执行 docker rm --force repolaunch-repro。

注意：仓库提交已固定，但基础镜像标签和未固定版本的 Python 依赖仍可能变化，未声称字节级环境复现。演示任务的运行结果为模拟；真实结果仅验证 HTTP 就绪。依赖安装及目标应用可访问网络；Docker 不是运行恶意代码的安全承诺。
"""
            archive.writestr("REPRODUCE.md", instructions)
            archive.writestr("Dockerfile", engine.render_dockerfile(plan))
            archive.writestr(".dockerignore", ".git\n.env\n.env.*\n*.pem\n*.key\n.venv\n__pycache__\n")
        return buffer.getvalue()

    def close(self):
        for flag in self.cancel_flags.values():
            flag.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()


def create_app(data_dir: Path | None = None, *, load_environment: bool | None = None) -> FastAPI:
    # Explicit data_dir is an isolated app/test instance; never import a user's
    # project credentials or DATABASE_URL unless explicitly requested.
    if load_environment is None:
        load_environment = data_dir is None
    if load_environment:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    manager = TaskManager(data_dir or Path(os.getenv("REPOLAUNCH_DATA_DIR", str(ROOT / ".repolaunch"))), os.getenv("DATABASE_URL", ""))

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(title="RepoLaunch", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return Response("仅允许本机访问。", status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname != host or parsed.port != request.url.port:
                return Response("不允许跨站请求。", status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("x-repolaunch") != "1":
            return Response("缺少本地请求标识。", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return Response(json.dumps({"detail": "任务不存在。"}, ensure_ascii=False), status_code=404, media_type="application/json")

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return Response(json.dumps({"detail": manager.redact(str(exc))}, ensure_ascii=False), status_code=400, media_type="application/json")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic input/context may contain credentials from an invalid request body.
        errors = [{key: item[key] for key in ("type", "loc", "msg") if key in item} for item in exc.errors()]
        return Response(json.dumps({"detail": manager.scrub(errors)}, ensure_ascii=False), status_code=422, media_type="application/json")

    @app.get("/api/health")
    def health():
        return {"application": "repolaunch", "version": __version__, "ready": True}

    @app.get("/api/system")
    def system():
        return manager.scrub({"version": __version__, "docker": engine.docker_status(), "model": {"configured": bool(manager.settings["api_key"]), "model": manager.settings["model"], "base_url": manager.settings["base_url"], "mode": "llm" if manager.settings["api_key"] else "rules", "connection": manager.model_connection, "secure_storage": manager.secure_storage, "embedding": {"configured": bool(manager.embedding_settings["api_key"] and manager.embedding_settings["base_url"]), "model": manager.embedding_settings["model"]}}, "integrations": manager.integration_status(), "demo_available": True, "example_available": True})

    @app.get("/api/tasks")
    def list_tasks():
        return manager.scrub(manager.store.list())

    @app.post("/api/tasks", status_code=201)
    def create_task(body: CreateTask):
        return manager.create(body.repository_url.strip(), body.mode)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        return manager.scrub(manager.store.get(task_id))

    @app.post("/api/tasks/{task_id}/approve")
    def approve_task(task_id: str, body: Approval):
        return manager.approve(task_id, body.plan)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        return manager.cancel(task_id)

    @app.post("/api/tasks/{task_id}/repair")
    def repair_task(task_id: str):
        return manager.repair(task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str):
        return manager.stop(task_id)

    @app.get("/api/tasks/{task_id}/export")
    def export_task(task_id: str):
        data = manager.export(task_id)
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="repolaunch-{task_id}.zip"'})

    @app.put("/api/settings")
    @app.post("/api/settings")
    def settings(body: ModelSettings):
        return manager.configure_model(body.base_url, body.model, body.api_key, body.remember, body.use_for_embeddings, body.embedding_model)

    @app.post("/api/settings/check")
    def check_settings():
        return manager.check_model()

    @app.get("/docs", response_class=HTMLResponse)
    def docs():
        return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>RepoLaunch API</title><link rel="stylesheet" href="/static/styles.css"><body><main><h1>RepoLaunch 本地API</h1><p>所有写请求必须包含 X-RepoLaunch: 1 请求头。接口只接受同源本机请求。</p><p><a href="/openapi.json">查看完整 OpenAPI 定义</a> · <a href="/">返回工作空间</a></p><ul><li>GET /api/system — 环境状态</li><li>GET /api/tasks — 任务历史</li><li>POST /api/tasks — repository_url、mode</li><li>GET /api/tasks/{id} — 任务详情与日志</li><li>POST /api/tasks/{id}/approve — 确认方案，JSON: {plan: ...}</li><li>POST /api/tasks/{id}/cancel — 取消任务</li><li>POST /api/tasks/{id}/repair — 提出修复，需再次确认</li><li>POST /api/tasks/{id}/stop — 停止容器</li><li>GET /api/tasks/{id}/export — 导出报告</li><li>PUT /api/settings — 配置模型（只存内存；空密钥保留原配置）</li><li>POST /api/settings/check — 显式测试已保存的模型配置，产生一次小请求</li></ul></main></body></html>"""

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app
