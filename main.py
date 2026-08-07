"""战队对战战报插件入口。

功能：
- 排表：`/排表 [规则]` + 战队名单 → 生成随机配对的第一轮战报模板
- 提交：`/发送` + 粘贴战报文本 → 解析入库（MySQL）
- 查询：排行 / 战绩 / 趋势图 / 导出 / 删除 / 撤销 / 帮助
- 数据按群隔离，存储于线上 MySQL（配置 mysql_* 字段）。
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
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Star, StarTools, register

from . import chart, lineup, stats
from .lineup import _int_to_cn, format_duel_results
from .battle_report_parser import (
    _parse_month_filter,
    _strip_ruled,
    _strip_sub,
    determine_match_winner,
    month_range,
    parse_battle_report,
    parse_export_payload,
    split_reports,
)
from .database import Database

_SUBMIT_CMDS = ("发送", "/发送", "战报", "/战报")
_LINEUP_CMDS = ("排表", "/排表")

# 查询类命令：新名称为主（去掉 战报 前缀），旧名称 战报Xxx 仍兼容
_RANK_CMDS = ("排行", "/排行", "战报排行", "/战报排行")
_RECORD_CMDS = ("战绩", "/战绩", "战报战绩", "/战报战绩")
_TREND_CMDS = ("趋势", "/趋势", "战报趋势", "/战报趋势")
_EXPORT_CMDS = ("导出", "/导出", "战报导出", "/战报导出")

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


_HELP_HINT = "\n更多功能请发送：/帮助"
_NEED_HOME = "⚠️ 本群未绑定战队，无法使用该功能。\n请管理/群主使用 /绑定战队 <战队> 绑定。"


def _strip_command(raw: str, cmds: tuple[str, ...]) -> str:
    """剥离指令词前缀（兼容带/不带唤醒前缀）。"""
    raw = raw.strip()
    for cmd in cmds:
        if raw == cmd:
            return ""
        if raw.startswith(cmd):
            return raw[len(cmd):].strip()
    return raw


@register("battle_report", "RLotusX", "战队对战战报：排表、提交、排行、趋势、导出", "1.12.14")
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
            return "❌ 数据库未连接，请检查插件配置中的 MySQL 连接信息。"
        return None

    def _date_from(self, days: int) -> str | None:
        if not days or days <= 0:
            return None
        return (datetime.now() - timedelta(days=days)).date().isoformat()

    async def _get_effective_home(self, group_id: str) -> str | None:
        """群战队绑定优先，其次配置 home_team。"""
        if self.db_ready and self.db:
            bound = await self.db.get_group_home(group_id)
            if bound:
                return bound
        return str(self.config.get("home_team", "") or "").strip() or None

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
            # 只取以『战队:』开头的战报消息
            return [t for t in texts if t and t.lstrip().startswith("战队:")]

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
        if text and text.lstrip().startswith("战队:"):
            # 兜底：get_msg 返回疑似被平台截断（无法完整解析）时，从群消息历史按 ID 重取
            if parse_battle_report(text).errors:
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
            yield event.plain_result("⚠️ 请在群聊中使用排表。" + _HELP_HINT)
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _LINEUP_CMDS)
        if not payload:
            # 未传任何参数：分两段发送排表使用指南
            yield event.plain_result(
                "欢迎使用海马集团 智能排表秘书\n"
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
                "❌ 排表失败：\n" + "\n".join(result.errors)
                + "\n\n格式示例：\nKC:红莲 凯撒亮 悠悠球\nDYG:老千 蓝大 红大"
                + _HELP_HINT
            )
            return
        if len(result.teams) < 2:
            yield event.plain_result("❌ 需要至少两支队伍才能排表。" + _HELP_HINT)
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
            yield event.plain_result("❌ 仅群管理或群主可绑定战队。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        team = team.strip().upper()
        if not team:
            yield event.plain_result("用法：绑定战队 <战队名>")
            return
        await self.db.set_group_home(group_id, team)
        await self.db.backfill_group_home(group_id, team)
        yield event.plain_result(f"✅ 本群战队已绑定为：{team}（已有战报已回填）")

    @filter.command("查看战队", alias={"/查看战队"})
    async def view_home(self, event: AstrMessageEvent):
        """查看本群战队"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        home = await self.db.get_group_home(group_id)
        if home:
            yield event.plain_result(f"🏠 本群战队：{home}")
        else:
            yield event.plain_result(
                "本群尚未绑定战队，请管理/群主使用 /绑定战队 <战队> 绑定。"
            )

    # ---------- 群禁用管理（超级管理员） ----------

    async def _admin_check(self, event) -> str | None:
        """组合检查：数据库就绪 + 超级管理员。"""
        err = self._check_db()
        if err:
            return err
        if not self._is_super_admin(event):
            return "❌ 仅超级管理员可执行此操作。"
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
            yield event.plain_result("用法：禁群 <群号>")
            return
        await self.db.set_group_ban(gid, True)
        yield event.plain_result(f"🚫 群 {gid} 已禁用插件功能。")

    @filter.command("启群", alias={"/启群"})
    async def enable_group(self, event: AstrMessageEvent, group_id: str = ""):
        """开启某个群的全部插件功能（仅超级管理员）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        gid = group_id.strip()
        if not gid:
            yield event.plain_result("用法：启群 <群号>")
            return
        await self.db.set_group_ban(gid, False)
        yield event.plain_result(f"✅ 群 {gid} 已启用插件功能。")

    @filter.command("查群", alias={"/查群"})
    async def check_group(self, event: AstrMessageEvent, group_id: str = ""):
        """查询群的禁用状态（仅超级管理员）"""
        err = await self._admin_check(event)
        if err:
            yield event.plain_result(err)
            return
        gid = group_id.strip()
        if not gid:
            yield event.plain_result("用法：查群 <群号>")
            return
        banned = await self.db.get_group_ban(gid)
        yield event.plain_result(
            f"🚫 群 {gid} 已禁用插件功能。" if banned else f"✅ 群 {gid} 插件功能正常。"
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
            yield event.plain_result("暂无战队。")
            return
        yield event.plain_result("🏆 全部战队：\n" + "、".join(teams))

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
                f"暂无群。" if not filter_team else f"暂无绑定 {filter_team} 的群。"
            )
            return
        title = "📋 群列表：" if not filter_team else f"📋 绑定 {filter_team} 的群："
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
            yield event.plain_result("❌ 仅群管理或群主可设置群属性。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        chat_type = chat_type.strip()
        if chat_type not in ("友谊群", "战报群", "主群"):
            yield event.plain_result("用法：群聊属性 <友谊群|战报群|主群>")
            return
        await self.db.set_group_chat_type(group_id, chat_type)
        yield event.plain_result(f"✅ 本群属性已设为「{chat_type}」。")

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
            yield event.plain_result("用法：通告 <内容>\n　或：通告 <群号/QQ号>\n<内容>")
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result("❌ 无法获取平台连接。")
            return

        # 首行是纯数字 → 视为指定目标（群/人），其余为内容
        lines = rest.split("\n")
        target = lines[0].strip() if lines and lines[0].strip().isdigit() else ""
        content = "\n".join(lines[1:]).strip() if target else rest
        if not content:
            yield event.plain_result("用法：通告 <内容>\n　或：通告 <群号/QQ号>\n<内容>")
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
                    yield event.plain_result(f"❌ 发送到群 {target} 失败：{e}")
                    return
                yield event.plain_result(f"📢 通告已发送到群 {target}。")
            else:
                try:
                    await bot.call_action("send_private_msg", user_id=int(target), message=content)
                except Exception as e:
                    logger.warning(f"通告发送失败 {target}: {e}")
                    yield event.plain_result(f"❌ 发送给 {target} 失败：{e}")
                    return
                yield event.plain_result(f"📢 通告已发送给 {target}。")
            return

        # 发送到所有群
        try:
            groups = await bot.call_action("get_group_list")
        except Exception as e:
            logger.warning(f"获取群列表失败: {e}")
            yield event.plain_result("❌ 获取群列表失败。")
            return
        if not isinstance(groups, list):
            groups = []
        if not groups:
            yield event.plain_result("当前机器人不在任何群中。")
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

        msg = f"📢 通告已发送到 {ok} 个群。"
        if failed:
            msg += f"\n⚠️ {len(failed)} 个群发送失败：{'、'.join(failed[:10])}"
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
                return "🚫 本群已被管理员禁用插件功能。"
        return None

    async def _group_check(self, event) -> str | None:
        """组合检查：数据库就绪 + 群未被禁用。"""
        err = self._check_db()
        if err:
            return err
        return await self._check_enabled(event)

    async def _require_home(self, event) -> tuple[str | None, str | None]:
        """获取群号与绑定战队；失败时返回 (错误文案, None)。"""
        err = await self._group_check(event)
        if err:
            return err, None
        group_id = event.get_group_id()
        if not group_id:
            return "⚠️ 请在群聊中使用。", None
        home = await self.db.get_group_home(group_id)
        if not home:
            return "❌ 本群未绑定战队，请管理/群主使用 /绑定战队 <战队> 绑定。", None
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
            yield event.plain_result(
                "你尚未绑定参赛ID，请先使用 /查ID <参赛ID> 进行模糊查询，"
                "之后使用 /绑定ID <参赛ID> 绑定。"
            )
            return
        players = await self.db.get_user_players(home, user["id"])
        yield event.plain_result(
            f"👤 你的身份：{user['name']}（{home}）\n"
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
            yield event.plain_result(f"未找到匹配的参赛ID（战队 {home}）。")
            return
        lines = [f"🔍 战队 {home} 参赛ID（/绑定ID <参赛ID> 绑定）："]
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
            yield event.plain_result("用法：绑定ID <参赛ID> [参赛ID ...]")
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
            yield event.plain_result("用法：解绑ID <参赛ID> [参赛ID ...]")
            return

        qq = event.get_sender_id()
        my_user = await self.db.get_user_by_qq(home, qq)
        is_manager = await self._is_manager(event)
        lines: list[str] = []
        for p in names:
            binding = await self.db.get_player_binding(home, p)
            if not binding or not binding.get("user_id"):
                lines.append(f"⚠️ 参赛ID「{p}」未绑定角色，无需解除。")
                continue
            if (my_user and binding["user_id"] == my_user["id"]) or is_manager:
                await self.db.unbind_player(home, p)
                tag = "（管理操作）" if is_manager and not (my_user and binding["user_id"] == my_user["id"]) else ""
                lines.append(f"✅ 已解除参赛ID「{p}」的绑定{tag}。")
            else:
                lines.append(f"❌ 参赛ID「{p}」绑定在其他角色（{binding['user_name']}）下，无法解除。")
        yield event.plain_result("\n".join(lines))

    async def _bind_one_id(self, home, player, qq, my_user):
        """绑定单个参赛ID到用户。返回 (提示文案, 生效的 my_user)。"""
        pool = await self.db.get_player_pool(home, player, 50)
        if player not in pool:
            return f"❌ 参赛ID「{player}」不在战队 {home} 的参赛记录中。", my_user

        binding = await self.db.get_player_binding(home, player)
        if binding and binding.get("user_id"):
            # 该参赛ID已绑定某角色
            if my_user and my_user["id"] == binding["user_id"]:
                return f"✅ 参赛ID「{player}」已在你自己的角色下。", my_user
            if my_user:
                # 已绑定其他角色：无论该角色是否被认领，都不得改绑到自己的角色
                return f"❌ 参赛ID「{player}」已绑定角色「{binding['user_name']}」，不能绑定到你的角色「{my_user['name']}」。", my_user
            # 我无角色 → 认领该ID所在的角色
            status, uid = await self.db.claim_user_by_name(home, binding["user_name"], qq)
            if status == "claimed_else":
                return f"❌ 参赛ID「{player}」已被其他角色（{binding['user_name']}）绑定。", my_user
            if status == "not_found":
                uid = await self.db.find_or_create_user(home, binding["user_name"], qq)
                await self.db.bind_player_to_user(home, player, uid)
            user = await self.db.get_user_by_id(uid)
            players = await self.db.get_user_players(home, uid)
            return (
                f"✅ 已认领角色「{user['name']}」（{home}）并绑定参赛ID\n"
                f"📌 该角色参赛ID：{'、'.join(players)}",
                user,
            )

        # 未绑定 → 挂到我的角色（无角色则创建，以参赛ID为初始角色名）
        uid = await self.db.find_or_create_user(home, player, qq)
        await self.db.bind_player_to_user(home, player, uid)
        user = await self.db.get_user_by_id(uid)
        return f"✅ 参赛ID「{player}」已绑定到你的角色「{user['name']}」。", user

    @filter.command("管理ID", alias={"/管理ID"})
    async def admin_id(self, event: AstrMessageEvent):
        """管理/群主：查看/批量绑定本战队参赛ID（逗号分隔）"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 仅群管理或群主可管理参赛ID。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, ("管理ID", "/管理ID")).strip()
        if not payload:
            status = await self.db.get_pool_status(home, None, 50)
            if not status:
                yield event.plain_result(f"战队 {home} 暂无参赛ID（尚无战报记录）。")
                return
            lines = [f"📋 战队 {home} 参赛ID列表："]
            for s in status:
                mark = f"→ {s['user_name']}" if s["user_name"] else "（未绑定）"
                lines.append(f"{s['player']} {mark}")
            lines.append("绑定：/管理ID <参赛ID[,参赛ID...]> <用户名>")
            yield event.plain_result("\n".join(lines))
            return

        # 最后一个空白分隔符为用户名，其余为逗号分隔的参赛ID集合
        parts = payload.rsplit(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：管理ID <参赛ID[,参赛ID...]> <用户名>")
            return
        id_part, username = parts[0], parts[1].strip()
        players = [_strip_ruled(_strip_sub(p)[0])[0] for p in id_part.split(",") if p.strip()]
        if not players or not username:
            yield event.plain_result("用法：管理ID <参赛ID[,参赛ID...]> <用户名>")
            return

        pool = await self.db.get_player_pool(home, None, 500)
        uid = await self.db.find_or_create_user(home, username)
        lines = []
        for p in players:
            if p not in pool:
                lines.append(f"❌ 参赛ID「{p}」不在战队 {home} 的参赛记录中。")
                continue
            await self.db.bind_player_to_user(home, p, uid)
            lines.append(f"✅ 参赛ID「{p}」已绑定到用户「{username}」。")
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
            yield event.plain_result("你尚未绑定参赛ID，请先使用 /查ID <参赛ID> 进行模糊查询，之后使用 /绑定ID <参赛ID> 绑定。")
            return
        players = await self.db.get_user_players(home, user["id"])
        if not players:
            yield event.plain_result(f"用户「{user['name']}」尚未绑定任何参赛ID。")
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

        lines = [
            f"👤 {user['name']}（{home}）",
            f"📌 参赛ID：{'、'.join(players)}",
        ]
        if len(players) == 1:
            # 只有一个ID：汇总即该ID明细，无需再列
            lines.append(
                f"📊 战绩：胜{wins} 负{losses} 平{agg['draws']}  总{total}  "
                f"友谊{agg.get('friendship', 0)}  胜率{wr}%"
            )
        else:
            lines.append(
                f"📊 汇总战绩：胜{wins} 负{losses} 平{agg['draws']}  总{total}  "
                f"友谊{agg.get('friendship', 0)}  胜率{wr}%"
            )
            for p in players:
                rec = await self.db.get_player_record(home, p, date_from, date_to)
                w = int(rec.get("wins", 0))
                l = int(rec.get("losses", 0))
                d = int(rec.get("draws", 0))
                t = int(rec.get("total", 0))
                wr2 = round(w * 100.0 / (w + l), 1) if (w + l) else 0.0
                lines.append(
                    f"  · {p}：胜{w} 负{l} 平{d} 总{t} 友谊{rec.get('friendship', 0)} 胜率{wr2}%"
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
            yield event.plain_result("你尚未绑定参赛ID，请先使用 /查ID <参赛ID> 进行模糊查询，之后使用 /绑定ID <参赛ID> 绑定。")
            return
        name = name.strip()
        if not name:
            yield event.plain_result("用法：改名 <新名字>")
            return
        if len(name) > 30:
            yield event.plain_result("名字过长（最多 30 字）。")
            return
        status = await self.db.rename_user(home, user["id"], name)
        if status == "conflict":
            yield event.plain_result(f"❌ 名字「{name}」已被本战队其他用户使用。")
            return
        yield event.plain_result(f"✅ 已改名为「{name}」（{home}）。")

    # ---------- 追加轮次（/第N轮） ----------

    @filter.custom_filter(RoundCommandFilter)
    async def round_cmd(self, event: AstrMessageEvent):
        """追加轮次：/第N轮 [玩家A [比分] 玩家B]；无追加时随机匹配上一轮胜者"""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
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
            yield event.plain_result("❌ 轮次应为 2 及以上的数字（如 /第二轮、/第三轮）。")
            return

        info_lines = [ln.strip() for ln in text[m.end():].splitlines() if ln.strip()]

        draft = await self._read_latest_report(event)
        if not draft:
            yield event.plain_result("❌ 未在群聊记录中找到战报，请先使用 /排表 生成。")
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
            yield event.plain_result("⚠️ 部分对局未解析：\n" + "\n".join(result.errors))

    # ---------- 记录比分（/记录） ----------

    @filter.command("记录", alias={"/记录"})
    async def record_cmd(self, event: AstrMessageEvent):
        """记录比分：/记录 玩家名 比分 [对手]；无参数时提示格式"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        disabled = await self._check_enabled(event)
        if disabled:
            yield event.plain_result(disabled)
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, ("记录", "/记录"))
        if not payload.strip():
            yield event.plain_result(
                "请提供记录信息，格式：\n"
                "记录 玩家名 比分\n"
                "记录 玩家名 比分 对手\n"
                "比分支持 2:0 或紧凑 20。"
            )
            return

        info_lines = [ln.strip() for ln in payload.splitlines() if ln.strip()]
        draft = await self._read_latest_report(event)
        if not draft:
            yield event.plain_result("❌ 未在群聊记录中找到战报，请先使用 /排表 生成。")
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
            yield event.plain_result("❌ 没有成功记录任何对局。\n" + "\n".join(errors))
            return
        yield event.plain_result(current)
        if errors:
            yield event.plain_result("⚠️ 部分记录失败：\n" + "\n".join(errors))

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
            yield event.plain_result("❌ 当前配置下仅管理员可提交战报。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _SUBMIT_CMDS)

        # 无参数且无引用时直接提示
        if not payload.strip() and not await self._has_reply(event):
            yield event.plain_result(
                "请提供战报内容：\n"
                "· /发送 + 粘贴战报文本\n"
                "· 或回复引用合并转发的战报消息后发送 /发送"
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
            yield event.plain_result("❌ 战报内容为空。")
            return

        # 逐个解析：失败不阻断后续战报，记录报错（含行号）后跳过该份
        reply_hint = (
            "\n\n（提示：引用内容可能不完整，可重新发送该战报或直接 /发送 粘贴全文）"
            if from_reply else ""
        )
        parsed: list = []
        fail_msgs: list[str] = []
        for i, chunk in enumerate(report_chunks, 1):
            result = parse_battle_report(chunk)
            if result.errors:
                fail_msgs.append(
                    f"❌ 第 {i} 份战报解析失败：\n"
                    + "\n".join(result.errors)
                    + "\n\n📄 收到的战报原文：\n" + chunk.strip()
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
            loc = parsed[0][1].location or ""
            if self.config.get("allow_private_chat", True) and loc.strip():
                group_id = loc.strip()
            else:
                yield event.plain_result(
                    "⚠️ 无法确定群号：请在群聊提交，或让战报中的『地点:』填写群号。"
                )
                return

        # 上传需群已绑定战队
        bound_home = await self.db.get_group_home(group_id)
        if not bound_home:
            yield event.plain_result(
                "❌ 本群未绑定战队，无法上传战报。\n请管理/群主使用 /绑定战队 <战队> 绑定。"
            )
            return
        home_team = bound_home

        # 逐个提交：成功与失败分两个回复集合
        success_responses: list[str] = []
        failure_responses: list[str] = list(fail_msgs)
        for i, report, warnings, chunk in parsed:
            report.group_id = group_id
            report.submitted_by = event.get_sender_id()
            report.submitted_name = event.get_sender_name()
            report.created_at = int(time.time())

            # 未完成对局：对阵不足 3 场
            if len(report.duels) < 3:
                failure_responses.append(
                    f"❌ 第 {i} 份战报未完成对局（对阵仅 {len(report.duels)} 场，需 ≥3 场）：\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time}\n"
                    f"📄 收到的战报原文：\n{chunk.strip()}"
                )
                continue

            # 判定胜者：胜负未定则不记录
            winner = determine_match_winner(report)
            if winner is None:
                unfinished = [
                    f"第{_int_to_cn(d.round_no)}轮 {d.player_a} 0:0 {d.player_b}"
                    for d in report.duels if d.score_a == 0 and d.score_b == 0
                ]
                msg = (
                    f"❌ 第 {i} 份比赛胜负未定，未记录：\n"
                    f"{report.team_a} VS {report.team_b} | {report.match_time}"
                )
                if unfinished:
                    msg += "\n存在未填写比分的对局：\n" + "\n".join(unfinished)
                msg += (
                    "\n📄 收到的战报原文：\n" + chunk.strip()
                    + "\n（请填写比分后重试）"
                )
                failure_responses.append(msg)
                continue

            if home_team == winner:
                home_result = f"🏆 {home_team} 获胜！"
            elif home_team in (report.team_a, report.team_b):
                home_result = f"💀 {home_team} 战败"
            else:
                home_result = f"本场胜者：{winner}"

            try:
                match_id = await self.db.insert_report(report, winner, home_team, chunk)
            except Exception as e:
                logger.exception("战报入库失败")
                failure_responses.append(
                    f"❌ 写入失败：{report.team_a} VS {report.team_b} | {report.match_time}\n{e}"
                )
                continue

            # 汇总 + 每场对阵结果（供核对）
            summary = (
                f"✅ 战报已记录（ID {match_id}）\n"
                f"{report.team_a} VS {report.team_b} | {report.match_time} | "
                f"共 {len(report.duels)} 局\n"
                f"{home_result}\n"
                f"{format_duel_results(report, home_team)}"
            )
            success_responses.append(summary)
            if warnings:
                success_responses.append("⚠️ 解析警告：\n" + "\n".join(warnings))

        # 成功集与失败集分别发送（各自 ≤3 逐条，否则合并转发）
        for x in self._send_responses(event, success_responses):
            yield x
        for x in self._send_responses(event, failure_responses):
            yield x

    # ---------- 查询 ----------

    @filter.command("排行", alias={"/排行", "/战报排行"})
    async def ranking(self, event: AstrMessageEvent):
        """排行榜（个人/队伍），默认本月，末尾可加 X月"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return

        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _RANK_CMDS)
        )
        tokens = payload.split()
        scope = tokens[0] if tokens else "个人"
        date_from, date_to = month_range(month)
        limit = int(self.config.get("ranking_limit", 10) or 10)

        home_team = await self._get_effective_home(group_id)
        if not home_team:
            yield event.plain_result(_NEED_HOME)
            return
        try:
            if scope in ("队伍", "战队", "队"):
                rows = await self.db.get_home_team_vs_opponents(
                    home_team, date_from, date_to
                )
                yield event.plain_result(stats.format_home_team_vs(home_team, rows))
            else:
                min_games = int(self.config.get("min_games", 1) or 1)
                month_label = f"{month}月" if month else "本月"
                if scope in ("全部", "所有"):
                    rows = await self.db.get_player_ranking(
                        home_team, date_from, date_to, min_games, limit, team=None
                    )
                    fallback = stats.format_player_ranking(rows, limit, min_games)
                    title = f"个人积分榜（全战队 · 前 {limit}）"
                    caption = f"🏆 全战队个人榜（前 {limit} · {month_label}）"
                    # 全战队 top-N 按配置截断（默认 30 行）
                    max_rows = int(self.config.get("ranking_image_max_rows", 30) or 30)
                else:
                    # 默认只统计战队选手，显示全部队员（不设上限，图片不截断）
                    rows = await self.db.get_player_ranking(
                        home_team, date_from, date_to, min_games, None, team=home_team
                    )
                    note = f"\n（战队 {home_team}，共 {len(rows)} 人）" if home_team else ""
                    fallback = stats.format_player_ranking(rows, None, min_games) + note
                    title = f"个人积分榜（{home_team} · 共 {len(rows)} 人 · {month_label}）"
                    caption = f"🏆 {home_team} 个人榜（{month_label}）"
                    max_rows = None  # 全部队员，不截断
                if rows and self.config.get("ranking_image", True):
                    # 图片表格展示，生成失败回退文字表格
                    try:
                        cells = stats.build_ranking_cells(rows)
                        aligns = ["right", "left"] + ["right"] * 7
                        out = self.data_dir / "rankings" / f"rank_{int(time.time())}.png"
                        path = chart.make_ranking_image(cells, aligns, title, out, max_rows)
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
            yield event.plain_result("❌ 查询出错，请稍后重试。")

    @filter.command("战绩", alias={"/战绩", "/战报战绩"})
    async def record(self, event: AstrMessageEvent):
        """个人战绩，默认本月，末尾可加 X月"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _RECORD_CMDS)
        )
        name = payload.strip()
        date_from, date_to = month_range(month)
        home_team = await self._get_effective_home(group_id)
        if not home_team:
            yield event.plain_result(_NEED_HOME)
            return
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
            yield event.plain_result("❌ 查询出错，请稍后重试。")

    @filter.command("趋势", alias={"/趋势", "/战报趋势"})
    async def trend(self, event: AstrMessageEvent):
        """胜率走势图，默认本月，末尾可加 X月 或 [最近N天]"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        payload, month = _parse_month_filter(
            _strip_command(event.get_message_str(), _TREND_CMDS)
        )
        tokens = payload.split()
        name = tokens[0] if tokens else ""
        days = tokens[1] if len(tokens) > 1 else ""
        home_team = await self._get_effective_home(group_id)
        if not home_team:
            yield event.plain_result(_NEED_HOME)
            return
        if not name:
            # 未指定时默认展示战队
            name = home_team
        if not name:
            yield event.plain_result("用法：趋势 <玩家名或队伍名> [最近N天|X月]")
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
                yield event.plain_result(f"未找到「{name}」{title_suffix}的数据。")
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
                Plain(f"📈 {display} 胜率走势："),
                Image.fromFileSystem(str(path)),
            ])
        except Exception:
            logger.exception("趋势图生成失败")
            yield event.plain_result("❌ 趋势生成出错，请稍后重试。")

    @filter.command("导出", alias={"/导出", "/战报导出"})
    async def export(self, event: AstrMessageEvent):
        """导出战报：可指定玩家/胜场负场/时间（X月|最近N天），合并转发或 csv/json 文件"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
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

        home_team = await self._get_effective_home(group_id)
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
                yield event.plain_result("❌ 导出失败，请稍后重试。")
                return
            if players is not None or outcome != "全部":
                # 以比赛级聚合过滤出命中 match_id，再裁剪对局行
                try:
                    reports = await self.db.get_reports_for_export(home_team, date_from, date_to)
                except Exception:
                    logger.exception("导出查询失败")
                    yield event.plain_result("❌ 导出失败，请稍后重试。")
                    return
                reports = lineup.filter_report_outcome(reports, outcome, players, member_team)
                keep = {r["match_id"] for r in reports}
                rows = [r for r in rows if r["match_id"] in keep]
            if not rows:
                msg = f"{player_label} 没有符合条件的战报。" if player_label else "当前战队暂无战报数据。"
                yield event.plain_result(msg)
                return

            if fmt == "csv":
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    "战报ID", "群号", "队伍A", "队伍B", "日期", "规则", "地点",
                    "轮次", "玩家A", "比分A", "玩家B", "比分B", "胜者",
                    "玩家A替补", "玩家B替补",
                ])
                for r in rows:
                    writer.writerow([
                        r["match_id"], r["group_id"], r["team_a"], r["team_b"], r["match_time"],
                        r["rule"], r["location"], r["round_no"], r["player_a"], r["score_a"],
                        r["player_b"], r["score_b"], r["result"],
                        "是" if r["a_sub"] else "", "是" if r["b_sub"] else "",
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
                Plain("📦 战报数据："),
                File(name=out.name, file=str(out)),
            ])
            return

        # ---------- 合并转发导出（全部/胜场/负场） ----------
        try:
            reports = await self.db.get_reports_for_export(home_team, date_from, date_to)
        except Exception:
            logger.exception("导出查询失败")
            yield event.plain_result("❌ 导出失败，请稍后重试。")
            return
        reports = lineup.filter_report_outcome(reports, outcome, players, member_team)
        if not reports:
            msg = f"{player_label} 没有符合条件的战报。" if player_label else "没有符合该条件的战报。"
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
        yield event.plain_result(f"📤 已导出 {len(nodes)} 份战报（{filter_label}），共 {batch} 条转发。")

    # ---------- 管理 ----------

    @filter.command("战报删除", alias={"/战报删除"})
    async def delete(self, event: AstrMessageEvent, match_id: str = ""):
        """按 ID 删除战报（仅管理员）"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 仅群管理或群主可删除战报。")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        if not match_id.isdigit():
            yield event.plain_result("用法：战报删除 <战报ID>")
            return
        ok = await self.db.delete_match(group_id, int(match_id))
        yield event.plain_result("✅ 已删除战报。" if ok else "❌ 未找到该战报或不属于当前群。")

    @filter.command("战报撤销", alias={"/战报撤销"})
    async def undo(self, event: AstrMessageEvent):
        """撤销自己最近一条战报"""
        err = await self._group_check(event)
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        mid = await self.db.get_last_match_by_submitter(group_id, event.get_sender_id())
        if not mid:
            yield event.plain_result("没有可撤销的记录。")
            return
        ok = await self.db.delete_match(group_id, mid)
        yield event.plain_result("✅ 已撤销最近一条战报。" if ok else "❌ 撤销失败。")

    @filter.command("帮助", alias={"/帮助", "/战报帮助"})
    async def help_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """帮助（按群属性分类；全部/超管）"""
        arg = arg.strip()
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
