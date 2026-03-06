#!/usr/bin/env python3
"""
部署前检查脚本
检查环境、依赖和配置是否正确
"""

import os
import sys
import subprocess
from pathlib import Path

def check_python_version():
    """检查 Python 版本"""
    print("检查 Python 版本...", end=" ")
    version = sys.version_info
    if version.major == 3 and version.minor >= 8:
        print(f"✅ Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        print(f"❌ 需要 Python 3.8+，当前版本: {version.major}.{version.minor}.{version.micro}")
        return False

def check_dependencies():
    """检查依赖安装"""
    print("检查依赖...", end=" ")
    required = {
        "aiogram": "aiogram",
        "aiofiles": "aiofiles",
        "dotenv": "python-dotenv"
    }
    
    missing = []
    for name, package in required.items():
        try:
            __import__(name)
        except ImportError:
            missing.append(package)
    
    if missing:
        print(f"❌ 缺少: {', '.join(missing)}")
        print(f"   运行: pip install -r requirements.txt")
        return False
    else:
        print("✅ 所有依赖已安装")
        return True

def check_environment_variables():
    """检查环境变量"""
    print("检查环境变量...", end=" ")
    
    required = ["BOT_TOKEN", "GROUP_IDS", "ADMIN_IDS"]
    missing = []
    
    for var in required:
        if not os.getenv(var):
            missing.append(var)
    
    if missing:
        print(f"❌ 缺少: {', '.join(missing)}")
        print(f"   参考 .env.example 文件配置")
        return False
    else:
        print("✅ 环境变量已配置")
        return True

def check_bot_token():
    """检查 Bot Token 格式"""
    print("检查 Bot Token...", end=" ")
    token = os.getenv("BOT_TOKEN", "")
    
    if not token or len(token) < 20:
        print("❌ Bot Token 无效")
        return False
    
    if not token.startswith(("4", "5", "6", "7", "8", "9")):
        print("⚠️  Token 格式可能不正确")
        return True  # 警告但继续
    
    print("✅ Bot Token 格式正确")
    return True

def check_group_ids():
    """检查 Group IDs"""
    print("检查 Group IDs...", end=" ")
    group_ids_str = os.getenv("GROUP_IDS", "").strip()
    
    if not group_ids_str:
        print("❌ GROUP_IDS 为空")
        return False
    
    try:
        group_ids = [int(gid.strip()) for gid in group_ids_str.split()]
        if not group_ids:
            print("❌ GROUP_IDS 为空")
            return False
        print(f"✅ 已配置 {len(group_ids)} 个群组")
        return True
    except ValueError:
        print("❌ GROUP_IDS 格式错误，应为整数（空格分隔）")
        return False

def check_admin_ids():
    """检查 Admin IDs"""
    print("检查 Admin IDs...", end=" ")
    admin_ids_str = os.getenv("ADMIN_IDS", "").strip()
    
    if not admin_ids_str:
        print("❌ ADMIN_IDS 为空")
        return False
    
    try:
        admin_ids = [int(uid.strip()) for uid in admin_ids_str.split()]
        if not admin_ids:
            print("❌ ADMIN_IDS 为空")
            return False
        print(f"✅ 已配置 {len(admin_ids)} 个管理员")
        return True
    except ValueError:
        print("❌ ADMIN_IDS 格式错误，应为整数（空格分隔）")
        return False

def check_files():
    """检查必需文件"""
    print("检查文件...", end=" ")
    required_files = [
        "main.py",
        "bot_config.py",
        "bot_data.py",
        "bot_logging.py",
        "bot_admin.py",
        "requirements.txt",
        "Procfile",
        ".env.example"
    ]
    
    missing = []
    for file in required_files:
        if not os.path.exists(file):
            missing.append(file)
    
    if missing:
        print(f"❌ 缺少文件: {', '.join(missing)}")
        return False
    else:
        print("✅ 所有必需文件存在")
        return True

def check_data_directory():
    """检查数据目录"""
    print("检查数据目录...", end=" ")
    data_dir = os.getenv("CONFIG_DIR", "/data")
    
    if not os.path.exists(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
            print(f"✅ 已创建: {data_dir}")
            return True
        except Exception as e:
            print(f"❌ 无法创建: {e}")
            return False
    else:
        print(f"✅ 目录存在: {data_dir}")
        return True

def check_file_permissions():
    """检查文件权限"""
    print("检查文件权限...", end=" ")
    data_dir = os.getenv("CONFIG_DIR", "/data")
    
    try:
        # 测试写入权限
        test_file = os.path.join(data_dir, ".test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        print(f"✅ 目录可写: {data_dir}")
        return True
    except Exception as e:
        print(f"❌ 无写入权限: {e}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("🚀 Telegram 机器人部署前检查")
    print("=" * 60)
    print()
    
    checks = [
        ("Python 版本", check_python_version),
        ("依赖包", check_dependencies),
        ("环境变量", check_environment_variables),
        ("Bot Token", check_bot_token),
        ("Group IDs", check_group_ids),
        ("Admin IDs", check_admin_ids),
        ("文件", check_files),
        ("数据目录", check_data_directory),
        ("文件权限", check_file_permissions),
    ]
    
    results = {}
    for name, check_func in checks:
        try:
            results[name] = check_func()
        except Exception as e:
            print(f"❌ 检查失败: {e}")
            results[name] = False
        print()
    
    # 总结
    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    if passed == total:
        print(f"✅ 所有检查通过！({passed}/{total})")
        print("=" * 60)
        return 0
    else:
        print(f"❌ 检查未通过 ({passed}/{total})")
        print("\n失败项目:")
        for name, result in results.items():
            if not result:
                print(f"  - {name}")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    # 加载 .env 文件
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except:
        pass
    
    sys.exit(main())
