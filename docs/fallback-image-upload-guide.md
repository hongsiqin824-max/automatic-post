# 栏目兜底图片上传与配置操作手册

本文档用于在当前服务器上新增各联赛、杯赛或其他非精选栏目的兜底图片。以后新增图片时，使用同一套流程即可，不需要再次修改 Nginx，也不需要因为新增图片而重启 Automatic Post 服务。

## 1. 当前固定配置

服务器静态文件目录：

```text
/var/www/automatic-post-assets/fallback/
```

公网 URL 前缀：

```text
https://matchgif.aisportsapp.com/automatic-post-assets/fallback/
```

两者的对应关系如下：

```text
/var/www/automatic-post-assets/fallback/sweden-super.jpg
https://matchgif.aisportsapp.com/automatic-post-assets/fallback/sweden-super.jpg
```

当前服务器的 Nginx 映射已经生效。只要把新图片放入上述目录并保证文件权限正确，新 URL 就会立即可访问。

## 2. 图片生效规则

系统创建懂球帝草稿或直接发布时，按以下优先级选择后台封面：

1. 正文中的第一张有效图片。
2. 素材接口返回的有效封面图。
3. 文章所属非精选栏目配置的兜底图片。
4. 系统绿色公共兜底图。

因此，栏目兜底图只在正文没有有效图片，并且素材也没有有效封面图（或者素材封面就是绿色公共图）时使用。

其他规则：

- 精选栏目本身不使用栏目兜底图。当前精选栏目后台 ID 为 `58`，旧数据兼容 ID `-1`；如果文章还属于其他普通栏目，系统会跳过精选并继续查找普通栏目的配置。
- 一篇文章属于多个非精选栏目时，按栏目顺序使用第一个已配置的兜底图。
- 使用栏目兜底图时，该图片会作为提交给懂球帝的 `litpic`；如果正文没有图片，系统也会把它插入正文。
- 已经在懂球帝创建完成的草稿或已经发布的文章不会被追溯修改。
- 尚未提交到懂球帝的文章，在实际提交时会读取栏目当前保存的兜底图配置。

## 3. 图片准备规范

### 3.1 支持格式

建议使用 JPEG 图片，`.jpg` 和 `.jpeg` 都可以。PNG 也可以使用，但通常文件更大。

文件扩展名必须与图片真实格式相符。不要只修改文件名来转换格式。例如，把 PNG 直接改名为 `.jpeg` 并不会把它转换成 JPEG。

### 3.2 推荐尺寸

- 建议使用横图，宽高比约为 `16:9`。
- 可使用 `1200x675`、`1280x720` 等尺寸。
- 文件应适当压缩，避免使用体积过大的原图。
- 图片主体尽量位于中央，避免列表页裁剪后看不到关键信息。

以上是展示建议，不是本系统代码强制限制。

### 3.3 正式文件命名

正式文件名统一使用：

- 小写英文字母和数字。
- 单词之间使用短横线 `-`。
- 不使用空格、中文、括号或特殊符号。
- `.jpg` 或 `.jpeg` 扩展名与真实格式保持一致。

示例：

| 栏目 | 原始文件名示例 | 正式文件名示例 |
| --- | --- | --- |
| 瑞典超 | `瑞典超.jpg` | `sweden-super.jpg` |
| 德乙 | `2. Bundesliga.jpeg` | `germany-bundesliga-2.jpeg` |
| 挪威超 | `Norway.jpeg` | `norway-eliteserien.jpeg` |
| 丹麦超 | `丹麦超级联赛.jpg` | `denmark-superliga.jpg` |

上传到临时目录时可以保留原文件名，但复制到正式目录时必须改成规范名称。命令中遇到空格时要使用双引号包住完整路径。

## 4. 标准上传流程

下面以德乙图片为例：

```text
原始文件名：2. Bundesliga.jpeg
正式文件名：germany-bundesliga-2.jpeg
最终 URL：https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
```

### 第一步：登录服务器并确认用户

在服务器终端执行：

```bash
whoami
```

如果当前是 root，预期输出：

```text
root
```

如果不是 root，后续写入 `/var/www` 的 `install` 命令前需要加 `sudo`。

### 第二步：创建临时上传目录

执行：

```bash
mkdir -p /tmp/automatic-post-fallback-upload
cd /tmp/automatic-post-fallback-upload
pwd
```

