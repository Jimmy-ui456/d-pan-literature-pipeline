# Literature Search

> 成熟度：早期实验工具，适合试用、审查和贡献；尚无跨课题准确率基准，也不承诺自动完成可靠综述。本项目采用 MIT License，见 [LICENSE](LICENSE)。

这是一个可配置的微生物代谢工程文献证据管线。它把检索、去重、身份核验、PDF 身份比对、证据定位和人工审阅分开保存；候选记录、自动分类或同句数字都不是科学结论。

## 安装

需要 Python 3.10 或更高版本。在发行根目录创建独立环境后安装：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

查看命令和运行测试：

```powershell
python -m litsearch --help
python -m unittest discover -v
```

```sh
python -m litsearch --help
python -m unittest discover -v
```

内置课题为 `pantothenate`、`lysine` 和 `glutathione`。它们是词表和筛选协议，不代表覆盖所有文献。

## 默认离线演示

以下命令只处理仓库内的手写合成输入，并主动阻断程序运行范围内的 socket 联网；不检索网络、不读取或写入 Zotero，也不使用真实论文、PDF 或标识符。

```powershell
python scripts/run_synthetic_demo.py --out-dir .validation/demo-output
```

```sh
python scripts/run_synthetic_demo.py --out-dir .validation/demo-output
```

输入在 `examples/synthetic/records.json`。其中的数量文字仅用于测试候选提取，不能作为生物学、发酵或产量事实。演示输出会保留未核验身份、待人工审核的记录；`review_ready.json` 必须为空。

## 联网检索

联网检索不是默认测试。请先查看参数，并准备有效联系邮箱、网络条件和对数据源限流的理解：

```powershell
python -m litsearch --help
python -m litsearch --topic pantothenate --source europepmc --out-dir outputs/pantothenate
```

```sh
python -m litsearch --help
python -m litsearch --topic pantothenate --source europepmc --out-dir outputs/pantothenate
```

若使用 PubMed 或 OpenAlex，必须提供有效 `--email`；OpenAlex 还需要 API key。对已有 JSON 的离线复审使用 `--from-json`，只有显式加入 `--verify-online` 才重新联网核实身份。

## 输出与人审

每次运行要求新的空输出目录。`run_manifest.json` 和 `events.jsonl` 记录运行参数、计数与异常；`all_screened_records.json`、CSV/XLSX 和 `review_decisions.template.json` 用于检查候选；`review_ready.json`、RIS 和 review materials 只包含通过身份、证据与人工审核门禁的条目。输出中的“产量核对状态”不会自动归属产量或建立结论。

人工审核者应阅读证据卡和原文，填写与该课题指纹和记录指纹匹配的审阅 JSON，再以 `--review-decisions` 重新运行。没有人工决定，或身份/PDF/证据条件不满足时，条目不会进入可用于综述的集合。

## 可选 Zotero 工具

`litsearch_zotero` 是本机辅助扩展，核心包不依赖它。先只查看帮助：

```powershell
python -m litsearch_zotero.attach_pdfs --help
python -m litsearch_zotero.audit_missing_pdfs --help
```

附件工具默认是 dry-run，但仍可能读取本机 API；只有显式 `--apply` 才会写入。对真实 Zotero 库操作前，应先审阅 dry-run 输出并备份数据。

限制、架构和参与方式分别见 [docs/limitations.md](docs/limitations.md)、[docs/architecture.md](docs/architecture.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)。本项目采用 [MIT License](LICENSE)，版权署名为 Jimmy-ui456；历史候选文本与第三方资料边界见 [许可证状态](LICENSE-STATUS.md)。

## 工程主题与Word报告预检

新增独立 `engineering-review` / `python -m litsearch.engineering` 入口，支持湿法刻蚀分面计划、Crossref候选检索及引用/条件预检。它使用独立工程配置，不改变旧微生物筛选和审批规则。运行流程、输入格式和边界见 [工程检索与预检说明](docs/ENGINEERING_REVIEW.md)。

## 发布状态与反馈

公开准备的核查范围和未完成能力见 [发布准备说明](docs/RELEASE_READINESS.md)。遇到问题请通过仓库 Issues 提交运行环境、命令、错误和脱敏后的最小示例；不要提交论文全文、私有研究数据或 API 密钥。
