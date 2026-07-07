"""
实习数据管理系统 — 每日定时调度引擎（仅签到/签退）

纯标准库实现，零额外依赖。
功能:
- 按 config.json 中 schedule.tasks 数组配置多个签到/签退任务
- 每个任务支持 random_window（秒级随机偏移）：
    sign_in:  实际时间 = 设定时间 - random(0, random_window)  （提前）
    sign_out: 实际时间 = 设定时间 + random(0, random_window)  （延后）
- 每天为每个任务计算一次随机时间，当天不再变动
- 工作日过滤（可配置周一至周日）
- 失败重试机制
- 通过 threading.Event 实现可中断休眠（配合 SIGTERM 优雅退出）
"""

import datetime
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

_CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.json",
    ),
)

logger = logging.getLogger(__name__)


@dataclass
class TaskDef:
    """
    单个定时任务的定义（从 config.json 解析）。
    action:        "sign_in" 或 "sign_out"
    target_time:   目标锚点时间（HH:MM:SS）
    tolerance:     时间容差（秒）
    random_window: 随机偏移窗口（秒），为 0 表示不随机
    """
    action: str
    target_time: datetime.time
    tolerance: int = 60
    random_window: int = 0


@dataclass
class _ScheduledTask:
    """
    内部结构：已计算好当天实际执行时间的任务。
    fire_at: 当天的实际触发时间
    task:    对应的任务定义
    done:    是否已执行
    """
    fire_at: datetime.datetime
    task: TaskDef
    done: bool = False


