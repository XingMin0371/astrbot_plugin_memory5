"""鱼鱼日记 —— 加密与文件读写层。

对外只暴露这一组函数，插件其它模块不再直接 open() 记忆文件，保证写入必加密、
读取必解密。

三档加密强度（配置项 `crypto_mode`）：
    高性能 fast      M5P1  AES-256-GCM(主密钥) + 随机 nonce + AAD
    平衡   balanced  M5E3  AES-256-GCM + HKDF-SHA256 每条记录子密钥 + AAD
    安全   secure    M5S1  AES-256-GCM + HKDF 子密钥 + HMAC-SHA256 + AAD

历史格式（只读，启动时迁移成当前档位）：
    M5E2  AES-256-GCM-SIV
    M5E1  AES-256-GCM

存储形态：
    *.md / *.json   整文件加密：MAGIC + 结构体
    *.jsonl         每行独立加密："MAGIC:" + Base64(结构体)，追加不需要重写整份文件
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from collections import OrderedDict

from astrbot.api import logger

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    from cryptography.hazmat.primitives.hmac import HMAC
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except Exception:  # pragma: no cover - 缺依赖时退化为明文
    AESGCM = None  # type: ignore[assignment]
    AESGCMSIV = None  # type: ignore[assignment]
    HKDF = None  # type: ignore[assignment]
    HMAC = None  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]

def guess_data_dir() -> str:
    """推断数据目录：环境变量优先，其次按插件所在位置（<data>/plugins/memory5）。"""
    env = os.environ.get("MEMORY5_DATA")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = os.path.dirname(here)
    if os.path.basename(plugins_dir) == "plugins":
        return os.path.dirname(plugins_dir)
    return "/AstrBot/data"


DATA_DIR = guess_data_dir()
KEY_PATH = os.environ.get("MEMORY5_KEY_FILE", os.path.join(DATA_DIR, ".memory5.key"))


class CryptoError(RuntimeError):
    """解密失败：缺密钥、密钥不匹配或文件被改动。"""


MODES = ("fast", "balanced", "secure")
MODE_LABELS = {"fast": "高性能", "balanced": "平衡", "secure": "安全"}
MODE_ALIAS = {
    "高性能": "fast", "fast": "fast",
    "平衡": "balanced", "balanced": "balanced",
    "安全": "secure", "secure": "secure",
}

FORMAT = {
    "fast": {"line": "M5P1:", "file": b"M5P1F", "line_aad": b"M5P1L", "file_aad": b"M5P1F"},
    "balanced": {"line": "M5E3:", "file": b"M5E3F", "line_aad": b"M5E3L", "file_aad": b"M5E3F"},
    "secure": {"line": "M5S1:", "file": b"M5S1F", "line_aad": b"M5S1L", "file_aad": b"M5S1F"},
}
MODE_INFO = {
    "fast": "AES-256-GCM",
    "balanced": "AES-256-GCM + HKDF-SHA256 子密钥",
    "secure": "AES-256-GCM + HKDF-SHA256 + HMAC-SHA256",
}
FORMAT_TAG = {"fast": "M5P1", "balanced": "M5E3", "secure": "M5S1"}
LEGACY = (
    ("M5E2:", b"M5E2F", "siv"),
    ("M5E1:", b"M5E1F", "gcm"),
)
# 这些文件要给宿主机脚本直接读，保持明文、不参与加密迁移
PLAIN_FILES = {"backup_policy.json", "backup_verify.json", "export_history.json"}

_MODE = "balanced"
_KEY: bytes | None = None
_FAST_CIPHER = None
_TEXT_CACHE: "OrderedDict[str, tuple[tuple[int, int], str, bool]]" = OrderedDict()
_CACHE_MAX_FILES = 512
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_CACHE_BYTES = 0
_KEY_LOADED = False
_PLAIN_WARNED = False


# --------------------------------------------------------------- 模式与密钥
def set_crypto_mode(mode: str) -> str:
    """设置当前加密档位，返回实际生效的档位。"""
    global _MODE
    _MODE = MODE_ALIAS.get(str(mode or "").strip().lower(), "balanced")
    return _MODE


def crypto_mode() -> str:
    return _MODE


def crypto_suite() -> str:
    return MODE_INFO[_MODE]


def crypto_version() -> str:
    return FORMAT_TAG[_MODE]


def _read_key_file(path: str) -> bytes | None:
    try:
        with open(path, encoding="ascii") as fh:
            raw = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def _load_key_ring() -> list[bytes]:
    """主密钥 + 轮换中的新钥 + 旧钥（解密时依次尝试）。"""
    ring: list[bytes] = []
    for path in (KEY_PATH, KEY_PATH + ".next"):
        key = _read_key_file(path)
        if key and key not in ring:
            ring.append(key)
    try:
        with open(KEY_PATH + ".old", encoding="ascii") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    key = bytes.fromhex(line)
                except ValueError:
                    continue
                if len(key) == 32 and key not in ring:
                    ring.append(key)
    except OSError:
        pass
    return ring


def enc_key() -> bytes | None:
    """读取 32 字节主密钥；缺失时返回 None（按明文工作，便于排错）。"""
    global _KEY, _KEY_LOADED
    if _KEY_LOADED:
        return _KEY
    _KEY_LOADED = True
    _KEY = None
    if AESGCM is None or HKDF is None:
        logger.warning("memory5: 缺少 cryptography，日记按明文保存")
        return None
    key = _read_key_file(KEY_PATH)
    if key is None:
        try:
            with open(KEY_PATH, encoding="ascii") as fh:
                raw = fh.read().strip()
        except OSError:
            return None
        if raw and len(raw) != 64:
            logger.warning("memory5: 密钥长度不对，按明文处理")
        return None
    _KEY = key
    return key


def decrypt_keys() -> list[bytes]:
    """解密可用密钥：主钥优先，其次轮换中的新钥与旧钥。"""
    primary = enc_key()
    ring = _load_key_ring()
    if primary is None:
        return ring
    return [primary] + [k for k in ring if k != primary]


def key_age_seconds() -> float:
    try:
        return max(0.0, time.time() - os.path.getmtime(KEY_PATH))
    except OSError:
        return -1.0


def generate_new_key() -> bytes | None:
    """无条件生成一把新密钥并写入密钥文件。"""
    key = secrets.token_bytes(32)
    _write_key_file(KEY_PATH, key)
    reset_key_cache()
    return enc_key()


def ensure_key() -> tuple[bytes | None, bool]:
    """确保存在可用密钥；缺失或损坏时自动生成。返回 (密钥, 是否新生成)。"""
    key = enc_key()
    if key is not None:
        return key, False
    if os.path.exists(KEY_PATH):
        try:
            os.replace(KEY_PATH, KEY_PATH + ".invalid")
        except OSError:
            pass
    new_key = secrets.token_bytes(32)
    _write_key_file(KEY_PATH, new_key)
    reset_key_cache()
    return enc_key(), True


def reset_key_cache() -> None:
    """测试或换钥后强制重新读取。"""
    global _KEY, _KEY_LOADED
    _KEY = None
    _KEY_LOADED = False


def _write_key_file(path: str, key: bytes) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def rotate_key(root: str, keep_old: int = 3) -> dict:
    """生成新密钥，把 root 下所有记忆文件重加密成新钥，再切换密钥文件。

    过程中新旧密钥都在解密环里，任何时刻中断都不会读不出数据；
    下次启动检测到 .next 会自动续跑。
    """
    global _KEY, _KEY_LOADED, _TEXT_CACHE, _CACHE_BYTES
    if AESGCM is None:
        return {"ok": False, "error": "缺少 cryptography"}
    current = enc_key()
    if current is None:
        return {"ok": False, "error": "缺少主密钥"}
    next_path = KEY_PATH + ".next"
    old_path = KEY_PATH + ".old"

    new_key = _read_key_file(next_path) if os.path.exists(next_path) else None
    resumed = new_key is not None
    if new_key is None:
        new_key = secrets.token_bytes(32)
        _write_key_file(next_path, new_key)

    # 切换到新钥写入（旧钥仍在解密环里）
    _KEY = new_key
    _KEY_LOADED = True
    _TEXT_CACHE.clear()
    _CACHE_BYTES = 0
    try:
        changed = encrypt_tree(root, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"memory5: 密钥轮换重加密失败 {type(exc).__name__}: {exc}")
        _KEY, _KEY_LOADED = current, True
        return {"ok": False, "error": f"重加密失败：{type(exc).__name__}"}

    # 旧钥留档，新钥转正
    try:
        olds = []
        if os.path.exists(old_path):
            with open(old_path, encoding="ascii") as fh:
                olds = [line.strip() for line in fh if line.strip()]
        olds = [current.hex()] + [x for x in olds if x != current.hex()]
        tmp = old_path + ".tmp"
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write("\n".join(olds[:keep_old]) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, old_path)
        os.replace(next_path, KEY_PATH)
    except OSError as exc:
        return {"ok": False, "error": f"切换密钥文件失败：{exc}"}

    reset_key_cache()
    _TEXT_CACHE.clear()
    _CACHE_BYTES = 0
    enc_key()
    return {"ok": True, "files": changed, "resumed": resumed}


def _derive(master: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(master)


def _record_info(mode: str, kind: str) -> bytes:
    # 平衡档沿用 memory5/v3/*，与历史数据保持一致
    if mode == "balanced":
        return b"memory5/v3/" + kind.encode()
    return b"memory5/" + mode.encode() + b"/" + kind.encode()


# ------------------------------------------------------------------ 原语
def _fast_cipher(key: bytes):
    global _FAST_CIPHER
    if _FAST_CIPHER is None or _FAST_CIPHER[0] != key:
        _FAST_CIPHER = (key, AESGCM(key))
    return _FAST_CIPHER[1]


def _enc_fast(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + _fast_cipher(key).encrypt(nonce, data, aad)


def _dec_fast(key: bytes, blob: bytes, aad: bytes) -> bytes:
    return _fast_cipher(key).decrypt(blob[:12], blob[12:], aad)


def _enc_balanced(key: bytes, data: bytes, aad: bytes, info: bytes) -> bytes:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    sub = _derive(key, salt, info)
    return salt + nonce + AESGCM(sub).encrypt(nonce, data, aad)


def _dec_balanced(key: bytes, blob: bytes, aad: bytes, info: bytes) -> bytes:
    sub = _derive(key, blob[:16], info)
    return AESGCM(sub).decrypt(blob[16:28], blob[28:], aad)


def _enc_secure(key: bytes, data: bytes, aad: bytes, info: bytes) -> bytes:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_derive(key, salt, info + b"/enc")).encrypt(nonce, data, aad)
    mac = HMAC(_derive(key, salt, info + b"/mac"), hashes.SHA256())
    mac.update(aad + salt + nonce + ct)
    return salt + nonce + ct + mac.finalize()


def _dec_secure(key: bytes, blob: bytes, aad: bytes, info: bytes) -> bytes:
    salt, nonce, ct, tag = blob[:16], blob[16:28], blob[28:-32], blob[-32:]
    mac = HMAC(_derive(key, salt, info + b"/mac"), hashes.SHA256())
    mac.update(aad + salt + nonce + ct)
    try:
        mac.verify(tag)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("完整性校验失败（密钥不匹配或文件被改动）") from exc
    return AESGCM(_derive(key, salt, info + b"/enc")).decrypt(nonce, ct, aad)


def _encrypt(key: bytes, mode: str, data: bytes, kind: str) -> bytes:
    fmt = FORMAT[mode]
    info = _record_info(mode, kind)
    if mode == "fast":
        return _enc_fast(key, data, fmt[f"{kind}_aad"])
    if mode == "secure":
        return _enc_secure(key, data, fmt[f"{kind}_aad"], info)
    return _enc_balanced(key, data, fmt[f"{kind}_aad"], info)


def _decrypt_with(key: bytes, mode: str, blob: bytes, kind: str) -> bytes:
    fmt = FORMAT[mode]
    info = _record_info(mode, kind)
    if mode == "fast":
        return _dec_fast(key, blob, fmt[f"{kind}_aad"])
    if mode == "secure":
        return _dec_secure(key, blob, fmt[f"{kind}_aad"], info)
    return _dec_balanced(key, blob, fmt[f"{kind}_aad"], info)


def _decrypt(key: bytes, mode: str, blob: bytes, kind: str) -> bytes:
    last: Exception | None = None
    for candidate in decrypt_keys():
        try:
            return _decrypt_with(candidate, mode, blob, kind)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise CryptoError("解密失败（密钥不匹配或文件被改动）") from last


# -------------------------------------------------------------- 行/文件封装
def encrypt_line(text: str) -> str:
    key = enc_key()
    if key is None:
        return text
    fmt = FORMAT[_MODE]
    return fmt["line"] + base64.b64encode(_encrypt(key, _MODE, text.encode("utf-8"), "line")).decode("ascii")


def decrypt_line(line: str) -> str:
    for mode in MODES:
        fmt = FORMAT[mode]
        if not line.startswith(fmt["line"]):
            continue
        key = enc_key()
        if key is None:
            raise CryptoError("日记已加密，但缺少密钥文件")
        blob = base64.b64decode(line[len(fmt["line"]):])
        return _decrypt(key, mode, blob, "line").decode("utf-8")
    for magic, _file_magic, algo in LEGACY:
        if not line.startswith(magic):
            continue
        key = enc_key()
        if key is None:
            raise CryptoError("日记已加密，但缺少密钥文件")
        blob = base64.b64decode(line[len(magic):])
        suite = AESGCMSIV if algo == "siv" else AESGCM
        if suite is None:
            raise CryptoError("旧格式需要 AES-GCM-SIV 支持，当前环境不可用")
        for candidate in decrypt_keys():
            try:
                return suite(candidate).decrypt(blob[:12], blob[12:], None).decode("utf-8")
            except Exception:  # noqa: BLE001
                continue
        raise CryptoError("旧格式解密失败（密钥不匹配或文件被改动）")
    return line


def encrypt_bytes(data: bytes) -> bytes:
    key = enc_key()
    if key is None:
        return data
    fmt = FORMAT[_MODE]
    return fmt["file"] + _encrypt(key, _MODE, data, "file")


def decrypt_bytes(data: bytes) -> bytes:
    for mode in MODES:
        fmt = FORMAT[mode]
        if not data.startswith(fmt["file"]):
            continue
        key = enc_key()
        if key is None:
            raise CryptoError("日记已加密，但缺少密钥文件")
        return _decrypt(key, mode, data[len(fmt["file"]):], "file")
    for _line_magic, file_magic, algo in LEGACY:
        if not data.startswith(file_magic):
            continue
        key = enc_key()
        if key is None:
            raise CryptoError("日记已加密，但缺少密钥文件")
        suite = AESGCMSIV if algo == "siv" else AESGCM
        if suite is None:
            raise CryptoError("旧格式需要 AES-GCM-SIV 支持，当前环境不可用")
        nonce = data[len(file_magic):len(file_magic) + 12]
        for candidate in decrypt_keys():
            try:
                return suite(candidate).decrypt(nonce, data[len(file_magic) + 12:], None)
            except Exception:  # noqa: BLE001
                continue
        raise CryptoError("旧格式解密失败（密钥不匹配或文件被改动）")
    return data


def is_encrypted_bytes(data: bytes) -> bool:
    if any(data.startswith(FORMAT[m]["file"]) for m in MODES):
        return True
    return any(data.startswith(m) for _l, m, _a in LEGACY)


# ---------------------------------------------------------------- 文件读写

def _cache_drop(path: str) -> None:
    """写入后让缓存失效；只影响内存，不影响磁盘数据。"""
    global _CACHE_BYTES
    hit = _TEXT_CACHE.pop(path, None)
    if hit is not None:
        _CACHE_BYTES -= len(hit[1])


def _cache_store(path: str, stat_key: tuple[int, int], text: str, encrypted: bool) -> None:
    """LRU 缓存解密后的文本：超出文件数或总字节时淘汰最久未用的条目。

    淘汰只发生在内存缓存里，磁盘文件不受影响；下次读取会重新解密。
    """
    global _CACHE_BYTES
    if len(text) > 4 * 1024 * 1024:
        return
    old = _TEXT_CACHE.pop(path, None)
    if old is not None:
        _CACHE_BYTES -= len(old[1])
    while _TEXT_CACHE and (len(_TEXT_CACHE) >= _CACHE_MAX_FILES
                           or _CACHE_BYTES + len(text) > _CACHE_MAX_BYTES):
        _, evicted = _TEXT_CACHE.popitem(last=False)
        _CACHE_BYTES -= len(evicted[1])
    _TEXT_CACHE[path] = (stat_key, text, encrypted)
    _CACHE_BYTES += len(text)


def cache_entries(limit: int = 50) -> list[dict]:
    """缓存里当前有哪些文件（按占用从大到小）。"""
    items = sorted(((path, len(value[1])) for path, value in _TEXT_CACHE.items()),
                   key=lambda item: -item[1])
    out = []
    for path, size in items[:limit]:
        try:
            name = os.path.relpath(path, DATA_DIR)
        except ValueError:
            name = os.path.basename(path)
        out.append({"name": name, "bytes": size})
    return out


def cache_by_scope() -> list[dict]:
    """按会话汇总缓存：同一个群/QQ 的多个日期文件算作一处。"""
    buckets: dict[str, dict] = {}
    for path, (_stat, text, _enc) in _TEXT_CACHE.items():
        if not os.path.exists(path):
            continue  # 文件已删除，不再计入
        try:
            rel = os.path.relpath(path, os.path.join(DATA_DIR, "memory5"))
        except ValueError:
            rel = os.path.basename(path)
        parts = rel.split(os.sep)
        if len(parts) >= 2 and (parts[0].startswith("group_") or parts[0].startswith("private_")):
            scope = parts[0].replace("_", ":", 1)
        else:
            scope = "公共文件"
        bucket = buckets.setdefault(scope, {"scope": scope, "files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += len(text)
    return sorted(buckets.values(), key=lambda b: -b["bytes"])


def cache_stats() -> dict:
    files = 0
    size = 0
    for path, (_stat, text, _enc) in _TEXT_CACHE.items():
        if os.path.exists(path):
            files += 1
            size += len(text)
    return {"files": files, "bytes": size,
            "max_files": _CACHE_MAX_FILES, "max_bytes": _CACHE_MAX_BYTES}


def _load_plain_text(path: str) -> tuple[str, bool]:
    """读取并解密成纯文本；按 (mtime, size) 命中缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return "", False
    stat_key = (st.st_mtime_ns, st.st_size)
    hit = _TEXT_CACHE.get(path)
    if hit and hit[0] == stat_key:
        _TEXT_CACHE.move_to_end(path)  # LRU：命中的挪到队尾
        return hit[1], hit[2]
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return "", False

    encrypted = False
    if is_encrypted_bytes(data):  # 整文件加密
        data = decrypt_bytes(data)
        encrypted = True
    text = data.decode("utf-8", "replace")

    if path.endswith(".jsonl"):
        # JSONL 是逐行加密，没有整文件 magic，需要看行前缀
        line_magics = tuple(fmt["line"] for fmt in FORMAT.values())
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(line.startswith(line_magics) for line in lines):
            text = "\n".join(decrypt_line(line) for line in lines) + ("\n" if lines else "")
            encrypted = True

    _cache_store(path, stat_key, text, encrypted)
    return text, encrypted

