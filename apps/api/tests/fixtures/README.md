# 合成 BIFF8 样例

本目录的 `.xls` 文件由 `xlwt==1.3.0` 和 `tests.helpers.corpus.legacy_xls_bytes`
生成，只含合成数据，无宏、外部链接或用户资料。测试只需要运行时已有的 xlrd，
无需安装样例生成器。

- `costs-general.xls`：成本表的八行重复产品布局；产品 A 金额 10、产品 B 金额 20，
  数字格式为 General。验证 MarkItDown 路由、真实 Parse／Chunk 和整数末尾零。
- `legacy-names.xls`：表头“姓名／数量”和记录“张三／3”；验证 AnyDoc SDK 的真实 BIFF8 能力。

重现内容时，在临时目录调用生成器，分别传入 `test_spreadsheet_ingestion.py` 中
`_COST_ROWS` 的八行，以及 `("姓名", "数量"), ("张三", 3)` 两行。
