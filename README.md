# Hermes Agent 离线安装包构建器（Linux ARM64 / 信创）

把 [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) 那一条

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

**换成一个可以在零网络机器上跑的离线包**：自带 Python 运行时、全部
aarch64 wheel、Node.js、预构建 node_modules、Playwright Chromium、
ripgrep/ffmpeg、编译好的原生扩展，以及**预编译好的 dashboard 前端**
（Web 控制台开箱即用，目标机不需要 npm）。目标机只要解压 + 一条 `install.sh`。

---

## 目标场景

| 维度 | 取值 |
|---|---|
| 目标架构 | `aarch64` / `arm64`（飞腾、鲲鹏、倚天等 ARMv8） |
| 目标系统 | Kylin V10、UOS 20、openEuler 20.03+ 及衍生发行版 |
| 目标 glibc | 默认 **2.28**；`--glibc 2.17` 可适配更老 |
| 目标 Python | **不用系统 Python**，自带 CPython 3.11 独立运行时 |
| 目标机不需要 | gcc、make、npm、git、Docker、任何网络 |

---

## 一条命令构建

构建跑在 **GitHub Actions 的 arm64 runner + manylinux 容器**里
（公开仓库免费且不限额度）：

```
Actions → Build Hermes Agent Offline Bundle (ARM64) → Run workflow
```

跑完下载 artifact `hermes-offline-arm64-tarball`，就是一个单文件
`hermes-offline-arm64.tar.gz`，适合拷进内网。

命令行触发 / 取回（`gh` 的 `run download` 没有超时，国内走代理容易无限
挂起，所以用自带的 `gh_run.py` 下载，带超时 + 断点续传 + sha256 校验）：

```bash
gh workflow run build-offline-bundle.yml -f glibc=2.28 -f node_line=24
python build/gh_run.py watch    --repo <你>/hermes-offline-arm64 --run <RUN_ID>
python build/gh_run.py arts     --repo <你>/hermes-offline-arm64 --run <RUN_ID>
python build/gh_run.py download --repo <你>/hermes-offline-arm64 \
       --artifact <ART_ID> --out dist
python build/gh_run.py verify   --tarball dist/hermes-offline-arm64.tar.gz
```

`verify` 会在解压前就地把关：关键条目是否齐全、有没有混进非 aarch64 的
wheel、有没有 glibc 基线高于目标机的 wheel —— 后者正是"构建全绿、上机
`GLIBC_2.39 not found`"那类事故的唯一可检出面。

> **为什么必须是 arm64 runner + manylinux 容器这两层？**
>
> runner 本体是 `ubuntu-24.04-arm`，它的 glibc 是 **2.39**。直接在它上面编译
> 原生扩展，产物会链接到 2.39，拷到 glibc 2.28 的信创机就是
> `GLIBC_2.39 not found` —— 而且这种包在构建日志里**完全看不出问题**。
> 所以构建必须再套一层 `quay.io/pypa/manylinux_2_28_aarch64` 容器。
>
> 一句话：**runner 提供架构，容器提供 glibc 基线。**

---

## 在离线机上安装

把 `hermes-offline-arm64/` 整个目录（或解压后的同名目录）拷过去，然后：

```bash
cd hermes-offline-arm64
bash check-env.sh          # 只读预检，强烈建议先跑
sudo bash install.sh       # 一键安装
```

装完：

```bash
hermes doctor              # 自检
vi /root/.hermes/.env      # 填模型 API key（必做）
hermes                     # 命令行交互
hermes dashboard           # Web 控制台 → http://127.0.0.1:9119
```

`hermes dashboard` 直接可用：前端已经预编译在包里，`install.sh` 也补写了
构建戳，所以它**不会**去跑 `npm install`。如果哪次真提示要重建，加
`--skip-build` 直接服务包内 dist 即可。

常用参数：

```bash
sudo bash install.sh --dir /opt/hermes --hermes-home /data/hermes
sudo bash install.sh --skip-browser      # 不铺 Chromium
sudo bash install.sh --force             # 重建 venv / 重解压运行时
sudo bash install.sh --uninstall         # 卸载（保留数据目录）
```

---

## 目录结构

```
offline-deploy/
├── .github/workflows/build-offline-bundle.yml   CI 流水线（arm64 runner + manylinux 容器）
├── build/
│   ├── build_bundle.py        构建器主程序（原生模式）
│   ├── ci-entry.sh            容器内入口（CI 调用）
│   ├── test_native_mode.py    参数不变量测试（19 项断言，CI 前置）
│   ├── test_web_ui.py         dashboard 前端预编译不变量（23 项断言，CI 前置）
│   ├── check_undefined.py     零依赖 AST 未定义名检查（CI 前置）
│   ├── test_workflow.py       工作流自检：YAML / run 块语法 / inputs 引用 / pipefail
│   ├── gh_run.py              查运行 / 下载 artifact / 校验离线包（多分片+续传）
│   ├── test_download.py       下载器自测（并发分片 / 续传 / 拼接，本地 Range 服务）
│   ├── push_api.py            用 Git Data API 推送（github.com 的 git push 走不通时）
│   ├── test_push_api.py       push_api 的 blob 批量解析逐字节自测
│   └── requirements.in        参考用
├── target/                    ↓ 这些文件会被打进离线包
│   ├── install.sh             目标机一键安装
│   ├── check-env.sh           目标机只读预检
│   └── README.md              包内说明（装完要看的）
├── docs/
│   ├── 部署方案.md            完整设计、取舍与坑位说明
│   └── 故障排查.md            症状 → 原因 → 处置
└── dist/                      构建产物（自动生成）
    ├── hermes-offline-arm64/          ← 自包含的离线包目录
    ├── hermes-offline-arm64.tar.gz    ← 单文件形式（CI artifact 用的就是它）
    └── logs/                          构建日志 + requirements.lock.txt
```

