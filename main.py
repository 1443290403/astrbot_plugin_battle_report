"""战队对战战报插件入口。

功能：
- 排表：`/排表 [规则]` + 战队名单 → 生成随机配对的第一轮战报模板
- 提交：`/战报` + 粘贴战报文本 → 解析入库（MySQL）
- 查询：排行 / 战绩 / 趋势图 / 导出 / 删除 / 撤销 / 帮助
- 数据按群隔离，存储于线上 MySQL（配置 mysql_* 字段）。
"""

import csv
import io
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Star, StarTools, register

from . import chart, lineup, stats
from .lineup import format_duel_results
from .battle_report_parser import (
    BattleReport,
    determine_match_winner,
    parse_battle_report,
    split_reports,
)
from .database import Database

_SUBMIT_CMDS = ("战报", "/战报")
_LINEUP_CMDS = ("排表", "/排表")

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
    "红莲 2:1 牌大\n"
    "凯撒亮 2:1 蓝大"
)

_HELP_TEXT = (
    "📋 战队对战战报插件\n"
    "━━━━━━━━━━━━\n"
    "▎排表\n"
    "/排表 [规则]\n"
    "KC:红莲 凯撒亮 悠悠球\n"
    "DYG:老千 蓝大 红大\n"
    "→ 生成随机配对的第一轮战报模板\n\n"
    "▎追加轮次\n"
    "/第二轮 [玩家A [比分] 玩家B]\n"
    "无追加：随机匹配上一轮胜者\n"
    "如：/第二轮 红莲 2:0 蓝大（记录比分）\n"
    "读取群聊中最近一条战报并追加该轮\n\n"
    "▎记录比分\n"
    "/记录 玩家名 比分 [对手]\n"
    "如：/记录 红莲 20（填入红莲最后一场未记录对阵）\n"
    "如：/记录 红莲 20 蓝大（无未记录对阵时插入最新轮次）\n"
    "比分支持 2:0 或紧凑 20\n\n"
    "▎提交战报\n"
    "/战报 + 粘贴排表模板（填入实际比分）\n\n"
    "▎查询\n"
    "/战报排行 [个人|队伍]  排行榜\n"
    "/战报战绩 <玩家名>      个人战绩\n"
    "/战报趋势 <玩家名|队伍> [天数]  胜率走势图\n"
    "/战报导出 [csv|json]   导出数据\n\n"
    "▎战队绑定\n"
    "/绑定战队 <战队>        绑定本群战队（管理/群主）\n"
    "/查看战队              查看本群战队\n\n"
    "▎用户与参赛ID\n"
    "/查ID <关键词>          模糊查询本战队参赛ID\n"
    "/绑定ID <参赛ID>        将参赛ID绑定到自己的用户\n"
    "/改名 <新名字>          修改自己的用户名称\n"
    "/我的ID                查看/确认自己的身份\n"
    "/我的战绩              查询自己的总战绩\n"
    "/管理ID [参赛ID] [用户名] 管理/群主查看/绑定参赛ID\n\n"
    "▎管理\n"
    "/战报删除 <战报ID>      仅管理/群主\n"
    "/战报撤销              撤销自己最近一条\n"
    "/看排表                查看本群战队名单\n"
    "/战报帮助              本帮助"
)


def _strip_command(raw: str, cmds: tuple[str, ...]) -> str:
    """剥离指令词前缀（兼容带/不带唤醒前缀）。"""
    raw = raw.strip()
    for cmd in cmds:
        if raw == cmd:
            return ""
        if raw.startswith(cmd):
            return raw[len(cmd):].strip()
    return raw


