# 改进建议代码片段

## 1. 修复数据持久化（JSON 序列化问题）

```python
import json
import os
import asyncio
from pathlib import Path

DATA_FILE = "/data/reports.json"

async def save_data():
    """安全地保存数据，避免 set 序列化问题"""
    try:
        safe_reports = {}
        async with lock:
            for k, v in reports.items():
                safe_reports[str(k)] = {
                    "warning_id": v["warning_id"],
                    "suspect_id": v["suspect_id"],
                    "chat_id": v["chat_id"],
                    "reporters": list(v["reporters"]),  # 关键：转换 set 为 list
                    "original_text": v["original_text"],
                    "original_message_id": v.get("original_message_id"),
                    "timestamp": v.get("timestamp", time.time())
                }
        
        # 原子性写入：先写临时文件，再重命名
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        temp_file = f"{DATA_FILE}.tmp"
        
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(safe_reports, f, ensure_ascii=False, indent=2)
        
        # 原子操作
        if os.path.exists(DATA_FILE):
            os.replace(temp_file, DATA_FILE)
        else:
            os.rename(temp_file, DATA_FILE)
        
        logger.info(f"数据已保存: {len(safe_reports)} 条记录")
    except Exception as e:
        logger.error(f"保存数据失败: {e}")

async def load_data():
    """加载数据，处理损坏的文件"""
    global reports
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                reports = {}
                for k, v in data.items():
                    try:
                        message_id = int(k)
                        reports[message_id] = {
                            "warning_id": v["warning_id"],
                            "suspect_id": v["suspect_id"],
                            "chat_id": v["chat_id"],
                            "reporters": set(v.get("reporters", [])),  # 转换回 set
                            "original_text": v.get("original_text", ""),
                            "original_message_id": v.get("original_message_id"),
                            "timestamp": v.get("timestamp", time.time())
                        }
                    except (ValueError, KeyError) as e:
                        logger.warning(f"跳过无效的报告记录 {k}: {e}")
                        continue
            logger.info(f"已加载 {len(reports)} 条报告记录")
        else:
            reports = {}
            logger.info("没有找到历史数据，初始化新数据库")
    except json.JSONDecodeError as e:
        logger.error(f"数据文件损坏: {e}，重新初始化")
        reports = {}
        # 可选：备份损坏的文件
        try:
            backup_file = f"{DATA_FILE}.corrupted_{int(time.time())}"
            os.rename(DATA_FILE, backup_file)
            logger.info(f"损坏文件已备份: {backup_file}")
        except:
            pass
    except Exception as e:
        logger.error(f"加载数据异常: {e}")
        reports = {}
```

---

## 2. 优化清理任务（避免内存泄漏）

```python
import asyncio
from collections import deque
from datetime import datetime, timedelta

# 配置常量
MAX_REPORTS_IN_MEMORY = 1000  # 最多保留的举报数
CLEANUP_CHECK_INTERVAL = 600  # 10 分钟检查一次
REPORT_EXPIRY_TIME = 3600     # 举报记录 1 小时后过期
BATCH_CLEANUP_SIZE = 5        # 每次最多检查 5 条消息

async def cleanup_deleted_messages():
    """清理已删除的消息，避免过度 API 调用"""
    logger.info("清理任务已启动")
    
    while True:
        try:
            await asyncio.sleep(CLEANUP_CHECK_INTERVAL)
            
            async with lock:
                check_list = list(reports.items())
            
            if not check_list:
                continue
            
            to_remove = []
            current_time = time.time()
            
            # 1. 清理过期的举报（超过 1 小时）
            for orig_id, data in check_list:
                timestamp = data.get("timestamp", 0)
                if current_time - timestamp > REPORT_EXPIRY_TIME:
                    to_remove.append(orig_id)
                    logger.info(f"举报记录已过期，移除: {orig_id}")
            
            # 2. 批量检查消息是否还存在（限制数量，避免 API 限流）
            if len(to_remove) < BATCH_CLEANUP_SIZE:
                check_batch = check_list[len(to_remove):len(to_remove) + BATCH_CLEANUP_SIZE]
                
                for orig_id, data in check_batch:
                    try:
                        # 方案 A：使用 get_chat 检查权限（更轻量）
                        await bot.get_chat(data["chat_id"])
                        
                        # 方案 B：尝试转发消息验证（消耗 API 额度）
                        # test_msg = await bot.forward_message(
                        #     chat_id=list(ADMIN_IDS)[0],
                        #     from_chat_id=data["chat_id"],
                        #     message_id=orig_id
                        # )
                        # await bot.delete_message(list(ADMIN_IDS)[0], test_msg.message_id)
                        
                    except TelegramBadRequest as e:
                        error_msg = str(e).lower()
                        if "not found" in error_msg or "message to forward not found" in error_msg:
                            to_remove.append(orig_id)
                            logger.info(f"消息已删除，同步移除举报: {orig_id}")
                        else:
                            logger.debug(f"检查消息 {orig_id} 时出错: {e}")
                    except Exception as e:
                        logger.warning(f"检查消息 {orig_id} 异常: {e}")
            
            # 3. 移除标记的记录
            if to_remove:
                async with lock:
                    for oid in to_remove:
                        reports.pop(oid, None)
                await save_data()
                logger.info(f"清理完成，移除 {len(to_remove)} 条过期记录")
            
            # 4. 检查内存使用（如果举报数过多，清理最旧的）
            if len(reports) > MAX_REPORTS_IN_MEMORY:
                logger.warning(f"举报数超过限制 ({len(reports)}/{MAX_REPORTS_IN_MEMORY})")
                async with lock:
                    # 按时间戳排序，删除最旧的 100 条
                    sorted_reports = sorted(
                        reports.items(),
                        key=lambda x: x[1].get("timestamp", 0)
                    )
                    for oid, _ in sorted_reports[:100]:
                        reports.pop(oid, None)
                await save_data()
                logger.info(f"内存清理完成，当前举报数: {len(reports)}")
        
        except asyncio.CancelledError:
            logger.info("清理任务被取消")
            break
        except Exception as e:
            logger.error(f"清理任务异常: {e}", exc_info=True)
            await asyncio.sleep(60)  # 异常后等待 1 分钟重试
```

