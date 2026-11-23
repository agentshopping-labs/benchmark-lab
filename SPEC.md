# Benchmark Lab v1 规格（抓取/SERP API × MCP 实测基准）

> 日期：2026-11-23｜状态：用户已批准开工
> 战略定位：方向2（联盟）与方向4（独立实测媒体）合并为**单一主引擎**。实测内容是「进店理由」，联盟链接是「收银台」；先有可核验的跑分，再填联盟链接。
> 本规格是 runner 实现与报告产出的唯一依据；改动本文件须在文件末尾记录。

## 1. v1 目标与边界

**目标**：用同一组美国真实网页，对四家抓取 API 的免费档做可复现实测，回答一个具体问题——**把这些 API 给 AI agent / MCP 当抓取工具时，真实 token 开销、反爬首次成功率、延迟与每千次成本各是多少。**

**v1 不做**：
- 不写总排名、赢家、评分；只呈现分项原始数据与区间。
- 不把免费档结果外推到付费档；不做统计显著性推断（n=20，只描述）。
- 不引用厂商自评；不做需要登录/付费才能看到的内容。
- 不跑无头浏览器、不并发、不做语料之外的站点。

## 2. 评测对象与等价调用定义

四家均提供「单 URL → markdown」的原生 HTTP 接口，全部使用免费档（$0 支出）：

| 厂商 | 请求 | 认证 | 取 markdown 的固定配置 | 响应字段 |
|---|---|---|---|---|
| Firecrawl | `POST https://api.firecrawl.dev/v2/scrape` | Bearer | body `{"url":URL,"formats":["markdown"]}`；`onlyMainContent` 默认 true（主内容） | `data.markdown` |
| ZenRows | `GET https://api.zenrows.com/v1/` | query apikey | params `url=URL&mode=auto&response_type=markdown`（无主内容开关，整页转换） | 响应体即 markdown |
| Bright Data | `POST https://api.brightdata.com/datasets/v3/scrape` | Bearer | body `{"input":[{"url":URL}],"custom_output_fields":"url\|markdown"}`（整页 markdown） | 记录数组中 `markdown` |
| Zyte | `POST https://api.zyte.com/v1/extract` | HTTP Basic（key 作用户名，空密码） | body `{"url":URL,"httpResponseBody":true,"markdown":true,"markdownOptions":{"mode":"mainContent"}}` | `markdown` |

**已知非等价点（如实写进报告，不做隐藏）**：Firecrawl/Zyte 取主内容，ZenRows/Bright Data 为整页转换；各家 markdown 转换器与反爬接入方式不同。所有配置逐字入仓（`configs/providers.json`），报告标注「结论仅对上述配置与测试档位有效」。

文档依据（实现时再逐一回读核对）：
- Firecrawl：https://docs.firecrawl.dev/api-reference/endpoint/scrape
- ZenRows：https://docs.zenrows.com/universal-scraper-api/features/markdown
- Bright Data：https://docs.brightdata.com/api-reference/scrapers/web-content-apis/fetch-api-collect-by-url
- Zyte：https://docs.zyte.com/zyte-api/usage/extract/

## 3. 测试语料（20 URL，冻结入仓）

`corpus/corpus.json`，每条含 `url`、`label`、`page_type`、`anti_bot`、`notes`。

- 构成：8 个电商产品页、6 个 SaaS 定价/产品页、6 个文档/内容页；其中至少 6 个标注为反爬页（Cloudflare/DataDome 等），至少 4 个轻量静态页作参照。
- 选择规则：美国主市场站点、公开可访问、无需登录、页面长期稳定；不含需要同意 cookie 才能进入的区域化跳转页。
- 语料冻结：v1 跑完前不更换；若某 URL 失效（404/改版），在日志记录并以同类型备选替换，不静默替换。
- 可复现附件：每次调用的原始 markdown 与响应元数据全部存档，保证第三方可复核。

## 4. 指标定义

