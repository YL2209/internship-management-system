# web/cache.py
import json
import logging
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from core.config.paths import get_project_root

logger = logging.getLogger(__name__)


# 缓存目录与备份目录
_CACHE_DIR = os.path.join(get_project_root(), "cache")
_BACKUP_DIR = os.path.join(get_project_root(), "backups", "cache_backups")

_CLOCK_DETAIL_CACHE_FILE = os.path.join(_CACHE_DIR, "clock_detail_cache.json")
_JOURNAL_LIST_CACHE_FILE = os.path.join(_CACHE_DIR, "journal_list_cache.json")
_RANKING_CACHE_FILE = os.path.join(_CACHE_DIR, "ranking_cache.json")
# JSESSIONID 本地缓存文件默认路径
DEFAULT_CACHE_FILE = os.path.join(_CACHE_DIR, ".session_cache.json")

# 锁（每个文件独立，避免相互阻塞）
_lock_clock_detail = threading.Lock()
_lock_journal_list = threading.Lock()
_lock_ranking = threading.Lock()
_lock_session = threading.Lock()


# ---------- TTL 获取（与登录会话解耦，默认24小时） ----------
def _get_cache_ttl(cache_type: str) -> int:
    """
    获取指定缓存类型的过期时间（秒）。
    优先级：config.json 中的 `cache_ttl_{cache_type}` > `cache_ttl` > 默认 86400。
    """
    try:
        from web.config_manager import read_config
        cfg = read_config()
        # 尝试读取特定缓存类型的 TTL
        specific_key = f"cache_ttl_{cache_type}"
        if specific_key in cfg:
            return int(cfg[specific_key])
        # 回退到通用 cache_ttl
        return int(cfg.get("cache_ttl", 86400))
    except Exception:
        return 86400