作用：

- `mkdir -p` 创建临时上传目录；目录已经存在也不会报错。
- `cd` 进入该目录。
- `pwd` 确认当前所在目录。

预期最后输出：

```text
/tmp/automatic-post-fallback-upload
```

### 第三步：把本地图片上传到临时目录

推荐使用服务器网页终端或服务器管理面板自带的“上传文件”功能，把图片上传到：

```text
/tmp/automatic-post-fallback-upload/
```

上传完成后，服务器上的完整路径应为：

```text
/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg
```

如果使用本地终端和 SCP，可以执行下面的命令。请先把示例 IP `203.0.113.10` 替换为真实服务器 IP：

```bash
scp "/本地实际目录/2. Bundesliga.jpeg" root@203.0.113.10:/tmp/automatic-post-fallback-upload/
```

正常情况下会看到上传进度，例如：

```text
2. Bundesliga.jpeg                    100%  420KB   2.1MB/s   00:00
```

如果提示 `No such file or directory`，说明本地图片路径写错；如果提示 `Permission denied`，需要检查 SSH 用户、密钥或服务器登录权限。

### 第四步：确认文件已经上传且确实是图片

在服务器终端执行：

```bash
ls -lh "/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg"
file "/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg"
```

正常输出示例：

```text
-rw-r--r-- 1 root root 420K Aug 28 15:30 /tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg
/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg: JPEG image data, JFIF standard 1.01, 1200x675, components 3
```

重点检查：

- `ls` 显示文件大小不能是 `0`。
- `file` 应显示 `JPEG image data`；PNG 则应显示 `PNG image data`。
- 如果 `file` 显示 `HTML document`、`ASCII text` 或普通 `data`，说明上传的不是有效图片，不要继续发布。

### 第五步：确认正式文件名尚未被占用

执行：

```bash
test -e /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg && echo "目标文件已经存在，暂时不要覆盖" || echo "文件名可用，可以继续"
```

第一次上传时，预期输出：

```text
文件名可用，可以继续
```

如果输出：

```text
目标文件已经存在，暂时不要覆盖
```

不要直接执行下一步。请按本文“更新已有图片”章节操作，优先采用新的版本文件名，避免 CDN 或浏览器缓存继续显示旧图。

### 第六步：安装到正式静态资源目录

当前用户是 root 时执行：

```bash
install -o root -g root -m 0644 \
  "/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg" \
  "/var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg"
```

非 root 用户执行：

```bash
sudo install -o root -g root -m 0644 \
  "/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg" \
  "/var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg"
```

作用：

- 把临时图片复制到正式目录。
- 把文件所有者设置为 `root:root`。
- 把权限设置为 `0644`，即所有用户可以读取，只有 root 可以修改。

成功时通常没有任何输出。Linux 命令“没有输出且返回终端提示符”即表示执行成功，下一步仍需验证。

### 第七步：检查正式文件和权限

执行：

```bash
ls -lh /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
file /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
sha256sum "/tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg" /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
```

正常输出示例：

```text
-rw-r--r-- 1 root root 420K Aug 28 15:35 /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
/var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg: JPEG image data, JFIF standard 1.01, 1200x675, components 3
abc123...  /tmp/automatic-post-fallback-upload/2. Bundesliga.jpeg
abc123...  /var/www/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
```

两行 `sha256sum` 的哈希值应该完全相同，表示正式文件与上传文件内容一致。实际哈希值会很长，也不会与示例中的 `abc123...` 相同。

### 第八步：验证公网 URL

执行完整 GET 检查：

```bash
curl -sS -L --max-time 20 -o /dev/null \
  -w 'HTTP=%{http_code}\nTYPE=%{content_type}\nSIZE=%{size_download}\n' \
  https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
```

正常输出示例：

```text
HTTP=200
TYPE=image/jpeg
SIZE=430080
```

判断标准：

- `HTTP=200`：URL 能正常访问。
- `TYPE=image/jpeg`：服务器返回 JPEG 图片；PNG 应为 `image/png`。
- `SIZE` 必须大于 `0`，具体数值因图片而异。

最后再直接用浏览器打开：

[德乙兜底图片](https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2.jpeg)

浏览器能正常显示图片，并且 `curl` 检查同时通过，才算静态资源发布成功。

