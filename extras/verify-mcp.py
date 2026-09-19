#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 服务器冒烟验证：真发一次 stdio 握手，不靠"文件在不在"猜。

为什么值得单独写一个：
    离线机上的 MCP 失败模式几乎全是**静默**的 —— 配置写进了 config.yaml、
    `hermes doctor` 不报错、启动日志里最多一行 warning，只有在真正要用工具时
    才发现某个 server 根本起不来。而"文件存在 / 能 import"完全不能证明它
    能作为 MCP server 说话（版本不匹配、入口不对、缺原生扩展都会在握手时才暴露）。

    所以这里模拟 Hermes 原生客户端的行为：
        initialize  ->  notifications/initialized  ->  tools/list
    拿到 tools 数量才算通过。

用法：
    python verify-mcp.py --servers servers.json [--only name,name] [--json]

servers.json 形如：
    [{"name": "...", "command": "...", "args": [...], "env": {...}}, ...]
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2024-11-05"

# 与 Hermes 原生 MCP 客户端一致的白名单式环境变量传递：
# 只给这些 + 每个 server 自己声明的 env，避免把 API key 漏给子进程。
SAFE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM",
            "SHELL", "TMPDIR")


def build_env(extra: dict | None) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    for k, v in os.environ.items():
        if k.startswith("XDG_"):
            env[k] = v
    env.update(extra or {})
    return env


def _pump(stream, q: "queue.Queue[str | None]") -> None:
    """把子进程的一路输出逐行推进队列；EOF 时放一个 None 当哨兵。"""
    try:
        for line in stream:
            q.put(line)
    except Exception:  # noqa: BLE001 - 子进程被杀时读会炸，忽略即可
        pass
    finally:
        q.put(None)


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _wait_for_id(q: "queue.Queue[str | None]", want_id: int,
                 deadline: float) -> dict | None:
    """从队列里找 id == want_id 的 JSON-RPC 响应；顺手跳过通知/日志行。"""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:          # 子进程退出了
            return None
        line = line.strip()
        if not line:
            continue
        # 有些 node server 会先往 stdout 打非 JSON 的横幅，跳过即可
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg


def probe(server: dict, timeout: float) -> dict:
    name = server["name"]
    cmd = [server["command"], *server.get("args", [])]
    result: dict = {"name": name, "ok": False, "tools": 0, "detail": ""}

    if not os.path.isfile(cmd[0]):
        result["detail"] = f"命令不存在：{cmd[0]}"
        return result

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=build_env(server.get("env")),
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=server.get("cwd") or None,
        )
    except OSError as exc:
        result["detail"] = f"启动失败：{exc}"
        return result

    out_q: "queue.Queue[str | None]" = queue.Queue()
    err_lines: list[str] = []
    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_q), daemon=True)
    t_err = threading.Thread(
        target=lambda: [err_lines.append(ln) for ln in proc.stderr], daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hermes-extras-verify", "version": "1.0"},
            },
        })
        init = _wait_for_id(out_q, 1, deadline)
        if init is None:
            result["detail"] = "initialize 无响应（超时或进程提前退出）"
        elif "error" in init:
            result["detail"] = f"initialize 报错：{init['error']}"
        else:
            server_info = (init.get("result") or {}).get("serverInfo") or {}
            _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                         "params": {}})
            tools = _wait_for_id(out_q, 2, deadline)
            if tools is None:
                result["detail"] = "tools/list 无响应"
            elif "error" in tools:
                result["detail"] = f"tools/list 报错：{tools['error']}"
            else:
                items = (tools.get("result") or {}).get("tools") or []
                result["ok"] = True
                result["tools"] = len(items)
                result["server_info"] = server_info.get("name", "")
                result["detail"] = f"{len(items)} 个工具"
    finally:
        for closer in (proc.stdin, proc.stdout, proc.stderr):
            try:
                closer and closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    if not result["ok"] and err_lines:
        tail = " | ".join(ln.strip() for ln in err_lines[-3:] if ln.strip())
        if tail:
            result["detail"] = f"{result['detail']}；stderr: {tail[:400]}"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP 服务器 stdio 冒烟验证")
    ap.add_argument("--servers", required=True, help="服务器清单 JSON")
    ap.add_argument("--only", default="", help="只验这些（逗号分隔）")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    servers = json.loads(open(args.servers, encoding="utf-8").read())
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        servers = [s for s in servers if s["name"] in want]

    results = []
    for s in servers:
        r = probe(s, args.timeout)
        results.append(r)
        if not args.json:
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {r['name']:18s} {r['detail']}", flush=True)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    bad = [r for r in results if not r["ok"]]
    if bad:
        print(f"\n{len(bad)}/{len(results)} 个 MCP 服务器没通过握手", file=sys.stderr)
        return 1
    print(f"\n{len(results)} 个 MCP 服务器全部握手成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
