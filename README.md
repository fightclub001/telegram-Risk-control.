# Telegram 群组监控机器人 - 生产级版本

生产级、功能完整的 Telegram 群组内容监控机器人，支持举报系统、管理员控制面板和动态配置管理。

## 📋 功能特性

### 核心功能
- ✅ **自动内容检测** - 基于关键词的显示名和简介检测
- ✅ **举报系统** - 群成员可举报可疑用户，支持多人举报
- ✅ **禁言管理** - 支持 24 小时禁言和永久限制
- ✅ **管理员面板** - 完整的 Web 化操作面板
- ✅ **参数管理** - 除三个核心变量外，所有参数都可动态调整
- ✅ **数据持久化** - 完整的数据备份和恢复机制
- ✅ **日志系统** - 结构化分级日志，便于调试

### 管理员面板功能
- 🔧 **配置管理** - 实时修改 18+ 个配置参数
- 🔍 **关键词管理** - 添加/删除/查看敏感词
- 🚫 **黑名单管理** - 配置群组黑名单规则
- 📊 **统计信息** - 查看系统运行状态
- 💾 **数据备份** - 一键备份所有数据

## 🚀 快速开始

### 前置要求
- Python 3.8+
- Telegram Bot Token（从 @BotFather 获取）
- Railway 账户（用于部署）或本地 Linux 环境

### 本地运行

#### 1. 克隆或下载项目

```bash
git clone <your-repo-url>
cd telegram-bot
```

#### 2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows
```

#### 3. 安装依赖

```bash
pip install -r requirements.txt
```

#### 4. 配置环境变量

创建 `.env` 文件或直接导出环境变量：

```bash
# 必需的三个变量（不可动态修改）
export BOT_TOKEN="your_bot_token_here"
export GROUP_IDS="123456789 987654321"  # 多个群组用空格分隔
export ADMIN_IDS="111222333 444555666"  # 多个管理员用空格分隔

# 可选：日志级别
export LOG_LEVEL="INFO"  # DEBUG/INFO/WARNING/ERROR

