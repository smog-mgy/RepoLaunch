"""Foreground launcher with durable startup logs and verified browser handoff."""
from __future__ import annotations

import argparse
import json
import logging
import logging.config
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
LOGGER = logging.getLogger("repolaunch.launcher")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_server(port: int, timeout: float = 0.8) -> bool:
    """Only identify RepoLaunch by its explicit local health response."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(f"http://{HOST}:{port}/api/health", timeout=timeout) as response:
            if response.status != 200:
                return False
            data = response.read(16385)
            if len(data) > 16384:
                return False
            identity = json.loads(data)
            return isinstance(identity, dict) and identity.get("application") == "repolaunch" and identity.get("ready") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def reserve_listener(port: int) -> socket.socket:
    """Reserve the exact socket passed to Uvicorn, avoiding preflight races."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((HOST, port))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def _configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s", "datefmt": "%Y-%m-%d %H:%M:%S"}},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "plain"},
            "file": {"class": "logging.handlers.RotatingFileHandler", "filename": str(log_file), "maxBytes": 1_000_000, "backupCount": 2, "encoding": "utf-8", "formatter": "plain"},
        },
        "loggers": {
            "repolaunch.launcher": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": True},
        },
    })


def _open_browser(url: str) -> None:
    try:
        if webbrowser.open(url, new=2):
            LOGGER.info("已请求默认浏览器打开 %s；若未出现页面，请手动复制该地址。", url)
        else:
            LOGGER.warning("未能唤起默认浏览器，请手动打开 %s", url)
    except Exception as exc:
        LOGGER.warning("浏览器启动失败（%s）；请手动打开 %s", type(exc).__name__, url)


def _ready_handoff(server, port: int, stopped: threading.Event, open_browser: bool, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while not stopped.is_set() and time.monotonic() < deadline:
        if server.started and probe_server(port):
            url = f"http://{HOST}:{port}"
            LOGGER.info("服务已通过就绪检查：%s", url)
            LOGGER.info("请保留当前窗口；按 Ctrl+C 停止服务。")
            if open_browser:
                _open_browser(url)
            return
        stopped.wait(0.25)
    if not stopped.is_set():
        LOGGER.error("启动超过 %.0f 秒仍未通过就绪检查。请查看 startup.log；不会自动打开未验证的页面。", timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RepoLaunch locally; keep this foreground terminal open.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--auto-port", action="store_true", help="try up to 20 consecutive ports without stopping existing programs")
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--open-browser", action="store_true", help="open the default browser only after RepoLaunch is ready")
    browser.add_argument("--no-browser", action="store_true", help="keep browser opening disabled")
    parser.add_argument("--log-file", type=Path, default=Path(__file__).resolve().parent.parent / "startup.log")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        _configure_logging(args.log_file.resolve())
    except Exception as exc:
        print(f"Cannot write startup log: {exc}", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{args.port}"
    LOGGER.info("启动 RepoLaunch；Python %s；日志 %s", sys.version.split()[0], args.log_file.resolve())
    if probe_server(args.port):
        LOGGER.info("RepoLaunch 已在运行：%s。复用现有服务，不启动第二个实例。", url)
        if args.open_browser:
            _open_browser(url)
        return 0
    listener = None
    initial_port = args.port
    last_error = None
    for offset in range(20 if args.auto_port else 1):
        candidate = initial_port + offset
        if candidate > 65535:
            break
        candidate_url = f"http://{HOST}:{candidate}"
        if offset and probe_server(candidate, timeout=0.3):
            LOGGER.info("复用已就绪的 RepoLaunch：%s", candidate_url)
            if args.open_browser:
                _open_browser(candidate_url)
            return 0
        try:
            listener = reserve_listener(candidate)
            args.port = candidate
            url = candidate_url
            if offset:
                LOGGER.info("原端口 %s 被占用，已自动改用 %s；未终止任何已有程序。", initial_port, url)
            break
        except OSError as exc:
            last_error = exc
            # Strict explicit-port launches allow a simultaneous own startup.
            if not args.auto_port:
                for _ in range(6):
                    if probe_server(candidate, timeout=0.3):
                        LOGGER.info("检测到另一个 RepoLaunch 实例已就绪：%s", candidate_url)
                        if args.open_browser:
                            _open_browser(candidate_url)
                        return 0
                    time.sleep(0.25)
    if listener is None:
        LOGGER.error("无法监听本机端口 %s：%s。端口被占用，或系统阻止了本机监听。", initial_port, last_error)
        LOGGER.error("请尝试 .\\start.ps1 -Port %s；未终止任何已有程序。", initial_port + 20 if initial_port < 65515 else 8787)
        return 1
    stopped = threading.Event()
    worker = None
    try:
        import uvicorn
        config = uvicorn.Config("repolaunch.app:create_app", host=HOST, port=args.port, factory=True, log_config=None, access_log=False, proxy_headers=False)
        server = uvicorn.Server(config)
        worker = threading.Thread(target=_ready_handoff, args=(server, args.port, stopped, args.open_browser), name="repolaunch-readiness", daemon=True)
        worker.start()
        LOGGER.info("正在加载应用；就绪前不会自动打开浏览器。")
        server.run(sockets=[listener])
        if not server.started:
            LOGGER.error("应用未完成启动，请检查上方错误和 startup.log。")
            return 1
        return 0
    except KeyboardInterrupt:
        LOGGER.info("已请求停止 RepoLaunch。")
        return 0
    except SystemExit as exc:
        LOGGER.error("应用启动失败（退出码 %s），请查看 startup.log。", exc.code)
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception:
        LOGGER.exception("RepoLaunch 启动失败。请确认已运行 setup.ps1 且项目目录可写。")
        return 1
    finally:
        stopped.set()
        listener.close()
        if worker:
            worker.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
