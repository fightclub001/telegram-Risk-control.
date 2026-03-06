# Telegram 机器人代码审查报告

## 📋 项目概览
- **框架**: aiogram 3.14.0+
- **部署环境**: Railway
- **主要功能**: 群组内容监控、举报系统、管理员控制面板
- **数据存储**: 本地 JSON 文件（基于 `/data/` 目录）

---

## ✅ 优点

### 1. 架构设计
- ✅ 使用 FSM（有限状态机）管理管理员操作流程
- ✅ 事件驱动架构，清晰的路由分离
- ✅ 异步设计充分利用 aiogram 特性
- ✅ 使用 `asyncio.Lock` 保护共享数据

### 2. 功能完整性
- ✅ 支持多群组管理
- ✅ 动态关键词管理（简介、显示名）
- ✅ 举报系统（带人数累积）
- ✅ 自动禁言/永久限制功能
- ✅ 自动清理已删除消息的警告信息
- ✅ 退群自动拉黑功能

### 3. 安全防护
- ✅ 环境变量配置，不硬编码敏感信息
- ✅ 权限验证（仅 ADMIN_IDS 可操作）
- ✅ 异常处理覆盖主要流程
- ✅ TelegramBadRequest 异常详细处理

---

## ⚠️ 问题与建议

### 高优先级

#### 1. **关键性能问题：内存泄漏** 🔴
**位置**: `cleanup_deleted_messages()` 函数（866-893 行）

**问题**:
```python
async def cleanup_deleted_messages():
    while True:
        await asyncio.sleep(300)  # 每 5 分钟检查一次
        for orig_id, data in check_list:
            try:
                # 转发消息到管理员用以检查是否存在
                test_msg = await bot.forward_message(...)
                await bot.delete_message(...)
            except TelegramBadRequest:
                if "not found" in str(e).lower():
                    await bot.delete_message(...)  # 删除警告
```

**风险**：
- 如果报告被举报者很多，每 5 分钟对数千条消息执行转发操作
- Railway 免费层网络有限额，可能被封禁
- 消息 ID 数值会随时间溢出

**建议**：
```python
async def cleanup_deleted_messages():
    while True:
        await asyncio.sleep(600)  # 增加到 10 分钟
        to_remove = []
        async with lock:
            check_list = list(reports.items())
        
        # 批量处理，每次不超过 5 条
        for orig_id, data in check_list[:5]:  # 限制检查数量
            try:
                # 使用 bot.get_chat_message 而不是转发
                await bot.get_chat(data["chat_id"])
                # 或直接检查缓存，避免 API 调用
            except:
                to_remove.append(orig_id)
        
        # 异步删除
        for oid in to_remove:
            asyncio.create_task(delete_warning_safely(oid))
```

#### 2. **数据持久化不安全** 🔴
**位置**: `save_data()` / `load_data()` 函数

**问题**：
- 完整 `reports` 字典包含 `set()` 对象（`reporters`），JSON 不能直接序列化
- 并发写入 JSON 文件可能造成损坏
- 无数据校验机制

**查看的代码片段**（第 600-650 行）：
```python
def save_data():
    # 问题：reporters 是 set，JSON 无法序列化
    json.dump(reports, f)  # 将失败！
```

**建议**：
```python
async def save_data():
    try:
        safe_reports = {}
        for k, v in reports.items():
            safe_reports[k] = {
                **v,
                "reporters": list(v["reporters"])  # 转换为列表
            }
        # 原子性写入（写临时文件后重命名）
        temp_file = DATA_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(safe_reports, f, ensure_ascii=False, indent=2)
        os.rename(temp_file, DATA_FILE)
    except Exception as e:
        print(f"保存数据失败: {e}")

async def load_data():
    global reports
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                reports = {}
                for k, v in data.items():
                    reports[int(k)] = {
                        **v,
                        "reporters": set(v.get("reporters", []))
                    }
    except json.JSONDecodeError:
        print("数据文件损坏，重新初始化")
        reports = {}
```

