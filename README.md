# MinerU PDF 转 Markdown

用 Python 将本地 PDF 交给 MinerU 云端解析，整理得到 Markdown 与图片；支持批量转换、图形界面、Zotero 附件选择和 Obsidian 导入。PDF 会上传到 MinerU，请仅在接受云端处理时使用。默认不修改源文件或 Zotero 数据，Obsidian 导入需要用户确认。

## 安装

需要 Python 3.13 或更新版本、网络连接、MinerU API Key；图形界面还需要 Python 自带的 `tkinter`。不需要 Xcode 工程或编译桌面应用。进入本项目目录后执行：

```bash
python3 --version                        # 确认版本为 3.13 或更新
python3 -m venv .venv
source .venv/bin/activate                 # macOS / Linux
python -m pip install -e '.[test]'
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境，然后执行相同的 `python -m pip install -e '.[test]'`。如只需运行、不需要测试，可将安装命令改为 `python -m pip install -e .`。安装 Python 依赖可能需要网络。

在项目目录下通过不回显的提示输入自己的 Key：

```bash
mineru-converter init
mineru-converter doctor --json
```

`init` 在当前目录创建 `.env`，不会覆盖已有文件；`doctor` 只显示配置状态，不显示 Key。也可以设置环境变量 `MINERU_API_KEY`。`.env` 是本机私有配置，已经被 Git 忽略；不要把 Key 粘贴到聊天或提交到仓库。命令默认在当前工作目录读取 `.env`，因此运行时请切回创建配置的目录，或使用相应命令的 `--env-file` 指定配置文件。

## 图形界面：日常使用

```bash
mineru-converter gui
```

粘贴 PDF 路径或选择文件，点击“追加解析”，检查队列后选择批量或逐个转换。可移除队列中的项目，源 PDF 不会被删除；进度区显示上传、解析和整理状态。默认把结果放在当前目录的 `outputs/`，每个结果包含 Markdown 文件和同名 `_assets/` 图片目录。

从 Zotero 选择附件：点击“打开 Zotero”，在 Zotero 左侧选择文件夹，回到本程序点击“读取当前 Zotero 文件夹”，在选择器中勾选附件并加入队列。此功能依赖运行中的 Zotero 及其本地接口；Zotero 数据库仅以只读方式访问，不写入记录。Better BibTeX citation key 可用于命名结果。

导入 Obsidian：转换后选择 Vault、目标目录和已完成结果，查看预览与冲突提示后确认导入。发生覆盖时会先备份；导入会修改选定 Vault 中的目标文件，请核对预览。图形界面会在本机保存上次选择的输出和 Vault 路径。

## 命令行

```bash
mineru-converter convert '/path/to/paper.pdf'
mineru-converter convert '/path/to/scan.pdf' --ocr
mineru-converter batch '/path/to/folder'            # 批量处理，递归扫描
mineru-converter batch '/path/to/folder' --serial   # 逐个处理
mineru-converter paste --batch                       # 粘贴多个路径
mineru-converter convert '/path/to/paper.pdf' --dry-run --json
```

`paste` 支持换行、引号、中文路径和 `file://`。默认模型为 `vlm`，公式和表格识别开启，OCR 关闭；扫描件可加 `--ocr`。单批最多 50 个文件，程序会对部分临时网络错误重试。完整参数可通过 `mineru-converter --help` 和各子命令的 `--help` 查看。

Obsidian 导入默认只预览，确认目标、图片及冲突后再添加 `--apply`：

```bash
mineru-converter import 'outputs/example-hash' --vault '/path/to/vault' --target '论文笔记'
mineru-converter import 'outputs/example-hash' --vault '/path/to/vault' --target '论文笔记' --apply
```

## Python API

```python
from pathlib import Path
from mineru_converter import MineruClient, ParseOptions
from mineru_converter.config import get_api_key
from mineru_converter.paths import build_source_records
from mineru_converter.pipeline import convert_records

records = build_source_records(['/path/to/paper.pdf'])
client = MineruClient(get_api_key())
results = convert_records(client, records, options=ParseOptions(language='en'), output_root=Path('outputs'))
```

## 开发与验证

```bash
python -m compileall -q src tests
python -m pytest
```

仓库只包含程序、测试和使用说明，不包含 API Key、PDF、转换结果或 Zotero/Obsidian 数据。测试使用本地模拟，不代表云端服务当前可用。首次使用图形界面时请先确认本机 `tkinter` 可以运行。
