"""Bounded repository acquisition and Docker-only execution.

Docker is not a hardened hostile-code VM; use trusted public repositories.
Builds and applications can access the network.
"""
from __future__ import annotations
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

Emit = Callable[[str, str], None]
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000
MAX_LOG_CHARS = 80_000
BUILD_TIMEOUT = 600
START_TIMEOUT = 60
VERIFY_TIMEOUT = 45
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}(?:==[A-Za-z0-9][A-Za-z0-9_.+!-]{0,59})?\Z")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)
_RESOURCE = re.compile(r"repolaunch-[a-z0-9-]{1,64}\Z")


class TaskCancelled(RuntimeError):
    pass


class GitHubRateLimit(RuntimeError):
    pass


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise TaskCancelled("任务已取消")


def validate_repository_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 240 or url != url.strip():
        raise ValueError("请输入完整的 GitHub HTTPS 仓库地址")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("仅支持 https://github.com/owner/repository，不支持其他域名、参数或片段")
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) != 3 or parts[0] != "":
        raise ValueError("请填写仓库首页地址，不要包含 tree、blob 或其他路径")
    owner, name = parts[1:]
    if name.endswith(".git"):
        name = name[:-4]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        raise ValueError("GitHub 用户或组织名称不合法")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("GitHub 仓库名称不合法")
    return owner, name


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("请求发生重定向；请使用正式仓库地址或不重定向的验证路径")


