#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""push_api 的 blob 批量解析自测（零外部依赖，只需要 git）。

为什么值得单独测：

    `git cat-file --batch` 的输出是「头行 + 原始字节 + 一个换行」拼成的流，
    push_api 用位置游标顺序切分。游标一旦算错，从那个点开始的**每个** blob
    都会错位 —— 而且错位不一定报错，可能只是"推上去的文件内容不对"，
    甚至因为长度恰好对得上而推出一棵结构正确、内容微妙的树。
    这是整个推送路径上最难靠肉眼发现的一处。

    所以这里不复用 push_api 的解析结果做自检，而是拿 git 自己的索引当
    唯一真相，逐文件交叉验证。判定式是 git 的对象哈希定义：

        blob_sha1 = sha1(b"blob <字节数>\\x00" + 内容)

    重算结果必须等于索引里记的 sha。等价于"我们取到的字节与 git 里的字节
    完全相同" —— 比比对长度强得多。

用法：python build/test_push_api.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push_api  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, ok: str, bad: str) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + (ok if cond else bad))
    if not cond:
        FAIL.append(bad)
    return cond


def index_shas(repo: str) -> dict[str, str]:
    raw = push_api.git(repo, "ls-files", "-s", "-z", binary=True)
    out: dict[str, str] = {}
    for rec in raw.split(b"\x00"):
        if not rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        _mode, sha, _stage = meta.decode("ascii").split()
        out[raw_path.decode("utf-8")] = sha
    return out


def main() -> int:
    repo = str(Path(__file__).resolve().parent.parent)
    print("── push_api blob 批量解析自测 ──")

    files = push_api.local_files(repo)
    check(bool(files), f"local_files() 取到 {len(files)} 个跟踪文件",
          "local_files() 返回空 —— 仓库没有跟踪文件？")
    if not files:
        return 1

    idx = index_shas(repo)
    check(len(files) == len(idx),
          f"条目数与 git 索引一致（{len(files)}）",
          f"条目数不一致：local_files {len(files)} / 索引 {len(idx)}")

    bad: list[str] = []
    for path, mode, blob in files:
        exp = idx.get(path)
        if exp is None:
            bad.append(f"{path}（索引里没有这个路径）")
            continue
        got = hashlib.sha1(b"blob %d\x00" % len(blob) + blob).hexdigest()
        if got != exp:
            bad.append(f"{path}: 重算 {got[:12]} != 索引 {exp[:12]}")
    check(not bad,
          f"每个 blob 的字节都与 git 对象完全一致（重算 sha1 全中）",
          f"{len(bad)} 个文件字节不符（游标错位）：" + "; ".join(bad[:5]))

    # 模式位：可执行位丢失会让目标机上的 *.sh 不能直接跑
    modes = {m for _, m, _ in files}
    check(modes <= {"100644", "100755", "120000"},
          f"模式位正常（出现：{sorted(modes)}）",
          f"出现异常模式位：{sorted(modes - {'100644', '100755', '120000'})}")

    # head_info 一次调用要能同时给出 commit sha 与 root tree sha
    head_sha, tree_sha, msg = push_api.head_info(repo)
    r_head = push_api.git(repo, "rev-parse", "HEAD").strip()
    r_tree = push_api.git(repo, "rev-parse", "HEAD^{tree}").strip()
    check(head_sha == r_head and tree_sha == r_tree,
          f"head_info 与 rev-parse 一致（{head_sha[:12]} / tree {tree_sha[:12]}）",
          f"head_info 不符：{head_sha[:12]},{tree_sha[:12]} vs {r_head[:12]},{r_tree[:12]}")
    check(bool(msg.strip()), f"提交信息非空（{len(msg)} 字符）", "提交信息为空")

    # 路径区分非 ASCII：否则 GitHub 上会多出 `"docs` 这种引号目录
    nonascii = [p for p in idx if any(ord(c) > 127 for c in p)]
    check(all('"' not in p and "\\" not in p for p in idx),
          f"路径未被 core.quotePath 转义（含 {len(nonascii)} 个非 ASCII 路径）",
          "路径里出现引号或反斜杠 —— ls-files 没用 -z")

    print()
    if FAIL:
        print(f"❌ push_api 自测失败（{len(FAIL)} 项）")
        return 1
    print("✅ push_api 自测通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