#### 3. **关键词检测逻辑不完善** 🔴
**位置**: `check_keywords()` / `is_sensitive_user()` 函数（第 550-650 行）

**问题**：
- 仅做简单字符串匹配，容易被规避（如"qq号"可写成"ｑｑ号"、"qǫ号"等）
- 没有对检测结果的日志记录
- 短关键词匹配可能有大量误报（如"v"会匹配"ev"、"movie"等）

**建议**：
```python
import re

def is_sensitive_user(user):
    username = (user.username or "").lower()
    first_name = (user.first_name or "").lower()
    last_name = (user.last_name or "").lower()
    
    display_name = f"{first_name} {last_name}".lower()
    
    for keyword in DISPLAY_NAME_KEYWORDS:
        # 使用单词边界的正则
        if re.search(rf'\b{re.escape(keyword)}\b', display_name):
            print(f"警告: 用户 {user.id} 显示名含敏感词 '{keyword}'")
            return True
        if keyword in username:
            return True
    return False
```

---

### 中优先级

#### 4. **缺少数据库设计** 🟠
**问题**：
- 使用内存存储 `reports`，重启丢失所有数据
- 黑名单配置存在 `/data/` 但其他数据没有正确持久化

**建议**：使用 SQLite 或 Redis
```python
# 使用 SQLite 示例
import aiosqlite

async def init_db():
    async with aiosqlite.connect("/data/bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                message_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                chat_id INTEGER,
                reporters TEXT,
                timestamp REAL,
                status TEXT
            )
        """)
        await db.commit()
```

#### 5. **缺少日志系统** 🟠
**问题**：
- 仅使用 `print()`，无日志级别、时间戳
- Railway 环境下难以调试
- 无法持久化查看历史

**建议**：
```python
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/data/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 替换所有 print()
logger.info(f"管理员 {caller_id} 对 {suspect_id} 执行禁言")
```

#### 6. **管理员通知逻辑有缺陷** 🟠
**位置**: 第 745 行
```python
await bot.send_message(list(ADMIN_IDS)[0], ...)
```

**问题**：
- 仅通知第一个管理员，其他管理员收不到
- 如果 ADMIN_IDS 为空会崩溃
- 没有考虑管理员离线的情况

**建议**：
```python
async def notify_admins(message: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, message)
        except Exception as e:
            logger.error(f"通知管理员 {admin_id} 失败: {e}")
```

#### 7. **缺少优雅关闭机制** 🟠
**位置**: `main()` 函数

**问题**：
- Railway 环境下重启时没有优雅关闭
- 清理任务被中断可能导致数据丢失

**建议**：
```python
async def main():
    try:
        cleanup_task = asyncio.create_task(cleanup_deleted_messages())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info("收到关闭信号")
    finally:
        await dp.storage.close()
        await bot.session.close()
        logger.info("机器人已关闭")
```

---

### 低优先级

#### 8. **代码组织** 🟡
**问题**：
- 所有代码在单个文件中，超过 900 行
- 重复的错误处理代码

**建议**：分离为模块
```
bot/
├── main.py           # 入口
├── handlers/
│   ├── admin.py      # 管理员处理
│   ├── monitoring.py  # 监控处理
│   └── moderation.py # 审核处理
├── utils/
│   ├── keywords.py   # 关键词检测
│   └── data.py       # 数据持久化
└── config.py         # 配置
```

#### 9. **环境变量验证** 🟡
**位置**: 第 21-33 行

**问题**：
- GROUP_IDS 为空时的错误信息不清晰

