"""Small, inspectable diagnostic retrieval corpus.

This is lexical retrieval, not a pretend vector database. Cases are shipped with
source links and can be replaced by an external retriever without changing the
planner. No repository text or credentials are sent to a retrieval service.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

CASES = [
    {
        "id": "missing-module", "title": "Python 模块未安装",
        "terms": ["modulenotfounderror", "importerror", "no module named", "module", "dependency"],
        "explanation": "导入名不一定等于 PyPI 包名；优先核对仓库声明的依赖和当前解释器。",
        "action": "只为明确识别的缺失模块补充已知包名，然后重新构建并验证。",
        "source": "https://docs.python.org/3/library/exceptions.html#ModuleNotFoundError",
    },
    {
        "id": "python-version", "title": "Python 版本与项目要求不兼容",
        "terms": ["requires-python", "requires a different python", "python version", "syntaxerror", "pyproject"],
        "explanation": "项目的 requires-python 和依赖的 Python 范围必须同时满足。",
        "action": "以项目声明为依据选择受支持的解释器，不随意升级所有依赖。",
        "source": "https://packaging.python.org/en/latest/specifications/pyproject-toml/#requires-python",
    },
    {
        "id": "uvicorn-bind", "title": "ASGI 服务已启动但容器外不可访问",
        "terms": ["uvicorn", "fastapi", "asgi", "connection refused", "127.0.0.1", "host"],
        "explanation": "容器内服务需监听 0.0.0.0；发布到宿主机的端口仍只绑定回环地址。",
        "action": "使用受控 uvicorn 参数指定 host 和 port，再检查已发布端口。",
        "source": "https://www.uvicorn.org/settings/#socket-binding",
    },
    {
        "id": "flask-launch", "title": "Flask 应用入口与监听地址",
        "terms": ["flask", "wsgi", "could not import", "application", "flask_app"],
        "explanation": "Flask CLI 的 --app 应与实际模块对应，演示服务器仅用于本地体验。",
        "action": "从源文件识别 Flask 实例并生成固定 CLI 参数，避免照抄任意 shell。",
        "source": "https://flask.palletsprojects.com/en/stable/cli/",
    },
    {
        "id": "required-secret", "title": "缺失外部服务密钥或必填配置",
        "terms": ["api_key", "api key", "keyerror", "environ", "environment", "secret", "token"],
        "explanation": "仓库运行密钥与规划模型密钥是两套权限，不能自动相互转发。",
        "action": "列出必填配置并暂停；本版本不会把规划模型密钥传入目标容器。",
        "source": "https://docs.python.org/3/library/os.html#os.environ",
    },
    {
        "id": "pip-resolution", "title": "依赖解析冲突",
        "terms": ["resolutionimpossible", "conflict", "pip", "requirements", "dependencies"],
        "explanation": "依赖冲突可能来自互斥的版本约束，盲目忽略依赖会隐藏运行错误。",
        "action": "结合原始版本约束分析；无法给出受控修复时停止并导出日志。",
        "source": "https://pip.pypa.io/en/stable/topics/dependency-resolution/",
    },
]


def _lexical_retrieve(query: str, limit: int = 3) -> list[dict]:
    """Rank bundled cases by exact diagnostic phrases and token overlap."""
    text = query[:60000].casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", text))
    ranked = []
    for case in CASES:
        score = sum(3 if " " in term else 1 for term in case["terms"] if term in text)
        score += len(tokens.intersection(case["terms"]))
        if score:
            result = deepcopy(case)
            result["score"] = score
            ranked.append(result)
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    return ranked[:max(0, min(limit, 6))]

# ---- Explicit opt-in vector retrieval --------------------------------------
COLLECTION_NAME = "repolaunch_diagnostics"
_EXPECTED_DIMENSIONS = {"BAAI/bge-m3": 1024, "Pro/BAAI/bge-m3": 1024}
_STATUS_LOCK = threading.RLock()
_LAST_STATUS: dict = {}
_ENV_KEYS = {"REPOLAUNCH_MILVUS_URI", "REPOLAUNCH_MILVUS_TOKEN", "REPOLAUNCH_EMBEDDING_BASE_URL",
             "REPOLAUNCH_EMBEDDING_MODEL", "REPOLAUNCH_EMBEDDING_API_KEY", "REPOLAUNCH_DATA_DIR"}


class RetrievalError(ValueError):
    """Safe diagnostic failure; does not contain external service responses."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _diagnostic_query(text: str) -> str:
    lowered = text[:60000].casefold()
    return " ".join(sorted({term for case in CASES for term in case["terms"] if term in lowered}))[:2000]


