# 微生物代谢工程文献检索与证据整理管线（v3）

> 本指南保留历史命令及其含义；当前唯一源码、安装、测试和发布入口是本仓库根目录，见 [README](../README.md) 和 [发布流程](RELEASING.md)。旧工作区的 `literature_pipeline.py` 只是转发器；在独立克隆中请使用 `python -m litsearch`。下文研究资料路径属于历史工作区，不随源码发行。

这是“找文献、核身份、筛相关性、整理可追溯证据”的工具，不是能自动保证结论正确的综述生成器。

最重要的变化：**找到文章 ≠ 确认是这篇文章 ≠ 与课题有关 ≠ 能支撑你的结论。** 这四件事现在分开记录。候选表不能直接当成综述参考文献表。

旧工作区入口 `literature_pipeline.py` 仅作转发，独立克隆使用 `python -m litsearch`；不需要 API key 的 Europe PMC 是默认检索源。OpenAlex 和 PubMed 可选。发行包名称暂留旧名称，避免破坏既有安装；功能不再局限于泛酸。

## 换课题怎么用

内置配置都在 `litsearch/profiles/`：

| 配置名 | 用途 |
|---|---|
| pantothenate | 泛酸、泛醇的微生物生产；中间体和酶学作为辅助 |
| lysine | 赖氨酸生产 |
| glutathione | 谷胱甘肽生产 |

复制一份配置到你自己的 JSON 文件，修改研究问题、目标物别名、通路、宿主、工程词和排除提示，然后用 `--topic 新配置.json`。配置严格校验，有指纹；旧课题的人工结论不能自动沿用到新课题。术语仅填词或短语，不要在其中写 AND/OR 检索表达式。

检索阶段采用较宽的目标物/通路词组合，不要求摘要里一定写出宿主。否则，一篇在正文才写明菌种的文章会被提前漏掉。规则按目标物、宿主/过程和工程信号排序，但最后仍要人工判断“生产对象究竟是不是我们的目标物”。排除词是提醒，不是一票否决。

## 安装与运行

需要 Python 3.10+；主要依赖为 openpyxl 和 pypdf。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 在线检索：默认不下载PDF，默认核实候选文献身份
.\.venv\Scripts\python.exe -m litsearch --topic lysine --source europepmc --search-max 500

# 用历史记录重新审查；此命令明确授权重新联网核验DOI/PMID
.\.venv\Scripts\python.exe -m litsearch --topic pantothenate --from-json outputs_core_final/pre_download_records.json --verify-online --pdf-dir zotero_pdf_staging

# 离线重跑：不发起检索/身份网络请求，只使用未过期核验缓存
.\.venv\Scripts\python.exe -m litsearch --topic pantothenate --from-json outputs_core_final/pre_download_records.json --skip-pdf-extraction

