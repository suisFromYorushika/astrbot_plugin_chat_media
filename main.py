# -*- coding: utf-8 -*-
"""群聊历史与媒体助手。

三件事：
1. 给模型 query_group_history / list_history_images / send_history_image 三个工具，
   按「日期 / 时间点 / 谁发的 / 关键词」检索本群历史，并把历史图片发回群里。
2. 两个等价的手打指令：/群史、/发图。
3. 后台把新消息的图片/视频/语音/文件落盘，并把本地路径补写回官方那条记录。

设计原则：所有回调都吞异常，绝不因为本插件出错而影响机器人正常回复。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess as _sp
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record, Video
from astrbot.api.message_components import File as FileComp
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

PLUGIN = "chat_media"
DEFAULT_DB = "/AstrBot/data/data_v4.db"
DEFAULT_ROOT = "/AstrBot/data/qqarchive"
DEFAULT_MEDIA_DIR = "/AstrBot/data/qqarchive/media_new"

SEG2KIND = {"Image": "image", "Record": "voice", "Video": "video", "File": "file"}
KIND_CN = {"image": "图片", "video": "视频", "voice": "语音", "file": "文件", "emoji": "表情包"}


class QQArchivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.cfg = config if isinstance(config, dict) else {}
        self.db_path = str(self.cfg.get("db_path") or DEFAULT_DB)
        self.root = Path(str(self.cfg.get("archive_root") or DEFAULT_ROOT))
        self.media_dir = Path(str(self.cfg.get("media_dir") or DEFAULT_MEDIA_DIR))
        logger.info("[%s] 已加载 | db=%s | root=%s", PLUGIN, self.db_path, self.root)

    # ------------------------------------------------------------------ 基础设施

    def _conn(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @staticmethod
    def _parse_content(content_json):
        """官方 content JSON -> (纯文本, _media 列表, 引用信息)"""
        try:
            d = json.loads(content_json)
        except Exception:
            return "", [], None
        if not isinstance(d, dict):
            return "", [], None
        parts, quote = [], None
        for item in d.get("message", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "plain":
                t = item.get("text", "")
                if t:
                    parts.append(t)
            elif item.get("type") == "reply" and not quote:
                quote = {"name": item.get("sender_name", ""), "text": item.get("text", "")}
        media = d.get("_media")
        return " ".join(parts).strip(), (media if isinstance(media, list) else []), quote

    def _scan(self, session, date="", clock="", sender="", keyword="",
              media_only=False, limit=200):
        """按条件扫描本会话历史（时间升序）。"""
        max_rows = int(self.cfg.get("max_scan_rows") or 20000)
        where, params = ["user_id = ?"], [session]
        rng = self._local_range(date, clock)
        if rng:
            where.append("created_at >= ? AND created_at < ?")
            params += [rng[0], rng[1]]
        elif clock:
            where.append("created_at LIKE ?")
            params.append("% " + clock.strip() + "%")
        if sender:
            where.append("(sender_name LIKE ? OR sender_id LIKE ?)")
            params += ["%" + sender.strip() + "%", "%" + sender.strip() + "%"]
        sql = ("SELECT created_at, sender_name, sender_id, content FROM platform_message_history "
               "WHERE " + " AND ".join(where) + " ORDER BY created_at ASC LIMIT ?")
        params.append(max_rows)
        out = []
        con = self._conn()
        try:
            for created_at, sname, sid, content in con.execute(sql, params):
                text, media, quote = self._parse_content(content)
                if keyword and keyword.strip() and keyword.strip() not in text:
                    continue
                if media_only and not media:
                    continue
                out.append({"created_at": created_at or "", "sender_name": sname or "",
                            "sender_id": sid or "", "text": text, "media": media, "quote": quote})
                if len(out) >= limit:
                    break
        finally:
            con.close()
        return out

    def _local_range(self, date, clock):
        """把用户说的【本地日期/时间】换算成数据库里的 UTC 区间。

        AstrBot 存 created_at 用 UTC（实测：容器 TZ=Asia/Shanghai，但同一时刻库里少 8 小时），
        而用户说的是本地时间，所以这里必须做一次换算。
        """
        date = (date or "").strip()
        clock = (clock or "").strip()
        off = timedelta(hours=float(self.cfg.get("utc_offset_hours", 8) or 8))
        try:
            if date and len(date) == 7:                 # YYYY-MM
                lo = datetime.strptime(date + "-01 00:00:00", "%Y-%m-%d %H:%M:%S")
                hi = (lo.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0)
            elif date and clock:                        # YYYY-MM-DD + HH:MM
                c = datetime.strptime(date + " " + clock + ":00", "%Y-%m-%d %H:%M:%S")
                lo, hi = c - timedelta(seconds=90), c + timedelta(seconds=90)
            elif date:                                  # YYYY-MM-DD
                lo = datetime.strptime(date + " 00:00:00", "%Y-%m-%d %H:%M:%S")
                hi = lo + timedelta(days=1)
            else:
                return None
        except Exception:
            return None
        f = "%Y-%m-%d %H:%M:%S.%f"
        return (lo - off).strftime(f), (hi - off).strftime(f)

    # 扩展名 -> 真实媒体类型（_media.kind 有时不准：视频可能只存了封面 png）
    EXT_KIND = {
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
        ".webp": "image", ".bmp": "image",
        ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video", ".webm": "video",
        ".amr": "voice", ".silk": "voice", ".mp3": "voice", ".wav": "voice",
        ".ogg": "voice", ".m4a": "voice", ".flac": "voice",
    }

    def _real_kind(self, media, path):
        """以磁盘上的真实文件类型为准。"""
        k = (self.EXT_KIND.get(Path(path).suffix.lower()) if path else None)
        if k:
            return k
        return media.get("kind") or "image"

    @staticmethod
    def _component(kind, path, name=""):
        if kind == "video":
            return Video.fromFileSystem(str(path))
        if kind == "voice":
            return Record.fromFileSystem(str(path))
        if kind == "file":
            return FileComp(name or Path(path).name, file=str(path))
        return Image.fromFileSystem(str(path))

    def _abs_media(self, rel):
        """把 _media.file 解析成容器内的绝对路径。"""
        rel = (rel or "").strip()
        if not rel:
            return None
        p = Path(rel)
        if p.is_absolute():
            return p if p.is_file() else None
        for cand in (self.root / rel, self.root / "media" / rel, self.root / "media" / Path(rel).name):
            if cand.is_file():
                return cand
        return None

    @staticmethod
    def _fmt_time(created_at):
        return (created_at or "")[:16]

    # ------------------------------------------------------------------ 工具：查历史

    @filter.llm_tool(name="query_group_history")
    async def query_group_history(self, event: AstrMessageEvent, date: str = "",
                                  clock: str = "", sender: str = "", keyword: str = "",
                                  limit: int = 60) -> str:
        """查询本群的历史聊天记录（可以查到很久以前，包括机器人还没进群时的记录）。

        Args:
            date(string): 日期，支持 "2024-03-17"（某天）或 "2024-03"（某月）。留空表示不限。
            clock(string): 时间点，形如 "18:24"，只看这一分钟附近。留空表示不限。
            sender(string): 只看某个人发的，填群名片或 QQ 号，如 "kumo"。留空表示所有人。
            keyword(string): 只看包含该关键词的消息。留空表示不筛。
            limit(number): 最多返回多少条，默认 60。
        """
        try:
            rows = self._scan(event.unified_msg_origin, date=date, clock=clock,
                              sender=sender, keyword=keyword, limit=max(1, min(int(limit or 60), 200)))
            if not rows:
                return "没有匹配的历史记录。可以放宽条件：date 支持 2024-03-17 或 2024-03；sender 填群名片；也可以只给 keyword。"
            lines = ["共 %d 条（%s ~ %s）：" % (len(rows), self._fmt_time(rows[0]["created_at"]),
                                              self._fmt_time(rows[-1]["created_at"]))]
            for r in rows:
                tag = ""
                if r["media"]:
                    kinds = []
                    for m in r["media"]:
                        k = KIND_CN.get(m.get("kind"), m.get("kind") or "媒体")
                        if k not in kinds:
                            kinds.append(k)
                    tag = " [" + "/".join(kinds) + "]"
                qt = ""
                if r["quote"] and r["quote"].get("text"):
                    qt = " (回复 %s: %s)" % (r["quote"].get("name", ""), r["quote"]["text"][:30])
                lines.append("%s %s%s：%s%s" % (self._fmt_time(r["created_at"]),
                                                r["sender_name"] or "?", tag, r["text"][:300], qt))
            if any(r["media"] for r in rows):
                lines.append("提示：带 [图片]/[视频] 等标记的消息可以用 send_history_media 把原文件发到群里。")
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("[%s] query_group_history 失败", PLUGIN)
            return "查询出错：%s" % exc

    @filter.llm_tool(name="list_history_media")
    async def list_history_media(self, event: AstrMessageEvent, date: str = "",
                                  sender: str = "", keyword: str = "", limit: int = 30) -> str:
        """列出本群历史消息里的图片/视频清单（只列有媒体的），用于确认要发哪一张。

        Args:
            date(string): 日期，如 "2024-03-17" 或 "2024-03"。留空不限。
            sender(string): 只看某人发的，如 "kumo"。留空不限。
            keyword(string): 该消息文本包含的关键词。留空不限。
            limit(number): 最多列多少条，默认 30。
        """
        try:
            rows = self._scan(event.unified_msg_origin, date=date, sender=sender,
                              keyword=keyword, media_only=True,
                              limit=max(1, min(int(limit or 30), 100)))
            if not rows:
                return "没有找到带媒体的历史消息。"
            lines = ["带媒体的历史消息 %d 条：" % len(rows)]
            for r in rows:
                names = []
                for m in r["media"]:
                    lbl = KIND_CN.get(m.get("kind"), m.get("kind") or "媒体")
                    if m.get("thumb_only"):
                        lbl += "(仅缩略图)"
                    names.append(lbl + (":" + Path(m.get("file", "")).name if m.get("file") else ""))
                lines.append("%s %s：%s | %s" % (self._fmt_time(r["created_at"]),
                                                 r["sender_name"] or "?", "、".join(names),
                                                 (r["text"] or "")[:60]))
            return "\n".join(lines)
        except Exception as exc:
            logger.exception("[%s] list_history_media 失败", PLUGIN)
            return "查询出错：%s" % exc

    @filter.llm_tool(name="send_history_media")
    async def send_history_media(self, event: AstrMessageEvent, date: str = "",
                                 clock: str = "", sender: str = "", keyword: str = "",
                                 kind: str = "image", index: int = 1) -> str:
        """把本群某条历史消息里的图片/视频/语音/文件发到当前群里。按日期/时间/发送人/关键词定位。

        Args:
            date(string): 日期，如 "2024-03-17" 或 "2024-03"。留空不限。
            clock(string): 时间点，如 "18:24"（精确到分钟）。留空不限。
            sender(string): 谁发的，如 "kumo"。留空不限。
            keyword(string): 该消息文本里的关键词。留空不限。
            kind(string): 要发哪一种，可选 image / video / voice / file / any，默认 image。
            index(number): 匹配到多个时发第几个，从 1 开始，默认 1。
        """
        try:
            want = (kind or "image").strip().lower()
            if want in ("pic", "photo", "图片"): want = "image"
            if want in ("视频",): want = "video"
            max_mb = float(self.cfg.get("max_send_mb", 50) or 50)
            rows = self._scan(event.unified_msg_origin, date=date, clock=clock,
                              sender=sender, keyword=keyword, media_only=True, limit=200)
            cands, oversize = [], 0
            for r in rows:
                for m in r["media"]:
                    p = self._abs_media(m.get("file"))
                    if not p:
                        continue
                    rk = self._real_kind(m, p)
                    if want != "any" and rk != want:
                        continue
                    if p.stat().st_size > max_mb * 1048576:
                        oversize += 1
                        continue
                    cands.append((r, rk, p))
            if not cands:
                tip = ("（另有 %d 个超过 %.0f MB 上限发不了）" % (oversize, max_mb)) if oversize else ""
                return "没有找到符合条件的媒体%s。可以先调 list_history_media 看有哪些。" % tip
            i = max(1, min(int(index or 1), len(cands)))
            row, rk, path = cands[i - 1]
            label = KIND_CN.get(rk, rk)
            chain = MessageChain([])
            if self.cfg.get("reply_on_send", True):
                chain.chain.append(Plain("%s %s 发的%s：" % (self._fmt_time(row["created_at"]),
                                                             row["sender_name"] or "?", label)))
            chain.chain.append(self._component(rk, path))
            await self.context.send_message(event.unified_msg_origin, chain)
            extra = "" if len(cands) == 1 else "（共 %d 个匹配，这是第 %d 个）" % (len(cands), i)
            return "已把 %s %s 发的%s发到群里%s，%.1f MB。" % (
                self._fmt_time(row["created_at"]), row["sender_name"] or "?", label, extra,
                path.stat().st_size / 1048576)
        except Exception as exc:
            logger.exception("[%s] send_history_media 失败", PLUGIN)
            return "发送出错：%s" % exc

    # ------------------------------------------------------------------ 手打指令

    @filter.command("群史")
    async def cmd_history(self, event: AstrMessageEvent):
        arg = (event.message_str or "").replace("/群史", "", 1).strip()
        parts = [p for p in re.split(r"\s+", arg) if p]
        date = next((p for p in parts if re.match(r"^\d{4}(-\d{2}){0,2}$", p)), "")
        rest = [p for p in parts if p != date]
        keyword = " ".join(rest)
        rows = self._scan(event.unified_msg_origin, date=date, keyword=keyword, limit=80)
        if not rows:
            yield event.plain_result("没有匹配的记录。用法：/群史 2024-03-17  或  /群史 2024-03 kumo")
            return
        head = "%s%s 共 %d 条：" % (date or "全部", (" / " + keyword) if keyword else "", len(rows))
        body = "\n".join("%s %s：%s" % (self._fmt_time(r["created_at"]),
                                        r["sender_name"] or "?", (r["text"] or "")[:80]) for r in rows[:40])
        yield event.plain_result(head + "\n" + body)

    @filter.command("发图")
    async def cmd_send_image(self, event: AstrMessageEvent):
        arg = (event.message_str or "").replace("/发图", "", 1).strip()
        parts = [p for p in re.split(r"\s+", arg) if p]
        date = next((p for p in parts if re.match(r"^\d{4}(-\d{2}){0,2}$", p)), "")
        clock = next((p for p in parts if re.match(r"^\d{1,2}:\d{2}$", p)), "")
        sender = " ".join(p for p in parts if p not in (date, clock))
        rows = self._scan(event.unified_msg_origin, date=date, clock=clock,
                          sender=sender, media_only=True, limit=50)
        for r in rows:
            for m in r["media"]:
                if (m.get("kind") or "image") != "image":
                    continue
                p = self._abs_media(m.get("file"))
                if p:
                    chain = MessageChain([])
                    chain.chain.append(Plain("%s %s 发的图片：" % (self._fmt_time(r["created_at"]),
                                                                  r["sender_name"] or "?")))
                    chain.chain.append(Image.fromFileSystem(str(p)))
                    await self.context.send_message(event.unified_msg_origin, chain)
                    return
        yield event.plain_result("没找到可发的图。用法：/发图 2024-03-17 或 /发图 2024-03-17 18:24 kumo")

    # ------------------------------------------------------------------ 后台：媒体落盘

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def archive_media(self, event: AstrMessageEvent):
        if not self.cfg.get("media_archive", True):
            return
        try:
            comps = event.get_messages() or []
            saved = []
            day = datetime.now().strftime("%Y-%m-%d")
            for comp in comps:
                raw = getattr(comp, "type", None)
                name = getattr(raw, "value", None) or str(raw)
                kind = SEG2KIND.get(name)
                if not kind:
                    continue
                src, mode = self._resolve_source(comp)
                if not src:
                    continue
                dst = await self._store(src, mode, kind, day)
                if dst:
                    saved.append({"kind": kind, "file": dst, "thumb_only": False})
            if saved:
                self._attach_media(event, saved)
        except Exception:
            logger.exception("[%s] archive_media 失败（已忽略）", PLUGIN)

    @staticmethod
    def _resolve_source(comp):
        for attr in ("path", "file", "url"):
            v = getattr(comp, attr, None)
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
            if v.startswith("file://"):
                v = v[7:]
            if v.startswith(("http://", "https://")):
                return v, "url"
            if os.path.isfile(v):
                return v, "local"
        return None, ""

    def _transcode_voice(self, path):
        """把 QQ 语音转成 mp3。失败就保留原文件。

        注意：QQ 语音后缀常写 .amr，但真身多是 **SILK v3**（文件头 #!SILK_V3），
        而 ffmpeg 没有 SILK 解码器。所以顺序是：
          SILK -> pilk 解成 PCM -> ffmpeg 编成 mp3
          普通 AMR -> 直接 ffmpeg
        pilk 是可选的（requirements.txt 里声明）；没装就跳过，不影响其它功能。
        """
        if not self.cfg.get("transcode_voice", True):
            return path
        if path.suffix.lower() not in (".amr", ".silk", ".slk"):
            return path
        mp3 = path.with_suffix(".mp3")
        try:
            with open(path, "rb") as fh:
                head = fh.read(16)
        except Exception:
            return path
        try:
            if b"SILK" in head:
                import pilk
                pcm = path.with_suffix(".pcm")
                pilk.decode(str(path), str(pcm))
                r = _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le",
                             "-ar", "24000", "-ac", "1", "-i", str(pcm),
                             "-b:a", "64k", str(mp3)], capture_output=True, timeout=120)
                pcm.unlink(missing_ok=True)
            else:
                r = _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), str(mp3)],
                            capture_output=True, timeout=120)
            if r.returncode == 0 and mp3.is_file() and mp3.stat().st_size > 0:
                path.unlink(missing_ok=True)
                logger.info("[%s] 语音已转码 %s -> %s", PLUGIN, path.name, mp3.name)
                return mp3
            logger.warning("[%s] 转码失败，保留原文件：%s", PLUGIN, r.stderr[:150])
        except ImportError:
            logger.info("[%s] 未安装 pilk，跳过 SILK 语音转码（pip install pilk）", PLUGIN)
        except Exception:
            logger.warning("[%s] 转码异常，保留原文件：%s", PLUGIN, path, exc_info=True)
        return path

    async def _store(self, src, mode, kind, day):
        ext_default = {"image": ".jpg", "video": ".mp4", "voice": ".amr", "file": ""}
        name = os.path.basename(src.split("?")[0]) or (kind + ext_default.get(kind, ""))
        if "." not in name and ext_default.get(kind):
            name += ext_default[kind]
        target_dir = self.media_dir / day
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / name
        if dst.exists():
            return str(dst)
        if mode == "local":
            shutil.copy2(src, dst)
        else:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                async with sess.get(src, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return None
                    dst.write_bytes(await resp.read())
        if kind == "voice":
            dst = self._transcode_voice(dst)
        return str(dst)

    def _attach_media(self, event, saved):
        """把刚落盘的媒体路径补写回官方那条历史记录（拿不到行号就跳过）。"""
        hid = event.get_extra("_current_platform_message_history_id")
        if not hid:
            return
        try:
            con = self._conn()
            try:
                row = con.execute("SELECT content FROM platform_message_history WHERE id = ?",
                                  (hid,)).fetchone()
                if not row:
                    return
                content = json.loads(row[0])
                exist = content.get("_media") or []
                exist.extend(saved)
                content["_media"] = exist
                con.execute("UPDATE platform_message_history SET content = ? WHERE id = ?",
                            (json.dumps(content, ensure_ascii=False), hid))
                con.commit()
            finally:
                con.close()
        except Exception:
            logger.exception("[%s] 回写 _media 失败（已忽略）", PLUGIN)

    async def terminate(self):
        logger.info("[%s] 已卸载", PLUGIN)
