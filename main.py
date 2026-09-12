"""战队对战战报插件入口。

功能：
- 排表：`/排表 [规则]` + 战队名单 → 生成随机配对的第一轮战报模板
- 提交：`/发送` + 粘贴战报文本 → 解析入库（MySQL）。友谊赛与**踢馆**都走这里，
  按首行（`战队: …` 或 `A踢馆B`）自动分流到对应的解析器。
- 查询：排行 / 战绩 / 踢馆 / 趋势图 / 导出 / 删除 / 撤销 / 帮助
- 数据存储于线上 MySQL（配置 mysql_* 字段）。
- 写入/删除按群隔离（group_id）；**统计按战队跨群聚合**（home_team），
  同一战队的多个群会合并统计——别在聚合 SQL 里加 group_id 过滤。
  唯一例外是踢馆：按 `team_a`/`team_b` 对称取数（否则守馆方拿不到分）。
"""

import asyncio
import csv
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import At, File, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Star, StarTools, register

from . import chart, lineup, stats
from .lineup import _int_to_cn, format_duel_results, format_raid_results
from .battle_report_parser import (
    KIND_RAID,
    _TOKEN_MONTH_RE,
    _cn_to_int,
    _parse_month_filter,
    _strip_ruled,
    _strip_sub,
    determine_match_winner,
    is_raid_header,
    month_range,
    settle_month_range,
    parse_battle_report,
    parse_export_payload,
    parse_raid_report,
    split_reports,
)
from .database import Database, DuplicateReportError

_SUBMIT_CMDS = ("发送", "/发送", "战报", "/战报")
_LINEUP_CMDS = ("排表", "/排表")

# 查询类命令：新名称为主（去掉 战报 前缀），旧名称 战报Xxx 仍兼容
_RANK_CMDS = ("排行", "/排行", "战报排行", "/战报排行")
_RECORD_CMDS = ("战绩", "/战绩", "战报战绩", "/战报战绩")
_RAID_CMDS = ("踢馆", "/踢馆", "战报踢馆", "/战报踢馆")
_TREND_CMDS = ("趋势", "/趋势", "战报趋势", "/战报趋势")
_EXPORT_CMDS = ("导出", "/导出", "战报导出", "/战报导出")
_SETTLE_CMDS = ("结算", "/结算")
_RESET_SETTLE_CMDS = ("重置结算", "/重置结算")

# /第N轮 命令（N 为数字或中文，第 1 轮由排表生成）
_ROUND_CMD_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零\d]+)\s*轮")


class RoundCommandFilter(CustomFilter):
    """匹配 /第N轮 形式的追加轮次命令（用 CustomFilter 避免仪表盘显示正则字符串）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        return bool(_ROUND_CMD_RE.match(event.get_message_str().strip()))

_FORMAT_EXAMPLE = (
    "格式示例：\n"
    "战队: KC VS DYG\n"
    "时间: 2026.08.01\n"
    "规则: 2/3【KOF】\n"
    "地点: 群号\n"
    "------第一轮------\n"
    "红莲  2:1  牌大\n"
    "凯撒亮  2:1  蓝大"
)


_HELP_HINT = "\n还想看点别的就发：/帮助 喵～"
# 未绑定战队 / 非群聊 的统一提示（全插件共用，避免同一种情况出现多套说法）
_NEED_HOME = "⚠️ 本群还没绑定战队喵，这个功能用不了。\n请管理/群主用 /绑定战队 <战队> 绑定一下喵～ (๑•̀ㅂ•́)و"
_NEED_GROUP = "⚠️ 这个要在群里用喵。"

# 以下几条在多个 handler 里逐字重复，抽成常量；改文案只改这里
_ERR_QUERY = "❌ 查询出错喵…稍后再试一次嘛 (；・∀・)"
_ERR_EXPORT = "❌ 导出出错喵…稍后再试一次嘛 (；・∀・)"
_NEED_BIND = (
    "喵…你还没有绑定参赛ID喵。\n"
    "先 /查ID <参赛ID> 模糊查一下，再用 /绑定ID <参赛ID> 绑上就好啦～"
)
_NO_REPORT = "❌ 群里翻不到战报喵，先用 /排表 生成一份吧。"
_ADMIN_ONLY = "❌ 这个只有超级管理员能用喵。"
_USAGE_MANAGE_ID = "用法喵：管理ID <参赛ID[,参赛ID...]> <用户名>"
_USAGE_BROADCAST = "用法喵：通告 <内容>\n　或：通告 <群号/QQ号>\n<内容>"
_GROUP_DISABLED = "🚫 群 {} 已经禁用插件功能喵。"


def _strip_command(raw: str, cmds: tuple[str, ...]) -> str:
    """剥离指令词前缀（兼容带/不带唤醒前缀）。"""
    raw = raw.strip()
    for cmd in cmds:
        if raw == cmd:
            return ""
        if raw.startswith(cmd):
            return raw[len(cmd):].strip()
    return raw


def _parse_settle_args(payload: str) -> tuple[int | None, int]:
    """解析 `/结算` 的参数 → `(月份, 奖励名次数)`。

    **不能直接用 `_parse_month_filter`** —— 它的正则锚定在**末尾**，而
    `/结算 7月 12` 的月份在中间，会解析不出月份。这里改成逐个 token 试
    `_TOKEN_MONTH_RE`（它认 `7月` / `七月` / `时间=7月`），纯数字的 token 当
    名次数，所以 `7月 12` 与 `12 7月` 两种顺序都认。

    名次数缺省 4。复用 parser 的月份正则而不是另写一套，免得两种写法逐渐走偏。
    """
    month: int | None = None
    ranks = 4
    for tok in payload.split():
        if tok.isdigit():
            ranks = int(tok)
            continue
        m = _TOKEN_MONTH_RE.match(tok)
        if m:
            month = _cn_to_int(m.group(1) or m.group(2))
    return month, ranks


def _is_report_text(text: str) -> bool:
    """文本首行是否是一份战报的开头（友谊赛 `战队: …` 或踢馆 `A踢馆B`）。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    first = lines[0].lstrip()
    return first.startswith("战队:") or is_raid_header(first)


def _parse_chunk(chunk: str) -> "ParseResult":
    """按首行分流解析：`A踢馆B` 走踢馆解析器，其余按友谊赛解析。

    两种格式的差异太大（无 `战队:`/`时间:` 前缀、无轮次、有 `(馆主)` 与 `规则`
    占位），共用一套 `parse_battle_report` 只会让友谊赛路径连带承担回归风险。
    """
    for line in chunk.splitlines():
        if line.strip():
            return parse_raid_report(chunk) if is_raid_header(line) else parse_battle_report(chunk)
    return parse_battle_report(chunk)


