# JoinLab · 双表关联预览与执行

先选关联字段与关联方式，在页面上看清**键的唯一性、未匹配记录、预计结果行数**，
确认之后再执行；结果里每一行都能追溯回两侧原始行。

纯 Python 标准库 + 原生前端，没有第三方依赖。

## 快速开始

```bash
python3 run.py            # 打开 http://127.0.0.1:8765/
python3 -m unittest discover -s tests   # 后端 40 个测试
node tests/frontend_harness.js          # 前端交互（Node DOM 垫片，无需浏览器）
```

命令行与页面共用同一套规则：

```bash
python3 cli.py preview --config sample.json
python3 cli.py execute --config sample.json --out outputs/result.csv
```

## 规则是怎么算的

**行数按实际键频次计算。** 对每个两侧都存在的键 `k`，产出
`freq_left[k] × freq_right[k]` 行；预览里的“预计结果行数”与执行写出的行数
来自同一份 `build_side` / `iter_join_rows` 实现，不会出现预览一套、执行一套。

- **一对一**：键在两侧各出现一次，不放大行数。
- **一对多**：至少一侧键重复。
- **多对多**：两侧键都重复，行数成倍膨胀。页面会列出膨胀最多的键，
  点击可展开**两侧各自的具体记录**（行号 + 原值），而不是只给一个总体重复率。

**空值匹配由页面逐字段选择。** 默认“空值视为未匹配”：键中含空值的行不参与
匹配，全部计入未匹配，并在未匹配数旁单独标出其中多少行是空值导致的。
选择“参与匹配”后，两侧空值才会互相配对。

**复合键按字段组合比较。** 键是带类型标记的字段元组
（`[("v","A|B"),("v","C")]`），不是字符串拼接，所以
`region="A|B", code="C"` 与 `region="A", code="B|C"` 不会碰撞。

**去重与次级字段可以在预览里反复试。** 勾选“按关联键去重”或追加次级字段后
再次预览，对比表会列出结果行数、匹配行数、两侧未匹配、重复键与去重丢弃行的
变化，确认收益后再执行。去重只按你选择的键，**绝不按金额**——示例数据里
`south/C` 有两笔合法订单金额都是 300，按金额去重会误删有效记录。

**上限不会静默丢行。** 完整结果超过上限时，执行只返回受限预览、不写任何文件，
并明确告知完整规模；只有你显式同意截断，才会写出前 N 行，状态标记为
“已截断写出”，审计记录里 `truncated=true`。

## 追溯

输出 CSV 前三列是 `_left_row`、`_right_row`、`_key`：前两者是两侧原始文件的
数据行号（从 1 开始，不含表头），可反查原始记录；`_key` 是命中的键值。
两侧同名列会加 `left.` / `right.` 前缀。

## 审计

每次执行都在 `audit/` 写一份 JSON，包含：

- 两侧文件的绝对路径与 SHA-256、行数、列名；
- 关联方式、关联字段、次级字段；
- 逐字段空值策略、去重模式与丢弃行数；
- 行数计算规则、输出行数上限、是否截断；
- 输出文件路径与 SHA-256。

页面“执行记录”可查看历史运行，CLI 与 HTTP 执行都会记录。

## 目录

```
app/engine.py   关联引擎（键、频次、行迭代、上限、键展开）
app/config.py   预览/执行共用的配置解析
app/server.py   HTTP API 与静态文件
app/audit.py    执行审计
static/         页面
data/           示例数据（orders / payments）
tests/          后端测试 + 前端交互测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/datasets` | 示例数据集与列 |
| POST | `/api/preview` | 预览统计 |
| POST | `/api/key-detail` | 展开某个键两侧记录 |
| POST | `/api/execute` | 执行（需 `confirmed: true`） |
| GET | `/api/runs` | 执行记录列表 |
| GET | `/api/runs/{id}` | 单次记录详情 |

请求体示例见 `sample.json`；执行时可加 `output_name`、`max_output_rows`、
`allow_truncate`。