## 5. 在 Automatic Post 后台配置栏目

静态图片验证通过后，再进行栏目配置：

1. 打开 Automatic Post 的“栏目与来源”或配置页面。
2. 找到“德乙”栏目。
3. 点击该栏目右侧的编辑按钮。
4. 在“栏目兜底图片（可选）”中填写完整 URL：

   ```text
   https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2.jpeg
   ```

5. 点击保存。
6. 再次打开德乙栏目编辑窗口，确认 URL 仍然存在，表示已经保存到数据库。

新增图片和保存栏目配置都不需要重启 Automatic Post 服务。只有服务器仍运行不包含该功能的旧版代码时，部署新代码后才需要进行一次服务重启。

## 6. 验证业务是否真正使用兜底图

公网 URL 可访问不等于业务一定会使用它。业务验证必须选择一篇满足以下条件的德乙文章：

- 文章已经正确归入“德乙”栏目。
- 正文中没有有效的 `<img>` 图片。
- 素材没有有效封面，或者素材封面是系统绿色公共图。
- 文章尚未提交到懂球帝。

建议先做无副作用的预览验证：

1. 打开符合条件的文章详情。
2. 查看“发布预览”。
3. 查看“后台封面（将提交）”。
4. 确认显示的是德乙图片，而不是绿色公共图。
5. 检查正文预览，确认正文没有原图时，德乙图片已被插入正文。

确认预览正确后，再创建懂球帝草稿或直接发布。提交后的预期效果：

- 请求中的 `litpic` 是德乙图片 URL。
- 懂球帝后台列表右侧封面显示德乙图片。
- 原正文没有图片时，正文首段附近会出现德乙图片。

如果测试文章本身有正文图片或有效素材封面，系统优先使用原图，这是正常行为，不能用这类文章判断栏目兜底图是否失效。

## 7. 以后新增任意联赛时的通用模板

每次只需要确定下面三个值：

```text
原始文件名：上传前的图片文件名
正式文件名：规范化的英文文件名
栏目名称：Automatic Post 中需要配置的栏目
```

假设：

```text
原始文件名：原始图片.jpeg
正式文件名：league-slug.jpeg
栏目名称：目标联赛
```

服务器命令模板：

```bash
mkdir -p /tmp/automatic-post-fallback-upload

ls -lh "/tmp/automatic-post-fallback-upload/原始图片.jpeg"
file "/tmp/automatic-post-fallback-upload/原始图片.jpeg"

test -e /var/www/automatic-post-assets/fallback/league-slug.jpeg && echo "目标文件已经存在，暂时不要覆盖" || echo "文件名可用，可以继续"

install -o root -g root -m 0644 \
  "/tmp/automatic-post-fallback-upload/原始图片.jpeg" \
  "/var/www/automatic-post-assets/fallback/league-slug.jpeg"

ls -lh /var/www/automatic-post-assets/fallback/league-slug.jpeg
file /var/www/automatic-post-assets/fallback/league-slug.jpeg

curl -sS -L --max-time 20 -o /dev/null \
  -w 'HTTP=%{http_code}\nTYPE=%{content_type}\nSIZE=%{size_download}\n' \
  https://matchgif.aisportsapp.com/automatic-post-assets/fallback/league-slug.jpeg
```

后台填写模板：

```text
https://matchgif.aisportsapp.com/automatic-post-assets/fallback/league-slug.jpeg
```

使用模板时，必须把 `原始图片.jpeg` 和 `league-slug.jpeg` 全部替换成真实值，不要原样执行占位符命令。

## 8. 更新已有图片

不建议直接覆盖已经上线的同名文件。浏览器、Nginx 上游或 CDN 可能继续缓存旧图片，导致服务器文件已经更新但页面仍显示旧图。

推荐使用版本化文件名：

```text
旧文件：germany-bundesliga-2.jpeg
新文件：germany-bundesliga-2-v2.jpeg
```

按标准上传流程发布 `germany-bundesliga-2-v2.jpeg`，验证新 URL 后，再把后台德乙栏目的兜底图片改成：

```text
https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2-v2.jpeg
```

确认新图业务生效后，旧文件可以暂时保留。这样既可以回滚，也不会影响已经引用旧 URL 的历史内容。

## 9. 常见问题排查

### 9.1 公网返回 404

含义：Nginx 找不到目标文件。

