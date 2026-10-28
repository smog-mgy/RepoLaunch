"""Real, read-only MCP stdio verification; prints one sanitized JSON result.

Run with the project Python: python tools/verify_mcp.py --expected-backend mysql
The worker uses the actual MCP SDK handshake and only the three read-only tools.
No model/embedding/tracing key is passed to the subprocess or loaded by MCP.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"list_tasks", "task_status", "export_metadata"}
ENV_NAMES = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "DATABASE_URL", "REPOLAUNCH_DATA_DIR"}
STATUSES = {"analyzing", "awaiting_approval", "running", "repairing", "succeeded", "failed", "cancelled", "stopped"}


def minimal_environment() -> dict[str, str]:
    result = {name: value for name, value in os.environ.items() if name in ENV_NAMES}
    result["PYTHONUTF8"] = "1"
    return result


def tool_value(result):
    if getattr(result, "isError", False):
        raise ValueError("MCP read-only tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    content = getattr(result, "content", [])
    text = "".join(item.text for item in content if getattr(item, "type", None) == "text")
    if len(text) > 65536:
        raise ValueError("MCP result exceeded verification bounds")
    return json.loads(text)


async def verify_sdk(expected_backend: str | None = None) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "tools" / "mcp_history.py")], env=minimal_environment(), cwd=str(ROOT.parent))
    with open(os.devnull, "w", encoding="utf-8") as errors:
        async with asyncio.timeout(9):
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=4)) as session:
                    initialized = await session.initialize()
                    backend_match = re.search(r"\(([a-z0-9_-]{1,20})\)$", initialized.serverInfo.name)
                    backend = backend_match.group(1) if backend_match else "unknown"
                    if expected_backend and backend != expected_backend:
                        raise ValueError("MCP backend did not match expected database")
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    if names != TOOLS:
                        raise ValueError("MCP tool surface did not match read-only contract")
                    readonly = all(getattr(tool.annotations, "readOnlyHint", False) is True and getattr(tool.annotations, "destructiveHint", None) is False for tool in tools)
                    if not readonly:
                        raise ValueError("MCP tools are missing read-only annotations")
                    tasks = tool_value(await session.call_tool("list_tasks", {"limit": 5}))
                    if not isinstance(tasks, list):
                        raise ValueError("MCP list_tasks did not return a list")
                    checked = False
                    task_status = None
                    identifier = next((task.get("id") for task in tasks if isinstance(task, dict) and re.fullmatch(r"[a-f0-9]{16,32}", str(task.get("id", "")))), None)
                    if identifier:
                        status = tool_value(await session.call_tool("task_status", {"task_id": identifier}))
                        if not isinstance(status, dict) or status.get("id") != identifier:
                            raise ValueError("MCP task status did not match listed task")
                        checked = True
                        task_status = status.get("status") if status.get("status") in STATUSES else "unknown"
                    version = str(initialized.protocolVersion)
                    return {"ok": True, "transport": "stdio", "protocol_version": version if re.fullmatch(r"\d{4}-\d{2}-\d{2}", version) else "negotiated", "database_backend": backend, "tool_names": sorted(names), "all_tools_read_only": readonly, "sample_task_count": len(tasks), "task_status_checked": checked, "task_status": task_status}


def worker(expected_backend: str | None) -> int:
    try:
        result = asyncio.run(verify_sdk(expected_backend))
    except Exception:
        # SDK/provider/database exception strings can contain local paths or secrets.
        result = {"ok": False, "error": "mcp_verification_failed", "message": "MCP handshake/read-only verification failed; check local database readiness and installed MCP dependency."}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result["ok"] else 1


def terminate_owned_tree(process: subprocess.Popen) -> bool:
    if process.poll() is not None:
        return True
    try:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            if result.returncode != 0:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
        return True
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify RepoLaunch using the real MCP stdio SDK (read-only).")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-backend", choices=["mysql", "sqlite"])
    args = parser.parse_args()
    if args.worker:
        return worker(args.expected_backend)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    if args.expected_backend:
        command += ["--expected-backend", args.expected_backend]
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen(command, cwd=ROOT, env=minimal_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", **options)
    except OSError:
        print(json.dumps({"ok": False, "error": "verifier_start_failed", "message": "Unable to start the bounded MCP verifier subprocess."}), flush=True)
        return 1
    try:
        output, _ = process.communicate(timeout=12)
        parsed = json.loads(output)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("ok"), bool):
            raise ValueError("Invalid worker result")
        # Never relay worker output not in the fixed result schema.
        allowed = {"ok", "transport", "protocol_version", "database_backend", "tool_names", "all_tools_read_only", "sample_task_count", "task_status_checked", "task_status", "error", "message"}
        result = {key: value for key, value in parsed.items() if key in allowed}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "mcp_verification_timeout", "message": "MCP verification exceeded the bounded time limit.", "cleanup_confirmed": terminate_owned_tree(process)}
    except (ValueError, OSError):
        terminate_owned_tree(process)
        result = {"ok": False, "error": "invalid_verifier_output", "message": "MCP verifier did not produce a valid sanitized result."}
    finally:
        if process.stdout:
            process.stdout.close()
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
