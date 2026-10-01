# Nascence 辉夜 · QQ bot

一个常驻 Python 服务运行辉夜 QQ bot，并在同一地址的根路径 `/` 提供网页管理面板。对话输入与公开回复仅通过 QQ / NapCat；网页管理 QQ 配置、记忆、模型、群素材与笔记，查看历史、任务、日志和备份。

已删除自训练、陪练、模拟世界、合成对话注入、桌面聊天及 CLI 聊天入口。保留真实 QQ 交互形成记忆、记忆关联与概念检索、生物节律、素材与笔记动作，以及默认关闭的 QQ 主动行为。当前支持群聊，一个部署对应一个人格、一个 QQ 账号，可接入多个群。

## 快速启动

Docker 部署（从源码构建，含面板前端）见 [docker/README.md](docker/README.md)。

需要 Python 3.12、Node.js 20+ / npm、Ollama 与 NapCat。Linux/macOS：

```bash
bash setup.sh
bash start.sh
```

`setup.sh` 安装 Python 依赖并构建面板。Linux 可选 `bash setup.sh --with-ollama --pull-model` 下载完整本地 Ollama 运行时并拉取配置中的向量模型，需要 `curl`、`tar`、`zstd`。它按 `x86_64 → amd64`、`aarch64 → arm64` 选择 `.tar.zst`，保留 `bin` 与 `lib`；参见 [Ollama 官方 Linux 安装说明](https://docs.ollama.com/linux)。macOS 使用官方 Ollama 应用，脚本不从应用包提取单个可执行文件。

Windows：

```powershell
.\setup.ps1
.\start.bat
# 可选安装本地 Ollama 与拉取模型
.\setup.ps1 -WithOllama -PullModel
```

启动脚本和安装脚本共用 `OLLAMA_MODELS=<项目>/ollama/models`；设置该环境变量可使用其他目录。已运行的 Ollama 进程不会被脚本结束；脚本只回收自己启动的进程。安装不会偷偷拉取模型，只有显式的 `--pull-model` / `-PullModel` 会执行拉取。

也可以直接启动现有 Python 环境：

```bash
python main.py --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000/**。管理员账号为 `admin`。首次创建账号时，可通过 `HUIYE_ADMIN_PASSWORD` 设置至少 12 字符的密码；否则初始随机密码写入数据目录的 `initial_admin_password.txt`（Linux 文件权限 `0600`）。密码不输出到控制台。登录后在“模型与行为”修改密码，会撤销全部旧会话并删除初始密码文件。该环境变量只用于首次创建账号。

模型不可用或旧数据未迁移时，管理面板仍可登录，核心会显示未就绪；修正配置后点“重新初始化核心”。启动就绪检查不会自动下载模型。

## QQ / NapCat 设置

1. 在面板“模型与行为”设置机器人 QQ 号、主模型地址/名称/API Key、Ollama 地址/模型/维度和 NapCat token。token 至少 16 字符，也可用 `HUIYE_NAPCAT_TOKEN` 环境变量覆盖。
2. 在“QQ 设置”填写群白名单；首次默认空白名单。姓名映射格式为 `{"群号":{"QQ号":"显示名"}}`。
3. 在 NapCat 配置 **反向 WebSocket**：`ws://127.0.0.1:8000/internal/onebot/ws`，使用相同 token（`Authorization: Bearer <token>`）。旧独立 `6700` 端口不再使用。
4. 多模态理解使用单独的多模态服务配置。支持实际服务提供的图片/音频/视频格式；音频转换可能需要系统 `ffmpeg`。识别失败会记录失败任务，不写入假识别结果。
5. 主动行为默认关闭；开启时必须将主动目标群放入白名单。发送前再次校验原任务群、白名单、暂停、睡眠和有效期。

只接受白名单群事件，机器人自己的消息不产生输入任务。群事件由 `(bot_id, group_id, external_id, role)` 去重；输入原文和处理任务先落盘，模型调用在独立线程执行。原群任务使用原群投递目标，修改主动目标不会改写其他群的回复。

回复在收到 NapCat 的 `status=ok, retcode=0` ACK 后，才写公开回复、公开记忆和发送指标。这个状态表示渠道已接受，不能证明所有群成员实际看到了消息。超时/断线/崩溃中断的发送标记 `unknown`，避免盲目重发；明确失败的投递可在有效期内从面板重试，复用已保存的内容和目标。失败的处理任务按已保存阶段重试，已送达或结果未知的任务禁止整体重放。关闭浏览器不会停止 QQ 服务或后台维护任务。

## 数据与范围

默认数据目录为 `<项目>/data/test`，配置目录为 `<项目>/config`，均使用绝对路径，不依赖启动时的工作目录。可用 `HUIYE_DATA_DIR`、`HUIYE_CONFIG_DIR` 指定目录。生产数据与真实凭据已在 `.gitignore` 排除。

SQLite `memory.db` 是唯一事实来源：保存记忆/关联/概念、QQ 消息、任务阶段、投递、管理员会话、管理事件和审计。JSON 为导出快照；启动从 SQLite 全量重建 FAISS 和关键词索引，不接受旧缓存作为恢复依据。只有一个核心写入执行器，一个进程/worker；数据目录的实例锁防止重复启动。

群短期上下文、概念归并、检索、引用、素材与笔记按群隔离。记忆范围有：

- `group`：仅指定群可检索，QQ 交互默认使用此范围。
- `persona_shared`：管理员明确设置的共享人格资料，所有群可检索。
- `legacy_unattributed`：旧数据归属未知，管理员可查看，QQ 不自动使用。

FAISS 检索先构造允许范围，再进行排名；关联扩散与概念查询也校验范围。管理记忆编辑和 QQ 记忆写入使用同一核心校验、事务和索引。面板资料导入只写资料，不模拟 QQ 用户，不触发对话或发言。

新时间字段均使用真实 Unix 时间，界面/时间短语/节律按配置时区显示（默认 `Asia/Taipei`）。时长与保存节流使用单调时钟。无虚拟倍速、模式切换或旧 clock 状态读取；可选固定睡眠窗口开始/结束相同表示不设置窗口。

## 旧数据迁移

**先停止旧服务。** 迁移工具默认只读预览，不调用模型：

```bash
python tools/migrate_legacy.py --preview
# 明确单群归属时才添加 --group；不填写则保留归属未知
python tools/migrate_legacy.py --apply --group 123456789
```

SQLite 为主，旧 JSON 中缺失于数据库的记录按 ID 补入，数据库同 ID 记录不会被 JSON 覆盖。保留原 ID、内容、关联、概念和历史；向量无效会拒绝发布。执行迁移会先在 `data/test/backups/offline-<id>/` 保存数据库与原文件/配置备份，在临时副本上修改、校验，再发布。旧全局素材、`data/test/notes` 或更早的 `data/notes` 仅在显式指定群后复制到该群；已存在的目标群目录会拒绝覆盖。

旧 QQ 记忆可能使用虚拟时间，概念本身又使用真实时间，因此不会统一减/加偏移。只有有依据的字段才能在 `--time-map times.json` 指定转换：

```json
{
  "memories": {
    "旧记忆ID": {"basis": "qq_virtual", "offset": 1700000000},
    "另一记忆ID": {"basis": "unix_utc"}
  },
  "wordweb": {
    "词A||词B": {"basis": "qq_virtual", "offset": 1700000000}
  },
  "links": {
    "源ID||目标ID": {"basis": "qq_virtual", "offset": 1700000000}
  }
}
```

`qq_virtual` 的 `offset` 必须来自验证过的旧 QQ 时间映射。未指定记录原时间不变，标记 `legacy_unknown`，不按旧数值衰减清除；新 QQ 记忆正常衰减。`legacy_time_originals` 保存原字段。概念创建/访问时间不偏移；事件段仅在成员时间全部确认时重建边界。迁移完成后重复执行直接返回 `already_migrated`，不会二次偏移。旧 clock/世界文件仅保留为备份/历史原件，运行时不使用。

模型或向量维度变化需要离线重新嵌入，面板对已有数据禁止直接修改：

```bash
python tools/reembed.py --model 新模型 --dimension 768
python tools/reembed.py --model 新模型 --dimension 768 --apply
```

先拉取目标模型，停止 bot，再执行 `--apply`。工具验证全部记忆和概念向量后才发布数据库与配置，保留完整备份。操作过程留下 `offline_operation.json` 标记；异常中断时核心拒绝就绪，应检查标记里的备份并恢复配套数据库/配置，不能仅删除标记后继续运行。

## 备份、恢复与远程访问

面板“日志与维护”提供后台备份、索引重建、自检与睡眠清理。备份使用 SQLite backup API，ZIP 包含配置和群素材/笔记，需要管理员登录下载；配置中包含凭据，应保管备份。恢复时停止 bot，将包内 `memory.db` 恢复到数据目录、`config/*` 恢复到配置目录，其余文件按相对路径恢复；旧版笔记归档 `legacy_notes_original/` 可恢复到数据目录同名文件夹，迁移工具会读取它；不要保留旧 `memory.db-wal` / `memory.db-shm`。恢复后通过 SQLite 删除 `admin_sessions`，撤销备份中的旧会话，再启动检查完整性和就绪状态。离线迁移备份也可按相同原则恢复。

默认只绑定 `127.0.0.1`。公网管理建议使用 HTTPS 反向代理；**禁止代理 `/internal/*`**。示例 Nginx：

```nginx
location /internal/ { return 404; }
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off; # SSE 管理事件
    proxy_read_timeout 60s;
}
```

`/health/live` 仅表示进程存活，`/health/ready` 表示核心就绪。生产启动只能用 **一个 Uvicorn worker**；不能用自动 reload、多 worker 或多个实例共享数据目录。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
npm ci --prefix frontend
npm run build --prefix frontend
# 前端开发（API 代理到 8000），生产只有 Python 端口
npm run dev --prefix frontend
```

自动测试使用临时数据和模拟模型，禁止外部网络与真实 QQ 发言。覆盖向量校验、事务回滚、保存恢复、群隔离、历史游标、ACK/未知结果、管理鉴权、迁移/重新嵌入、模型 JSON 和素材分页。`tools/verify_browser.py` 对正在运行的临时测试服务做管理页面浏览器验收。

`manager.py` 是调用鉴权管理 API 的维护 CLI；`control_panel.py`、旧“启动控制面板”脚本只兼容启动同一个服务。`fix_memory_time.py` 兼容调用保守迁移工具，不再统一偏移全部时间。`metrics_excel.py` 为可选指标导出工具，与 bot 运行无关，需要 `openpyxl`。

软件代码采用 Apache License 2.0，详见 [LICENSE](LICENSE)。项目伦理约定单独保留于 [MISEI-ETHICS.md](MISEI-ETHICS.md)，与软件版权许可并行适用。
