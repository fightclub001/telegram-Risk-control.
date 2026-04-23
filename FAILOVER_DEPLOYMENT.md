# 本地 Ubuntu 主运行 + Railway 免费兜底

## 目标

- 主实例运行在家里的 Ubuntu 主机。
- Railway 免费实例默认待机，不主动接管。
- 通过 Cloudflare Worker + KV 保存极轻量心跳。
- 只要 Ubuntu 心跳超时，Railway 自动启动 `main.py`。
- Ubuntu 恢复后，Railway 自动退出待机 bot，避免双实例同时处理消息。

## 组件

1. `main.py`
   - 真正的 Telegram bot 进程。
2. `local_heartbeat_sender.py`
   - Ubuntu 主机每 15 秒上报一次心跳到 Cloudflare Worker。
3. `railway_failover_runner.py`
   - Railway 轮询 Worker 的 `/status`。
   - 主节点健康时不启动 bot。
   - 主节点失联连续 N 次后才启动 bot（防抖，避免公网瞬断误判）。
   - 主节点恢复后按恢复阈值停止 Railway bot（默认 1 次健康即停）。
4. `cloudflare-heartbeat-worker.js`
   - Worker 提供 `/heartbeat` 和 `/status` 两个接口。
   - 使用 KV 存储最近一次心跳。

## 当前推荐方案

- Ubuntu 本机运行 bot。
- Ubuntu 额外起一个只读健康接口，检查 `telegram-risk-control.service` 是否 `active`。
- 通过 Cloudflare Tunnel 暴露健康接口为单独 hostname。
- Railway 的 `railway_failover_runner.py` 直接轮询这个公开健康地址。

这样不需要额外的云端状态数据库，也不需要把运行数据实时写上云。

## Cloudflare 侧部署

### 1. 创建 Worker 和 KV

- 新建一个 Cloudflare Worker。
- 绑定一个 KV namespace，变量名固定为 `HEARTBEAT_KV`。
- 将 `cloudflare-heartbeat-worker.js` 作为 Worker 代码。
- 可直接从 `wrangler.toml.example` 复制出一份本地部署配置。

如果不想额外申请 Cloudflare Workers API token，也可以跳过 Worker/KV，直接使用上面的“公开健康接口”方案。

### 2. 配置 Worker 环境变量

- `HEARTBEAT_WRITE_TOKEN`
- `HEARTBEAT_READ_TOKEN`

建议：

- 读写 token 分开。
- token 长度至少 32 字符。
- 若后续需要管理页，再放到 Cloudflare Access 后面，不直接裸奔。

### 3. 推荐接口

- `POST /heartbeat`
- `GET /status?node_id=ubuntu-main&max_age=90`
- `GET /healthz`

## Ubuntu 主机部署

### 1. bot 主进程

主 bot 继续正常运行：

```bash
python main.py
```

### 2. 心跳进程

单独跑一个轻量 sender：

```bash
python local_heartbeat_sender.py
```

建议在 Ubuntu 上最终做成两个 `systemd` 服务：

- `telegram-risk-control.service`
- `telegram-risk-heartbeat.service`
- `telegram-risk-health.service`

这样 bot 崩了和心跳崩了可以单独拉起。

仓库里已经准备好对应模板：

- `deploy/ubuntu/telegram-risk-control.service`
- `deploy/ubuntu/telegram-risk-heartbeat.service`
- `deploy/ubuntu/telegram-risk-health.service`
- `deploy/ubuntu/setup_ubuntu_bot.sh`

### 3. Ubuntu 所需环境变量

最少新增：

```bash
PRIMARY_NODE_ID=ubuntu-main
HEARTBEAT_WRITE_URL=https://your-worker.example.workers.dev/heartbeat
HEARTBEAT_WRITE_TOKEN=replace-with-write-token
HEARTBEAT_INTERVAL_SEC=15
HEARTBEAT_TIMEOUT_SEC=5
```

## Railway 兜底部署

现在 `railway.json` 和 `Procfile` 都已经改成：

```bash
python railway_failover_runner.py
```

逻辑如下：

- 未配置 `HEARTBEAT_STATUS_URL` 时，Railway 直接运行 `main.py`。
- 配置后，Railway 只在主节点失联时接管。

### Railway 需要的新增环境变量

```bash
PRIMARY_NODE_ID=ubuntu-main
HEARTBEAT_STATUS_URL=https://your-worker.example.workers.dev/status
HEARTBEAT_STATUS_TOKEN=replace-with-read-token
HEARTBEAT_MAX_AGE_SEC=90
HEARTBEAT_POLL_SEC=15
HEARTBEAT_TIMEOUT_SEC=5
HEARTBEAT_FAIL_CONFIRM_LOOPS=3
HEARTBEAT_RECOVER_CONFIRM_LOOPS=1
```

