# Steam 公告图上方间距实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为普通游戏公告图及其固定游戏图回退增加 16 像素上方留白，消除正文末行紧贴图片的现象。

**Architecture:** 在 `RenderBlock` 中增加默认值为 0 的 `top_gap`，由绘制和高度测量共同处理。普通游戏公告分支通过 `NEWS_IMAGE_TOP_MARGIN = 16` 启用该属性，其他图片类型继续使用默认值。

**Tech Stack:** Python 3.12、Pillow、`unittest`、AstrBot 插件运行环境、Git、Docker

## Global Constraints

- 普通游戏公告图上方固定保留 16 像素。
- 公告原图和固定游戏图回退采用相同规则。
- 公告图继续限宽、等比缩放、水平居中且完整展示。
- 创意工坊、免费游戏及其他页眉图片维持现有排版。
- `RenderBlock.top_gap` 默认值必须为 0，并放在字段末尾以保持位置参数兼容。
- 配置结构、UMO 通知、旧群号字段、状态文件和图片缓存格式维持现状。
- GitHub 推送仅允许发送到 `RhoninSeiei/astrbot_plugin_steam_updates` 的 `master`，禁止向上游 `DearCrazyLeaf` 仓库推送。
- 生产部署必须保留线上配置、运行状态和 `appid_map.json`。

---

### Task 1: 图片块前置间距能力

**Files:**
- Modify: `tests/test_news_images.py:570-606`
- Modify: `main.py:78-85`
- Modify: `main.py:3222-3247`
- Modify: `main.py:3532-3548`

**Interfaces:**
- Consumes: `RenderBlock(kind, text, font, color, gap, image, align)` 的既有位置参数形式。
- Produces: `RenderBlock.top_gap: int = 0`；`_draw_blocks()` 和 `_measure_blocks_height()` 对图片块采用 `top_gap + image.height + gap`。

- [ ] **Step 1: 编写失败测试**

在 `NewsImageLayoutTest` 中加入：

```python
def test_image_top_gap_is_measured_and_drawn(self):
    plugin = self._make_plugin()
    canvas = self.mod.PilImage.new("RGB", (900, 500), (0, 0, 0))
    image = self.mod.PilImage.new("RGB", (400, 400), (255, 0, 0))
    blocks = [
        self.mod.RenderBlock(
            "image",
            image=image,
            gap=10,
            align="center",
            top_gap=16,
        )
    ]

    end_y = plugin._draw_blocks(
        canvas,
        self.mod.ImageDraw.Draw(canvas),
        blocks,
        900,
        52,
        0,
    )

    self.assertEqual(canvas.getpixel((250, 15)), (0, 0, 0))
    self.assertEqual(canvas.getpixel((250, 16)), (255, 0, 0))
    self.assertEqual(end_y, 426)
    self.assertEqual(plugin._measure_blocks_height(blocks, 0), 426)
```

- [ ] **Step 2: 运行测试并确认因接口缺失而失败**

从隔离测试副本的插件根目录运行：

```bash
python -m unittest tests.test_news_images.NewsImageLayoutTest.test_image_top_gap_is_measured_and_drawn -v
```

预期：`TypeError`，错误信息包含 `unexpected keyword argument 'top_gap'`。

- [ ] **Step 3: 增加最小实现**

在 `RenderBlock` 最后增加：

```python
top_gap: int = 0
```

在 `_draw_blocks()` 的图片分支中，存在图片时先推进坐标：

```python
elif block.kind == "image":
    if block.image:
        y += block.top_gap
        image_x = padding
        if block.align == "center":
            content_width = width - 2 * padding
            image_x += (content_width - block.image.width) // 2
        img.paste(
            block.image,
            (image_x, y),
            block.image if block.image.mode == "RGBA" else None,
        )
        y += block.image.height + block.gap
```

在 `_measure_blocks_height()` 的图片分支中同步计入：

```python
elif block.kind == "image":
    height += block.top_gap + (block.image.height if block.image else 0) + block.gap
```

- [ ] **Step 4: 运行目标测试并确认通过**

