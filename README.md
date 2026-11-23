# Benchmark Lab

独立、可复现的抓取 API 实测基准。v1 回答一个问题：把 Firecrawl / ZenRows / Bright Data / Zyte 给 AI agent 当抓取工具时，**token 开销、反爬首次成功率、延迟与每千次成本**各是多少。完整方法论见 `SPEC.md`。

## 目录

- `SPEC.md` — 规格与护栏（唯一方法论依据）
- `configs/providers.json` — 四家等价调用的固定配置
- `configs/detection.json` — 拦截页检测规则
- `corpus/corpus.json` — 冻结的 20 个美国页面
- `runner/bench.py` — 单线程限速 runner（stdlib，tiktoken 可选）
- `runs/` — 每次运行的原始响应与汇总（gitignored）

## 复跑步骤

1. 复制 `.env.example` 为 `.env`，填入四家 key（账号注册由本人完成）。
2. 干跑验证配置（不发请求）：
   ```
   python3 runner/bench.py --round r1 --dry-run
   ```
3. 正式跑一轮（20 URL × 4 家，单线程，间隔 2 秒）：
   ```
   python3 runner/bench.py --round r1
   ```
4. 跨时段至少跑 3 轮（r1/r2/r3），结果在各自 `runs/<run-id>/summary.json`。

## 安全与红线

- 单线程、无无头浏览器、每调用 120s 超时、调用间隔 ≥2s；只请求配置内的厂商端点。
- 停止：在本目录建 `STOP` 文件，或设 `BENCH_KILL=1`，下一次调用前退出。
- `.env` 不入库；结果不写 key；不新开公网端口、不影响 VPN。
- 报告只呈现分项数据与区间，不写总排名；免费档结论不外推到付费档。
