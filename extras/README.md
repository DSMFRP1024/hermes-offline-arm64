# Hermes 离线增强包（ARM64）

给**已经装好 Hermes Agent 主包**的离线信创机补上日常要用的东西。
面向「日常办公为主 + 轻度数据 / 轻度编程 + PPT 制作 + 修图 + 剪辑」。

全程不需要网络。

---

## 一、这个包装了什么

| 类别 | 内容 | 装到哪 |
|---|---|---|
| **Python 包** | Word / Excel / PPT / PDF 处理、图像、视频、数据图表、编程工具，约 110 个包 | hermes 自己的 venv |
| **MCP 服务器** | 9 个：Office 三件套 + 时间 / Git / SQLite + 文件系统 / 知识记忆 / 分步推理 | 独立 MCP venv + 预装 node_modules |
| **Skill** | 25 个官方可选技能（PPT 生成、Excel 生成、看板、手绘图、白板、数据 notebook…） | `~/.hermes/skills/` |

**没有下载任何东西进这个包的部分**：技能是从主包自带的
`<安装目录>/optional-skills/`（151 个官方可选技能）里就地启用的。
主包其实一直带着它们，只是默认不激活。

### 具体清单

**办公文档**：python-docx、docxtpl（模板套打）、openpyxl、xlsxwriter、xlrd（老式 .xls）、
python-pptx、odfpy（ODF/ODS，永中/WPS 系）

**PDF**：pypdf、pdfplumber、reportlab、pypdfium2、PyMuPDF、img2pdf、msoffcrypto-tool（解密加密 Office）

**中文办公零碎**：pypinyin、opencc（简繁转换）、segno / qrcode（二维码）、tabulate、
ruamel.yaml（改配置时保住注释）

**图像**：piexif（EXIF）；Pillow + pillow-heif 主包已带；重物组另含 opencv-python-headless、scikit-image

**视频**：moviepy、imageio-ffmpeg、ffmpeg-python、pysubs2（字幕）、av；
ffmpeg 本体在主包的 `bin/ffmpeg`

**数据**：numpy、pandas、matplotlib、plotly + kaleido；重物组另含 scipy、seaborn

**编程**：pytest、ruff、lxml、beautifulsoup4、html5lib、markdownify、tqdm；
重物组另含 ipython、jupyterlab、black

---

## 二、快速开始

把 `hermes-extras-offline-arm64.tar.gz` 拷到目标机，然后：

```bash
tar -xzf hermes-extras-offline-arm64.tar.gz
cd hermes-extras-offline-arm64

bash install-extras.sh --dry-run     # 先看一眼会做什么
sudo bash install-extras.sh          # 真的装
```

装完直接：

```bash
hermes                 # 进 CLI，MCP 工具会自动加载
hermes dashboard       # 或者开浏览器控制台
```

安装脚本会自己找到主包装在哪（读 `~/.hermes/.offline-install`），
找不到再用 `--install-dir` 指定。

### 常用参数

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打印会做什么，不动任何东西 |
| `--fs-root DIR` | filesystem MCP 允许访问的目录（可重复）。默认取存在的 `~/Documents`、`~/Desktop`、`~/Downloads`，再加 `~/.hermes/workspace` |
| `--skip-python` / `--skip-mcp` / `--skip-skills` | 只装其中一部分 |
| `--no-shims` | 不往 `~/.hermes/bin` 放 python3/pip 垫片 |
| `--no-keep-wheels` | 不把 wheelhouse 留在安装目录（省几百 MB，但就没法离线 `pip install` 了） |
| `--force` | 重装 Python 包与 MCP venv |
| `--no-verify` | 跳过装完的验证 |

---

## 三、它会动你机器上的哪些东西

全部列出来，方便你核对：

| 位置 | 动作 |
|---|---|
| `<安装目录>/venv` | 装 Python 包（**不覆盖已有依赖的版本**，除非确实冲突） |
| `<安装目录>/mcp/venv` | 新建，独立的 MCP venv |
| `<安装目录>/mcp/node` | 新建，Node 侧 MCP 服务器 |
| `<安装目录>/mcp/servers.json` | 新建，MCP 清单 |
| `<安装目录>/wheelhouse-extras` | 新建，离线 wheelhouse |
| `<安装目录>/.extras-installed` | 新建，安装记录 |
| `~/.hermes/config.yaml` | **先备份**再改，只动 `mcp_servers` 一节；同名但不是本包装的条目**绝不覆盖** |
| `~/.hermes/skills/` | 新增 25 个技能目录（已存在的不动） |
| `~/.hermes/bin/` | 新增 python3 / python / pip / pip3 四个垫片 |
| `~/.hermes/workspace` | 新建 |

