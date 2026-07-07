"""
Web 管理后台 — 配置文件安全读写模块

提供 config.json 的原子读写，防止并发写入导致数据损坏。
"""

import json
import logging
import os
import tempfile
import threading
from typing import Any

logger = logging.getLogger(__name__)

# 配置文件路径（默认为项目根目录下的 config.json）
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))

# 写入锁（防止多个请求同时写入）
_write_lock = threading.Lock()


def read_config() -> dict:
    """
    读取 config.json，返回配置字典。

    文件不存在时返回空字典。
    JSON 格式错误时抛出 ValueError。
    """
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"配置文件不存在: {CONFIG_PATH}")
        return {}

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(config: dict) -> bool:
    """
    安全写入 config.json（Docker bind mount 兼容）。

    策略:
        1. 备份当前文件到 .bak
        2. 直接覆盖写入 CONFIG_PATH + fsync
        3. 成功则删除 .bak，失败则从 .bak 恢复

    线程安全：使用 _write_lock 防止并发写入。
    """
    with _write_lock:
        config_dir = os.path.dirname(CONFIG_PATH) or "."
        backup_path = CONFIG_PATH + ".bak"

        try:
            # 确保目录存在
            os.makedirs(config_dir, exist_ok=True)

            # ---- 步骤 1: 备份 ----
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as src:
                        with open(backup_path, "w", encoding="utf-8") as dst:
                            dst.write(src.read())
                except Exception as e:
                    logger.warning(f"创建备份失败（继续写入）: {e}")

            # ---- 步骤 2: 覆盖写入（不尝试 os.replace，避免 Docker bind mount EBUSY）----
            config_json = json.dumps(config, ensure_ascii=False, indent=4)

            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(config_json)
                f.flush()
                os.fsync(f.fileno())

            logger.info(f"配置已保存到 {CONFIG_PATH}")

            # ---- 步骤 3: 清理备份 ----
            if os.path.exists(backup_path):
                os.unlink(backup_path)

            return True

        except Exception as e:
            logger.error(f"写入配置文件失败: {e}")

            # 尝试从备份恢复
            if os.path.exists(backup_path):
                try:
                    with open(backup_path, "r", encoding="utf-8") as src:
                        with open(CONFIG_PATH, "w", encoding="utf-8") as dst:
                            dst.write(src.read())
                    logger.info("已从备份恢复配置文件")
                except Exception as restore_err:
                    logger.error(f"备份恢复也失败: {restore_err}")

            return False


def get_config_value(key_path: str, default: Any = None) -> Any:
    """
    按路径读取配置值。

    支持点分隔的嵌套路径，如 "schedule.tasks" 或 "location.latitude"。

    参数:
        key_path: 点分隔的键路径
        default:  默认值

    返回:
        配置值或默认值
    """
    config = read_config()
    keys = key_path.split(".")
    current = config
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def validate_config_structure(config: dict) -> list[str]:
    """
    校验配置结构的基本完整性。

    返回:
        错误消息列表，空列表表示通过
    """
    errors = []

    if not isinstance(config.get("location"), dict):
        errors.append("缺少 location 配置")
    if not isinstance(config.get("device"), dict):
        errors.append("缺少 device 配置")

    schedule = config.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            errors.append("schedule 必须是对象")
        else:
            enabled = schedule.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append("schedule.enabled 必须是布尔值")
            tasks = schedule.get("tasks", [])
            if tasks is not None and not isinstance(tasks, list):
                errors.append("schedule.tasks 必须是数组")

    auth_mode = config.get("auth_mode", "auto")
    if auth_mode not in ("auto", "manual"):
        errors.append(f"auth_mode 无效: {auth_mode}，支持 auto 或 manual")

    # ---------- 新增缓存 TTL 校验 ----------
    for key in ("cache_ttl_ranking", "cache_ttl_clock_detail", "cache_ttl_journal_list"):
        val = config.get(key)
        if val is not None:
            if not isinstance(val, (int, float)) or val < 0:
                errors.append(f"{key} 必须是非负数字")
    # -------------------------------------

    map_radius = config.get("map_radius")
    if map_radius is not None and (not isinstance(map_radius, (int, float)) or map_radius < 50):
        errors.append("map_radius 必须是数字且不小于50")

    return errors