@register("battle_report", "RLotusX", "战队对战战报：排表、提交、排行、趋势、导出", "1.3.8")
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

    async def _roster_warnings(self, group_id: str, report: BattleReport) -> list[str]:
        """战报玩家是否都在已排表名单中（软校验，不阻断）。"""
        try:
            teams = await self.db.get_teams(group_id)
        except Exception:
            return []
        if not teams:
            return []
        declared = {t["player_name"] for t in teams}
        unknown: list[str] = []
        for d in report.duels:
            if d.player_a not in declared:
                unknown.append(d.player_a)
            if d.player_b not in declared:
                unknown.append(d.player_b)
        if unknown:
            return [f"⚠️ 以下玩家不在已排表的名单中：{'、'.join(dict.fromkeys(unknown))}"]
        return []

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
        """从 OneBot 消息对象中提取纯文本。"""
        arr = msg.get("message")
        if isinstance(arr, list):
            parts = []
            for seg in arr:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str(seg.get("data", {}).get("text", "")))
            if parts:
                return "".join(parts)
        raw = msg.get("raw_message") or msg.get("message_str") or ""
        return str(raw)

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
        """提取合并转发内单条消息的纯文本（兼容 content / message 两种段结构）。"""
        for key in ("content", "message"):
            arr = item.get(key)
            if isinstance(arr, list):
                parts = []
                for seg in arr:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        parts.append(str(seg.get("data", {}).get("text", "")))
                if parts:
                    return "".join(parts)
        raw = item.get("raw_message") or item.get("message_str") or ""
        return str(raw)

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
            # 只取以『战队:』开头的战报消息
            return [t for t in texts if t and t.lstrip().startswith("战队:")]

        # 非转发：被引用消息自身的文本（须为战报）
        text = self._extract_msg_text(msg)
        if text and text.lstrip().startswith("战队:"):
            return [text]
        return None

    # ---------- 排表 ----------

    @filter.command("排表", alias={"/排表"})
    async def lineup_cmd(self, event: AstrMessageEvent):
        """排表：解析名单 → 存库 → 生成随机配对模板"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用排表。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _LINEUP_CMDS)
        result = lineup.parse_lineup(payload, self.config.get("default_rule", "2/3【KOF】"))
        if result.errors:
            yield event.plain_result(
                "❌ 排表失败：\n" + "\n".join(result.errors)
                + "\n\n格式示例：\nKC:红莲 凯撒亮 悠悠球\nDYG:老千 蓝大 红大"
            )
            return
        if len(result.teams) < 2:
            yield event.plain_result("❌ 需要至少两支队伍才能排表。")
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

    @filter.command("看排表", alias={"/看排表"})
    async def view_lineup(self, event: AstrMessageEvent):
        """查看当前群已存的战队名单"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        teams = await self.db.get_teams(group_id)
        grouped: dict[str, list[str]] = {}
        for t in teams:
            grouped.setdefault(t["team_name"], []).append(t["player_name"])
        yield event.plain_result(
            "📋 本群战队名单：\n" + lineup.format_roster_display(list(grouped.items()))
        )

    # ---------- 群战队绑定 ----------

    @filter.command("绑定战队", alias={"/绑定战队"})
    async def bind_home(self, event: AstrMessageEvent, team: str = ""):
        """绑定本群战队（管理/群主）"""
        err = self._check_db()
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
        err = self._check_db()
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

    # ---------- 用户与参赛ID ----------

    async def _require_home(self, event) -> tuple[str | None, str | None]:
        """获取群号与绑定战队；失败时返回 (错误文案, None)。"""
        err = self._check_db()
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
                f"你尚未绑定参赛ID。\n用 /查ID <关键词> 查询本战队参赛ID，"
                f"再用 /绑定ID <参赛ID> 绑定。"
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
            mark = f"（已绑 {s['user_name']}）" if s["user_name"] else "（未绑定）"
            lines.append(f"{s['player']} {mark}")
        yield event.plain_result("\n".join(lines))

    @filter.command("绑定ID", alias={"/绑定ID"})
    async def bind_id(self, event: AstrMessageEvent, player: str = ""):
        """将参赛ID绑定到自己的用户（或认领已有用户）"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        player = player.strip()
        if not player:
            yield event.plain_result("用法：绑定ID <参赛ID>")
            return
        pool = await self.db.get_player_pool(home, player, 50)
        if player not in pool:
            yield event.plain_result(
                f"❌ 参赛ID「{player}」不在战队 {home} 的参赛记录中。\n用 /查ID <关键词> 查询。"
            )
            return

        qq = event.get_sender_id()
        binding = await self.db.get_player_binding(home, player)
        my_user = await self.db.get_user_by_qq(home, qq)

        if binding and binding.get("user_id"):
            # 该参赛ID已绑定某角色
            if my_user and my_user["id"] == binding["user_id"]:
                yield event.plain_result(f"✅ 参赛ID「{player}」已在你自己的角色下。")
                return
            if my_user:
                # 我已有角色 → 把该ID挂到我的角色下（多ID绑定到一个角色）
                await self.db.bind_player_to_user(home, player, my_user["id"])
                yield event.plain_result(
                    f"✅ 参赛ID「{player}」已绑定到你的角色「{my_user['name']}」。"
                )
                return
            # 我无角色 → 认领该ID所在的角色
            status, uid = await self.db.claim_user_by_name(home, binding["user_name"], qq)
            if status == "claimed_else":
                yield event.plain_result(
                    f"❌ 参赛ID「{player}」已被其他角色（{binding['user_name']}）绑定。"
                )
                return
            if status == "not_found":
                uid = await self.db.find_or_create_user(home, binding["user_name"], qq)
                await self.db.bind_player_to_user(home, player, uid)
            user = await self.db.get_user_by_id(uid)
            players = await self.db.get_user_players(home, uid)
            yield event.plain_result(
                f"✅ 已认领角色「{user['name']}」（{home}）并绑定参赛ID\n"
                f"📌 该角色参赛ID：{'、'.join(players)}"
            )
            return

        # 未绑定 → 挂到我的角色（无角色则创建，以参赛ID为初始角色名）
        uid = await self.db.find_or_create_user(home, player, qq)
        await self.db.bind_player_to_user(home, player, uid)
        user = await self.db.get_user_by_id(uid)
        yield event.plain_result(
            f"✅ 参赛ID「{player}」已绑定到你的角色「{user['name']}」。\n"
            f"可用 /我的ID 查看身份、/我的战绩 查询汇总战绩。"
        )

    @filter.command("管理ID", alias={"/管理ID"})
    async def admin_id(self, event: AstrMessageEvent, player: str = "", username: str = ""):
        """管理/群主：查看/绑定本战队参赛ID"""
        err, home = await self._require_home(event)
        if err:
            yield event.plain_result(err)
            return
        if not await self._is_manager(event):
            yield event.plain_result("❌ 仅群管理或群主可管理参赛ID。")
            return
        if not player:
            status = await self.db.get_pool_status(home, None, 50)
            if not status:
                yield event.plain_result(f"战队 {home} 暂无参赛ID（尚无战报记录）。")
                return
            lines = [f"📋 战队 {home} 参赛ID列表："]
            for s in status:
                mark = f"→ {s['user_name']}" if s["user_name"] else "（未绑定）"
                lines.append(f"{s['player']} {mark}")
            lines.append("绑定：/管理ID <参赛ID> <用户名>")
            yield event.plain_result("\n".join(lines))
            return
        if not username:
            yield event.plain_result("用法：管理ID <参赛ID> <用户名>")
            return
        pool = await self.db.get_player_pool(home, player, 50)
        if player not in pool:
            yield event.plain_result(
                f"❌ 参赛ID「{player}」不在战队 {home} 的参赛记录中。"
            )
            return
        uid = await self.db.find_or_create_user(home, username.strip())
        await self.db.bind_player_to_user(home, player, uid)
        yield event.plain_result(f"✅ 参赛ID「{player}」已绑定到用户「{username.strip()}」。")

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
            yield event.plain_result("你尚未绑定参赛ID，使用 /绑定ID <参赛ID> 绑定。")
            return
        players = await self.db.get_user_players(home, user["id"])
        if not players:
            yield event.plain_result(f"用户「{user['name']}」尚未绑定任何参赛ID。")
            return
        days = int(self.config.get("default_days", 0) or 0)
        date_from = self._date_from(days)
        agg = await self.db.get_players_aggregate(
            None, players, date_from, None, home_team=home
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
                f"📊 战绩：胜{wins} 负{losses} 平{agg['draws']}  总{total}  胜率{wr}%"
            )
        else:
            lines.append(
                f"📊 汇总战绩：胜{wins} 负{losses} 平{agg['draws']}  总{total}  胜率{wr}%"
            )
            for p in players:
                rec = await self.db.get_player_record(None, p, date_from, None, home_team=home)
                w = int(rec.get("wins", 0))
                l = int(rec.get("losses", 0))
                d = int(rec.get("draws", 0))
                t = int(rec.get("total", 0))
                wr2 = round(w * 100.0 / (w + l), 1) if (w + l) else 0.0
                lines.append(f"  · {p}：胜{w} 负{l} 平{d} 总{t} 胜率{wr2}%")
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
            yield event.plain_result("你尚未绑定参赛ID，使用 /绑定ID <参赛ID> 绑定。")
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

    @filter.command("战报", alias={"/战报"})
    async def submit_report(self, event: AstrMessageEvent):
        """提交战报（可一次粘贴多份，按『战队:』行拆分）"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        if self.config.get("submit_requires_admin", False) and not event.is_admin():
            yield event.plain_result("❌ 当前配置下仅管理员可提交战报。")
            return

        raw = event.get_message_str()
        payload = _strip_command(raw, _SUBMIT_CMDS)

        # 优先从回复引用（含合并转发）提取战报
        reply_reports = await self._extract_reply_reports(event)
        if reply_reports:
            report_chunks = reply_reports
        else:
            report_chunks = split_reports(payload)
        if not report_chunks:
            yield event.plain_result("❌ 战报内容为空。")
            return

        # 解析所有战报
        parsed: list = []
        for chunk in report_chunks:
            result = parse_battle_report(chunk)
            if result.errors:
                yield event.plain_result(
                    "❌ 战报解析失败：\n" + "\n".join(result.errors) + "\n\n" + _FORMAT_EXAMPLE
                )
                return
            parsed.append((result.report, result.warnings))

        # 确定群号
        group_id = event.get_group_id()
        if not group_id:
            loc = parsed[0][0].location or ""
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

        # 逐个提交，收集回复
        responses: list[str] = []
        for report, warnings in parsed:
            report.group_id = group_id
            report.submitted_by = event.get_sender_id()
            report.submitted_name = event.get_sender_name()
            report.created_at = int(time.time())

            # 判定胜者：胜负未定则不记录
            winner = determine_match_winner(report)
            if winner is None:
                responses.append(
                    f"❌ 比赛胜负未定，未记录：\n{report.team_a} VS {report.team_b} | "
                    f"{report.match_time}\n（请补全比分后重试）"
                )
                continue

            if home_team == winner:
                home_result = f"🏆 {home_team} 获胜！"
            elif home_team in (report.team_a, report.team_b):
                home_result = f"💀 {home_team} 战败"
            else:
                home_result = f"本场胜者：{winner}"

            try:
                match_id = await self.db.insert_report(report, winner, home_team)
            except Exception as e:
                logger.exception("战报入库失败")
                responses.append(
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
            responses.append(summary)
            if warnings:
                responses.append("⚠️ 解析警告：\n" + "\n".join(warnings))
            for w in await self._roster_warnings(group_id, report):
                responses.append(w)

        # 发送：>3 条合并为一个转发消息集，否则逐条
        if len(responses) <= 3:
            for r in responses:
                yield event.plain_result(r)
        else:
            nodes = [
                Node(
                    name=event.get_sender_name(),
                    uin=event.get_sender_id(),
                    content=[Plain(r)],
                )
                for r in responses
            ]
            yield event.chain_result([Nodes(nodes)])

    # ---------- 查询 ----------

    @filter.command("战报排行", alias={"/战报排行"})
    async def ranking(self, event: AstrMessageEvent, scope: str = ""):
        """排行榜（个人/队伍）"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return

        days = int(self.config.get("default_days", 0) or 0)
        date_from = self._date_from(days)
        scope = (scope or "个人").strip()
        limit = int(self.config.get("ranking_limit", 10) or 10)

        home_team = await self._get_effective_home(group_id)
        try:
            if scope in ("队伍", "战队", "队"):
                rows = await self.db.get_team_ranking(
                    group_id, date_from, None, limit, home_team=home_team
                )
                yield event.plain_result(stats.format_team_ranking(rows, limit))
            else:
                min_games = int(self.config.get("min_games", 1) or 1)
                if scope in ("全部", "所有"):
                    rows = await self.db.get_player_ranking(
                        group_id, date_from, None, min_games, limit,
                        team=None, home_team=home_team,
                    )
                    yield event.plain_result(stats.format_player_ranking(rows, limit, min_games))
                else:
                    # 默认只统计战队选手，显示全部队员（不设上限）
                    rows = await self.db.get_player_ranking(
                        group_id, date_from, None, min_games, None,
                        team=home_team, home_team=home_team,
                    )
                    note = f"\n（战队 {home_team}，共 {len(rows)} 人）" if home_team else ""
                    yield event.plain_result(stats.format_player_ranking(rows, None, min_games) + note)
        except Exception:
            logger.exception("排行查询失败")
            yield event.plain_result("❌ 查询出错，请稍后重试。")

    @filter.command("战报战绩", alias={"/战报战绩"})
    async def record(self, event: AstrMessageEvent, name: str = ""):
        """个人战绩"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        if not name.strip():
            yield event.plain_result("用法：战报战绩 <玩家名>")
            return
        days = int(self.config.get("default_days", 0) or 0)
        home_team = await self._get_effective_home(group_id)
        try:
            agg = await self.db.get_player_record(
                group_id, name.strip(), self._date_from(days), None, home_team=home_team
            )
            yield event.plain_result(stats.format_player_record(name.strip(), agg))
        except Exception:
            logger.exception("战绩查询失败")
            yield event.plain_result("❌ 查询出错，请稍后重试。")

    @filter.command("战报趋势", alias={"/战报趋势"})
    async def trend(self, event: AstrMessageEvent, name: str = "", days: str = ""):
        """胜率走势图"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        name = name.strip()
        home_team = await self._get_effective_home(group_id)
        if not name:
            # 未指定时默认展示战队
            name = home_team or ""
        if not name:
            yield event.plain_result("用法：战报趋势 <玩家名或队伍名> [最近N天]")
            return
        d = int(days) if days.isdigit() else int(self.config.get("default_days", 30) or 30)
        date_from = self._date_from(d)

        try:
            pts = await self.db.get_player_trend(group_id, name, date_from, home_team=home_team)
            if not pts:
                pts = await self.db.get_team_trend(group_id, name, date_from, home_team=home_team)
            if not pts:
                yield event.plain_result(f"未找到「{name}」最近 {d} 天的数据。")
                return
            points = stats.compute_cumulative(pts)
            out = self.data_dir / "trends" / f"trend_{int(time.time())}.png"
            path = chart.make_trend_chart(
                points,
                f"{name} 胜率走势（最近 {d} 天）",
                out,
                int(self.config.get("trend_chart_width", 960) or 960),
                int(self.config.get("trend_chart_height", 480) or 480),
            )
            yield event.chain_result([
                Plain(f"📈 {name} 胜率走势："),
                Image.fromFileSystem(str(path)),
            ])
        except Exception:
            logger.exception("趋势图生成失败")
            yield event.plain_result("❌ 趋势生成出错，请稍后重试。")

    @filter.command("战报导出", alias={"/战报导出"})
    async def export(self, event: AstrMessageEvent, fmt: str = ""):
        """导出战报数据（csv/json）"""
        err = self._check_db()
        if err:
            yield event.plain_result(err)
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return
        fmt = (fmt or "csv").lower()
        if fmt not in ("csv", "json"):
            yield event.plain_result("❌ 格式仅支持 csv / json。")
            return

        try:
            rows = await self.db.get_export_rows(group_id)
        except Exception:
            logger.exception("导出查询失败")
            yield event.plain_result("❌ 导出失败，请稍后重试。")
            return
        if not rows:
            yield event.plain_result("当前群暂无战报数据。")
            return

        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([
                "战报ID", "群号", "队伍A", "队伍B", "日期", "规则", "地点",
                "轮次", "玩家A", "比分A", "玩家B", "比分B", "胜者",
            ])
            for r in rows:
                writer.writerow([
                    r["match_id"], r["group_id"], r["team_a"], r["team_b"], r["match_time"],
                    r["rule"], r["location"], r["round_no"], r["player_a"], r["score_a"],
                    r["player_b"], r["score_b"], r["result"],
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

    # ---------- 管理 ----------

    @filter.command("战报删除", alias={"/战报删除"})
    async def delete(self, event: AstrMessageEvent, match_id: str = ""):
        """按 ID 删除战报（仅管理员）"""
        err = self._check_db()
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
        err = self._check_db()
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

    @filter.command("战报帮助", alias={"/战报帮助"})
    async def help_cmd(self, event: AstrMessageEvent):
        """帮助"""
        yield event.plain_result(_HELP_TEXT)