```bash
python -m unittest tests.test_news_images.NewsImageLayoutTest.test_image_top_gap_is_measured_and_drawn -v
```

预期：`Ran 1 test` 和 `OK`。

- [ ] **Step 5: 运行公告图测试模块**

```bash
python -m unittest tests.test_news_images -v
```

预期：原有 19 项测试与新增测试全部通过，共 20 项。

- [ ] **Step 6: 提交独立修改**

```bash
git add main.py tests/test_news_images.py
git commit -m "feat: support image block top spacing"
```

---

### Task 2: 普通游戏公告图启用专用间距

**Files:**
- Modify: `tests/test_news_images.py:408-471`
- Modify: `main.py:45-58`
- Modify: `main.py:3466-3478`

**Interfaces:**
- Consumes: Task 1 提供的 `RenderBlock.top_gap: int = 0`。
- Produces: `NEWS_IMAGE_TOP_MARGIN: int = 16`；普通游戏公告原图和固定游戏图回退生成的图片块均设置 `top_gap=NEWS_IMAGE_TOP_MARGIN`。

- [ ] **Step 1: 扩充回退测试并确认失败**

在 `NewsImageFallbackTest.test_each_announcement_uses_its_image_or_fixed_fallback` 现有图片和居中断言之后加入：

```python
self.assertEqual(
    [block.top_gap for block in blocks if block.kind == "image"],
    [
        self.mod.NEWS_IMAGE_TOP_MARGIN,
        self.mod.NEWS_IMAGE_TOP_MARGIN,
        self.mod.NEWS_IMAGE_TOP_MARGIN,
    ],
)
```

运行：

```bash
python -m unittest tests.test_news_images.NewsImageFallbackTest.test_each_announcement_uses_its_image_or_fixed_fallback -v
```

预期：测试失败，实际值为 `[0, 0, 0]`，期望值为 `[16, 16, 16]`；如果常量尚未定义，则先以字面值 `[16, 16, 16]` 完成失败验证，再在实现后改用常量引用。

- [ ] **Step 2: 增加专用常量并应用到普通游戏公告图**

在卡片渲染常量区域增加：

```python
NEWS_IMAGE_TOP_MARGIN = 16
```

将普通游戏公告分支的图片块构造改为：

```python
if item_image:
    blocks.append(
        RenderBlock(
            "image",
            image=item_image,
            gap=10,
            align="center",
            top_gap=NEWS_IMAGE_TOP_MARGIN,
        )
    )
```

该位置位于 `is_workshop_sec` 和 `is_free_games_sec` 之外，仅覆盖普通游戏公告原图和固定游戏图回退。

- [ ] **Step 3: 运行目标测试并确认通过**

```bash
python -m unittest tests.test_news_images.NewsImageFallbackTest.test_each_announcement_uses_its_image_or_fixed_fallback -v
```

预期：`Ran 1 test` 和 `OK`。

- [ ] **Step 4: 运行公告图模块并确认排版兼容**

```bash
python -m unittest tests.test_news_images -v
```

预期：20 项测试全部通过；水平居中、原比例高度、像素预算和固定图回退测试保持通过。

- [ ] **Step 5: 提交公告图修正**

```bash
git add main.py tests/test_news_images.py
git commit -m "fix: separate Steam news images from text"
```

---

### Task 3: 完整验证、个人仓库发布与生产更新

**Files:**
- Verify: `main.py`
- Verify: `_conf_schema.json`
- Verify: `metadata.yaml`
- Verify: `tests/test_free_games.py`
- Verify: `tests/test_local_poll_lock.py`
- Verify: `tests/test_news_images.py`
- Verify: `tests/test_notify_targets.py`
- Preserve: `/volume1/docker/astrbot/data/config/astrbot_plugin_steam_updates_config.json`
- Preserve: `/volume1/docker/astrbot/data/plugins/astrbot_plugin_steam_updates/appid_map.json`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的两个通过审查的提交。
- Produces: 个人仓库 `master`、生产插件目录和运行中的插件实例采用相同代码提交。

