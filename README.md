# Benchmark Lab

独立、可复现的抓取 API 实测基准。v1 回答一个问题：把 Firecrawl / ZenRows / Bright Data / Zyte 给 AI agent 当抓取工具时，**token 开销、反爬首次成功率、延迟与每千次成本**各是多少。完整方法论见 `SPEC.md`。

## 目录

- `SPEC.md` — 规格与护栏（唯一方法论依据）
- `configs/providers.json` — 四家等价调用的固定配置
- `configs/detection.json` — 拦截页检测规则
- `corpus/corpus.json` — 冻结的 20 个美国页面
- `runner/bench.py` — 单线程限速 runner（stdlib，tiktoken 可选；含熔断）
- `runs/` — 每次运行的原始响应与汇总（gitignored，本地归档）
- `metrics/` — 入公开 Git 的指标摘要与响应体 SHA-256（不含响应体）

## 复跑步骤

1. 复制 `.env.example` 为 `.env`，填入四家 key（账号注册由本人完成）。
2. 干跑验证配置（不发请求）：
   ```
   python3 runner/bench.py --round r1 --dry-run
   ```
3. **探路扣费**：先跑 1 静态 + 1 反爬两页（索引按 corpus 实际编号），核对各厂商后台真实 credit 倍率，再定单轮安全样本数：
   ```
   python3 runner/bench.py --round probe --indices 1,5
   ```
4. 正式跑一轮（单线程，间隔 2 秒）：
   ```
   python3 runner/bench.py --round r1
   ```
5. 跨时段至少跑 3 轮（r1/r2/r3），结果在各自 `runs/<run-id>/summary.json`；可公开的去响应体副本在 `metrics/<run-id>/`。

## 安全与红线

- 单线程、无无头浏览器、每调用 120s 超时、调用间隔 ≥2s；只请求配置内的厂商端点。
- 连续 3 次配额/网络/拦截失败自动熔断中止；停止：建 `STOP` 文件或设 `BENCH_KILL=1`。
- `.env` 不入库；结果不写 key；不新开公网端口、不影响 VPN。
- 公开仓库只放 URL、harness、指标 JSON 与 SHA-256，原始响应体仅本地留存。
- 报告用 Workload-Fit 场景矩阵，不写综合排名、不贬损；免费档结论不外推付费档。
