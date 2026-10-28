"""Optional user-bound Windows DPAPI storage. Plaintext is never written to disk."""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path

_HEADER = b"REPOLAUNCH-WIN-DPAPI-1\n"
_ALLOWED = {"api_key", "base_url", "model", "embedding_api_key", "embedding_base_url", "embedding_model"}
_MAX_BYTES = 32768


class CredentialError(ValueError):
    pass


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise CredentialError("加密保存目前仅支持 Windows；仍可使用仅存内存的模型设置。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signature = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                 ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect = library.CryptUnprotectData if decrypt else library.CryptProtectData
    protect.argtypes = signature
    protect.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_bytes = b"RepoLaunch.credentials.v1"
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    entropy = Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    try:
        if not protect(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise CredentialError("Windows 无法解密已保存的配置，请在原 Windows 用户下重新设置。" if decrypt else "Windows 加密保存失败；没有将密钥写入磁盘。")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(output.pbData)


def _validate(values: dict) -> dict[str, str]:
    if not isinstance(values, dict) or set(values) - _ALLOWED:
        raise CredentialError("保存的模型配置格式不受支持。")
    if any(not isinstance(value, str) or len(value) > 4096 or "\r" in value or "\n" in value for value in values.values()):
        raise CredentialError("模型配置包含无效值。")
    return dict(values)


def save_credentials(data_dir: Path, values: dict) -> None:
    values = _validate(values)
    raw = json.dumps(values, ensure_ascii=False).encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise CredentialError("模型配置超过大小限制。")
    protected = _crypt(raw, decrypt=False)
    if not protected or len(protected) > _MAX_BYTES * 2:
        raise CredentialError("Windows 未返回有效的加密配置；未保存。")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "credentials.enc"
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix="credentials-", suffix=".tmp", dir=directory)
        with os.fdopen(descriptor, "wb") as target:
            target.write(_HEADER + protected)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
    except OSError:
        raise CredentialError("无法保存加密配置，请检查数据目录写入权限。") from None
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def load_credentials(data_dir: Path) -> dict[str, str]:
    source = Path(data_dir) / "credentials.enc"
    if not source.exists():
        return {}
    try:
        if source.stat().st_size > _MAX_BYTES * 2 + len(_HEADER):
            raise CredentialError("已保存的加密配置超过大小限制。")
        encrypted = source.read_bytes()
        if not encrypted.startswith(_HEADER):
            raise CredentialError("已保存的加密配置格式无效，请重新设置。")
        decoded = _crypt(encrypted[len(_HEADER):], decrypt=True)
        if len(decoded) > _MAX_BYTES:
            raise CredentialError("解密配置超过大小限制。")
        return _validate(json.loads(decoded))
    except CredentialError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise CredentialError("无法读取已保存的加密配置，请重新设置。") from None
