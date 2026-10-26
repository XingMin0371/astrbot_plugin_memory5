"""memory5 v2 — 分层记忆（重要/提醒/普通/低密度/无效），按会话隔离。

设计要点：
- 存储按 scope 分目录：group_群号 / private_QQ号
- 增量抽取（默认每 12 小时），处理积压消息
- 注入按 核心 → 重要 → 提醒 → 普通（匹配 + 随机）→ 低密度 分层，带总预算
- 提醒由插件自己按到期时间分多次发送
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import random
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record, Video
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:  # 包内正常加载
    from .crypto import (
        DATA_DIR,
        MEM_ROOT,
        PLUGIN_DATA_DIR,
        migrate_legacy_layout,
        KEY_PATH,
        CryptoError,
        MODE_LABELS,
        append_jsonl_row,
        crypto_mode,
        crypto_suite,
        crypto_version,
        enc_key,
        ensure_key,
        encrypt_tree,
        generate_new_key,
        key_age_seconds,
        rotate_key,
        key_age_seconds,
        rotate_key,
        ensure_dir,
        read_json,
        read_jsonl,
        read_text,
        set_crypto_mode,
        write_json,
        write_jsonl,
        write_text,
    )
except ImportError:  # 单文件方式加载（本地测试）
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from crypto import (  # type: ignore[no-redef]
        DATA_DIR,
        MEM_ROOT,
        PLUGIN_DATA_DIR,
        migrate_legacy_layout,
        KEY_PATH,
        CryptoError,
        MODE_LABELS,
        append_jsonl_row,
        crypto_mode,
        crypto_suite,
        crypto_version,
        enc_key,
        ensure_key,
        encrypt_tree,
        generate_new_key,
        key_age_seconds,
        rotate_key,
        key_age_seconds,
        rotate_key,
        ensure_dir,
        read_json,
        read_jsonl,
        read_text,
        set_crypto_mode,
        write_json,
        write_jsonl,
        write_text,
    )



def _umos_path() -> str:
    return os.path.join(MEM_ROOT, "umos.json")


def load_umos() -> dict:
    data = read_json(_umos_path(), {})
    return data if isinstance(data, dict) else {}


def record_umo(scope: str, umo: str) -> None:
    if not umo or not scope:
        return
    data = load_umos()
    if data.get(scope) != umo:
        data[scope] = umo
        write_plain_json(_umos_path(), data)

_SYSTEM_MARKERS = ("已确认授权", "管理员授权验证", "管理员权限已移除", "已确认授权：")


def encrypt_existing_storage() -> int:
    """把已有记忆迁移到当前加密档位。"""
    return encrypt_tree(MEM_ROOT)


# 面板模块（依赖 crypto，不反向依赖 main，因此可以直接导入）
try:
    from .webui import Memory5WebUI, purge_old_trash
except Exception as exc:  # pragma: no cover - 面板不可用时主体仍可跑
    logger.warning(f"memory5: 面板模块加载失败 {type(exc).__name__}: {exc}")
    Memory5WebUI = None  # type: ignore[assignment]
    purge_old_trash = None  # type: ignore[assignment]


try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]

DB_PATH = os.path.join(DATA_DIR, "data_v4.db")
TZ = timezone(timedelta(hours=8))


_SCOPE_RE = re.compile(r"^(group|private):[A-Za-z0-9_-]{1,64}$")


def scope_dir(scope: str) -> str:
    """group:123 -> <MEM_ROOT>/group_123。"""
    if not _SCOPE_RE.match(str(scope)):
        raise ValueError("非法 scope")
    return os.path.join(MEM_ROOT, str(scope).replace(":", "_"))


def encrypt_existing_storage() -> int:
    """把已有记忆迁移到当前加密档位。"""
    return encrypt_tree(MEM_ROOT)

VERSION = "2.1.12beta1"

TAGS = ("重要", "提醒", "普通", "低密度", "无效")
DEFAULT_HEADER = "【以下记忆是用第三人称写的，是你自己的日记，不是别人】"
DEFAULT_BLOCKED = ["习近平", "法轮功", "六四", "色情", "裸聊", "约炮", "毒品", "冰毒", "赌博", "枪支", "暴恐", "自杀"]


def _to_int(value, default: int) -> int:
    """评分字段转整数；0 是合法值（信息量为 0），不能当空值。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# 去重优先级：数字越大越优先保留；提醒不因别处重复而被删
DEDUP_RANK = {"core": 5, "remind": 4, "重要": 3, "普通": 2, "低密度": 1}
CORE_LINE_RE = re.compile(
    r"^- \[(?P<topic>[^\]]*)\] (?P<subject>\S+) :: (?P<text>.*?)（更新 (?P<date>[^）]*)）\s*$"
)


def _norm_text(text: str) -> str:
    return re.sub(r"[\s，。！？、：；,.!?;:~～·…（）()\[\]【】\"'“”‘’\-—_]", "", str(text or ""))