执行：

```bash
ls -lh /var/www/automatic-post-assets/fallback/
```

检查：

- 正式文件是否真的存在。
- URL 文件名与服务器文件名是否完全一致。
- Linux 文件名区分大小写，`.JPG`、`.jpg`、`.jpeg` 是不同名称。
- URL 中是否误写空格、中文或多余路径。

### 9.2 公网返回 403

含义：文件存在，但 Nginx 没有读取权限。

执行：

```bash
ls -ld /var /var/www /var/www/automatic-post-assets /var/www/automatic-post-assets/fallback
ls -l /var/www/automatic-post-assets/fallback/league-slug.jpeg
namei -l /var/www/automatic-post-assets/fallback/league-slug.jpeg
```

正常文件权限应类似：

```text
-rw-r--r-- 1 root root ... league-slug.jpeg
```

如果文件权限不对，执行：

```bash
chmod 0644 /var/www/automatic-post-assets/fallback/league-slug.jpeg
```

不要随意对整个 `/var/www` 执行递归 `chmod`。

### 9.3 HTTP 200，但 TYPE 不是图片

如果输出类似：

```text
HTTP=200
TYPE=text/html
```

通常表示 URL 返回了网页、错误页或跳转后的默认页面，不是图片。需要检查文件真实格式和 Nginx 路径，不能把它填入栏目配置。

### 9.4 URL 正常，但后台仍使用绿色公共图

依次检查：

1. “德乙”栏目编辑窗口中是否保存了完整 `https://...` URL。
2. 测试文章是否真的属于德乙栏目。
3. 测试文章是否已经提交；已创建的草稿不会自动更新封面。
4. 当前文章是否只属于精选栏目；精选会被跳过，只有同时存在普通栏目时才可能找到栏目兜底图。
5. 页面是否运行包含栏目兜底图功能的新版服务。

### 9.5 URL 正常，但后台使用了别的图片

先检查文章是否已有正文图片或素材有效封面。系统优先使用这些原图，只有缺图时才使用栏目兜底图，这是预期行为。

如果文章属于多个非精选栏目，并且多个栏目都配置了兜底图片，系统会选择栏目顺序中的第一张有效配置图。

### 9.6 更新图片后浏览器仍显示旧图

这是缓存的典型表现。不要反复重启服务，改用 `-v2`、`-v3` 等新文件名，验证新 URL 后更新栏目配置。

## 10. 每次上线前检查清单

- [ ] 图片真实格式正确，`file` 显示 JPEG 或 PNG。
- [ ] 正式文件名只包含小写英文、数字、短横线和正确扩展名。
- [ ] 正式文件位于 `/var/www/automatic-post-assets/fallback/`。
- [ ] 文件权限为 `0644`，Nginx 可以读取。
- [ ] `curl` 返回 `HTTP=200`。
- [ ] `content-type` 为 `image/jpeg` 或 `image/png`。
- [ ] 浏览器可以直接打开并正确显示图片。
- [ ] Automatic Post 对应栏目已经保存完整 URL。
- [ ] 使用无正文图片、无有效素材封面的文章完成预览验证。
- [ ] 发布预览的“后台封面（将提交）”显示目标联赛图片。

## 11. 多联赛连续上传登记表

一次准备多张图片时，仍建议逐张执行“格式检查、正式安装、公网验证、后台保存、业务预览”，不要在尚未验证时一次性把所有 URL 填入后台。

可以复制下面的表格作为当次操作记录：

| 栏目 | 原始文件名 | 正式文件名 | 公网 URL | HTTP/类型通过 | 后台已保存 | 业务预览通过 |
| --- | --- | --- | --- | --- | --- | --- |
| 德乙 | `2. Bundesliga.jpeg` | `germany-bundesliga-2.jpeg` | `https://matchgif.aisportsapp.com/automatic-post-assets/fallback/germany-bundesliga-2.jpeg` | 待检查 | 待配置 | 待验证 |
| 待填写 | 待填写 | 待填写 | 待填写 | 待检查 | 待配置 | 待验证 |

每一行只有在以下三项都完成后才算上线完成：

1. 公网检查为 `HTTP=200` 且类型为图片。
2. 对应栏目重新打开后仍能看到已保存的 URL。
3. 符合缺图条件的文章在“后台封面（将提交）”中显示正确图片。