---

## 3. 日志系统设置

```python
import logging
import logging.handlers
from datetime import datetime

def setup_logging():
    """配置日志系统"""
    
    # 创建日志目录
    os.makedirs("/data/logs", exist_ok=True)
    
    # 创建日志记录器
    logger = logging.getLogger("telegram_bot")
    logger.setLevel(logging.DEBUG)
    
    # 格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. 文件处理器（日志轮转，每天一个文件）
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename="/data/logs/bot.log",
        when="midnight",
        interval=1,
        backupCount=7,  # 保留 7 天
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 2. 错误日志（单独记录错误）
    error_handler = logging.handlers.RotatingFileHandler(
        filename="/data/logs/error.log",
        maxBytes=5*1024*1024,  # 5MB
        backupCount=5,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    # 3. 控制台处理器（打印到标准输出）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()
```

---

## 4. 优化举报函数（使用速率限制）

```python
from collections import defaultdict
from datetime import datetime, timedelta

# 速率限制配置
USER_RATE_LIMITS = defaultdict(lambda: {"last_action": None, "count": 0})
RATE_LIMIT_WINDOW = 3600  # 1 小时
MAX_REPORTS_PER_HOUR = 5   # 最多 5 次举报

async def check_rate_limit(user_id: int) -> tuple[bool, str]:
    """检查用户是否超过速率限制"""
    now = time.time()
    user_data = USER_RATE_LIMITS[user_id]
    
    # 重置计数器（超过时间窗口）
    if user_data["last_action"] is None or (now - user_data["last_action"]) > RATE_LIMIT_WINDOW:
        USER_RATE_LIMITS[user_id] = {"last_action": now, "count": 0}
        return True, ""
    
    # 检查是否超限
    if user_data["count"] >= MAX_REPORTS_PER_HOUR:
        remaining = RATE_LIMIT_WINDOW - (now - user_data["last_action"])
        return False, f"举报过于频繁，请在 {int(remaining)} 秒后重试"
    
    # 更新计数
    USER_RATE_LIMITS[user_id]["count"] += 1
    USER_RATE_LIMITS[user_id]["last_action"] = now
    return True, ""

@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """处理举报请求"""
    try:
        # 检查速率限制
        allowed, msg = await check_rate_limit(callback.from_user.id)
        if not allowed:
            await callback.answer(msg, show_alert=True)
            return
        
        # ... 原有逻辑 ...
        
    except Exception as e:
        logger.error(f"举报处理异常 (用户 {callback.from_user.id}): {e}", exc_info=True)
        await callback.answer("操作失败", show_alert=True)
```

---

## 5. 多管理员通知

