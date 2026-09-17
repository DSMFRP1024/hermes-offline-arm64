# Hermes Agent 离线安装包（ARM64）

这是一个**自包含**的离线安装包。目标机不需要任何网络、不需要 gcc/make/npm/git。

---

## 三步装完

```bash
cd hermes-offline-arm64

bash check-env.sh        # 1. 只读预检（建议先跑，能提前发现 glibc/磁盘/缺失系统库）
sudo bash install.sh     # 2. 一键安装
hermes doctor            # 3. 自检
```

装完之后**必须配置一个模型**才能工作 —— Hermes 本身只是个壳：

```bash
vi /root/.hermes/.env     # 填入模型 API key / 内网端点
# 或者走交互式向导：
hermes setup
```

然后：

```bash
hermes                    # 启动交互式会话
hermes --help             # 看全部命令
hermes dashboard          # 启动 Web 控制台 → 浏览器打开 http://127.0.0.1:9119
```

`hermes dashboard` 开箱即用：前端已预编译在包里，安装脚本也补写了构建戳，
所以它**不会**去跑 `npm install`。万一提示要重建前端，加 `--skip-build`
即可直接服务包内的 dist。

---

## 安装脚本参数

```bash
sudo bash install.sh
    --dir /opt/hermes        安装目录（默认 root: /usr/local/lib/hermes-agent）
    --hermes-home /data/h    数据目录（默认 ~/.hermes，root 即 /root/.hermes）
    --force                  重建虚拟环境 / 重新解压运行时 / 重铺 node_modules
    --skip-browser           不铺 Chromium（浏览器工具不可用，其余不受影响）
    --no-verify              跳过 MANIFEST.sha256 校验
    --uninstall              卸载（保留数据目录）
```

非 root 用户跑时会自动落到 `~/.hermes/hermes-agent` + `~/.local/bin`，
不碰系统目录。

---

## 装完之后

| 目录 / 命令 | 说明 |
|---|---|
| `~/.hermes/` | 数据目录：配置、会话、日志、技能、缓存 |
| `~/.hermes/.env` | **模型 API key 等敏感配置（必填）** |
| `~/.hermes/config.yaml` | 主配置（首装时从上游模板生成） |
| `~/.hermes/logs/` | 运行日志，出问题先看这里 |
| `hermes doctor` | 自检，能覆盖大部分环境问题 |
| `hermes` | 启动 |

---

## 这个包里有什么

```
install.sh / check-env.sh / README.md / docs/
runtime/         CPython 3.11 独立运行时 + Node.js（linux-arm64）
repo/            hermes-agent 源码（按 commit 固定的 tar.gz）
                 └─ 内含 hermes_cli/web_dist/ —— 预编译的 dashboard 前端
wheels/          全部 aarch64 wheel（含预构建的 sdist-only 包）
wheels-prebuilt/ 本地构建出来的补充 wheel
node_modules/    预构建 node_modules（含 node-pty 原生模块，目标机无需编译器）
browsers/        Playwright Chromium（linux-arm64）
bin/             ripgrep / ffmpeg / uv
lib/             fts5_cjk.so（中文分词检索加速）
requirements.lock.txt  精确依赖锁（完整闭包）
build-info.json       构建元信息（目标平台、glibc 基线、上游 commit）
MANIFEST.sha256       完整性校验清单
```

---

## 离线环境的预期行为（不是 bug）

1. **`hermes update` 不能用** —— 拉不到代码。升级请重新构建离线包后
   在目标机重跑 `install.sh --force`。
2. **首次使用某些功能会报"装不上"** —— Hermes 部分后端（某些 TTS/STT、
   搜索、消息平台）是首次使用时才联网安装的。离线机上会以
   `ModuleNotFoundError` 或安装失败体现。需要在构建时就包含。
3. **`uv` 虽然装了但装不了任何东西** —— 放它是为了让 Hermes 的托管 uv
   检测路径短路，不去尝试联网下载 uv 本身。
4. **目标机上不要指望重新编译任何东西** —— Node/npm 虽然都铺好了，
   但 npm registry 不可达。dashboard 前端已经预编译并配了构建戳；
   万一它仍判定"需要重建"（比如你改动了 `web/` 下的源码），
   用 `hermes dashboard --skip-build` 直接服务包内 dist。

---

## 出问题时

先看这两处：

```bash
cat ~/.hermes/logs/*.log | tail -100
hermes doctor
```

再查 `docs/故障排查.md`，里面按"症状 → 原因 → 处置"编排，覆盖了
glibc 不匹配、原生扩展 import 失败、Chromium 缺系统库、中文检索退化等
常见情况。
