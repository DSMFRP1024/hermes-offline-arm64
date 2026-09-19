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
# 本地静态门
python3 build/check_undefined.py build/build_extras.py build/test_extras.py
python3 build/test_extras.py
python3 build/test_workflow.py

# 出包（GitHub Actions）
gh workflow run build-extras.yml -f lean=false
python3 build/gh_run.py watch    --repo <owner>/<repo> --run <RUN_ID>
python3 build/gh_run.py download --repo <owner>/<repo> --artifact <ID> --out dist --parts 6
```

构建分三步，缺一不可：

1. **`manylinux_2_28_aarch64` 容器里下轮子** —— 容器同时提供 glibc 2.28 基线和
   cp311，所以 pip 求值出来的就是信创机要的那一份。**只用 arm64 runner 会产出
   链接到 glibc 2.39 的轮子**，装机报 `GLIBC_2.39 not found`，而构建日志全绿。
2. **arm64 runner 上原生 `npm install`** —— 目标机零网络，依赖树必须预装好。
3. **打包** —— 生成 lock、MANIFEST.sha256、build-info.json，再打 tar.gz。
