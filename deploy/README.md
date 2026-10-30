# 服务器部署说明

服务是单进程 Flask（`python run.py web`），定时抓稿 / 质检 / 发布 / 告警都在进程内线程里。
服务器上由 systemd 托管，只监听 `127.0.0.1:8890`，域名经 Nginx 转发进来。

| 文件 | 用途 | 在哪运行 |
|---|---|---|
| `deploy.sh` | 日常部署 / 回滚 / 状态 / 日志 | 服务器 |
| `deploy/setup.sh` | 首次安装（venv、依赖、systemd、备份、Nginx） | 服务器，一次 |
| `deploy/export_from_mac.sh` | 停 Mac 服务、导出数据库快照和 env | Mac，迁移当天一次 |
| `deploy/import_db.sh` | 导入数据库快照 | 服务器，迁移当天一次 |
| `deploy/backup.sh` | 每日备份（cron 04:30，保留 7 份） | 服务器，自动 |
| `deploy/automatic-post.service` | systemd 服务模板 | setup.sh 安装 |
| `deploy/nginx-automatic-post.conf` | Nginx 反向代理模板 | setup.sh 安装 |

> **铁律：Mac 和服务器绝不能同时运行服务**，否则同一篇稿子会发两次。

---

## 一、服务器到手后：环境检测（只读，不改任何东西）

在跳板机网页终端粘贴执行，把输出发给开发：

```bash
cat /etc/os-release | head -2; uname -m; python3 --version; git --version; sudo -n true 2>/dev/null && echo "sudo: 免密可用" || echo "sudo: 需要密码或无权限"; systemctl --version | head -1; timedatectl 2>/dev/null | grep "Time zone"; df -h ~ | tail -1; for u in https://gitee.com https://aigc-core.dongqiudi.com https://aigw.dongqiudi.com https://tokenhub.dongqiudi.com https://platform.dongqiudi.com https://dadmin.dongqiudi.com https://matchgif.aisportsapp.com https://open.feishu.cn https://pypi.org https://mirrors.aliyun.com/pypi/simple/; do printf "%-45s" "$u"; curl -s -o /dev/null -m 8 -w "%{http_code}\n" "$u" || echo "不通"; done; (timeout 5 bash -c "</dev/tcp/172.21.0.148/9030" && echo "StarRocks 9030 通") || echo "StarRocks 9030 不通"
```

## 二、首次安装（迁移前一天做即可，不影响 Mac 上正在跑的服务）

### 1. 配置 Gitee 部署公钥（只读）

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/gitee_deploy -C "automatic-post-server"
cat ~/.ssh/gitee_deploy.pub
```

把输出的公钥添加到 Gitee 仓库 →「管理」→「部署公钥管理」→「添加部署公钥」。然后：

```bash
cat >> ~/.ssh/config <<'EOF'
Host gitee.com
  IdentityFile ~/.ssh/gitee_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@gitee.com   # 首次会问是否信任，输入 yes；看到 "successfully authenticated" 即成功
```

### 2. 拉代码并安装

```bash
git clone git@gitee.com:tsdka/automatic-post.git ~/automatic-post
cd ~/automatic-post
bash deploy/setup.sh                          # 没有域名时
bash deploy/setup.sh --domain <运维给的域名>     # 有域名时（需先装 nginx）
```

服务器访问不了 pypi.org 时，前面加镜像：`PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ bash deploy/setup.sh ...`

此时还没有 `.env`，脚本会提示，没关系——`.env` 在迁移当天和数据库一起上传。
setup.sh 只把服务设为开机自启，**不会启动**。

## 三、迁移当天

| 步骤 | 在哪 | 操作 |
|---|---|---|
| 1 | Mac | `bash deploy/export_from_mac.sh` → 输入 `YES`。会停掉 Mac 服务并取消自启，导出到 `../server-migration/` |
| 2 | 跳板机 | 上传 `automatic_post.sqlite3.gz`、`automatic_post.sqlite3.gz.sha256`、`env` 到服务器 `~/automatic-post/` |
| 3 | 服务器 | `cd ~/automatic-post && mv env .env && chmod 600 .env` |
| 4 | 服务器 | `bash deploy/import_db.sh automatic_post.sqlite3.gz`（自动校验完整性） |
| 5 | 服务器 | `bash deploy/setup.sh [--domain 域名]` 再跑一次，确认 `.env` 检查都通过 |
| 6 | 服务器 | `sudo systemctl start automatic-post && ./deploy.sh status` |
| 7 | 服务器 | `./deploy.sh logs 50`：应看到「TokenHub / AIGW 余额检查正常」；10 分钟内应出现抓稿和 `aigw.dongqiudi.com ... 200` |
| 8 | 浏览器 | 打开域名，审核页面能正常显示 |
| 9 | Mac | 上传完成后删除 `../server-migration/`（env 里有密钥） |

Mac 停机到服务器启动之间的稿件不会丢：素材接口按最近 6 小时拉取，服务器启动后会补上。

**迁移失败要让 Mac 继续跑**（服务器还没启动过时才能这样做）：

```bash
mv ~/Library/LaunchAgents/disabled/com.automatic-post.web.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.automatic-post.web.plist
```

服务器已经运行并发过稿之后，Mac 上的库就旧了，不能直接切回去。

## 四、日常改代码上线

```bash
# Mac 上
python3 -m pytest tests/ -q
git push gitee main

# 跳板机进服务器
cd ~/automatic-post && ./deploy.sh
```

`./deploy.sh` 做的事：拉 Gitee 最新 main（只快进）→ 依赖有变才重装 → 跑测试（不过就回退代码、不重启）→
重启 → 健康检查（不过自动回退到上一版）→ 打印本次更新的提交和最新日志。

| 命令 | 作用 |
|---|---|
| `./deploy.sh` | 部署最新代码 |
| `./deploy.sh rollback` | 回退到上一次部署前的版本 |
| `./deploy.sh rollback <commit>` | 回退到指定版本 |
| `./deploy.sh status` | 当前版本、Gitee 上有几个新提交、服务状态、健康检查、最近部署记录 |
| `./deploy.sh logs [N]` | 最近 N 行日志（默认 100） |
| `FORCE=1 ./deploy.sh` | 代码没变也重启一次（如改了 `.env` 之后） |
| `SKIP_TESTS=1 ./deploy.sh` | 跳过测试（仅紧急修复） |

**改了 `.env`**：服务器上 `vi ~/automatic-post/.env` 后 `sudo systemctl restart automatic-post`。

## 五、常见问题

| 现象 | 处理 |
|---|---|
| `./deploy.sh` 提示「服务器上的代码有改动」 | 有人直接在服务器改了代码。`git status` 看是什么，确认无用后 `git checkout -- .`；代码只在 Mac 改 |
| 「无法快进合并」 | Gitee 上的 main 被强推过。确认 Gitee 版本正确后 `git fetch origin && git reset --hard origin/main` |
| 页面 502 | 服务没起来：`./deploy.sh logs 100` |
| 余额读取失败告警 | TokenHub / AIGW 访问令牌过期，在对应平台「个人资料」重新生成，改 `.env` 后重启 |
| 开放平台授权已过期 | 在审核页面重新走一次开放平台授权 |
| 恢复备份 | `sudo systemctl stop automatic-post`，`bash deploy/import_db.sh instance/backup/<某个备份>.gz`，再启动 |