配置备份形如 `config.yaml.bak-20260919T120000Z`，随时可以拿来对照。

---

## 四、装完怎么用

### MCP 工具

工具名规则是 `mcp_<服务器名>_<工具名>`：

| 服务器 | 典型工具 |
|---|---|
| `mcp_office_pptx_*` | 建幻灯片、加页面、插入形状/图表/图片 |
| `mcp_office_word_*` | 建/改 Word、加表格、页眉页脚 |
| `mcp_office_excel_*` | 读写单元格、建工作表、公式 |
| `mcp_filesystem_*` | 在限定的目录内读写文件 |
| `mcp_sqlite_*` | 查询 / 建表 / 描述表结构 |
| `mcp_git_*` | 看历史、diff、状态 |
| `mcp_time_*` | 当前时间、时区换算 |
| `mcp_memory_*` | 知识图谱式记忆（落在 `~/.hermes/mcp/memory.json`） |
| `mcp_thinking_*` | 结构化分步推理 |

启动时每个服务器都会拉一个子进程。**嫌启动慢就注释掉 `~/.hermes/config.yaml`
里 `mcp_servers` 下对应的条目**，剩下的照常工作，不会互相影响。

改完配置要重启 `hermes`（MCP 没有热重载）。

### 技能

```bash
<安装目录>/venv/bin/python <安装目录>/hermes skills list
```

想再启用别的官方可选技能（本地还有 120 多个）：

```bash
<安装目录>/venv/bin/python <安装目录>/hermes skills browse --source official
<安装目录>/venv/bin/python <安装目录>/hermes skills install official/<分类>/<名字> --yes
```

### 离线装更多 Python 包

如果这个包里有你要的轮子：

```bash
pip install <包名>                                   # 垫片会自动走本地 wheelhouse
hermes …        # 垫片只在 hermes 启动的会话里生效，不影响系统 PATH
```

想联网时：

```bash
HERMES_PIP_ONLINE=1 pip install <包名>
```

### 在 Python 脚本里用

`python3` 垫片已经指向 hermes 的 venv，所以：

```bash
python3 -c "import docx, openpyxl, pptx, fitz, pandas; print('ok')"
```

---

## 五、怎么确认装对了

安装脚本最后会自动做这些，全部通过才算好：

1. **关键依赖逐个 import** —— 原生扩展在 glibc 不匹配时只会在 import 期炸
2. **依赖版本对比** —— 打印「已有依赖的版本被改动了」的清单（新装的不算），
   一个都没被改动是最好的结果
3. **MCP 真实握手** —— 对每个服务器真发一次 `initialize` + `tools/list`，
   拿到工具数量才算通。这一步专门防「配置写进去了、服务器其实起不来」的静默失败

手动复查：

```bash
# MCP 是否都能起来
~/.hermes/../lib/hermes-agent/venv/bin/python verify-mcp.py \
    --servers <安装目录>/mcp/servers.json

# 关键包
python3 -c "import docx, openpyxl, pptx, pypdf, fitz, PIL, pandas, matplotlib"
```

---

## 六、常见问题

**Q：装完 `hermes` 起不来了？**
脚本会在第 8 步报「hermes 核心依赖 import 失败」。这是 extras 与 hermes 共用 venv
的理论风险。恢复办法：

```bash
sudo bash <主包目录>/install.sh --force    # 重建 venv，再重跑 install-extras.sh
```

装之前的依赖快照留在 `<安装目录>/venv/.extras-prev-freeze.txt`，装完的在
`.extras-post-freeze.txt`，可以直接 diff 看是谁动了什么。

**Q：某个 MCP 服务器握手失败？**
不影响 hermes 本体，只是那个服务器的工具用不了。常见原因：
- node 侧：`<安装目录>/mcp/node/node_modules` 不完整
- python 侧：MCP venv 里缺包

**Q：为什么 MCP 要用一个单独的 venv，不能跟 hermes 共用？**
因为版本硬冲突：hermes 自带 `mcp==2.0.0`，而 office 系列依赖的 `fastmcp 2.x`
要求 `mcp<2`。装在一起会把 hermes 的原生 MCP 客户端顶掉 —— 而且
`hermes doctor` 不会报错。独立 venv 之后两边各自收敛，互不干扰。

**Q：为什么包里没有 markitdown？**
上游的 `read_file` 已经内置了 `firecrawl-anydoc`，能自动把
docx / xlsx / pptx / PDF 抽成文本。再引 markitdown 功能重复，还会连带拖进
`magika → onnxruntime`（主包刻意排除的重物）。

**Q：磁盘不够。**
- `--no-keep-wheels` 省几百 MB
- 用精简版增强包（构建时 `lean=true`），不含 opencv / scipy / jupyterlab