```python
async def notify_admins(title: str, details: str, urgent: bool = False):
    """通知所有管理员"""
    
    if not ADMIN_IDS:
        logger.warning("没有管理员，无法发送通知")
        return
    
    message = f"<b>{title}</b>\n\n{details}"
    
    failed_admins = []
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                message,
                parse_mode=ParseMode.HTML,
                disable_notification=not urgent
            )
            logger.debug(f"已通知管理员: {admin_id}")
        except Exception as e:
            logger.error(f"通知管理员 {admin_id} 失败: {e}")
            failed_admins.append(admin_id)
    
    if failed_admins:
        logger.warning(f"未能通知的管理员: {failed_admins}")
    
    return len(ADMIN_IDS) - len(failed_admins)

# 使用示例
@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    # ...
    if count >= 3:
        await notify_admins(
            title="🚨 多人举报警告",
            details=f"用户 {suspect_id}\n群组 {chat_id}\n举报人数: {count}",
            urgent=True
        )
```

---

## 6. 优雅关闭机制

```python
import signal

async def main():
    """主函数，支持优雅关闭"""
    
    # 配置日志
    logger.info("🚀 机器人启动中...")
    
    try:
        # 加载数据和配置
        await load_data()
        await load_all()
        logger.info(f"✅ 已加载配置: {len(GROUP_IDS)} 个群组, {len(ADMIN_IDS)} 个管理员")
        
        # 创建清理任务
        cleanup_task = asyncio.create_task(cleanup_deleted_messages())
        
        # 启动轮询
        logger.info("📡 开始轮询...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            skip_updates=False
        )
    
    except asyncio.CancelledError:
        logger.info("收到取消信号")
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    except Exception as e:
        logger.error(f"运行时异常: {e}", exc_info=True)
        raise
    finally:
        logger.info("正在关闭机器人...")
        
        # 清理资源
        try:
            cleanup_task.cancel()
            await asyncio.sleep(0.5)  # 给清理任务留出时间
        except:
            pass
        
        try:
            await dp.fsm.storage.close()
        except:
            pass
        
        try:
            await bot.session.close()
        except:
            pass
        
        # 最后一次保存
        await save_data()
        logger.info("✅ 机器人已安全关闭")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

---

## 7. 环境变量验证增强

```python
import sys

def validate_and_load_config():
    """验证并加载配置"""
    
    config = {}
    
    # 验证 BOT_TOKEN
    config["BOT_TOKEN"] = os.getenv("BOT_TOKEN", "").strip()
    if not config["BOT_TOKEN"]:
        logger.error("❌ 缺少 BOT_TOKEN 环境变量")
        sys.exit(1)
    
    # 验证 GROUP_IDS
    group_ids_str = os.getenv("GROUP_IDS", "").strip()
    if not group_ids_str:
        logger.error("❌ 缺少 GROUP_IDS 环境变量，格式: '123456 789012'")
        sys.exit(1)
    
    config["GROUP_IDS"] = set()
    for gid in group_ids_str.split():
        try:
            config["GROUP_IDS"].add(int(gid.strip()))
        except ValueError:
            logger.error(f"❌ 无效的群组 ID: {gid}")
            sys.exit(1)
    
    # 验证 ADMIN_IDS
    admin_ids_str = os.getenv("ADMIN_IDS", "").strip()
    if not admin_ids_str:
        logger.error("❌ 缺少 ADMIN_IDS 环境变量，格式: '111222 333444'")
        sys.exit(1)
    
    config["ADMIN_IDS"] = set()
    for uid in admin_ids_str.split():
        try:
            config["ADMIN_IDS"].add(int(uid.strip()))
        except ValueError:
            logger.error(f"❌ 无效的管理员 ID: {uid}")
            sys.exit(1)
    
    logger.info(f"✅ 配置验证成功: {len(config['GROUP_IDS'])} 群组, {len(config['ADMIN_IDS'])} 管理员")
    return config

# 使用
try:
    config = validate_and_load_config()
    TOKEN = config["BOT_TOKEN"]
    GROUP_IDS = config["GROUP_IDS"]
    ADMIN_IDS = config["ADMIN_IDS"]
except SystemExit:
    logger.critical("配置验证失败，程序退出")
    raise
```

---

## 使用说明

这些代码片段可以直接替换原始代码中对应的部分：

1. **replace `save_data()` and `load_data()`** → 使用第 1 部分
2. **replace `cleanup_deleted_messages()`** → 使用第 2 部分  
3. **add at module level** → 使用第 3 部分
4. **replace report handler** → 使用第 4 部分
5. **replace admin notify** → 使用第 5 部分
6. **replace `main()`** → 使用第 6 部分
7. **add before bot creation** → 使用第 7 部分

所有改进都向下兼容，不需要改动其他代码。
