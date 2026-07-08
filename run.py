#!/usr/bin/env python3
"""
实习签到辅助工具 — 守护进程入口

用法:
    python run.py --config my_config.json

环境变量:
    CACHE_DIR    JSESSIONID 缓存目录，默认 '.'（Docker 中设为 '/app/cache'）
    LOG_DIR      日志目录，默认 './logs'
    LOG_LEVEL    日志级别，默认 INFO
    TZ           时区，默认 Asia/Shanghai

信号:
    SIGTERM / SIGINT → 守护进程优雅退出
"""

# ==================== 标准库导入 ====================
import json                # 读写 JSON 配置文件
import logging             # 日志系统
import os                  # 环境变量、文件路径操作
import signal              # 捕获系统信号，实现优雅退出
import sys                 # 程序退出码
import time                # 主循环 sleep
from logging.handlers import RotatingFileHandler  # 日志文件自动轮转
from pathlib import Path   # 现代路径处理

import requests            # HTTP 会话（复用 Cookie）

# ==================== 项目模块导入 ====================
from core.apis.signer import SignInClient
from core.config.paths import get_log_dir
from core.scheduler import DailyScheduler


# -------------------- 工具函数 --------------------
def get_cache_file() -> Path:
    """
    获取 JSESSIONID 缓存文件路径。
    优先使用环境变量 CACHE_DIR 指定的目录，Docker 中为 /app/cache。
    """
    cache_dir = Path(os.environ.get("CACHE_DIR", "."))
    return cache_dir / ".session_cache.json"


def setup_logging() -> None:
    """
    配置全局日志系统：
    - 同时输出到控制台（Docker logs）和文件 sign.log
    - 日志文件最大 5MB，保留 5 个历史备份
    - 日志级别可通过环境变量 LOG_LEVEL 调整（默认 INFO）
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 根 logger 设置
    root = logging.getLogger()
    root.setLevel(log_level)

    # 控制台输出（供 docker compose logs 查看）
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件输出（供 Web 后台查看历史日志）
    log_dir = get_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "sign.log")

    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as e:
        root.warning(f"无法创建日志文件 {log_file}: {e}")


def load_config(path: str) -> dict:
    """
    加载 JSON 配置文件。
    若文件不存在或格式错误，抛出异常。
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件 JSON 格式错误: {e}")


# -------------------- 守护进程主逻辑 --------------------
def start_daemon_thread(config: dict, config_path: str = "config.json"):
    """
    在后台 daemon 线程中启动定时签到调度器，供 Flask 主进程调用。

    返回 DailyScheduler 实例（用于优雅停止），若 schedule 未启用则返回 None。
    """
    logger = logging.getLogger("daemon")
    schedule_cfg = config.get("schedule")
    scheduler = None

    if not schedule_cfg:
        logger.warning("⚠️ 配置文件中缺少 'schedule' 字段，守护进程不会启动")
        return None

    schedule_enabled = schedule_cfg.get("enabled", True)
    if not schedule_enabled:
        logger.info("🔛 schedule.enabled=false，定时任务已关闭")
        return None
    if not schedule_cfg.get("tasks"):
        logger.warning("⚠️ schedule.tasks 为空，守护进程不会启动")
        return None

    cache_file = str(get_cache_file())
    session = requests.Session()

    def do_sign(action: str) -> dict:
        try:
            fresh_config = load_config(config_path)
        except Exception as e:
            logger.warning(f"⚠️ 重新加载配置失败，回退到启动时配置: {e}")
            fresh_config = config
        client = SignInClient(fresh_config, session, cache_file=cache_file)
        return client.execute(action)

    scheduler = DailyScheduler(schedule_cfg, do_sign)
    scheduler.start()

    workdays = schedule_cfg.get("workdays", [1, 2, 3, 4, 5])
    weekdays_str = "、".join("一二三四五六日"[d - 1] for d in workdays)
    logger.info(f"🟢 守护线程已启动 (工作日: 周{weekdays_str}, 任务数: {len(schedule_cfg.get('tasks', []))})")

    return scheduler


