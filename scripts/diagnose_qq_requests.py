#!/usr/bin/env python3
"""只读收集 QQ 申请诊断信息；支持宿主机和 Docker，不加载插件或连接 QQ。"""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
STATUSES = {"pending", "processing", "approved", "rejected", "uncertain", "superseded"}
ROOTS = (Path("/AstrBot/data/plugin_data"), Path("/app/data/plugin_data"),
         Path("/data/plugin_data"), Path("data/plugin_data"))


def fingerprint(value):
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:12]


def flag_details(flag, group):
    if not isinstance(flag, str):
        return {"format": "invalid"}
    detail = {"fingerprint": fingerprint(flag)}
    match = re.fullmatch(r"slreq:(\d{1,3}):(\d{1,20}):(\d{1,20}):(\d{1,5}):([01])", flag)
    if match:
        detail.update(format="slreq:" + match[1], sequence=match[2], event_type=int(match[4]),
                      filtered=match[5] == "1", group_matches_record=match[3] == str(group))
    elif re.fullmatch(r"(?:invite|add):[0-9]+:.+", flag, re.DOTALL):
        detail["format"] = "legacy_" + flag.split(":", 1)[0]
    elif re.fullmatch(r"[0-9]{1,20}", flag):
        detail.update(format="sequence", sequence=flag)
    else:
        detail["format"] = "opaque"
    return detail


def timestamp(value):
    return value if type(value) in (int, float) and 0 < value < 10**12 and math.isfinite(value) else None


def read_store(path, token=None, limit=8):
    """固定字段白名单；不输出 QQ、昵称、验证消息、原始 flag 或通知全文。"""
    result = {"path": str(path)}
    if path.name != "request_reviews.json":
        return {**result, "error": "not_a_request_store"}
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {**result, "error": "store_too_large"}
        content = json.loads(raw)
        if (not isinstance(content, dict) or content.get("version") != 1
                or not isinstance(content.get("requests"), dict)):
            return {**result, "error": "unsupported_store_format"}
        rows = []
        for code, record in content["requests"].items():
            if (not re.fullmatch(r"[0-9A-Fa-f]{8}", code) or not isinstance(record, dict)
                    or (token and code.upper() != token.upper())):
                continue
            source = record.get("source", "absent")
            rows.append({
                "token": code.upper(),
                "kind": record.get("kind") if record.get("kind") in ("friend", "group") else "unknown",
                "status": record.get("status") if isinstance(record.get("status"), str)
                          and record["status"] in STATUSES else "unknown",
                "source": source if source in ("absent", "snowluma_invite_card") else "unknown",
                "request_time": timestamp(record.get("request_time")),
                "created_at": timestamp(record.get("created_at")),
                "bot_ref": fingerprint(record.get("self_id", "")),
                "group_ref": fingerprint(record.get("group_id", "")),
                "sender_ref": fingerprint(record.get("user_id", "")),
                "flag": flag_details(record.get("flag"), record.get("group_id")),
            })
        rows.sort(key=lambda row: row["created_at"] or row["request_time"] or 0, reverse=True)
        return {**result, "total_records": len(content["requests"]), "records": rows[:limit]}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {**result, "error": "store_unreadable_or_invalid"}


def local_stores(paths=None, token=None, limit=8):
    candidates = set()
    for root in paths or ROOTS:
        root = Path(root)
        try:
            if root.name == "request_reviews.json":
                if root.is_file():
                    candidates.add(root.resolve())
            elif root.is_dir():
                candidates.update(p.resolve() for p in root.glob("*/request_reviews.json") if p.is_file())
        except OSError:
            continue
    return [read_store(path, token, limit) for path in sorted(candidates)]


def docker_stores(container=None, token=None, limit=8):
    notes, stores = [], []
    if not shutil.which("docker"):
        return stores, ["docker_unavailable"]
    try:
        if container:
            containers = [container]
        else:
            listing = subprocess.run(["docker", "ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Names}}"],
                                     capture_output=True, text=True, timeout=8)
            if listing.returncode:
                return stores, ["docker_list_failed"]
            lines = listing.stdout.splitlines()
            preferred = [line for line in lines if "astrbot" in line.lower()]
            containers = [line.split("\t")[0] for line in (preferred or lines)][:20]
        # 使用当前脚本的同一套读取逻辑；容器内 --worker 禁止再次枚举 Docker。
        source = Path(__file__).read_text(encoding="utf-8")
        deadline = time.monotonic() + 30
        for name in containers:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                notes.append("docker_scan_timeout")
                break
            cmd = ["docker", "exec", "-i", name, "python3", "-", "--worker", "--limit", str(limit)]
            if token:
                cmd += ["--token", token]
            try:
                output = subprocess.run(cmd, input=source, capture_output=True, text=True,
                                        timeout=min(8, remaining))
                if output.returncode or len(output.stdout) > 256 * 1024:
                    notes.append("container_reader_failed:" + name)
                    continue
                report = json.loads(output.stdout)
                if not isinstance(report, dict) or not isinstance(report.get("stores"), list):
                    raise ValueError("invalid worker report")
                if not all(isinstance(store, dict) for store in report["stores"]):
                    raise ValueError("invalid worker stores")
                for store in report["stores"]:
                    stores.append({**store, "container": name})
            except (OSError, ValueError, subprocess.TimeoutExpired):
                notes.append("container_reader_failed:" + name)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        notes.append("docker_reader_unavailable")
    return stores, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--path", type=Path, help="指定 request_reviews.json 或 plugin_data 目录")
    location.add_argument("--container", help="只检查指定的 AstrBot 容器")
    parser.add_argument("--token", help="只显示指定的八位申请编号")
    parser.add_argument("--limit", type=int, default=8, help="每个文件最多显示几条，默认 8")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.token and not re.fullmatch(r"[0-9A-Fa-f]{8}", args.token):
        parser.error("--token 必须是八位十六进制编号")
    if not 1 <= args.limit <= 50:
        parser.error("--limit 必须在 1 到 50 之间")
    if args.container and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.container):
        parser.error("--container 格式无效")
    stores = [] if args.container else local_stores([args.path] if args.path else None, args.token, args.limit)
    notes = []
    if not args.worker and not args.path:
        extra, notes = docker_stores(args.container, args.token, args.limit)
        stores += extra
    if not stores:
        notes.append("未找到申请记录；可用 --container 指定容器，或 --path 指定文件路径。")
    print(json.dumps({"stores": stores, "notes": notes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