- [ ] **Step 1: 创建隔离测试副本并运行完整测试**

将当前工作树的 `main.py`、配置结构和 `tests` 复制到 AstrBot 容器内的临时目录，避免在生产插件目录执行开发测试：

```bash
docker exec astrbot mkdir -p /tmp/steam-news-image-spacing-test/tests
docker cp main.py astrbot:/tmp/steam-news-image-spacing-test/main.py
docker cp _conf_schema.json astrbot:/tmp/steam-news-image-spacing-test/_conf_schema.json
docker cp tests/. astrbot:/tmp/steam-news-image-spacing-test/tests/
```

运行采用顶层导入的三个测试模块：

```bash
docker exec -w /tmp/steam-news-image-spacing-test/tests astrbot python -m unittest test_free_games test_local_poll_lock test_notify_targets -v
```

预期：57 项全部通过。

运行采用包导入的公告图测试模块：

```bash
docker exec -w /tmp/steam-news-image-spacing-test astrbot python -m unittest tests.test_news_images -v
```

预期：20 项全部通过。两组共 77 项测试，失败数为 0。

- [ ] **Step 2: 执行静态检查和仓库检查**

```bash
docker exec astrbot python -c 'compile(open("/tmp/steam-news-image-spacing-test/main.py", encoding="utf-8").read(), "main.py", "exec")'
python3 -m json.tool _conf_schema.json
git diff --check
git status --short
```

预期：编译与 JSON 检查退出码为 0，`git diff --check` 无输出，仓库仅包含预期提交且工作树干净。

- [ ] **Step 3: 核验个人仓库目标并仅推送 master**

```bash
git remote -v
git branch -f master HEAD
git branch --set-upstream-to=personal/master master
git push --dry-run personal HEAD:refs/heads/master
git push personal HEAD:refs/heads/master
git ls-remote personal refs/heads/master
```

预期：推送目标为 `https://github.com/RhoninSeiei/astrbot_plugin_steam_updates.git`，远程 `master` 指向当前 `HEAD`。任何命令都不得使用 `origin` 作为推送目标。

- [ ] **Step 4: 备份生产插件与配置**

记录部署时间并创建：

```text
/volume1/docker/astrbot/data/plugin_backups/steam_updates_v1.2.10_<YYYYMMDD-HHMMSS>/plugin/
```

完整复制生产插件目录，并单独复制：

```text
/volume1/docker/astrbot/data/config/astrbot_plugin_steam_updates_config.json
```

部署前记录 `main.py`、配置文件和 `appid_map.json` 的 SHA256。

- [ ] **Step 5: 更新生产插件并保留线上数据**

使用 `rsync -rlt --no-perms --no-owner --no-group` 从当前工作树同步到：

```text
/volume1/docker/astrbot/data/plugins/astrbot_plugin_steam_updates/
```

排除 `.git`、`__pycache__`、`.superpowers`、`.worktrees` 和 `appid_map.json`。同步后仅为本次代码文件补充容器读取权限。再次计算 SHA256，要求生产 `main.py` 与当前工作树一致，配置和 `appid_map.json` 与部署前一致。

- [ ] **Step 6: 热重载插件并核验运行状态**

通过 AstrBot 本机管理接口执行：

```text
POST /api/v1/plugins/astrbot_plugin_steam_updates/reload
```

预期响应为 HTTP `200` 和 `重载成功`。随后检查：

```bash
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.RestartCount}}' astrbot
docker logs --since 10m --tail 400 astrbot
```

预期：容器为 `running healthy`，插件成功加载 `v1.2.10`，新 `.poll.instance` 时间晚于部署时间，没有插件加载失败、异常堆栈或发送错误。

- [ ] **Step 7: 记录生产核验结果**

记录以下证据：个人仓库 `master` 提交号、生产 `main.py` SHA256、配置和 `appid_map.json` 部署前后 SHA256、备份目录、热重载响应、容器健康状态、测试总数。下一次实际公告推送出现后，检查公告图首行前具有 16 像素背景区域。
