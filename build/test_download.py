#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh_run.download_file 的多分片端到端自测（零外部依赖，本地起 Range 服务）。

为什么值得测：
    分片下载是并发代码，出错方式极隐蔽 —— 拼接顺序错、区间边界算错、
    续传偏移算错，都可能产出一个**长度正确但内容错乱**的文件。字节数校验
    根本抓不住，只有整文件 sha256 对比能抓住。所以这个测试在下载完成后
    必须比对源文件哈希。

    另外它同时覆盖三条容易退化的分支：
      · 分片临时文件是否清理干净（残留会让下次续传基于错误偏移）；
      · 预置残缺分片时能否正确续传；
      · 小于阈值（每片 4 MiB）的文件是否自动退回单连接。

用法：python build/test_download.py
"""

from __future__ import annotations

import hashlib
import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 本地回环绝不能走代理：环境里可能设了 HTTP_PROXY 指向某个代理，
# 而 ProxyHandler() 无参时会读环境变量，把 127.0.0.1 也代理走。
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import gh_run  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, ok: str, bad: str) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + (ok if cond else bad))
    if not cond:
        FAIL.append(bad)
    return cond


# 可复现的伪随机内容：不用 os.urandom，便于复现失败
DATA = bytes((i * 7 + (i >> 8) * 13) & 0xFF for i in range(16 * 1024 * 1024))
WANT = hashlib.sha256(DATA).hexdigest()


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """支持 Range 的最小静态服务；python -m http.server 不支持 Range。"""

    def log_message(self, *a):  # 静音访问日志
        pass

    def do_GET(self):
        total = len(DATA)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            start = int(s) if s else 0
            end = min(int(e) if e else total - 1, total - 1)
            body = DATA[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.end_headers()
            self.wfile.write(DATA)


def main() -> int:
    print("── 多分片下载自测 ──")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/blob"

    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)

            total, ranged = gh_run.probe_total(url, "tok", 10)
            check(total == len(DATA) and ranged,
                  f"probe_total 拿到长度与 Range 支持（{total} 字节）",
                  f"total={total} ranged={ranged}")

            dest = d / "full.bin"
            gh_run.download_file(url, dest, "tok", 10, parts=4)
            got = hashlib.sha256(dest.read_bytes()).hexdigest()
            check(dest.stat().st_size == len(DATA) and got == WANT,
                  f"4 分片并发下载结果与源逐字节相同（sha256 {got[:12]}…）",
                  f"size={dest.stat().st_size} sha={got[:16]} want={WANT[:16]}")

            left = sorted(p.name for p in d.glob("*.part*"))
            check(not left, "临时分片清理干净", f"残留 {left}")

            # 续传：预置一个完整分片 + 一个残缺分片
            span = -(-len(DATA) // 4)
            spans = [(i * span, min(len(DATA), (i + 1) * span) - 1)
                     for i in range(4)]
            (d / "resume.bin.part2").write_bytes(
                DATA[spans[2][0]:spans[2][1] + 1])
            (d / "resume.bin.part0").write_bytes(
                DATA[spans[0][0]:spans[0][0] + 5000])
            dest2 = d / "resume.bin"
            gh_run.download_file(url, dest2, "tok", 10, parts=4)
            got2 = hashlib.sha256(dest2.read_bytes()).hexdigest()
            check(got2 == WANT,
                  "残缺 part0 + 完整 part2 的续传结果正确",
                  f"sha={got2[:16]} want={WANT[:16]}")
            check(not list(d.glob("resume.bin.part*")),
                  "续传路径同样清理了临时分片", "有残留")

            # 小文件应自动退回单连接
            small = bytes(2 * 1024 * 1024)
            globals()["DATA"] = small
            dest3 = d / "small.bin"
            gh_run.download_file(url, dest3, "tok", 10, parts=8)
            check(dest3.read_bytes() == small
                  and not list(d.glob("small.bin.part*")),
                  "小文件自动退回单连接且结果正确",
                  f"size={dest3.stat().st_size}")
    finally:
        srv.shutdown()

    print()
    if FAIL:
        print(f"❌ 多分片下载自测失败（{len(FAIL)} 项）")
        return 1
    print("✅ 多分片下载自测通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
