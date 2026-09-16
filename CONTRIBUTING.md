# 参与贡献

请在发行根目录使用 Python 3.10+ 的虚拟环境安装项目，再运行：

```powershell
python -m pip install .
python -m unittest discover -v
python scripts/run_synthetic_demo.py --out-dir .validation/contributor-demo
```

POSIX 环境可使用相同的 `python` 命令。提交变更前，新增功能应有能实际运行的测试，且不得把网络、真实 Zotero 或真实研究数据作为默认测试前提。

安全回归要求：不得弱化身份核验、PDF 身份、证据或人工审核门禁；不要把未核验缓存、合成输入或自动提取的数字标记为可用于综述；附件工具必须保持默认 dry-run 和显式 `--apply` 才写入的行为。请避免提交 API key、本机绝对路径、PDF、数据库、缓存和生成输出。