def _download(url: str, limit: int, cancel: threading.Event) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "RepoLaunch/0.1", "Accept": "application/vnd.github+json"})
    chunks, length, deadline = [], 0, time.monotonic() + 90
    try:
        with opener.open(request, timeout=15) as response:
            while True:
                _check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise RuntimeError("下载仓库超时")
                chunk = response.read(min(65536, limit + 1 - length))
                if not chunk:
                    break
                length += len(chunk)
                if length > limit:
                    raise ValueError("仓库超过首版允许大小，请选择较小的 Python 项目")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        messages = {404: "找不到公开仓库，私有仓库暂不支持", 403: "GitHub 限制了请求频率，请稍后重试", 429: "GitHub 请求过于频繁"}
        error_class = GitHubRateLimit if exc.code in {403, 429} else RuntimeError
        raise error_class(messages.get(exc.code, f"GitHub 请求失败：HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("无法连接 GitHub，请检查网络连接") from exc
    return b"".join(chunks)


def _extract_archive(archive: Path, destination: Path, cancel: threading.Event) -> None:
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > MAX_ARCHIVE_FILES or sum(x.file_size for x in entries) > MAX_EXPANDED_BYTES:
            raise ValueError("仓库文件数量或解压大小超过限制")
        roots = set()
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if not parts or entry.filename.startswith("/") or "\\" in entry.filename or any(p in {".", ".."} or ":" in p for p in parts):
                raise ValueError("仓库压缩包包含不安全路径")
            roots.add(parts[0])
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("暂不支持包含符号链接或特殊文件的仓库")
            if entry.file_size > 16 * 1024 * 1024:
                raise ValueError("仓库存在超过 16MB 的文件")
        if len(roots) != 1:
            raise ValueError("仓库压缩包结构不合法")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            _check_cancel(cancel)
            parts = PurePosixPath(entry.filename).parts[1:]
            if not parts:
                continue
            target = destination.joinpath(*parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("仓库文件超出了项目目录")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 65536)


def _resolve_git_head(url: str, cancel: threading.Event, emit: Emit) -> str:
    """Read HEAD via HTTPS when unauthenticated GitHub API is rate limited."""
    if not shutil.which("git"):
        raise RuntimeError("GitHub API请求受限，且未找到Git。请安装Git或稍后重试。")
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
        and key.upper() not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "REPOLAUNCH_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"}
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    argv = ["git", "-c", "credential.helper=", "-c", "credential.interactive=false",
            "-c", "core.hooksPath=" + os.devnull, "-c", "http.followRedirects=false",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "ls-remote", "--exit-code", url, "HEAD"]
    raw = _command(argv, emit, cancel, 30, environment=environment)
    match = re.fullmatch(r"([a-f0-9]{40})\s+HEAD\s*", raw.strip())
    if not match:
        raise RuntimeError("Git未返回有效的HEAD提交，无法固定仓库版本。")
    return match.group(1)


def inspect_repository(url: str, task_dir: Path, cancel: threading.Event, emit: Emit) -> dict:
    owner, name = validate_repository_url(url)
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    emit("info", "读取公开仓库并固定提交版本")
    try:
        info = json.loads(_download(f"https://api.github.com/repos/{owner}/{name}/commits/HEAD", 512_000, cancel))
        commit = info.get("sha", "")
    except GitHubRateLimit:
        emit("warning", "GitHub API请求受限，改用只读Git查询固定提交；不会下载或执行Git钩子。")
        commit = _resolve_git_head(f"https://github.com/{owner}/{name}", cancel, emit)
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise RuntimeError("GitHub 未返回有效提交，无法固定版本")
    archive = task_dir / "source.zip"
    archive.write_bytes(_download(f"https://codeload.github.com/{owner}/{name}/zip/{commit}", MAX_ARCHIVE_BYTES, cancel))
    repo_dir = task_dir / "repo"
    _extract_archive(archive, repo_dir, cancel)
    archive.unlink(missing_ok=True)
    files, total_chars = {}, 0
    candidates = sorted((p for p in repo_dir.rglob("*") if p.is_file()), key=lambda p: (len(p.relative_to(repo_dir).parts), str(p)))
    for path in candidates:
        relative = path.relative_to(repo_dir).as_posix()
        depth = len(path.relative_to(repo_dir).parts)
        if depth > 3 or path.name.startswith(".") or ".git" in path.parts:
            continue
        useful = (path.name.lower().startswith("readme") or path.name in {"pyproject.toml", "setup.cfg", "setup.py", "Pipfile"} or (path.name.startswith("requirements") and path.suffix == ".txt") or (path.suffix == ".py" and depth <= 2))
        if not useful or len(files) >= 35 or total_chars >= 160_000:
            continue
        content = path.read_bytes()[:18_000].decode("utf-8", errors="replace")
        files[relative] = content
        total_chars += len(content)
    readme = next((text for name, text in files.items() if name.lower().startswith("readme")), "")
    warnings = ["仅支持受信任的 CPU Python 项目；依赖安装和应用运行可能访问网络。"]
    if not any(name.endswith(".py") or name == "pyproject.toml" for name in files):
        warnings.append("未发现可识别的 Python 项目入口。")
    emit("info", f"已读取 {len(files)} 个关键文件，提交 {commit[:12]}")
    return {"repo_url": f"https://github.com/{owner}/{name}", "commit": commit, "repo_dir": str(repo_dir), "readme": readme, "files": files, "warnings": warnings}


def resolve_docker_path() -> str | None:
    """Resolve on each call so a newly installed Docker needs no PATH refresh.

    REPOLAUNCH_DOCKER_PATH is authoritative; invalid overrides fail closed.
    """
    override = os.getenv("REPOLAUNCH_DOCKER_PATH", "").strip().strip('"')
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    on_path = shutil.which("docker")
    if on_path:
        return str(Path(on_path).resolve())
    candidates = [
        Path(__file__).resolve().parents[2] / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
    ]
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
    candidates.append(Path.home() / "AppData" / "Local" / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
    return next((str(path.resolve()) for path in candidates if path.is_file()), None)


def _docker_argv(*arguments: str) -> list[str]:
    executable = resolve_docker_path()
    if not executable:
        raise FileNotFoundError("未找到 Docker CLI，请检查安装位置或 REPOLAUNCH_DOCKER_PATH。")
    return [executable, *arguments]


def _docker_failure_status(executable: str, error: str) -> dict:
    sanitized = re.sub(r"[\x00-\x1f\x7f]", " ", str(error))
    sanitized = re.sub(r"(?i)(api[_-]?key|authorization|token|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", sanitized)
    sanitized = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", sanitized)
    for key in ("REPOLAUNCH_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.getenv(key, "")
        if value:
            sanitized = sanitized.replace(value, "[REDACTED]")
    denied = any(text in sanitized.casefold() for text in ("permission denied", "access denied", "access is denied", "拒绝访问", "访问被拒绝", "e_accessdenied"))
    return {
        "available": False,
        "reason": "access_denied" if denied else "daemon_unavailable",
        "message": ("Docker引擎访问被拒绝。请从普通Windows会话运行RepoLaunch，并确认当前Windows账户可以访问Docker Desktop；不需要重新安装Docker。"
                    if denied else "Docker已安装，但引擎未连接。请确认Docker Desktop已启动并处于Running状态。"),
        "cli_path": executable,
        "engine_error": sanitized[-600:],
    }


def docker_status() -> dict:
    executable = resolve_docker_path()
    if not executable:
        message = "未找到Docker。请安装Docker Desktop并启动引擎；当前仍可使用演示和仓库分析。"
        if os.getenv("REPOLAUNCH_DOCKER_PATH"):
            message = "REPOLAUNCH_DOCKER_PATH指定的文件不存在，请检查该路径。"
        return {"available": False, "reason": "cli_missing", "message": message, "cli_path": None, "engine_error": ""}
    try:
        result = subprocess.run([executable, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace")
        if result.returncode:
            return _docker_failure_status(executable, result.stderr or result.stdout)
        return {"available": True, "reason": "ready", "message": "Docker引擎已就绪", "version": result.stdout.strip(), "cli_path": executable, "engine_error": ""}
    except PermissionError as exc:
        return _docker_failure_status(executable, "Access denied: " + str(exc))
    except subprocess.TimeoutExpired:
        return _docker_failure_status(executable, "Docker engine status check timed out after 8 seconds.")
    except OSError as exc:
        return _docker_failure_status(executable, str(exc))


def _safe_file(repo_dir: Path, value: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 180 or "\\" in value or ":" in value or value.startswith("/"):
        raise ValueError("项目文件路径不合法")
    parts = PurePosixPath(value).parts
    if any(p in {".", ".."} or p.startswith("-") for p in parts):
        raise ValueError("项目文件路径不合法")
    path = repo_dir.joinpath(*parts)
    if path.is_symlink() or not path.resolve().is_relative_to(repo_dir.resolve()) or not path.is_file() or (suffix and path.suffix != suffix):
        raise ValueError(f"项目文件不存在或超出允许范围：{value}")
    current = path.parent
    while current != repo_dir and current.is_relative_to(repo_dir):
        if current.is_symlink():
            raise ValueError("不允许通过符号链接引用文件")
        current = current.parent
    return PurePosixPath(value).as_posix()


def validate_plan(plan: dict, repo_dir: Path) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("运行方案格式不合法")
    normalized = dict(plan)
    version = str(plan.get("python_version", "3.11"))
    if version not in {"3.10", "3.11", "3.12", "3.13"}:
        raise ValueError("仅支持 Python 3.10—3.13")
    mode = plan.get("install_mode", "none")
    if mode not in {"none", "requirements", "pyproject"}:
        raise ValueError("不支持该依赖安装方式")
    requirements = ""
    if mode == "requirements":
        requirements = _safe_file(Path(repo_dir), plan.get("requirements_file", "requirements.txt"), ".txt")
    elif mode == "pyproject":
        _safe_file(Path(repo_dir), "pyproject.toml")
    kind, entry = plan.get("start_kind"), plan.get("entrypoint", "")
    if kind == "uvicorn":
        parts = entry.split(":") if isinstance(entry, str) else []
        if len(parts) != 2 or not _MODULE.fullmatch(parts[0]) or not re.fullmatch(r"[A-Za-z_]\w*", parts[1], re.ASCII):
            raise ValueError("Uvicorn 入口必须为 module:app 格式")
    elif kind == "flask":
        if not isinstance(entry, str) or not _MODULE.fullmatch(entry):
            raise ValueError("Flask 入口必须为有效 Python 模块名")
    elif kind == "python":
        entry = _safe_file(Path(repo_dir), entry, ".py")
    else:
        raise ValueError("首版仅支持 Uvicorn、Flask 或 Python 文件入口")
    port = plan.get("port", 8000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("应用端口必须为 1024—65535 的整数")
    verify_path = plan.get("verify_path", "/")
    if not isinstance(verify_path, str) or len(verify_path) > 200 or not re.fullmatch(r"/[A-Za-z0-9_./~-]*", verify_path) or verify_path.startswith("//") or ".." in verify_path.split("/"):
        raise ValueError("验证路径必须为当前应用内的绝对路径，例如 /health")
    packages = plan.get("extra_packages", [])
    if not isinstance(packages, list) or len(packages) > 12 or any(not isinstance(p, str) or not _PACKAGE.fullmatch(p) for p in packages):
        raise ValueError("附加依赖仅支持普通 PyPI 包名或固定版本，不支持 URL、路径或安装选项")
    env = plan.get("needs_env", [])
    if not isinstance(env, list) or len(env) > 30 or any(not isinstance(k, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,79}", k) for k in env):
        raise ValueError("环境变量需求格式不合法")
    normalized.update(python_version=version, install_mode=mode, requirements_file=requirements, start_kind=kind, entrypoint=entry, port=port, verify_path=verify_path, extra_packages=list(dict.fromkeys(packages)), needs_env=env)
    return normalized


def _start_argv(plan: dict) -> list[str]:
    if plan["start_kind"] == "uvicorn":
        return ["python", "-m", "uvicorn", plan["entrypoint"], "--host", "0.0.0.0", "--port", str(plan["port"])]
    if plan["start_kind"] == "flask":
        return ["python", "-m", "flask", "--app", plan["entrypoint"], "run", "--host", "0.0.0.0", "--port", str(plan["port"])]
    return ["python", plan["entrypoint"]]


def _dockerfile(plan: dict) -> str:
    lines = [f"FROM python:{plan['python_version']}-slim", "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1", "RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app", "WORKDIR /app", "COPY --chown=10001:10001 . /app", "USER 10001:10001", "ENV PATH=/home/app/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin", "ENV HOME=/home/app", "ENV HOST=0.0.0.0", f"ENV PORT={plan['port']}"]
    if plan["install_mode"] == "requirements":
        lines.append("RUN " + json.dumps(["python", "-m", "pip", "install", "--no-cache-dir", "--user", "-r", plan["requirements_file"]]))
    elif plan["install_mode"] == "pyproject":
        lines.append('RUN ["python", "-m", "pip", "install", "--no-cache-dir", "--user", "."]')
    if plan["extra_packages"]:
        lines.append("RUN " + json.dumps(["python", "-m", "pip", "install", "--no-cache-dir", "--user", *plan["extra_packages"]]))
    lines.extend([f"EXPOSE {plan['port']}", "CMD " + json.dumps(_start_argv(plan))])
    return "\n".join(lines) + "\n"


def render_dockerfile(plan: dict) -> str:
    """Render a plan already validated against its repository by validate_plan."""
    return _dockerfile(plan)


def _command(argv: list[str], emit: Emit, cancel: threading.Event, timeout: int, environment: dict | None = None) -> str:
    _check_cancel(cancel)
    messages = queue.Queue(maxsize=2048)
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", shell=False, env=environment if environment is not None else {k: v for k, v in os.environ.items() if k.upper() not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_API_KEY", "REPOLAUNCH_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"}})
    reader_done = threading.Event()
    def reader():
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    messages.put(line[:4000], timeout=1)
                except queue.Full:
                    pass
        except (OSError, ValueError):
            # Closing a cancelled subprocess can interrupt its reader.
            pass
        finally:
            try:
                messages.put(None, timeout=1)
            except queue.Full:
                pass
            reader_done.set()
    threading.Thread(target=reader, daemon=True).start()
    output, started = "", time.monotonic()
    try:
        while True:
            _check_cancel(cancel)
            if time.monotonic() - started > timeout:
                raise RuntimeError(f"执行超过 {timeout} 秒，已停止")
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                # Process exit does not imply its separate pipe reader drained.
                # Wait for EOF rather than dropping a fast command's final output.
                if reader_done.is_set():
                    break
                continue
            if line is None:
                break
            output = (output + line)[-MAX_LOG_CHARS:]
            emit("log", line.rstrip())
        code = process.wait(timeout=5)
        if code:
            raise RuntimeError(f"命令执行失败（退出码 {code}）\n{output[-18_000:]}")
        return output
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()


def _prepare_context(repo_dir: Path, task_id: str, plan: dict) -> tuple[Path, str]:
    context = Path(tempfile.mkdtemp(prefix="build-" + task_id + "-", dir=repo_dir.parent))
    ignore_names = {".git", ".env", ".venv", "venv", "__pycache__", "node_modules", ".ssh", ".aws", ".azure", ".gcloud"}
    def ignore(path, names):
        return [name for name in names if name in ignore_names or name.startswith(".env.") or name.endswith((".pem", ".key", ".p12", ".pfx")) or Path(path, name).is_symlink()]
    shutil.copytree(repo_dir, context, dirs_exist_ok=True, ignore=ignore, symlinks=False)
    dockerfile = _dockerfile(plan)
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (context / ".dockerignore").write_text(".git\n.env\n.env.*\n*.pem\n*.key\n.venv\n__pycache__\n", encoding="utf-8")
    return context, dockerfile


def _read_logs(name: str) -> str:
    try:
        result = subprocess.run(_docker_argv("logs", "--tail", "180", name), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        return (result.stdout + result.stderr)[-MAX_LOG_CHARS:]
    except (OSError, subprocess.TimeoutExpired):
        return "无法读取容器日志"


def run_project(task_id: str, repo_dir: Path, plan: dict, emit: Emit, cancel: threading.Event) -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,48}", task_id):
        raise ValueError("任务编号不合法")
    plan = validate_plan(plan, Path(repo_dir))
    if plan["needs_env"]:
        raise ValueError("项目需要额外环境变量，首版不向容器注入密钥：" + ", ".join(plan["needs_env"]))
    status = docker_status()
    if not status["available"]:
        raise RuntimeError(status["message"])
    context, dockerfile = _prepare_context(Path(repo_dir), task_id, plan)
    name, image = "repolaunch-" + task_id, "repolaunch-" + task_id + ":trial"
    started, keep_running = False, False
    primary_error = None
    try:
        emit("stage", "building")
        emit("info", "在独立容器环境中构建依赖，不在宿主机执行仓库命令")
        build_logs = _command(_docker_argv("build", "--pull=false", "--progress=plain", "--tag", image, str(context)), emit, cancel, BUILD_TIMEOUT)
        emit("stage", "starting")
        command = _docker_argv("run", "--detach", "--name", name, "--label", "repolaunch.managed=true", "--label", f"repolaunch.task={task_id}", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--log-driver", "local", "--log-opt", "max-size=5m", "--log-opt", "max-file=2", "--restart", "no", "--publish", f"127.0.0.1::{plan['port']}", image)
        started = True
        result = _command(command, emit, cancel, START_TIMEOUT)
        container_id = result.strip().splitlines()[-1] if result.strip() else name
        ports = _command(_docker_argv("port", name, f"{plan['port']}/tcp"), emit, cancel, 10).strip()
        match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", ports)
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise RuntimeError("未获得有效的本机容器访问端口")
        url = "http://127.0.0.1:" + match.group(1)
        emit("stage", "verifying")
        emit("info", "检查 HTTP 响应；不代表完整业务功能测试")
        verification = {"passed": False, "status_code": None, "path": plan["verify_path"], "kind": "http_readiness", "message": "服务未在规定时间内通过 HTTP 检查"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            _check_cancel(cancel)
            try:
                with opener.open(url + plan["verify_path"], timeout=2) as response:
                    verification["status_code"] = response.status
                    if 200 <= response.status < 300:
                        verification.update(passed=True, message="已通过 HTTP 就绪检查，业务功能需另行验证")
                        break
            except urllib.error.HTTPError as exc:
                verification["status_code"] = exc.code
            except (OSError, ValueError, urllib.error.URLError):
                pass
            cancel.wait(0.6)
        logs = _read_logs(name)
        if logs:
            emit("log", logs)
        keep_running = verification["passed"]
        return {"container_id": name, "docker_id": container_id, "url": url if keep_running else None, "verification": verification, "dockerfile": dockerfile, "start_command": _start_argv(plan), "logs": (build_logs + "\n" + logs)[-MAX_LOG_CHARS:], "image": image, "build_context": str(context)}
    except Exception as exc:
        primary_error = exc
        if started:
            # A process can exit before Docker exposes its published port.
            # Retain its diagnostic output before the owned container is removed.
            try:
                logs = _read_logs(name)
                if logs:
                    emit("log", logs)
            except Exception:
                pass  # Diagnostic collection must not replace the startup error.
        raise
    finally:
        if started and not keep_running:
            try:
                stop_project(name)
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                try:
                    emit("warning", "容器清理尚未确认完成：" + str(cleanup_error))
                except Exception:
                    pass


def stop_project(container_id: str) -> None:
    """Idempotently remove an existing container only after ownership verification."""
    if not isinstance(container_id, str) or not _RESOURCE.fullmatch(container_id):
        raise ValueError("只允许清理 RepoLaunch 创建的容器")
    try:
        inspection = subprocess.run(
            _docker_argv("inspect", "--type", "container", "--format", "{{json .Config.Labels}}", container_id),
            capture_output=True, timeout=10, encoding="utf-8", errors="replace",
        )
        if inspection.returncode:
            if "no such" in inspection.stderr.lower():
                return
            raise RuntimeError("无法检查目标容器，尚未确认已停止：" + inspection.stderr[-500:])
        try:
            labels = json.loads(inspection.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("目标容器的归属信息不合法，未执行删除") from exc
        expected_task = container_id.removeprefix("repolaunch-")
        if not isinstance(labels, dict) or labels.get("repolaunch.managed") != "true" or labels.get("repolaunch.task") != expected_task:
            raise ValueError("容器缺少匹配的 RepoLaunch 归属标签，未执行删除")
        result = subprocess.run(
            _docker_argv("rm", "--force", container_id),
            capture_output=True, timeout=15, encoding="utf-8", errors="replace",
        )
        if result.returncode and "no such" not in result.stderr.lower():
            raise RuntimeError("Docker 未能停止目标容器：" + result.stderr[-500:])
    except FileNotFoundError:
        return
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("停止容器超时，请在 Docker Desktop 中检查") from exc
