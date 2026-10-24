"""OneBot 好友申请、邀请机器人入群的转发与审批；不依赖战报数据库。"""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

logger = logging.getLogger(__name__)
_DECISION = re.compile(r"/?(同意|拒绝)(?:\s+([0-9a-fA-F]{8}))?")
_STATUSES = {"pending", "processing", "approved", "rejected", "uncertain", "superseded"}
_ALREADY_APPROVED = re.compile(
    r"OIDB error 120162004 on 0x10c8_[12]:\s*already agree msg by self", re.IGNORECASE)


class OneBotActionError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__("OneBot action failed")


def parse_decision(text: str) -> tuple[bool, str | None] | None:
    match = _DECISION.fullmatch(text.strip())
    return (match[1] == "同意", match[2].upper() if match[2] else None) if match else None


def _qq(value) -> str:
    value = str(value or "")
    return value if value.isascii() and value.isdigit() and int(value) > 0 else ""


def request_payload(raw, self_id: str) -> dict | None:
    if not isinstance(raw, Mapping) or raw.get("post_type") != "request":
        return None
    if not _qq(self_id) or _qq(raw.get("self_id")) != self_id:
        return None
    kind = raw.get("request_type")
    if kind == "group":
        if raw.get("sub_type") != "invite" or not _qq(raw.get("group_id")):
            return None
        invited = raw.get("invited_id")
        if invited not in (None, 0, "0", "") and _qq(invited) != self_id:
            return None
    elif kind != "friend":
        return None
    if not _qq(raw.get("user_id")) or not isinstance(raw.get("flag"), str) or not raw["flag"]:
        return None
    payload = {"kind": kind, "user_id": _qq(raw["user_id"]),
               "group_id": _qq(raw.get("group_id")) if kind == "group" else "",
               "flag": raw["flag"], "comment": str(raw.get("comment") or "")[:500]}
    if type(raw.get("time")) is int and raw["time"] > 0:
        payload["request_time"] = raw["time"]
    return payload


def invite_card_payload(raw, self_id: str) -> dict | None:
    """读取发给机器人的原始邀请卡；仅供已确认的 SnowLuma 连接使用。"""
    if (not isinstance(raw, Mapping) or raw.get("post_type") != "message"
            or raw.get("message_type") != "private" or raw.get("sub_type") != "friend"
            or raw.get("group_id") or _qq(raw.get("self_id")) != self_id
            or not _qq(self_id) or not _qq(raw.get("user_id"))
            or _qq(raw["user_id"]) == self_id or type(raw.get("time")) is not int
            or raw["time"] <= 0 or not isinstance(raw.get("message"), list)):
        return None
    for segment in raw["message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "json":
            continue
        data = segment.get("data")
        text = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(text, str) or len(text) > 65536:
            continue
        try:
            card = json.loads(text)
            if (not isinstance(card, dict)
                    or card.get("app") not in ("com.tencent.qun.invite", "com.tencent.tuwen.lua")):
                continue
            meta = card.get("meta")
            news = meta.get("news") if isinstance(meta, dict) else None
            jump = news.get("jumpUrl") if isinstance(news, dict) else None
            if not isinstance(jump, str):
                continue
            url = urlsplit(jump)
            if url.scheme != "mqqapi" or url.netloc != "group" or url.path != "/invite_join":
                continue
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=32)
            values = [query.get(key, []) for key in ("groupcode", "msgseq")]
            if any(len(v) != 1 or not _qq(v[0]) or int(v[0]) > 2**53 - 1 for v in values):
                continue
            group, sequence = (str(int(v[0])) for v in values)
        except (ValueError, TypeError, RecursionError):
            continue
        # 与 SnowLuma 的 formatGroupRequestFlag / 私聊邀请卡协议一致。
        return {"kind": "group", "user_id": _qq(raw["user_id"]), "group_id": group,
                "flag": f"slreq:1:{sequence}:{group}:2:0", "comment": "",
                "source": "snowluma_invite_card", "request_time": raw["time"]}
    return None


def _data(result):
    """兼容 CQHttp 解包结果和 OneBot 原始响应，拒绝显式失败。"""
    if isinstance(result, Mapping):
        if result.get("status") == "failed" or result.get("retcode", 0) != 0:
            raise OneBotActionError(result)
        return result.get("data", result)
    return result


def _message_text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(str(s.get("data", {}).get("text", "")) for s in message
                       if isinstance(s, Mapping) and s.get("type") == "text"
                       and isinstance(s.get("data"), Mapping))
    return ""


