# Automatic Post

独立的文章素材质检与待发管理系统。当前版本实现发布前流程，不修改或运行旧项目。

## 当前能力

- 在界面中维护栏目以及 `source -> tab` 唯一映射
- 按接口文档分页拉取已翻译素材
- 使用 `source + source_url` 去重，本地 `id` 驱动整个处理流程
- 检查标题、正文完整性、广告和脏内容
- 可选 LLM 标题自动补全，异常统一进入人工审核
- 人工通过、驳回、修正后通过
- 待发队列、文章详情、运行记录和完整状态时间线

最终状态为 `READY_TO_PUBLISH`。懂球帝草稿创建和正式发布将在下一阶段接入。

## 处理流程

```text
已启用 source -> 素材接口 -> 本地去重入库 -> 自动质检
                                     |-> 待人工审核 -> 通过/修正/驳回
                                     `-> 待发队列
```

首次使用时，先在“栏目与来源”中为 source 选择一个 tab，再启用该 source。系统只请求已启用来源。标题或正文疑似不完整、含广告/脏内容，或者标题无法自动修正时，文章进入人工审核；其余文章进入待发队列。`channels` 当前仅校验和清洗 ID，不做语义增删。

## ID 说明

- `articles.id`：新系统自己的文章主键，质检、审核和状态流转都使用它。
- `source + source_url`：原始素材唯一键，重复拉取不会创建第二篇文章。
- `upstream_archive_id`：素材接口的 `archive_id`；非零表示已存在懂球帝文章。
- `dqd_source_id`、`dqd_archive_id`：为下一阶段后台草稿和发布适配预留，不参与当前流程。

## 启动

```bash
cd "/Users/demo/Desktop/automatic/automatic post"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python run.py web
```

打开 <http://127.0.0.1:8890>。未配置素材接口密钥时，页面仍可正常使用，但“立即拉取”会显示配置缺失。

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