def run_daemon(config: dict, config_path: str = "config.json") -> None:
    """
    以守护进程模式运行：
    - 根据 schedule.tasks 配置创建定时调度器
    - 到达设定时间时自动触发签到/签退
    - 支持信号 SIGTERM/SIGINT 优雅退出
    - 每次执行任务前会重新加载配置文件，实现热更新
    """
    logger = logging.getLogger("daemon")
    schedule_cfg = config.get("schedule")

    # 校验 schedule 配置
    if not schedule_cfg:
        logger.error("❌ 配置文件中缺少 'schedule' 字段，无法以守护进程模式运行。")
        sys.exit(1)

    # 检查定时任务总开关
    schedule_enabled = schedule_cfg.get("enabled", True)
    if not schedule_enabled:
        logger.info("🔛 schedule.enabled=false，定时任务已关闭，守护进程将空转等待配置变更")
    elif not schedule_cfg.get("tasks"):
        logger.error("❌ schedule.tasks 为空，至少需要配置一个签到或签退任务。")
        sys.exit(1)

    # 缓存文件路径
    cache_file = str(get_cache_file())
    # 全局 HTTP 会话，复用 Cookie 和 JSESSIONID
    session = requests.Session()

    # 调度器回调函数：每次任务触发时调用
    def do_sign(action: str) -> dict:
        """
        执行签到或签退操作。
        每次执行前都会从磁盘重新加载配置文件，以确保使用 Web 管理后台修改后的最新配置。
        """
        # 重新加载最新配置（热更新）
        try:
            fresh_config = load_config(config_path)
        except Exception as e:
            logger.warning(f"⚠️ 重新加载配置失败，回退到启动时配置: {e}")
            fresh_config = config

        # 创建 SignInClient 并执行动作（action 为 "sign_in" 或 "sign_out"）
        client = SignInClient(fresh_config, session, cache_file=cache_file)
        return client.execute(action)

    # 创建调度器实例
    scheduler = DailyScheduler(schedule_cfg, do_sign)

    # ---- 信号处理：优雅退出 ----
    def _on_shutdown(signum, frame):
        name = signal.Signals(signum).name
        logger.info(f"收到信号 {name}，正在退出...")
        scheduler.stop(timeout=15)   # 等待调度线程结束
        session.close()              # 关闭 HTTP 会话
        logger.info("守护进程已退出")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)

    # 启动调度器（在后台线程中运行）
    scheduler.start()

    # 打印运行状态信息
    workdays = schedule_cfg.get("workdays", [1, 2, 3, 4, 5])
    weekdays_str = "、".join("一二三四五六日"[d - 1] for d in workdays)
    enabled_str = "已开启" if schedule_enabled else "⚠️ 已关闭"
    logger.info(f"🟢 守护进程运行中 (定时: {enabled_str}, 工作日: 周{weekdays_str})")
    logger.info(f"   已加载 {len(schedule_cfg.get('tasks', []))} 个定时任务")

    # 主线程保持运行，直到收到退出信号
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass

    scheduler.stop(timeout=15)
    session.close()
    logger.info("守护进程已退出")


# -------------------- 程序入口 --------------------
def main() -> None:
    """程序入口：初始化日志、加载配置、启动守护进程。"""
    setup_logging()
    logger = logging.getLogger(__name__)

    # 解析命令行参数（仅保留 --config）
    import argparse
    parser = argparse.ArgumentParser(description="实习签到守护进程")
    parser.add_argument("--config", type=str, default="config.json", help="配置文件路径")
    args = parser.parse_args()

    config_path = os.path.abspath(args.config)
    logger.info(f"📂 配置文件: {config_path}")

    # 加载配置文件
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f"❌ 加载配置文件失败: {e}")
        sys.exit(1)

    # 打印认证模式及对应参数（敏感信息脱敏显示）
    auth_mode = str(config.get("auth_mode", "auto")).strip().lower()
    if auth_mode == "manual":
        # 手动模式：脱敏显示四个关键参数
        def _mask(val, show=4):
            s = str(val or "")
            if len(s) <= show * 2:
                return s[:show] + "*" * max(0, len(s) - show)
            return s[:show] + "*" * (len(s) - show * 2) + s[-show:]

        logger.info("🔐 认证模式: MANUAL (手动)")
        logger.info(f"   unionId      = {_mask(config.get('unionId'))}")
        logger.info(f"   encryptValue = {_mask(config.get('encryptValue'), 6)}")
        logger.info(f"   openId       = {_mask(config.get('openId'))}")
        logger.info(f"   sessionId    = {_mask(config.get('sessionId'), 6)}")
    else:
        # 自动模式：仅提示 code 是否已填写
        logger.info("🔐 认证模式: AUTO (自动登录)")
        code_status = "已填写" if config.get("code", "").strip() else "未填写（将使用缓存）"
        logger.info(f"   code = {code_status}")

    # 额外提示：本守护进程仅负责签到/签退，周记/月记需通过 Web 后台手动操作
    logger.info("📝 提示：本守护进程仅负责定时签到/签退，不包含周记/月记自动提交功能")

    # 启动守护进程
    run_daemon(config, config_path)


if __name__ == "__main__":
    main()