**建议**：
```python
def validate_env():
    try:
        group_ids = set()
        for gid in os.getenv("GROUP_IDS", "").strip().split():
            try:
                group_ids.add(int(gid))
            except ValueError:
                raise ValueError(f"无效的群组 ID: {gid}")
        
        admin_ids = set()
        for uid in os.getenv("ADMIN_IDS", "").strip().split():
            try:
                admin_ids.add(int(uid))
            except ValueError:
                raise ValueError(f"无效的管理员 ID: {uid}")
        
        if not group_ids:
            raise ValueError("GROUP_IDS 不能为空，格式: '123456 789012'")
        if not admin_ids:
            raise ValueError("ADMIN_IDS 不能为空")
        
        return group_ids, admin_ids
    except ValueError as e:
        print(f"❌ 环境变量验证失败: {e}")
        sys.exit(1)

GROUP_IDS, ADMIN_IDS = validate_env()
```

#### 10. **缺少速率限制** 🟡
**问题**：
- 没有防止同一用户频繁举报的机制
- 没有防止关键词查询的 DoS 攻击

**建议**：
```python
from datetime import datetime, timedelta

user_actions = {}  # user_id -> {"last_report": datetime, "count": int}

async def check_rate_limit(user_id, action="report"):
    if user_id not in user_actions:
        user_actions[user_id] = {"last_report": None, "count": 0}
    
    now = datetime.now()
    user_data = user_actions[user_id]
    
    # 1 小时内最多举报 5 次
    if user_data["last_report"] and (now - user_data["last_report"]) < timedelta(hours=1):
        if user_data["count"] >= 5:
            return False
    else:
        user_data["count"] = 0
    
    user_data["last_report"] = now
    user_data["count"] += 1
    return True
```

#### 11. **魔法数字需要常量化** 🟡
**问题**：
```python
await asyncio.sleep(10)      # 第 821 行
await asyncio.sleep(300)     # 第 868 行
count >= 3                   # 第 743 行
```

**建议**：
```python
# 在文件顶部定义
DELETED_MESSAGE_CLEANUP_DELAY = 10        # 删除警告消息的延迟
CLEANUP_CHECK_INTERVAL = 600              # 清理检查间隔
AUTO_BAN_THRESHOLD = 3                    # 自动通知管理员的举报阈值
BAN_DURATION_24H = 86400                  # 24小时秒数
BAN_DURATION_WEEK = 604800                # 1周秒数
```

---

## 🚀 部署建议

### Railway 特定优化

1. **设置环境变量**：
```
BOT_TOKEN=your_token_here
GROUP_IDS=123456789 987654321
ADMIN_IDS=111222333 444555666
```

2. **Procfile**（如果使用）：
```
worker: python main__1_.py
```

3. **requirements.txt**：
```
aiogram>=3.14.0
aiofiles>=23.0.0
```

4. **监控告警**：
```python
# 添加健康检查
async def health_check():
    while True:
        await asyncio.sleep(300)
        if not reports:
            logger.warning("没有举报记录，检查机器人是否正常运行")
```

---

## 📊 测试清单

- [ ] 测试多管理员权限验证
- [ ] 测试关键词匹配的各种变体（全角、特殊符号等）
- [ ] 测试并发举报是否正确累加计数
- [ ] 测试 Railway 重启后数据是否恢复
- [ ] 测试网络中断时的异常恢复
- [ ] 测试大量用户同时举报的性能
- [ ] 测试禁言权限不足时的提示
- [ ] 压力测试：10000+ 举报记录的性能

---

## 📝 总结

| 类别 | 评分 | 说明 |
|------|------|------|
| 功能完整性 | ⭐⭐⭐⭐⭐ | 功能齐全，覆盖管理、监控、举报全流程 |
| 代码质量 | ⭐⭐⭐ | 有基础错误处理，但架构需要优化 |
| 安全性 | ⭐⭐⭐ | 权限验证完整，但数据持久化有风险 |
| 性能 | ⭐⭐ | 内存泄漏风险，Railway 环境需要优化 |
| 可维护性 | ⭐⭐⭐ | 代码组织可改进，缺少日志系统 |

**建议优先处理**: #1、#2、#3 三个高优先级问题，特别是数据持久化的 JSON 序列化问题可能导致机器人直接崩溃。