补充一个更稳的控制项：

```bash
# Ubuntu 健康接口还活着时，先保留 Ubuntu 主导 5 分钟
# 只有持续失健康超过这个窗口，Railway 才允许接管
HEALTH_FAILOVER_GRACE_SEC=300
```

### 配置强一致（推荐开启）

为避免“本地是 2、Railway 还是 50”这类漂移，现在支持把 Railway volume 当作同步中心：

- Railway `railway_failover_runner.py` 提供受保护接口：
  - `GET /config` 读取 Railway volume 的 `config.json`
  - `PUT /config` 写入 Railway volume 的 `config.json`
  - `GET /image-fuzzy-blocks` 读取 Railway volume 的 `image_fuzzy_blocks.json`
  - `PUT /image-fuzzy-blocks` 写入 Railway volume 的 `image_fuzzy_blocks.json`
  - `GET /state-manifest` 返回管理员可编辑状态清单（带 `mtime_ns` / `sha256`）
  - `GET /state-bundle` 下载完整管理员状态包
  - `PUT /state-bundle` 写入完整管理员状态包
- Ubuntu `main.py` 支持：
  - 启动时先对账本地/远端管理员状态，而不是盲目用远端覆盖本地
  - 自动同步以下管理员面板可编辑数据：
    - `config.json`
    - `image_fuzzy_blocks.json`
    - `semantic_ads/semantic_ads.db`
  - 当两端文件不同步时，按文件级 `mtime_ns + sha256` 判断方向
  - 每次配置变更、关键图样本变更、广告词库学习/删除后，异步推送最新状态包到 Railway volume

建议变量：

```bash
# Ubuntu + Railway 统一设置同一个 token
CONFIG_SYNC_TOKEN=replace-with-strong-shared-token

# Ubuntu 上设置：指向 Railway 服务公开域名
CONFIG_SYNC_URL=https://<your-railway-domain>/config
# 可选：不填时自动由 CONFIG_SYNC_URL 推导为 /image-fuzzy-blocks
IMAGE_FUZZY_SYNC_URL=https://<your-railway-domain>/image-fuzzy-blocks
# 可选：不填时自动由 CONFIG_SYNC_URL 推导为 /state-manifest 和 /state-bundle
STATE_SYNC_MANIFEST_URL=https://<your-railway-domain>/state-manifest
STATE_SYNC_BUNDLE_URL=https://<your-railway-domain>/state-bundle
CONFIG_SYNC_TIMEOUT_SEC=5
CONFIG_SYNC_PULL_ON_START=true
CONFIG_SYNC_PUSH_ON_SAVE=true

# Railway 上可选（默认即可）
CONFIG_SYNC_PORT=8080
CONFIG_SYNC_PATH=/config
IMAGE_FUZZY_SYNC_PATH=/image-fuzzy-blocks
STATE_SYNC_MANIFEST_PATH=/state-manifest
STATE_SYNC_BUNDLE_PATH=/state-bundle
```

## 切换策略

### 主节点健康

- Ubuntu 持续发心跳。
- Railway 只做轮询，不运行 bot。
- 现在只要 Ubuntu 健康接口还能正常返回，且未超过 `HEALTH_FAILOVER_GRACE_SEC`，Railway 不会抢主。

### 主节点失联

触发条件示例：

- 断电
- 断网
- Ubuntu bot 进程挂了，且心跳服务也停止
- 家庭网络波动超过 `HEARTBEAT_MAX_AGE_SEC`

此时 Railway 自动拉起 bot。

### Ubuntu 可通信但短暂失健康

- 例如你在 Ubuntu 上短暂停 bot、重启服务、调试别的项目导致网络探针瞬时失败。
- 健康接口会返回 `healthy=false`，但在 `HEALTH_FAILOVER_GRACE_SEC` 时间窗内同时返回 `failover_allowed=false`。
- Railway 收到这种响应时会继续让 Ubuntu 保持主导，不会进入 standby。

### 主节点恢复

- Ubuntu 恢复发送心跳。
- Railway 默认 1 次确认健康就主动停止 standby bot（可通过 `HEARTBEAT_RECOVER_CONFIRM_LOOPS` 调整）。

## 风险边界

### 1. 双活风险

如果 Ubuntu 的 bot 活着，但心跳 sender 死了，Railway 会误判接管，形成双活。

后续正式上 Ubuntu 时，建议：