# 可选：数据目录
export CONFIG_DIR="/data"  # 默认为 /data
```

#### 5. 运行机器人

```bash
python main.py
```

### Railway 部署

#### 1. 创建 Railway 项目

```bash
railway init
```

#### 2. 配置环境变量

```bash
railway variables set BOT_TOKEN="your_token"
railway variables set GROUP_IDS="123456789"
railway variables set ADMIN_IDS="111222333"
```

#### 3. 部署

```bash
railway up
```

#### 4. 查看日志

```bash
railway logs
```

## 📖 使用指南

### 启动管理员面板

在 Telegram 中向机器人发送：

```
/admin
```

或点击下方菜单按钮（如果已配置）。

### 配置参数

#### 配置分类

1. **🧹 清理任务**
   - `cleanup_check_interval` - 清理检查间隔（秒）
   - `report_expiry_time` - 举报记录过期时间（秒）
   - `deleted_message_cleanup_delay` - 删除消息延迟（秒）
   - `max_reports_in_memory` - 最多保留举报数
   - `batch_cleanup_size` - 批量清理消息数

2. **📊 举报系统**
   - `auto_ban_threshold` - 自动通知管理员的举报阈值
   - `ban_duration_24h` - 24 小时禁言时长（秒）
   - `ban_duration_week` - 1 周禁言时长（秒）

3. **⚡ 速率限制**
   - `rate_limit_window` - 速率限制窗口（秒）
   - `max_reports_per_hour` - 每小时最多举报次数
   - `max_keyword_queries_per_hour` - 每小时关键词查询次数

4. **🔍 关键词检测**
   - `enable_bio_check` - 是否启用简介检查
   - `enable_display_name_check` - 是否启用显示名检查
   - `enable_fuzzy_match` - 是否启用模糊匹配（实验功能）

5. **💬 消息管理**
   - `enable_delete_after_ban` - 禁言后是否删除消息
   - `delete_warning_timeout` - 删除警告消息延迟（秒）
   - `warning_message_timeout` - 警告消息保留时间（秒）

6. **🚫 黑名单**
   - `default_blacklist_duration` - 默认黑名单时长（秒）
   - `enable_auto_blacklist` - 是否启用自动黑名单

7. **📝 日志配置**
   - `log_level` - 日志级别（DEBUG/INFO/WARNING/ERROR）
   - `log_retention_days` - 日志保留天数

8. **⚙️ 性能配置**
   - `max_concurrent_operations` - 最多并发操作数
   - `api_call_timeout` - API 调用超时（秒）

### 关键词管理

在管理面板中选择 "🔍 关键词管理"：

- **➕ 添加关键词** - 输入新的敏感词
- **➖ 删除关键词** - 从现有列表删除
- **📋 查看所有关键词** - 列出所有敏感词

### 黑名单配置

在管理面板中选择 "🚫 黑名单管理"：

- **⚙️ 配置黑名单** - 为群组设置黑名单规则
- **📋 查看配置** - 查看当前黑名单配置

## 🔒 安全性说明

### 不可修改的变量

以下三个变量通过环境变量设置，**不能在管理面板中修改**，需要修改时必须重新部署：

- `BOT_TOKEN` - 机器人令牌
- `GROUP_IDS` - 监控群组列表
- `ADMIN_IDS` - 管理员列表

这样设计是为了防止管理员错误操作导致机器人无法运行或访问控制丧失。

### 权限控制

- 只有 `ADMIN_IDS` 中的用户可以访问管理面板
- 所有管理操作都会被记录到日志
- 配置修改自动保存，带有时间戳

### 数据安全

- 所有数据使用原子操作保存（先写临时文件，再重命名）
- 损坏的文件自动备份
- 支持一键数据备份

## 📊 监控和调试

### 查看日志

本地运行时，日志输出到：
- 控制台（实时）
- `/data/logs/bot.log`（INFO 级别）
- `/data/logs/error.log`（ERROR 级别）

Railway 部署时，使用：
```bash
railway logs
```

### 统计信息

在管理面板选择 "📊 统计信息" 查看：
- 监控群组数
- 管理员数
- 当前举报数
- 敏感词数量
- 黑名单配置数

## 🛠️ 故障排除

### 机器人无响应

1. 检查 `BOT_TOKEN` 是否正确
2. 查看日志是否有错误信息
3. 确认机器人是否在监控的群组中

### 命令无响应

1. 确认用户 ID 在 `ADMIN_IDS` 中
2. 检查私聊是否已启用
3. 查看 `enable_*` 配置是否被禁用

### 数据丢失

1. 检查 `/data` 目录是否可写
2. 查看日志中的保存错误
3. 使用 "📊 统计信息" 检查数据状态

### Railway 部署失败

1. 检查 `requirements.txt` 是否完整
2. 确认 `Procfile` 格式正确
3. 查看 Railway 日志中的错误信息

## 🔄 更新和维护

### 更新依赖

```bash
pip install --upgrade aiogram
```

### 备份数据

手动备份 `/data` 目录：

```bash
tar -czf backup_$(date +%Y%m%d_%H%M%S).tar.gz /data/
```

或使用管理面板的自动备份功能。

## 📝 日志说明

### 日志级别

- **DEBUG** - 详细调试信息
- **INFO** - 一般信息消息
- **WARNING** - 警告信息
- **ERROR** - 错误信息

### 日志轮转

- `bot.log` - 每天午夜自动轮转，保留 7 天
- `error.log` - 大小达到 10MB 时轮转，保留 5 个备份

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📞 技术支持

如有问题，请：
1. 检查日志寻找错误信息
2. 查看本 README 的故障排除部分
3. 提交 Issue 详细描述问题

---

**最后更新**: 2024 年

**版本**: 2.0.0 (生产级)

**兼容性**: aiogram >= 3.14.0, Python >= 3.8
