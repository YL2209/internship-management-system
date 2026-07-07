import os
import time
import json
import threading
from core.config.paths import get_log_dir
import logging
logger = logging.getLogger(__name__)


def _ts_ms() -> str:
    """返回毫秒级时间戳（精确到毫秒，用于日志和排序）。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]




# ============================================================
# 结构化 HTTP 请求日志（写入 logs/requests.log.jsonl）
# ============================================================

# HTTP 日志文件路径
_HTTP_LOG_PATH = os.path.join(get_log_dir(), "requests.log.jsonl")
_HTTP_LOG_MAX_BYTES = 5 * 1024 * 1024   # 单文件最大 5MB
_HTTP_LOG_BACKUPS = 5                    # 保留 5 个备份文件
# 写入锁
_http_log_lock = threading.Lock()

# 敏感字段名（请求/响应中这些字段的值会被脱敏，防止 token/密码泄露）
_SENSITIVE_KEYS = {
    "password", "encryptvalue", "encryptValue", "sessionId",
    "sessionid", "jsessionid", "JSESSIONID", "token", "access_token",
    "authorization", "Authorization", "cookie", "Cookie", "set-cookie",
    "x-auth-token", "api_key", "secret", "unionId", "unionid",
    "openId", "openid", "devicecode", "deviceCode", "key"
}


def _sanitize_value(key: str, value) -> str:
    """对敏感字段值脱敏：保留前 4 位，其余替换为 ***"""
    s = str(value)
    if any(sk.lower() in str(key).lower() for sk in _SENSITIVE_KEYS):
        if len(s) <= 4:
            return "***"
        return s[:4] + "*" * (len(s) - 8) + s[-4:] if len(s) > 8 else s[:4] + "***"
    return s


def _sanitize_dict(d: dict) -> dict:
    """递归对字典中所有敏感字段脱敏"""
    if not isinstance(d, dict):
        return d
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            result[k] = [_sanitize_dict(i) if isinstance(i, dict) else _sanitize_value(k, i) for i in v]
        else:
            result[k] = _sanitize_value(k, v)
    return result

def _sanitize_body_str(body_str: str, max_len: int = 5000) -> str:
    """对 HTTP body 字符串做脱敏 + 截断"""
    if not body_str:
        return ""
    try:
        # 尝试按 JSON 解析
        parsed = json.loads(body_str)
        if isinstance(parsed, dict):
            sanitized = _sanitize_dict(parsed)
            # 转回 JSON 字符串
            body_str = json.dumps(sanitized, ensure_ascii=False)
        # 如果是 list 等非 dict JSON，保持原样（或你可以扩展 _sanitize_dict 处理 list）
    except (json.JSONDecodeError, TypeError):
        # 不是合法 JSON（如纯文本、HTML），保持原字符串
        pass
    return _truncate(body_str, max_len)

def _truncate(s: str, max_len: int = 800) -> str:
    """截断过长字符串"""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"...(截断，原长度 {len(s)})"


def _log_http_request(
    action: str = "",
    url: str = "",
    method: str = "POST",
    req_headers: dict = None,
    req_params: dict = None,
    req_cookies: dict = None,
    req_body: str = "",
    resp_status: int = 0,
    resp_headers: dict = None,
    resp_body: str = "",
    duration_ms: int = 0,
    error: str = "",
):
    """
    记录一次完整的 HTTP 请求/响应到 logs/requests.log.jsonl。

    所有敏感字段自动脱敏，响应体超过 800 字符自动截断。
    """
    record = {
        "timestamp": _ts_ms(),
        "action": action,
        "url": url,
        "method": method,
        "req_headers": _sanitize_dict(req_headers or {}),
        "req_params": _sanitize_dict(req_params or {}),
        "req_cookies": _sanitize_dict(req_cookies or {}),
        "req_body": _sanitize_body_str(req_body) if req_body else "",
        "resp_status": resp_status,
        "resp_headers": _sanitize_dict(resp_headers or {}),
        "resp_body": _sanitize_body_str(resp_body) if resp_body else "",
        "duration_ms": duration_ms,
        "error": error,
    }

    try:
        log_dir = os.path.dirname(_HTTP_LOG_PATH)
        os.makedirs(log_dir, exist_ok=True)
        with _http_log_lock:
            _rotate_http_log_if_needed()
            with open(_HTTP_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"写入 HTTP 请求日志失败: {e}")


def _rotate_http_log_if_needed():
    """轮转 HTTP 请求日志文件（类似 RotatingFileHandler）。"""
    if not os.path.exists(_HTTP_LOG_PATH):
        return
    if os.path.getsize(_HTTP_LOG_PATH) <= _HTTP_LOG_MAX_BYTES:
        return
    for i in range(_HTTP_LOG_BACKUPS - 1, 0, -1):
        src = f"{_HTTP_LOG_PATH}.{i}"
        dst = f"{_HTTP_LOG_PATH}.{i + 1}"
        if os.path.exists(src):
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
    os.rename(_HTTP_LOG_PATH, _HTTP_LOG_PATH + ".1")




# ------------------------------------------------------------------
# 签到记录（写入 logs/records.jsonl 供 Web 界面查询）
# ------------------------------------------------------------------

# 记录文件写锁（同进程内线程安全）
_record_lock = threading.Lock()

# 记录文件路径
RECORDS_FILE = os.path.join(get_log_dir(), "records.jsonl")


def log_record(config: dict, action: str, success: bool, message: str,
               geo: dict = None, elapsed: float = 0, trainee_id: str = "",
               lat_lng: dict = None, auth_mode: str = "auto",
               record_file: str = None,
               # ---- 直接传值（优先级高于 geo/lat_lng/config 提取） ----
               address: str = "",
               adcode: str = "",
               latitude: str = "",
               longitude: str = "",
               elapsed_ms: int = 0) -> dict:
    """
    将本次签到/签退结果追加写入 records.jsonl（JSON Lines 格式），线程安全。

    参数:
        config:      配置字典（需包含 location、device 等字段）
        action:      签到类型（sign_in / sign_out / 补签签到申请 / 补签签退申请）
        success:     是否成功
        message:     结果消息
        geo:         逆地理编码结果（含 formatted_address、addressComponent.adcode）
        elapsed:     耗时（秒），与 elapsed_ms 二选一即可
        trainee_id:  实习生 ID
        lat_lng:     最终使用的经纬度 dict {longitude, latitude}
        auth_mode:   认证模式（auto / manual）
        record_file: 记录文件路径，默认 logs/records.jsonl
        address:     直接传入地址（优先级高于 geo 提取）
        adcode:      直接传入区域编码（优先级高于 geo 提取）
        latitude:    直接传入纬度（优先级高于 lat_lng/config 提取）
        longitude:   直接传入经度（优先级高于 lat_lng/config 提取）
        elapsed_ms:  直接传入毫秒耗时（优先级高于 elapsed 换算）

    返回:
        写入的记录 dict（用于调试或被调用方二次使用）
    """
    loc_cfg = config.get("location", {})
    lat_lng = lat_lng or {}

    # 地址/编码：优先使用直接传入的值，否则从 geo 提取
    final_address = address or (str(geo.get("formatted_address", "")) if geo else "")
    final_adcode  = adcode or (str(geo.get("addressComponent", {}).get("adcode", "")) if geo else "")
    # 经纬度：优先使用直接传入的值，否则从 lat_lng/config 提取
    final_lat     = latitude or str(lat_lng.get("latitude", loc_cfg.get("latitude", "")))
    final_lng     = longitude or str(lat_lng.get("longitude", loc_cfg.get("longitude", "")))
    # 耗时：优先使用直接传入的 millis，否则从 elapsed 换算
    final_elapsed = elapsed_ms or (int(elapsed * 1000) if elapsed else 0)

    record = {
        "timestamp":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "action":     action,
        "success":    success,
        "message":    message,
        "address":    final_address,
        "adcode":     final_adcode,
        "latitude":   final_lat,
        "longitude":  final_lng,
        "device":     str(config.get("device", {}).get("model", "")),
        "auth_mode":  auth_mode,
        "trainee_id": str(trainee_id),
        "elapsed_ms": final_elapsed,
    }

    filepath = record_file or RECORDS_FILE
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with _record_lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.debug("记录已写入: %s → %s", action, "成功" if success else "失败")
    except Exception as e:
        logger.warning("写入签到记录失败（不影响签到）: %s", e)
    return record

