"""Local-only infrastructure deployment. Never prints generated credentials."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROJECT = "repolaunch-integrations"
PORTS = {"MYSQL_PORT": 13306, "MILVUS_PORT": 19530, "MILVUS_HEALTH_PORT": 19091,
         "MINIO_PORT": 19000, "MINIO_CONSOLE_PORT": 19001,
         "LANGFUSE_PORT": 30300, "LANGFUSE_WORKER_PORT": 30301}
IMAGES = {
    "mysql": "mysql:8.4.13",
    "etcd": "quay.io/coreos/etcd:v3.5.25",
    "minio": "bitnamilegacy/minio:2025.7.23-debian-12-r5",
    "milvus": "milvusdb/milvus:v2.6.11",
    "postgres": "postgres:16.15-bookworm",
    "redis": "redis:7.2.16-alpine",
    "clickhouse": "clickhouse/clickhouse-server:25.3.9.72",
    "langfuse-web": "langfuse/langfuse:3.202.0",
    "langfuse-worker": "langfuse/langfuse-worker:3.202.0",
}
DATA_SUBDIRS = ["mysql", "etcd", "minio", "milvus", "postgres", "redis", "clickhouse-data", "clickhouse-logs"]
SECRET_KEYS = ["MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD",
               "CLICKHOUSE_PASSWORD", "MINIO_ROOT_PASSWORD", "LANGFUSE_SALT", "LANGFUSE_ENCRYPTION_KEY",
               "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SECRET_KEY", "LANGFUSE_ADMIN_PASSWORD"]


def variable(name: str) -> str:
    return "$" + "{" + name + "}"


def service(image: str, memory: str, **kwargs) -> dict:
    return {"image": image, "restart": "unless-stopped", "mem_limit": memory,
            "labels": {"repolaunch.stack": "integrations"},
            "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2"}}, **kwargs}


def health(command: list[str], start: str = "20s") -> dict:
    return {"test": command, "interval": "10s", "timeout": "5s", "retries": 30, "start_period": start}


def bind(folder: str, target: str) -> dict:
    return {"type": "bind", "source": "../.repolaunch/services/" + folder, "target": target}


def port(key: str, target: int) -> str:
    return "127.0.0.1:" + variable(key) + ":" + str(target)


def build_compose() -> dict:
    v = variable
    healthy = lambda *names: {name: {"condition": "service_healthy"} for name in names}
    langfuse = {
        "DATABASE_URL": "postgresql://langfuse:" + v("POSTGRES_PASSWORD") + "@postgres:5432/langfuse",
        "SALT": v("LANGFUSE_SALT"), "ENCRYPTION_KEY": v("LANGFUSE_ENCRYPTION_KEY"),
        "NEXTAUTH_URL": "http://127.0.0.1:" + v("LANGFUSE_PORT"),
        "TELEMETRY_ENABLED": "false", "LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES": "false",
        "CLICKHOUSE_MIGRATION_URL": "clickhouse://clickhouse:9000",
        "CLICKHOUSE_URL": "http://clickhouse:8123", "CLICKHOUSE_USER": "langfuse",
        "CLICKHOUSE_PASSWORD": v("CLICKHOUSE_PASSWORD"), "CLICKHOUSE_CLUSTER_ENABLED": "false",
        "REDIS_HOST": "redis", "REDIS_PORT": "6379", "REDIS_AUTH": v("REDIS_PASSWORD"),
        "REDIS_TLS_ENABLED": "false", "LANGFUSE_S3_EVENT_UPLOAD_BUCKET": "langfuse",
        "LANGFUSE_S3_EVENT_UPLOAD_REGION": "us-east-1",
        "LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID": "repolaunch",
        "LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY": v("MINIO_ROOT_PASSWORD"),
        "LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT": "http://minio:9000",
        "LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE": "true",
        "LANGFUSE_S3_EVENT_UPLOAD_PREFIX": "events/",
        "LANGFUSE_S3_MEDIA_UPLOAD_BUCKET": "langfuse",
        "LANGFUSE_S3_MEDIA_UPLOAD_REGION": "us-east-1",
        "LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID": "repolaunch",
        "LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY": v("MINIO_ROOT_PASSWORD"),
        "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT": "http://127.0.0.1:" + v("MINIO_PORT"),
        "LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE": "true",
        "LANGFUSE_S3_MEDIA_UPLOAD_PREFIX": "media/",
        "LANGFUSE_S3_BATCH_EXPORT_ENABLED": "false",
    }
    node_health = lambda url: ["CMD", "node", "-e", "fetch('" + url + "').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
    services = {
        "mysql": service(IMAGES["mysql"], "1g",
            environment={"MYSQL_ROOT_PASSWORD": v("MYSQL_ROOT_PASSWORD"), "MYSQL_DATABASE": "repolaunch", "MYSQL_USER": "repolaunch", "MYSQL_PASSWORD": v("MYSQL_PASSWORD"), "TZ": "UTC"},
            command=["--innodb-buffer-pool-size=256M", "--max-connections=50", "--lower-case-table-names=1"],
            ports=[port("MYSQL_PORT", 3306)], volumes=[bind("mysql", "/var/lib/mysql")],
            healthcheck=health(["CMD-SHELL", "MYSQL_PWD=$$MYSQL_ROOT_PASSWORD mysqladmin ping -h127.0.0.1 -uroot --silent"], "40s")),
        "etcd": service(IMAGES["etcd"], "512m",
            environment={"ETCD_AUTO_COMPACTION_MODE": "revision", "ETCD_AUTO_COMPACTION_RETENTION": "1000", "ETCD_QUOTA_BACKEND_BYTES": "4294967296", "ETCD_SNAPSHOT_COUNT": "50000"},
            command=["etcd", "--advertise-client-urls=http://etcd:2379", "--listen-client-urls=http://0.0.0.0:2379", "--data-dir=/etcd"],
            volumes=[bind("etcd", "/etcd")], healthcheck=health(["CMD", "etcdctl", "endpoint", "health"])),
        "minio": service(IMAGES["minio"], "1g",
            environment={"MINIO_ROOT_USER": "repolaunch", "MINIO_ROOT_PASSWORD": v("MINIO_ROOT_PASSWORD")},
            entrypoint=["/bin/sh", "-c"],
            command=["mkdir -p /data/langfuse && exec /opt/bitnami/minio/bin/minio server /data --address ':9000' --console-address ':9001'"],
            ports=[port("MINIO_PORT", 9000), port("MINIO_CONSOLE_PORT", 9001)],
            volumes=[bind("minio", "/data")],
            healthcheck=health(["CMD", "/bin/bash", "-ec", "exec 3<>/dev/tcp/127.0.0.1/9000; printf 'GET /minio/health/live HTTP/1.0\r\nHost: localhost\r\n\r\n' >&3; read -r protocol status rest <&3; test \"$$status\" = 200"])),
        "milvus": service(IMAGES["milvus"], "4g",
            command=["milvus", "run", "standalone"],
            security_opt=["seccomp:unconfined"],
            environment={"ETCD_ENDPOINTS": "etcd:2379", "MINIO_ADDRESS": "minio:9000",
                         "MINIO_ACCESS_KEY_ID": "repolaunch", "MINIO_SECRET_ACCESS_KEY": v("MINIO_ROOT_PASSWORD"), "MINIO_REGION": "us-east-1"},
            volumes=[bind("milvus", "/var/lib/milvus")],
            ports=[port("MILVUS_PORT", 19530), port("MILVUS_HEALTH_PORT", 9091)],
            depends_on=healthy("etcd", "minio"),
            healthcheck=health(["CMD", "curl", "-f", "http://localhost:9091/healthz"], "90s")),
        "postgres": service(IMAGES["postgres"], "1g",
            environment={"POSTGRES_USER": "langfuse", "POSTGRES_DB": "langfuse", "POSTGRES_PASSWORD": v("POSTGRES_PASSWORD"), "PGDATA": "/var/lib/postgresql/data/pgdata", "TZ": "UTC", "PGTZ": "UTC"},
            volumes=[bind("postgres", "/var/lib/postgresql/data")],
            healthcheck=health(["CMD", "pg_isready", "-U", "langfuse", "-d", "langfuse"])),
        "redis": service(IMAGES["redis"], "512m",
            environment={"REDISCLI_AUTH": v("REDIS_PASSWORD")},
            command=["redis-server", "--requirepass", v("REDIS_PASSWORD"), "--maxmemory", "256mb", "--maxmemory-policy", "noeviction", "--appendonly", "yes"],
            volumes=[bind("redis", "/data")], healthcheck=health(["CMD", "redis-cli", "ping"])),
        "clickhouse": service(IMAGES["clickhouse"], "3g",
            user="101:101", environment={"CLICKHOUSE_DB": "default", "CLICKHOUSE_USER": "langfuse", "CLICKHOUSE_PASSWORD": v("CLICKHOUSE_PASSWORD"), "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT": "1", "TZ": "UTC"},
            volumes=[{"type": "volume", "source": "clickhouse-data", "target": "/var/lib/clickhouse"},
                     {"type": "volume", "source": "clickhouse-logs", "target": "/var/log/clickhouse-server"}],
            ulimits={"nofile": {"soft": 262144, "hard": 262144}},
            healthcheck=health(["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:8123/ping"])),
        "langfuse-worker": service(IMAGES["langfuse-worker"], "2g",
            environment={**langfuse, "HOSTNAME": "0.0.0.0"}, ports=[port("LANGFUSE_WORKER_PORT", 3030)],
            depends_on=healthy("postgres", "redis", "clickhouse", "minio"),
            healthcheck=health(node_health("http://localhost:3030/api/health"), "90s")),
        "langfuse-web": service(IMAGES["langfuse-web"], "2g",
            environment={**langfuse, "HOSTNAME": "0.0.0.0", "PORT": "3000", "NEXTAUTH_SECRET": v("LANGFUSE_NEXTAUTH_SECRET"),
                         "LANGFUSE_INIT_ORG_ID": "repolaunch-local", "LANGFUSE_INIT_ORG_NAME": "RepoLaunch Local",
                         "LANGFUSE_INIT_PROJECT_ID": "repolaunch", "LANGFUSE_INIT_PROJECT_NAME": "RepoLaunch",
                         "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": v("LANGFUSE_PUBLIC_KEY"),
                         "LANGFUSE_INIT_PROJECT_SECRET_KEY": v("LANGFUSE_SECRET_KEY"),
                         "LANGFUSE_INIT_USER_EMAIL": "admin@repolaunch.local",
                         "LANGFUSE_INIT_USER_NAME": "RepoLaunch Admin",
                         "LANGFUSE_INIT_USER_PASSWORD": v("LANGFUSE_ADMIN_PASSWORD"),
                         "AUTH_DISABLE_SIGNUP": "true"},
            ports=[port("LANGFUSE_PORT", 3000)],
            depends_on=healthy("postgres", "redis", "clickhouse", "minio"),
            healthcheck=health(node_health("http://localhost:3000/api/public/health?failIfDatabaseUnavailable=true"), "120s")),
    }
    return {"name": PROJECT, "services": services,
            "volumes": {name: {"labels": {"repolaunch.stack": "integrations", "repolaunch.store": name}}
                        for name in ["clickhouse-data", "clickhouse-logs"]}}


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    with staging.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    try:
        os.chmod(staging, 0o600)
    except OSError:
        pass
    os.replace(staging, path)


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def prepare(root: Path = ROOT) -> dict[str, str]:
    root = root.resolve()
    folder = root / ".repolaunch" / "services"
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise ValueError("服务数据目录不能通过符号链接移出项目。")
    for name in DATA_SUBDIRS:
        target = folder / name
        target.mkdir(exist_ok=True)
        if not target.resolve().is_relative_to(folder):
            raise ValueError("服务持久化目录不能移出项目。")
    env_file = folder / "services.env"
    config = read_env(env_file)
    if config:
        if any(not config.get(key) for key in SECRET_KEYS + ["LANGFUSE_PUBLIC_KEY"]):
            raise ValueError("已有服务配置不完整；为避免数据密码失配，不会自动重置凭证。请检查 services.env。")
    else:
        if any(any((folder / name).iterdir()) for name in DATA_SUBDIRS):
            raise ValueError("发现已有服务数据但缺少凭证配置。不会生成新密码覆盖，请先恢复 services.env。")
        config = {key: secrets.token_hex(24) for key in SECRET_KEYS}
        config["MINIO_ROOT_PASSWORD"] = secrets.token_hex(16)
        config["LANGFUSE_ENCRYPTION_KEY"] = secrets.token_hex(32)
        config["LANGFUSE_PUBLIC_KEY"] = "pk-lf-" + uuid.uuid4().hex
        config["LANGFUSE_SECRET_KEY"] = "sk-lf-" + uuid.uuid4().hex
        config.update({key: str(value) for key, value in PORTS.items()})
        private_write(env_file, "# Generated locally. Do not share or commit this file.\n" + "\n".join(key + "=" + value for key, value in config.items()) + "\n")
    for key, default in PORTS.items():
        value = int(config.get(key, default))
        if not 1024 <= value <= 65535:
            raise ValueError("服务端口配置不合法：" + key)
        config[key] = str(value)
    return config


def sanitized(message: str, config: dict, *, max_chars: int | None = 12000) -> str:
    text = str(message)
    for key in SECRET_KEYS + ["LANGFUSE_PUBLIC_KEY"]:
        if config.get(key):
            text = text.replace(config[key], "[REDACTED]")
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[-max_chars:] if max_chars else text


def docker_command(arguments: list[str], config: dict, timeout: int = 600, *, include_stderr: bool = False) -> str:
    from repolaunch.engine import resolve_docker_path
    docker = resolve_docker_path()
    if not docker:
        raise RuntimeError("没有找到Docker Desktop，请先安装并启动。")
    result = subprocess.run([docker, *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False)
    if result.returncode:
        raise RuntimeError(sanitized(result.stderr or result.stdout or "Docker命令失败", config))
    return result.stdout + ("\n" + result.stderr if include_stderr and result.stderr else "")


def compose_command(root: Path, arguments: list[str], config: dict, timeout: int = 600) -> str:
    return docker_command(["compose", "--project-name", PROJECT, "--file", str(root / "infra" / "compose.yaml"),
                           "--env-file", str(root / ".repolaunch" / "services" / "services.env"), *arguments], config, timeout)


def ensure_ports_free(config: dict, owned_ports: set[int] | None = None) -> None:
    owned_ports = owned_ports or set()
    for key in PORTS:
        value = int(config[key])
        if value in owned_ports:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", value))
            except OSError as exc:
                raise ValueError(f"本机端口 {value}（{key}）已被其他程序占用；未停止任何程序。请在 services.env 中更换该端口后重试。") from exc


def app_configuration(config: dict) -> dict[str, str]:
    return {
        "DATABASE_URL": "mysql+pymysql://repolaunch:" + quote(config["MYSQL_PASSWORD"], safe="") + "@127.0.0.1:" + config["MYSQL_PORT"] + "/repolaunch?charset=utf8mb4",
        "REPOLAUNCH_MILVUS_URI": "http://127.0.0.1:" + config["MILVUS_PORT"],
        "LANGFUSE_PUBLIC_KEY": config["LANGFUSE_PUBLIC_KEY"], "LANGFUSE_SECRET_KEY": config["LANGFUSE_SECRET_KEY"],
        "LANGFUSE_BASE_URL": "http://127.0.0.1:" + config["LANGFUSE_PORT"],
    }


def update_app_env(root: Path, values: dict[str, str]) -> None:
    """Replace only integration-owned settings, preserving model configuration."""
    path = root / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    pending = dict(values)
    rewritten = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            if key in pending:
                rewritten.append(key + "=" + pending.pop(key))
        else:
            rewritten.append(line)
    rewritten.extend(key + "=" + value for key, value in pending.items())
    private_write(path, "\n".join(rewritten).rstrip() + "\n")


def verify_services(config: dict) -> dict:
    from sqlalchemy import create_engine, text
    from pymilvus import MilvusClient
    import httpx
    app = app_configuration(config)
    database = create_engine(app["DATABASE_URL"], connect_args={"connect_timeout": 8}, pool_pre_ping=True)
    try:
        with database.connect() as connection:
            if connection.execute(text("SELECT 1")).scalar_one() != 1:
                raise RuntimeError("MySQL SELECT 1 did not return the expected value")
    finally:
        database.dispose()
    client = MilvusClient(uri=app["REPOLAUNCH_MILVUS_URI"], timeout=10)
    try:
        client.list_collections(timeout=10)
        milvus_version = client.get_server_version(timeout=10)
    finally:
        client.close()
    base = app["LANGFUSE_BASE_URL"]
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        response = http.get(base + "/api/public/health?failIfDatabaseUnavailable=true")
        response.raise_for_status()
        response = http.get(base + "/api/public/projects", auth=(config["LANGFUSE_PUBLIC_KEY"], config["LANGFUSE_SECRET_KEY"]))
        response.raise_for_status()
        if not any(item.get("id") == "repolaunch" for item in response.json().get("data", [])):
            raise RuntimeError("Langfuse尚未创建指定项目或API凭证未通过验证。")
    return {"mysql": {"ready": True, "host": "127.0.0.1", "port": int(config["MYSQL_PORT"]), "database": "repolaunch"},
            "milvus": {"ready": True, "uri": app["REPOLAUNCH_MILVUS_URI"], "version": milvus_version},
            "langfuse": {"ready": True, "url": base, "project": "repolaunch", "login_email": "admin@repolaunch.local"}}


def migrate_history(root: Path, database_url: str) -> dict:
    """Copy historical task records transactionally only into an empty target."""
    from sqlalchemy import func, select
    from repolaunch.storage import TaskStore
    source = root / ".repolaunch" / "tasks.sqlite3"
    if not source.is_file():
        return {"copied": 0, "reason": "no_sqlite_source"}
    target = TaskStore(root / ".repolaunch", database_url)
    try:
        with target.engine.connect() as connection:
            if connection.execute(select(func.count()).select_from(target.table)).scalar_one():
                return {"copied": 0, "reason": "target_not_empty"}
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repolaunch_tasks'").fetchone():
                return {"copied": 0, "reason": "no_source_table"}
            records = connection.execute("SELECT id,data FROM repolaunch_tasks").fetchall()
        rows = []
        for identity, raw in records:
            task = json.loads(raw)
            if task.get("id") != identity:
                raise ValueError("SQLite历史记录编号不一致，未迁移。")
            rows.append({"id": identity, "data": json.dumps(task, ensure_ascii=False)})
        if rows:
            with target.engine.begin() as connection:
                if connection.execute(select(func.count()).select_from(target.table)).scalar_one():
                    raise ValueError("迁移前发现目标数据库已有记录，未覆盖。")
                connection.execute(target.table.insert(), rows)
        return {"copied": len(rows), "reason": "copied", "source_preserved": True}
    finally:
        target.close()


def deployment_metadata(config: dict, services: dict, history: dict) -> dict:
    return {"project": PROJECT, "verified_at": datetime.now(timezone.utc).isoformat(),
            "data_directory": str(ROOT / ".repolaunch" / "services"), "services": services,
            "history": history, "images": IMAGES,
            "storage": {"clickhouse": {"kind": "project_scoped_named_volumes",
                "volumes": [PROJECT + "_clickhouse-data", PROJECT + "_clickhouse-logs"],
                "physical_location": "Docker Desktop Linux disk image (this installation is configured on F drive)",
                "reason": "Windows NTFS bind mounts rejected ClickHouse atomic directory rename; previous bind directories are preserved"}},
            "credentials_file": str(ROOT / ".repolaunch" / "services" / "credentials.txt")}


DIAGNOSTIC_SERVICES = {"langfuse-web", "langfuse-worker", "postgres", "clickhouse"}
DIAGNOSTIC_INSPECT_FORMAT = ('{"state":{{json .State}},"restart_count":{{json .RestartCount}},'
    '"memory_limit":{{json .HostConfig.Memory}},"image":{{json .Config.Image}},'
    '"exposed_ports":{{json .Config.ExposedPorts}},"entrypoint":{{json .Config.Entrypoint}},'
    '"command":{{json .Config.Cmd}}}')


def collect_diagnostics(root: Path = ROOT, reason: str = "") -> Path:
    """Read only this Compose project's state and bounded, redacted logs."""
    config = read_env(root / ".repolaunch" / "services" / "services.env")
    report = {"project": PROJECT, "collected_at": datetime.now(timezone.utc).isoformat(),
              "reason": reason, "containers": [], "errors": []}
    try:
        info = docker_command(["info", "--format", '{"memory_total":{{json .MemTotal}},"cpu_count":{{json .NCPU}}}'], config, 10)
        report["docker_resources"] = json.loads(info)
    except Exception as exc:
        report["errors"].append("Docker resources: " + str(exc))
    try:
        output = compose_command(root, ["ps", "--all", "--format", "json"], config, 15)
        try:
            parsed = json.loads(output or "[]")
            records = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            records = [json.loads(line) for line in output.splitlines() if line.strip()]
        for record in records:
            if not isinstance(record, dict):
                continue
            selected = {key: record.get(key) for key in ["ID", "Name", "Service", "State", "Status", "Health", "ExitCode", "Publishers"]}
            report["containers"].append(selected)
            identity = record.get("ID", "")
            if record.get("Service") not in DIAGNOSTIC_SERVICES or not re.fullmatch(r"[a-f0-9]{12,64}", str(identity)):
                continue
            try:
                details = docker_command(["inspect", "--type", "container", "--format", DIAGNOSTIC_INSPECT_FORMAT, identity], config, 8)
                selected["details"] = json.loads(details)
            except Exception as exc:
                selected["inspect_error"] = str(exc)
            try:
                selected["logs"] = docker_command(["logs", "--tail", "120", identity], config, 10, include_stderr=True)[-64000:]
            except Exception as exc:
                selected["logs_error"] = str(exc)
    except Exception as exc:
        report["errors"].append("Compose project state: " + str(exc))
    path = root / ".repolaunch" / "services" / "diagnostics.json"
    def redact(value):
        if isinstance(value, str):
            return sanitized(value, config, max_chars=None)
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    private_write(path, json.dumps(redact(report), ensure_ascii=False, indent=2) + "\n")
    return path


