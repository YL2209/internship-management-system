import os
import sys


def get_project_root() -> str:
    """返回项目根目录的绝对路径（core/config/paths.py → core/config → core → 项目根）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_log_dir() -> str:
    """返回日志目录，优先使用 LOG_DIR 环境变量。"""
    return os.environ.get("LOG_DIR", os.path.join(get_project_root(), "logs"))