@register("battle_report", "RLotusX", "战队对战战报：排表、提交、排行、踢馆、趋势、导出", "1.15.0")
class BattleReportPlugin(Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.db: Database | None = None
        self.db_ready = False
        try:
            self.data_dir = StarTools.get_data_dir()
        except Exception as e:
            logger.warning(f"get_data_dir 失败，使用兜底目录: {e}")
            self.data_dir = Path("data/plugin_data/battle_report")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """初始化：覆写 /第N轮 处理器显示名 + 连接 MySQL。"""
        self._friendly_round_display()
        try:
            self.db = Database(
                host=str(self.config.get("mysql_host", "127.0.0.1")),
                port=int(self.config.get("mysql_port", 3306)),
                user=str(self.config.get("mysql_user", "root")),
                password=str(self.config.get("mysql_password", "")),
                db=str(self.config.get("mysql_db", "astrbot_battle_report")),
            )
            await self.db.initialize()
            self.db_ready = True
            logger.info(f"战报插件数据库就绪: {self.config.get('mysql_db', 'astrbot_battle_report')}")
        except Exception as e:
            logger.error(f"战报插件数据库连接失败: {e}")
            self.db_ready = False

    def _friendly_round_display(self):
        """覆写 /第N轮 处理器在仪表盘的显示名（函数名 round_cmd 不变）。

        仪表盘对 CustomFilter 处理器显示 handler_name（即函数名），这里在
        注册后把显示名改为友好的『/第N轮』。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for handler in star_handlers_registry:
                if (
                    handler.handler_name == "round_cmd"
                    and "astrbot_plugin_battle_report" in (handler.handler_module_path or "")
                ):
                    handler.handler_name = "/第N轮"
                    return
        except Exception as e:
            logger.warning(f"设置 /第N轮 显示名失败: {e}")

    async def terminate(self):
        if self.db:
            await self.db.close()

    # ---------- 内部工具 ----------

    def _check_db(self) -> str | None:
        """数据库未就绪时返回提示文案。"""
        if not self.db_ready or self.db is None:
            return "❌ 连不上数据库喵…检查一下插件配置里的 MySQL 连接信息吧 (；・∀・)"
        return None

    def _date_from(self, days: int) -> str | None:
        if not days or days <= 0:
            return None
        return (datetime.now() - timedelta(days=days)).date().isoformat()

    async def _is_manager(self, event: AstrMessageEvent) -> bool:
        """是否 AstrBot 管理员 / 群管理 / 群主。"""
        if event.is_admin():
            return True
        try:
            group = await event.get_group()
        except Exception as e:
            logger.warning(f"获取群信息失败: {e}")
            group = None
        if group is None:
            return False
        sender = str(event.get_sender_id())
        if sender == str(group.group_owner):
            return True
        admins = [str(a) for a in (group.group_admins or [])]
        return sender in admins

    @staticmethod
    def _extract_msg_text(msg: dict) -> str:
        """从 OneBot 消息对象中提取纯文本。

        优先取 message 段拼接；若某来源（message 段 / raw_message）被平台截断，
        取两者中较长者，避免只拿到半截战报。
        """
        arr = msg.get("message")
        seg_text = ""
        if isinstance(arr, list):
            parts = []
            for seg in arr:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str(seg.get("data", {}).get("text", "")))
            seg_text = "".join(parts)
        raw = str(msg.get("raw_message") or msg.get("message_str") or "")
        return seg_text if len(seg_text) >= len(raw) else raw

    async def _read_latest_report(self, event: AstrMessageEvent) -> str | None:
        """从群聊记录中读取最近一条以『战队:』开头的战报文本。"""
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        if bot is None or not group_id:
            return None
        try:
            gid = int(group_id)
        except (ValueError, TypeError):
            gid = group_id
        try:
            ret = await bot.call_action(
                "get_group_msg_history",
                group_id=gid,
                count=50,
                message_id=0,
            )
        except Exception as e:
            logger.warning(f"读取群消息历史失败: {e}")
            return None
        messages = (ret or {}).get("messages", []) if isinstance(ret, dict) else []
        best = None  # (time, text)
        for msg in messages:
            text = self._extract_msg_text(msg)
            if text and text.lstrip().startswith("战队:"):
                t = msg.get("time") or 0
                if best is None or t >= best[0]:
                    best = (t, text)
        return best[1] if best else None

    @staticmethod
    def _find_forward_id(msg: dict) -> str | None:
        """从消息对象的 message 段中提取合并转发 id。"""
        arr = msg.get("message") if isinstance(msg, dict) else None
        if isinstance(arr, list):
            for seg in arr:
                if isinstance(seg, dict) and seg.get("type") == "forward":
                    data = seg.get("data") or {}
                    for k in ("id", "res_id", "forward_id"):
                        if data.get(k):
                            return str(data[k])
        return None

    @staticmethod
    def _extract_forward_text(item: dict) -> str:
        """提取合并转发内单条消息的纯文本（兼容 content / message 两种段结构）。

        与 _extract_msg_text 同理：取 message 段拼接与 raw_message 中较长者，防平台截断。
        """
        seg_text = ""
        for key in ("content", "message"):
            arr = item.get(key)
            if isinstance(arr, list):
                parts = []
                for seg in arr:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        parts.append(str(seg.get("data", {}).get("text", "")))
                if parts:
                    seg_text = "".join(parts)
                    break
        raw = str(item.get("raw_message") or item.get("message_str") or "")
        return seg_text if len(seg_text) >= len(raw) else raw

    async def _extract_reply_reports(self, event: AstrMessageEvent) -> list[str] | None:
        """从回复引用中提取战报文本列表。

        支持：引用的消息本身是战报文本，或引用的消息是合并转发（内含多条战报）。
        无回复/获取失败返回 None。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        reply = next(
            (c for c in event.get_messages() if isinstance(c, Reply)),
            None,
        )
        if reply is None or not reply.id:
            return None
        try:
            msg = await bot.call_action("get_msg", message_id=int(reply.id))
        except Exception as e:
            logger.warning(f"获取被引用消息失败: {e}")
            return None
        if not isinstance(msg, dict):
            return None

        forward_id = self._find_forward_id(msg)
        if forward_id:
            try:
                ret = await bot.call_action("get_forward_msg", id=forward_id)
            except Exception as e:
                logger.warning(f"获取合并转发消息失败: {e}")
                return None
            inner = (ret or {}).get("messages", []) if isinstance(ret, dict) else []
            texts = [self._extract_forward_text(m) for m in inner]
            # 诊断：转发内每条消息的段数与拼接长度，用于排查平台截断
            for idx, t in enumerate(texts):
                logger.info(
                    "引用转发第 %d 条 text_len=%d forward_msg=%s",
                    idx, len(t), str(t)[:50],
                )
            # 只取战报消息（友谊赛『战队:』开头，或踢馆『A踢馆B』开头）
            return [t for t in texts if _is_report_text(t)]

        # 非转发：被引用消息自身的文本（须为战报）
        text = self._extract_msg_text(msg)
        # 诊断：记录 get_msg 返回的 message 段数量与 raw_message 长度，排查平台截断
        logger.info(
            "引用消息 text_len=%d segs=%s raw_message_len=%d text_head=%s",
            len(text),
            len(msg.get("message")) if isinstance(msg.get("message"), list) else "?",
            len(str(msg.get("raw_message") or "")),
            text[:40].replace("\n", "\\n"),
        )
        if _is_report_text(text):
            # 兜底：get_msg 返回疑似被平台截断（无法完整解析）时，从群消息历史按 ID 重取
            if _parse_chunk(text).errors:
                alt = await self._read_msg_by_id_from_history(event, reply.id)
                if alt and len(alt) > len(text):
                    logger.info("引用消息 get_msg 疑似截断，已从群消息历史取到更完整文本 (%d→%d)", len(text), len(alt))
                    text = alt
            return [text]
        return None

    async def _read_msg_by_id_from_history(self, event, message_id) -> str | None:
        """从群消息历史中按消息 ID 取文本（`get_msg` 返回被平台截断时的兜底路径）。

        取最近 50 条中 message_id 匹配的那条；未找到返回 None。
        """
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        if bot is None or not group_id:
            return None
        try:
            gid = int(group_id)
        except (ValueError, TypeError):
            gid = group_id
        try:
            ret = await bot.call_action(
                "get_group_msg_history", group_id=gid, count=50, message_id=0
            )
        except Exception as e:
            logger.warning(f"读取群消息历史失败: {e}")
            return None
        messages = (ret or {}).get("messages", []) if isinstance(ret, dict) else []
        for m in messages:
            if str(m.get("message_id")) == str(message_id):
                return self._extract_msg_text(m)
        return None

    async def _has_reply(self, event: AstrMessageEvent) -> bool:
        """消息是否带回复引用（轻量检查）。"""
        try:
            return any(isinstance(c, Reply) for c in event.get_messages())
        except Exception:
            return False

    # ---------- 排表 ----------

    @filter.command("排表", alias={"/排表"})
    async def lineup_cmd(self, event: AstrMessageEvent):
        """排表：解析名单 → 存库 → 生成随机配对模板"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err + _HELP_HINT)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 排表要在群里用喵。" + _HELP_HINT)
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _LINEUP_CMDS)
        if not payload:
            # 未传任何参数：分两段发送排表使用指南
            yield event.plain_result(
                "欢迎使用海马集团 智能排表秘书喵～ (=^･ω･^=)\n"
                "其他规则 第一行：(/排表+空格+规则名称)\n"
                "更多功能请发送：/帮助\n"
                "排表格式："
            )
            yield event.plain_result(
                "/排表\n"
                "a队:云玩家 萌新 遗老\n"
                "b队:复读机 鸽子 柠檬"
            )
            return
        result = lineup.parse_lineup(payload, self.config.get("default_rule", "2/3【KOF】"))
        if result.errors:
            yield event.plain_result(
                "❌ 排表没成功喵：\n" + "\n".join(result.errors)
                + "\n\n格式长这样喵：\nKC:红莲 凯撒亮 悠悠球\nDYG:老千 蓝大 红大"
                + _HELP_HINT
            )
            return
        if len(result.teams) < 2:
            yield event.plain_result("❌ 至少要两支队伍才排得起来喵。" + _HELP_HINT)
            return

        await self.db.replace_teams(group_id, result.teams)

        (team_a, players_a), (team_b, players_b) = result.teams[0], result.teams[1]
        today = datetime.now().strftime("%Y-%m-%d")
        gen = lineup.generate_template(
            team_a, players_a, team_b, players_b,
            today, result.rule, group_id,
            seed=(self.config.get("pairing_seed") or None),
        )

        yield event.plain_result(gen.template)
        for w in gen.warnings:
            yield event.plain_result(w)

    # ---------- 群战队绑定 ----------

    @filter.command("绑定战队", alias={"/绑定战队"})
    async def bind_home(self, event: AstrMessageEvent, team: str = ""):
        """绑定本群战队（管理/群主）"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 绑定战队只有群管理/群主能做喵。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        team = team.strip().upper()
        if not team:
            yield event.plain_result("用法喵：绑定战队 <战队名>")
            return
        await self.db.set_group_home(group_id, team)
        await self.db.backfill_group_home(group_id, team)
        yield event.plain_result(
            f"✅ 本群战队已经绑定成 {team} 啦喵～ (=^･ω･^=)（已有战报已回填）"
        )

    @filter.command("查看战队", alias={"/查看战队"})
    async def view_home(self, event: AstrMessageEvent):
        """查看本群战队"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        home = await self.db.get_group_home(group_id)
        if home:
            yield event.plain_result(f"🏠 本群战队：{home} 喵～")
        else:
            yield event.plain_result(
                "喵…本群还没绑定战队，请管理/群主用 /绑定战队 <战队> 绑一下。"
            )

    # ---------- 群禁用管理（超级管理员） ----------

    async def _admin_check(self, event) -> str | None:
        """组合检查：数据库就绪 + 超级管理员。"""
        err = self._check_db()
        if err:
            return err
        if not self._is_super_admin(event):
            return _ADMIN_ONLY
        return None

    @filter.command("禁群", alias={"/禁群"})
    async def ban_group(self, event: AstrMessageEvent, group_id: str = ""):
        """禁用某个群的全部插件功能（仅超级管理员）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        gid = group_id.strip()
        if not gid:
            yield event.plain_result("用法喵：禁群 <群号>")
            return
        await self.db.set_group_ban(gid, True)
        yield event.plain_result(_GROUP_DISABLED.format(gid))

    @filter.command("启群", alias={"/启群"})
    async def enable_group(self, event: AstrMessageEvent, group_id: str = ""):
        """开启某个群的全部插件功能（仅超级管理员）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        gid = group_id.strip()
        if not gid:
            yield event.plain_result("用法喵：启群 <群号>")
            return
        await self.db.set_group_ban(gid, False)
        yield event.plain_result(f"✅ 群 {gid} 的插件功能开回来啦喵～ (=^･ω･^=)")

    @filter.command("查群", alias={"/查群"})
    async def check_group(self, event: AstrMessageEvent, group_id: str = ""):
        """查询群的禁用状态（仅超级管理员）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        gid = group_id.strip()
        if not gid:
            yield event.plain_result("用法喵：查群 <群号>")
            return
        banned = await self.db.get_group_ban(gid)
        yield event.plain_result(
            _GROUP_DISABLED.format(gid)
            if banned
            else f"✅ 群 {gid} 的插件功能好好的喵～"
        )

    @filter.command("战队列表", alias={"/战队列表"})
    async def list_teams(self, event: AstrMessageEvent):
        """查看全部战队"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        teams = await self.db.get_all_teams()
        if not teams:
            yield event.plain_result("喵…一个战队都还没有呢。")
            return
        yield event.plain_result("🏆 全部战队喵～\n" + "、".join(teams))

    @filter.command("群列表", alias={"/群列表"})
    async def list_groups(self, event: AstrMessageEvent, team: str = ""):
        """查看全部群及其绑定战队（可按战队过滤，仅超管）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        filter_team = team.strip().upper() or None
        groups = await self.db.get_all_groups(filter_team)
        if not groups:
            yield event.plain_result(
                "喵…一个群都还没有呢。"
                if not filter_team
                else f"喵…还没有绑定 {filter_team} 的群呢。"
            )
            return
        title = (
            "📋 群列表喵～"
            if not filter_team
            else f"📋 绑定 {filter_team} 的群喵～"
        )
        lines = [title]
        for g in groups:
            mark = "🚫" if g["banned"] else "✅"
            home = g["home_team"] or "未绑定"
            lines.append(f"{mark} {g['group_id']} → {home}")
        yield event.plain_result("\n".join(lines))

    @filter.command("群聊属性", alias={"/群聊属性"})
    async def set_chat_type(self, event: AstrMessageEvent, chat_type: str = ""):
        """设置当前群属性：友谊群/战报群/主群（管理/群主）"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 群属性只有群管理/群主能改喵。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        chat_type = chat_type.strip()
        if chat_type not in ("友谊群", "战报群", "主群"):
            yield event.plain_result("用法喵：群聊属性 <友谊群|战报群|主群>")
            return
        await self.db.set_group_chat_type(group_id, chat_type)
        yield event.plain_result(f"✅ 本群属性设成「{chat_type}」啦喵～ (=^･ω･^=)")

    @filter.command("通告", alias={"/通告"})
    async def broadcast(self, event: AstrMessageEvent):
        """发布通告（仅超管）：`通告 <内容>` 发到所有群；`通告 <群号/QQ号>\n<内容>` 仅发指定目标。"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        raw = event.get_message_str()
        rest = _strip_command(raw, ("通告", "/通告"))
        if not rest:
            yield event.plain_result(_USAGE_BROADCAST)
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result("❌ 连不上平台喵… (；・∀・)")
            return

        # 首行是纯数字 → 视为指定目标（群/人），其余为内容
        lines = rest.split("\n")
        target = lines[0].strip() if lines and lines[0].strip().isdigit() else ""
        content = "\n".join(lines[1:]).strip() if target else rest
        if not content:
            yield event.plain_result(_USAGE_BROADCAST)
            return

        beijing = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        content = f"{content}\n{beijing}"

        # 指定目标：是群则发群消息，否则按私聊发给该用户
        if target:
            groups: list = []
            try:
                groups = await bot.call_action("get_group_list")
            except Exception as e:
                logger.warning(f"获取群列表失败: {e}")
            if not isinstance(groups, list):
                groups = []
            if any(str(g.get("group_id") or g.get("group")) == target for g in groups):
                try:
                    await bot.call_action("send_group_msg", group_id=int(target), message=content)
                except Exception as e:
                    logger.warning(f"通告发送失败 {target}: {e}")
                    yield event.plain_result(f"❌ 发到群 {target} 失败了喵：{e}")
                    return
                yield event.plain_result(f"📢 通告已经发到群 {target} 啦喵～ 🐾")
            else:
                try:
                    await bot.call_action("send_private_msg", user_id=int(target), message=content)
                except Exception as e:
                    logger.warning(f"通告发送失败 {target}: {e}")
                    yield event.plain_result(f"❌ 发给 {target} 失败了喵：{e}")
                    return
                yield event.plain_result(f"📢 通告已经发给 {target} 啦喵～ 🐾")
            return

        # 发送到所有群
        try:
            groups = await bot.call_action("get_group_list")
        except Exception as e:
            logger.warning(f"获取群列表失败: {e}")
            yield event.plain_result("❌ 拿不到群列表喵… (；・∀・)")
            return
        if not isinstance(groups, list):
            groups = []
        if not groups:
            yield event.plain_result("喵…机器人现在一个群都没在。")
            return

        ok = 0
        failed: list[str] = []
        for g in groups:
            gid = g.get("group_id") or g.get("group")
            if not gid:
                continue
            try:
                await bot.call_action("send_group_msg", group_id=gid, message=content)
                ok += 1
                await asyncio.sleep(0.2)  # 限速，避免被风控
            except Exception as e:
                failed.append(str(gid))
                logger.warning(f"通告发送失败 {gid}: {e}")

        msg = f"📢 通告已经发到 {ok} 个群啦喵～ 🐾"
        if failed:
            msg += f"\n⚠️ 有 {len(failed)} 个群没发出去喵：{'、'.join(failed[:10])}"
        yield event.plain_result(msg)

    # ---------- 用户与参赛ID ----------

    def _is_super_admin(self, event) -> bool:
        """是否超级管理员。"""
        return str(event.get_sender_id()) == str(
            self.config.get("super_admin", "1443290403") or ""
        )

    async def _check_enabled(self, event) -> str | None:
        """群被禁用时返回提示文案。"""
        group_id = event.get_group_id()
        if group_id and self.db_ready and self.db:
            if await self.db.get_group_ban(group_id):
                return "🚫 本群已经被管理员禁用插件功能了喵。"
        return None

    async def _group_check(self, event) -> str | None:
        """组合检查：数据库就绪 + 群未被禁用。"""
        err = self._check_db()
        if err:
            return err
        return await self._check_enabled(event)

    async def _require_home(self, event) -> tuple[str | None, str | None]:
        """获取群号与绑定战队；失败时返回 (错误文案, None)。

        战队解析只有这一条路径：**必须本群已绑定**，没有配置兜底（v1.13.0 起
        原先"未绑定就吃配置 home_team"的宽松路径已删除，以免未绑定的群把战报
        统计挂到别的战队上）。
        """
        err = await self._group_check(event)
        if err:
            return err, None
        group_id = event.get_group_id()
        if not group_id:
            return _NEED_GROUP, None
        home = await self.db.get_group_home(group_id)
        if not home:
            return _NEED_HOME, None
        return None, home

    @filter.command("我的ID", alias={"/我的ID"})
    async def auth(self, event: AstrMessageEvent):
        """查看/确认自己的用户身份"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        qq = event.get_sender_id()
        user = await self.db.get_user_by_qq(home, qq)
        if not user:
            yield event.plain_result(_NEED_BIND)
            return
        players = await self.db.get_user_players(home, user["id"])
        yield event.plain_result(
            f"👤 你的身份喵：{user['name']}（{home}）\n"
            f"📌 已绑参赛ID：{'、'.join(players) if players else '（无）'}"
        )

    @filter.command("查ID", alias={"/查ID"})
    async def search_id(self, event: AstrMessageEvent, keyword: str = ""):
        """模糊查询本战队参赛ID"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        status = await self.db.get_pool_status(home, keyword.strip() or None, 20)
        if not status:
            yield event.plain_result(f"喵…战队 {home} 里没有匹配的参赛ID呢。")
            return
        lines = [f"🔍 战队 {home} 参赛ID喵（/绑定ID <参赛ID> 绑定）："]
        for s in status:
            if s["user_name"] and not s["qq_id"]:
                # 已绑定角色但角色尚未被任何人认领
                mark = f"（已绑 {s['user_name']} 绑定此ID将同时绑定角色）"
            elif s["user_name"]:
                mark = f"（已绑 {s['user_name']}）"
            else:
                mark = "（未绑定）"
            lines.append(f"{s['player']} {mark}")
        yield event.plain_result("\n".join(lines))

    @filter.command("绑定ID", alias={"/绑定ID"})
    async def bind_id(self, event: AstrMessageEvent):
        """批量将参赛ID绑定到自己的用户（或认领已有用户）"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        raw = event.get_message_str()
        payload = _strip_command(raw, ("绑定ID", "/绑定ID"))
        names = [_strip_ruled(_strip_sub(p)[0])[0] for p in payload.split() if p.strip()]
        if not names:
            yield event.plain_result("用法喵：绑定ID <参赛ID> [参赛ID ...]")
            return

        qq = event.get_sender_id()
        my_user = await self.db.get_user_by_qq(home, qq)
        lines: list[str] = []
        for p in names:
            msg, my_user = await self._bind_one_id(home, p, qq, my_user)
            lines.append(msg)
        yield event.plain_result("\n".join(lines))

    @filter.command("解绑ID", alias={"/解绑ID"})
    async def unbind_id(self, event: AstrMessageEvent):
        """批量解除参赛ID绑定：自己的或管理/群主操作"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        raw = event.get_message_str()
        payload = _strip_command(raw, ("解绑ID", "/解绑ID"))
        names = [_strip_ruled(_strip_sub(p)[0])[0] for p in payload.split() if p.strip()]
        if not names:
            yield event.plain_result("用法喵：解绑ID <参赛ID> [参赛ID ...]")
            return

        qq = event.get_sender_id()
        my_user = await self.db.get_user_by_qq(home, qq)
        is_manager = await self._is_manager(event)
        lines: list[str] = []
        for p in names:
            binding = await self.db.get_player_binding(home, p)
            if not binding or not binding.get("user_id"):
                lines.append(f"⚠️ 参赛ID「{p}」本来就没绑角色喵，不用解。")
                continue
            if (my_user and binding["user_id"] == my_user["id"]) or is_manager:
                await self.db.unbind_player(home, p)
                tag = "（管理操作）" if is_manager and not (my_user and binding["user_id"] == my_user["id"]) else ""
                lines.append(f"✅ 参赛ID「{p}」解绑好啦喵～{tag}")
            else:
                lines.append(
                    f"❌ 参赛ID「{p}」绑在别的角色（{binding['user_name']}）身上喵，解不了。"
                )
        yield event.plain_result("\n".join(lines))

    async def _bind_one_id(self, home, player, qq, my_user):
        """绑定单个参赛ID到用户。返回 (提示文案, 生效的 my_user)。"""
        pool = await self.db.get_player_pool(home, player, 50)
        if player not in pool:
            return f"❌ 战队 {home} 的参赛记录里没有「{player}」这个参赛ID喵。", my_user

        binding = await self.db.get_player_binding(home, player)
        if binding and binding.get("user_id"):
            # 该参赛ID已绑定某角色
            if my_user and my_user["id"] == binding["user_id"]:
                return f"✅ 参赛ID「{player}」早就在你自己的角色下啦喵～", my_user
            if my_user:
                # 已绑定其他角色：无论该角色是否被认领，都不得改绑到自己的角色
                return (
                    f"❌ 参赛ID「{player}」已经绑在角色「{binding['user_name']}」身上了喵，"
                    f"不能再改绑到你的角色「{my_user['name']}」哦。"
                ), my_user
            # 我无角色 → 认领该ID所在的角色
            status, uid = await self.db.claim_user_by_name(home, binding["user_name"], qq)
            if status == "claimed_else":
                return f"❌ 参赛ID「{player}」已经被别的角色（{binding['user_name']}）认领走了喵。", my_user
            if status == "not_found":
                uid = await self.db.find_or_create_user(home, binding["user_name"], qq)
                await self.db.bind_player_to_user(home, player, uid)
            user = await self.db.get_user_by_id(uid)
            players = await self.db.get_user_players(home, uid)
            return (
                f"✅ 角色「{user['name']}」（{home}）认领好啦喵～ (=^･ω･^=)\n"
                f"参赛ID也一起绑上了喵：\n"
                f"📌 该角色参赛ID：{'、'.join(players)}",
                user,
            )

        # 未绑定 → 挂到我的角色（无角色则创建，以参赛ID为初始角色名）
        uid = await self.db.find_or_create_user(home, player, qq)
        await self.db.bind_player_to_user(home, player, uid)
        user = await self.db.get_user_by_id(uid)
        return f"✅ 参赛ID「{player}」绑到你的角色「{user['name']}」上啦喵～ (=^･ω･^=)", user

    @filter.command("管理ID", alias={"/管理ID"})
    async def admin_id(self, event: AstrMessageEvent):
        """管理/群主：查看/批量绑定本战队参赛ID（逗号分隔）"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 管理参赛ID只有群管理/群主能做喵。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, ("管理ID", "/管理ID")).strip()
        if not payload:
            status = await self.db.get_pool_status(home, None, 50)
            if not status:
                yield event.plain_result(f"喵…战队 {home} 还没有参赛ID呢（还没有战报记录）。")
                return
            lines = [f"📋 战队 {home} 参赛ID列表喵～"]
            for s in status:
                mark = f"→ {s['user_name']}" if s["user_name"] else "（未绑定）"
                lines.append(f"{s['player']} {mark}")
            lines.append("绑定：/管理ID <参赛ID[,参赛ID...]> <用户名>")
            yield event.plain_result("\n".join(lines))
            return

        # 最后一个空白分隔符为用户名，其余为逗号分隔的参赛ID集合
        parts = payload.rsplit(None, 1)
        if len(parts) < 2:
            yield event.plain_result(_USAGE_MANAGE_ID)
            return
        id_part, username = parts[0], parts[1].strip()
        players = [_strip_ruled(_strip_sub(p)[0])[0] for p in id_part.split(",") if p.strip()]
        if not players or not username:
            yield event.plain_result(_USAGE_MANAGE_ID)
            return

        pool = await self.db.get_player_pool(home, None, 500)
        uid = await self.db.find_or_create_user(home, username)
        lines = []
        for p in players:
            if p not in pool:
                lines.append(f"❌ 战队 {home} 的参赛记录里没有「{p}」这个参赛ID喵。")
                continue
            await self.db.bind_player_to_user(home, p, uid)
            lines.append(f"✅ 参赛ID「{p}」绑到用户「{username}」上啦喵～")
        yield event.plain_result("\n".join(lines))

    @filter.command("我的战绩", alias={"/我的战绩"})
    async def my_record(self, event: AstrMessageEvent):
        """按自己绑定的参赛ID查询战绩"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        qq = event.get_sender_id()
        user = await self.db.get_user_by_qq(home, qq)
        if not user:
            yield event.plain_result(_NEED_BIND)
            return
        players = await self.db.get_user_players(home, user["id"])
        if not players:
            yield event.plain_result(f"喵…用户「{user['name']}」还没绑任何参赛ID呢。")
            return
        _, month = _parse_month_filter(
            _strip_command(event.get_message_str(), ("我的战绩", "/我的战绩"))
        )
        date_from, date_to = month_range(month)
        agg = await self.db.get_players_aggregate(
            home, players, date_from, date_to
        )
        wins = int(agg["wins"])
        losses = int(agg["losses"])
        total = int(agg["total"])
        wr = round(wins * 100.0 / (wins + losses), 1) if (wins + losses) else 0.0
        raid_pts = int(agg.get("raid_points", 0))

        lines = [
            f"👤 {user['name']}（{home}）喵～",
            f"📌 参赛ID：{'、'.join(players)}",
        ]
        if len(players) == 1:
            # 只有一个ID：汇总即该ID明细，无需再列
            lines.append(
                f"📊 战绩喵～ 胜{wins} 负{losses} 平{agg['draws']}  总{total}  "
                f"友谊{agg.get('friendship', 0)}  积分{stats._points(wins, losses)}  "
                f"踢馆积分{raid_pts}  总积分{stats.total_points(stats._points(wins, losses), raid_pts)}  "
                f"胜率{wr}%"
            )
        else:
            lines.append(
                f"📊 汇总战绩喵～ 胜{wins} 负{losses} 平{agg['draws']}  总{total}  "
                f"友谊{agg.get('friendship', 0)}  积分{stats._points(wins, losses)}  "
                f"踢馆积分{raid_pts}  总积分{stats.total_points(stats._points(wins, losses), raid_pts)}  "
                f"胜率{wr}%"
            )
            for p in players:
                rec = await self.db.get_player_record(home, p, date_from, date_to)
                w = int(rec.get("wins", 0))
                l = int(rec.get("losses", 0))
                d = int(rec.get("draws", 0))
                t = int(rec.get("total", 0))
                wr2 = round(w * 100.0 / (w + l), 1) if (w + l) else 0.0
                lines.append(
                    f"  · {p}：胜{w} 负{l} 平{d} 总{t} 友谊{rec.get('friendship', 0)} "
                    f"踢馆{rec.get('raid_points', 0)} 胜率{wr2}%"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("改名", alias={"/改名"})
    async def rename_me(self, event: AstrMessageEvent, name: str = ""):
        """修改自己的用户名称（角色名）"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        qq = event.get_sender_id()
        user = await self.db.get_user_by_qq(home, qq)
        if not user:
            yield event.plain_result(_NEED_BIND)
            return
        name = name.strip()
        if not name:
            yield event.plain_result("用法喵：改名 <新名字>")
            return
        if len(name) > 30:
            yield event.plain_result("喵…名字太长了啦（最多 30 字）。")
            return
        status = await self.db.rename_user(home, user["id"], name)
        if status == "conflict":
            yield event.plain_result(f"❌ 名字「{name}」被本战队别人先占啦喵。")
            return
        yield event.plain_result(f"✅ 改名成功喵～ 以后就叫「{name}」了（{home}）(=^･ω･^=)")

    # ---------- 追加轮次（/第N轮） ----------

    @filter.custom_filter(RoundCommandFilter)
    async def round_cmd(self, event: AstrMessageEvent):
        """追加轮次：/第N轮 [玩家A [比分] 玩家B]；无追加时随机匹配上一轮胜者"""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        disabled = await self._check_enabled(event)
        if disabled:
            yield event.plain_result(disabled)
            return

        text = event.get_message_str().strip()
        m = _ROUND_CMD_RE.match(text)
        if not m:
            return
        round_no = lineup.parse_round_no(m.group(1))
        if round_no is None:
            yield event.plain_result("❌ 轮次要写 2 以上的数字喵（比如 /第二轮、/第三轮）。")
            return

        info_lines = [ln.strip() for ln in text[m.end():].splitlines() if ln.strip()]

        draft = await self._read_latest_report(event)
        if not draft:
            yield event.plain_result(_NO_REPORT)
            return

        result = lineup.build_next_round(
            draft, round_no, info_lines,
            seed=(self.config.get("pairing_seed") or None),
        )
        if not result.ok:
            yield event.plain_result("❌ " + "\n".join(result.errors))
            return
        yield event.plain_result(result.new_text)
        if result.errors:
            yield event.plain_result("⚠️ 有几行对局没看懂喵：\n" + "\n".join(result.errors))

    # ---------- 记录比分（/记录） ----------

    @filter.command("记录", alias={"/记录"})
    async def record_cmd(self, event: AstrMessageEvent):
        """记录比分：/记录 玩家名 比分 [对手]；无参数时提示格式"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        disabled = await self._check_enabled(event)
        if disabled:
            yield event.plain_result(disabled)
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, ("记录", "/记录"))
        if not payload.strip():
            yield event.plain_result(
                "告诉我要记录什么喵～ 格式是这样：\n"
                "记录 玩家名 比分\n"
                "记录 玩家名 比分 对手\n"
                "比分写 2:0 或者紧凑的 20 都行喵。"
            )
            return

        info_lines = [ln.strip() for ln in payload.splitlines() if ln.strip()]
        draft = await self._read_latest_report(event)
        if not draft:
            yield event.plain_result(_NO_REPORT)
            return

        # 逐行处理记录信息（一行失败不影响其余行）
        all_added: list[str] = []
        errors: list[str] = []
        current = draft
        for info in info_lines:
            result = lineup.record_from_info(current, info)
            if result.ok:
                all_added.extend(result.added_lines)
                current = result.new_text
            else:
                errors.extend(result.errors)

        if not all_added:
            yield event.plain_result("❌ 一局都没记上喵… (；・∀・)\n" + "\n".join(errors))
            return
        yield event.plain_result(current)
        if errors:
            yield event.plain_result("⚠️ 有几条没记上喵：\n" + "\n".join(errors))

    # ---------- 提交战报 ----------

    def _send_responses(self, event, responses: list[str]):
        """发送一组回复：≥2 条用合并转发封装，1 条逐条发送。空列表不发送。"""
        if not responses:
            return
        if len(responses) < 2:
            for r in responses:
                yield event.plain_result(r)
            return
        nodes = [
            Node(
                name=event.get_sender_name(),
                uin=event.get_sender_id(),
                content=[Plain(r)],
            )
            for r in responses
        ]
        yield event.chain_result([Nodes(nodes)])

    @filter.command("发送", alias={"/发送", "/战报"})
    async def submit_report(self, event: AstrMessageEvent):
        """提交战报（可一次粘贴多份，按『战队:』行拆分）"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        if self.config.get("submit_requires_admin", False) and not event.is_admin():
            yield event.plain_result("❌ 当前配置下只有管理员能提交战报喵。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _SUBMIT_CMDS)

        # 无参数且无引用时直接提示
        if not payload.strip() and not await self._has_reply(event):
            yield event.plain_result(
                "把战报给我喵～\n"
                "· /发送 + 直接粘贴战报文本\n"
                "· 或引用合并转发的那条战报消息，再发 /发送"
            )
            return

        # 优先从回复引用（含合并转发）提取战报
        reply_reports = await self._extract_reply_reports(event)
        from_reply = bool(reply_reports)
        if reply_reports:
            report_chunks = reply_reports
        else:
            report_chunks = split_reports(payload)
        if not report_chunks:
            yield event.plain_result("❌ 战报是空的喵…")
            return

        # 逐个解析：失败不阻断后续战报，记录报错（含行号）后跳过该份
        reply_hint = (
            "\n\n（提示喵：引用的内容可能不全，可以重发那份战报，或者直接 /发送 粘贴全文）"
            if from_reply else ""
        )
        parsed: list = []
        fail_msgs: list[str] = []
        for i, chunk in enumerate(report_chunks, 1):
            result = _parse_chunk(chunk)
            if result.errors:
                fail_msgs.append(
                    f"❌ 第 {i} 份战报看不懂喵：\n"
                    + "\n".join(result.errors)
                    + "\n\n📄 我收到的战报原文：\n" + chunk.strip()
                    + reply_hint
                    + "\n\n" + _FORMAT_EXAMPLE
                )
                continue
            parsed.append((i, result.report, result.warnings, chunk))

        if not parsed:
            for x in self._send_responses(event, fail_msgs):
                yield x
            return

        # 确定群号
        group_id = event.get_group_id()
        if not group_id:
            _no_group = "⚠️ 认不出这是哪个群喵：请在群里提交，或者把战报『地点:』那行填成群号。"
            if not self.config.get("allow_private_chat", True):
                yield event.plain_result(
                    "⚠️ 私聊提交关掉了喵，请到群里提交战报。"
                )
                return
            # 私聊没有群上下文，只能从战报的『地点:』取群号。该字段是自由
            # 文本，必须确认是纯数字才当群号用 —— 否则会拿「上海」「XX网吧」
            # 之类的文本去查战队，既必然查不到、又白白拖慢每条消息。
            loc = (parsed[0][1].location or "").strip()
            if not loc:
                yield event.plain_result(_no_group)
                return
            if not loc.isdigit():
                yield event.plain_result(
                    f"⚠️ 认不出这是哪个群喵：『地点:』写的是「{loc}」，不是群号。\n"
                    "请在群里提交，或者把『地点:』改成群号（纯数字）。"
                )
                return
            group_id = loc

        # 上传需群已绑定战队
        bound_home = await self.db.get_group_home(group_id)
        if not bound_home:
            yield event.plain_result(
                "❌ 本群还没绑定战队喵，战报传不上去。\n请管理/群主用 /绑定战队 <战队> 绑定一下喵～"
            )
            return
        home_team = bound_home

        # 单份战报的最少对局数（默认 3）。只作提交前置条件，与排名的
        # min_games 门槛无关；2 人赛制的战队可通过配置放行。
        min_duels = max(1, int(self.config.get("min_duels", 3) or 3))

        # 逐个提交：成功与失败分两个回复集合
        success_responses: list[str] = []
        failure_responses: list[str] = list(fail_msgs)
        for i, report, warnings, chunk in parsed:
            report.group_id = group_id
            report.submitted_by = event.get_sender_id()
            report.submitted_name = event.get_sender_name()
            report.created_at = int(time.time())

            # 战报对阵必须包含本群绑定的战队，否则阻止发送（防止误归队/脏队标入库）
            if home_team not in (report.team_a, report.team_b):
                failure_responses.append(
                    f"❌ 第 {i} 份战报里的队伍（{report.team_a}、{report.team_b}）"
                    f"没有本群绑定的战队（{home_team}）喵，所以拦下来没发：\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time}\n"
                    f"📄 我收到的战报原文：\n{chunk.strip()}"
                )
                continue

            # 未完成对局：对阵数低于 min_duels（默认 3，2 人赛制的战队可调小）。
            # **踢馆报不设这个门槛**：首轮就被终结、无人守馆都是合法且完整的
            # 踢馆记录（对阵数 1 或 0），卡门槛会把它们全挡掉。
            if report.kind != KIND_RAID and len(report.duels) < min_duels:
                failure_responses.append(
                    f"❌ 第 {i} 份战报还没打完喵"
                    f"（只有 {len(report.duels)} 场对阵，至少要 {min_duels} 场）：\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time}\n"
                    f"📄 我收到的战报原文：\n{chunk.strip()}"
                )
                continue

            # 判定胜者：胜负未定则不记录
            winner = determine_match_winner(report)
            if winner is None:
                if report.kind == KIND_RAID:
                    # 踢馆没有轮次，也没拿 0:0 当「未完成」的口径（0:0 就是空位）
                    unfinished = [
                        f"{d.player_a} {d.score_a}:{d.score_b} {d.player_b}"
                        for d in report.duels
                        if d.score_a == d.score_b and d.score_a > 0
                    ]
                else:
                    unfinished = [
                        f"第{_int_to_cn(d.round_no)}轮 {d.player_a} 0:0 {d.player_b}"
                        for d in report.duels if d.score_a == 0 and d.score_b == 0
                    ]
                msg = (
                    f"❌ 第 {i} 份比赛还没分出胜负喵，先不记了：\n"
                    + (
                        f"{report.team_a} 踢馆 {report.team_b} | {report.match_time}"
                        if report.kind == KIND_RAID
                        else f"{report.team_a} VS {report.team_b} | {report.match_time}"
                    )
                )
                if unfinished:
                    msg += "\n这些对局还空着比分喵：\n" + "\n".join(unfinished)
                msg += (
                    "\n📄 我收到的战报原文：\n" + chunk.strip()
                    + "\n（把比分填上再发一次喵）"
                )
                failure_responses.append(msg)
                continue

            if report.kind == KIND_RAID:
                # 踢馆的胜负说法与友谊赛不同：踢馆方赢=踢破，守馆方赢=守馆成功
                home_win = home_team == winner
                if home_team == report.team_b:
                    home_result = (
                        f"🛡️ {home_team} 守馆成功喵！(๑•̀ㅂ•́)و"
                        if home_win
                        else f"💀 {home_team} 被踢破了喵…"
                    )
                elif home_team == report.team_a:
                    home_result = (
                        f"⚔️ {home_team} 踢馆成功喵！(๑•̀ㅂ•́)و"
                        if home_win
                        else f"💀 {home_team} 踢馆失败了喵…"
                    )
                else:
                    home_result = f"本场胜者是 {winner} 喵"
            elif home_team == winner:
                home_result = f"🏆 {home_team} 赢啦喵！(=^･ω･^=)"
            elif home_team in (report.team_a, report.team_b):
                home_result = f"💀 {home_team} 输掉了喵…"
            else:
                home_result = f"本场胜者是 {winner} 喵"

            try:
                match_id = await self.db.insert_report(report, winner, home_team, chunk)
            except DuplicateReportError as e:
                # match_id 为 None：并发兜底下没回查到对方（刚被删）。降级提示。
                if e.match_id:
                    hint = (
                        f"要是原来那条记错了，可以用 /战报删除 {e.match_id} "
                        f"删掉再重新发喵。"
                    )
                else:
                    hint = "要是原来那条记错了，先把旧记录删掉再重发喵。"
                failure_responses.append(
                    f"⚠️ 第 {i} 份战报已经有了喵"
                    f"（ID {e.match_id or '未知'}），跳过：\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time}\n"
                    f"{hint}"
                )
                continue
            except Exception as e:
                logger.exception("战报入库失败")
                failure_responses.append(
                    f"❌ 写不进去喵：{report.team_a} VS {report.team_b} | {report.match_time}\n{e}"
                )
                continue

            # 汇总 + 每场对阵结果（供核对）
            if report.kind == KIND_RAID:
                summary = (
                    f"✅ 踢馆战报记好啦喵～（ID {match_id}）\n"
                    f"{report.team_a} 踢馆 {report.team_b} | {report.match_time} | "
                    f"共 {len(report.duels)} 局\n"
                    f"{home_result}\n"
                    f"{format_raid_results(report, home_team)}"
                )
            else:
                summary = (
                    f"✅ 战报记好啦喵～（ID {match_id}）\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time} | "
                    f"共 {len(report.duels)} 局\n"
                    f"{home_result}\n"
                    f"{format_duel_results(report, home_team)}"
                )
            success_responses.append(summary)
            if warnings:
                success_responses.append("⚠️ 解析时有点小警告喵：\n" + "\n".join(warnings))

        # 成功集与失败集分别发送（各自 ≤3 逐条，否则合并转发）
        for x in self._send_responses(event, success_responses):
            yield x
        for x in self._send_responses(event, failure_responses):
            yield x

    # ---------- 查询 ----------

    @filter.command("排行", alias={"/排行", "/战报排行"})
    async def ranking(self, event: AstrMessageEvent):
        """排行榜（个人/队伍），默认本月，末尾可加 X月"""
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return

        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _RANK_CMDS)
        )
        tokens = payload.split()
        scope = tokens[0] if tokens else "个人"
        date_from, date_to = month_range(month)
        limit = int(self.config.get("ranking_limit", 10) or 10)

        try:
            if scope in ("队伍", "战队", "队"):
                rows = await self.db.get_home_team_vs_opponents(
                    home_team, date_from, date_to
                )
                yield event.plain_result(stats.format_home_team_vs(home_team, rows))
            else:
                min_games = int(self.config.get("min_games", 1) or 1)
                month_label = f"{month}月" if month else "本月"
                # 图片左侧的「战队战绩」面板（v1.15.0）。默认 None ——
                # `/排行 全部` 是跨队榜，面板属于单个战队，不给它加。
                # 必须在分支**之前**初始化，否则那条分支会 UnboundLocalError，
                # 被下面那个宽 except 吞成「❌ 查询出错」，很难查。
                panel = None
                if scope in ("全部", "所有"):
                    rows = await self.db.get_player_ranking(
                        home_team, date_from, date_to, min_games, limit, team=None
                    )
                    fallback = stats.format_player_ranking(rows, limit)
                    title = f"个人积分榜（全战队 · 前 {limit}）"
                    caption = f"🏆 全战队个人榜喵～（前 {limit} · {month_label}）"
                    # 全战队 top-N 按配置截断（默认 30 行）
                    max_rows = int(self.config.get("ranking_image_max_rows", 30) or 30)
                else:
                    # 默认只统计战队选手，显示全部队员（不设上限，图片不截断）
                    rows = await self.db.get_player_ranking(
                        home_team, date_from, date_to, min_games, None, team=home_team
                    )
                    # 该月已结算过就带上「奖金」列。月份键直接取查出来的区间
                    # （'2026-07-01'[:7] = '2026-07'），保证与上面查的是同一个月。
                    # 「未结算就不加键」这层判断在 attach_bonus 里面。
                    # `/排行 全部` 是跨队榜，结算记录属于单个战队，故不给它加。
                    stats.attach_bonus(
                        rows, await self.db.get_settlement(home_team, date_from[:7])
                    )
                    note = f"\n（战队 {home_team}，一共 {len(rows)} 个人喵）" if home_team else ""
                    fallback = stats.format_player_ranking(rows, None) + note
                    title = f"个人积分榜（{home_team} · 共 {len(rows)} 人 · {month_label}）"
                    caption = f"🏆 {home_team} 个人榜喵～（{month_label}）"
                    max_rows = None  # 全部队员，不截断
                    # 战队面板与表格用同一对 date_from/date_to，必然是同一个月份窗口
                    rec = await self.db.get_home_team_record(home_team, date_from, date_to)
                    panel = stats.build_team_panel(rec)
                if rows and self.config.get("ranking_image", True):
                    # 图片表格展示，生成失败回退文字表格
                    try:
                        cells = stats.build_ranking_cells(rows)
                        aligns = stats.rank_aligns(len(cells[0]))
                        out = self.data_dir / "rankings" / f"rank_{int(time.time())}.png"
                        path = chart.make_ranking_image(
                            cells, aligns, title, out, max_rows, panel
                        )
                        yield event.chain_result([
                            Plain(f"{caption}："),
                            Image.fromFileSystem(str(path)),
                        ])
                        return
                    except Exception:
                        logger.exception("排行图片生成失败，回退文字表格")
                yield event.plain_result(fallback)
        except Exception:
            logger.exception("排行查询失败")
            yield event.plain_result(_ERR_QUERY)

    @filter.command("结算", alias={"/结算"})
    async def settle(self, event: AstrMessageEvent):
        """月度结算：给该月积分榜前 N 名发奖金。

        管理/群主 = **结算并写库**（该月第一次结算 / 获奖名次变了时，顺带 @ 获奖
        成员发公告）；其他人 = **只读查询**已结算的结果（见下）。
        """
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        # 权限在这里取一次，但**不再直接拦掉非管理员** —— 它现在只决定走
        # 「写」还是「读」两条路（v1.15.0）。检查顺序仍照抄 /绑定战队：
        # 权限先于业务参数（参数错也要先告诉非管理员他没有写权限）。
        is_admin = await self._is_manager(event)

        month, reward_ranks = _parse_settle_args(
            _strip_command(event.get_message_str(), _SETTLE_CMDS)
        )
        if not 1 <= reward_ranks <= stats.MAX_REWARD_RANKS:
            yield event.plain_result(
                f"❌ 名次数要在 1~{stats.MAX_REWARD_RANKS} 之间喵。"
            )
            return

        # month 缺省（`/结算` 不带月份）= **上个月**，见 settle_month_range。
        # 年月一律从 key 反解出来用，这样回执上写的年月与真正查库/写库的那个月
        # **必然是同一个**（跨年回退也在 key 里定死了），不会出现「说 12 月、
        # 其实查的是去年 12 月」这种对不上的文案。
        date_from, date_to, key = settle_month_range(month)
        year, mon = int(key[:4]), int(key[5:7])

        if not is_admin:
            # 只读查询：**只查 settlements 表**，不碰任何排行榜数字、不写库。
            # 没结算过就直说，不展示「如果现在结算会发多少」——那会让人误以为
            # 钱已经发出去了。
            try:
                entries = await self.db.get_settlement_entries(home_team, key)
                if not entries:
                    yield event.plain_result(
                        f"📋 {home_team} {year}年{mon}月：这个月还没结算呢喵。\n"
                        f"（结算是群管理/群主来做的：/结算 {mon}月）"
                    )
                    return
                yield event.plain_result(
                    stats.format_settlement_view(home_team, year, mon, entries)
                )
            except Exception:
                logger.exception("结算查询失败")
                yield event.plain_result(_ERR_QUERY)
            return

        # 「指定月份必须已经结束」：settle_month_range 会把本年尚未到来的月份回退
        # 一年，所以这里只剩「当前月」这一种未结束的情况 —— 它必须挡掉，否则
        # 9 月跑 /结算 9月 会静默结掉一个还在进行中的月份。
        # 只挡写路径：查询当前月没有意义也没有危险，回「尚未结算」更贴切。
        if date_to >= datetime.now().date().isoformat():
            yield event.plain_result(f"❌ {mon} 月还没过完喵，结算不了。")
            return

        try:
            min_games = int(self.config.get("min_games", 1) or 1)
            # limit=None + team=home_team：结算属于**本战队**，不跨队发给别人；
            # 名次用 stats.ranks_for 取，与 /排行 表格里显示的名次同一份实现。
            rows = await self.db.get_player_ranking(
                home_team, date_from, date_to, min_games, None, team=home_team
            )
            if not rows:
                yield event.plain_result(f"❌ {mon} 月没有能结算的战绩喵。")
                return
            ranks = stats.ranks_for(rows)
            entries = [
                {
                    "rank_no": ranks[i],
                    "player": r["player"],
                    "total_points": r.get("total_points") or 0,
                    "bonus": stats.rank_bonus(ranks[i], r.get("total_points") or 0),
                }
                for i, r in enumerate(rows[:reward_ranks])
            ]
            # 覆盖写之前先读旧记录：**只有「该月第一次结算」或「获奖名次变了」
            # 才 @ 全群**（重跑但名单没变就只提示，不打扰人）。读必须放在写前面，
            # 写完再读拿到的是自己刚写进去的，永远比不出变化。
            old_entries = await self.db.get_settlement_entries(home_team, key)
            await self.db.set_settlement(home_team, key, entries)
            receipt = stats.format_settlement(
                home_team, year, mon, entries, reward_ranks
            )
            if not stats.settlement_awards_changed(old_entries, entries):
                yield event.plain_result(receipt + stats.format_settlement_repeat())
                return
            # @ 获奖成员。绑定了 QQ 的发真 At（QQ 上会弹提醒），没绑定的退化成
            # 字面「@名字」—— @ 不出来好过整条公告不发。
            names = [e["player"] for e in entries]
            qq_of = await self.db.get_qq_ids_by_names(home_team, names)
            chain: list = [Plain(receipt + "\n\n")]
            for kind, value in stats.settlement_announcement(names):
                if kind == "at":
                    qq = qq_of.get(value)
                    chain.append(At(qq=qq) if qq else Plain(f"@{value}"))
                else:
                    chain.append(Plain(value))
            yield event.chain_result(chain)
        except Exception:
            logger.exception("结算失败")
            yield event.plain_result("❌ 结算出错了喵…稍后再试一次嘛 (；・∀・)")

    @filter.command("重置结算", alias={"/重置结算"})
    async def reset_settle(self, event: AstrMessageEvent):
        """把指定月份恢复成**未结算**（删掉该月的结算记录）。管理/群主专用。

        `month` 缺省 = 上个月，与 `/结算` 一致。删完不补数据，于是 `/排行` 回到
        14 列、`/结算` 又能被当成「该月第一次结算」重新发公告。
        """
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 重置结算只有群管理/群主能做喵。")
            return

        # 复用 `/结算` 的参数解析（月份在中/在末尾都认）。名次数对重置没有意义，
        # 解析出来直接丢掉 —— `/重置结算 7月 12` 与 `/重置结算 7月` 等价。
        month, _ = _parse_settle_args(
            _strip_command(event.get_message_str(), _RESET_SETTLE_CMDS)
        )
        _, _, key = settle_month_range(month)
        year, mon = int(key[:4]), int(key[5:7])

        try:
            removed = await self.db.clear_settlement(home_team, key)
            if not removed:
                yield event.plain_result(
                    f"📋 {home_team} {year}年{mon}月：这个月本来就没结算过喵，不用重置。"
                )
                return
            yield event.plain_result(
                f"♻️ {home_team} {year}年{mon}月 的结算重置好啦喵～ "
                f"（删掉了 {removed} 条记录）\n"
                f"这个月又变回「未结算」了喵：/排行 {mon}月 不再显示「奖金」列，"
                f"重新 /结算 {mon}月 会当成第一次结算、重新 @ 获奖成员哦。"
            )
        except Exception:
            logger.exception("重置结算失败")
            yield event.plain_result("❌ 重置出错了喵…稍后再试一次嘛 (；・∀・)")

    @filter.command("战绩", alias={"/战绩", "/战报战绩"})
    async def record(self, event: AstrMessageEvent):
        """个人战绩，默认本月，末尾可加 X月"""
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _RECORD_CMDS)
        )
        name = payload.strip()
        date_from, date_to = month_range(month)
        suffix = f"（{month}月）" if month else "（本月）"
        try:
            if not name:
                # 本战队总体战绩（默认）
                record = await self.db.get_home_team_record(home_team, date_from, date_to)
                yield event.plain_result(stats.format_team_record(home_team, record, suffix))
                return
            role = await self.db.resolve_role(home_team, name)
            if role:
                # 绑定角色：聚合该角色全部参赛ID，显示角色名
                agg = await self.db.get_players_aggregate(
                    home_team, role["players"], date_from, date_to
                )
                yield event.plain_result(stats.format_player_record(role["user_name"], agg))
            else:
                agg = await self.db.get_player_record(home_team, name, date_from, date_to)
                yield event.plain_result(stats.format_player_record(name, agg))
        except Exception:
            logger.exception("战绩查询失败")
            yield event.plain_result(_ERR_QUERY)

    @filter.command("踢馆", alias={"/踢馆", "/战报踢馆"})
    async def raid(self, event: AstrMessageEvent):
        """踢馆成绩（无玩家名=本战队），默认本月，末尾可加 X月"""
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _RAID_CMDS)
        )
        name = payload.strip()
        date_from, date_to = month_range(month)
        suffix = f"{month}月" if month else "本月"
        try:
            if not name:
                agg = await self.db.get_raid_team_stats(home_team, date_from, date_to)
                yield event.plain_result(
                    stats.format_raid_record(f"{home_team}（{suffix}）", agg)
                )
                return
            # 绑定角色 → 合并其全部参赛ID按同一套规则重算；否则按单个参赛ID查
            role = await self.db.resolve_role(home_team, name)
            players = role["players"] if role else [name]
            agg = await self.db.get_raid_stats_for_players(
                home_team, players, date_from, date_to
            )
            who = role["user_name"] if role else name
            yield event.plain_result(stats.format_raid_record(f"{who}（{suffix}）", agg))
        except Exception:
            logger.exception("踢馆查询失败")
            yield event.plain_result(_ERR_QUERY)

    @filter.command("趋势", alias={"/趋势", "/战报趋势"})
    async def trend(self, event: AstrMessageEvent):
        """胜率走势图，默认本月，末尾可加 X月 或 [最近N天]"""
        err, home_team = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _TREND_CMDS)
        )
        tokens = payload.split()
        name = tokens[0] if tokens else ""
        days = tokens[1] if len(tokens) > 1 else ""
        if not name:
            # 未指定时默认展示战队
            name = home_team
        if not name:
            yield event.plain_result("用法喵：趋势 <玩家名或队伍名> [最近N天|X月]")
            return

        # 日期范围：月份 优先 → 数字天数 → 默认本月
        if month is not None:
            date_from, date_to = month_range(month)
            title_suffix = f"{month}月"
        elif days.isdigit():
            d = int(days)
            date_from, date_to = self._date_from(d), None
            title_suffix = f"最近 {d} 天"
        else:
            date_from, date_to = month_range(None)
            title_suffix = "本月"

        try:
            role = await self.db.resolve_role(home_team, name)
            if role:
                # 绑定角色：聚合该角色全部参赛ID走势，展示角色名
                display = role["user_name"]
                pts = await self.db.get_players_trend(
                    home_team, role["players"], date_from, date_to
                )
            else:
                display = name
                pts = await self.db.get_player_trend(home_team, name, date_from, date_to)
            if not pts:
                pts = await self.db.get_team_trend(home_team, name, date_from, date_to)
            if not pts:
                yield event.plain_result(f"喵…没找到「{name}」{title_suffix}的数据呢。")
                return
            points = stats.compute_cumulative(pts)
            out = self.data_dir / "trends" / f"trend_{int(time.time())}.png"
            path = chart.make_trend_chart(
                points,
                f"{display} 胜率走势（{title_suffix}）",
                out,
                int(self.config.get("trend_chart_width", 960) or 960),
                int(self.config.get("trend_chart_height", 480) or 480),
            )
            yield event.chain_result([
                Plain(f"📈 {display} 的胜率走势喵～"),
                Image.fromFileSystem(str(path)),
            ])
        except Exception:
            logger.exception("趋势图生成失败")
            yield event.plain_result("❌ 走势图生成失败了喵…稍后再试一次嘛 (；・∀・)")

    @filter.command("导出", alias={"/导出", "/战报导出"})
    async def export(self, event: AstrMessageEvent):
        """导出战报：可指定玩家/胜场负场/时间（X月|最近N天），合并转发或 csv/json 文件"""
        # 这里不用 _require_home：导出的文件名要用到群号，所以需自行保留 group_id
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        args = parse_export_payload(
            _strip_command(event.get_message_str(), _EXPORT_CMDS)
        )
        fmt = args["fmt"]
        outcome = args["outcome"]

        # 时间三态：月份 优先 → 最近N天 → 默认本月
        if args["month"]:
            date_from, date_to = month_range(args["month"])
            period_label = f"{args['month']}月"
        elif args["days"]:
            date_from, date_to = self._date_from(args["days"]), None
            period_label = f"最近 {args['days']} 天"
        else:
            date_from, date_to = month_range(None)
            period_label = "本月"

        home_team = await self.db.get_group_home(group_id)
        if not home_team:
            yield event.plain_result(_NEED_HOME)
            return

        # 指定玩家 → 参赛ID集合（绑定角色聚合/直接按参赛ID）
        players = None
        player_label = ""
        member_team = None  # 已绑定本战队成员时，限制其在本战队一侧出场（跨队同名排除）
        if args["player"]:
            role = await self.db.resolve_role(home_team, args["player"])
            if role:
                players = role["players"]
                player_label = role["user_name"]
                member_team = home_team
            else:
                players = [args["player"]]
                player_label = args["player"]
        filter_label = " · ".join(x for x in (outcome, player_label, period_label) if x)

        # ---------- 文件导出（csv/json） ----------
        if fmt:
            try:
                rows = await self.db.get_export_rows(home_team, date_from, date_to)
            except Exception:
                logger.exception("导出查询失败")
                yield event.plain_result(_ERR_EXPORT)
                return
            if players is not None or outcome != "全部":
                # 以比赛级聚合过滤出命中 match_id，再裁剪对局行
                try:
                    reports = await self.db.get_reports_for_export(home_team, date_from, date_to)
                except Exception:
                    logger.exception("导出查询失败")
                    yield event.plain_result(_ERR_EXPORT)
                    return
                reports = lineup.filter_report_outcome(reports, outcome, players, member_team)
                keep = {r["match_id"] for r in reports}
                rows = [r for r in rows if r["match_id"] in keep]
            if not rows:
                msg = (
                    f"喵…{player_label} 没有符合条件的战报呢。"
                    if player_label
                    else "喵…本战队还没有战报数据呢。"
                )
                yield event.plain_result(msg)
                return

            if fmt == "csv":
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    "战报ID", "群号", "战报类型", "队伍A", "队伍B", "日期", "规则", "地点",
                    "轮次", "玩家A", "比分A", "玩家B", "比分B", "胜者",
                    "玩家A替补", "玩家B替补", "防守方馆主",
                ])
                for r in rows:
                    writer.writerow([
                        r["match_id"], r["group_id"],
                        # 战报类型：踢馆报的 A 侧恒为踢馆方（见 REQUIREMENTS §5-M13）
                        "踢馆" if r.get("kind") == KIND_RAID else "友谊",
                        r["team_a"], r["team_b"], r["match_time"],
                        r["rule"], r["location"], r["round_no"], r["player_a"], r["score_a"],
                        r["player_b"], r["score_b"], r["result"],
                        "是" if r["a_sub"] else "", "是" if r["b_sub"] else "",
                        "是" if r.get("owner") else "",
                    ])
                content = buffer.getvalue()
                encoding = "utf-8-sig"
            else:
                content = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
                encoding = "utf-8"

            out = self.data_dir / "exports" / f"battle_report_{group_id}.{fmt}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding=encoding)
            yield event.chain_result([
                Plain("📦 战报数据喵～"),
                File(name=out.name, file=str(out)),
            ])
            return

        # ---------- 合并转发导出（全部/胜场/负场） ----------
        try:
            reports = await self.db.get_reports_for_export(home_team, date_from, date_to)
        except Exception:
            logger.exception("导出查询失败")
            yield event.plain_result(_ERR_EXPORT)
            return
        reports = lineup.filter_report_outcome(reports, outcome, players, member_team)
        if not reports:
            msg = (
                f"喵…{player_label} 没有符合条件的战报呢。"
                if player_label
                else "喵…没有符合这个条件的战报呢。"
            )
            yield event.plain_result(msg)
            return

        # 每份战报一个转发节点；头部逐字保留、对局段双空格重建
        nodes = [
            Node(
                name=r["submitted_name"] or "战队战报",
                uin=r["submitted_by"] or "10001",
                content=[Plain(lineup.report_to_text(r))],
            )
            for r in reports
        ]
        max_nodes = 100  # QQ 合并转发单条节点上限
        batch = (len(nodes) + max_nodes - 1) // max_nodes
        for i in range(0, len(nodes), max_nodes):
            yield event.chain_result([Nodes(nodes[i:i + max_nodes])])
        yield event.plain_result(
            f"📤 导出好啦喵～ 一共 {len(nodes)} 份战报（{filter_label}），"
            f"分 {batch} 条转发打包好了 🐾"
        )

    @filter.command("导出群成员", alias={"/导出群成员"})
    async def export_group_members(self, event: AstrMessageEvent, group_id: str = ""):
        """导出群成员列表到 CSV 文件（仅超级管理员）"""
        if not self._is_super_admin(event):
            yield event.plain_result(_ADMIN_ONLY)
            return

        gid = group_id.strip() or event.get_group_id()
        if not gid:
            yield event.plain_result("⚠️ 用法喵：/导出群成员 <群号>")
            return
        if not str(gid).isdigit():
            yield event.plain_result("❌ 群号得是数字喵。")
            return

        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result("❌ 拿不到 Bot 实例喵… (；・∀・)")
            return
        try:
            ret = await bot.call_action("get_group_member_list", group_id=int(gid))
        except Exception as e:
            logger.warning(f"获取群成员列表失败 {gid}: {e}")
            yield event.plain_result(f"❌ 拿群成员列表失败了喵：{e}")
            return
        # AstrBot call_action 返回解包后的 data：成员列表是 list，个别适配器可能包一层 dict
        members = ret.get("data") if isinstance(ret, dict) else ret
        if not isinstance(members, list):
            members = []
        if not members:
            yield event.plain_result("⚠️ 这个群没有成员列表喵（可能机器人不在群里，或者适配器不支持）。")
            return

        # 排序：群主 → 管理员 → 成员，同角色按入群时间升序
        role_rank = {"owner": 0, "admin": 1, "member": 2}
        members.sort(key=lambda m: (role_rank.get(m.get("role"), 9), m.get("join_time") or 0))

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["序号", "QQ号", "昵称", "群名片", "角色", "所在地", "入群时间"])
        for i, m in enumerate(members, 1):
            role = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(
                m.get("role"), str(m.get("role", "")))
            join_ts = m.get("join_time") or 0
            join_time = datetime.fromtimestamp(join_ts).strftime("%Y-%m-%d %H:%M") if join_ts else ""
            writer.writerow([
                i, m.get("user_id", ""), m.get("nickname", ""),
                m.get("card", ""), role, m.get("area", ""), join_time,
            ])

        out = self.data_dir / "exports" / f"members_{gid}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(buffer.getvalue(), encoding="utf-8-sig")

        yield event.chain_result([
            Plain(f"📋 群 {gid} 的成员列表喵～ 一共 {len(members)} 个人"),
            File(name=out.name, file=str(out)),
        ])

    # ---------- 管理 ----------

    @filter.command("战报删除", alias={"/战报删除"})
    async def delete(self, event: AstrMessageEvent, match_id: str = ""):
        """按 ID 删除战报（仅管理员）"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 删战报只有群管理/群主能做喵。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        if not match_id.isdigit():
            yield event.plain_result("用法喵：战报删除 <战报ID>")
            return
        ok = await self.db.delete_match(group_id, int(match_id))
        yield event.plain_result(
            "✅ 战报删掉啦喵～" if ok else "❌ 没找到这条战报喵，或者它不属于本群。"
        )

    @filter.command("战报撤销", alias={"/战报撤销"})
    async def undo(self, event: AstrMessageEvent):
        """撤销自己最近一条战报"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(_NEED_GROUP)
            return
        mid = await self.db.get_last_match_by_submitter(group_id, event.get_sender_id())
        if not mid:
            yield event.plain_result("喵…没有能撤销的记录呢。")
            return
        ok = await self.db.delete_match(group_id, mid)
        yield event.plain_result(
            "✅ 最近那条战报撤销好啦喵～" if ok else "❌ 撤销失败了喵… (；・∀・)"
        )

    @filter.command("帮助", alias={"/帮助", "/战报帮助"})
    async def help_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """帮助（按群属性分类；全部/超管）"""
        arg = arg.strip()
        # 只挡「群被禁用」这一条，**不**要求已绑定战队：帮助是用户遇到
        # "命令怎么不灵" 时的自助排查入口，被未绑定拦住就自相矛盾了。
        disabled = await self._check_enabled(event)
        if disabled:
            yield event.plain_result(disabled)
            return
        if arg == "超管":
            yield event.plain_result(stats.render_help(["超级管理"]))
            return
        if arg == "全部":
            yield event.plain_result(stats.render_help(stats.ALL_SECTIONS))
            return
        chat_type = "友谊群"
        group_id = event.get_group_id()
        if group_id and self.db_ready and self.db:
            chat_type = await self.db.get_group_chat_type(group_id)
        sections = stats.CHAT_TYPE_SECTIONS.get(chat_type, ["排表", "追加轮次", "记录比分"])
        yield event.plain_result(stats.render_help(sections))