# 显式下载PDF：需自己的联系邮箱；只对身份核验通过的记录尝试下载
.\.venv\Scripts\python.exe -m litsearch --topic glutathione --download-pdfs --email your-real-email@example.org --max-pdfs 10
```

如果没有 `py`，用本机 Python 可执行文件的完整路径代替。上面的邮箱是示例，运行时换成自己的真实联系邮箱。

输出默认放在 `outputs_v3/<课题>/<UTC运行时间>/`。指定 `--out-dir` 时必须使用空目录；不会覆盖历史结果。JSON 导入的旧“已核验”“产量”“Results”“已批准”字段一律不作为证据复用，旧 `--reuse-derived-fields` 参数仅兼容解析、不再授信。

联网身份核验默认 3 并发、最多 4 并发，共享请求节流。可用 `--verification-workers 1` 串行运行。缓存默认 7 天有效；正式交稿前建议联网加 `--refresh-verification`，因为撤稿、更正及元数据会变化。

## 看哪些文件

| 文件 | 大白话用途 |
|---|---|
| topic_snapshot.json / run_manifest.json | 这次究竟搜什么、用什么参数、何时运行、是否截断或失败 |
| raw_records.json | 收到的候选原始元数据；数据源中途失败也尽量保留已收到的批次 |
| all_screened_records.json | 包含被排除者的完整筛选结果，便于查漏和重新审阅 |
| literature_candidates.csv / .xlsx | 候选阅读清单，不是“已证实的综述结论” |
| screening_decisions.csv | 规则筛选理由；“是否纳入”仅指是否进入候选阅读清单 |
| identity_audit.csv | 每篇 DOI/PMID 的核验结果、来源、时间和冲突理由 |
| evidence_cards.json | 原文句子、来源种类、文本指纹、位置、PDF指纹及数值线索 |
| review_decisions.template.json | 需要人填写的相关性和结论审核表 |
| review_ready.json / review_materials.md | 通过身份、人工相关性判断和证据门禁的写作材料 |
| review_references.ris | 仅导出审阅通过的书目；未审阅时为空，不把候选伪装成最终引用 |
| events.jsonl | 失败、截断、无法核验等事件，不悄悄隐藏 |

自动表不再填“强化某酶导致增产”之类猜测。数值同句出现也只算线索：例如“葡萄糖80 g/L、泛酸2 g/L”不能直接认成“泛酸80 g/L”。浓度/效价（titer）、底物得率（yield）、单位时间产率（productivity）不可混排为同一个“产量”。

## 文献真实性怎样核验

1. DOI/PMID 必须符合标识符格式；不从一段垃圾文字中截取“像 DOI 的字符串”。
2. 向 Crossref 精确 DOI 接口、Europe PMC DOI/PMID 查询核对标题、年份和作者；不靠相似标题拼凑 DOI。B5/B 5等排版差异会规范化；在线/纸版年份差异须由返回的公开发表日期解释，不做任意“±1年容差”。保留来源快照、时间和内容哈希。
3. 检查 Crossref 反向更新通知，以及 Europe PMC 返回的撤稿/更正信号。发现更新通知暂不放行，先读通知。
4. PDF 文件名、下载成功和 PDF 文件头都不是身份依据。还须在首页匹配完整题名和 DOI/PMID，并排除补充文件/参考文献误匹配。

| 状态 | 意义 |
|---|---|
| verified | 请求的标识符和核验元数据相符，已执行所支持的更新检查 |
| metadata_conflict | 标题/年份/作者/标识符有冲突；可能是错误记录，也可能是版本或排版差异，须查证 |
| retracted / publication_update | 发现撤稿或更新信号；不能直接作为正常证据使用 |
| verified_updates_unknown | 身份匹配，但更新检查不可用，写作门禁暂不放行 |
| unverified / not_indexed / invalid_identifier | 未核实、所查索引未收录或格式无效；不等于“证明是伪造论文” |

`verified` 只说明记录相符，不说明实验一定可靠，不保证撤稿索引没有延迟，也不自动证明全文内容、相关性或因果关系。Crossref 不覆盖所有 DOI 注册机构；没有 DOI/PMID 的旧论文仍须补充可靠书目身份。缓存哈希防止意外损坏，不是防恶意本地篡改的数字签名；核验缓存目录必须可信。

## 如何把候选变成综述材料

将模板另存为 `review_decisions.json`，不要覆盖输出模板。对读过的文章填写 reviewer、reason、decision（include/exclude）、role（primary/background/method）和 claims。每个 claim 必须引用本次证据卡的 ID。

例如一条人工填写的定性 claim 结构：

```json
{
  "kind": "qualitative",
  "text": "这里填写人工读原文后确认、且不超出证据的结论",
  "evidence_ids": ["从 evidence_cards.json 复制实际证据ID"]
}
```

这段是格式说明，不是可直接提交的真实审阅结果。定量 claim 要用 `kind: quantitative`，并在 context 中填写：product、host、strain、intervention、comparator、cultivation、medium、metric、value、unit、time、scale、limitations。缺失明确写 not_reported；不要猜。metric 只能是 titer/yield/productivity/conversion/other。代码不做跨条件的自动数值比较。

重跑时使用同一原始记录文件、同一课题和 PDF 库，传入 `--review-decisions review_decisions.json`。输出目录仍需换新。记录、课题或证据发生变化时旧审核会失效。人工可提出纳入规则漏筛的文章，程序会对其补做核验；没有证据卡时先输出新卡，再填结论重跑。综述文章只能以背景等用途使用，不能直接冒充原始增产实验。

人工同意也不能绕过身份冲突、失效证据或撤稿门禁。程序能够检验证据引用存在，不能替人证明一句结论在逻辑上必然由原文推出；专业阅读仍是必要环节。

## 边界与未完成能力

- 规则词表不是训练过的相关性模型，目前没有经专家标注的跨课题金标准；不能给出可信的召回率/准确率保证。
- Europe PMC 支持游标翻页；超过 `--search-max` 会明确标截断。OpenAlex 当前保留单页预算（最多200/查询），PubMed最多10000；都不是自动穷尽检索。
- 只提供已实现索引的核验；尚未增加 DataCite 专用适配和无标识符论文的人工书目核验流程。引用追踪通过独立 `literature-discover` 工具执行，结果仍是待核验候选。
- 主入口 PDF Results 仍是标题启发式，缺少可匹配首页的 PDF 会被保守拦住。新增独立解析工具可提取 PDF/HTML 表格与 OCR 文字；补充材料对齐、跨段条件归属和科学语义判断仍需复核，见 [工程工具说明](ENGINEERING_TOOLS.md)。
- 公开可访问的 PDF 链接不自动代表开放许可；默认不下载、不扫描落地页。不会绕过付费墙。
- 网络客户端连接经过检查的公网 IP，并对跳转重新检查；默认禁用系统代理，以免代理重新解析绕过检查。需要企业代理的环境目前未支持。
- 历史报告生成器、旧 Word/Excel 成品、Zotero 附件脚本没有变成通用综述入口；它们保留供历史任务使用，不能据此宣称旧报告已完成真实性审核。

## 验证与参考

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q litsearch tests
# 可选实连小样本，不是准确率基准
.\.venv\Scripts\python.exe -m tests.live_probe --output outputs_v3/my_live_probe
```

历史发现、开源参考和 v2 说明保留在原研究工作区，不随本源码仓库发行；当前能力与限制以 [README](../README.md) 为准。

## 工程主题与Word报告预检

新增独立 `engineering-review` / `python -m litsearch.engineering` 入口，支持湿法刻蚀分面计划、Crossref候选检索及引用/条件预检。它使用独立工程配置，不改变旧微生物筛选和审批规则。运行流程、输入格式和边界见 [工程检索与预检说明](ENGINEERING_REVIEW.md)。

## 独立工程工具

新增 `literature-service-probe`（显式联网服务验证）、`literature-discover`（同义词检索及引用追踪）和 `literature-parse`（复杂表格与 OCR）。网络工具默认只生成计划，解析工具读取显式指定的本地文件；安装依赖、参数、状态和合成演示见 [工程工具说明](ENGINEERING_TOOLS.md)。这些工具的维护入口是本仓库，不再维护工作区副本。
