#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MCP 服务器写进 $HERMES_HOME/config.yaml 的 mcp_servers 节。

三条硬规则（都是踩过的坑换来的）：

1. **先备份再改**。config.yaml 里有用户自己的 provider / 模型 / 工具配置，
   改坏了很难重建。每次写之前都留一份带时间戳的 .bak。
2. **只加不覆盖**。同名条目如果 `command` 不是指向本次安装目录下的 mcp/，
   说明是用户自己配的，跳过并报告 —— 绝不静默顶掉。
3. **保留注释**。上游的 cli-config.yaml.example 是带大段注释的，用 PyYAML
   safe_dump 会把注释全抹掉。优先用 ruamel.yaml 的 round-trip 模式；
   实在没有才退回 PyYAML 并明确告知。

用法：
    python merge-mcp-config.py --config <config.yaml> --servers <servers.json>
                              [--template <cli-config.yaml.example>]
                              [--mcp-dir <安装目录/mcp>] [--remove] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

# 我们写进去的条目，command 一定落在这个目录下；用它判断"这条是不是我们的"
DEFAULT_MCP_DIR = ""


def _owned(entry: object, mcp_dir: str) -> bool:
    if not isinstance(entry, dict) or not mcp_dir:
        return False
    cmd = str(entry.get("command", ""))
    args = entry.get("args") or []
    blob = cmd + " " + " ".join(str(a) for a in args)
    return mcp_dir in blob


def load(path: Path):
    """返回 (数据, 序列化函数, 是否保注释)。"""
    raw = path.read_text(encoding="utf-8")
    try:
        from ruamel.yaml import YAML
        y = YAML()
        y.preserve_quotes = True
        y.width = 4096            # 别把长路径折行
        data = y.load(raw) or {}
        return data, (lambda d, fh: y.dump(d, fh)), True
    except ImportError:
        import yaml as pyyaml
        data = pyyaml.safe_load(raw) or {}
        return (data,
                lambda d, fh: pyyaml.safe_dump(
                    d, fh, allow_unicode=True, sort_keys=False,
                    default_flow_style=False),
                False)


def main() -> int:
    ap = argparse.ArgumentParser(description="合并 MCP 服务器配置")
    ap.add_argument("--config", required=True)
    ap.add_argument("--servers", required=True, help="install-extras.sh 生成的 servers.json")
    ap.add_argument("--template", default="", help="config.yaml 不存在时的模板")
    ap.add_argument("--mcp-dir", default=DEFAULT_MCP_DIR,
                    help="判断条目归属的目录前缀（安装目录/mcp）")
    ap.add_argument("--remove", action="store_true", help="移除本增强包写入的条目")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Path(args.config)
    if not cfg.exists():
        tpl = Path(args.template) if args.template else None
        if tpl and tpl.is_file():
            shutil.copy2(tpl, cfg)
            print(f"  · config.yaml 不存在，已从上游模板生成：{tpl}")
        else:
            print(f"✗ config.yaml 不存在，且没有可用模板：{cfg}", file=sys.stderr)
            return 1

    servers = json.loads(Path(args.servers).read_text(encoding="utf-8"))
    data, dump, keeps_comments = load(cfg)
    if not keeps_comments:
        print("  ! 没有 ruamel.yaml，退回 PyYAML —— config.yaml 里的注释会丢失"
              "（已留 .bak 备份）")

    existing = data.get("mcp_servers")
    if existing is not None and not isinstance(existing, dict):
        print("✗ config.yaml 的 mcp_servers 不是映射，无法安全合并；请手工检查",
              file=sys.stderr)
        return 1
    mcp = existing or {}

    added: list[str] = []
    replaced: list[str] = []
    kept: list[str] = []
    removed: list[str] = []

    if args.remove:
        for s in servers:
            name = s["name"]
            if name in mcp and _owned(mcp[name], args.mcp_dir):
                del mcp[name]
                removed.append(name)
    else:
        for s in servers:
            name = s["name"]
            entry: dict = {"command": s["command"]}
            if s.get("args"):
                entry["args"] = list(s["args"])
            if s.get("env"):
                entry["env"] = dict(s["env"])
            entry["timeout"] = int(s.get("timeout", 120))
            if name in mcp:
                if _owned(mcp[name], args.mcp_dir):
                    mcp[name] = entry
                    replaced.append(name)
                else:
                    kept.append(name)
                    continue
            else:
                mcp[name] = entry
                added.append(name)

    if added or replaced or removed:
        data["mcp_servers"] = mcp
        if args.dry_run:
            print(f"  · dry-run：将写入 {cfg}（新增 {len(added)} / 更新 {len(replaced)}"
                  f" / 移除 {len(removed)}）")
        else:
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = cfg.with_suffix(cfg.suffix + f".bak-{ts}")
            shutil.copy2(cfg, backup)
            with cfg.open("w", encoding="utf-8") as fh:
                dump(data, fh)
            print(f"  ✓ 已写入 {cfg}（备份：{backup.name}）")
    else:
        print("  · 无需改动 config.yaml")

    if added:
        print(f"      新增：{', '.join(sorted(added))}")
    if replaced:
        print(f"      更新：{', '.join(sorted(replaced))}")
    if removed:
        print(f"      移除：{', '.join(sorted(removed))}")
    if kept:
        print(f"  ! 跳过（同名但不是本增强包写的，没动它）：{', '.join(sorted(kept))}")

    # 汇总给 install-extras.sh 看
    print("RESULT " + json.dumps(
        {"added": added, "replaced": replaced, "kept": kept,
         "removed": removed, "comments_preserved": keeps_comments},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