def _configuration_id() -> tuple:
    # Secrets are deliberately not part of the observable status or cache key.
    return tuple(os.environ.get(key, "") for key in
                 ("REPOLAUNCH_MILVUS_URI", "REPOLAUNCH_EMBEDDING_BASE_URL", "REPOLAUNCH_EMBEDDING_MODEL"))


def _record_status(**fields) -> None:
    with _STATUS_LOCK:
        if _LAST_STATUS.get("configuration_id") != _configuration_id():
            _LAST_STATUS.clear()
        _LAST_STATUS.update(configuration_id=_configuration_id(), checked_at=datetime.now(timezone.utc).isoformat(), **fields)


def _vector_configuration() -> dict:
    uri = os.environ.get("REPOLAUNCH_MILVUS_URI", "").strip()
    if not uri:
        raise RetrievalError("not_configured", "未配置 REPOLAUNCH_MILVUS_URI。")
    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RetrievalError("invalid_configuration", "Milvus 地址必须是无内嵌凭证的 HTTP/HTTPS 服务地址。")
    api_key = os.environ.get("REPOLAUNCH_EMBEDDING_API_KEY", "")
    base_url = os.environ.get("REPOLAUNCH_EMBEDDING_BASE_URL", "")
    if not api_key or not base_url:
        raise RetrievalError("embedding_not_configured", "需要单独配置 REPOLAUNCH_EMBEDDING_API_KEY 与 REPOLAUNCH_EMBEDDING_BASE_URL。")
    # Delayed import avoids planner/knowledge initialization cycles.
    from .planner import validate_provider_url
    base_url = validate_provider_url(base_url)
    host = (urlsplit(base_url).hostname or "").casefold()
    default_model = "BAAI/bge-m3" if host in {"api.siliconflow.cn", "api.siliconflow.com"} else "text-embedding-3-small"
    model = os.environ.get("REPOLAUNCH_EMBEDDING_MODEL", default_model).strip()
    if not model or len(model) > 200 or any(ord(char) < 32 for char in model):
        raise RetrievalError("invalid_configuration", "嵌入模型名称无效。")
    return {"uri": uri, "token": os.environ.get("REPOLAUNCH_MILVUS_TOKEN", ""),
            "api_key": api_key, "base_url": base_url, "model": model}


def _milvus_client(config: dict):
    try:
        from pymilvus import MilvusClient
    except ImportError:
        raise RetrievalError("dependency_missing", "尚未安装 Milvus 可选依赖，请运行 pip install -e '.[integrations]'。") from None
    return MilvusClient(uri=config["uri"], token=config["token"], timeout=5)


@contextmanager
def _vector_resources(config: dict):
    try:
        from langchain_openai import OpenAIEmbeddings
        import httpx
    except ImportError:
        raise RetrievalError("dependency_missing", "尚未安装嵌入模型依赖，请安装项目核心依赖。") from None
    client = None
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as http_client:
            embeddings = OpenAIEmbeddings(
                model=config["model"], api_key=config["api_key"], base_url=config["base_url"],
                http_client=http_client, request_timeout=20, max_retries=0,
                check_embedding_ctx_length=False, model_kwargs={"encoding_format": "float"},
            )
            # Do not send dimensions: SiliconFlow bge-m3 has fixed 1024 dimensions.
            client = _milvus_client(config)
            yield client, embeddings
    finally:
        if client is not None:
            client.close()