---

## 七、卸载

```bash
# 1. 去掉 MCP 配置
<安装目录>/venv/bin/python merge-mcp-config.py \
    --config ~/.hermes/config.yaml \
    --servers <安装目录>/mcp/servers.json \
    --mcp-dir <安装目录>/mcp --remove

# 2. 删掉 MCP venv / node / wheelhouse
rm -rf <安装目录>/mcp <安装目录>/wheelhouse-extras <安装目录>/.extras-installed

# 3. 删掉垫片（想留 python3 指向 hermes venv 的话就别删）
rm -f ~/.hermes/bin/{python3,python,pip,pip3}

# 4. 技能：逐个删，或干脆不用管
rm -rf ~/.hermes/skills/<分类>/<名字>
```

Python 包没法干净撤回（它们和 hermes 的依赖混在一个 venv 里）。
要彻底回到装之前：

```bash
sudo bash <主包目录>/install.sh --force
```

---

## 八、构建（维护者看）

```bash
# 本地静态门（都在拉镜像之前跑，秒级）
python3 build/check_undefined.py build/build_extras.py build/test_extras.py \
    build/check_undefined.py build/test_workflow.py build/smoke_pack.py \
    build/verify_extras_tarball.py
python3 build/test_extras.py      # 161 项不变量（含 argparse 字段门 + 反向验证）
python3 build/smoke_pack.py       # pack 端到端冒烟（合成最小树，不需要网络）
python3 build/test_workflow.py    # 需要 PyYAML

# 产物侧体检（CI 打完包就跑；本地下载回来也跑一遍，同一份规则）
python3 build/verify_extras_tarball.py dist/hermes-extras-offline-arm64.tar.gz

# 出包（GitHub Actions）
gh workflow run build-extras.yml -f lean=false
python3 build/gh_run.py watch    --repo <owner>/<repo> --run <RUN_ID>
python3 build/gh_run.py arts     --repo <owner>/<repo> --run <RUN_ID>
python3 build/gh_run.py download --repo <owner>/<repo> --artifact <ID> --out dist --parts 6
```

`smoke_pack.py` 是**必须保留**的一道：静态分析能查「引用了不存在的名字」，
却查不出「某个子命令少一个选项、而 `main()` 无条件读它」——这种写法
argparse 一声不吭，直到真跑到那一步才 `AttributeError`。
真实案例：`pack` 阶段读 `args.index`（`--index` 只挂在 `wheels` 上），
前面六步全绿，白烧一轮十几分钟的 CI。

`verify_extras_tarball.py` 是**产物侧的唯一裁判**，流式读 tar、不解包。
它拦过两个真 bug —— 两个都是 `stage_pack()` 的语义/顺序问题，
而 `smoke_pack.py` 当时**拦不住**（因为它只看"跑通没跑通"，不看清单语义）：

| bug | 现象 | 根因 |
|---|---|---|
| MANIFEST 登记了符号链接 | 体检同时报「sha256 对不上」+「文件缺失」，各 4 个（`node_modules/.bin/mcp-server-*`、`node-which`） | `Path.is_file()` 会**跟随**链接，于是按"目标内容"给链接算了哈希；tar 存的是 symlink 条目，读不到内容。目标机 `sha256sum -c` 会报可疑告警 |
| build-info.json 没登记 | 「1 个文件没登记，例如 `['build-info.json']`」 | 它排在 MANIFEST **之后**生成，自己进不了清单 |

修法：MANIFEST 只登记普通文件（符号链接跳过 —— 链接目标本来就是普通文件，
各自已入册，**不损失覆盖**），且 `build-info.json` 先写、MANIFEST 后写。
符号链接本身由体检单独把关：必须是相对路径、不悬空、目标已入册。
运行时也不依赖那些垫片 —— `install-extras.sh` 是用 `node <dist/index.js>`
起 Node MCP 服务器的，走不到 `.bin/`。

体检脚本自己的规则也踩过一个坑：第一版用裸子串找 `pip install` 判"是否联网"，
把 `log_ok "... 离线 pip install 可直接取"` 这句**提示语**误判成联网动作。
现在只认真正的调用行（含 `-m pip`，或行首是 `pip`/`pip3`），
并且把 `\` 续行接成逻辑行后再判（`--no-index` 常写在续行上）。

**这个冒烟必须与 CI 同形**——第二轮的教训（CI run 35415356313）就是栽在这：
`stage_pack()` 里只写了 `dest = Path(args.out)`，而 `resolve_node_entry()`
返回的是 `.resolve()` 过的**绝对**路径；CI 传的 `--out` 却是相对的
（`dist/hermes-extras-offline-arm64`），于是

```
ValueError: '.../mcp-node/node_modules/@modelcontextprotocol/.../index.js'
            is not in the subpath of 'dist/hermes-extras-offline-arm64/mcp-node'
