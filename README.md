# Automatic Post

独立的文章素材质检、审核与开放平台草稿创建系统。不修改或运行旧项目。

## 当前能力

- 在界面中维护栏目以及 `source -> tabs` 多栏目映射
- 按接口文档分页拉取已翻译素材
- 使用精确来源标识去重；KBS 按 `ncd` 合并 PC/移动 URL，其他来源仍使用 `source + source_url`
- 检查标题、正文完整性、广告和脏内容
- 可选 LLM 标题自动补全，异常统一进入人工审核
- 人工通过、驳回、修正后通过
- 待发队列、文章详情、运行记录和完整状态时间线
- 可选接入懂球帝开放平台，自动把待发文章创建为后台草稿

发布 worker 未启用时，最终状态为 `READY_TO_PUBLISH`；启用后会进入 `PUBLISHING`，成功后变为 `DRAFT_CREATED` 并保存 `dqd_archive_id`。正式发布接口仍需后续单独接入。

## 处理流程

```text
已启用 source -> 素材接口 -> 本地去重入库 -> 自动质检
                                     |-> 待人工审核 -> 通过/修正/驳回
                                     `-> 待发队列 -> 开放平台创建草稿
```

首次使用时，先在“栏目与来源”中为 source 选择一个或多个 tab，再启用该 source。新文章入库时会保存当时的栏目快照；之后修改 source 配置，只影响新获取的文章。创建草稿时会提交文章快照中的全部 `tabs[]`。系统只请求已启用来源。标题或正文疑似不完整、含广告/脏内容，或者标题无法自动修正时，文章进入人工审核；其余文章进入待发队列。`channels` 当前仅校验和清洗 ID，不做语义增删，也不会反向决定栏目。

## ID 说明

- `articles.id`：新系统自己的文章主键，质检、审核和状态流转都使用它。
- `origin_key`：可稳定提取原始文章 ID 时的唯一键；当前 KBS 使用 `ncd`。
- `source + source_url`：无法提取稳定原始文章 ID 时的兼容唯一键。
- `upstream_archive_id`：素材接口的 `archive_id`；非零表示已存在懂球帝文章。
- `dqd_archive_id`：开放平台创建草稿成功后返回的懂球帝文章 ID。
- `dqd_source_id`：预留给后续后台素材映射，不参与当前草稿创建。

## 启动

```bash
cd "/Users/demo/Desktop/automatic/automatic post"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python run.py web
```

打开 <http://127.0.0.1:8890>。未配置素材接口密钥时，页面仍可正常使用，但“立即拉取”会显示配置缺失。

若要启用草稿创建，在 `.env` 填写 `DQD_OPEN_APPID`、`DQD_OPEN_APPSECRET`、`DQD_OPEN_ENNAME`，并设置 `AUTOMATIC_POST_PUBLISHER=true`。创建文章接口使用 `status=0`，只落后台草稿。

当开放平台返回 5xx、请求超时/断连，或成功响应缺少 `archive_id` 时，文章会进入 `DRAFT_CONFIRMING`。默认仅对 HTTP 502 在 5 秒后自动再次调用一次创建接口（`DQD_OPEN_502_RETRY_ENABLED=true`、`DQD_OPEN_502_RETRY_DELAY_SECONDS=5`）；第二次仍未拿到 `archive_id` 时停止继续重试。503、504、超时和缺少 `archive_id` 不会在非幂等模式下自动重发。由于创建接口未承诺幂等，502 单次重试仍存在第一次已成功、第二次又创建一份草稿的风险。只有在上游已经实现以 `client_request_id`（或配置的字段名）做幂等唯一约束后，才可设置 `DQD_OPEN_IDEMPOTENCY_ENABLED=true`；此时 worker 会用同一请求号按 15 秒、1 分钟、3 分钟、10 分钟、30 分钟自动确认。

执行单轮任务：

```bash
.venv/bin/python run.py once
```

运行测试：

```bash
.venv/bin/pytest -q
```

敏感信息只能保存在未跟踪的 `.env` 中，禁止写入源码、日志或测试快照。

当前 Web 服务没有鉴权，只绑定 `127.0.0.1`。不要改为公网监听，直到身份验证和权限控制完成。
