# 来源抓取量预警

## 功能说明

每天北京时间 **11:00 与 19:00** 各推送一次，统计「上一个边界到这个边界」内各来源
的抓取量，与**昨日同一期**对比，检测异常后推送到飞书群。

两期首尾相接、覆盖完整一天：

| 期 | 统计区间（北京时间） | 时长 |
|---|---|---|
| 11:00 | 前一天 19:00 → 当天 11:00 | 16h |
| 19:00 | 当天 11:00 → 当天 19:00 | 8h |

环比基准是昨日同期而非昨日全天：8 小时的量去比 24 小时必然偏低，会让每个来源
每天都被误报成骤降。

## 文件结构

```
app/
├── db.py                        # source_report_deliveries、source_period_stats 两张表
├── repository.py                # 期统计 SQL、快照读写、投递幂等
└── services/
    ├── source_monitor.py        # 纯计算：环比、异常判定、汇总
    └── source_report.py         # 期计算、文案、发送、进程内调度器

scripts/
└── run_daily_report.py          # 手动补发/排查入口
```

## 定时机制

定时由 Web 进程内的 `SourceReportScheduler` 负责：每 30 秒轮询一次，判断最近一个
边界是否已过且该期尚未发送。**不依赖 cron 或 launchd**，主应用起来就在跑。

> 早先用的是 crontab，但 macOS 的 cron 需要单独授予「完全磁盘访问权限」，实际
> 从未成功执行过——`logs/` 目录始终是空的，数据库里那两条快照都是手动跑出来的。
> 改到进程内调度后不再有这个坑，失败也会进 `source_report_deliveries` 留痕。

幂等由 `source_report_deliveries` 表保证，唯一键是 `(period_start, period_end)`，
所以同一期只会发一次，30 秒轮询不会刷群。发送失败进入 `FAILED` 并按
`SOURCE_REPORT_RETRY_SECONDS` 退避重试；结果未知（超时/连接中断）时保持
`SENDING` 且不再自动重试，避免重复发送。

手动补发或排查：

```bash
python run.py source-report
# 或
python3 scripts/run_daily_report.py
```

## 配置

全部通过 `.env` 提供：

```
SOURCE_REPORT_ENABLED=true
SOURCE_REPORT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
SOURCE_REPORT_BOUNDARIES=11:00,19:00
SOURCE_REPORT_TIMEOUT_SECONDS=10
SOURCE_REPORT_RETRY_SECONDS=300
SOURCE_REPORT_STALE_SECONDS=900
SOURCE_REPORT_CHECK_INTERVAL_SECONDS=30
```

`SOURCE_REPORT_BOUNDARIES` 同时决定发送时刻和统计区间的切分，改成
`08:00,14:00,20:00` 就是一天三期。写错的项会被丢弃并退回默认值，不会让应用起不来。

这个 webhook 与 `FEISHU_REPORT_WEBHOOK_URL`（直接发布统计）是**两个不同的机器人**，
两条链路各自独立，互不影响。

## 统计口径

- 窗口按 UTC 时间戳半开区间 `[start, end)` 比较。`articles.created_at` 存的是
  UTC，早先按 `DATE(created_at)` 匹配北京日期，实际统计的是北京 08:00→次日
  08:00，凌晨那批稿子被算进了前一天（实测一天漏 241 篇）。
- 「待处理」= `DRAFT_CREATED` + `NEEDS_REVIEW`。早先按 `DRAFT`/`REJECTED` 统计，
  而项目从不写这两个状态，所以那两行常年是 0。
- 启用中但 0 篇的来源仍会出现在统计里——抓取挂掉正是预警要抓的情况。
- 来源显示名有重复（三个来源都叫「韩媒」），文案里统一带上 code。

## 异常检测

门槛按 24 小时一期定标，再按实际期长等比缩放（16h 期 ×2/3、8h 期 ×1/3），
最低为 1。否则短期内小来源永远达不到门槛、彻底不报警。

| 级别 | 规则（括号内为 24h 基准值） |
|---|---|
| 🚨 严重 | 本期 0 篇且昨日同期 ≥ 10 篇；或骤降 > 80% 且昨日同期 ≥ 10 篇 |
| ⚠️ 警告 | 下降 50%–80% 且昨日同期 ≥ 5 篇 |
| 📈 信息 | 激增超过一倍且昨日同期 ≥ 5 篇；或新增来源 |

## 首次运行

第一期没有昨日同期快照，环比显示「新增」/「无昨日同期数据」。快照在发送前就会
落库，即使推送失败也不影响第二天的基准。