```

第一版冒烟之所以漏过，是因为它**传的是绝对路径、而且没传 `--node-src`**，
`resolve_node_entry` + `relative_to` 那条路径一次都没被执行。
所以现在 `smoke_pack.py` 硬性做到这些（`test_extras.py` 逐条守住）：

1. 造真的 `node_modules`（`bin` 指向**嵌套**的 `dist/index.js`，专门踩 `relative_to`），
   并造出 `.bin/` 里的**符号链接**（npm 真实产物形态）；
2. **「全相对（＝CI 同形）/ 全绝对」两种传参各跑一遍**，都必须通过；
3. 断言产物结构：`mcp-servers.json` 的 node 段、入口文件、版本、MANIFEST 路径，
   并直接开 tar 比对「MANIFEST 登记的每个条目在包里都是**普通文件**」；
4. 三处**反向验证**，各自都要能证明"规则是活的"：
   - 拿掉三处 `.resolve()`（植回 bug #2）→ 必须失败，且原因必须是 `relative_to`；
   - 手工构造坏树/好树（`TarInfo(type=SYMTYPE)`，**与宿主无关**）→
     体检必须报「MANIFEST 登记了符号链接」，且不误判好树；
   - 拿掉「清单跳过符号链接」（植回 bug #3）→ 体检必须报出来。

> 为什么符号链接那条要用**手工构造**的 tar 成员：Windows 非管理员执行
> `os.symlink` 会 `WinError 1314`，靠宿主建链接会让这条测试在本机**静默变成假通过**。
> 生成本机建不了链接时，冒烟会打印一行 `! …降级…`，由 CI 的 Linux runner 覆盖全长；
> 但上面第 4 条里的两个"体检反向验证"是与宿主无关的，**任何平台都真跑**。

写测试时的通用教训：**夹具的传参形态要跟 CI 一致**（相对/绝对、是否传可选参数），
否则"通过"只说明你没测到那条路。凡是"只在某个分支才走到"的代码，
冒烟里都要有东西逼它走进去。更进一步：**别让夹具依赖宿主的特权**
（符号链接、执行位、权限位），否则"通过"会在本机变成谎话，只在 CI 才现原形。

构建分三步，缺一不可：

1. **`manylinux_2_28_aarch64` 容器里下轮子** —— 容器同时提供 glibc 2.28 基线和
   cp311，所以 pip 求值出来的就是信创机要的那一份。**只用 arm64 runner 会产出
   链接到 glibc 2.39 的轮子**，装机报 `GLIBC_2.39 not found`，而构建日志全绿。
2. **arm64 runner 上原生 `npm install`** —— 目标机零网络，依赖树必须预装好。
3. **打包** —— 生成 lock、build-info.json、MANIFEST.sha256，再打 tar.gz
   （顺序有讲究：build-info 必须在 MANIFEST 之前，否则它自己进不了清单）。

### 触发与取构建日志（国内网络）

两个和构建逻辑无关、但每次都要绊一下的点：

```bash
# ① `gh workflow run` 会先查默认分支（走 graphql），代理一抖就 502。
#    绕开 graphql，直接调 REST 分发接口，ref 显式给：
HTTPS_PROXY= https_proxy= HTTP_PROXY= http_proxy= \
  gh api -X POST repos/<owner>/<repo>/actions/workflows/build-extras.yml/dispatches \
  -f ref=main -f inputs[lean]=false

# ② `gh run view --log-failed` 要连 results-receiver.actions.githubusercontent.com，
#    那个域基本不通；`gh api .../jobs/<id>/logs` 也常因 DNS 直接失败。
#    自己带重试拉，并且**跳转时摘掉 Authorization**（否则 Azure blob 回 401）：
#      取日志的小脚本见本仓库 gh_run.py 的 _NoAuthOnHostChange 写法
gh api repos/<owner>/<repo>/actions/runs/<RUN_ID>/jobs    # 先拿 job id
gh api repos/<owner>/<repo>/actions/jobs/<JOB_ID>/logs    # 再取该 job 的原始日志
```

`HTTPS_PROXY=` 置空不是笔误：`urllib`/`gh`/`requests` 读环境变量时**跳过空值**，
所以空串等价于「走直连」。实测沙箱代理对 github 常返 `502 Bad Gateway`，
同一时刻直连 `api.github.com` 是 `200 / 0.7s` —— **见到成片 502 先切直连**，
别干等。注意跨主机 302 到 `*.blob.core.windows.net` 时必须摘掉 `Authorization`。