class DailyScheduler:
    """
    每日定时调度器（仅签到/签退）。

    工作流程:
        1. 每天凌晨重新计算所有任务的随机执行时间
        2. 找到最近的下一个待触发任务
        3. 休眠到触发时间（或 tolerance 范围内）
        4. 执行任务（失败则重试）
        5. 标记为已完成，继续查找下一个任务
        6. 当天所有任务完成后，休眠到第二天凌晨
    """

    def __init__(self, schedule_cfg: dict, sign_callback: Callable[[str], dict]):
        """
        参数:
            schedule_cfg:   config.json 中 "schedule" 字段的值
            sign_callback:  回调函数 (action: str) -> {"success": bool, "message": str}
                            action 值为 "sign_in" 或 "sign_out"
        """
        self._enabled = schedule_cfg.get("enabled", True)
        self._workdays = schedule_cfg.get("workdays", [1, 2, 3, 4, 5])
        self._retry_interval = schedule_cfg.get("retryIntervalSeconds", 300)
        self._max_retries = schedule_cfg.get("maxRetries", 3)
        self._sign_callback = sign_callback

        # 解析签到/签退任务
        self._tasks: list[TaskDef] = []
        for raw in schedule_cfg.get("tasks", []):
            self._tasks.append(
                TaskDef(
                    action=raw["action"],
                    target_time=_parse_time(raw["time"]),
                    tolerance=int(raw.get("tolerance", 60)),
                    random_window=int(raw.get("random_window", 0)),
                )
            )
            logger.info(
                f"📋 加载任务: {raw['action']} @ {raw['time']}"
                + (f", random_window={raw['random_window']}s" if raw.get("random_window") else "")
            )

        # 状态持久化（仅 auto 模式）
        cache_dir = os.environ.get("CACHE_DIR", ".")
        self._use_state_file = self._check_auto_mode()
        self._state_file = (
            os.path.join(cache_dir, ".schedule_state.json")
            if self._use_state_file
            else None
        )

        # 运行时状态
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._today_tasks: list[_ScheduledTask] = []
        self._tasks_date: Optional[datetime.date] = None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def start(self):
        """启动调度器（后台守护线程）"""
        if self._thread is not None:
            raise RuntimeError("调度器已在运行中")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="sign-scheduler"
        )
        self._thread.start()
        logger.info("🕐 调度器已启动")

    def stop(self, timeout: float = 30.0):
        """通知调度器停止，等待线程退出"""
        if self._thread is None:
            return
        logger.info("正在停止调度器...")
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("调度器线程未在超时时间内退出")
        else:
            logger.info("调度器已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run_loop(self):
        last_config_mtime = 0
        last_auto_backup = 0
        AUTO_BACKUP_CHECK = 600
        MAX_SLEEP = 60

        while not self._stop_event.is_set():
            try:
                self._reload_config_if_changed(last_config_mtime)
                last_config_mtime = self._get_config_mtime()

                now_ts = time.time()
                if now_ts - last_auto_backup >= AUTO_BACKUP_CHECK:
                    self._auto_backup()
                    last_auto_backup = now_ts

                if not self._enabled:
                    if self._stop_event.wait(MAX_SLEEP):
                        break
                    continue

                self._refresh_daily_tasks()

                if not self._today_tasks:
                    if self._stop_event.wait(MAX_SLEEP):
                        break
                    continue

                now = datetime.datetime.now()
                pending = [
                    t for t in self._today_tasks if not t.done and t.fire_at <= now
                ]

                if not pending:
                    future = [
                        t for t in self._today_tasks if not t.done and t.fire_at > now
                    ]
                    if not future:
                        if self._stop_event.wait(MAX_SLEEP):
                            break
                        continue

                    next_task = min(future, key=lambda t: t.fire_at)
                    wait_secs = max(
                        0,
                        (next_task.fire_at - now).total_seconds()
                        - next_task.task.tolerance,
                    )
                    wait_secs = min(wait_secs, MAX_SLEEP)
                    label = _action_label(next_task.task.action)
                    if wait_secs > 10:
                        logger.info(
                            f"⏳ 下一个任务: {label} "
                            f"安排在 {next_task.fire_at.strftime('%H:%M:%S')} "
                            f"(等待 {wait_secs/60:.1f} 分钟)"
                        )
                    if self._stop_event.wait(wait_secs):
                        break
                    continue

                target = min(pending, key=lambda t: t.fire_at)
                now = datetime.datetime.now()
                late_secs = (now - target.fire_at).total_seconds()
                skip_threshold = (
                    max(target.task.tolerance, 300)
                    if target.task.tolerance > 0
                    else 300
                )
                if late_secs > skip_threshold:
                    logger.warning(
                        f"⚠️ 任务 {_action_label(target.task.action)} "
                        f"({target.fire_at.strftime('%H:%M:%S')}) "
                        f"已超时 {late_secs:.0f}s (阈值 {skip_threshold}s)，跳过"
                    )
                    target.done = True
                    continue

                if late_secs > 60:
                    logger.info(
                        f"⏰ 任务延迟 {late_secs:.0f}s 仍在可接受范围，立即执行"
                    )

                self._execute_task(target)
                target.done = True

            except Exception:
                logger.exception("调度循环异常，60 秒后重试")
                if self._stop_event.wait(60):
                    break

    # ------------------------------------------------------------------
    # 配置热加载
    # ------------------------------------------------------------------

    @staticmethod
    def _get_config_mtime() -> float:
        try:
            return os.path.getmtime(_CONFIG_PATH)
        except Exception:
            return 0

    def _reload_config_if_changed(self, last_mtime: float):
        current_mtime = self._get_config_mtime()
        if current_mtime <= last_mtime or current_mtime == 0:
            return

        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            schedule_cfg = cfg.get("schedule", {})

            new_auto = str(cfg.get("auth_mode", "auto")).strip().lower() == "auto"
            if new_auto != self._use_state_file:
                logger.info(
                    f"🔐 认证模式已切换，state 持久化: {'开启' if new_auto else '关闭'}"
                )
                self._use_state_file = new_auto
                self._state_file = (
                    os.path.join(os.environ.get("CACHE_DIR", "."), ".schedule_state.json")
                    if new_auto
                    else None
                )

            new_enabled = schedule_cfg.get("enabled", True)
            if new_enabled != self._enabled:
                status = "开启" if new_enabled else "关闭"
                logger.info(f"🔛 定时任务开关已更新: {status}")
                self._enabled = new_enabled

            new_workdays = schedule_cfg.get("workdays", [1, 2, 3, 4, 5])
            if new_workdays != self._workdays:
                logger.info(f"🔄 工作日已更新: {new_workdays}")
                self._workdays = new_workdays
                self._tasks_date = None

            self._retry_interval = schedule_cfg.get("retryIntervalSeconds", 300)
            self._max_retries = schedule_cfg.get("maxRetries", 3)

            new_tasks = []
            for raw in schedule_cfg.get("tasks", []):
                new_tasks.append(
                    TaskDef(
                        action=raw["action"],
                        target_time=_parse_time(raw["time"]),
                        tolerance=int(raw.get("tolerance", 60)),
                        random_window=int(raw.get("random_window", 0)),
                    )
                )
                logger.info(
                    f"🔄 重新加载任务: {raw['action']} @ {raw['time']}"
                    + (
                        f", random_window={raw['random_window']}s"
                        if raw.get("random_window")
                        else ""
                    )
                )

            if len(new_tasks) != len(self._tasks) or any(
                a.action != b.action
                or a.target_time != b.target_time
                or a.random_window != b.random_window
                or a.tolerance != b.tolerance
                for a, b in zip(new_tasks, self._tasks)
            ):
                logger.info("📋 检测到任务变更，重新计算今日任务")
                self._tasks = new_tasks
                self._tasks_date = None

        except Exception as e:
            logger.warning(f"⚠️ 重新加载配置失败: {e}")

    # ------------------------------------------------------------------
    # 自动备份
    # ------------------------------------------------------------------

    def _auto_backup(self):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            ab = cfg.get("auto_backup")
            if not isinstance(ab, dict) or not ab.get("enabled"):
                return

            interval_hours = int(ab.get("interval_hours", 24))
            max_backups = int(ab.get("max_backups", 30))
            backup_dir = os.path.join(os.path.dirname(_CONFIG_PATH), "backups")
            os.makedirs(backup_dir, exist_ok=True)

            existing = sorted(
                f
                for f in os.listdir(backup_dir)
                if f.startswith("config.backup.") and f.endswith(".json")
            )
            if existing:
                latest = existing[-1]
                try:
                    ts_str = latest.replace("config.backup.", "").replace(".json", "")
                    latest_ts = time.mktime(time.strptime(ts_str, "%Y%m%d_%H%M%S"))
                    if time.time() - latest_ts < interval_hours * 3600:
                        return
                except (ValueError, OSError):
                    pass

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"config.backup.{ts}.json"
            fpath = os.path.join(backup_dir, fname)
            with open(fpath, "w", encoding="utf-8") as dst:
                json.dump(cfg, dst, ensure_ascii=False, indent=4)

            logger.info(f"📦 自动备份: {fname}")

            all_backups = sorted(
                f
                for f in os.listdir(backup_dir)
                if f.startswith("config.backup.") and f.endswith(".json")
            )
            while len(all_backups) > max_backups:
                oldest = all_backups.pop(0)
                try:
                    os.remove(os.path.join(backup_dir, oldest))
                    logger.info(f"🗑️ 清理旧备份: {oldest}")
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"⚠️ 自动备份失败: {e}")

    # ------------------------------------------------------------------
    # 状态持久化辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _check_auto_mode() -> bool:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return str(cfg.get("auth_mode", "auto")).strip().lower() == "auto"
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 每日任务刷新
    # ------------------------------------------------------------------

    def _refresh_daily_tasks(self):
        today = datetime.date.today()

        if self._tasks_date == today:
            return
        self._tasks_date = today

        is_workday = today.isoweekday() in self._workdays

        today_done = self._load_done_state(today.isoformat())
        if today_done:
            logger.info(f"📥 从持久化状态恢复: 今天已完成 {', '.join(today_done)}")

        self._today_tasks = []
        if self._tasks and is_workday:
            logger.info(f"--- 📅 {today} 任务安排 ---")

        for task in self._tasks:
            if not is_workday:
                continue

            base = datetime.datetime.combine(today, task.target_time)

            if task.random_window > 0:
                offset = random.randint(0, task.random_window)
                if task.action == "sign_in":
                    fire_at = base - datetime.timedelta(seconds=offset)
                else:
                    fire_at = base + datetime.timedelta(seconds=offset)
            else:
                fire_at = base
                offset = 0

            scheduled = _ScheduledTask(fire_at=fire_at, task=task)
            done_key = (
                f"{task.action}@{task.target_time.strftime('%H:%M:%S')}@{task.tolerance}"
            )
            if done_key in today_done:
                scheduled.done = True
            self._today_tasks.append(scheduled)

            label = _action_label(task.action)
            direction = "前" if task.action == "sign_in" else "后"
            offset_info = (
                f" (随机偏移 {offset}s，在{task.target_time.strftime('%H:%M:%S')}之{direction})"
                if offset
                else ""
            )
            status = " [已完成]" if scheduled.done else ""
            logger.info(
                f"  📌 {label} → {fire_at.strftime('%H:%M:%S')}{offset_info}{status}"
            )

        self._today_tasks.sort(key=lambda t: t.fire_at)

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------

    def _execute_task(self, target: _ScheduledTask):
        action = target.task.action
        label = _action_label(action)
        logger.info(
            f"🚀 开始执行: {label} (计划 {target.fire_at.strftime('%H:%M:%S')})"
        )

        for attempt in range(1, self._max_retries + 1):
            try:
                result = self._sign_callback(action)
                if result.get("success"):
                    logger.info(f"✅ {label} 成功 (第 {attempt} 次尝试)")
                    done_key = f"{action}@{target.task.target_time.strftime('%H:%M:%S')}@{target.task.tolerance}"
                    self._save_done_state(
                        target.fire_at.date().isoformat(), done_key
                    )
                    return
                else:
                    logger.warning(f"⚠️ {label} 返回失败: {result.get('message')}")
            except Exception as e:
                logger.error(f"❌ {label} 第 {attempt} 次尝试异常: {e}")

            if attempt < self._max_retries:
                logger.info(
                    f"⏳ {self._retry_interval} 秒后重试 ({attempt}/{self._max_retries})..."
                )
                remaining = self._retry_interval
                CHUNK = 60
                while remaining > 0:
                    wait = min(remaining, CHUNK)
                    if self._stop_event.wait(wait):
                        return
                    remaining -= wait
                    if not self._enabled:
                        logger.info("🛑 定时任务已关闭，放弃重试")
                        return

        logger.error(f"💀 {label} 失败，已达最大重试次数 ({self._max_retries})")

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load_done_state(self, date_str: str) -> set:
        if not self._use_state_file or not self._state_file:
            return set()
        try:
            if not os.path.exists(self._state_file):
                return set()
            with open(self._state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("date") == date_str:
                return set(state.get("done", []))
        except Exception:
            pass
        return set()

    def _save_done_state(self, date_str: str, action: str):
        if not self._use_state_file or not self._state_file:
            return
        existing = self._load_done_state(date_str)
        existing.add(action)

        state = {"date": date_str, "done": list(existing)}
        with open(self._state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        try:
            os.chmod(self._state_file, 0o600)
        except Exception:
            pass
        logger.debug(f"💾 状态已持久化: {date_str} → {action}")


def _parse_time(s: str) -> datetime.time:
    parts = s.strip().split(":")
    if len(parts) == 2:
        return datetime.time(int(parts[0]), int(parts[1]))
    if len(parts) == 3:
        return datetime.time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(
        f"无法解析时间字符串: '{s}'，支持格式: HH:MM 或 HH:MM:SS"
    )


def _action_label(action: str) -> str:
    return {"sign_in": "签到", "sign_out": "签退"}.get(action, action)