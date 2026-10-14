# 工程主题检索与报告预检

本入口根据湿法刻蚀三版对比新增，使用现有安全网络客户端、书目核验和CSV输出功能。它把检索计划、候选文献、可读材料和报告结论分开；不是自动发表综述的程序。

旧 `literature-search` 入口、微生物课题配置和人工审阅结构继续适用原流程。工程主题使用独立 `engineering-review` 或 `python -m litsearch.engineering`，不要向旧 `--topic` 传入 `wet_etch`。

## 执行流程

在文献检索项目目录运行，`python` 可替换为本机Python完整路径。每次输出目录须为空或不存在。

```powershell
# 1. 保存研究分面、论文检索式和官方来源检索清单，不联网
python -m litsearch.engineering plan --protocol wet_etch --out-dir outputs_engineering/plan_01

# 2. 分主题检索Crossref；每主题最多30条候选，明确报告预算截断
python -m litsearch.engineering search --protocol wet_etch --budget 30 --out-dir outputs_engineering/search_01

# 3. 候选仍须走现有DOI身份核查及人工相关性判断。按计划另查
# 高校设施、厂商和危害资料，保存实际可读材料并建立catalog。

# 4. 对Word草稿做引用、覆盖与数值预检；以下文件名为待准备输入
python -m litsearch.engineering audit --document draft.docx --catalog sources.json --out-dir outputs_engineering/audit_01

# 5. 加入逐段数据证据台账后重跑；输出仍不是科学批准
python -m litsearch.engineering audit --document draft.docx --catalog sources.json --claims checks.json --out-dir outputs_engineering/audit_02
```

返回码：0为所请求的机械任务完成；2为失败、硬问题或仍有复核项；3为候选预算截断。`plan` 的0不表示执行了检索，`audit` 的0不表示专业认可或全文全部核实。结果始终保留 `scientific_approval: false`。

## 已补的能力

| 能力 | 具体做法 | 边界 |
|---|---|---|
| 工程研究问题分解 | wet_etch配置含12个主题分面与8条官方来源入口 | 主题出现不等于论述充分，配置可继续调整 |
| 工程论文召回 | Crossref通用query，显式score相关度排序，按offset分批；每主题预算不超过1000 | 不穷尽数据库，不自动引文追踪；候选不等于纳入文献 |
| 请求是否真正执行 | 校验响应query回显、批次和计数，保存原始响应、失败与截断 | 无关结果仍需相关性判断；接口异常不静默变为成功 |
| 官方与论文资料统一登记 | research/review/facility/vendor/institution/hazard/bibliography独立类型与访问层级 | 目前为清单导入和预检，官方页面检索及可读正文仍需人工或浏览工具完成 |
| 期刊错配拦截 | 原身份核验入口新增Crossref全期刊名称检查，不静默覆盖旧记录 | 未解决的期刊缩写、曾用名需显式核对；缺少远端期刊字段时无法比较 |
| 引用预检 | 检查未定义编号、重复编号、未引用条目和DOI/URL同源重复 | 简短参考文献缺期刊会提示补全，不判其为伪造论文 |
| 工程数据台账 | 捕捉nm/min、Å/cycle、温度、产能、比例等数值线索；要求材料、配方、条件与量测字段 | 词法提示不保证找齐所有数字，也不理解因果或指标定义 |
| 数据与原材料绑定 | Word文件、段落、材料快照哈希；验证引文、来源URL和定位信息 | 用户声明的来源关联仍须专业确认；哈希不证明材料本身真实 |

本次实测先发现深游标会返回与主题无关的排序结果，故最终改用显式score排序的有界offset检索，并增加回归测试。钨主题3条在线候选含Pacco论文的期刊版和SSRN版；两个DOI保留各自身份，不把预印本当作另一项独立实验。第一轮失败/不合格实测保留在回归工作区，未覆盖。

## 来源目录格式

`sources.json` 使用 `schema_version: 1` 和 `sources` 数组。每项字段：

- `id`：唯一字符串；不要求等于Word参考文献编号。
- `citation`、`journal`、`doi`、`urls`：完整书目、实际期刊、可选DOI和可核对URL。
- `kind`：research / review / facility / vendor / institution / hazard / bibliography。
- `access_level`：full_text / official_document / abstract / excerpt / bibliographic / unconfirmed。
- `audit_note`：实际读到哪些内容、适用范围和未确认事项。
- 可选 `journal_aliases`：已核对的书目缩写；不使用模糊匹配自动改名。

身份匹配基于DOI或完整URL；保留URL查询参数，避免把不同目录年份合并。目录由用户或AI审核记录导入，离线预检不再联网核实其中所有声明。须保存它的建立依据；最终报告提交前应重做有时效性的书目核查。

## 逐段数据台账格式

`checks.json` 顶层包含 `schema_version: 1`、当前Word的 `document_sha256` 和 `checks` 数组。每条check包含：

- `paragraph_sha256`：预检结果中对应段落的哈希。
- `source_ids`：当前段引用且在目录登记的来源；单条check当前支持一项来源快照。
- `source_url`、`snapshot_path`、`snapshot_sha256`、`locator`、`quote`：实际读取的UTF-8文本材料、精确引用和位置。相对快照路径按台账所在目录解析；不能把生成的总结充当原文。
- `context`：以下字段均填写字符串；缺失明确写 `not_reported`，会保留复核提示。

`target_material, protected_material, composition, chemistry, temperature, duration, geometry, metric, value, unit, measurement, uncertainty, limitations`。

参考实验的温度、材料比例、保护层、晶面、测量口径、产品版本与产能测试条件都应保留。EPC和REPC不是自动可互换的单位。8 Å与0.8 nm的等价转换、选择比重算、跨句数据归属等仍需显式人工记录；本版不会自动推断。

## 如何使用三版回归结果

回归工作区在 `综述写作/文献检索增强_湿法回归`，`replay_wet_etch.py` 从上一轮核查目录构造49项目录，用四份真实Word原文件重跑。它复用已经完成的AI核查，不冒称新一轮全文验证。

`regression/comparison.json` 和四份 `*_preflight.json` 可复现：Codex版13与16的期刊问题、网页及Deep版的同源引用、合并稿的书目结构与主题来源覆盖。所有文档仍报告需要结构化证据台账的数字段落；56组人工/AI核查表没有被自动冒充为已完成的逐段机器批准。

## 尚未完成

OCR及表格语义识别、跨句条件归属、补充材料内容核查、论文逻辑支持关系、引文追踪召回、自动网页正文提取、自动生成并核对完整综述，仍不能由本次小规模增强替代。报告生成器必须把预检设为交付前步骤；本次没有重写或重新执行历史Word生成脚本。