def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def read_text(path: str) -> str:
    return _load_plain_text(path)[0]


def write_text(path: str, text: str) -> None:
    global _PLAIN_WARNED
    ensure_dir(os.path.dirname(path))
    if text and enc_key() is None and not _PLAIN_WARNED:
        _PLAIN_WARNED = True
        logger.warning("memory5: 没有密钥，日记将以明文写入")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(encrypt_bytes(text.encode("utf-8")))
    os.replace(tmp, path)
    _cache_drop(path)


def read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    text, encrypted = _load_plain_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            if encrypted:
                # 加密文件里出现解不开的行，说明文件被改动，不能静默跳过
                raise CryptoError("有一行解密失败（文件被改动或密钥不匹配）") from exc
            continue
    return rows


def write_jsonl(path: str, rows: list[dict]) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(encrypt_line(json.dumps(row, ensure_ascii=False)) + "\n")
    os.replace(tmp, path)
    _cache_drop(path)


def append_jsonl_row(path: str, row: dict) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(encrypt_line(json.dumps(row, ensure_ascii=False)) + "\n")
    _cache_drop(path)


def read_json(path: str, default=None):
    try:
        return json.loads(read_text(path))
    except ValueError:
        return default


def write_json(path: str, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ 迁移
def encrypt_tree(root: str, force: bool = False) -> int:
    """把 root 下的记忆文件统一迁移到当前档位。

    返回迁移文件数。任何一个文件解密失败都会整份跳过并记日志，绝不部分重写。
    """
    if enc_key() is None:
        return 0
    fmt_file = FORMAT[_MODE]["file"]
    fmt_line = FORMAT[_MODE]["line"]
    changed = 0
    for base, dirs, files in os.walk(root):
        if os.path.basename(base) == "media":
            dirs[:] = []
            continue
        for name in files:
            if name in PLAIN_FILES:  # 给宿主机脚本读的明文文件，不参与迁移
                continue
            if not name.endswith((".md", ".json", ".jsonl")):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if not data:
                continue
            try:
                if name.endswith(".jsonl"):
                    text = data.decode("utf-8", "replace")
                    lines = [line.strip() for line in text.splitlines() if line.strip()]
                    if not force and lines and all(line.startswith(fmt_line) for line in lines):
                        continue
                    rows = [json.loads(decrypt_line(line)) for line in lines]
                    write_jsonl(path, rows)
                else:
                    if not force and data.startswith(fmt_file):
                        continue
                    write_text(path, decrypt_bytes(data).decode("utf-8", "replace"))
            except (CryptoError, ValueError) as exc:
                logger.warning(f"memory5: 跳过 {os.path.basename(path)}（{type(exc).__name__}）")
                continue
            changed += 1
    return changed