def _validate_vectors(vectors: list, count: int, model: str) -> int:
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise RetrievalError("invalid_vectors", "嵌入服务返回的向量数量不匹配。")
    if not isinstance(vectors[0], list) or not 1 <= len(vectors[0]) <= 16384:
        raise RetrievalError("invalid_vectors", "嵌入服务返回的向量维度无效。")
    dimension = len(vectors[0])
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in vector
        ) or not any(vector):
            raise RetrievalError("invalid_vectors", "嵌入服务返回了长度不一致、非有限或全零向量。")
    expected = _EXPECTED_DIMENSIONS.get(model)
    if expected is not None and dimension != expected:
        raise RetrievalError("dimension_mismatch", f"嵌入向量维度应为 {expected}，当前返回 {dimension}；未修改集合。")
    return dimension


def _collection_dimension(client) -> int | None:
    description = client.describe_collection(collection_name=COLLECTION_NAME, timeout=5)
    for field in description.get("fields", []):
        if field.get("name") == "vector":
            try:
                return int(field.get("params", {}).get("dim"))
            except (TypeError, ValueError):
                return None
    return None


def _case_filter(model: str) -> str:
    return "embedding_model == " + json.dumps(model)


def _indexed_case_count(client, model: str) -> int:
    rows = client.query(collection_name=COLLECTION_NAME, filter=_case_filter(model),
                        output_fields=["case_id"], limit=len(CASES) + 1, timeout=5,
                        consistency_level="Strong")
    expected = {case["id"] for case in CASES}
    return len({row.get("case_id") for row in rows if row.get("case_id") in expected})


def retrieval_status(probe: bool = False) -> dict:
    """Default is local cached metadata only. probe=True reads Milvus, never embeds."""
    configured = bool(os.environ.get("REPOLAUNCH_MILVUS_URI"))
    result = {"configured": configured, "backend": "lexical", "status": "disabled" if not configured else "not_checked",
              "collection": COLLECTION_NAME, "connected": None, "collection_exists": None,
              "indexed_count": None, "dimension": None, "checked_at": None,
              "embedding_configured": bool(os.environ.get("REPOLAUNCH_EMBEDDING_API_KEY") and os.environ.get("REPOLAUNCH_EMBEDDING_BASE_URL")),
              "embedding_model": os.environ.get("REPOLAUNCH_EMBEDDING_MODEL") or None,
              "message": "使用本地关键词检索。" if not configured else "已配置，尚未进行真实检索或只读检查。"}
    if not configured:
        return result
    if probe:
        client = None
        try:
            config = _vector_configuration()
            client = _milvus_client(config)
            exists = client.has_collection(collection_name=COLLECTION_NAME, timeout=5)
            dimension = _collection_dimension(client) if exists else None
            count = _indexed_case_count(client, config["model"]) if exists else 0
            ready = exists and count == len(CASES)
            _record_status(status="indexed" if ready else "not_indexed", connected=True,
                           collection_exists=exists, indexed_count=count, dimension=dimension,
                           embedding_model=config["model"], backend="lexical",
                           message="Milvus 案例已读回；尚未由此次检查调用嵌入服务。" if ready else "Milvus 已连接，但尚未确认完整诊断索引。")
        except Exception as error:
            _record_status(status="unavailable", connected=False, backend="lexical",
                           message=str(error) if isinstance(error, RetrievalError) else "Milvus 只读检查失败，请检查服务与集合配置。")
        finally:
            if client is not None:
                client.close()
    with _STATUS_LOCK:
        if _LAST_STATUS.get("configuration_id") == _configuration_id():
            result.update({key: value for key, value in _LAST_STATUS.items() if key != "configuration_id"})
    return result