def save_failure_diagnostics(reason: str) -> None:
    try:
        path = collect_diagnostics(reason=reason)
        print("只读诊断已保存：" + str(path), file=sys.stderr)
    except Exception:
        print("只读诊断未能保存；已有数据与容器保持不变。", file=sys.stderr)


def _deploy_unlocked(root: Path = ROOT, timeout: int = 600) -> dict:
    from repolaunch.engine import docker_status
    status = docker_status()
    if not status.get("available"):
        raise RuntimeError(status.get("message", "Docker不可用"))
    config = prepare(root)
    compose_command(root, ["version", "--short"], config, 15)
    existing = docker_command(["ps", "--filter", "label=com.docker.compose.project=" + PROJECT, "--format", "{{.Ports}}"], config, 15)
    owned_ports = {int(value) for value in re.findall(r"127\.0\.0\.1:(\d+)->", existing)}
    ensure_ports_free(config, owned_ports)
    total = docker_command(["info", "--format", "{{.MemTotal}}"], config, 15).strip()
    if total.isdigit() and int(total) < 12 * 1024**3:
        print("提示：全部服务建议分配至少12GB Docker内存；当前较少，启动可能较慢。", flush=True)
    compose_command(root, ["config", "--quiet"], config, 30)
    print("正在下载固定版本镜像，数据保存在项目F盘目录；不会删除已有容器或数据。", flush=True)
    compose_command(root, ["pull"], config, max(timeout, 600))
    print("正在启动MySQL、Milvus、Langfuse及依赖，并等待健康检查……", flush=True)
    compose_command(root, ["up", "--detach", "--wait", "--wait-timeout", str(timeout)], config, timeout + 30)
    deadline = time.monotonic() + 90
    while True:
        try:
            services = verify_services(config)
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("服务容器已启动，但SQL/Milvus或Langfuse项目API验证未通过；未修改应用配置，请重试。") from None
            time.sleep(3)
    history = migrate_history(root, app_configuration(config)["DATABASE_URL"])
    update_app_env(root, app_configuration(config))
    credentials = ("RepoLaunch本地服务凭证。不要分享或提交此文件。\n"
                   "Langfuse: http://127.0.0.1:" + config["LANGFUSE_PORT"] + "\n"
                   "Login: admin@repolaunch.local\nPassword: " + config["LANGFUSE_ADMIN_PASSWORD"] + "\n"
                   "MinIO console: http://127.0.0.1:" + config["MINIO_CONSOLE_PORT"] + "\n"
                   "MinIO user: repolaunch\nMinIO password: " + config["MINIO_ROOT_PASSWORD"] + "\n"
                   "MySQL app user: repolaunch\nMySQL app password: " + config["MYSQL_PASSWORD"] + "\n")
    private_write(root / ".repolaunch" / "services" / "credentials.txt", credentials)
    metadata = deployment_metadata(config, services, history)
    metadata["app_port"] = find_app_port()
    private_write(root / ".repolaunch" / "services" / "status.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print("三项服务已通过真实连接验证，应用配置已更新。凭证仅写入本机文件，未输出。", flush=True)
    return metadata


def find_app_port() -> int:
    for number in range(8787, 8827):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", number))
                return number
            except OSError:
                continue
    raise RuntimeError("没有可用于RepoLaunch的新端口；服务已部署，稍后可手动运行start.bat。")


@contextmanager
def deployment_lock(root: Path):
    folder = root / ".repolaunch" / "services"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "deployment.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("另一个服务部署任务正在运行；请保留该窗口，勿重复启动。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def deploy(root: Path = ROOT, timeout: int = 600) -> dict:
    with deployment_lock(root):
        return _deploy_unlocked(root, timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy RepoLaunch's local optional services.")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--diagnostics", action="store_true", help="Read-only project state and redacted startup logs")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if not 60 <= args.timeout <= 1200:
        parser.error("timeout must be 60..1200")
    try:
        if args.diagnostics:
            print("只读诊断已保存：" + str(collect_diagnostics()))
        elif args.prepare_only:
            prepare()
            print("已准备本机服务配置；尚未部署或修改应用配置。")
        elif args.verify_only:
            config = prepare()
            print(json.dumps(verify_services(config), ensure_ascii=False, indent=2))
        else:
            deploy(timeout=args.timeout)
        return 0
    except subprocess.TimeoutExpired:
        message = "部署等待超时。保留现有数据和容器，重新运行可继续；未输出任何凭证。"
        print(message, file=sys.stderr)
        if not args.diagnostics:
            save_failure_diagnostics(message)
        return 1
    except Exception as exc:
        config = read_env(ROOT / ".repolaunch" / "services" / "services.env")
        message = sanitized(str(exc), config)
        print("服务部署未完成：" + message, file=sys.stderr)
        if not args.diagnostics:
            save_failure_diagnostics(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