# ---------- 原子写入 ----------
def _atomic_write_json(filepath: str, data: dict) -> None:
    """先写临时文件，再原子替换（带 fsync）。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

# ---------- 普通读取 ----------
def _read_json(filepath: str) -> dict:
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("读取缓存文件失败 (%s): %s", filepath, e)
        return {}

# ---------- 备份过期条目 ----------
def _backup_expired_entries(expired_entries: dict, cache_type: str) -> bool:
    if not expired_entries:
        return True
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{cache_type}_backup_{ts}.json"
    filepath = os.path.join(_BACKUP_DIR, filename)
    backup_data = {
        "backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cache_type": cache_type,
        "expired_entries": expired_entries,
    }
    try:
        _atomic_write_json(filepath, backup_data)
        logger.info(f"缓存备份已保存: {filepath} (共 {len(expired_entries)} 条)")
        return True
    except Exception as e:
        logger.error(f"缓存备份失败: {e}")
        return False

# ---------- 清理过期 ----------
def _clean_expired(cache: dict, ttl: int, cache_type: str, backup: bool = True) -> dict:
    now_ts = time.time()
    expired = {k: v for k, v in cache.items() if v.get("expire_at", 0) < now_ts}
    if not expired:
        return cache
    if backup:
        if not _backup_expired_entries(expired, cache_type):
            logger.warning(f"{cache_type} 过期条目备份失败，跳过清理")
            return cache
    valid = {k: v for k, v in cache.items() if v.get("expire_at", 0) >= now_ts}
    logger.info(f"{cache_type} 已清理 {len(expired)} 条过期条目（{'已备份' if backup else '未备份'}），剩余 {len(valid)} 条")
    return valid

# ---------- 通用加载 ----------
def _load_cache(filepath: str, lock: threading.Lock) -> dict:
    with lock:
        cache = _read_json(filepath)
        if not cache:
            return {}
        return cache

# ---------- 通用保存 ----------
def _save_cache(filepath: str, lock: threading.Lock, cache_type: str, cache: dict, backup: bool = True) -> None:
    with lock:
        ttl = _get_cache_ttl(cache_type)
        cleaned = _clean_expired(cache, ttl, cache_type, backup=backup)
        _atomic_write_json(filepath, cleaned)

# ---------- 通用清除 ----------
def _invalidate_cache(filepath: str, lock: threading.Lock, cache_type: str, backup: bool = True) -> None:
    with lock:
        cache = _read_json(filepath)
        if not cache:
            return
        if not backup:
            try:
                os.unlink(filepath)
                logger.info(f"已清除缓存文件（未备份）: {filepath}")
            except OSError as e:
                logger.error(f"删除缓存文件失败 ({filepath}): {e}")
            return
        if _backup_expired_entries(cache, cache_type):
            try:
                os.unlink(filepath)
                logger.info(f"已清除缓存文件: {filepath}")
            except OSError as e:
                logger.error(f"删除缓存文件失败 ({filepath}): {e}")
        else:
            logger.error(f"备份失败，保留原缓存文件: {filepath}")

# ---------- 对外接口 ----------
def load_clock_detail_cache() -> dict:
    return _load_cache(_CLOCK_DETAIL_CACHE_FILE, _lock_clock_detail)

def load_journal_list_cache() -> dict:
    return _load_cache(_JOURNAL_LIST_CACHE_FILE, _lock_journal_list)

def load_ranking_cache() -> dict:
    return _load_cache(_RANKING_CACHE_FILE, _lock_ranking)

def save_clock_detail_cache(cache: dict) -> None:
    _save_cache(_CLOCK_DETAIL_CACHE_FILE, _lock_clock_detail, "clock_detail", cache, backup=False)

def save_journal_list_cache(cache: dict) -> None:
    _save_cache(_JOURNAL_LIST_CACHE_FILE, _lock_journal_list, "journal_list", cache, backup=False)

def save_ranking_cache(cache: dict) -> None:
    _save_cache(_RANKING_CACHE_FILE, _lock_ranking, "ranking", cache)

def invalidate_ranking_cache() -> None:
    _invalidate_cache(_RANKING_CACHE_FILE, _lock_ranking, "ranking")

def invalidate_clock_detail_cache() -> None:
    _invalidate_cache(_CLOCK_DETAIL_CACHE_FILE, _lock_clock_detail, "clock_detail", backup=False)

def invalidate_journal_cache(blog_type: str) -> None:
    """清除指定 blog_type 的所有分页缓存（线程安全）"""
    with _lock_journal_list:
        cache = _read_json(_JOURNAL_LIST_CACHE_FILE)
        prefix = f"blog_type_{blog_type}_page_"
        keys_to_delete = [k for k in cache if k.startswith(prefix)]
        if not keys_to_delete:
            return
        expired = {k: cache[k] for k in keys_to_delete}
        for k in keys_to_delete:
            del cache[k]
        _atomic_write_json(_JOURNAL_LIST_CACHE_FILE, cache)
        logger.info(f"已清除 {len(keys_to_delete)} 条周记缓存（未备份，blog_type={blog_type})")


def get_journal_list_cache_stats() -> dict:
    """获取周记列表缓存统计（只读，不触发清理）"""
    cache = _read_json(_JOURNAL_LIST_CACHE_FILE)
    total_entries = len(cache)
    weekly_pages = sum(1 for k in cache if k.startswith("blog_type_1_"))
    monthly_pages = sum(1 for k in cache if k.startswith("blog_type_2_"))
    latest = ""
    for v in cache.values():
        if isinstance(v, dict):
            cached_at = v.get("cached_at", "")
            if cached_at > latest:
                latest = cached_at
    now = time.time()
    expired = sum(1 for v in cache.values()
                  if isinstance(v, dict) and v.get("expire_at", 0) < now)
    return {
        "total_entries": total_entries,
        "weekly_pages": weekly_pages,
        "monthly_pages": monthly_pages,
        "latest_cached_at": latest,
        "expired_count": expired,
    }

def get_clock_detail_cache_stats() -> dict:
    """获取签到详情缓存统计（只读，不触发清理）"""
    cache = _read_json(_CLOCK_DETAIL_CACHE_FILE)
    total_ranges = len(cache)
    total_records = sum(len(v.get("data", [])) for v in cache.values() if isinstance(v, dict))
    latest = ""
    for v in cache.values():
        if isinstance(v, dict):
            cached_at = v.get("cached_at", "")
            if cached_at > latest:
                latest = cached_at
    now = time.time()
    expired = sum(1 for v in cache.values()
                  if isinstance(v, dict) and v.get("expire_at", 0) < now)
    return {
        "total_ranges": total_ranges,
        "total_records": total_records,
        "latest_cached_at": latest,
        "expired_count": expired,
    }

def get_ranking_cache_stats() -> dict:
    """获取排行榜缓存统计（只读，不触发清理）"""
    cache = _read_json(_RANKING_CACHE_FILE)
    total_months = len(cache)
    latest = ""
    for v in cache.values():
        if isinstance(v, dict):
            cached_at = v.get("cached_at", "")
            if cached_at > latest:
                latest = cached_at
    now = time.time()
    expired = sum(1 for v in cache.values()
                  if isinstance(v, dict) and v.get("expire_at", 0) < now)
    return {
        "total_months": total_months,
        "latest_cached_at": latest,
        "expired_count": expired,
    }



# ------------------------------------------------------------------
# JSESSIONID 会话缓存管理（纯模块级函数，不绑定类）
# ------------------------------------------------------------------


def _check_session_valid(response_json: dict) -> bool:
    """
    检查响应 JSON 是否表示会话仍然有效。

    当响应类似 {'code': '205', 'data': None, 'msg': '未登录', ...} 时返回 False
    反之返回 True。

    这是整个项目的会话校验入口，requests.py 中的 _assert_session 也依赖于此。
    """
    if not isinstance(response_json, dict):
        return True
    code = response_json.get("code")
    msg = response_json.get("msg", "")
    if code == "205" or code == 205 or "未登录" in str(msg):
        return False
    return True


def check_session_validity(response_json: dict) -> bool:
    """_check_session_valid 的别名，保持向后兼容。"""
    return _check_session_valid(response_json)


def load_session_cache(cache_file=None, expire_seconds=None, old_sessionId=False) -> dict | None:
    """
    从本地文件加载 JSESSIONID 缓存。

    参数:
        cache_file:     缓存文件路径，默认 DEFAULT_CACHE_FILE
        expire_seconds: 过期时间（秒），默认 86400（24 小时）

    返回:
        有效的缓存字典 {openId, unionId, encryptValue, sessionId, traineeId, planId}
        文件不存在 / 损坏 / 过期 → None
    """
    filepath = Path(cache_file if cache_file else DEFAULT_CACHE_FILE)
    if not filepath.exists():
        return None
    try:
        cache = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return None
    ttl = expire_seconds if expire_seconds else 86400
    if  old_sessionId and time.time() - cache.get("timestamp", 0) > ttl:
        logger.info("会话缓存已过期（超过 %.0f 小时）", ttl / 3600)
        return None
    return {
        "openId": cache.get("openId"),
        "unionId": cache.get("unionId"),
        "encryptValue": cache.get("encryptValue"),
        "sessionId": cache.get("sessionId"),
        "traineeId": cache.get("traineeId"),
        "planId": cache.get("planId"),
    }


def save_session_cache(args: dict, plan_data: dict = None,
                       cache_file=None, expire_seconds=None):
    """
    保存 JSESSIONID 缓存到本地文件。

    参数:
        args:           登录结果 {openId, unionId, encryptValue, sessionId}
        plan_data:      实习计划数据（包含 clockVo.traineeId、clockVo.planId）
        cache_file:     缓存文件路径，默认 DEFAULT_CACHE_FILE
        expire_seconds: 过期时间（秒），默认 86400（24 小时）
    """
    filepath = Path(cache_file if cache_file else DEFAULT_CACHE_FILE)
    ttl = expire_seconds if expire_seconds else 86400
    record = {
        "sessionId": args["sessionId"],
        "encryptValue": args["encryptValue"],
        "openId": args["openId"],
        "unionId": args["unionId"],
        "timestamp": int(time.time()),
        "expire_seconds": ttl,
    }
    if plan_data and plan_data.get("clockVo"):
        record["traineeId"] = str(plan_data["clockVo"]["traineeId"])
        record["planId"] = str(plan_data["clockVo"]["planId"])

    # 保留已有的 securityFingerprint（登录前的 _get_open_id 可能已写入）
    try:
        if filepath.exists():
            old = json.loads(filepath.read_text(encoding="utf-8"))
            if old.get("securityFingerprint"):
                record["securityFingerprint"] = old["securityFingerprint"]
    except Exception:
        pass

    with _lock_session:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("会话缓存已保存到 %s（%.0fh 有效）", filepath, ttl / 3600)


def clear_session_cache(cache_file=None):
    """
    删除本地 JSESSIONID 缓存文件。

    参数:
        cache_file: 缓存文件路径，默认 DEFAULT_CACHE_FILE
    """
    filepath = Path(cache_file if cache_file else DEFAULT_CACHE_FILE)
    with _lock_session:
        if not filepath.exists():
            return
        try:
            filepath.unlink()
            logger.info("已清除缓存文件: %s", filepath)
        except OSError as e:
            logger.error("删除缓存文件失败 (%s): %s", filepath, e)


def handle_invalid_session(cache_file=None):
    """处理失效的会话：清除缓存并记录警告。"""
    clear_session_cache(cache_file)
    logger.warning("JSESSIONID 已失效，已清除缓存，请重新获取 code")