- 把 bot 和 sender 放在同一个 `systemd` target 下管理。
- 或者增加本地 health endpoint，由 sender 先探测 bot 进程健康再发心跳。

### 2. Telegram webhook / polling 冲突

如果当前 bot 使用 long polling，同一 token 双实例同时运行会抢更新。

所以生产上必须确保：

- 主实例稳定发心跳。
- sender 和 bot 生命周期尽量绑定。

### 3. 免费层控制

当前公开健康接口方案只做：

- Railway 每 15 秒 1 次 `GET`
- Ubuntu 本地健康服务仅返回一个小 JSON

量级更低，也更适合现在这个场景。

## 下一步

等恢复到局域网并重新拿回 Ubuntu 控制权后，继续补：

1. Ubuntu `systemd` 服务文件
2. Cloudflare Zero Trust 私网入口
3. Ubuntu 上的本地部署目录和持久化数据目录
4. Railway 环境变量对齐和实测切换

---

## 发布门禁（强制快照 + 自动回滚）

新增脚本：`deploy/release_guard.py`

用途：

- 发布前自动快照管理员可编辑状态（`config.json`、`image_fuzzy_blocks.json`、`semantic_ads/semantic_ads.db`）
- 执行发布命令
- 发布后校验本地状态文件是否仍完整、并校验 Railway 远端 manifest 哈希是否一致
- 若任一步失败：自动恢复快照，并回推快照状态包到 Railway

### Ubuntu 推荐用法

```bash
cd /opt/telegram-risk-control/app
/opt/telegram-risk-control/venv/bin/python deploy/release_guard.py \
  --data-dir /opt/telegram-risk-control/data \
  --deploy-cmd "sudo systemctl restart telegram-risk-control.service telegram-risk-health.service" \
  --rollback-cmd "sudo systemctl restart telegram-risk-control.service telegram-risk-health.service"
```

说明：

- `STATE_SYNC_MANIFEST_URL` / `STATE_SYNC_BUNDLE_URL` / `CONFIG_SYNC_TOKEN` 可直接走当前环境变量。
- 如只配了 `CONFIG_SYNC_URL`，脚本会自动推导出 `/state-manifest` 与 `/state-bundle`。
- 快照默认落到：`<DATA_DIR>/backups/release-guard/`。

### CI/CD 或人工发布约束

- 禁止只做临时直传发布而不 `git push`。
- 若是 Railway GitHub 自动部署链路，必须先 push 到 `origin/main`，再观察自动部署日志。
- 发布后必须检查：
  - Ubuntu `status` 健康
  - Railway `/state-manifest` 返回非 `404`（正常应是 `200` 或未带 token 时 `401`）

## 强制双端代码同步

新增脚本：`deploy/publish_everywhere.py`

用途：

- 可只提交并推送本次指定运行文件，避免把工作区其它脏改动一起带上
- 先 `git push origin main`
- 再自动通过 SSH 把运行文件同步到 Ubuntu `/opt/telegram-risk-control/app`
- 自动在 Ubuntu 端先备份旧代码；若安装/重启/健康检查失败，自动回滚
- 自动执行远端 `py_compile`
- 优先自动重启 `telegram-risk-control.service` + `telegram-risk-health.service`
- 若没有 sudo 但服务进程归 `fightclub` 用户所有，可退化为进程轮换重启并等待 systemd 自恢复
- 自动等待 Ubuntu `/status` 健康
- 最后逐文件比对本地与 Ubuntu 的 `sha256`

这样以后不再允许出现“GitHub/Railway 已更新，但 Ubuntu 运行目录还是旧文件”的分叉。

### 推荐用法

Windows PowerShell：

```powershell
$env:UBUNTU_SUDO_PASSWORD="你的Ubuntu sudo密码"
python deploy/publish_everywhere.py --branch main --auto-commit --commit-message "你的本次发布说明"
```

默认同步这些运行文件：

- `main.py`
- `image_fuzzy_blocker.py`
- `semantic_ads.py`
- `railway_failover_runner.py`
- `deploy/release_guard.py`

说明：

- 若所选运行文件和 `HEAD` 不一致、但你又没传 `--auto-commit`，脚本会直接拒绝执行，避免 GitHub/Railway 与 Ubuntu 分叉。
- 若只想同步单个文件，可重复传 `--file`。
- 若某次只想同步 Ubuntu、不再 push GitHub，可加 `--skip-push`。
- 任何一步失败，脚本会自动尝试恢复 Ubuntu 端备份，再退出。
- 只有 push、Ubuntu 健康检查、以及远端文件哈希校验都通过，才算同步成功。
