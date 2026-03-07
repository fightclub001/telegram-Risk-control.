# 🚀 部署指南 - Railway + GitHub 完整步骤

## 📋 前置条件

- GitHub 账户
- Railway 账户（[railway.app](https://railway.app)）
- Telegram Bot Token（从 [@BotFather](https://t.me/botfather) 获取）
- 管理员用户 ID 和群组 ID

## 🔑 获取必要信息

### 1. 获取 Telegram Bot Token

1. 在 Telegram 中找到 [@BotFather](https://t.me/botfather)
2. 发送 `/newbot` 命令
3. 按照提示创建机器人
4. 复制返回的 Token（格式：`123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`）

### 2. 获取用户 ID（管理员）

方法一（快速）：
1. 向机器人发送任意消息
2. 使用工具查看日志中的用户 ID

方法二（通用）：
1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送 `/start`
3. 查看返回的用户 ID

### 3. 获取群组 ID

方法一（使用机器人）：
1. 将机器人加入目标群组
2. 在群组中发送 `/status`（如果部署了）
3. 群组 ID 会显示在日志中

方法二（手动）：
1. 在群组中发送任意消息
2. 转发该消息给 [@userinfobot](https://t.me/userinfobot)
3. 返回的消息中包含群组 ID（负数格式，如 `-1001234567890`）

## 📝 Step 1: 准备 GitHub 仓库

### A. 克隆或创建仓库

```bash
# 如果已有仓库，跳过此步

# 创建新仓库
mkdir telegram-bot
cd telegram-bot
git init
```

### B. 复制文件

将以下文件放入项目根目录：

```
telegram-bot/
├── main.py                 ← 主程序文件
├── requirements.txt        ← Python依赖
├── Procfile               ← Railway启动配置
├── railway.json           ← Railway项目配置
├── runtime.txt            ← Python版本指定
├── .gitignore             ← Git忽略文件
├── README.md              ← 项目说明
└── .github/
    └── workflows/
        └── check.yml      ← GitHub Actions自动检查
```

### C. 推送到 GitHub

```bash
git add .
git commit -m "Initial commit: Telegram bot setup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/telegram-bot.git
git push -u origin main
```

## 🚀 Step 2: Railway 部署

### A. 连接 GitHub 仓库

1. 登录 [Railway.app](https://railway.app)
2. 点击 **"New Project"** → **"Deploy from GitHub"**
3. 授权 Railway 访问你的 GitHub 账户
4. 选择 `telegram-bot` 仓库
5. 点击 **"Deploy Now"**

### B. 配置环境变量

部署后立即配置环境变量：

1. 在 Railway 项目面板中，点击你的服务
2. 找到 **"Variables"** 标签
3. 添加以下环境变量：

| 变量名 | 值 | 示例 |
|--------|-----|------|
| `BOT_TOKEN` | 你的机器人 Token | `123456:ABC-DEF...` |
| `ADMIN_IDS` | 管理员用户 ID（空格分隔） | `123456789 987654321` |
| `GROUP_IDS` | 群组 ID（空格分隔） | `-1001234567890 -1009876543210` |

4. 每个变量添加后按 **Enter** 确认

```
示例：
BOT_TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz1234567890"
ADMIN_IDS = "123456789 987654321"
GROUP_IDS = "-1001234567890"
```

### C. 查看部署状态

1. 在 Railway 仪表板上观察部署进度
2. 点击 **"Logs"** 查看实时日志
3. 看到 **"机器人启动成功"** 消息表示部署完成 ✅

## ✅ Step 3: 验证机器人

### A. 测试基本功能

1. 打开 Telegram，找到你的机器人
2. 点击 **"START"** 或输入 `/admin`
3. 应该看到 **"👮 管理员控制面板"** 菜单

### B. 测试群组功能

1. 将机器人添加到配置的群组
2. **确保机器人是群组管理员**
3. 在群组中发送测试消息
4. 查看机器人是否正常工作

### C. 测试管理功能

1. 在私聊中发送 `/admin`
2. 选择 **"⚙️ 群组管理"**
3. 选择一个群组
4. 尝试修改配置

## 🔄 更新代码

### 使用 GitHub + Railway 自动更新

1. 在本地修改代码
2. 提交并推送到 GitHub：
   ```bash
   git add .
   git commit -m "Update feature"
   git push origin main
   ```
3. Railway 会**自动检测**并重新部署
4. 检查日志确认更新完成

## ⚙️ 配置说明

### 环境变量详解

#### BOT_TOKEN
- **说明**：Telegram 机器人 Token
- **获取**：[@BotFather](https://t.me/botfather) 的 `/newbot` 命令
- **格式**：`123456:ABCDEFghijklmnopqrstuvwxyz...`

#### ADMIN_IDS
- **说明**：管理员用户 ID 列表
- **格式**：用空格分隔的整数
- **示例**：`123456789 987654321`
- **作用**：只有这些用户可以使用 `/admin` 命令

#### GROUP_IDS
- **说明**：需要监控的群组 ID 列表
- **格式**：用空格分隔的负整数或正整数
- **示例**：`-1001234567890 -1009876543210` 或 `1234567890 9876543210`
- **作用**：机器人只在这些群组中进行检测

### 群组权限要求

机器人需要以下权限（推荐设置为管理员）：
- ✅ 发送消息
- ✅ 编辑消息  
- ✅ 删除消息
- ✅ 限制成员（禁言）
- ✅ 查看成员列表

**设置步骤**：
1. 打开群组 → **"管理员和权限"**
2. 找到机器人
3. 启用上述权限
4. 保存

## 🐛 故障排查

### 问题：机器人无法启动

**症状**：Railway 日志显示错误

**解决方案**：
1. 检查 `BOT_TOKEN` 是否正确
2. 检查 `ADMIN_IDS` 和 `GROUP_IDS` 格式
3. 查看 Railway 日志找到具体错误
4. 确保 `main.py` 文件完整且无语法错误

### 问题：/admin 命令无响应

**症状**：发送 `/admin` 后无反应

**解决方案**：
1. 检查你的用户 ID 是否在 `ADMIN_IDS` 中
2. 在**私聊**中发送命令（不是在群组中）
3. 查看 Railway 日志是否有错误信息

### 问题：群组检测无效

**症状**：机器人加入群组但无反应

**解决方案**：
1. 确保群组 ID 在 `GROUP_IDS` 中
2. **确保机器人是群组管理员**
3. 检查机器人是否被禁言
4. 查看日志中是否有权限错误

### 问题：Railway 部署失败

**症状**：Railway 显示部署失败

**解决方案**：
1. 检查 `requirements.txt` 依赖是否正确
2. 查看部署日志找到具体错误
3. 确保代码没有语法错误
4. 重新尝试部署：点击 **"Redeploy"** 按钮

### 问题：自动更新不工作

**症状**：推送代码到 GitHub 后 Railway 没有更新

**解决方案**：
1. 检查 Railway 与 GitHub 的连接状态
2. 手动点击 Railway 的 **"Redeploy"** 按钮
3. 查看 GitHub Actions 是否有报错

## 📊 监控

### 查看 Railway 日志

1. 进入 Railway 项目
2. 点击你的服务
3. 选择 **"Logs"** 标签
4. 可以看到实时日志

关键日志信息：
```
🚀 机器人启动成功           ← 启动成功
加载配置失败: ...           ← 配置问题
检测到疑似广告引流规避      ← 检测触发
管理员 xxx 对 yyy 执行封禁   ← 管理操作
```

## 💾 数据备份

数据存储在 `/data/` 目录：
- `config.json` - 所有群组配置
- `reports.json` - 举报记录

**备份建议**：
- Railway 提供持久存储
- 数据会在重新部署后保留
- 建议定期导出配置作为备份

## 📱 常见操作

### 添加新的群组

1. 获取新群组的 ID
2. 编辑 Railway 的 `GROUP_IDS` 环境变量
3. 将新 ID 添加到列表（空格分隔）
4. 保存并重新部署

### 临时禁用机器人

1. 进入 Railway 仪表板
2. 点击 **"Remove"** 停止服务
3. 需要重新启动时点击 **"Redeploy"**

### 修改管理员

1. 更新 Railway 的 `ADMIN_IDS` 环境变量
2. 保存（无需重新部署，实时生效）

## ✨ 最佳实践

1. **定期备份配置** - 导出 `config.json`
2. **监控日志** - 定期查看 Railway 日志
3. **测试更新** - 在推送前在本地测试
4. **管理员权限** - 定期检查机器人管理员权限
5. **群组维护** - 定期清理过期的群组 ID

## 🎉 部署完成！

恭喜！你的机器人现在已经部署并运行。

### 接下来：

1. 在群组中使用 `/admin` 配置机器人
2. 调整检测参数以适应你的需求
3. 监控日志确保一切正常
4. 根据需要添加更多群组

## 📞 技术支持

如遇问题，检查以下内容：

1. ✅ 环境变量是否正确设置
2. ✅ 群组 ID 和用户 ID 格式是否正确
3. ✅ 机器人是否有群组管理员权限
4. ✅ Railway 日志中是否有错误信息
5. ✅ GitHub 与 Railway 的连接是否正常

## 📄 相关链接

- 🤖 [BotFather](https://t.me/botfather) - 创建和管理机器人
- 👤 [UserInfoBot](https://t.me/userinfobot) - 获取用户和群组 ID
- 🚀 [Railway.app](https://railway.app) - 云部署平台
- 📚 [aiogram 文档](https://docs.aiogram.dev/) - 机器人框架文档

---

**祝部署顺利！** 🚀
