"""
管理员面板处理器
提供交互式配置调整界面
"""

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot_config import (
    IMMUTABLE_CONFIG, config_manager, get_all_configurable_keys,
    format_config_value, DEFAULT_CONFIG
)
from bot_data import (
    keyword_manager, report_manager, blacklist_manager, save_all_data
)
from bot_logging import logger

router = Router()

# ==================== FSM 状态定义 ====================
class AdminPanelStates(StatesGroup):
    """管理员面板状态"""
    main_menu = State()                    # 主菜单
    config_category = State()              # 配置分类选择
    config_selection = State()             # 配置项选择
    config_input = State()                 # 配置输入
    keyword_menu = State()                 # 关键词菜单
    keyword_input = State()                # 关键词输入
    blacklist_menu = State()               # 黑名单菜单
    confirm_action = State()               # 确认操作

# ==================== 按钮生成器 ====================
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """主菜单键盘"""
    buttons = [
        [InlineKeyboardButton(text="⚙️ 配置管理", callback_data="admin:config_main")],
        [InlineKeyboardButton(text="🔍 关键词管理", callback_data="admin:keyword_main")],
        [InlineKeyboardButton(text="🚫 黑名单管理", callback_data="admin:blacklist_main")],
        [InlineKeyboardButton(text="📊 统计信息", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🔄 数据备份", callback_data="admin:backup")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_config_category_keyboard() -> InlineKeyboardMarkup:
    """配置分类键盘"""
    categories = get_all_configurable_keys()
    buttons = []
    for category, keys in categories.items():
        buttons.append([
            InlineKeyboardButton(
                text=category,
                callback_data=f"admin:config_cat:{category}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_config_list_keyboard(category: str) -> InlineKeyboardMarkup:
    """获取配置项列表键盘"""
    categories = get_all_configurable_keys()
    keys = categories.get(category, [])
    
    buttons = []
    for key in keys:
        value = config_manager.get(key)
        display_value = format_config_value(value)
        button_text = f"{key}: {display_value}"
        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin:config_edit:{key}"
            )
        ])
    
    buttons.append([InlineKeyboardButton(text="← 返回配置分类", callback_data="admin:config_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_bool_toggle_keyboard(config_key: str) -> InlineKeyboardMarkup:
    """布尔值切换键盘"""
    current = config_manager.get(config_key)
    buttons = [
        [
            InlineKeyboardButton(
                text="✅ 启用",
                callback_data=f"admin:config_set:{config_key}:true"
            ),
            InlineKeyboardButton(
                text="❌ 禁用",
                callback_data=f"admin:config_set:{config_key}:false"
            ),
        ],
        [InlineKeyboardButton(text="← 返回", callback_data="admin:main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_keyword_menu_keyboard() -> InlineKeyboardMarkup:
    """关键词菜单键盘"""
    buttons = [
        [InlineKeyboardButton(text="➕ 添加关键词", callback_data="admin:keyword_add")],
        [InlineKeyboardButton(text="➖ 删除关键词", callback_data="admin:keyword_remove")],
        [InlineKeyboardButton(text="📋 查看所有关键词", callback_data="admin:keyword_list")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_blacklist_menu_keyboard() -> InlineKeyboardMarkup:
    """黑名单菜单键盘"""
    buttons = [
        [InlineKeyboardButton(text="⚙️ 配置黑名单", callback_data="admin:blacklist_config")],
        [InlineKeyboardButton(text="📋 查看配置", callback_data="admin:blacklist_list")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    """确认操作键盘"""
    buttons = [
        [
            InlineKeyboardButton(text="✅ 确认", callback_data=f"admin:confirm:{action}"),
            InlineKeyboardButton(text="❌ 取消", callback_data="admin:main"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 主菜单处理 ====================
@router.message(Command("admin"), F.from_user.id.in_(IMMUTABLE_CONFIG["ADMIN_IDS"]))
async def cmd_admin(message: Message, state: FSMContext):
    """管理员面板主命令"""
    try:
        await state.clear()
        text = (
            "👑 <b>管理员控制面板</b>\n\n"
            "欢迎使用机器人管理面板。"
            "您可以在此修改所有可配置参数。\n\n"
            "<i>提示：BOT_TOKEN、GROUP_IDS、ADMIN_IDS 不可修改，"
            "需要修改请重新部署机器人。</i>"
        )
        kb = get_main_menu_keyboard()
        await message.reply(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(AdminPanelStates.main_menu)
        logger.info(f"管理员 {message.from_user.id} 打开管理面板")
    except Exception as e:
        logger.error(f"打开管理面板失败: {e}")
        await message.reply(f"❌ 操作失败: {str(e)}")

# ==================== 配置管理处理 ====================
@router.callback_query(F.data == "admin:config_main")
async def handle_config_main(callback: CallbackQuery, state: FSMContext):
    """配置分类选择"""
    try:
        text = "📋 <b>配置管理</b>\n\n请选择配置分类："
        kb = get_config_category_keyboard()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(AdminPanelStates.config_category)
        await callback.answer()
    except Exception as e:
        logger.error(f"配置分类选择失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("admin:config_cat:"))
async def handle_config_category(callback: CallbackQuery, state: FSMContext):
    """配置项列表"""
    try:
        category = callback.data.split(":", 2)[2]
        text = f"📋 <b>{category}</b>\n\n请选择要修改的配置项："
        kb = get_config_list_keyboard(category)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.update_data(current_category=category)
        await state.set_state(AdminPanelStates.config_selection)
        await callback.answer()
    except Exception as e:
        logger.error(f"配置项列表失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("admin:config_edit:"))
async def handle_config_edit(callback: CallbackQuery, state: FSMContext):
    """配置编辑"""
    try:
        config_key = callback.data.split(":", 2)[2]
        current_value = config_manager.get(config_key)
        description = await config_manager.get_config_description(config_key)
        
        text = (
            f"🔧 <b>{config_key}</b>\n\n"
            f"<b>当前值:</b> {format_config_value(current_value)}\n\n"
            f"<b>说明:</b>\n{description}\n\n"
        )
        
        # 根据类型选择输入方式
        if isinstance(current_value, bool):
            text += "请选择新值:"
            kb = get_bool_toggle_keyboard(config_key)
        else:
            text += "请输入新值或点击下方按钮:"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回", callback_data="admin:main")
            ]])
        
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.update_data(editing_config=config_key, current_value=current_value)
        await state.set_state(AdminPanelStates.config_input)
        await callback.answer()
    except Exception as e:
        logger.error(f"配置编辑失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("admin:config_set:"))
async def handle_config_set(callback: CallbackQuery, state: FSMContext):
    """配置设置（来自键盘）"""
    try:
        parts = callback.data.split(":", 3)
        config_key = parts[2]
        value_str = parts[3]
        
        # 类型转换
        expected_type = type(DEFAULT_CONFIG[config_key])
        if expected_type == bool:
            new_value = value_str.lower() == "true"
        elif expected_type == int:
            new_value = int(value_str)
        else:
            new_value = value_str
        
        # 更新配置
        success = await config_manager.update(config_key, new_value)
        
        if success:
            await callback.answer(f"✅ 已更新: {config_key} = {format_config_value(new_value)}")
            text = (
                f"✅ <b>配置已更新</b>\n\n"
                f"配置项: {config_key}\n"
                f"新值: {format_config_value(new_value)}\n\n"
                f"更改已自动保存。"
            )
            await callback.message.edit_text(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
            logger.info(f"管理员 {callback.from_user.id} 修改配置: {config_key} = {new_value}")
        else:
            await callback.answer("❌ 配置更新失败，请检查输入值", show_alert=True)
        
        await state.set_state(AdminPanelStates.main_menu)
    except ValueError as e:
        logger.warning(f"配置值类型错误: {e}")
        await callback.answer(f"❌ 输入错误: {str(e)}", show_alert=True)
    except Exception as e:
        logger.error(f"配置设置失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.message(AdminPanelStates.config_input)
async def handle_config_input(message: Message, state: FSMContext):
    """处理配置输入（文本）"""
    try:
        data = await state.get_data()
        config_key = data.get("editing_config")
        
        if not config_key:
            await message.reply("❌ 配置项丢失，请返回主菜单重新开始")
            return
        
        # 类型转换和验证
        expected_type = type(DEFAULT_CONFIG[config_key])
        try:
            if expected_type == int:
                new_value = int(message.text.strip())
                if new_value < 0:
                    await message.reply("❌ 数值不能为负数，请重新输入")
                    return
            else:
                new_value = message.text.strip()
        except ValueError:
            await message.reply(f"❌ 输入格式错误，应输入 {expected_type.__name__} 类型")
            return
        
        # 更新配置
        success = await config_manager.update(config_key, new_value)
        
        if success:
            text = (
                f"✅ <b>配置已更新</b>\n\n"
                f"配置项: {config_key}\n"
                f"新值: {format_config_value(new_value)}\n\n"
                f"更改已自动保存。"
            )
            await message.reply(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
            logger.info(f"管理员 {message.from_user.id} 修改配置: {config_key} = {new_value}")
        else:
            await message.reply("❌ 配置更新失败，请检查输入值")
        
        await state.set_state(AdminPanelStates.main_menu)
    except Exception as e:
        logger.error(f"处理配置输入失败: {e}")
        await message.reply(f"❌ 操作失败: {str(e)}")

# ==================== 关键词管理处理 ====================
@router.callback_query(F.data == "admin:keyword_main")
async def handle_keyword_main(callback: CallbackQuery, state: FSMContext):
    """关键词菜单"""
    try:
        keyword_count = await keyword_manager.get_count()
        text = (
            f"🔍 <b>关键词管理</b>\n\n"
            f"当前简介敏感词: <b>{keyword_count}</b> 个\n\n"
            f"请选择操作:"
        )
        kb = get_keyword_menu_keyboard()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(AdminPanelStates.keyword_menu)
        await callback.answer()
    except Exception as e:
        logger.error(f"关键词菜单失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data == "admin:keyword_add")
async def handle_keyword_add(callback: CallbackQuery, state: FSMContext):
    """添加关键词"""
    try:
        text = (
            "➕ <b>添加关键词</b>\n\n"
            "请输入要添加的关键词:"
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回", callback_data="admin:keyword_main")
            ]]),
            parse_mode="HTML"
        )
        await state.set_state(AdminPanelStates.keyword_input)
        await callback.answer()
    except Exception as e:
        logger.error(f"添加关键词界面失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data == "admin:keyword_remove")
async def handle_keyword_remove(callback: CallbackQuery, state: FSMContext):
    """删除关键词"""
    try:
        keywords = await keyword_manager.get_keywords()
        if not keywords:
            await callback.answer("没有可删除的关键词", show_alert=True)
            return
        
        # 分页显示（一次最多 10 个）
        buttons = []
        for kw in keywords[:10]:
            buttons.append([
                InlineKeyboardButton(
                    text=f"❌ {kw}",
                    callback_data=f"admin:keyword_del:{kw}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="← 返回", callback_data="admin:keyword_main")])
        
        text = (
            "➖ <b>删除关键词</b>\n\n"
            f"共 {len(keywords)} 个，显示前 10 个\n\n"
            "选择要删除的关键词:"
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(AdminPanelStates.keyword_menu)
        await callback.answer()
    except Exception as e:
        logger.error(f"删除关键词界面失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("admin:keyword_del:"))
async def handle_keyword_delete(callback: CallbackQuery, state: FSMContext):
    """确认删除关键词"""
    try:
        keyword = callback.data.split(":", 2)[2]
        success = await keyword_manager.remove_keyword(keyword)
        
        if success:
            await callback.answer(f"✅ 已删除关键词: {keyword}")
            await handle_keyword_main(callback, state)
            logger.info(f"管理员 {callback.from_user.id} 删除关键词: {keyword}")
        else:
            await callback.answer("❌ 删除失败", show_alert=True)
    except Exception as e:
        logger.error(f"删除关键词失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.message(AdminPanelStates.keyword_input)
async def handle_keyword_input(message: Message, state: FSMContext):
    """处理关键词输入"""
    try:
        keyword = message.text.strip()
        
        # 验证
        if not keyword or len(keyword) > 100:
            await message.reply("❌ 关键词长度必须在 1-100 个字符之间")
            return
        
        success = await keyword_manager.add_keyword(keyword)
        
        if success:
            await message.reply(
                f"✅ 已添加关键词: <b>{keyword}</b>",
                reply_markup=get_keyword_menu_keyboard(),
                parse_mode="HTML"
            )
            logger.info(f"管理员 {message.from_user.id} 添加关键词: {keyword}")
        else:
            await message.reply(f"⚠️ 关键词 '{keyword}' 已存在或添加失败")
        
        await state.set_state(AdminPanelStates.keyword_menu)
    except Exception as e:
        logger.error(f"处理关键词输入失败: {e}")
        await message.reply(f"❌ 操作失败: {str(e)}")

@router.callback_query(F.data == "admin:keyword_list")
async def handle_keyword_list(callback: CallbackQuery, state: FSMContext):
    """列出所有关键词"""
    try:
        keywords = await keyword_manager.get_keywords()
        
        if not keywords:
            text = "🔍 <b>关键词列表</b>\n\n还没有添加关键词"
        else:
            # 分组显示（每 20 个为一组）
            keyword_text = "、".join(keywords[:50])
            if len(keywords) > 50:
                keyword_text += f" ... 等共 {len(keywords)} 个"
            
            text = (
                f"🔍 <b>关键词列表</b>\n\n"
                f"<b>总数:</b> {len(keywords)}\n\n"
                f"<b>关键词:</b>\n{keyword_text}"
            )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回", callback_data="admin:keyword_main")
            ]]),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"列出关键词失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

# ==================== 黑名单管理处理 ====================
@router.callback_query(F.data == "admin:blacklist_main")
async def handle_blacklist_main(callback: CallbackQuery, state: FSMContext):
    """黑名单菜单"""
    try:
        count = await blacklist_manager.get_config_count()
        text = (
            f"🚫 <b>黑名单管理</b>\n\n"
            f"已配置的群组: <b>{count}</b> 个\n\n"
            f"请选择操作:"
        )
        kb = get_blacklist_menu_keyboard()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(AdminPanelStates.blacklist_menu)
        await callback.answer()
    except Exception as e:
        logger.error(f"黑名单菜单失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

@router.callback_query(F.data == "admin:blacklist_list")
async def handle_blacklist_list(callback: CallbackQuery, state: FSMContext):
    """列出黑名单配置"""
    try:
        from bot_config import IMMUTABLE_CONFIG
        
        text = "🚫 <b>黑名单配置</b>\n\n"
        
        for group_id in IMMUTABLE_CONFIG["GROUP_IDS"]:
            config = await blacklist_manager.get_blacklist_config(str(group_id))
            if config:
                enabled = "✅ 启用" if config.get("enabled") else "❌ 禁用"
                duration = config.get("duration", 0)
                duration_text = "永久" if duration == 0 else f"{duration} 秒"
                text += f"\n群 {group_id}: {enabled} - {duration_text}"
            else:
                text += f"\n群 {group_id}: ❌ 未配置"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回", callback_data="admin:blacklist_main")
            ]]),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"列出黑名单配置失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

# ==================== 统计信息处理 ====================
@router.callback_query(F.data == "admin:stats")
async def handle_stats(callback: CallbackQuery):
    """显示统计信息"""
    try:
        report_count = await report_manager.get_count()
        keyword_count = await keyword_manager.get_count()
        blacklist_count = await blacklist_manager.get_config_count()
        
        from bot_config import IMMUTABLE_CONFIG
        
        text = (
            "📊 <b>系统统计信息</b>\n\n"
            f"<b>机器人配置:</b>\n"
            f"├ 监控群组: {len(IMMUTABLE_CONFIG['GROUP_IDS'])}\n"
            f"├ 管理员: {len(IMMUTABLE_CONFIG['ADMIN_IDS'])}\n\n"
            f"<b>数据统计:</b>\n"
            f"├ 举报记录: {report_count}\n"
            f"├ 敏感词: {keyword_count}\n"
            f"├ 黑名单配置: {blacklist_count}\n\n"
            f"<b>系统状态:</b>\n"
            f"├ 状态: ✅ 运行正常"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")
            ]]),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)

# ==================== 数据备份处理 ====================
@router.callback_query(F.data == "admin:backup")
async def handle_backup(callback: CallbackQuery):
    """数据备份"""
    try:
        await save_all_data()
        await callback.answer("✅ 所有数据已备份保存", show_alert=False)
        
        text = (
            "✅ <b>数据备份完成</b>\n\n"
            "所有数据已保存到存储：\n"
            "├ 举报记录\n"
            "├ 敏感词列表\n"
            "├ 黑名单配置\n"
            "├ 系统配置"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← 返回主菜单", callback_data="admin:main")
            ]]),
            parse_mode="HTML"
        )
        logger.info(f"管理员 {callback.from_user.id} 进行了数据备份")
    except Exception as e:
        logger.error(f"数据备份失败: {e}")
        await callback.answer(f"❌ 备份失败: {str(e)}", show_alert=True)

# ==================== 返回主菜单处理 ====================
@router.callback_query(F.data == "admin:main")
async def handle_return_main(callback: CallbackQuery, state: FSMContext):
    """返回主菜单"""
    try:
        await state.clear()
        text = (
            "👑 <b>管理员控制面板</b>\n\n"
            "欢迎使用机器人管理面板。"
        )
        kb = get_main_menu_keyboard()
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(AdminPanelStates.main_menu)
        await callback.answer()
    except Exception as e:
        logger.error(f"返回主菜单失败: {e}")
        await callback.answer(f"❌ 失败: {str(e)}", show_alert=True)
