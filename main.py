"""
管理员面板处理器 - 完整版
所有18个参数都可调，完全中文菜单
有完整的返回/取消机制
"""

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot_config import IMMUTABLE_CONFIG, config_manager, DEFAULT_CONFIG
from bot_data import keyword_manager, report_manager, blacklist_manager, save_all_data
from bot_logging import logger

router = Router()

class AdminPanelStates(StatesGroup):
    main_menu = State()
    config_category = State()
    config_selection = State()
    config_input = State()
    keyword_menu = State()
    keyword_input = State()

# ==================== 中文配置标签映射 ====================
ZH_LABELS = {
    "cleanup_check_interval": "清理检查间隔(秒)",
    "report_expiry_time": "举报记录过期时间(秒)",
    "deleted_message_cleanup_delay": "删除消息延迟(秒)",
    "max_reports_in_memory": "最多保留举报数",
    "batch_cleanup_size": "批量清理消息数",
    
    "auto_ban_threshold": "自动通知管理员阈值(人数)",
    "ban_duration_24h": "24小时禁言时长(秒)",
    "ban_duration_week": "1周禁言时长(秒)",
    
    "rate_limit_window": "速率限制窗口(秒)",
    "max_reports_per_hour": "每小时最多举报次数",
    "max_keyword_queries_per_hour": "每小时最多查询次数",
    
    "enable_bio_check": "启用简介检查",
    "enable_display_name_check": "启用显示名检查",
    "enable_fuzzy_match": "启用模糊匹配(实验)",
    
    "enable_delete_after_ban": "禁言后删除消息",
    "delete_warning_timeout": "删除警告延迟(秒)",
    "warning_message_timeout": "警告消息保留时间(秒)",
    
    "default_blacklist_duration": "默认黑名单时长(秒)",
    "enable_auto_blacklist": "启用自动黑名单",
}

# ==================== 参数分类 ====================
CATEGORIES = {
    "🧹 清理任务": [
        "cleanup_check_interval",
        "report_expiry_time",
        "deleted_message_cleanup_delay",
        "max_reports_in_memory",
        "batch_cleanup_size",
    ],
    "📊 举报系统": [
        "auto_ban_threshold",
        "ban_duration_24h",
        "ban_duration_week",
    ],
    "⚡ 速率限制": [
        "rate_limit_window",
        "max_reports_per_hour",
        "max_keyword_queries_per_hour",
    ],
    "🔍 关键词检测": [
        "enable_bio_check",
        "enable_display_name_check",
        "enable_fuzzy_match",
    ],
    "💬 消息管理": [
        "enable_delete_after_ban",
        "delete_warning_timeout",
        "warning_message_timeout",
    ],
    "🚫 黑名单": [
        "default_blacklist_duration",
        "enable_auto_blacklist",
    ],
}

def format_value(value):
    """格式化值显示"""
    if isinstance(value, bool):
        return "✅ 启用" if value else "❌ 禁用"
    return str(value)

