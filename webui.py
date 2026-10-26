"""memory5 的 WebUI API：日记查看、编辑、删除（进回收站）、恢复、彻底删除。"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import time
import uuid

from quart import jsonify, request

from astrbot.api import logger

try:  # 数据目录与加密层保持一致
    from .crypto import MEM_ROOT
except ImportError:  # pragma: no cover - 单文件加载
    try:
        from crypto import MEM_ROOT  # type: ignore[no-redef]
    except ImportError:
        MEM_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "plugin_data", "astrbot_plugin_memory5", "memory5")

TRASH_DIR = os.path.join(MEM_ROOT, "trash")
TRASH_TTL_DAYS = 7


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


_SCOPE_RE = re.compile(r"^(group|private):[A-Za-z0-9_-]{1,64}$")
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


def scope_dir(scope: str) -> str:
    if not _SCOPE_RE.match(str(scope)):
        raise ValueError("非法 scope")
    return os.path.join(MEM_ROOT, str(scope).replace(":", "_"))


# 统一走 crypto 的加密读写，别让面板绕过加密层
try:
    from .crypto import (  # noqa: F401
        append_jsonl_row,
        read_json,
        read_jsonl,
        read_text,
        write_json,
        write_jsonl,
        write_text,
    )
except Exception:  # pragma: no cover - 独立运行时退化成明文
    def read_json(path: str, default):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def write_json(path: str, data) -> None:
        ensure_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def read_jsonl(path: str) -> list[dict]:
        rows: list[dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    def write_jsonl(path: str, rows: list[dict]) -> None:
        ensure_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def read_text(path: str) -> str:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def write_text(path: str, text: str) -> None:
        ensure_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def append_jsonl_row(path: str, row: dict) -> None:
        ensure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def entries_path(scope: str, source: str, day: str) -> str:
    base = scope_dir(scope)
    if source == "daily":
        return os.path.join(base, "daily", f"{day}.jsonl")
    if source == "important":
        return os.path.join(base, "important.jsonl")
    if source == "remind":
        return os.path.join(base, "remind.jsonl")
    raise ValueError("未知来源")


CORE_LINE_RE = re.compile(
    r"^- \[(?P<topic>[^\]]*)\] (?P<subject>\S+) :: (?P<text>.*?)（更新 (?P<date>[^）]*)）\s*$"
)


def core_items(text: str) -> list[dict]:
    items: list[dict] = []
    for line_no, raw in enumerate(text.splitlines()):
        matched = CORE_LINE_RE.match(raw.strip())
        if not matched:
            continue
        items.append({
            "line_no": line_no,
            "index": len(items),
            "topic": matched.group("topic"),
            "subject": matched.group("subject"),
            "text": matched.group("text"),
            "date": matched.group("date"),
        })
    return items


def rewrite_core(text: str, index: int, new_text: str | None = None, remove: bool = False):
    lines = text.splitlines()
    items = core_items(text)
    target = next((item for item in items if item["index"] == index), None)
    if not target:
        raise ValueError("没找到这条长期记忆")
    pos = target["line_no"]
    if remove:
        removed = lines.pop(pos)
        return ("\n".join(lines) + "\n") if lines else "", removed
    lines[pos] = f"- [{target['topic']}] {target['subject']} :: {new_text}（更新 {target['date']}）"
    return "\n".join(lines) + "\n", None


def trash_entry(scope: str, source: str, day: str, entry: dict) -> str:
    ensure_dir(TRASH_DIR)
    tid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    payload = {
        "id": tid,
        "scope": scope,
        "source": source,
        "day": day,
        "entry": entry,
        "deleted_at": int(time.time()),
    }
    write_json(os.path.join(TRASH_DIR, f"{tid}.json"), payload)
    return tid


def purge_old_trash() -> int:
    if not os.path.isdir(TRASH_DIR):
        return 0
    cutoff = time.time() - TRASH_TTL_DAYS * 86400
    removed = 0
    for path in glob.glob(os.path.join(TRASH_DIR, "*.json")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


class _TasksMixin:
    """定时任务（定时提醒助手的功能并入）。"""

    async def tasks(self):
        plugin = self.plugin
        usage = {}
        limits = {}
        for kind in ("image", "voice", "video"):
            usage[kind] = round(plugin.media_usage_mb(kind), 2)
            limits[kind] = plugin.media_total_limit_mb(kind)
        tasks = plugin.load_tasks()
        text_bytes = sum(len(str(t.get("message") or "").encode("utf-8")) for t in tasks)
        usage["text"] = round(text_bytes / 1024 / 1024, 3)
        limits["text"] = plugin._int_raw("text_total_mb", 10)
        paged = self._paged(tasks)
        paged.update({"tasks": paged["items"], "usage": usage, "limits": limits})
        return jsonify({"status": "ok", "data": paged})

    async def task_save(self):
        body = await request.get_json() or {}
        name = str(body.get("name", "")).strip()
        scope = str(body.get("scope", "")).strip()
        message = str(body.get("message", "")).strip()
        kind = str(body.get("kind", "daily")).strip()
        at = str(body.get("at", "")).strip()
        date = str(body.get("date", "")).strip()[:10]
        weekdays = body.get("weekdays") or []
        if not name or not scope:
            raise ValueError("名称和目标会话不能为空")
        if not message and not str(body.get("media", "")).strip():
            raise ValueError("内容和媒体至少填一个")
        if len(message.encode("utf-8")) > self.plugin._media_limit_mb("text") * 1024 * 1024:
            raise ValueError(f"文字不能超过 {self.plugin._media_limit_mb('text')}MB")
        if kind not in ("once", "daily", "weekly"):
            raise ValueError("类型只能是 once / daily / weekly")
        try:
            hh, mm = [int(x) for x in at.split(":")[:2]]
        except (ValueError, AttributeError):
            raise ValueError("时间格式应为 HH:MM")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间超出范围")
        if kind == "once" and not date:
            raise ValueError("一次性任务需要日期")
        if kind == "weekly" and not weekdays:
            raise ValueError("每周任务需要选星期")
        if kind not in ("once", "daily", "weekly"):
            raise ValueError("类型只能是 once / daily / weekly")
        repeat_days = int(body.get("repeat_days") or 0)
        if kind == "daily" and repeat_days < 0:
            repeat_days = 0
        media_name = str(body.get("media", "")).strip()
        if media_name:
            media_name = os.path.basename(media_name)
            path = os.path.join(MEM_ROOT, "media", media_name)
            if not os.path.abspath(path).startswith(os.path.abspath(os.path.join(MEM_ROOT, "media"))):
                raise ValueError("非法媒体路径")
            repeat_limit = self.plugin._int_raw("repeat_media_limit_mb", 30)
            if repeat_limit > 0 and os.path.isfile(path) and os.path.getsize(path) > repeat_limit * 1024 * 1024:
                if kind == "weekly":
                    raise ValueError(
                        f"超过 {repeat_limit}MB 的媒体不能设为每周，请改成「每天 + 连续天数（1~7）」"
                    )
                if kind == "daily" and not (1 <= repeat_days <= 7):
                    raise ValueError(
                        f"超过 {repeat_limit}MB 的媒体只能连续发送 1~7 天，请把连续天数改成 1~7"
                    )
        if kind == "daily" and not date:
            date = __import__("time").strftime("%Y-%m-%d")
        tasks = self.plugin.load_tasks()
        task_id = str(body.get("id", "")).strip()
        text_limit = self.plugin._int_raw("text_total_mb", 10)
        other_text = sum(
            len(str(t.get("message") or "").encode("utf-8"))
            for t in tasks if t.get("id") != task_id
        )
        if text_limit > 0 and (other_text + len(message.encode("utf-8"))) > text_limit * 1024 * 1024:
            used_mb = other_text / 1024 / 1024
            raise ValueError(
                f"文字已超过容量上限（{text_limit}MB，当前 {used_mb:.2f}MB），请删除一部分后上传"
            )
        payload = {
            "id": task_id or uuid.uuid4().hex[:10],
            "name": name, "scope": scope, "message": message,
            "kind": kind, "at": f"{hh:02d}:{mm:02d}", "date": date,
            "repeat_days": repeat_days,
            "weekdays": [int(d) for d in weekdays if str(d).isdigit()],
            "enabled": bool(body.get("enabled", True)),
            "msg_type": str(body.get("msg_type", "text")),
            "media": str(body.get("media", "")),
        }
        replaced = False
        for idx, task in enumerate(tasks):
            if task.get("id") == payload["id"]:
                payload["last_sent"] = task.get("last_sent")
                tasks[idx] = payload
                replaced = True
                break
        if not replaced:
            tasks.append(payload)
        self.plugin.save_tasks(tasks)
        return jsonify({"status": "ok", "data": payload})

    async def task_delete(self):
        body = await request.get_json() or {}
        task_id = str(body.get("id", "")).strip()
        tasks = self.plugin.load_tasks()
        target = next((t for t in tasks if t.get("id") == task_id), None)
        if target:
            self.plugin.cleanup_task_media(target, keep_others=True)
        tasks = [t for t in tasks if t.get("id") != task_id]
        self.plugin.save_tasks(tasks)
        return jsonify({"status": "ok"})

    async def task_toggle(self):
        body = await request.get_json() or {}
        task_id = str(body.get("id", "")).strip()
        enabled = bool(body.get("enabled", True))
        tasks = self.plugin.load_tasks()
        for task in tasks:
            if task.get("id") == task_id:
                task["enabled"] = enabled
                if enabled:
                    task.pop("last_sent", None)
        self.plugin.save_tasks(tasks)
        return jsonify({"status": "ok"})

    async def task_media(self):
        files = await request.files
        upload = files.get("file")
        if upload is None:
            for value in files.values():
                upload = value
                break
        if upload is None:
            raise ValueError("没有收到文件")
        name = getattr(upload, "filename", "") or "media"
        ext = os.path.splitext(name)[1].lower()
        kinds = {
            ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
            ".webp": "image", ".bmp": "image",
            ".mp3": "voice", ".wav": "voice", ".ogg": "voice", ".silk": "voice",
            ".amr": "voice", ".m4a": "voice",
            ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video", ".flv": "video",
        }
        if ext not in kinds:
            raise ValueError("不支持的文件类型")
        kind = kinds[ext]
        limit_mb = self.plugin._media_limit_mb(kind)
        raw = upload.read()
        if hasattr(raw, "__await__"):
            raw = await raw
        size_mb = len(raw) / 1024 / 1024
        if limit_mb > 0 and size_mb > limit_mb:
            raise ValueError(f"单个{kind}不能超过 {limit_mb}MB（当前 {size_mb:.1f}MB）")
        used = self.plugin.media_usage_mb(kind)
        total_limit = self.plugin.media_total_limit_mb(kind)
        if total_limit > 0 and used + size_mb > total_limit:
            label = {"image": "图片", "voice": "语音", "video": "视频"}.get(kind, kind)
            raise ValueError(
                f"{label}已超过容量上限（{total_limit}MB，当前 {used:.1f}MB），请删除一部分后上传"
            )
        media_dir = os.path.join(MEM_ROOT, "media")
        ensure_dir(media_dir)
        filename = uuid.uuid4().hex[:12] + ext
        with open(os.path.join(media_dir, filename), "wb") as fh:
            fh.write(raw)
        return jsonify({"status": "ok", "data": {"media": filename, "kind": kind}}) 
    # ------------------------------------------------------------ 导出/别名/健康
    async def bulk(self):
        """批量删除/调整等级；按 (日期, 序号) 定位，跨天也安全。"""
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        source = str(body.get("source", "daily")).strip()
        action = str(body.get("action", "delete")).strip()
        target = str(body.get("tag", "")).strip()
        items = body.get("items") or []
        if not items:
            items = [{"day": str(body.get("day") or ""), "seq": str(x)}
                     for x in (body.get("seqs") or [])]
        if not scope or not items:
            raise ValueError("缺少 scope 或条目")
        if action not in ("delete", "tag"):
            raise ValueError("未知操作")

        if source == "core":
            if action != "delete":
                raise ValueError("长期记忆只支持批量删除")
            path = os.path.join(scope_dir(scope), "core.md")
            all_lines = read_text(path).splitlines()
            drop = {int(it["seq"]) for it in items if str(it.get("seq", "")).isdigit()}
            keep = [ln for idx, ln in enumerate(all_lines) if idx not in drop]
            write_text(path, "\n".join(keep) + ("\n" if keep else ""))
            self._invalidate_dates(scope)
            return jsonify({"status": "ok", "data": {"removed": len(all_lines) - len(keep)}})

        seq_set = {str(it.get("seq")) for it in items}
        removed = moved = 0
        groups: dict[str, list[str]] = {}
        for it in items:
            groups.setdefault(str(it.get("day") or ""), []).append(str(it.get("seq")))

        if source != "daily":
            path = entries_path(scope, source, "")
            original = read_jsonl(path)
            picked = [r for r in original if str(r.get("seq")) in seq_set]
            rest = [r for r in original if str(r.get("seq")) not in seq_set]
            write_jsonl(path, rest)
            if action == "delete":
                for row in picked:
                    trash_entry(scope, source, "", row)
                    removed += 1
                self._invalidate_dates(scope)
                return jsonify({"status": "ok", "data": {"removed": removed}})
            moved = self._move_entries(scope, picked, target)
            self._invalidate_dates(scope)
            return jsonify({"status": "ok", "data": {"moved": moved}})

        for day, seqs in groups.items():
            if not day:
                raise ValueError("缺少日期")
            path = entries_path(scope, "daily", day)
            original = read_jsonl(path)
            picked = [r for r in original if str(r.get("seq")) in seqs]
            rest = [r for r in original if str(r.get("seq")) not in seqs]
            if action == "delete":
                for row in picked:
                    trash_entry(scope, "daily", day, row)
                    removed += 1
                write_jsonl(path, rest)
            else:
                write_jsonl(path, rest)
                moved += self._move_entries(scope, picked, target, day)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok", "data": {"removed": removed, "moved": moved}})

    def _move_entries(self, scope: str, rows: list[dict], target: str, day: str = "") -> int:
        """把条目改成目标等级；返回移动条数。"""
        if target not in ("重要", "普通", "低密度"):
            raise ValueError("目标等级只支持 重要/普通/低密度")
        moved = 0
        important = None
        important_seq = 0
        for row in rows:
            item = dict(row)
            item["tag"] = target
            if target == "重要":
                imp_path = os.path.join(scope_dir(scope), "important.jsonl")
                if important is None:
                    important = read_jsonl(imp_path)
                    important_seq = max([int(r.get("seq") or 0) for r in important] or [0])
                important_seq += 1
                item["seq"] = important_seq
                important.append(item)
            else:
                row_day = str(item.get("date") or day)
                daily_path = entries_path(scope, "daily", row_day)
                bucket = read_jsonl(daily_path)
                item["date"] = row_day
                item["seq"] = max([int(r.get("seq") or 0) for r in bucket] or [0]) + 1
                bucket.append(item)
                write_jsonl(daily_path, bucket)
            moved += 1
        if important is not None:
            write_jsonl(os.path.join(scope_dir(scope), "important.jsonl"), important)
        return moved

    async def export(self):
        scope = (request.args.get("scope") or "").strip()
        fmt = (request.args.get("format") or "json").strip().lower()
        device = (request.args.get("device") or "").strip()[:32]
        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        if not scope:
            raise ValueError("缺少 scope")

        def in_range(day: str) -> bool:
            if start and day < start:
                return False
            if end and day > end:
                return False
            return True

        from .main import refresh_index, scope_paths, write_plain_json  # 延迟导入，避免循环

        paths = scope_paths(scope)
        core = read_text(paths["core"])
        important = [r for r in read_jsonl(paths["important"]) if in_range(str(r.get("date", ""))[:10])]
        remind = [r for r in read_jsonl(paths["remind"])
                  if in_range(str(r.get("due", ""))[:10] or str(r.get("date", ""))[:10])]
        daily = {}
        daily_dir = paths["daily_dir"]
        if os.path.isdir(daily_dir):
            for name in sorted(os.listdir(daily_dir)):
                if not name.endswith(".jsonl"):
                    continue
                day = name[:-6]
                if in_range(day):
                    rows = read_jsonl(os.path.join(daily_dir, name))
                    if rows:
                        daily[day] = rows

        stamp = time.strftime("%Y%m%d-%H%M%S")
        if fmt == "md":
            lines = [f"# 鱼鱼日记导出 · {scope}", "", f"导出时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
            if core.strip():
                lines += ["## 长期记忆", "", core.strip(), ""]
            if important:
                lines += ["## 重要", ""]
                lines += [f"- {r.get('date', '')} [{r.get('topic', '')}] {r.get('text', '')}" for r in important]
                lines.append("")
            if remind:
                lines += ["## 提醒", ""]
                lines += [f"- {r.get('due', '')} {r.get('text', '')}" for r in remind]
                lines.append("")
            for day, rows in daily.items():
                lines += [f"## {day}", ""]
                for r in rows:
                    lines.append(f"- [{r.get('tag', '')}] {r.get('text', '')}")
                lines.append("")
            content = "\n".join(lines)
            filename = f"memory5-{scope.replace(':', '_')}-{stamp}.md"
        else:
            payload = {
                "scope": scope,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "core": core,
                "important": important,
                "remind": remind,
                "daily": daily,
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            filename = f"memory5-{scope.replace(':', '_')}-{stamp}.json"

        try:
            refresh_index(scope)
        except Exception:  # noqa: BLE001
            pass
        try:
            history_path = os.path.join(MEM_ROOT, "export_history.json")
            history = read_json(history_path, []) or []
            history.append({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "scope": scope,
                "format": fmt,
                "filename": filename,
                "bytes": len(content.encode("utf-8")),
                "device": device or "未知",
            })
            write_plain_json(history_path, history[-50:])
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"status": "ok", "data": {
            "filename": filename, "content": content, "device": device or "未知",
        }})

    def _nickname(self, scope: str, qq: str) -> str:
        """从最近的原始消息里找这个 QQ 的昵称。"""
        raw_dir = os.path.join(scope_dir(scope), "raw")
        if not os.path.isdir(raw_dir):
            return ""
        for name in sorted(os.listdir(raw_dir), reverse=True)[:7]:
            if not name.endswith(".jsonl"):
                continue
            for row in reversed(read_jsonl(os.path.join(raw_dir, name))):
                if str(row.get("qq")) == str(qq) and str(row.get("name") or "").strip():
                    return str(row["name"]).strip()
        return ""

    async def aliases(self):
        """按 QQ 聚合：QQ -> 昵称 + 别名列表（含置信度）。"""
        scope = (request.args.get("scope") or "").strip()
        if not scope:
            raise ValueError("缺少 scope")
        data = read_json(os.path.join(scope_dir(scope), "aliases.json"), {}) or {}
        groups = []
        for qq, items in data.items():
            aliases = []
            for item in items or []:
                aliases.append({
                    "alias": item.get("alias", ""),
                    "confidence": float(item.get("confidence") or 0),
                    "source": item.get("source", "other"),
                    "last_seen": item.get("last_seen", ""),
                })
            aliases.sort(key=lambda a: -a["confidence"])
            groups.append({"qq": qq, "nickname": self._nickname(scope, qq), "aliases": aliases})
        groups.sort(key=lambda g: g["qq"])
        return jsonify({"status": "ok", "data": groups})

    async def aliases_update(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        qq = str(body.get("qq", "")).strip()
        old = str(body.get("old", "")).strip()
        new = str(body.get("new", "")).strip()[:32]
        try:
            confidence = float(body.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        if not scope or not qq or not old:
            raise ValueError("缺少 scope / qq / old")
        path = os.path.join(scope_dir(scope), "aliases.json")
        data = read_json(path, {}) or {}
        bucket = data.get(qq, [])
        for item in bucket:
            if item.get("alias") == old:
                if new:
                    item["alias"] = new[:12]
                if confidence is not None:
                    item["confidence"] = max(0.0, min(1.0, confidence))
                item["last_seen"] = time.strftime("%Y-%m-%d")
                break
        else:
            raise ValueError("没有这个别名")
        # 改完可能重名，去重保高置信度
        dedup: dict[str, dict] = {}
        for item in bucket:
            key = item.get("alias", "")
            if key and (key not in dedup or item.get("confidence", 0) > dedup[key].get("confidence", 0)):
                dedup[key] = item
        data[qq] = list(dedup.values())
        write_json(path, data)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok"})

    async def aliases_delete(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        qq = str(body.get("qq", "")).strip()
        alias = str(body.get("alias", "")).strip()
        if not scope or not qq or not alias:
            raise ValueError("缺少 scope / qq / alias")
        path = os.path.join(scope_dir(scope), "aliases.json")
        data = read_json(path, {}) or {}
        bucket = [i for i in data.get(qq, []) if i.get("alias") != alias]
        if bucket:
            data[qq] = bucket
        else:
            data.pop(qq, None)
        write_json(path, data)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok"})

    async def aliases_merge(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        src = str(body.get("from", "")).strip()
        dst = str(body.get("to", "")).strip()
        if not scope or not src or not dst or src == dst:
            raise ValueError("需要 scope、from、to，且不能相同")
        path = os.path.join(scope_dir(scope), "aliases.json")
        data = read_json(path, {}) or {}
        merged = 0
        for qq, items in list(data.items()):
            bucket = []
            seen = set()
            for item in items or []:
                alias = item.get("alias", "")
                if alias == src:
                    alias = dst
                    merged += 1
                if not alias or alias in seen:
                    continue
                seen.add(alias)
                item["alias"] = alias
                bucket.append(item)
            data[qq] = bucket
        write_json(path, data)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok", "data": {"merged": merged}})

    async def export_history_clear(self):
        """清空导出历史（只清记录，不动任何日记）。"""
        from .main import write_plain_json

        path = os.path.join(MEM_ROOT, "export_history.json")
        write_plain_json(path, [])
        self._health_cache = {"ts": 0.0, "data": None}
        return jsonify({"status": "ok"})

    async def trash_clear(self):
        """彻底清空回收站：删除里面的文件与整目录条目。"""
        removed = 0
        if os.path.isdir(TRASH_DIR):
            for name in os.listdir(TRASH_DIR):
                path = os.path.join(TRASH_DIR, name)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    removed += 1
                except OSError:
                    continue
        self._dates_cache.clear()
        self._health_cache = {"ts": 0.0, "data": None}
        return jsonify({"status": "ok", "data": {"removed": removed}})

    async def export_history(self):
        rows = read_json(os.path.join(MEM_ROOT, "export_history.json"), []) or []
        return jsonify({"status": "ok", "data": list(reversed(rows[-20:]))})


    async def selfcheck(self):
        """跑一遍关键链路，把结果写到插件数据目录，返回日志路径。"""
        from .crypto import enc_key, read_text, write_text

        lines = []
        ok = True

        def add(name, passed, detail=""):
            nonlocal ok
            ok = ok and passed
            lines.append(f"[{'OK' if passed else 'FAIL'}] {name} {detail}".strip())

        data_root = os.path.dirname(os.path.dirname(os.path.dirname(MEM_ROOT)))
        add("数据目录可写", os.access(MEM_ROOT, os.W_OK), MEM_ROOT)
        try:
            from .main import VERSION
            add("版本", True, VERSION)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .crypto import crypto_mode, key_age_seconds
            add("加密模式", True, crypto_mode())
            add("密钥年龄", True, f"{key_age_seconds() / 3600:.1f}h")
        except Exception:  # noqa: BLE001
            pass
        try:
            add("会话数", True, str(len(getattr(self.plugin, "scopes", []))))
            state = self.plugin._admin_state()
            add("管理员", True, f"已授权 {len(state.get('authorized', []))} / 待确认 {len(state.get('pending', {}))}")
        except Exception:  # noqa: BLE001
            pass
        add("密钥存在", enc_key() is not None, "")
        probe = os.path.join(MEM_ROOT, ".selfcheck.tmp")
        try:
            write_text(probe, "selfcheck")
            good = read_text(probe) == "selfcheck"
            add("加密读写", good)
        except Exception as exc:  # noqa: BLE001
            add("加密读写", False, type(exc).__name__)
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
        try:
            import sqlite3
            db = os.path.join(data_root, "data_v4.db")
            db_uri = f"file:{db}?mode=ro"
            if os.name == "nt":
                db_uri = "file:/" + db.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(db_uri, uri=True, timeout=5)
            con.execute("select 1").fetchone()
            con.close()
            add("数据库只读", True)
        except Exception as exc:  # noqa: BLE001
            add("数据库只读", False, type(exc).__name__)
        try:
            provider = self.plugin.context.get_using_provider()
            add("模型提供商", provider is not None)
        except Exception as exc:  # noqa: BLE001
            add("模型提供商", False, type(exc).__name__)
        try:
            usage = shutil.disk_usage(MEM_ROOT)
            add("磁盘剩余", usage.free > 50 * 1024 * 1024, f"{usage.free // (1024 * 1024)}MB")
        except Exception as exc:  # noqa: BLE001
            add("磁盘剩余", False, type(exc).__name__)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(os.path.dirname(MEM_ROOT), f"selfcheck-{stamp}.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("memory5 自检 " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            fh.write("\n".join(lines) + "\n")
        return jsonify({"status": "ok", "data": {"ok": ok, "path": log_path, "lines": lines}})

    async def health(self):
        from .main import VERSION, refresh_index

        now_ts = time.time()
        cached = self._health_cache.get("data")
        if cached and now_ts - float(self._health_cache.get("ts") or 0) < 20:
            return jsonify({"status": "ok", "data": cached})

        cache = {}
        try:
            from .crypto import cache_by_scope, cache_entries, cache_stats

            cache = cache_stats()
            cache["entries"] = cache_entries(20)
            cache["by_scope"] = cache_by_scope()
        except Exception:  # noqa: BLE001
            pass
        mode = suite = version = ""
        key_age = -1
        try:
            from .crypto import crypto_mode, crypto_suite, crypto_version, key_age_seconds

            mode = crypto_mode()
            suite = crypto_suite()
            version = crypto_version()
            key_age = key_age_seconds()
        except Exception:  # noqa: BLE001
            pass

        scopes = []
        for name in sorted(os.listdir(MEM_ROOT)) if os.path.isdir(MEM_ROOT) else []:
            if not (name.startswith("group_") or name.startswith("private_")):
                continue
            scope = name.replace("_", ":", 1)
            try:
                index = read_json(os.path.join(scope_dir(scope), "index.json"), None)
                if not isinstance(index, dict):
                    index = {}
                # 长期/重要/提醒 直接读文件，数目永远和实际一致；只有日记总量用索引
                core_lines = [ln for ln in read_text(os.path.join(scope_dir(scope), "core.md")).splitlines()
                              if ln.strip().startswith("- ")]
                scopes.append({
                    "scope": scope,
                    "core": len(core_lines),
                    "important": len(read_jsonl(os.path.join(scope_dir(scope), "important.jsonl"))),
                    "remind": len(read_jsonl(os.path.join(scope_dir(scope), "remind.jsonl"))),
                    "daily": index.get("total", 0),
                    "index": index,
                })
            except Exception:  # noqa: BLE001
                scopes.append({"scope": scope, "index": {}})

        policy = read_json(os.path.join(MEM_ROOT, "backup_policy.json"), {})
        verify = read_json(os.path.join(MEM_ROOT, "backup_verify.json"), {})
        exports = read_json(os.path.join(MEM_ROOT, "export_history.json"), []) or []
        panel_enabled = bool(self.plugin._get("health_panel", True))
        payload = {
            "panel_enabled": panel_enabled,
            "export_total": len(exports),
            "exports": list(reversed(exports[-20:])),
            "version": VERSION,
            "encryption": {"mode": mode, "suite": suite, "format": version,
                           "key_age_hours": round(key_age / 3600, 1) if key_age >= 0 else None},
            "cache": cache,
            "scopes": scopes,
            "backup": {"policy": policy, "last_verify": verify},
        }
        self._health_cache = {"ts": now_ts, "data": payload}
        return jsonify({"status": "ok", "data": payload})

class Memory5WebUI(_TasksMixin):
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._dates_cache: dict[str, tuple[float, list[str]]] = {}
        self._health_cache: dict = {"ts": 0.0, "data": None}

    def register(self) -> None:
        ctx = self.plugin.context
        for path, handler, methods, desc in (
            ("/memory5/scopes", self.scopes, ["GET"], "列出记忆范围"),
            ("/memory5/bootstrap", self.bootstrap, ["GET"], "一次拿到范围+日期"),
            ("/memory5/days", self.days, ["GET"], "列出日期"),
            ("/memory5/dates", self.dates, ["GET"], "列出所有页签可用日期"),
            ("/memory5/page", self.page, ["GET"], "合并页面数据"),
            ("/memory5/update", self.update, ["POST"], "修改条目"),
            ("/memory5/delete", self.delete, ["POST"], "删除条目"),
            ("/memory5/trash", self.trash, ["GET"], "回收站"),
            ("/memory5/export", self.export, ["GET"], "导出日记"),
            ("/memory5/aliases", self.aliases, ["GET"], "别名列表"),
            ("/memory5/aliases-merge", self.aliases_merge, ["POST"], "合并别名"),
            ("/memory5/aliases-update", self.aliases_update, ["POST"], "修改别名"),
            ("/memory5/aliases-delete", self.aliases_delete, ["POST"], "删除别名"),
            ("/memory5/health", self.health, ["GET"], "健康状态"),
            ("/memory5/selfcheck", self.selfcheck, ["POST"], "自检"),
            ("/memory5/export-history", self.export_history, ["GET"], "导出历史"),
            ("/memory5/export-history-clear", self.export_history_clear, ["POST"], "清空导出历史"),
            ("/memory5/trash-clear", self.trash_clear, ["POST"], "清空回收站"),
            ("/memory5/bulk", self.bulk, ["POST"], "批量操作"),
            ("/memory5/restore", self.restore, ["POST"], "恢复条目"),
            ("/memory5/purge", self.purge, ["POST"], "彻底删除"),
            ("/memory5/purge-scope", self.purge_scope, ["POST"], "清空某范围"),
            ("/memory5/archive", self.archive, ["POST"], "立即归档"),
            ("/memory5/add", self.add, ["POST"], "手动新增记忆"),
            ("/memory5/tasks", self.tasks, ["GET"], "定时任务列表"),
            ("/memory5/task-save", self.task_save, ["POST"], "保存定时任务"),
            ("/memory5/task-delete", self.task_delete, ["POST"], "删除定时任务"),
            ("/memory5/task-toggle", self.task_toggle, ["POST"], "启用/停用定时任务"),
            ("/memory5/task-media", self.task_media, ["POST"], "上传任务富媒体"),
        ):
            ctx.register_web_api(path, self._wrap(handler), methods, desc)

    @staticmethod
    def _wrap(handler):
        async def wrapped():
            try:
                return await handler()
            except ValueError as exc:
                return jsonify({"status": "error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover
                logger.error(f"memory5 webui: {type(exc).__name__}: {exc}")
                return jsonify({"status": "error", "message": f"{type(exc).__name__}: {exc}"})

        return wrapped

    # ------------------------------------------------------------------ GET
    async def scopes(self):
        scopes = []
        for name in sorted(os.listdir(MEM_ROOT)) if os.path.isdir(MEM_ROOT) else []:
            path = os.path.join(MEM_ROOT, name)
            if not os.path.isdir(path):
                continue
            if not (name.startswith("group_") or name.startswith("private_")):
                continue
            scope = name.replace("_", ":", 1)
            days = len(glob.glob(os.path.join(path, "daily", "*.jsonl")))
            scopes.append({"scope": scope, "days": days})
        return jsonify({"status": "ok", "data": scopes})

    async def days(self):
        scope = (request.args.get("scope") or "").strip()
        if not scope:
            raise ValueError("缺少 scope")
        files = sorted(glob.glob(os.path.join(scope_dir(scope), "daily", "*.jsonl")))
        days = [os.path.basename(f)[:-6] for f in files]
        return jsonify({"status": "ok", "data": days[::-1]})

    def _invalidate_dates(self, scope: str | None = None) -> None:
        """数据变了就让日期缓存失效，并刷新轻量索引。"""
        self._health_cache = {"ts": 0.0, "data": None}
        if scope:
            self._dates_cache.pop(scope, None)
            try:
                from .main import refresh_index

                refresh_index(scope)
            except Exception:  # noqa: BLE001
                pass
        else:
            self._dates_cache.clear()

    async def dates(self):
        """所有页签能用到的日期：日记文件名 + 重要/提醒/长期的条目日期。"""
        scope = (request.args.get("scope") or "").strip()
        if not scope:
            raise ValueError("缺少 scope")
        return jsonify({"status": "ok", "data": self._scope_dates(scope)})

    def _scope_dates(self, scope: str) -> list[str]:
        cached = self._dates_cache.get(scope)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        base = scope_dir(scope)
        found: set[str] = set()
        for path in glob.glob(os.path.join(base, "daily", "*.jsonl")):
            found.add(os.path.basename(path)[:-6])
        for row in read_jsonl(os.path.join(base, "important.jsonl")):
            if row.get("date"):
                found.add(str(row["date"])[:10])
        for row in read_jsonl(os.path.join(base, "remind.jsonl")):
            due = str(row.get("due") or "")[:10]
            if due:
                found.add(due)
            if row.get("date"):
                found.add(str(row["date"])[:10])
        try:
            text = read_text(os.path.join(base, "core.md"))
            for item in core_items(text):
                if item.get("date"):
                    found.add(str(item["date"])[:10])
        except OSError:
            pass
        dates = sorted(found, reverse=True)
        self._dates_cache[scope] = (time.time(), dates)
        return dates

    async def bootstrap(self):
        """首屏一次拿全：范围列表 + 第一个范围的日期，减少页面往返。"""
        scopes = []
        for name in sorted(os.listdir(MEM_ROOT)) if os.path.isdir(MEM_ROOT) else []:
            path = os.path.join(MEM_ROOT, name)
            if not os.path.isdir(path):
                continue
            if not (name.startswith("group_") or name.startswith("private_")):
                continue
            scopes.append({"scope": name.replace("_", ":", 1),
                           "days": len(glob.glob(os.path.join(path, "daily", "*.jsonl")))})
        payload = {"scopes": scopes, "dates": {}}
        if scopes:
            payload["dates"][scopes[0]["scope"]] = self._scope_dates(scopes[0]["scope"])
        return jsonify({"status": "ok", "data": payload})

    def _page_args(self, total: int) -> tuple[int, int, int]:
        try:
            page = int(request.args.get("page", "1"))
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.args.get("page_size", "10"))
        except (TypeError, ValueError):
            size = 10
        size = max(1, min(size, 100))
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(page, pages))
        return page, size, pages

    def _paged(self, rows: list) -> dict:
        page, size, pages = self._page_args(len(rows))
        start = (page - 1) * size
        return {
            "items": rows[start:start + size],
            "page": page,
            "pages": pages,
            "total": len(rows),
            "page_size": size,
        }

    async def page(self):
        """合并接口：一次返回日期列表 + 当前页条目。"""
        scope = (request.args.get("scope") or "").strip()
        tab = (request.args.get("tab") or "core").strip()
        day = (request.args.get("day") or "").strip()
        query = (request.args.get("q") or "").strip()[:100]
        if not scope:
            raise ValueError("缺少 scope")

        dates = self._scope_dates(scope)

        if tab == "core":
            items = core_items(read_text(os.path.join(scope_dir(scope), "core.md")))
            for item in items:
                item["tag"] = "长期"
                item["seq"] = item.get("index", 0)
            if day:
                items = [i for i in items if i.get("date") == day]
            if query:
                items = [i for i in items
                         if query in str(i.get("subject", "")) or query in str(i.get("text", ""))]
            payload = self._paged(items)
        else:
            source = tab if tab in ("important", "remind") else "daily"
            if source == "daily":
                tag = "低密度" if tab == "low" else "普通"
                if day:
                    rows = read_jsonl(entries_path(scope, "daily", day))
                else:  # 全部日期
                    rows = []
                    for path in sorted(glob.glob(os.path.join(scope_dir(scope), "daily", "*.jsonl"))):
                        rows.extend(read_jsonl(path))
                rows = [r for r in rows if r.get("tag") == tag]
            else:
                rows = read_jsonl(entries_path(scope, source, day))
                if day:
                    if source == "remind":
                        rows = [r for r in rows
                                if str(r.get("due", ""))[:10] == day or str(r.get("date", "")) == day]
                    else:
                        rows = [r for r in rows if str(r.get("date", "")) == day]
            if query:
                rows = [r for r in rows
                        if query in str(r.get("subject", "")) or query in str(r.get("text", ""))]
            rows.sort(key=lambda r: (r.get("date", ""), r.get("seq", 0)))
            payload = self._paged(rows)

        payload["dates"] = dates
        return jsonify({"status": "ok", "data": payload})

    async def update(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        source = str(body.get("source", "daily")).strip()
        day = str(body.get("day", "")).strip()
        seq = body.get("seq")
        text = str(body.get("text", "")).strip()
        tag = str(body.get("tag", "")).strip()
        if not scope or not text:
            raise ValueError("缺少 scope 或 text")
        if source == "core":
            path = os.path.join(scope_dir(scope), "core.md")
            current = read_text(path)
            updated, _ = rewrite_core(current, int(body.get("index", -1)), new_text=text)
            write_text(path, updated)
            self._invalidate_dates(scope)
            return jsonify({"status": "ok"})
        path = entries_path(scope, source, day)
        rows = read_jsonl(path)
        for row in rows:
            if str(row.get("seq")) == str(seq) and (not tag or row.get("tag") == tag):
                row["text"] = text
                if tag:
                    row["tag"] = tag
                break
        else:
            raise ValueError("没找到这条记录")
        write_jsonl(path, rows)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok"})

    async def delete(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        source = str(body.get("source", "daily")).strip()
        day = str(body.get("day", "")).strip()
        seq = body.get("seq")
        if source == "core":
            core_path = os.path.join(scope_dir(scope), "core.md")
            current = read_text(core_path)
            updated, removed_line = rewrite_core(current, int(body.get("index", -1)), remove=True)
            write_text(core_path, updated)
            tid = trash_entry(scope, "core", day, {"line": removed_line})
            self._invalidate_dates(scope)
            return jsonify({"status": "ok", "data": {"id": tid}})
        path = entries_path(scope, source, day)
        rows = read_jsonl(path)
        keep, removed = [], None
        for row in rows:
            if removed is None and str(row.get("seq")) == str(seq):
                removed = row
                continue
            keep.append(row)
        if removed is None:
            raise ValueError("没找到这条记录")
        tid = trash_entry(scope, source, day, removed)
        write_jsonl(path, keep)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok", "data": {"id": tid}})

    async def trash(self):
        purge_old_trash()
        paths = sorted(glob.glob(os.path.join(TRASH_DIR, "*.json")), reverse=True)
        # 只读当前页的文件，不把整个回收站都读进内存
        page, size, pages = self._page_args(len(paths))
        start = (page - 1) * size
        items = []
        for path in paths[start:start + size]:
            payload = read_json(path, None)
            if payload:
                items.append(payload)
        return jsonify({"status": "ok", "data": {
            "items": items, "page": page, "pages": pages,
            "total": len(paths), "page_size": size,
        }})

    async def restore(self):
        body = await request.get_json() or {}
        tid = str(body.get("id", "")).strip()
        path = os.path.join(TRASH_DIR, f"{tid}.json")
        payload = read_json(path, None)
        if not payload:
            raise ValueError("回收站里没有这条")
        if payload["source"] == "core":
            core_path = os.path.join(scope_dir(payload["scope"]), "core.md")
            current = read_text(core_path)
            line = payload["entry"].get("line", "")
            write_text(core_path, (current + line + "\n") if current else line + "\n")
            os.remove(path)
            self._invalidate_dates(payload["scope"])
            return jsonify({"status": "ok"})
        target = entries_path(payload["scope"], payload["source"], payload["day"])
        rows = read_jsonl(target)
        rows.append(payload["entry"])
        rows.sort(key=lambda r: (r.get("date", ""), r.get("seq", 0)))
        write_jsonl(target, rows)
        os.remove(path)
        self._invalidate_dates(payload["scope"])
        return jsonify({"status": "ok"})

    async def purge(self):
        body = await request.get_json() or {}
        tid = str(body.get("id", "")).strip()
        path = os.path.join(TRASH_DIR, f"{tid}.json")
        if os.path.exists(path):
            os.remove(path)
        return jsonify({"status": "ok"})

    async def purge_scope(self):
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise ValueError("缺少 scope")
        target = scope_dir(scope)
        if not os.path.isdir(target):
            raise ValueError("没有这个范围")
        ensure_dir(TRASH_DIR)
        stamp = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        shutil.move(target, os.path.join(TRASH_DIR, f"scope-{stamp}"))
        self._invalidate_dates(scope)
        return jsonify({"status": "ok"})

    async def archive(self):
        """手动触发一次归档；只在面板里可用，不暴露成聊天指令。"""
        if getattr(self.plugin, "_archiving", False):
            return jsonify({"status": "ok", "message": "正在归档中，请稍候"})

        async def run() -> None:
            try:
                await self.plugin.trigger_archive()
            except Exception as exc:  # pragma: no cover
                logger.warning(f"memory5: 手动归档失败 {type(exc).__name__}: {exc}")

        asyncio.create_task(run())
        return jsonify({"status": "ok", "message": "已开始归档，完成后会发通知"})

    async def add(self):
        """手动新增一条长期/重要/提醒记忆。"""
        body = await request.get_json() or {}
        scope = str(body.get("scope", "")).strip()
        source = str(body.get("source", "")).strip()
        subject = str(body.get("subject", "")).strip()
        topic = str(body.get("topic", "")).strip()[:8] or "事件"
        text = str(body.get("text", "")).strip()
        due = str(body.get("due", "")).strip()
        if not scope:
            raise ValueError("缺少 scope")
        if not subject.isdigit():
            raise ValueError("主体 QQ 必须是纯数字")
        if not text:
            raise ValueError("内容不能为空")
        if source not in ("core", "important", "remind"):
            raise ValueError("只支持 长期/重要/提醒")
        now = time.localtime()
        day = time.strftime("%Y-%m-%d", now)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", now)

        if source == "core":
            path = os.path.join(scope_dir(scope), "core.md")
            current = read_text(path)
            line = f"- [{topic}] {subject} :: {text}（更新 {day}）"
            write_text(path, (current + line + "\n") if current else line + "\n")
            return jsonify({"status": "ok"})

        if source == "remind":
            if not due:
                raise ValueError("提醒需要填写到期时间")
            due_iso = due.replace(" ", "T")
            if len(due_iso) == 16:
                due_iso += ":00"
            entry = {
                "date": day, "seq": 0, "subject": subject, "topic": topic,
                "tag": "提醒", "text": text, "ts": iso,
                "stability": 2, "impact": 2, "info": 2,
                "due": due_iso, "sent": [],
                "repeat": str(body.get("repeat") or "") if str(body.get("repeat") or "") in
                          ("daily", "weekly", "weekdays") else "",
                "fallback": (str(body.get("fallback") or "important").strip().lower()
                             if str(body.get("fallback") or "important").strip().lower()
                             in ("important", "normal", "delete") else "important"),
            }
            path = os.path.join(scope_dir(scope), "remind.jsonl")
        else:
            entry = {
                "date": day, "seq": 0, "subject": subject, "topic": topic,
                "tag": "重要", "text": text, "ts": iso,
                "stability": 3, "impact": 3, "info": 3,
                "due": None, "remaining": None,
            }
            path = os.path.join(scope_dir(scope), "important.jsonl")

        rows = read_jsonl(path)
        entry["seq"] = max([int(r.get("seq", 0) or 0) for r in rows] + [0]) + 1
        rows.append(entry)
        write_jsonl(path, rows)
        self._invalidate_dates(scope)
        return jsonify({"status": "ok", "data": entry})




