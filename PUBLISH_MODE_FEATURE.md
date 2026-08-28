# 栏目发布模式功能说明

## 功能目标

每个栏目可在“创建草稿”和“直接发布”之间切换。配置保存在 SQLite 中，worker 每次首次提交文章前读取，因此保存后无需重启服务。

- `publish_mode=0`：创建懂球帝草稿。
- `publish_mode=1`：直接发布到懂球帝。
- 新栏目默认使用草稿模式。
- 已经首次提交过的文章保留第一次使用的模式，失败重试和 502 结果确认不会中途改成另一种模式。

## 配置优先级

实际提交模式按以下顺序解析：

1. 文章首次提交时保存的模式快照。
2. 来源明确设置的模式例外。
3. 文章关联栏目的模式。

来源通常选择“跟随栏目”。只有一个来源关联多个模式不同的栏目时，才需要统一栏目模式或明确设置来源例外。没有例外且栏目模式冲突时，文章进入 `MAPPING_BLOCKED`，不会提交到懂球帝。

## 数据结构

- `tabs.publish_mode`：栏目当前模式，默认 `0`。
- `sources.publish_mode_override`：来源例外；`NULL` 表示跟随栏目，`0/1` 表示强制模式。
- `articles.publish_mode`：文章第一次提交时保存的模式快照。
- `articles.publish_mode_decided_at`：快照保存时间。

数据库启动迁移会自动添加缺失字段。历史 source 级直接发布配置会保留为来源例外，避免升级后静默改变已有行为。

## 运行链路

```text
配置页切换栏目模式
  -> POST /api/tabs/<tab_id>
  -> SQLite 保存 tabs.publish_mode
  -> 页面局部更新栏目和受影响来源
  -> worker 处理 READY_TO_PUBLISH 文章
  -> 原子解析并保存 articles.publish_mode
  -> admin-archive-createarticle 请求携带 status=0/1
  -> 草稿成功写入 DRAFT_CREATED
  -> 直接发布成功写入 PUBLISHED
```

`api_name` 不需要切换，两种操作都继续调用 `admin-archive-createarticle`，仅表单 `status` 不同。

## 页面操作

1. 打开“栏目与来源”。
2. 在目标栏目右侧查看当前模式。
3. 点击切换按钮。
4. 切到“直接发布”前确认立即上线风险。
5. 保存成功后页面原地更新，不清空来源搜索或滚动位置。

文章详情中的人工提交按钮也会按有效模式显示：草稿模式显示“创建懂球帝草稿”，直接发布模式显示“直接发布到懂球帝”并给出明确警告。

## API

```http
POST /api/tabs/12
Content-Type: application/json

{"publish_mode": 1}
```

响应包含更新后的 `tab` 和 `affected_sources`，前端用它们局部刷新状态。

## 安全边界

- `status` 只接受整数 `0` 或 `1`，其他值在外部请求前被拒绝。
- 多栏目模式冲突时默认阻断，不猜测或自动选择直接发布。
- 配置切换只影响尚未首次提交的文章。
- 已产生 `archive_id` 的文章不会因为切换模式再次创建或发布。
- 直接发布成功使用独立终态 `PUBLISHED`，运行汇总单独显示发布数量。

## 验证

运行完整测试：

```bash
python3 -m pytest -q
```

重点覆盖栏目配置 API、`status=0/1` 表单、首次提交前即时切换、模式快照、失败重试、多栏目冲突、直接发布状态和人工操作警告。
