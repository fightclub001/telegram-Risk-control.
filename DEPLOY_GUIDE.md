# 📖 完整部署和配置指南

## 目录
1. [快速开始](#快速开始)
2. [Railway 部署](#railway-部署)
3. [本地开发](#本地开发)
4. [Docker 部署](#docker-部署)
5. [配置参数详解](#配置参数详解)
6. [常见问题](#常见问题)
7. [故障排除](#故障排除)

---

## 快速开始

### 前置要求

- Telegram Bot Token（从 @BotFather 获取）
- 群组 ID（使用 @get_id_bot 或 @userinfobot 获取）
- 管理员用户 ID

### 获取必要信息

#### 1. 获取 Bot Token

1. 在 Telegram 中找到 @BotFather
2. 发送 `/newbot` 命令
3. 按提示输入机器人名称和用户名
4. 复制生成的 Token

示例：`123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`

#### 2. 获取群组 ID

添加 @get_id_bot 到你的群组，它会自动显示群组 ID。

或使用 @userinfobot 获取你的用户 ID。

#### 3. 测试权限

确保：
- 机器人是群组的管理员
- 机器人有删除消息、限制用户等权限

---

## Railway 部署

### 方案一：Web 界面部署（推荐）

#### 1. 准备 GitHub 仓库

```bash
# 创建本地仓库
git init
git add .
git commit -m "Initial commit"

# 推送到 GitHub（需要先创建仓库）
git remote add origin https://github.com/your-username/telegram-bot.git
git branch -M main
git push -u origin main
```

#### 2. 连接 Railway

1. 访问 https://railway.app
2. 登录/注册 GitHub 账户
3. 点击 "Deploy from GitHub"
4. 选择你的仓库
5. Railway 自动检测并部署

#### 3. 配置环境变量

在 Railway 项目中：

1. 进入 "Variables" 标签页
2. 添加以下变量：

```
BOT_TOKEN=你的_token
GROUP_IDS=123456789 987654321
ADMIN_IDS=111222333
LOG_LEVEL=INFO
```

3. 点击 "Deploy" 开始部署

#### 4. 查看日志

```bash
# 使用 Railway CLI
railway login
railway link
railway logs
```

### 方案二：CLI 部署

```bash
# 安装 Railway CLI
npm install -g @railway/cli

# 登录
railway login

# 创建项目
railway init

# 设置环境变量
railway variables set BOT_TOKEN="your_token"
railway variables set GROUP_IDS="123456789"
railway variables set ADMIN_IDS="111222333"

# 部署
railway up

# 查看日志
railway logs
```

### 管理部署

```bash
# 查看服务状态
railway status

# 查看最近的部署
railway deployments

# 重启服务
railway restart

# 查看环境变量
railway variables list

# 更新环境变量
railway variables set KEY=value
```

---

## 本地开发

### 方案一：直接运行

#### 1. 克隆项目

```bash
git clone <your-repo-url>
cd telegram-bot
```

#### 2. 创建虚拟环境

```bash
# Linux/Mac
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

#### 3. 安装依赖

```bash
pip install -r requirements.txt
```

#### 4. 配置环境

创建 `.env` 文件：

```bash
cp .env.example .env
# 编辑 .env 文件，填入实际值
```

或导出环境变量：

```bash
export BOT_TOKEN="your_token"
export GROUP_IDS="123456789"
export ADMIN_IDS="111222333"
```

#### 5. 运行部署检查

```bash
python check_deploy.py
```

输出示例：
```
============================================================
🚀 Telegram 机器人部署前检查
============================================================

检查 Python 版本... ✅ Python 3.11.0
检查依赖... ✅ 所有依赖已安装
检查环境变量... ✅ 环境变量已配置
检查 Bot Token... ✅ Bot Token 格式正确
检查 Group IDs... ✅ 已配置 2 个群组
检查 Admin IDs... ✅ 已配置 2 个管理员
检查文件... ✅ 所有必需文件存在
检查数据目录... ✅ 已创建: /data
检查文件权限... ✅ 目录可写: /data

============================================================
✅ 所有检查通过！(9/9)
============================================================
```

#### 6. 运行机器人

```bash
python main.py
```

预期输出：
```
============================================================
🚀 Telegram 机器人启动中...
============================================================
✅ 环境变量验证成功: 2 个群组, 2 个管理员
✅ 数据加载完成: 0 举报, 25 关键词, 0 黑名单配置
✅ 日志系统已初始化 (级别: INFO)
📡 开始轮询 Telegram 服务器...
```

### 方案二：Docker 运行（推荐用于测试）

#### 1. 安装 Docker 和 Docker Compose

参考官方文档安装。

#### 2. 配置环境

```bash
cp .env.example .env
# 编辑 .env 文件
```

#### 3. 启动容器

```bash
docker-compose up -d
```

#### 4. 查看日志

```bash
docker-compose logs -f bot
```

#### 5. 停止容器

```bash
docker-compose down
```

#### 其他命令

```bash
# 重启
docker-compose restart

# 查看容器状态
docker-compose ps

# 进入容器
docker-compose exec bot bash

# 查看容器详细信息
docker-compose logs bot --tail=100
```

---

## Docker 部署

### 方案一：构建本地镜像

```bash
# 构建镜像
docker build -t telegram-bot:latest .

# 运行容器
docker run -d \
  --name telegram_bot \
  --restart unless-stopped \
  -e BOT_TOKEN="your_token" \
  -e GROUP_IDS="123456789" \
  -e ADMIN_IDS="111222333" \
  -v $(pwd)/data:/app/data \
  telegram-bot:latest

# 查看日志
docker logs -f telegram_bot

# 停止容器
docker stop telegram_bot
docker rm telegram_bot
```

### 方案二：使用 Docker Hub（部署到远程）

```bash
# 登录 Docker Hub
docker login

# 构建并标记镜像
docker build -t your-username/telegram-bot:latest .

# 推送到 Docker Hub
docker push your-username/telegram-bot:latest

# 在远程服务器上运行
docker run -d \
  --name telegram_bot \
  --restart unless-stopped \
  -e BOT_TOKEN="your_token" \
  -e GROUP_IDS="123456789" \
  -e ADMIN_IDS="111222333" \
  -v /data:/app/data \
  your-username/telegram-bot:latest
```

---

## 配置参数详解

### 环境变量（启动时设置）

#### 必需配置

| 变量 | 说明 | 示例 |
|------|------|------|
| `BOT_TOKEN` | Telegram Bot Token | `123456:ABC-DEF1234ghIkl` |
| `GROUP_IDS` | 监控群组 ID（空格分隔） | `123456789 987654321` |
| `ADMIN_IDS` | 管理员 ID（空格分隔） | `111222333 444555666` |

#### 可选配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LOG_LEVEL` | 日志级别 (DEBUG/INFO/WARNING/ERROR) | `INFO` |
| `CONFIG_DIR` | 数据存储目录 | `/data` |

### 动态配置参数（管理面板修改）

#### 1. 清理任务

```
cleanup_check_interval = 600           # 清理检查间隔（秒）
report_expiry_time = 3600              # 举报记录过期时间（秒）
deleted_message_cleanup_delay = 10     # 删除警告消息延迟（秒）
max_reports_in_memory = 1000           # 最多保留举报数
batch_cleanup_size = 5                 # 批量清理消息数
```

**推荐配置**：
- 小群组：`cleanup_check_interval=600, report_expiry_time=1800`
- 大群组：`cleanup_check_interval=1200, report_expiry_time=7200`

#### 2. 举报系统

```
auto_ban_threshold = 3                 # 自动通知管理员阈值
ban_duration_24h = 86400               # 24小时禁言时长（秒）
ban_duration_week = 604800             # 1周禁言时长（秒）
```

#### 3. 速率限制

```
rate_limit_window = 3600               # 限制窗口（秒）
max_reports_per_hour = 5               # 每小时最多举报次数
max_keyword_queries_per_hour = 10      # 每小时最多查询次数
```

**建议**：
- `max_reports_per_hour` 设置为 5-10，防止用户滥用
- `rate_limit_window` 根据群组活跃度调整

#### 4. 关键词检测

```
enable_bio_check = True                # 启用简介检查
enable_display_name_check = True       # 启用显示名检查
enable_fuzzy_match = False             # 启用模糊匹配（实验）
```

#### 5. 消息管理

```
enable_delete_after_ban = True         # 禁言后删除消息
delete_warning_timeout = 10            # 删除警告延迟（秒）
warning_message_timeout = 3600         # 警告保留时间（秒）
```

#### 6. 日志配置

```
log_level = "INFO"                     # 日志级别
log_retention_days = 7                 # 日志保留天数
```

---

## 常见问题

### Q1：如何修改参数？

A：通过管理面板修改：
1. 向机器人发送 `/admin`
2. 点击 "⚙️ 配置管理"
3. 选择分类和参数
4. 输入新值或选择新值

所有修改自动保存。

### Q2：三个环境变量能修改吗？

A：**不能**。这三个变量（`BOT_TOKEN`、`GROUP_IDS`、`ADMIN_IDS`）为了安全考虑，只能通过环境变量设置，需要修改时必须重新部署。

### Q3：如何添加新的敏感词？

A：通过管理面板：
1. `/admin` → "🔍 关键词管理"
2. "➕ 添加关键词"
3. 输入敏感词

### Q4：机器人不响应怎么办？

A：
1. 检查 `BOT_TOKEN` 是否正确
2. 确认机器人是群组的管理员
3. 查看日志：`docker logs bot` 或 `railway logs`
4. 检查网络连接

### Q5：如何备份数据？

A：
1. 方式一：管理面板 → "📊 统计信息" → "🔄 数据备份"
2. 方式二：直接复制 `/data` 目录
3. 方式三：`docker cp container_id:/app/data ./backup`

### Q6：如何恢复数据？

A：
1. 将备份文件覆盖到 `/data` 目录
2. 重启机器人

### Q7：如何查看日志？

A：
```bash
# 本地运行
tail -f /data/logs/bot.log

# Docker 运行
docker logs -f container_id

# Railway 部署
railway logs
```

### Q8：关键词匹配区分大小写吗？

A：不区分。所有匹配都转换为小写进行比较。

### Q9：可以同时监控多个群组吗？

A：可以。在 `GROUP_IDS` 中填入多个群组 ID（空格分隔）。

### Q10：如何测试机器人是否正常运行？

A：
1. 在群组中发送测试消息
2. 查看是否有警告消息
3. 点击举报按钮测试
4. 查看管理面板统计信息

---

## 故障排除

### 问题：Bot Token 无效

**症状**：启动时出错 `Unauthorized`

**解决**：
1. 检查 Token 是否正确复制
2. 确保 Token 没有过期（需要重新获取）
3. 检查是否有特殊字符被误删

### 问题：找不到群组

**症状**：机器人加入群组但不工作

**解决**：
1. 确认 GROUP_IDS 中的 ID 正确
2. 使用 @get_id_bot 验证群组 ID
3. 确保机器人是管理员

### 问题：权限不足

**症状**：删除/禁言消息失败

**解决**：
1. 确保机器人是管理员
2. 确保机器人有以下权限：
   - 删除消息
   - 限制成员
   - 更改群组信息

### 问题：数据丢失

**症状**：重启后举报记录消失

**解决**：
1. 检查 `/data` 目录是否可写
2. 检查磁盘空间是否充足
3. 查看日志中的保存错误
4. 恢复备份数据

### 问题：内存占用过高

**症状**：机器人运行缓慢或崩溃

**解决**：
1. 减少 `max_reports_in_memory` 值
2. 增加 `cleanup_check_interval` 频率
3. 检查日志中的错误

### 问题：Railway 部署失败

**症状**：部署卡住或出现错误

**解决**：
```bash
# 检查日志
railway logs

# 重新部署
railway deploy

# 查看构建过程
railway logs --tail=100

# 检查环境变量
railway variables list
```

### 问题：无法连接 Telegram

**症状**：日志中出现连接错误

**解决**：
1. 检查网络连接
2. 检查防火墙设置
3. 如果在国内，可能需要配置代理

### 问题：关键词不匹配

**症状**：应该被检测的用户没有被警告

**解决**：
1. 检查关键词是否正确添加
2. 确认 `enable_display_name_check` 已启用
3. 注意关键词是小写的
4. 查看日志中的匹配信息

---

## 性能优化建议

### 对于小群组（<1000 成员）

```
cleanup_check_interval = 600           # 10分钟检查一次
report_expiry_time = 1800              # 30分钟过期
max_reports_in_memory = 500
```

### 对于中等群组（1000-10000 成员）

```
cleanup_check_interval = 900           # 15分钟检查一次
report_expiry_time = 3600              # 1小时过期
max_reports_in_memory = 1000
```

### 对于大群组（>10000 成员）

```
cleanup_check_interval = 1200          # 20分钟检查一次
report_expiry_time = 7200              # 2小时过期
max_reports_in_memory = 2000
```

---

## 更新和维护

### 更新依赖

```bash
# 本地开发
pip install --upgrade -r requirements.txt

# Docker
docker build --no-cache -t telegram-bot:latest .
```

### 定期备份

建议每周备份一次：

```bash
# 本地
tar -czf backup_$(date +%Y%m%d).tar.gz /data/

# Docker
docker cp bot:/app/data ./backup_$(date +%Y%m%d)/
```

### 日志清理

日志自动轮转和清理，保留 7 天。手动清理：

```bash
# 删除旧日志
rm /data/logs/bot.log.*

# 查看日志大小
du -sh /data/logs/
```

---

## 联系和支持

有问题？

1. 检查本文的故障排除部分
2. 查看日志寻找错误信息
3. 提交 Issue 详细描述问题

---

**最后更新**: 2024 年

**版本**: 2.0.0