def _failure_message(exc, flag):
    """只展示平台返回的错误说明，不把 flag 或整个 API 响应发进 QQ。"""
    result = getattr(exc, "result", None)
    if not isinstance(result, Mapping):
        return "处理失败喵，请稍后重试。"
    detail = result.get("wording") or result.get("message") or result.get("msg")
    detail = str(detail or "").replace(flag, "[申请标识]")
    detail = " ".join(detail.split())[:200]
    code = result.get("retcode")
    if not detail and code is not None:
        detail = f"错误码 {code}"
    return f"处理失败喵：{detail}" if detail else "处理失败喵，请稍后重试。"


def _already_approved(exc):
    """QQ 对当前入群请求的明确结果；不把其他 OIDB 错误猜成成功。"""
    result = getattr(exc, "result", None)
    if not isinstance(result, Mapping) or result.get("status") != "failed":
        return False
    detail = result.get("wording") or result.get("message") or result.get("msg")
    return isinstance(detail, str) and _ALREADY_APPROVED.fullmatch(detail.strip()) is not None


def _handled_message(status, approve):
    agreed = status == "approved"
    result = f"这条申请已被{'同意' if agreed else '拒绝'}"
    if agreed != approve:
        result += f"，无法再{'同意' if approve else '拒绝'}"
    return result + "喵。"


