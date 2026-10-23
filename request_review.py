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
from uuid import uuid4

logger = logging.getLogger(__name__)
_DECISION = re.compile(r"/?(同意|拒绝)(?:\s+([0-9a-fA-F]{8}))?")
_STATUSES = {"pending", "processing", "approved", "rejected", "uncertain"}


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
    return {"kind": kind, "user_id": _qq(raw["user_id"]),
            "group_id": _qq(raw.get("group_id")) if kind == "group" else "",
            "flag": raw["flag"], "comment": str(raw.get("comment") or "")[:500]}


def _data(result):
    """兼容 CQHttp 解包结果和 OneBot 原始响应，拒绝显式失败。"""
    if isinstance(result, Mapping):
        if result.get("status") == "failed" or result.get("retcode", 0) != 0:
            raise ValueError("OneBot action failed")
        return result.get("data", result)
    return result


class ReviewInbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = asyncio.Lock()
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

    async def receive(self, payload: dict, scope: tuple[str, str], bot, admin: str):
        async with self.lock:
            token = next((t for t, r in self.records.items()
                          if self._in_scope(r, scope) and r["kind"] == payload["kind"]
                          and r["flag"] == payload["flag"]), None)
            if token is None:
                token = uuid4().hex[:8].upper()
                while token in self.records:
                    token = uuid4().hex[:8].upper()
                self._commit({**self.records, token: {**payload, "platform_id": scope[0],
                    "self_id": scope[1], "status": "pending", "created_at": time.time(),
                    "notified_to": "", "notification_id": ""}})
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
            sent = await self._action(bot, scope[1], "send_private_msg", user_id=int(admin),
                                     message=[{"type": "text", "data": {"text": "\n".join(lines)}}])
            mid = sent.get("message_id") if isinstance(sent, Mapping) else None
            message_id = str(mid) if mid is not None else ""
            async with self.lock:
                self._update(token, notified_to=admin, notification_id=message_id)
        except Exception:
            logger.exception("转发 QQ 申请失败，可通过申请列表查看")
        finally:
            async with self.lock:
                self.notifying.discard(token)

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

    async def decide(self, scope, bot, admin, approve, token=None, reply_id=None):
        async with self.lock:
            quoted = next((t for t, r in self.records.items() if self._in_scope(r, scope)
                           and r["notified_to"] == admin and r["notification_id"] == reply_id), None) if reply_id else None
            if reply_id and quoted is None:
                return "引用的消息不是申请通知喵，请查看 /申请列表。"
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
                return "这条申请已经处理过了喵。"
            if record["status"] == "processing":
                return "这条申请正在处理喵。"
            if record["status"] == "uncertain":
                return "上次处理结果未确认，请到 QQ 核实喵。"
            self._update(token, status="processing")
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
        except (TimeoutError, ConnectionError):
            async with self.lock:
                self._update(token, status="uncertain")
            logger.exception("处理 QQ 申请时连接中断")
            return "处理结果暂未确认，请到 QQ 核实喵。"
        except Exception:
            async with self.lock:
                self._update(token, status="pending")
            logger.exception("QQ 申请处理失败")
            return "处理失败喵，请稍后重试。"
        async with self.lock:
            try:
                self._update(token, status="approved" if approve else "rejected")
            except OSError:
                logger.exception("QQ 已处理申请，但状态保存失败")
                return "QQ 已处理申请，记录保存失败，请到 QQ 核实喵。"
        return f"✅ 已{'同意' if approve else '拒绝'}申请 {token} 喵。"