def _similar(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 6 and len(nb) >= 6 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.68


def renumber_scope(scope: str) -> int:
    """把各文件的序号整理成 1..N，避免删除/新增后出现大间隔。"""
    paths = scope_paths(scope)
    changed = 0
    for key in ("important", "remind"):
        rows = read_jsonl(paths[key])
        if not rows:
            continue
        rows.sort(key=lambda r: (str(r.get("date", "")), int(r.get("seq") or 0)))
        dirty = False
        for index, row in enumerate(rows, 1):
            if int(row.get("seq") or 0) != index:
                row["seq"] = index
                dirty = True
        if dirty:
            write_jsonl(paths[key], rows)
            changed += len(rows)
    daily_dir = paths["daily_dir"]
    if os.path.isdir(daily_dir):
        for name in os.listdir(daily_dir):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(daily_dir, name)
            rows = read_jsonl(path)
            if not rows:
                continue
            rows.sort(key=lambda r: int(r.get("seq") or 0))
            dirty = False
            for index, row in enumerate(rows, 1):
                if int(row.get("seq") or 0) != index:
                    row["seq"] = index
                    dirty = True
            if dirty:
                write_jsonl(path, rows)
                changed += len(rows)
    return changed


def dedupe_scope(scope: str, dry_run: bool = False) -> list[dict]:
    """同一件事只留最高优先级的条目；提醒不会被别处的重复挤掉。"""
    paths = scope_paths(scope)
    core_lines = [line.rstrip("\n") for line in read_text(paths["core"]).splitlines()]

    entries: list[dict] = []
    for line_no, line in enumerate(core_lines):
        matched = CORE_LINE_RE.match(line.strip())
        if matched:
            entries.append({
                "tier": "core", "line_no": line_no,
                "subject": matched.group("subject"), "topic": matched.group("topic"),
                "text": matched.group("text"), "line": line,
            })

    important = read_jsonl(paths["important"])
    for idx, row in enumerate(important):
        entries.append({
            "tier": "重要", "index": idx, "subject": str(row.get("subject", "")),
            "topic": str(row.get("topic", "")), "text": str(row.get("text", "")), "row": row,
        })

    remind = read_jsonl(paths["remind"])
    # 提醒内部去重：同一 QQ + 同一到期分钟 + 相似内容 只留一条
    deduped_remind: list[dict] = []
    for row in remind:
        duplicated = False
        for kept in deduped_remind:
            if (
                str(row.get("subject")) == str(kept.get("subject"))
                and str(row.get("due", ""))[:16] == str(kept.get("due", ""))[:16]
                and _similar(row.get("text", ""), kept.get("text", ""))
            ):
                duplicated = True
                break
        if not duplicated:
            deduped_remind.append(row)
    remind_dupes = len(remind) - len(deduped_remind)
    remind = deduped_remind
    for idx, row in enumerate(remind):
        entries.append({
            "tier": "remind", "index": idx, "subject": str(row.get("subject", "")),
            "topic": str(row.get("topic", "")), "text": str(row.get("text", "")), "row": row,
        })

    daily_dir = paths["daily_dir"]
    daily_files = sorted(
        os.path.join(daily_dir, name)
        for name in (os.listdir(daily_dir) if os.path.isdir(daily_dir) else [])
        if name.endswith(".jsonl")
    )
    daily_rows: dict[str, list[dict]] = {}
    for path in daily_files:
        rows = read_jsonl(path)
        daily_rows[path] = rows
        for idx, row in enumerate(rows):
            entries.append({
                "tier": str(row.get("tag") or "普通"), "index": idx, "file": path,
                "subject": str(row.get("subject", "")), "topic": str(row.get("topic", "")),
                "text": str(row.get("text", "")), "row": row,
            })

    entries.sort(key=lambda e: DEDUP_RANK.get(e["tier"], 0), reverse=True)
    winners: list[dict] = []
    dropped: list[dict] = []
    for entry in entries:
        conflict = None
        for keep in winners:
            if str(entry["subject"]) != str(keep["subject"]):
                continue
            if {entry["tier"], keep["tier"]} == {"core", "remind"}:
                continue  # 长期和提醒是两种用途，都留
            if _similar(entry["text"], keep["text"]):
                conflict = keep
                break
        if conflict and entry["tier"] != "remind":
            dropped.append({
                "tier": entry["tier"], "subject": entry["subject"],
                "topic": entry["topic"], "text": entry["text"],
            })
            continue
        winners.append(entry)

    if dry_run:
        return dropped

    keep_lines = {e["line_no"] for e in winners if e["tier"] == "core"}
    keep_imp = {e["index"] for e in winners if e["tier"] == "重要"}
    keep_rem = {e["index"] for e in winners if e["tier"] == "remind"}
    keep_daily: dict[str, set[int]] = {}
    for e in winners:
        if e["tier"] in ("普通", "低密度"):
            keep_daily.setdefault(e["file"], set()).add(e["index"])

    if len(keep_lines) != len(core_lines):
        write_text(paths["core"], "\n".join(line for i, line in enumerate(core_lines) if i in keep_lines)
                   + ("\n" if keep_lines else ""))
    if len(keep_imp) != len(important):
        write_jsonl(paths["important"], [r for i, r in enumerate(important) if i in keep_imp])
    if len(keep_rem) != len(remind):
        write_jsonl(paths["remind"], [r for i, r in enumerate(remind) if i in keep_rem])
    for path, rows in daily_rows.items():
        keep = keep_daily.get(path, set())
        if len(keep) != len(rows):
            write_jsonl(path, [r for i, r in enumerate(rows) if i in keep])
    if remind_dupes:
        logger.info(f"memory5: {scope} 提醒内部去重 {remind_dupes} 条")
    return dropped


RELATIVE_TIME_RE = re.compile(
    r"\d+\s*(?:分钟|小时)(?:之?后)|(?:等会儿?|一会儿|稍后|过会儿?)"
)
RELATIVE_DAY_RE = re.compile(r"(?:明天|后天|大后天)")
RELATIVE_EVENING_RE = re.compile(r"(?:今晚|明晚)")
RELATIVE_DAY_OFFSET_RE = re.compile(
    r"(?:\d+|[一两二三四五六七八九十]+)\s*(?:天|周|个?月|半个月)(?:之?后)"
)


def absolutize_time(text: str, due: datetime | None) -> str:
    """把“20分钟后/3天后/明天/今晚”这类相对说法换成具体时间，避免日记越看越糊涂。"""
    if not due or not text:
        return text
    same_day = due.strftime("%Y-%m-%d") == now().strftime("%Y-%m-%d")
    stamp = due.strftime("%H:%M") if same_day else due.strftime("%m-%d %H:%M")
    day = due.strftime("%m-%d")
    text = RELATIVE_TIME_RE.sub(stamp, text)
    text = RELATIVE_DAY_OFFSET_RE.sub(day, text)
    text = RELATIVE_EVENING_RE.sub(f"{day}晚上", text)
    return RELATIVE_DAY_RE.sub(day, text)


def now() -> datetime:
    return datetime.now(TZ)


def day_str(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d")


def parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def scope_paths(scope: str) -> dict:
    base = scope_dir(scope)
    return {
        "base": base,
        "core": os.path.join(base, "core.md"),
        "important": os.path.join(base, "important.jsonl"),
        "remind": os.path.join(base, "remind.jsonl"),
        "daily_dir": os.path.join(base, "daily"),
        "aliases": os.path.join(base, "aliases.json"),
        "cursor": os.path.join(base, "cursor.json"),
        "trash": os.path.join(MEM_ROOT, "trash"),
    }


def daily_file(scope: str, day: str) -> str:
    return os.path.join(scope_paths(scope)["daily_dir"], f"{day}.jsonl")


def append_entries(scope: str, entries: list[dict]) -> None:
    """按 (subject, topic, tag) 去重：新的覆盖旧的。"""
    if not entries:
        return
    paths = scope_paths(scope)
    by_day: dict[str, list[dict]] = {}
    for entry in entries:
        by_day.setdefault(entry["date"], []).append(entry)

    for day, items in by_day.items():
        path = daily_file(scope, day)
        existing = read_jsonl(path)
        seq = max([int(r.get("seq", 0) or 0) for r in existing] + [0]) + 1
        for item in items:
            if not item.get("seq"):
                item["seq"] = seq
                seq += 1
            key = (item["subject"], item["topic"], item["tag"])
            existing = [
                e for e in existing
                if (e.get("subject"), e.get("topic"), e.get("tag")) != key
            ]
            existing.append(item)
        existing.sort(key=lambda e: (e.get("date", ""), e.get("seq", 0)))
        write_jsonl(path, existing)


REPEAT_CHOICES = ("", "daily", "weekly", "weekdays")


def next_repeat_due(due: datetime, repeat: str) -> datetime | None:
    """按重复规则算出下一次到期时间；不重复返回 None。"""
    if repeat == "daily":
        return due + timedelta(days=1)
    if repeat == "weekly":
        return due + timedelta(days=7)
    if repeat == "weekdays":
        nxt = due + timedelta(days=1)
        while nxt.weekday() >= 5:  # 跳过周六周日
            nxt += timedelta(days=1)
        return nxt
    return None


def write_plain_json(path: str, data) -> None:
    """不加密的小 JSON（备份策略、校验结果），方便宿主机脚本直接读。"""
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def refresh_index(scope: str) -> dict:
    """刷新该会话的轻量索引（只统计数量，不参与加解密）。"""
    paths = scope_paths(scope)
    daily_dir = paths["daily_dir"]
    daily = {}
    if os.path.isdir(daily_dir):
        for name in os.listdir(daily_dir):
            if name.endswith(".jsonl"):
                daily[name[:-6]] = len(read_jsonl(os.path.join(daily_dir, name)))
    index = {
        "updated": now().isoformat(timespec="seconds"),
        "core": len([ln for ln in read_text(paths["core"]).splitlines() if ln.strip()]),
        "important": len(read_jsonl(paths["important"])),
        "remind": len(read_jsonl(paths["remind"])),
        "daily": daily,
        "total": sum(daily.values()),
    }
    try:
        write_json(os.path.join(paths["base"], "index.json"), index)
    except OSError:
        pass
    return index


# --------------------------------------------------------------- aliases
def aliases_path(scope: str) -> str:
    return scope_paths(scope)["aliases"]


def load_aliases(scope: str) -> dict:
    return read_json(aliases_path(scope), {})


def update_aliases(scope: str, pairs: list[dict]) -> None:
    data = load_aliases(scope)
    changed = False
    for pair in pairs or []:
        qq = str(pair.get("qq", "")).strip()
        alias = str(pair.get("alias", "")).strip()
        if not qq.isdigit() or not alias or len(alias) > 12:
            continue
        # 防投毒：带指令感的话术不收
        if re.search(r"记住|你叫|你是|以后叫|必须叫|听好", alias):
            continue
        source = str(pair.get("source", "other"))
        conf = 0.9 if source == "self" else 0.6
        bucket = data.setdefault(qq, [])
        found = next((b for b in bucket if b.get("alias") == alias), None)
        if found:
            found["last_seen"] = day_str()
            found["confidence"] = max(found.get("confidence", 0), conf)
        else:
            bucket.append(
                {"alias": alias, "source": source, "confidence": conf,
                 "first_seen": day_str(), "last_seen": day_str()}
            )
        changed = True
    if changed:
        write_json(aliases_path(scope), data)


# ------------------------------------------------------------- desensitize
def load_blocked_index(extra: list[str] | None = None) -> dict[str, list[str]]:
    """加载内置词库（houbb/sensitive-word 的 2~4 字中文子集）+ 额外词，按首字建索引。"""
    words: set[str] = set()
    path = os.path.join(os.path.dirname(__file__), "blocked_words.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                w = line.strip()
                if w:
                    words.add(w)
    except OSError as exc:
        logger.warning(f"memory5: 读取内置屏蔽词失败 {exc}")
    for w in extra or []:
        w = str(w).strip()
        if w:
            words.add(w)
    index: dict[str, list[str]] = {}
    for w in sorted(words, key=len, reverse=True):
        index.setdefault(w[0], []).append(w)
    return index


def desensitize(text: str, index: dict[str, list[str]]) -> tuple[str, bool]:
    if not text or not index:
        return text, False
    out: list[str] = []
    hit = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        matched = None
        for word in index.get(ch, ()):
            if text.startswith(word, i):
                matched = word
                break
        if matched:
            out.append("[已屏蔽]")
            i += len(matched)
            hit = True
        else:
            out.append(ch)
            i += 1
    return "".join(out), hit


# --------------------------------------------------------------- classify
def classify(stability: int, impact: int, info: int, due: datetime | None) -> str:
    if due is not None:
        return "提醒"
    if stability >= 3 and impact >= 2 and info >= 2:
        return "重要"
    if info <= 1 and impact <= 2:
        return "低密度"
    return "普通"

# ------------------------------------------------------------- extraction
def extract_text(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except Exception:
        return str(raw)[:200]
    items = obj.get("message") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "plain":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "at":
            qq = item.get("qq") or item.get("user_id") or item.get("target")
            if qq:
                parts.append(f"@{qq}")
    return " ".join(p for p in parts if p).strip()


def raw_dir(scope: str) -> str:
    return os.path.join(scope_dir(scope), "raw")


def append_raw(scope: str, qq: str, name: str, text: str) -> None:
    text = (text or "").replace("\n", " ").strip()[:300]
    if not text:
        return
    day = day_str()
    path = os.path.join(raw_dir(scope), f"{day}.jsonl")
    row = {"ts": now().isoformat(timespec="seconds"), "qq": str(qq), "name": str(name)[:32], "text": text}
    append_jsonl_row(path, row)


def raw_since(scope: str, cursor: dict, limit: int = 200) -> list[dict]:
    cutoff = cursor.get("last_ts", "")
    merge_from_db(scope, cutoff)
    rows: list[dict] = []
    base = raw_dir(scope)
    if not os.path.isdir(base):
        return rows
    # 读全部 raw 文件：积压跨多天时，只读最近两天会漏掉更早的消息
    for name in sorted(os.listdir(base)):
        if not name.endswith(".jsonl"):
            continue
        for row in read_jsonl(os.path.join(base, name)):
            if str(row.get("ts", "")) > cutoff:
                rows.append(row)
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows[:limit]



def _parse_model_items(raw: str) -> list[dict]:
    """尽量从不同模型的输出里抠出条目数组：纯 JSON、代码块、包在对象里都试。"""
    text = str(raw or "").strip()
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    candidates = [text]
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        candidates.append(match.group(0))
    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("items", "entries", "memories", "data", "list"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            return [data]
    return []

def merge_from_db(scope: str, since_ts: str) -> int:
    """群聊补一份 AstrBot 历史记录：适配器不上报机器人自己的消息，靠这个补齐。
    顺手把没采集到的消息写进 raw，保证下次游标推进后不丢。"""
    if not scope.startswith("group:"):
        return 0
    group_id = scope.split(":", 1)[1]
    if not group_id.isdigit():
        return 0
    since_utc = ""
    if since_ts:
        try:
            dt = datetime.fromisoformat(since_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            since_utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            since_utc = ""
    try:
        db_uri = f"file:{DB_PATH}?mode=ro"
        if os.name == "nt":
            db_uri = "file:/" + DB_PATH.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(db_uri, uri=True, timeout=10)
        cur = con.cursor()
        cur.execute(
            "SELECT created_at, sender_id, sender_name, content FROM platform_message_history "
            "WHERE user_id LIKE ? AND created_at > ? ORDER BY created_at LIMIT 500",
            (f"%GroupMessage:{group_id}", since_utc),
        )
        records = cur.fetchall()
        con.close()
    except Exception as exc:  # pragma: no cover
        logger.debug(f"memory5: 读历史失败 {type(exc).__name__}: {exc}")
        return 0

    if not records:
        return 0
    base = raw_dir(scope)
    existing = set()
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.endswith(".jsonl"):
                for row in read_jsonl(os.path.join(base, name)):
                    existing.add((str(row.get("ts")), str(row.get("qq")), str(row.get("text"))))

    added = 0
    near: dict[tuple, list[str]] = {}
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.endswith(".jsonl"):
                for row in read_jsonl(os.path.join(base, name)):
                    near.setdefault((str(row.get("ts")), str(row.get("qq"))), []).append(str(row.get("text")))

    def is_near_dup(ts: str, qq: str, text: str) -> bool:
        for other in near.get((ts, qq), ()):
            if other == text or other.endswith(text) or text.endswith(other):
                return True
        return False

    skip_marks = (
        "[Image]", "[图片]", "[表情]", "[CQ:image", "[CQ:face",
        "的记忆（共", "的记忆：没有记录", "记忆只对管理员开放",
        "长期记忆只在后台面板", "只能查自己的记忆", "级别只能是",
    )
    for created, sid, sname, content in records:
        text = extract_text(content).replace("\n", " ").strip()[:300]
        text = re.sub(r"@None\s*", "", text).strip()
        if not text or any(mark in text for mark in skip_marks):
            continue
        if text.startswith("/记忆查看"):
            continue
        try:
            dt = datetime.fromisoformat(str(created)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        local = dt.astimezone(timezone(timedelta(hours=8)))
        ts = local.isoformat(timespec="seconds")
        key = (ts, str(sid), text)
        if key in existing or is_near_dup(ts, str(sid), text):
            continue
        ensure_dir(base)
        append_jsonl_row(
            os.path.join(base, f"{local.strftime('%Y-%m-%d')}.jsonl"),
            {"ts": ts, "qq": str(sid), "name": str(sname or "")[:32], "text": text},
        )
        existing.add(key)
        added += 1
    return added



EXTRACT_PROMPT = """你是记忆整理器。下面是一段 QQ 群/私聊的新消息。请从中提取值得长期记住的信息。

对每一条输出一个 JSON 对象，字段：
- subject: 事件主体的 QQ 号（纯数字）
- topic: 2~6 个字的事件主题（如 称呼、作息、喜好、约定、关系、事件）
- stability: 1=瞬时, 2=中期, 3=长期
- impact: 1=低, 2=中, 3=高（对以后相处的影响）
- info: 0=没有信息, 1=低, 2=中, 3=高
- due: 如果有时效性（约定在某天做某事），填 ISO 时间 "YYYY-MM-DDTHH:MM:SS"，否则 null
- text: 一条记忆句子，口语备忘风格，主谓宾完整，不超过 40 字
- aliases: 可选的别名列表，形如 [{"qq":"123","alias":"外号","source":"self或other"}]

text 的写法（很重要）：
- 像群友随手记的备忘，用平常说话的口吻，别写成公文或报告。
- 主体要能看出来是谁（写名字或 QQ 号），不要只写“他/它”。
- 提到时间时必须换成具体时间点，不要用相对说法：
  “20分钟后”→写“12:11”；“明天”→写“10-26”；“今晚八点”→写“20:00”。
  禁止出现“几分钟后/半小时后/过会儿/明天/后天”这种。
- 例：「某某(123456)只吃主人投喂的白饭」「某某说 10-26 早点睡，别熬夜」
- 不要用“称/要求/表示”这类书面词，不要复述原话，不要堆形容词。

规则：
- 寒暄、复读、表情、无意义内容不要输出。
- 主体必须能落到具体 QQ，落不到的不要输出。
- 只输出 JSON 数组，不要任何解释。

消息：
"""


# ------------------------------------------------------------------- plugin
@register("memory5", "DSH", "鱼鱼日记 · 如鱼所书：分层日记 + 面板 + 提醒 + 定时任务", "2.1.12beta1")
class Memory5(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._flat = self._flatten(self.config)
        self._cache: dict[str, tuple[float, str]] = {}
        self._daily_cache: dict[str, tuple[float, list]] = {}
        self._blocked_index: dict[str, list[str]] | None = None
        self._archiving = False
        self._suppress: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._extract_task: asyncio.Task | None = None

    # ------------------------------------------------------------- config
    @staticmethod
    def _flatten(data, prefix: str = "") -> dict:
        out: dict = {}
        if not isinstance(data, dict):
            return out
        for key, value in data.items():
            if isinstance(value, dict) and not ("type" in value and "default" in value):
                out.update(Memory5._flatten(value))
            else:
                out[key] = value
        return out

    def _get(self, key, default):
        try:
            config = self.config or {}
            if key in config:
                value = config.get(key)
            else:
                value = self._flat.get(key, default)
        except Exception:
            return default
        return default if value is None or value == "" else value

    def _int(self, key, default, low=1):
        try:
            return max(low, int(self._get(key, default)))
        except (TypeError, ValueError):
            return default

    def _int_raw(self, key, default):
        """不夹逼的整数读取：允许 0（0 表示不限制）。"""
        try:
            return max(0, int(self._get(key, default)))
        except (TypeError, ValueError):
            return default

    def _classify(self, stability: int, impact: int, info: int, due: datetime | None) -> str:
        if due is not None:
            return "提醒"
        if (
            stability >= self._int("important_stability", self._int("th_important_stability", 3))
            and impact >= self._int("important_impact", self._int("th_important_impact", 2))
            and info >= self._int("important_info", self._int("th_important_info", 2))
        ):
            return "重要"
        if (
            info <= self._int("low_info", self._int("th_low_info", 1))
            and impact <= self._int("low_impact", self._int("th_low_impact", 2))
        ):
            return "低密度"
        return "普通"

    @property
    def enabled(self) -> bool:
        return bool(self._get("enable", True))

    @property
    def enabled_archive(self) -> bool:
        return self.enabled and bool(self._get("enable_archive", True))

    @property
    def enabled_inject(self) -> bool:
        return self.enabled and bool(self._get("enable_inject", True))

    @property
    def scopes(self) -> list[str]:
        raw = self._get("write_scopes", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return [str(x) for x in raw]

    @property
    def blocked(self) -> list[str]:
        raw = self._get("extra_blocked_words", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return [str(x) for x in raw if str(x).strip()]

    def blocked_index(self) -> dict[str, list[str]]:
        if self._blocked_index is None:
            self._blocked_index = load_blocked_index(self.blocked)
        return self._blocked_index

    @property
    def keep_core(self) -> int:
        return self._int("core_max_chars", 2000, 300)

    @property
    def budget(self) -> int:
        return self._int("inject_budget_chars", 1800, 300)

    @property
    def normal_budget(self) -> int:
        return self._int_raw("normal_budget_chars", 1200)

    def remind_times(self) -> list[tuple[int, int]]:
        raw = str(self._get("remind_times", "08:00,20:00"))
        out = []
        for piece in re.split(r"[,，]", raw):
            try:
                hh, mm = piece.strip().split(":")[:2]
                out.append((int(hh), int(mm)))
            except (ValueError, AttributeError):
                continue
        return out or [(8, 0), (20, 0)]


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def admin_auth_listener(self, event: AstrMessageEvent) -> None:
        await self._admin_auth_listener(event)

    # -------------------------------------------------------------- scope
    @staticmethod
    def scope_of(event: AstrMessageEvent) -> str | None:
        umo = getattr(event, "unified_msg_origin", "") or ""
        parts = umo.split(":")
        if len(parts) >= 3:
            kind = parts[1]
            if kind == "GroupMessage":
                scope = f"group:{parts[2]}"
            elif kind == "FriendMessage":
                scope = f"private:{event.get_sender_id()}"
            else:
                return None
            record_umo(scope, umo)
            return scope
        return None

    # ------------------------------------------------------------ collector
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def collect(self, event: AstrMessageEvent) -> None:
        """把白名单会话里的消息落一份，供后续抽取。
        规则：鱼鱼自己的正常聊天要记；命令输出和鱼鱼发的图片不记；用户发的图片也不记原图。"""
        try:
            if not self.enabled_archive:
                return
            scope = self.scope_of(event)
            if not scope or scope not in self.scopes:
                return
            text = (event.message_str or "").strip()
            if any(marker in text for marker in _SYSTEM_MARKERS):
                return
            sender = str(event.get_sender_id())
            self_ids = {str(x) for x in self.bot_qqs()}
            try:
                self_id = str(event.get_self_id() or "")
                if self_id:
                    self_ids.add(self_id)
            except Exception:
                pass

            if sender in self_ids:
                # 鱼鱼自己：没有文字（纯图片/表情）就不记
                if not text:
                    return
                # /记忆查看 的输出不记
                if text in self._suppress:
                    return
                append_raw(scope, sender, str(event.get_sender_name() or ""), text)
                return

            # 其他人：图片不记原图，等后续记鱼鱼的回复
            if any(isinstance(c, Image) for c in (event.get_messages() or [])):
                return
            if sender in self.blacklist_qqs():
                return
            if text.startswith("/记忆查看") or text.startswith("记忆查看"):
                return
            append_raw(scope, sender, str(event.get_sender_name() or ""), text)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"memory5: 采集跳过 {type(exc).__name__}: {exc}")

    @filter.on_llm_response()
    async def record_image_reply(self, event: AstrMessageEvent, response) -> None:
        """图片消息不记原图；鱼鱼的回复会作为它自己的聊天被采集，这里不再重复写。"""
        return

    # --------------------------------------------------------- core memory
    def _core_text(self, scope: str, limit: int | None = None) -> str:
        text = read_text(scope_paths(scope)["core"]).strip()
        limit = limit or self.keep_core
        return text[-limit:] if text else ""

    def _write_core(self, scope: str, entry: dict) -> None:
        path = scope_paths(scope)["core"]
        lines = [ln.rstrip() for ln in read_text(path).splitlines() if ln.strip()]
        key = f"[{entry['topic']}] {entry['subject']}"
        lines = [ln for ln in lines if not ln.startswith(f"- {key}")]
        lines.append(f"- {key} :: {entry['text']}（更新 {entry['date']}）")
        body = "\n".join(lines)
        while len(body) > self.keep_core and len(lines) > 1:
            lines.pop(0)
            body = "\n".join(lines)
        write_text(path, body + "\n")

    def _write_core_many(self, scope: str, entries: list[dict]) -> None:
        """批量写入长期记忆：整份文件只读写一次。"""
        if not entries:
            return
        path = scope_paths(scope)["core"]
        lines = [ln.rstrip() for ln in read_text(path).splitlines() if ln.strip()]
        for entry in entries:
            key = f"[{entry['topic']}] {entry['subject']}"
            lines = [ln for ln in lines if not ln.startswith(f"- {key}")]
            lines.append(f"- {key} :: {entry['text']}（更新 {entry['date']}）")
        body = "\n".join(lines)
        while len(body) > self.keep_core and len(lines) > 1:
            lines.pop(0)
            body = "\n".join(lines)
        write_text(path, body + "\n")

    # ---------------------------------------------------------- injection
    def _load_daily(self, scope: str, days: int) -> list[dict]:
        base = scope_paths(scope)["daily_dir"]
        if not os.path.isdir(base):
            return []
        cutoff = (now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows: list[dict] = []
        for name in sorted(os.listdir(base)):
            if not name.endswith(".jsonl"):
                continue
            day = name[:-6]
            if day < cutoff:
                continue
            path = os.path.join(base, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = self._daily_cache.get(path)
            if cached and cached[0] == mtime:
                rows.extend(cached[1])
                continue
            data = read_jsonl(path)
            self._daily_cache[path] = (mtime, data)
            rows.extend(data)
        return rows

    @staticmethod
    def _mentions(text: str) -> set[str]:
        return set(re.findall(r"\d{5,12}", text or ""))

    def _match(self, entry: dict, text: str, sender: str) -> bool:
        if str(entry.get("subject")) == str(sender):
            return True
        if str(entry.get("subject")) in self._mentions(text):
            return True
        topic = str(entry.get("topic", ""))
        return bool(topic and topic in text)

    def build_block(self, scope: str, sender: str, text: str) -> str:
        chunks: list[str] = []
        seen: list[str] = []

        def is_dup(content: str) -> bool:
            """注入去重：完全相同或互相包含（长度≥8）就算重复。"""
            key = _norm_text(content)
            if not key:
                return True
            for old in seen:
                if key == old:
                    return True
                if len(key) >= 12 and len(old) >= 12 and (key in old or old in key):
                    return True
            seen.append(key)
            return False

        core = self._core_text(scope)
        if core:
            for line in core.splitlines():
                matched = CORE_LINE_RE.match(line.strip())
                is_dup(matched.group("text") if matched else line)
            chunks.append("【长期记忆】\n" + core)

        # 跨 scope 共享：只取能对上 QQ 的身份/称呼（各≤5），关系不限
        relevant = {str(sender)} | self._mentions(text)
        shared_identity: list[str] = []
        shared_callname: list[str] = []
        shared_relation: list[str] = []
        for other in set(self.scopes):
            if other == scope:
                continue
            for line in self._core_text(other).splitlines():
                matched = CORE_LINE_RE.match(line.strip())
                if not matched:
                    continue
                if matched.group("subject") not in relevant:
                    continue
                topic = matched.group("topic")
                if is_dup(matched.group("text")):
                    continue
                if topic == "身份":
                    shared_identity.append(line)
                elif topic == "称呼":
                    shared_callname.append(line)
                elif topic == "关系":
                    shared_relation.append(line)
        shared = shared_identity[: self._int("identity_max", 5)] + shared_callname[: self._int("callname_max", 5)]
        relation_budget = self._int("shared_relation_max_chars", 600)
        relation_lines: list[str] = []
        used_chars = 0
        for line in shared_relation:
            if used_chars + len(line) > relation_budget:
                break
            relation_lines.append(line)
            used_chars += len(line)
        shared += relation_lines
        if shared:
            chunks.append("【身份（共享）】\n" + "\n".join(shared))

        # 重要：匹配当前消息的条目全放（总量由预算兜底），已在长期记忆里的跳过
        important = read_jsonl(scope_paths(scope)["important"])
        picked = [e for e in important if self._match(e, text, sender) and not is_dup(str(e.get("text", "")))]
        important_cap = self._int_raw("important_max_chars", 0)
        if important_cap > 0:
            limited, used_chars = [], 0
            for e in picked:
                cost = len(str(e.get("text", ""))) + 3
                if used_chars + cost > important_cap:
                    break
                limited.append(e)
                used_chars += cost
            picked = limited
        if picked:
            chunks.append("【重要】\n" + "\n".join(f"- {e['text']}" for e in picked))

        # 提醒：只在触发窗口前后注入，且每个 QQ 最多 5 条
        remind_by_qq: dict[str, list[str]] = {}
        for sc in set(self.scopes) | {scope}:
            for e in read_jsonl(scope_paths(sc)["remind"]):
                due = parse_dt(e.get("due", ""))
                if not due:
                    continue
                start, end = self._remind_window(due)
                if not (start <= now() <= end):
                    continue
                qq = str(e.get("subject", ""))
                if is_dup(str(e.get("text", ""))):
                    continue
                remind_by_qq.setdefault(qq, []).append(f"- {due.strftime('%m-%d %H:%M')} {e['text']}")
        # 提醒：只按条数限制（每个 QQ 最多 N 条），不再做字符预算
        remind_chunk = ""
        if remind_by_qq:
            per_qq = self._int("remind_max_per_qq", 5)
            picked_lines: list[str] = []
            for qq, items in remind_by_qq.items():
                picked_lines.extend(items[:per_qq])
            if picked_lines:
                remind_chunk = "【提醒】\n" + "\n".join(picked_lines)

        # 普通 + 低密度
        daily = self._load_daily(scope, self._int("keep_normal_days", 7))
        low_cutoff = (now() - timedelta(days=self._int("low_keep_days", 3))).strftime("%Y-%m-%d")
        matched, low, rest = [], [], []
        for e in daily:
            tag = e.get("tag")
            if tag == "普通":
                (matched if self._match(e, text, sender) else rest).append(e)
            elif tag == "低密度" and str(e.get("date", "")) >= low_cutoff and self._match(e, text, sender):
                low.append(e)

        # 普通段有独立预算，不和长期/重要抢
        normal_used = 0
        normal: list[str] = []
        for e in matched:
            if is_dup(str(e.get("text", ""))):
                continue
            line = f"- {e['text']}"
            if self.normal_budget > 0 and normal_used + len(line) > self.normal_budget:
                break
            normal.append(line)
            normal_used += len(line)
        pool = [e for e in rest if e not in matched]
        random.shuffle(pool)
        pool.sort(key=lambda e: e.get("date", ""), reverse=True)
        picked_random = pool[: self._int("random_min", 3)]
        for e in picked_random:
            if is_dup(str(e.get("text", ""))):
                continue
            line = f"- {e['text']}"
            if self.normal_budget > 0 and normal_used + len(line) > self.normal_budget:
                break
            normal.append(line)
            normal_used += len(line)
        if low:
            for e in low[:3]:
                if is_dup(str(e.get("text", ""))):
                    continue
                line = f"- {e['text']}"
                if self.normal_budget > 0 and normal_used + len(line) > self.normal_budget:
                    break
                normal.append(line)
                normal_used += len(line)
        recent_chunk = ("【近期】\n" + "\n".join(normal)) if normal else ""

        # 总预算只管 长期 + 共享 + 重要；普通记忆、提醒各自独立，不占这里
        def block_len() -> int:
            return len("\n\n".join(chunks))

        idx = len(chunks) - 1
        while block_len() > self.budget and idx > 0:
            parts = chunks[idx].split("\n")
            while len(parts) > 1 and block_len() > self.budget:
                parts.pop()
                chunks[idx] = "\n".join(parts)
            if len(parts) <= 1:
                chunks.pop(idx)
            idx -= 1

        # 长期记忆自己就超总预算时，才截它（保留标题行）
        if chunks and block_len() > self.budget:
            parts = chunks[0].split("\n")
            header, lines = parts[0], parts[1:]
            while lines and len("\n".join([header] + lines)) > self.budget:
                lines.pop()
            chunks[0] = "\n".join([header] + lines)

        parts_out = list(chunks)
        if remind_chunk:
            parts_out.append(remind_chunk)
        if recent_chunk:
            parts_out.append(recent_chunk)
        return "\n\n".join(parts_out)

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.enabled_inject:
            return
        try:
            scope = self.scope_of(event)
            if not scope:
                return
            block = self.build_block(scope, str(event.get_sender_id()), event.message_str or "")
            if not block:
                return
            text = DEFAULT_HEADER + "\n" + block
            if TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
            else:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + text
        except Exception as exc:  # 记忆出错不影响对话
            logger.warning(f"memory5: 注入跳过 ({type(exc).__name__}: {exc})")

    # ---------------------------------------------------------- extraction
    async def _summarize(self, rows: list[dict]) -> list[dict]:
        provider = self.context.get_using_provider()
        if not provider or not hasattr(provider, "text_chat"):
            logger.warning("memory5: 没有可用的模型提供商（可能还在启动中），本批跳过")
            return []
        lines = []
        for row in rows[-150:]:
            text = str(row.get("text", "")).replace("\n", " ").strip()
            if not text:
                continue
            lines.append(f"[{row.get('ts')}] {row.get('name')}({row.get('qq')}): {text[:200]}")
        if not lines:
            return []
        prompt = EXTRACT_PROMPT + "\n".join(lines)
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[], system_prompt=""),
                timeout=180,
            )
            raw = (resp.completion_text or "").strip()
        except Exception as exc:
            logger.warning(f"memory5: 抽取失败 {type(exc).__name__}: {exc}")
            return []
        return _parse_model_items(raw)

    async def extract_scope(self, scope: str) -> int:
        """一次归档把积压的消息分批处理完（默认最多 8 批 × 200 条）。"""
        paths = scope_paths(scope)
        total = 0
        batches = self._int("archive_max_batches", 8)
        for _ in range(batches):
            cursor = read_json(paths["cursor"], {})
            rows = raw_since(scope, cursor)
            if len(rows) < 3:
                break
            total += await self._extract_batch(scope, rows)
            last = str(rows[-1].get("ts", "")) if isinstance(rows[-1], dict) else str(rows[-1][0])
            write_json(paths["cursor"], {"last_ts": last})
            if len(rows) < 200:
                break
        if total:
            if bool(self._get("dedupe_enable", True)):
                removed = dedupe_scope(scope)
                if removed:
                    logger.info(f"memory5: {scope} 跨标签去重 {len(removed)} 条")
            try:
                renumbered = renumber_scope(scope)
                if renumbered:
                    logger.info(f"memory5: {scope} 序号已整理（{renumbered} 条）")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"memory5: 重排序号跳过 {type(exc).__name__}: {exc}")
            try:
                refresh_index(scope)
            except Exception:  # noqa: BLE001
                pass
        return total

    async def _extract_batch(self, scope: str, rows: list[dict]) -> int:
        paths = scope_paths(scope)
        items = await self._summarize(rows)
        entries, aliases = [], []
        for item in items:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject", "")).strip()
            text = str(item.get("text", "")).strip()
            topic = str(item.get("topic", "")).strip()[:8] or "事件"
            if not subject.isdigit() or not text:
                continue
            stability = _to_int(item.get("stability"), 2)
            impact = _to_int(item.get("impact"), 2)
            info = _to_int(item.get("info"), 2)
            due = parse_dt(str(item.get("due") or ""))
            if due and due - now() > timedelta(days=self._int("remind_max_future_days", 14)):
                due = None  # 超远期的提醒不记
            text, hit = desensitize(text[:40], self.blocked_index())
            text = absolutize_time(text, due)
            if hit:
                info = max(0, info - 1)
            tag = self._classify(stability, impact, info, due)
            if tag == "无效":
                continue
            # 条目归属日期用这批消息最后一条的时间，回填历史时才不会全堆到今天
            last_ts = str(rows[-1].get("ts", "")) if rows else ""
            day = last_ts[:10] if len(last_ts) >= 10 else day_str()
            entries.append({
                "date": day, "seq": 0, "subject": subject, "topic": topic,
                "tag": tag, "text": text,
                "ts": now().isoformat(timespec="seconds"),
                "stability": stability, "impact": impact, "info": info,
                "due": due.isoformat(timespec="minutes") if due else None,
                "sent": [] if tag == "提醒" else None,
            })
            for alias in item.get("aliases") or []:
                if isinstance(alias, dict):
                    aliases.append(alias)

        # seq 由 append_entries 统一分配（每个文件只读一次）

        important = [e for e in entries if e["tag"] == "重要"]
        remind = [e for e in entries if e["tag"] == "提醒"]
        daily = [e for e in entries if e["tag"] in ("普通", "低密度")]

        append_entries(scope, daily)
        if important:
            existing = read_jsonl(paths["important"])
            cores = []
            core_impact = self._int("core_impact", self._int("th_core_impact", 3))
            core_info = self._int("core_info", self._int("th_core_info", 3))
            for e in important:
                key = (e["subject"], e["topic"])
                existing = [x for x in existing if (x.get("subject"), x.get("topic")) != key]
                existing.append(e)
                if int(e.get("impact", 2)) >= core_impact and int(e.get("info", 2)) >= core_info:
                    cores.append(e)
            if cores:
                self._write_core_many(scope, cores)
            write_jsonl(paths["important"], existing)
        if remind:
            existing = read_jsonl(paths["remind"])
            for e in remind:
                key = (e["subject"], e["topic"])
                existing = [x for x in existing if (x.get("subject"), x.get("topic")) != key]
                existing.append(e)
            write_jsonl(paths["remind"], existing)
        if aliases:
            update_aliases(scope, aliases)

        logger.info(f"memory5: {scope} 本批 {len(entries)} 条（{len(rows)} 条消息）")
        return len(entries)

    def extract_times(self) -> list[tuple[int, int]]:
        raw = str(self._get("extract_times", "04:05,16:05"))
        out = []
        for piece in re.split(r"[,，]", raw):
            try:
                hh, mm = piece.strip().split(":")[:2]
                out.append((int(hh), int(mm)))
            except (ValueError, AttributeError):
                continue
        return sorted(out or [(4, 5), (16, 5)])

    def _seconds_to_next_slot(self) -> float:
        current = now()
        targets = []
        for hh, mm in self.extract_times():
            target = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= current:
                target += timedelta(days=1)
            targets.append(target)
        return max(60.0, (min(targets) - current).total_seconds())

    def _quiet_ranges(self) -> list[tuple[int, int]]:
        raw = str(self._get("notify_quiet_ranges", "00:00-08:00,12:00-14:00"))
        ranges: list[tuple[int, int]] = []
        for piece in re.split(r"[,，]", raw):
            chunk = piece.strip().replace("：", ":")
            if not chunk or "-" not in chunk:
                continue
            start, end = chunk.split("-", 1)
            try:
                sh, sm = [int(x) for x in start.split(":")[:2]]
                eh, em = [int(x) for x in end.split(":")[:2]]
                ranges.append((sh * 60 + sm, eh * 60 + em))
            except (ValueError, AttributeError):
                continue
        return ranges or [(0, 480), (720, 840)]

    def _in_quiet_hours(self) -> bool:
        cur = now()
        minutes = cur.hour * 60 + cur.minute
        for start, end in self._quiet_ranges():
            if start == end:
                continue
            if start < end:
                if start <= minutes < end:
                    return True
            elif minutes >= start or minutes < end:
                return True
        return False

    def _notify_state_path(self) -> str:
        return os.path.join(MEM_ROOT, ".notify_state.json")

    def _can_notify_today(self) -> bool:
        cap = self._int("notify_max_per_day", 2)
        state = read_json(self._notify_state_path(), {})
        if state.get("date") != day_str():
            return True
        return int(state.get("count", 0)) < cap

    def _mark_notified(self) -> None:
        state = read_json(self._notify_state_path(), {})
        if state.get("date") != day_str():
            state = {"date": day_str(), "count": 0}
        state["count"] = int(state.get("count", 0)) + 1
        write_json(self._notify_state_path(), state)

    async def _notify_archived(self, force: bool = False) -> None:
        if self._in_quiet_hours():
            logger.info("memory5: 处于免打扰时段，跳过归档通知")
            return
        if not self._can_notify_today():
            logger.info("memory5: 今日归档通知已达上限，跳过")
            return
        raw = self._get("notify_scopes", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        sent = 0
        for sc in raw:
            umo = self._umo_for(str(sc))
            if not umo:
                continue
            chain = MessageChain()
            chain.chain = [Plain("【今日回忆已完成归档】")]
            try:
                await asyncio.wait_for(self.context.send_message(umo, chain), timeout=15)
                sent += 1
                logger.info(f"memory5: 归档通知已发送 {sc}")
            except Exception as exc:
                logger.warning(f"memory5: 归档通知发送失败 {sc}: {exc}")
        if sent:
            self._mark_notified()

    async def _extract_loop(self) -> None:
        """每 12 小时（默认 04:05 / 16:05）整理一次；整理期间不产生聊天回复。"""
        await asyncio.sleep(45)
        last_run = float(read_json(os.path.join(MEM_ROOT, ".last_extract"), {}).get("ts", 0))
        if time.time() - last_run > 6 * 3600:
            if await self._run_extraction():
                await self._notify_archived()
        while True:
            try:
                await asyncio.sleep(self._seconds_to_next_slot())
                if await self._run_extraction():
                    await self._notify_archived()
            except Exception as exc:
                logger.warning(f"memory5: 抽取循环异常 {type(exc).__name__}: {exc}")
                await asyncio.sleep(300)

    async def _run_extraction(self) -> int:
        if not self.enabled_archive:
            return 0
        total = 0
        for scope in self.scopes:
            try:
                total += await self.extract_scope(scope)
            except Exception as exc:
                logger.warning(f"memory5: {scope} 抽取失败 {type(exc).__name__}: {exc}")
        write_json(os.path.join(MEM_ROOT, ".last_extract"), {"ts": time.time()})
        return total

    async def trigger_archive(self) -> bool:
        """手动触发（设置页开关）：不管有没有新条目，都尽量发一条归档通知。"""
        if self._archiving:
            return False
        self._archiving = True
        try:
            total = await self._run_extraction()
            logger.info(f"memory5: 手动归档完成，新增 {total} 条")
            await self._notify_archived(force=True)
        finally:
            self._archiving = False
        return True

    async def _delayed_archive(self) -> None:
        """等模型提供商就绪再跑，避免启动瞬间 provider 为空。"""
        await asyncio.sleep(25)
        for _ in range(6):
            provider = self.context.get_using_provider()
            if provider and hasattr(provider, "text_chat"):
                break
            await asyncio.sleep(10)
        await self.trigger_archive()

    def _consume_archive_flag(self) -> bool:
        """设置页勾选「立即归档一次」后读一次并复位；优先改内存里的配置，面板勾选也会跟着取消。"""
        if not bool(self._get("archive_now", False)):
            return False

        try:
            if isinstance(self.config, dict):
                basic = self.config.get("basic")
                if isinstance(basic, dict):
                    basic["archive_now"] = False
                else:
                    self.config["archive_now"] = False
            self._flat["archive_now"] = False
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
                return True
        except Exception as exc:  # pragma: no cover
            logger.warning(f"memory5: 内存里复位归档开关失败 {type(exc).__name__}: {exc}")

        return True
        basic = data.get("basic") or {}
        basic["archive_now"] = False
        data["basic"] = basic
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8-sig") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning(f"memory5: 复位归档开关失败 {exc}")
        return True

    # -------------------------------------------------------------- remind
    def _tasks_path(self) -> str:
        return os.path.join(MEM_ROOT, "tasks.json")

    def load_tasks(self) -> list[dict]:
        data = read_json(self._tasks_path(), {})
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return tasks if isinstance(tasks, list) else []

    def save_tasks(self, tasks: list[dict]) -> None:
        write_json(self._tasks_path(), {"tasks": tasks})

    @staticmethod
    def _task_slot(task: dict, current: datetime) -> datetime | None:
        """算这条定时任务今天该发的时间点，不适用就返回 None。"""
        try:
            hh, mm = [int(x) for x in str(task.get("at", "")).split(":")[:2]]
        except (ValueError, AttributeError):
            return None
        kind = str(task.get("kind") or "daily")
        slot = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if kind == "once":
            date = str(task.get("date") or "")[:10]
            if date != current.strftime("%Y-%m-%d"):
                return None
            return slot
        if kind == "weekly":
            days = task.get("weekdays") or []
            if current.isoweekday() not in {int(d) for d in days if str(d).isdigit()}:
                return None
            return slot
        # daily：支持“连续 N 天”，到期后自动停
        repeat_days = int(task.get("repeat_days") or 0)
        if repeat_days > 0:
            try:
                start = datetime.strptime(str(task.get("date"))[:10], "%Y-%m-%d").replace(tzinfo=TZ)
            except ValueError:
                return None
            elapsed = (current.date() - start.date()).days
            if elapsed < 0 or elapsed >= repeat_days:
                return None
        return slot

    def _media_limit_mb(self, kind: str) -> int:
        defaults = {"text": 1, "image": 5, "voice": 10, "video": 100}
        return self._int_raw(f"{kind}_limit_mb", defaults.get(kind, 5))

    def media_total_limit_mb(self, kind: str) -> int:
        defaults = {"text": 10, "image": 150, "voice": 300, "video": 1024}
        return self._int_raw(f"{kind}_total_mb", defaults.get(kind, 1024))

    def cleanup_orphan_media(self) -> int:
        """删掉没有任何任务引用的媒体文件（测试残留、已结束任务漏删的）。"""
        base = self._media_dir()
        if not os.path.isdir(base):
            return 0
        used = {str(t.get("media") or "") for t in self.load_tasks()}
        removed = 0
        for name in os.listdir(base):
            if name in used:
                continue
            try:
                os.remove(os.path.join(base, name))
                removed += 1
            except OSError:
                pass
        return removed

    @staticmethod
    def _media_dir() -> str:
        return os.path.join(MEM_ROOT, "media")

    @staticmethod
    def _media_kind_of(filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return "image"
        if ext in (".mp3", ".wav", ".ogg", ".silk", ".amr", ".m4a"):
            return "voice"
        if ext in (".mp4", ".mov", ".mkv", ".webm", ".flv"):
            return "video"
        return "other"

    def media_usage_mb(self, kind: str) -> float:
        total = 0
        base = self._media_dir()
        if os.path.isdir(base):
            for name in os.listdir(base):
                if self._media_kind_of(name) != kind:
                    continue
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        return total / 1024 / 1024

    def cleanup_task_media(self, task: dict, keep_others: bool = True) -> bool:
        """删掉任务引用的媒体文件；如果别的任务还在用就保留。"""
        media = str(task.get("media") or "")
        if not media:
            return False
        if keep_others:
            for other in self.load_tasks():
                if other is task or other.get("id") == task.get("id"):
                    continue
                if str(other.get("media") or "") == media:
                    return False
        path = os.path.join(self._media_dir(), media)
        try:
            if os.path.isfile(path):
                os.remove(path)
                return True
        except OSError:
            pass
        return False

    def _media_component(self, kind: str, media: str):
        """把图片/语音/视频转成消息组件；本地文件转 base64，外链直接透传。"""
        file_ref = media
        if not media.lower().startswith(("http://", "https://")):
            path = media if os.path.isabs(media) else os.path.join(MEM_ROOT, "media", media)
            if not os.path.isfile(path):
                logger.warning(f"memory5: 富媒体文件不存在 {path}")
                return None
            size = os.path.getsize(path)
            limit_mb = self._media_limit_mb(kind)
            if limit_mb > 0 and size > limit_mb * 1024 * 1024:
                logger.warning(f"memory5: {kind} 超过 {limit_mb}MB（{size // 1024}KB），跳过发送")
                return None
            with open(path, "rb") as fh:
                file_ref = "base64://" + base64.b64encode(fh.read()).decode()
        try:
            if kind == "image":
                return Image(file=file_ref)
            if kind == "voice":
                return Record(file=file_ref)
            if kind == "video":
                return Video(file=file_ref)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"memory5: 富媒体组件构造失败 {kind}: {exc}")
        return None

    async def _check_tasks(self) -> None:
        if not self.enabled:
            return
        tasks = self.load_tasks()
        if not tasks:
            return
        current = now()
        changed = False
        for task in tasks:
            if not task.get("enabled", True):
                continue
            slot = self._task_slot(task, current)
            if not slot:
                continue
            if current < slot or current - slot > timedelta(minutes=5):
                continue
            key = slot.strftime("%Y-%m-%dT%H:%M")
            if task.get("last_sent") == key:
                continue
            umo = self._umo_for(str(task.get("scope", "")))
            if not umo:
                continue
            chain = MessageChain()
            msg_type = str(task.get("msg_type") or "text")
            media = str(task.get("media") or "")
            if msg_type != "text" and media:
                component = self._media_component(msg_type, media)
                if component is not None:
                    chain.chain.append(component)
            if task.get("message"):
                chain.chain.append(Plain(str(task.get("message"))))
            if not chain.chain:
                continue
            try:
                await asyncio.wait_for(self.context.send_message(umo, chain), timeout=15)
                task["last_sent"] = key
                finished = str(task.get("kind")) == "once"
                if str(task.get("kind")) == "daily":
                    repeat_days = int(task.get("repeat_days") or 0)
                    if repeat_days > 0:
                        try:
                            start = datetime.strptime(str(task.get("date"))[:10], "%Y-%m-%d").replace(tzinfo=TZ)
                            if (current.date() - start.date()).days + 1 >= repeat_days:
                                finished = True
                        except ValueError:
                            pass
                if finished:
                    task["enabled"] = False
                    # 任务结束后清掉它上传的媒体，避免占空间
                    self.cleanup_task_media(task, keep_others=False)
                changed = True
                logger.info(f"memory5: 定时任务已发送 {task.get('name')} -> {task.get('scope')}")
            except Exception as exc:
                logger.warning(f"memory5: 定时任务发送失败 {exc}")
        if changed:
            self.save_tasks(tasks)

    def _fallback_remind(self, scope: str, entry: dict) -> bool:
        """提醒到期后按内容自动决定去向：够格→重要，普通事实→普通，没信息→删。
        总开关关掉时一律直接删除。"""
        if not bool(self._get("remind_fallback_enable", True)):
            return False
        try:
            text = str(entry.get("text", "")).strip()
            subject = str(entry.get("subject", "")).strip()
            topic = str(entry.get("topic", "")).strip() or "事件"
            if not text:
                return False
            stability = _to_int(entry.get("stability"), 2)
            impact = _to_int(entry.get("impact"), 2)
            info = _to_int(entry.get("info"), 2)
            # 归档时模型给的判断优先；没有就按内容现算
            raw_target = str(entry.get("fallback") or "").strip().lower()
            if raw_target in ("important", "normal", "delete"):
                target = {"important": "重要", "normal": "普通", "delete": "delete"}[raw_target]
            else:
                target = self._classify(stability, impact, info, None)
                if target == "低密度":
                    target = "delete"
            if target == "delete":
                return False

            if target == "重要":
                path = scope_paths(scope)["important"]
                rows = read_jsonl(path)
                for row in rows:
                    if str(row.get("subject")) == subject and (
                        str(row.get("topic")) == topic or _similar(str(row.get("text", "")), text)
                    ):
                        return False
                rows.append({
                    "date": day_str(), "seq": 0, "subject": subject, "topic": topic,
                    "tag": "重要", "text": text,
                    "ts": now().isoformat(timespec="seconds"),
                    "stability": stability, "impact": impact, "info": info,
                    "due": None, "sent": None,
                })
                for idx, row in enumerate(rows, 1):
                    row["seq"] = idx
                write_jsonl(path, rows)
                logger.info(f"memory5: 提醒过期回落为重要 {scope} {text[:20]}")
                return True

            path = daily_file(scope, day_str())
            rows = read_jsonl(path)
            for row in rows:
                if str(row.get("subject")) == subject and _similar(str(row.get("text", "")), text):
                    return False
            rows.append({
                "date": day_str(), "seq": max([int(r.get("seq", 0) or 0) for r in rows] + [0]) + 1,
                "subject": subject, "topic": topic, "tag": "普通", "text": text,
                "ts": now().isoformat(timespec="seconds"),
                "stability": stability, "impact": impact, "info": info,
                "due": None, "sent": None,
            })
            write_jsonl(path, rows)
            logger.info(f"memory5: 提醒过期回落为普通 {scope} {text[:20]}")
            return True
        except Exception as exc:  # pragma: no cover
            logger.debug(f"memory5: 提醒回落失败 {type(exc).__name__}: {exc}")
            return False

    async def _remind_loop(self) -> None:
        while True:
            try:
                self._prune()
                if purge_old_trash is not None:
                    purge_old_trash()
                await self._maybe_send_reminders({})
                await self._check_tasks()
            except Exception as exc:
                logger.warning(f"memory5: 提醒循环异常 {type(exc).__name__}: {exc}")
            await asyncio.sleep(120)

    def _prune(self) -> None:
        # raw：只保留最近 30 天的原始消息，避免无限增长
        raw_cutoff = (now() - timedelta(days=self._int("raw_keep_days", 30))).strftime("%Y-%m-%d")
        for sc in self.scopes:
            raw_dir = os.path.join(scope_dir(sc), "raw")
            if os.path.isdir(raw_dir):
                for name in os.listdir(raw_dir):
                    if name.endswith(".jsonl") and name[:-6] < raw_cutoff:
                        try:
                            os.remove(os.path.join(raw_dir, name))
                        except OSError:
                            pass
        # 普通：整份删掉超过保留天数的那天文件；低密度：逐条删掉超期的
        normal_days = self._int("keep_normal_days", 7)
        low_days = self._int("low_keep_days", 3)
        normal_cutoff = (now() - timedelta(days=normal_days)).strftime("%Y-%m-%d")
        low_cutoff = (now() - timedelta(days=low_days)).strftime("%Y-%m-%d")
        for sc in self.scopes:
            daily_dir = scope_paths(sc)["daily_dir"]
            if not os.path.isdir(daily_dir):
                continue
            for name in list(os.listdir(daily_dir)):
                if not name.endswith(".jsonl"):
                    continue
                day = name[:-6]
                path = os.path.join(daily_dir, name)
                if day < normal_cutoff:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                if day >= low_cutoff:
                    continue
                rows = read_jsonl(path)
                kept = [r for r in rows if r.get("tag") != "低密度"]
                if len(kept) != len(rows):
                    write_jsonl(path, kept)
            # 重要：超过保留天数的条目清掉（core 另算）
            imp_path = scope_paths(sc)["important"]
            rows = read_jsonl(imp_path)
            keep_days = self._int("important_keep_days", 30)
            imp_cutoff = (now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
            kept = [r for r in rows if str(r.get("date", "")) >= imp_cutoff]
            if len(kept) != len(rows):
                write_jsonl(imp_path, kept)
        for sc in self.scopes:
            paths = scope_paths(sc)
            rows = read_jsonl(paths["remind"])
            purge_minutes = self._int("remind_purge_minutes", 5)
            keep = []
            for e in rows:
                due = parse_dt(e.get("due", ""))
                if due is None:
                    continue
                if now() > due + timedelta(minutes=purge_minutes):
                    if str(e.get("repeat") or ""):
                        keep.append(e)  # 重复提醒不做过期回落，交给发送循环顺延
                        continue
                    # 过期提醒按条目设置回落到 重要 / 普通 / 直接删除
                    self._fallback_remind(sc, e)
                    continue
                keep.append(e)
            if len(keep) != len(rows):
                write_jsonl(paths["remind"], keep)

    def _remind_slots(self, due: datetime) -> list[datetime]:
        """按「提前 X 分钟、共 N 次」等分为 N 个触发点，最后一个就是到期时刻。
        例：X=30、N=4 -> 前30、前20、前10、准点。"""
        lead = self._int("remind_lead_minutes", 30)
        count = max(1, self._int("remind_count", 2))
        if count == 1 or lead <= 0:
            return [due]
        step = lead / (count - 1)
        return [due - timedelta(minutes=lead - i * step) for i in range(count)]

    def _remind_window(self, due: datetime) -> tuple[datetime, datetime]:
        lead = self._int("remind_lead_minutes", 30)
        margin = self._int("remind_inject_margin_minutes", 5)
        return due - timedelta(minutes=lead + margin), due + timedelta(minutes=margin)

    async def _maybe_send_reminders(self, _sent=None) -> None:
        if not self.enabled:
            return
        current = now()
        grace = timedelta(minutes=self._int("remind_grace_minutes", 5))
        for sc in self.scopes:
            path = scope_paths(sc)["remind"]
            rows = read_jsonl(path)
            if not rows:
                continue
            changed = False
            for entry in rows:
                due = parse_dt(entry.get("due", ""))
                if not due:
                    continue
                sent = list(entry.get("sent") or [])
                if sent is None:
                    sent = []
                for slot in self._remind_slots(due):
                    key = slot.strftime("%Y-%m-%dT%H:%M")
                    if key in sent:
                        continue
                    if current < slot:
                        continue
                    if current - slot > grace:
                        sent.append(key)  # 错过就标记，避免开机猛发
                        changed = True
                        continue
                    umo = self._umo_for(sc)
                    if not umo:
                        break
                    chain = MessageChain()
                    chain.chain = [Plain(f"⏰ 提醒：{entry.get('text', '')}")]
                    try:
                        await asyncio.wait_for(self.context.send_message(umo, chain), timeout=15)
                        sent.append(key)
                        entry["sent"] = sent
                        changed = True
                        logger.info(f"memory5: 已发送提醒 {sc} {slot.strftime('%H:%M')} {entry.get('text', '')[:20]}")
                    except Exception as exc:
                        logger.warning(f"memory5: 提醒发送失败 {exc}")
                        break
                repeat = str(entry.get("repeat") or "")
                if repeat and current >= due:
                    slots = self._remind_slots(due)
                    if all(slot.strftime("%Y-%m-%dT%H:%M") in sent for slot in slots):
                        nxt = next_repeat_due(due, repeat)
                        if nxt:
                            entry["due"] = nxt.isoformat(timespec="minutes")
                            entry["sent"] = []
                            changed = True
                            logger.info(f"memory5: 重复提醒顺延到 {entry['due']} {entry.get('text', '')[:16]}")
                if "sent" not in entry and sent:
                    entry["sent"] = sent
                    changed = True
            if changed:
                write_jsonl(path, rows)

    def _umo_for(self, scope: str) -> str | None:
        """优先用收到消息时记录的真实 UMO，其次按平台名拼。"""
        known = load_umos().get(str(scope))
        if known:
            return known
        platform = os.environ.get("MEMORY5_PLATFORM", "aiocqhttp")
        if scope.startswith("group:"):
            return f"{platform}:GroupMessage:{scope.split(':', 1)[1]}"
        if scope.startswith("private:"):
            return f"{platform}:FriendMessage:{scope.split(':', 1)[1]}"
        return None

    # --------------------------------------------------------------- init


    # ------------------------------------------------------- admin auth
    def _admin_state_path(self) -> str:
        return os.path.join(MEM_ROOT, "admins.json")

    def _admin_state(self) -> dict:
        data = read_json(self._admin_state_path(), {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("authorized", [])
        data.setdefault("pending", {})
        return data

    def _save_admin_state(self, state: dict) -> None:
        write_json(self._admin_state_path(), state)


    def _platform_names(self) -> list[str]:
        names = []
        try:
            for inst in self.context.platform_manager.platform_insts:
                meta = inst.meta()
                name = getattr(meta, "id", "") or getattr(meta, "name", "") or getattr(inst, "name", "")
                if name:
                    names.append(str(name))
        except Exception:  # noqa: BLE001
            pass
        return names

    def _umo_candidates(self, scope: str) -> list[str]:
        out = []
        platform = os.environ.get("MEMORY5_PLATFORM", "").strip()
        known = load_umos().get(str(scope))
        names = self._platform_names() + ([platform] if platform else [])
        for name in names + ["snowluma", "aiocqhttp"]:
            if not name:
                continue
            if scope.startswith("group:"):
                cand = f"{name}:GroupMessage:{scope.split(':', 1)[1]}"
            elif scope.startswith("private:"):
                cand = f"{name}:FriendMessage:{scope.split(':', 1)[1]}"
            else:
                continue
            if cand not in out:
                out.append(cand)
        if known and known not in out:
            out.append(known)
        return out

    async def _send_private(self, qq: str, text: str) -> bool:
        candidates = self._umo_candidates(f"private:{qq}")
        if not candidates:
            logger.warning(f"memory5: 找不到 {qq} 的私聊会话，授权消息发不出去")
            return False
        for umo in candidates:
            try:
                chain = MessageChain().message(text)
                result = await asyncio.wait_for(self.context.send_message(umo, chain), timeout=15)
                if result is False:
                    raise RuntimeError("send_message 返回 False")
                if load_umos().get(f"private:{qq}") != umo:
                    record_umo(f"private:{qq}", umo)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"memory5: 候选 {umo} 发送失败 {type(exc).__name__}")
        logger.warning(f"memory5: 给 {qq} 发送授权消息失败，所有候选会话都不可用")
        return False


    def _ensure_owner_memory(self, qq: str) -> None:
        try:
            core = read_text(scope_paths(f"private:{qq}")["core"])
            if str(qq) in core and "的主人" in core:
                return
        except Exception:  # noqa: BLE001
            pass
        self._write_owner_memory(qq)

    def _write_owner_memory(self, qq: str) -> None:
        if not str(qq).isdigit():
            return
        name = str(self._get("bot_name", "鱼鱼") or "鱼鱼")
        bots = sorted(self.bot_qqs())
        bot_qq = bots[0] if bots else ""
        text = f"{name}({bot_qq}) 的主人" if bot_qq else f"{name} 的主人"
        scope = f"private:{qq}"
        try:
            ensure_dir(scope_dir(scope))
            self._write_core(scope, {
                "topic": "主人", "subject": str(qq), "text": text, "date": day_str(),
            })
            logger.info(f"memory5: 已在长期记忆记录 {qq} 是 {name} 的主人")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"memory5: 写主人长期记忆失败 {type(exc).__name__}: {exc}")

    async def _admin_sync(self) -> None:
        desired = set()
        raw = self._get("admin_qqs", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        for item in raw or []:
            qq = str(item).strip()
            if qq.isdigit():
                desired.add(qq)
        state = self._admin_state()
        authorized = [str(x) for x in state.get("authorized", [])]
        pending = dict(state.get("pending", {}))
        now_ts = time.time()

        for qq in list(pending):
            if now_ts - float(pending[qq] or 0) > 24 * 3600:
                pending.pop(qq, None)

        for qq in desired - set(authorized):
            if qq in pending:
                continue
            sent = await self._send_private(
                qq,
                "管理员授权验证：请回复 [已确认授权] 确认这个 QQ 的管理员身份；24 小时内未确认不会生效。",
            )
            if sent:
                pending[qq] = now_ts
                logger.info(f"memory5: 已向 {qq} 发送管理员授权验证，等待回复")

        for qq in set(authorized) - desired:
            await self._send_private(qq, "管理员权限已移除：这个 QQ 不再拥有鱼鱼日记的管理员权限。")
            authorized.remove(qq)
            logger.info(f"memory5: 已移除管理员 {qq} 并发送通知")

        state["authorized"] = authorized
        state["pending"] = pending
        self._save_admin_state(state)
        for qq in authorized:
            self._ensure_owner_memory(qq)

    async def _admin_auth_listener(self, event: AstrMessageEvent) -> None:
        text = (event.message_str or "").strip()
        if "已确认授权" not in text:
            return
        event.stop_event()
        try:
            event.should_call_llm(False)
        except Exception:  # noqa: BLE001
            pass
        sender = str(event.get_sender_id())
        if not getattr(event, "is_private_chat", lambda: True)():
            return
        state = self._admin_state()
        pending = state.get("pending", {})
        if sender not in pending:
            return
        authorized = set(str(x) for x in state.get("authorized", []))
        authorized.add(sender)
        pending.pop(sender, None)
        state["authorized"] = sorted(authorized)
        state["pending"] = pending
        self._save_admin_state(state)
        logger.info(f"memory5: {sender} 已确认管理员授权")
        self._write_owner_memory(sender)
        await self._send_private(sender, "已确认授权：这个 QQ 现在是鱼鱼日记的管理员。")

    async def _admin_loop(self) -> None:
        while True:
            try:
                await self._admin_sync()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"memory5: 管理员授权检查跳过 {type(exc).__name__}: {exc}")
            await asyncio.sleep(60)

    async def initialize(self) -> None:
        try:
            moved = migrate_legacy_layout()
            if moved:
                logger.info(f"memory5: 已迁移 {moved} 个旧文件到 plugin_data/astrbot_plugin_memory5")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"memory5: 旧数据迁移跳过 {type(exc).__name__}: {exc}")
        ensure_dir(MEM_ROOT)
        key, generated = ensure_key()
        if generated and key is not None:
            logger.info("memory5: 首次运行，已自动生成加密密钥")
        if "执行重置" in str(self._get("reset_key", "不重置")):
            self._reset_key_and_memory()
        self._sync_schema_blocked_hint()
        set_crypto_mode(str(self._get("crypto_mode", "平衡")))
        try:
            converted = encrypt_existing_storage()
            if converted:
                logger.info(f"memory5: 已把 {converted} 个文件迁移到 {crypto_version()} 格式")
        except Exception as exc:  # 单个坏文件不能拖垮整个插件
            logger.warning(f"memory5: 加密迁移跳过 {type(exc).__name__}: {exc}")
        self._publish_security_status()
        self._maybe_rotate_key()
        self._write_backup_policy()
        try:
            removed = self.cleanup_orphan_media()
            if removed:
                logger.info(f"memory5: 清理无引用的媒体文件 {removed} 个")
        except Exception as exc:
            logger.warning(f"memory5: 媒体清理跳过 {type(exc).__name__}: {exc}")
        if Memory5WebUI is not None:
            try:
                Memory5WebUI(self).register()
                logger.info("memory5: WebUI API 已注册")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"memory5: WebUI 注册失败 {type(exc).__name__}: {exc}")
        if self._consume_archive_flag():
            logger.info("memory5: 检测到手动归档开关，稍后执行一次")
            asyncio.create_task(self._delayed_archive())
        asyncio.create_task(self._prewarm_cache())
        asyncio.create_task(self._admin_loop())
        if self._extract_task is None or self._extract_task.done():
            self._extract_task = asyncio.create_task(self._extract_loop())
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._remind_loop())
        logger.info(f"鱼鱼日记: 已启动，scope={self.scopes}")

    def _maybe_rotate_key(self) -> None:
        period = str(self._get("key_rotate_period", "关闭"))
        seconds = {"半天": 43200, "一天": 86400, "一周": 604800,
                   "一月": 2592000, "一年": 31536000}.get(period)
        if not seconds:
            return
        age = key_age_seconds()
        if age < 0 or age < seconds:
            return
        logger.info(f"memory5: 密钥已用 {int(age / 3600)} 小时，开始轮换（周期 {period}）")
        result = rotate_key(MEM_ROOT)
        if result.get("ok"):
            logger.info(f"memory5: 密钥轮换完成，重加密 {result.get('files')} 个文件")
        else:
            logger.warning(f"memory5: 密钥轮换失败 {result.get('error')}")

    def _write_backup_policy(self) -> None:
        label = str(self._get("backup_verify_period", "7天"))
        days = {"不校验": 0, "1天": 1, "3天": 3, "7天": 7, "30天": 30}.get(label, 7)
        write_plain_json(os.path.join(MEM_ROOT, "backup_policy.json"), {
            "enabled": days > 0,
            "period_days": days,
            "updated": now().isoformat(timespec="seconds"),
        })

    async def _prewarm_cache(self) -> None:
        """预热最近活跃会话的记忆文件，减少首次读取延迟。"""
        try:
            scored = []
            for scope in self.scopes:
                raw = os.path.join(scope_dir(scope), "raw")
                latest = 0.0
                if os.path.isdir(raw):
                    for name in os.listdir(raw):
                        try:
                            latest = max(latest, os.path.getmtime(os.path.join(raw, name)))
                        except OSError:
                            continue
                scored.append((latest, scope))
            scored.sort(reverse=True)
            for _, scope in scored[:3]:
                paths = scope_paths(scope)
                read_text(paths["core"])
                read_jsonl(paths["important"])
                read_jsonl(paths["remind"])
                daily_dir = paths["daily_dir"]
                if os.path.isdir(daily_dir):
                    for name in sorted(os.listdir(daily_dir))[-2:]:
                        if name.endswith(".jsonl"):
                            read_jsonl(os.path.join(daily_dir, name))
            if scored:
                logger.info(f"memory5: 已预热最近 {min(3, len(scored))} 个会话的缓存")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"memory5: 缓存预热跳过 {type(exc).__name__}: {exc}")

    def _sync_schema_blocked_hint(self) -> None:
        """把内置屏蔽词条数与路径写进设置页提示。"""
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            words_path = os.path.join(base, "blocked_words.txt")
            count = 0
            with open(words_path, encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
            schema_path = os.path.join(base, "_conf_schema.json")
            with open(schema_path, encoding="utf-8") as fh:
                schema = json.load(fh)
            item = schema.get("filter", {}).get("items", {}).get("extra_blocked_words")
            if item is not None:
                item["hint"] = (
                    f"内置词表 {count} 条（{words_path}）；此处补充额外屏蔽词，归档命中时会被替换。"
                )
                with open(schema_path, "w", encoding="utf-8") as fh:
                    json.dump(schema, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"memory5: 更新屏蔽词提示失败 {type(exc).__name__}: {exc}")

    def _reset_key_and_memory(self) -> None:
        """兜底重置：生成新密钥，并把旧记忆整体挪到旁边（需旧密钥才能恢复）。"""
        stamp = now().strftime("%Y%m%d-%H%M%S")
        old = os.path.join(os.path.dirname(MEM_ROOT), f"memory5-old-{stamp}")
        try:
            if os.path.isdir(MEM_ROOT):
                shutil.move(MEM_ROOT, old)
        except OSError as exc:
            logger.warning(f"memory5: 旧记忆目录移动失败 {exc}")
            return
        ensure_dir(MEM_ROOT)
        generate_new_key()
        try:
            self.config["reset_key"] = "不重置"
            self.config.save_config()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(f"memory5: 已重置密钥与记忆，旧目录 {old}")

    def _publish_security_status(self) -> None:
        """把加密状态写进设置页（只读展示，插件自己维护）。"""
        enabled = enc_key() is not None
        label = MODE_LABELS.get(crypto_mode(), crypto_mode())
        status = (f"已启用（{label} · {crypto_suite()} · 格式 {crypto_version()}）" if enabled
                  else "未启用：找不到密钥，日记以明文保存")
        try:
            sec = self.config.setdefault("security", {})
        except AttributeError:  # config 不是 dict-like
            return
        if sec.get("status") == status and sec.get("key_path") == KEY_PATH:
            return
        sec["status"] = status
        sec["key_path"] = KEY_PATH
        try:
            self.config.save_config()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"memory5: 写安全状态失败 {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ command
    def admin_qqs(self) -> set[str]:
        raw = self._get("admin_qqs", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return {str(x).strip() for x in raw if str(x).strip()}

    def bot_qqs(self) -> set[str]:
        raw = self._get("bot_qqs", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return {str(x).strip() for x in raw if str(x).strip()}

    def blacklist_qqs(self) -> set[str]:
        raw = self._get("blacklist_qqs", self._get("other_bot_qqs", []))
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _suppress_text(self, text: str) -> str:
        """登记命令输出，采集器看到同样的文字就不再写进记忆。"""
        now_ts = time.time()
        self._suppress = {k: v for k, v in self._suppress.items() if now_ts - v < 300}
        if text:
            self._suppress[text] = now_ts
        return text

    @filter.command("记忆查看")
    async def cmd_view(self, event: AstrMessageEvent) -> None:
        """用法：/记忆查看 [日期] [QQ] [普通级|提醒级|重要级|低密度级] [条数]
        条数默认 5，0 表示全部；长期记忆只能后台看。"""
        try:
            scope = self.scope_of(event) or "group:unknown"
            me = str(event.get_sender_id())
            is_admin = me in self.admin_qqs() or bool(event.is_admin())

            text = (event.message_str or "").replace("/记忆查看", "").strip()
            tokens = [t for t in re.split(r"[\s,，]+", text) if t]

            day = day_str()
            subject = ""
            level = "普通"
            count = 5
            numbers: list[str] = []
            for tok in tokens:
                if re.fullmatch(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", tok):
                    parts = [int(x) for x in tok.replace("/", "-").split("-")]
                    day = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
                    continue
                if tok in ("普通级", "提醒级", "重要级", "低密度级", "长期级", "长期记忆", "长期"):
                    level = tok[:-1] if tok.endswith("级") else tok
                    continue
                if tok.isdigit():
                    numbers.append(tok)
                    continue
                if tok in ("普通", "提醒", "重要", "低密度"):
                    level = tok

            for num in numbers:
                if len(num) >= 5:
                    subject = num
                else:
                    count = int(num)
            subject = subject or me

            if level.startswith("长期"):
                yield event.plain_result(self._suppress_text("长期记忆只在后台面板里查看，聊天里不提供。"))
                return
            if level not in ("普通", "提醒", "重要", "低密度"):
                yield event.plain_result(self._suppress_text("级别只能是：普通级、提醒级、重要级、低密度级。"))
                return
            if level == "重要" and not is_admin:
                yield event.plain_result(self._suppress_text("重要级记忆只对管理员开放。"))
                return
            if subject != me and not is_admin:
                yield event.plain_result(self._suppress_text("只能查自己的记忆。"))
                return

            if level in ("普通", "低密度"):
                rows = [r for r in read_jsonl(daily_file(scope, day))
                        if str(r.get("subject")) == subject and r.get("tag") == level]
                rows.sort(key=lambda r: r.get("seq", 0))
            elif level == "重要":
                rows = [r for r in read_jsonl(scope_paths(scope)["important"])
                        if str(r.get("subject")) == subject and str(r.get("date")) == day]
            else:
                rows = []
                for r in read_jsonl(scope_paths(scope)["remind"]):
                    if str(r.get("subject")) != subject:
                        continue
                    due_day = str(r.get("due", ""))[:10]
                    if due_day == day or str(r.get("date")) == day:
                        rows.append(r)

            if not rows:
                yield event.plain_result(self._suppress_text(f"{day} {subject} 的{level}记忆：没有记录。"))
                return

            show = rows if count == 0 else rows[:count]
            lines = [f"{day} {subject} 的{level}记忆（共 {len(rows)} 条，显示 {len(show)} 条）："]
            for idx, r in enumerate(show, 1):
                extra = f"（到期 {str(r.get('due'))[:16]}）" if r.get("due") else ""
                lines.append(f"{idx}. [{r.get('tag')}] {r.get('topic')}：{r.get('text')}{extra}")
            yield event.plain_result(self._suppress_text("\n".join(lines)))
        finally:
            # 必须在最后一个 yield 之后停：提前停会被 plain_result 覆盖
            event.stop_event()
            try:
                event.should_call_llm(False)
            except Exception:  # noqa: BLE001
                pass