class ReviewInbox:
    def __init__(self, path: Path, *, log=None):
        self.path = Path(path)
        self.logger = log if log is not None else logger
        self.lock = asyncio.Lock()
        self.refresh_lock = asyncio.Lock()
        self.notifying: set[str] = set()
        self.records: dict[str, dict] = {}
        if self.path.exists():
            content = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(content, dict) or content.get("version") != 1
                    or not isinstance(content.get("requests"), dict)):
                raise ValueError("不支持的申请记录格式")
            for token, record in content["requests"].items():
                if (not re.fullmatch(r"[0-9A-F]{8}", token) or not isinstance(record, dict)
                        or record.get("status") not in _STATUSES
                        or not isinstance(record.get("platform_id"), str)
                        or not all(isinstance(record.get(k), str)
                                   for k in ("comment", "notified_to", "notification_id"))
                        or not isinstance(record.get("notification_text", ""), str)
                        or record.get("source", "") not in ("", "snowluma_invite_card")
                        or (record.get("source") == "snowluma_invite_card"
                            and (record.get("kind") != "group" or "request_time" not in record))
                        or ("request_time" in record and (type(record["request_time"]) is not int
                                                          or record["request_time"] <= 0))
                        or not _qq(record.get("self_id"))
                        or request_payload({"post_type": "request", "self_id": record.get("self_id"),
                                            "request_type": record.get("kind"), "sub_type": "invite",
                                            **{k: record.get(k) for k in ("user_id", "group_id", "flag")}},
                                           record.get("self_id")) is None):
                    raise ValueError("申请记录损坏")
            self.records = content["requests"]
            # 上次进程可能在接口调用中退出，不能直接重复执行。
            recovered = {token: {**record, "status": "uncertain"} if record["status"] == "processing"
                         else record for token, record in self.records.items()}
            if recovered != self.records:
                self._commit(recovered)

    def _commit(self, records: dict[str, dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", delete=False) as f:
                name = f.name
                json.dump({"version": 1, "requests": records}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        self.records = records

    def _update(self, token, **changes):
        self._commit({**self.records, token: {**self.records[token], **changes}})

    @staticmethod
    async def _action(bot, self_id, action, **params):
        return _data(await bot.call_action(action, self_id=int(self_id), **params))

    @staticmethod
    def _in_scope(record, scope):
        return (record["platform_id"], record["self_id"]) == scope

    async def supports_invite_cards(self, scope, bot):
        try:
            info = await asyncio.wait_for(self._action(bot, scope[1], "get_version_info"), timeout=3)
        except Exception:
            self.logger.warning("无法确认 OneBot 实现，跳过 SnowLuma 邀请卡兼容")
            return False
        return isinstance(info, Mapping) and info.get("app_name") == "SnowLuma"

    async def receive(self, payload: dict, scope: tuple[str, str], bot, admin: str, *, refresh_tokens=()):
        async with self.lock:
            card = payload.get("source") == "snowluma_invite_card"
            related = {t: r for t, r in self.records.items()
                       if self._in_scope(r, scope) and r["kind"] == payload["kind"] == "group"
                       and r["group_id"] == payload["group_id"] and r["user_id"] == payload["user_id"]}
            # 历史查询期间申请可能已被处理，只恢复仍待处理且属于同一邀请人的群。
            if refresh_tokens and not any(t in related and related[t]["status"] == "pending"
                                          for t in refresh_tokens):
                return False
            if card and any(r.get("source") == "snowluma_invite_card"
                            and r["request_time"] > payload["request_time"] for r in related.values()):
                return
            # 新版同时上报卡片与 request；旧版的 UID 型 flag 则可能反复复用。
            # 已收到对应卡片时只保留其带 msgseq 的申请，不再发送一份旧推送通知。
            if (not card and payload["flag"].startswith(f"invite:{payload['group_id']}:")
                    and any(r.get("source") == "snowluma_invite_card"
                            and 0 <= r["request_time"] - payload.get("request_time", 0) <= 30
                            for r in related.values())):
                return
            token = next((t for t, r in self.records.items()
                          if self._in_scope(r, scope) and r["kind"] == payload["kind"]
                          and r["flag"] == payload["flag"]), None)
            if refresh_tokens and token is not None and self.records[token]["status"] != "pending":
                return False
            if card and token is not None and self.records[token]["status"] not in ("pending", "uncertain"):
                return
            changes = {}
            if token is None:
                token = uuid4().hex[:8].upper()
                while token in self.records:
                    token = uuid4().hex[:8].upper()
                changes[token] = {**payload, "platform_id": scope[0],
                    "self_id": scope[1], "status": "pending", "created_at": time.time(),
                    "notified_to": "", "notification_id": ""}
            if card:
                current = changes.get(token, self.records.get(token))
                changes[token] = {**current, "source": payload["source"], "request_time": payload["request_time"]}
                if current.get("source") != "snowluma_invite_card" and current["status"] == "pending":
                    changes[token]["notified_to"] = ""
                if refresh_tokens and current["status"] == "pending":
                    changes[token]["notified_to"] = ""
                for old, record in related.items():
                    if (old != token and record["status"] == "pending"
                            and record.get("request_time", 0) <= payload["request_time"]):
                        changes[old] = {**record, "status": "superseded"}
            if changes:
                self._commit({**self.records, **changes})
            record = self.records[token]
            if record["status"] != "pending" or record["notified_to"] == admin or token in self.notifying:
                return
            self.notifying.add(token)
        try:
            async def info(action, **kwargs):
                try:
                    result = await asyncio.wait_for(self._action(bot, scope[1], action, **kwargs), timeout=3)
                    return result if isinstance(result, Mapping) else {}
                except Exception:
                    return {}

            user, group = await asyncio.gather(
                info("get_stranger_info", user_id=int(record["user_id"])),
                info("get_group_info", group_id=int(record["group_id"]))
                if record["kind"] == "group" else asyncio.sleep(0, result={}),
            )
            async with self.lock:
                if self.records[token]["status"] != "pending":
                    return
            title = "好友申请" if record["kind"] == "friend" else "入群邀请"
            lines = [f"📩 {title} {token}", f"机器人 QQ：{scope[1]}",
                     f"{'申请人' if record['kind'] == 'friend' else '邀请人'} QQ：{record['user_id']}"]
            if user.get("nickname"):
                lines.append(f"昵称：{str(user['nickname'])[:100]}")
            if record["kind"] == "group":
                lines.append(f"群号：{record['group_id']}")
                if group.get("group_name"):
                    lines.append(f"群名：{str(group['group_name'])[:100]}")
            if record["comment"]:
                lines.append(f"验证消息：{record['comment']}")
            lines.append("引用本条回复“同意”或“拒绝”喵。")
            notice = "\n".join(lines)
            sent = await self._action(bot, scope[1], "send_private_msg", user_id=int(admin),
                                     message=[{"type": "text", "data": {"text": notice}}])
            mid = sent.get("message_id") if isinstance(sent, Mapping) else None
            message_id = str(mid) if mid is not None else ""
            async with self.lock:
                self._update(token, notified_to=admin, notification_id=message_id,
                             notification_text=notice)
            return True
        except Exception:
            self.logger.exception("转发 QQ 申请失败，可通过申请列表查看")
            if refresh_tokens:
                raise
        finally:
            async with self.lock:
                self.notifying.discard(token)

    async def refresh_requests(self, scope, bot, admin, token=None):
        """超管手动找回待处理群的原始邀请卡；只发通知，不执行审批。"""
        if self.refresh_lock.locked():
            return "正在刷新申请喵，请稍候。"
        async with self.refresh_lock:
            async with self.lock:
                pending = [(t, r) for t, r in self.records.items()
                           if self._in_scope(r, scope) and r["kind"] == "group"
                           and r["status"] == "pending" and (not token or token == t)]
                pending.sort(key=lambda item: item[1].get("created_at", 0), reverse=True)
                peers = list(dict.fromkeys(r["user_id"] for _, r in pending))
            if not pending:
                return "没有待刷新的入群邀请喵，请查看 /申请列表。"
            if not await self.supports_invite_cards(scope, bot):
                return "当前连接不支持找回邀请卡喵。"
            refreshed, failures = 0, 0
            # 只读已知邀请人的最近消息，不扫描陌生会话或从其他群创建申请。
            for peer in peers[:10]:
                anchors = {t: r for t, r in pending if r["user_id"] == peer}
                groups = {r["group_id"] for r in anchors.values()}
                try:
                    history = await asyncio.wait_for(self._action(
                        bot, scope[1], "get_friend_msg_history", user_id=int(peer),
                        message_id=0, count=100), timeout=10)
                    if not isinstance(history, Mapping) or not isinstance(history.get("messages"), list):
                        raise ValueError("无效的好友历史响应")
                    cards, ambiguous = {}, set()
                    for raw in history["messages"][:100]:
                        if not isinstance(raw, Mapping):
                            continue
                        # v1.14.11 sanitizeMessageEventForApi 会删除 post_type/self_id；
                        # 仅在已确认的当前连接历史响应中补齐，显式冲突的字段仍须拒绝。
                        payload = invite_card_payload({"post_type": "message", "self_id": scope[1], **raw}, scope[1])
                        if not payload or payload["user_id"] != peer or payload["group_id"] not in groups:
                            continue
                        group = payload["group_id"]
                        # 同群只保留最近的原始卡片，不把多次历史邀请逐一重新通知。
                        if group not in cards or payload["request_time"] > cards[group]["request_time"]:
                            cards[group] = payload
                            ambiguous.discard(group)
                        elif (payload["request_time"] == cards[group]["request_time"]
                              and payload["flag"] != cards[group]["flag"]):
                            ambiguous.add(group)
                    for group, payload in cards.items():
                        if group in ambiguous:
                            continue
                        if await self.receive(payload, scope, bot, admin, refresh_tokens=tuple(anchors)):
                            refreshed += 1
                except Exception:
                    failures += 1
                    self.logger.exception("刷新 QQ 申请失败")
            if refreshed:
                result = f"已刷新 {refreshed} 条邀请，请处理最新通知喵。"
                if failures:
                    result += "\n部分邀请刷新失败，可稍后重试。"
            elif failures:
                result = "刷新邀请失败喵，请查看日志。"
            else:
                result = "未找到可恢复的邀请卡喵，请让邀请人重新发送入群邀请。"
            if len(peers) > 10:
                result += "\n还有邀请未检查，可发送 /刷新申请 编号。"
            return result

    async def list_requests(self, scope):
        async with self.lock:
            records = [(t, r) for t, r in self.records.items()
                       if self._in_scope(r, scope) and r["status"] in ("pending", "processing", "uncertain")]
            if not records:
                return "暂无待处理申请喵。"
            lines = [f"📋 待处理申请 {len(records)} 条"]
            for token, r in records[:30]:
                label = "好友申请" if r["kind"] == "friend" else f"群邀请 {r['group_id']}"
                state = {"pending": "待处理", "processing": "处理中", "uncertain": "需核实"}[r["status"]]
                lines.append(f"{token} | {label} | QQ {r['user_id']} | {state}")
            lines.append("回复：同意 编号，或 拒绝 编号")
            return "\n".join(lines)

    def _match_notice(self, message, scope, admin):
        if not isinstance(message, Mapping):
            return None
        sender = message.get("sender")
        sender_id = sender.get("user_id") if isinstance(sender, Mapping) else message.get("sender_id")
        if _qq(sender_id) != scope[1] or message.get("message_type", "private") != "private":
            return None
        if message.get("target_id") is not None and _qq(message["target_id"]) != admin:
            return None
        text = _message_text(message.get("message")) or str(message.get("message_str") or "")
        for token, r in self.records.items():
            if not self._in_scope(r, scope) or r["notified_to"] != admin:
                continue
            if r.get("notification_text"):
                if text == r["notification_text"]:
                    return token
            else:
                # v1.18.0 的记录没有通知全文，核验固定抬头及原申请号码。
                title = "好友申请" if r["kind"] == "friend" else "入群邀请"
                actor = "申请人" if r["kind"] == "friend" else "邀请人"
                prefix = f"📩 {title} {token}\n机器人 QQ：{scope[1]}\n{actor} QQ：{r['user_id']}"
                lines = text.splitlines()
                if (text.startswith(prefix + "\n")
                        and (r["kind"] != "group" or f"群号：{r['group_id']}" in lines)):
                    return token
        return None

    async def _quoted_token(self, scope, bot, admin, ids, message):
        async with self.lock:
            matches = {t for t, r in self.records.items() if self._in_scope(r, scope)
                       and r["notified_to"] == admin and r["notification_id"]
                       and r["notification_id"] in ids}
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
            token = self._match_notice(message, scope, admin)
            if token:
                return token
        # AstrBot 可能改写 Reply.id，也可能在 get_msg 失败时丢掉 Reply 组件。
        # 用原始 reply.id 重取消息，只接受机器人发给当前超管的原通知。
        for mid in ids:
            if not re.fullmatch(r"-?\d{1,20}", mid):
                continue
            try:
                quoted = await asyncio.wait_for(
                    self._action(bot, scope[1], "get_msg", message_id=int(mid)), timeout=3)
            except Exception:
                continue
            async with self.lock:
                token = self._match_notice(quoted, scope, admin)
                if token:
                    return token
        return None

    async def decide(self, scope, bot, admin, approve, token=None, reply_id=None,
                     reply_ids=(), reply_message=None):
        ids = list(dict.fromkeys([str(i) for i in (reply_id, *reply_ids) if i is not None]))
        quoted = await self._quoted_token(scope, bot, admin, ids, reply_message) if ids else None
        async with self.lock:
            if ids and quoted is None:
                return "无法识别引用通知，请用 /申请列表 中的编号处理喵。"
            if token and quoted and token != quoted:
                return "编号与引用的申请不一致喵。"
            token = token or quoted
            if not token:
                pending = [t for t, r in self.records.items()
                           if self._in_scope(r, scope) and r["status"] == "pending"]
                if not pending:
                    return "暂无待处理申请喵。"
                if len(pending) > 1:
                    return "有多条申请，请引用通知回复，或发送“同意 编号”“拒绝 编号”喵。"
                token = pending[0]
            record = self.records.get(token)
            if not record or not self._in_scope(record, scope):
                return "找不到这条申请喵，请查看 /申请列表。"
            if record["status"] in ("approved", "rejected"):
                return _handled_message(record["status"], approve)
            if record["status"] == "superseded":
                return "这条邀请已更新，请处理最新通知喵。"
            if record["status"] == "processing":
                return "这条申请正在处理喵。"
            if record["status"] == "uncertain":
                return "上次处理结果未确认，请到 QQ 核实喵。"
            self._update(token, status="processing")
        outcome = "approved" if approve else "rejected"
        already_handled = False
        try:
            action = "set_friend_add_request" if record["kind"] == "friend" else "set_group_add_request"
            params = {"flag": record["flag"], "approve": approve}
            if record["kind"] == "group":
                params["sub_type"] = "invite"
            await self._action(bot, scope[1], action, **params)
        except asyncio.CancelledError:
            async with self.lock:
                self._update(token, status="uncertain")
            raise
        except (TimeoutError, OSError):
            async with self.lock:
                self._update(token, status="uncertain")
            self.logger.exception("处理 QQ 申请时连接中断")
            return "处理结果暂未确认，请到 QQ 核实喵。"
        except Exception as exc:
            if record["kind"] == "group" and _already_approved(exc):
                # 以 QQ 已确认的结果为准，不能因本次点击“拒绝”而记成 rejected。
                outcome, already_handled = "approved", True
                self.logger.info("QQ 已确认入群申请 %s 被同意，同步申请记录", token)
            else:
                async with self.lock:
                    self._update(token, status="pending")
                self.logger.exception("QQ 申请处理失败")
                return _failure_message(exc, record["flag"])
        async with self.lock:
            try:
                self._update(token, status=outcome)
            except OSError:
                self.logger.exception("QQ 已处理申请，但状态保存失败")
                return "QQ 已处理申请，记录保存失败，请到 QQ 核实喵。"
        result = (_handled_message(outcome, approve) if already_handled
                  else f"✅ 已{'同意' if approve else '拒绝'}申请 {token} 喵。")
        if record["kind"] == "group":
            # 先保存实际审批结果，再通知原邀请人；通知失败不能重新执行审批。
            notice = f"你的入群邀请已被{'同意' if outcome == 'approved' else '拒绝'}喵。\n群号：{record['group_id']}"
            try:
                await asyncio.wait_for(self._action(
                    bot, scope[1], "send_private_msg", user_id=int(record["user_id"]),
                    message=[{"type": "text", "data": {"text": notice}}]), timeout=10)
            except Exception:
                self.logger.exception("发送入群邀请结果通知失败")
                result += "\n邀请人通知未确认，请手动告知喵。"
        return result