1. **Markdown token 消耗**：用 `tiktoken`（编码 `cl100k_base`）对返回 markdown 计数；同时记录字符数与空白分词数作参照。tokenizer 版本/编码写入每次运行元数据。
2. **首次成功率**：每个 URL 每轮仅调用一次。成功 = HTTP 200 且 markdown 非空且通过拦截页检测。拦截页检测：标题/正文命中 challenge/captcha/access denied/verify you are human 等关键词，或 markdown 长度低于按页面类型设定的阈值；检测规则与阈值入仓。失败分类：HTTP 错误、空内容、拦截页、超时。
3. **端到端延迟（ms）**：客户端测量，含完整请求/响应；每调用超时上限 120s。每轮报告 P50/P95（n=20，注明 P95 近似最大值），并保留 min/max 与全部原始值。
4. **每 1,000 次成功抓取成本**：仅依据各厂商公开定价页（URL 入仓），明确计价假设（档位、是否含代理、成功才计费规则、月费/按量）；不假设未公开折扣；免费档期间该列为「基于公开定价的折算」，不是实际账单。

## 5. 四条护栏（强制）

1. **免费档披露**：每份报告显著位置写明测试档位、免费额度与测试日期，结论不外推。
2. **多样本不排名**：≥3 轮、跨时段运行；报告给区间与原始 JSON，不输出「谁第一」。
3. **可复现固定件**：语料、配置、tokenizer、原始响应、runner 版本全部入 Git；README 给出从零复跑命令。
4. **比较内容条款核查**：上线公开报告前逐家核联盟/API 条款对比较与贬损内容的规定（ZenRows 已明文无编辑控制权；其余三家逐一核查并记录出处），有禁止性规定的厂商以代号或仅展示其原始数据的方式处理。

## 6. 运行纪律（红线内）

- **单线程**串行；相邻调用间隔 ≥2 秒；单轮调用 ≤80 次（4 家 × 20 URL），三轮总量 ≤240 次。
- 无本地无头浏览器；runner 仅发出文档第 2 节定义的 HTTP 调用，无其他出站请求。
- 每调用超时 120s；整体运行设硬上限，异常即停并记录。
- Kill switch：检测到 `STOP` 文件或 `BENCH_KILL=1` 环境变量立即退出；不写任何系统目录、不新开公网端口。
- 密钥仅从 `.env`（`.env.example` 入仓）读取，`.env` 入 `.gitignore`；日志与结果中对 key 脱敏。
- 运行位置：LA（备源/工人机），轻量串行；也可先在 Mac Mini 干跑验证。不占带宽、不影响 8443/443 上的 VPN 服务。

## 7. 产物结构

```
benchmark-lab/
├── SPEC.md                 # 本文件
├── README.md               # 复跑指南
├── .env.example
├── configs/providers.json  # 第2节固定配置
├── corpus/corpus.json      # 第3节语料
├── runner/                 # 实现（单线程、限速、kill switch）
├── runs/<run-id>/          # 原始响应 markdown、元数据 JSON、汇总
└── reports/                # 面向人读的 markdown 报告（站点 /benchmarks/ 页的源稿）
```

后续阶段（不在 v1 实现内）：报告页部署到 `agentshopping.org/benchmarks/`（受控脚本 --yes）；跑分摘要接入 `/mcp`（新 intent 或 `match_workload` 数据）；联盟链接在各申请通过后回填。

## 8. 前置依赖（用户本人动作）

1. 注册四家免费档并把 key 填入 LA（或 Mac Mini）上的 `.env`：Firecrawl、ZenRows、Bright Data 产品侧（区别于联盟账号）、Zyte。
2. 联盟申请：Firecrawl（Dub，24–48h 审核）、ZenRows、Zyte；Bright Data 联盟审核结果待邮件。
3. 公开报告上线前的部署批准；社区分发由用户本人发帖。

## 变更记录
- 2026-11-23 v1 初版：范围、等价调用、语料、指标、护栏、纪律一次定齐。