离线包内容（`hermes-offline-arm64/`，自包含）：

```
hermes-offline-arm64/
├── install.sh / check-env.sh / README.md
├── docs/
├── runtime/           CPython 3.11 独立运行时 + Node.js linux-arm64
├── repo/              hermes-agent 源码（tar.gz，按 commit 固定）
│                      └─ 内含 hermes_cli/web_dist/ —— 预编译的 dashboard 前端
├── wheels/            全部 aarch64 manylinux wheel
├── wheels-prebuilt/   本地预构建的 sdist-only 包
├── node_modules/      预构建的 node_modules（含 node-pty 原生模块）
├── browsers/          Playwright Chromium（linux-arm64）
├── bin/               ripgrep / ffmpeg / uv
├── lib/               fts5_cjk.so（中文分词检索加速）
├── requirements.lock.txt / requirements.universal.txt
├── build-info.json
└── MANIFEST.sha256
```

---

## 四个关键设计决定

### 1. 依赖清单不重新求解，而是从仓库自带的 `uv.lock` 导出

`uv.lock` 里记着每个包**所有平台** wheel 的 URL + sha256 + 体积，
比任何二次求解都可靠。用 `uv export --frozen` 在**原生 arm64** 上导出，
PEP 508 标记天然按 `linux/aarch64` 求值，不存在"在 Windows 上解析出
`pywin32`"那类交叉求值事故。

### 2. 原生模式是唯一模式，且必须跑在 manylinux 容器里

因为平台本来就是对的，所以**不需要** `--platform` / `--python-version` /
marker 劫持那一整套交叉 hack —— 那是"在错误的平台上伪装成正确的平台"，
而这里我们就在正确的平台上。`test_native_mode.py` 用捕获真实命令行的方式
把这条不变量锁住：一旦有人给 pip 加上 `--platform`，测试立刻红。

### 3. `node_modules` 必须预构建

`node-pty` **没有 Linux 预编译包**，每次安装都要 `node-gyp` 编译，
而信创机通常没有 `make`/`gcc`。所以在 arm64 容器里编好再打包，
目标机只是解压 —— 彻底消灭编译器和构建工具链依赖。

### 4. dashboard 前端也必须预构建

上游 `hermes dashboard` 的启动路径是：找 `hermes_cli/web_dist` →
找不到就现编（`npm install --prefer-offline` + `npm run build`）→
编不出来就 `sys.exit(1)`。

在零网络机器上，`npm install` 取不到 registry，**这条路径必挂**，
症状是"装好了、命令也在，但控制台打不开"。所以构建时就把前端编好
（`npm run build --workspace web`，vite 的 `outDir` 正是
`../hermes_cli/web_dist`），产物随源码快照一起入包。

只放 dist 还不够：上游判"要不要重建"时还会比对
`$HERMES_HOME/web-ui-build-stamp.json` 里的内容哈希，而那个戳**不在仓库里**。
所以 `install.sh` 装完会用 venv 里的 Python 按当前源码树算一次并写戳 ——
这样裸跑 `hermes dashboard` 就判定"无需重建"，直接起服务。

`build/test_web_ui.py` 把这条链钉死，其中最要紧的一条是：
**`build_web_ui()` 必须排在 `pack_repo()` 之前** —— 排在后面，
产物就进不了源码快照，等于白编。

---

## 本地能验证什么

不需要 arm64 机器，以下检查都能在本机跑：

```bash
python build/check_undefined.py build/build_bundle.py build/test_native_mode.py \
       build/gh_run.py build/test_workflow.py build/push_api.py \
       build/test_push_api.py build/test_download.py build/test_web_ui.py
python build/test_native_mode.py
python build/test_workflow.py
python build/test_push_api.py
python build/test_download.py
python build/test_web_ui.py
bash -n build/ci-entry.sh target/install.sh target/check-env.sh
```

这些正是 CI 在拉镜像之前跑的前置门。想跑完整构建，`--native` 会在
非 aarch64 宿主上直接拒绝并给修复指引，不会静默产出一个平台标签全错的包。

`test_workflow.py` 专门盯"YAML 合法、shell 合法、CI 一路绿灯但行为是错的"
那一类问题：`run` 块里的引号/heredoc 写错、引用了没声明的
`github.event.inputs.*`（会静默取到空串）、以及 `set -o pipefail` 下
`cmd | head` / `cmd | grep -q` 这类末段提前退出的管道（会让整条管道判失败，
或在 `if !` 里把条件静默反转）。

---

## 已知限制

1. **`hermes update` 不可用**。离线环境下拉不到代码，这是预期行为。
   升级要重新构建离线包并在目标机重跑 `install.sh`（`--force`）。
2. **lazy-install 类的功能不可用**。Hermes 有些后端（各 TTS/STT/搜索/
   消息平台）是首次使用时才联网安装的，离线机上会失败。需要什么就在
   构建时的 `[all]` extra 里包含。
3. **Browser Use CLI 默认不打包**。它是 Playwright 之外的另一套浏览器
   后端（`uv tool install browser-use`，依赖树很重）。Hermes 会回退到
   内置浏览器工具 + 包内的 Chromium。需要它的话见 `docs/部署方案.md`。
4. **cua-driver（Computer Use）默认不打包**。它靠一个 curl 管道脚本从
   GitHub 装，离线机上装不了。
5. **Chromium 运行时依赖系统库**。信创机默认常缺 `libnss3`/`libgbm`/
   `libasound2` 等；`check-env.sh` 会逐个列出缺哪个，装系统包即可
   （不影响核心 CLI）。
