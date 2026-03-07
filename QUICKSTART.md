# ⚡ 5分钟快速开始指南

## 🎯 目标
部署一个完全可配置的 Telegram 群组管理机器人到 Railway

## 📋 需要的信息（提前准备）

```
BOT_TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz..."      ← @BotFather 获取
ADMIN_IDS = "123456789 987654321"                       ← 你的用户ID
GROUP_IDS = "-1001234567890 -1009876543210"             ← 群组ID
```

**如何快速获取？**
- Bot Token: [@BotFather](https://t.me/botfather) → /newbot
- 用户 ID: [@userinfobot](https://t.me/userinfobot) → /start
- 群组 ID: [@userinfobot](https://t.me/userinfobot) → 转发群组消息

## ⚡ 快速部署步骤

### 步骤 1️⃣ : 上传到 GitHub（2分钟）

```bash
# 1. 在 GitHub 创建新仓库：telegram-bot

# 2. 本地操作
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/telegram-bot.git
git push -u origin main
```

### 步骤 2️⃣ : Railway 连接（1分钟）

1. 访问 [Railway.app](https://railway.app)
2. 登录 GitHub 账户
3. **"New Project"** → **"Deploy from GitHub repo"**
4. 选择 `telegram-bot` 仓库
5. **Deploy Now** ✅

### 步骤 3️⃣ : 配置环境变量（1分钟）

Railway 项目界面：

1. 点击你的服务名称
2. **"Variables"** 标签
3. 添加三个变量：

```
BOT_TOKEN = 你的Token
ADMIN_IDS = 你的ID（空格分隔多个）
GROUP_IDS = 群组ID（空格分隔多个）
```

4. 保存 ✅

### 步骤 4️⃣ : 等待部署（1分钟）

- 查看 **"Logs"** 标签
- 看到 **"🚀 机器人启动成功"** 就完成了 ✅

## ✅ 验证机器人工作

### 测试 1: 私聊管理员命令
```
在 Telegram 中给机器人发送：
/admin

应该看到：👮 管理员控制面板
```

### 测试 2: 群组检测
```
1. 机器人加入群组
2. 确保机器人是管理员
3. 发送测试消息观察反应
```

## 🎮 使用机器人

### 配置群组

```
私聊 → /admin
   ↓
选择"⚙️ 群组管理"
   ↓
选择要配置的群组
   ↓
选择要修改的功能（简介检测、短消息检测等）
   ↓
点击按钮或输入参数
```

### 所有可调参数

✅ **简介检测**
- 启用/禁用链接检测
- 启用/禁用敏感词检测
- 编辑敏感词列表

✅ **显示名称检测**
- 启用/禁用检测
- 编辑敏感词列表

✅ **短消息检测**
- 启用/禁用
- 字数阈值 (默认3字)
- 连续条数 (默认2条)
- 时间窗口 (默认60秒)

✅ **填充垃圾检测**
- 启用/禁用
- 最小原始长度 (默认12)
- 清理后最大长度 (默认8)
- 空格比例 (默认0.30)

✅ **自动回复**
- 启用/禁用
- 编辑关键词
- 编辑回复文本
- 编辑按钮
- 编辑删除延时

## 🚨 常见问题

### Q: /admin 没反应？
A: 
- ✅ 确保在**私聊**中发送
- ✅ 你的 ID 必须在 `ADMIN_IDS` 中
- ✅ 查看 Railway 日志是否有错误

### Q: 群组无法检测？
A:
- ✅ 群组 ID 必须在 `GROUP_IDS` 中
- ✅ 机器人必须是**群组管理员**
- ✅ 群组配置不能是禁用状态

### Q: 如何更新代码？
A:
```bash
# 修改本地文件后：
git add .
git commit -m "Update"
git push
# Railway 自动部署 ✅
```

### Q: 如何添加新群组？
A:
1. 获取群组 ID
2. Railway 界面修改 `GROUP_IDS` 变量
3. 保存（立即生效）

## 📞 快速参考

| 操作 | 位置 | 说明 |
|------|------|------|
| 查看日志 | Railway → Logs | 调试问题 |
| 修改配置 | Railway → Variables | 环境变量 |
| 更新代码 | git push | 自动部署 |
| 查看状态 | /admin → 📊 | 机器人状态 |
| 重新部署 | Railway → Redeploy | 手动重启 |

## 📁 项目结构

你会获得这些文件：

```
telegram-bot/
├── main.py                    ← 主程序 ⭐
├── requirements.txt           ← 依赖
├── Procfile                   ← Railway启动
├── railway.json               ← Railway配置
├── runtime.txt                ← Python版本
├── .gitignore                 ← Git忽略
├── README.md                  ← 详细说明
├── DEPLOYMENT_GUIDE.md        ← 部署指南
└── .github/workflows/
    └── check.yml              ← 自动检查
```

## 🎯 重要提醒

1. ✅ 机器人**必须是群组管理员**才能正常工作
2. ✅ 确保有**删除消息**和**限制成员**权限
3. ✅ 环境变量设置后**立即生效**
4. ✅ 所有配置存储在 `/data/` 目录

## 🎉 完成！

恭喜！你的机器人已经部署完成。

**现在可以：**
- 👀 在群组中自动检测违规内容
- 👮 通过管理面板调整所有参数
- 📊 查看实时状态和日志
- 🔄 通过 GitHub 自动更新代码

**需要帮助？**
1. 查看 `README.md` - 完整功能说明
2. 查看 `DEPLOYMENT_GUIDE.md` - 详细部署步骤
3. 查看 Railway 日志 - 调试问题

## 📚 下一步

1. ✅ 配置检测参数
2. ✅ 添加更多群组
3. ✅ 启用自动回复
4. ✅ 设置敏感词列表
5. ✅ 监控日志确保工作正常

---

**Ready?** 开始部署吧！🚀