def index_diagnostic_cases() -> dict:
    """Upsert and read back curated cases; never index repository data."""
    config = _vector_configuration()
    texts = [" ".join([case["title"], *case["terms"], case["explanation"], case["action"]]) for case in CASES]
    try:
        with _vector_resources(config) as (client, embeddings):
            vectors = embeddings.embed_documents(texts)
            dimension = _validate_vectors(vectors, len(CASES), config["model"])
            exists = client.has_collection(collection_name=COLLECTION_NAME, timeout=5)
            if exists:
                current_dimension = _collection_dimension(client)
                if current_dimension != dimension:
                    raise RetrievalError("dimension_mismatch", "现有 Milvus 集合维度与嵌入模型不一致；已保留原集合，请人工处理。")
            else:
                client.create_collection(collection_name=COLLECTION_NAME, dimension=dimension,
                                         metric_type="COSINE", auto_id=False, timeout=10)
            rows = [{"id": index + 1, "vector": vector, "case_id": case["id"], "embedding_model": config["model"]}
                    for index, (case, vector) in enumerate(zip(CASES, vectors))]
            client.upsert(collection_name=COLLECTION_NAME, data=rows, timeout=10)
            client.flush(collection_name=COLLECTION_NAME, timeout=10)
            client.load_collection(collection_name=COLLECTION_NAME, timeout=10)
            count = _indexed_case_count(client, config["model"])
            if count != len(CASES):
                raise RetrievalError("index_unverified", "Milvus 写入后未能读回完整案例，不能宣称索引成功。")
            _record_status(status="indexed", connected=True, collection_exists=True, indexed_count=count,
                           dimension=dimension, embedding_model=config["model"], backend="lexical",
                           message="诊断案例已写入并读回验证；等待实际向量查询。")
            return {"collection": COLLECTION_NAME, "count": len(rows), "verified_count": count,
                    "dimension": dimension, "model": config["model"]}
    except RetrievalError:
        raise
    except Exception:
        raise RetrievalError("index_failed", "诊断案例索引失败，请检查 Milvus、嵌入服务、向量维度及权限。") from None


def retrieve_diagnostics(query: str, limit: int = 3) -> dict:
    """Return truthful backend metadata, preserving no-network lexical default."""
    limit = max(0, min(int(limit), 6))
    lexical = _lexical_retrieve(query, limit)
    if not os.environ.get("REPOLAUNCH_MILVUS_URI"):
        return {"cases": lexical, "backend": "lexical", "warning": ""}
    safe_query = _diagnostic_query(query)
    if not safe_query or not limit:
        return {"cases": lexical, "backend": "lexical", "warning": "无诊断关键词，未发送嵌入请求。"}
    try:
        config = _vector_configuration()
        with _vector_resources(config) as (client, embeddings):
            if not client.has_collection(collection_name=COLLECTION_NAME, timeout=5):
                _record_status(status="not_indexed", connected=True, collection_exists=False,
                               indexed_count=0, backend="lexical", message="Milvus 诊断集合尚未初始化。")
                return {"cases": lexical, "backend": "lexical", "warning": "Milvus 诊断集合尚未初始化，已使用本地关键词检索。请先执行索引命令。"}
            vector = embeddings.embed_query(safe_query)
            dimension = _validate_vectors([vector], 1, config["model"])
            collection_dimension = _collection_dimension(client)
            if dimension != collection_dimension:
                raise RetrievalError("dimension_mismatch", "Milvus 集合与查询向量维度不匹配。")
            hits = client.search(collection_name=COLLECTION_NAME, data=[vector], limit=limit,
                                 output_fields=["case_id"], search_params={"metric_type": "COSINE", "params": {}},
                                 filter=_case_filter(config["model"]), timeout=5, consistency_level="Strong")
            by_id = {case["id"]: case for case in CASES}
            cases = []
            for hit in (hits[0] if hits else []):
                case_id = hit.get("entity", {}).get("case_id")
                score = hit.get("distance", 0)
                if case_id in by_id and isinstance(score, (int, float)) and math.isfinite(score):
                    case = deepcopy(by_id[case_id])
                    case.update(score=float(score), retrieval_backend="milvus")
                    cases.append(case)
            if not cases:
                _record_status(status="no_matches", connected=True, collection_exists=True,
                               dimension=dimension, embedding_model=config["model"], backend="lexical",
                               message="Milvus 未返回匹配模型的案例，已使用本地检索。")
                return {"cases": lexical, "backend": "lexical", "warning": "Milvus 未返回匹配模型的案例，已使用本地关键词检索。"}
            _record_status(status="ready", connected=True, collection_exists=True,
                           dimension=dimension, embedding_model=config["model"], backend="milvus",
                           message="实际嵌入请求与 Milvus 向量查询已完成。")
            return {"cases": cases, "backend": "milvus", "warning": ""}
    except Exception as error:
        warning = str(error) if isinstance(error, RetrievalError) else "Milvus 或嵌入服务不可用，已使用本地关键词检索；未声称完成向量检索。"
        _record_status(status="unavailable", backend="lexical", connected=None, message=warning)
        return {"cases": lexical, "backend": "lexical", "warning": warning}