def get_main_menu_kb():
    """主菜单"""
    buttons = [
        [InlineKeyboardButton(text="⚙️ 配置管理(18个参数可调)", callback_data="admin:config_main")],
        [InlineKeyboardButton(text="🔍 关键词管理", callback_data="admin:keyword_main")],
        [InlineKeyboardButton(text="📊 统计信息", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💾 数据备份", callback_data="admin:backup")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_category_kb():
    """分类菜单"""
    buttons = []
    for category in CATEGORIES.keys():
        buttons.append([InlineKeyboardButton(text=category, callback_data=f"admin:cat:{category}")])
    buttons.append([InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_config_kb(category):
    """配置项菜单"""
    keys = CATEGORIES.get(category, [])
    buttons = []
    for key in keys:
        value = config_manager.get(key)
        display = format_value(value)
        label = ZH_LABELS.get(key, key)
        buttons.append([InlineKeyboardButton(text=f"{label}: {display}", callback_data=f"admin:edit:{key}")])
    buttons.append([InlineKeyboardButton(text="← 返回分类", callback_data="admin:config_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_bool_kb(key):
    """布尔值切换"""
    buttons = [
        [
            InlineKeyboardButton(text="✅ 启用", callback_data=f"admin:set:{key}:true"),
            InlineKeyboardButton(text="❌ 禁用", callback_data=f"admin:set:{key}:false"),
        ],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 主命令 ====================
@router.message(Command("admin"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def cmd_admin(message: Message, state: FSMContext):
    """管理员面板"""
    try:
        await state.clear()
        text = "👑 <b>管理员控制面板</b>\n\n✅ 所有菜单都是中文\n✅ 所有18个参数都可调整\n✅ 任何时候都可以返回\n\n请选择操作："
        await message.reply(text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
        logger.info(f"管理员 {message.from_user.id} 打开管理面板")
    except Exception as e:
        logger.error(f"打开管理面板失败: {e}")
        await message.reply(f"❌ 失败: {e}")

# ==================== 配置管理 ====================
@router.callback_query(F.data == "admin:config_main", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def config_main(callback: CallbackQuery, state: FSMContext):
    """配置分类"""
    await callback.message.edit_text("📋 <b>选择配置分类</b>\n\n共18个参数可调整：", reply_markup=get_category_kb(), parse_mode="HTML")
    await state.set_state(AdminPanelStates.config_category)
    await callback.answer()

@router.callback_query(F.data.startswith("admin:cat:"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def config_category(callback: CallbackQuery, state: FSMContext):
    """配置项列表"""
    category = callback.data.split(":", 1)[1]
    count = len(CATEGORIES.get(category, []))
    text = f"📋 <b>{category}</b>\n\n共{count}个参数，点击参数即可修改："
    await callback.message.edit_text(text, reply_markup=get_config_kb(category), parse_mode="HTML")
    await state.set_state(AdminPanelStates.config_selection)
    await callback.answer()

@router.callback_query(F.data.startswith("admin:edit:"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def config_edit(callback: CallbackQuery, state: FSMContext):
    """编辑配置"""
    key = callback.data.split(":", 1)[1]
    value = config_manager.get(key)
    label = ZH_LABELS.get(key, key)
    
    text = f"🔧 <b>{label}</b>\n\n当前值: <b>{format_value(value)}</b>\n\n"
    
    if isinstance(value, bool):
        text += "请选择新值："
        kb = get_bool_kb(key)
    else:
        text += "请输入新值（或点击下方返回按钮不修改）："
        # 添加返回按钮供文本输入时使用
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← 不修改，返回主菜单", callback_data="admin:main")]
        ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(editing_key=key, last_category=None)
    await state.set_state(AdminPanelStates.config_input)
    await callback.answer()

@router.callback_query(F.data.startswith("admin:set:"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def config_set(callback: CallbackQuery, state: FSMContext):
    """设置布尔值"""
    parts = callback.data.split(":")
    key = parts[1]
    value = parts[2] == "true"
    
    await config_manager.update(key, value)
    label = ZH_LABELS.get(key, key)
    
    text = f"✅ <b>已更新</b>\n\n{label}\n新值: {format_value(value)}"
    await callback.message.edit_text(text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
    await state.set_state(AdminPanelStates.main_menu)
    await callback.answer(f"✅ 已更新: {label}")
    logger.info(f"管理员 {callback.from_user.id} 修改 {key} = {value}")

@router.message(AdminPanelStates.config_input, F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def config_input(message: Message, state: FSMContext):
    """输入新值"""
    data = await state.get_data()
    key = data.get("editing_key")
    
    # 如果用户输入 "返回"、"取消"、"不改" 之类的，也返回
    user_input = message.text.strip().lower()
    if user_input in ["返回", "取消", "不改", "放弃"]:
        text = "👑 <b>管理员控制面板</b>\n\n✅ 返回主菜单"
        await message.reply(text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
        await state.set_state(AdminPanelStates.main_menu)
        return
    
    try:
        expected_type = type(DEFAULT_CONFIG[key])
        if expected_type == int:
            new_value = int(message.text.strip())
            if new_value < 0:
                await message.reply("❌ 数值不能为负\n\n请重新输入，或发送『返回』取消修改")
                return
        else:
            new_value = message.text.strip()
        
        await config_manager.update(key, new_value)
        label = ZH_LABELS.get(key, key)
        
        text = f"✅ <b>已更新</b>\n\n{label}\n新值: {format_value(new_value)}"
        await message.reply(text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
        await state.set_state(AdminPanelStates.main_menu)
        logger.info(f"管理员 {message.from_user.id} 修改 {key} = {new_value}")
    except ValueError:
        await message.reply(f"❌ 格式错误，应输入数字\n\n请重新输入，或发送『返回』取消修改")

# ==================== 关键词管理 ====================
@router.callback_query(F.data == "admin:keyword_main", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_main(callback: CallbackQuery, state: FSMContext):
    """关键词菜单"""
    count = await keyword_manager.get_count()
    text = f"🔍 <b>关键词管理</b>\n\n当前敏感词: <b>{count}</b> 个\n\n点击下方按钮操作："
    
    buttons = [
        [InlineKeyboardButton(text="➕ 添加关键词", callback_data="admin:kw_add")],
        [InlineKeyboardButton(text="➖ 删除关键词", callback_data="admin:kw_del")],
        [InlineKeyboardButton(text="📋 查看所有", callback_data="admin:kw_list")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")],
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await state.set_state(AdminPanelStates.keyword_menu)
    await callback.answer()

@router.callback_query(F.data == "admin:kw_add", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_add(callback: CallbackQuery, state: FSMContext):
    """添加关键词"""
    text = "➕ <b>添加关键词</b>\n\n请输入要添加的关键词\n\n（或点击下方按钮返回，不添加）"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 不添加，返回", callback_data="admin:keyword_main")]]), parse_mode="HTML")
    await state.set_state(AdminPanelStates.keyword_input)
    await callback.answer()

@router.message(AdminPanelStates.keyword_input, F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_input(message: Message, state: FSMContext):
    """处理关键词输入"""
    keyword = message.text.strip()
    
    # 如果用户输入取消相关词汇，返回
    if keyword.lower() in ["返回", "取消", "放弃", "不添加"]:
        count = await keyword_manager.get_count()
        text = f"🔍 <b>关键词管理</b>\n\n当前敏感词: <b>{count}</b> 个\n\n已取消添加，返回菜单"
        buttons = [
            [InlineKeyboardButton(text="➕ 添加关键词", callback_data="admin:kw_add")],
            [InlineKeyboardButton(text="➖ 删除关键词", callback_data="admin:kw_del")],
            [InlineKeyboardButton(text="📋 查看所有", callback_data="admin:kw_list")],
            [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")],
        ]
        await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        await state.set_state(AdminPanelStates.keyword_menu)
        return
    
    if not keyword or len(keyword) > 100:
        await message.reply("❌ 长度1-100字符\n\n请重新输入，或发送『返回』取消")
        return
    
    success = await keyword_manager.add_keyword(keyword)
    
    if success:
        await message.reply(f"✅ 已添加: <b>{keyword}</b>", parse_mode="HTML")
        logger.info(f"管理员添加关键词: {keyword}")
    else:
        await message.reply(f"⚠️ '{keyword}' 已存在或添加失败")
    
    # 回到菜单
    count = await keyword_manager.get_count()
    text = f"🔍 <b>关键词管理</b>\n\n当前敏感词: <b>{count}</b> 个\n\n点击下方按钮操作："
    buttons = [
        [InlineKeyboardButton(text="➕ 添加关键词", callback_data="admin:kw_add")],
        [InlineKeyboardButton(text="➖ 删除关键词", callback_data="admin:kw_del")],
        [InlineKeyboardButton(text="📋 查看所有", callback_data="admin:kw_list")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")],
    ]
    await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await state.set_state(AdminPanelStates.keyword_menu)

@router.callback_query(F.data == "admin:kw_del", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_del(callback: CallbackQuery):
    """删除关键词"""
    keywords = await keyword_manager.get_keywords()
    if not keywords:
        await callback.answer("没有关键词可删除", show_alert=True)
        return
    
    buttons = []
    for kw in keywords[:10]:
        buttons.append([InlineKeyboardButton(text=f"❌ {kw}", callback_data=f"admin:kw_del_confirm:{kw}")])
    buttons.append([InlineKeyboardButton(text="← 返回", callback_data="admin:keyword_main")])
    
    text = f"➖ <b>删除关键词</b>\n\n共{len(keywords)}个，显示前10个\n\n点击选择要删除的："
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("admin:kw_del_confirm:"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_del_confirm(callback: CallbackQuery):
    """确认删除关键词"""
    keyword = callback.data.split(":", 1)[1]
    success = await keyword_manager.remove_keyword(keyword)
    
    if success:
        await callback.answer(f"✅ 已删除: {keyword}")
        logger.info(f"管理员删除关键词: {keyword}")
    else:
        await callback.answer("❌ 删除失败", show_alert=True)

@router.callback_query(F.data == "admin:kw_list", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def keyword_list(callback: CallbackQuery):
    """列出所有关键词"""
    keywords = await keyword_manager.get_keywords()
    
    if not keywords:
        text = "🔍 <b>还没有关键词</b>"
    else:
        kw_text = "、".join(keywords[:50])
        if len(keywords) > 50:
            kw_text += f" ... 共{len(keywords)}个"
        text = f"🔍 <b>关键词列表</b>\n\n共<b>{len(keywords)}</b>个\n\n{kw_text}"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回", callback_data="admin:keyword_main")]]), parse_mode="HTML")
    await callback.answer()

# ==================== 统计信息 ====================
@router.callback_query(F.data == "admin:stats", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def stats(callback: CallbackQuery):
    """统计信息"""
    reports = await report_manager.get_count()
    keywords = await keyword_manager.get_count()
    
    text = (
        "📊 <b>系统统计</b>\n\n"
        f"监控群组: {len(IMMUTABLE_CONFIG['GROUP_IDS'])}\n"
        f"管理员: {len(IMMUTABLE_CONFIG['ADMIN_IDS'])}\n"
        f"举报记录: {reports}\n"
        f"敏感词: {keywords}\n\n"
        "状态: ✅ 正常运行"
    )
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")]]), parse_mode="HTML")
    await callback.answer()

# ==================== 数据备份 ====================
@router.callback_query(F.data == "admin:backup", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def backup(callback: CallbackQuery):
    """数据备份"""
    await save_all_data()
    text = "✅ <b>数据备份完成</b>\n\n所有数据已保存到存储"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")]]), parse_mode="HTML")
    await callback.answer("✅ 备份完成")
    logger.info(f"管理员 {callback.from_user.id} 进行数据备份")

# ==================== 返回主菜单 ====================
@router.callback_query(F.data == "admin:main", F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def back_main(callback: CallbackQuery, state: FSMContext):
    """返回主菜单"""
    await state.clear()
    text = "👑 <b>管理员控制面板</b>\n\n✅ 所有菜单都是中文\n✅ 所有18个参数都可调整\n✅ 任何时候都可以返回\n\n请选择操作："
    await callback.message.edit_text(text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
    await state.set_state(AdminPanelStates.main_menu)
    await callback.answer()