def retrieve_cases(query: str, limit: int = 3) -> list[dict]:
    return retrieve_diagnostics(query, limit)["cases"]


def load_integration_env(path: str | Path) -> int:
    """Explicit CLI-only env loading. No interpolation, execution or secret output."""
    count = 0
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in _ENV_KEYS or key in os.environ:
            continue
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise RetrievalError("invalid_configuration", "配置文件存在未闭合引号。")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ[key] = value
        count += 1
    return count


def load_saved_embedding_configuration(data_dir: str | Path | None = None) -> int:
    """CLI opt-in: load only separately saved embedding fields from user-bound storage."""
    from .credentials import load_credentials
    directory = Path(data_dir) if data_dir is not None else Path(os.environ.get(
        "REPOLAUNCH_DATA_DIR", str(Path(__file__).resolve().parent.parent / ".repolaunch")))
    try:
        saved = load_credentials(directory)
    except Exception:
        raise RetrievalError("credential_unavailable", "无法读取当前 Windows 用户的嵌入配置，请在本地网页中重新设置。") from None
    count = 0
    for field, variable in (("embedding_api_key", "REPOLAUNCH_EMBEDDING_API_KEY"),
                            ("embedding_base_url", "REPOLAUNCH_EMBEDDING_BASE_URL"),
                            ("embedding_model", "REPOLAUNCH_EMBEDDING_MODEL")):
        if not os.environ.get(variable) and isinstance(saved.get(field), str) and saved[field]:
            os.environ[variable] = saved[field]
            count += 1
    # Generic api_key/base_url/model are intentionally never reused here.
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="RepoLaunch diagnostic retrieval integration commands")
    parser.add_argument("--env-file", help="Explicit env file; only retrieval integration keys are read")
    parser.add_argument("--index", action="store_true", help="Calls embedding API for six curated cases and verifies Milvus writes")
    parser.add_argument("--smoke", action="store_true", help="Calls embedding API once and tests actual Milvus query")
    parser.add_argument("--status", action="store_true", help="Read-only Milvus collection check; no embedding API call")
    args = parser.parse_args()
    output = {"ok": True}
    try:
        if args.env_file:
            load_integration_env(args.env_file)
        load_saved_embedding_configuration()
        if args.index:
            output["index"] = index_diagnostic_cases()
        if args.smoke:
            retrieval = retrieve_diagnostics("ModuleNotFoundError: No module named 'yaml'", limit=3)
            output["smoke"] = {"backend": retrieval["backend"], "case_ids": [case["id"] for case in retrieval["cases"]],
                               "warning": retrieval["warning"]}
            output["ok"] = retrieval["backend"] == "milvus" and "missing-module" in output["smoke"]["case_ids"]
        if args.status or not (args.index or args.smoke):
            output["status"] = retrieval_status(probe=True)
    except Exception as error:
        output = {"ok": False, "error": str(error) if isinstance(error, RetrievalError) else "集成检查失败，请检查配置、依赖与服务。"}
    print(json.dumps(output, ensure_ascii=False))
    if not output["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
