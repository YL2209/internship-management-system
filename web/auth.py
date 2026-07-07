"""
Web 管理后台 — 认证与安全模块

提供:
- 密码哈希与验证 (bcrypt)
- JWT token 签发与校验
- 登录速率限制（防暴力破解）
- 管理员会话管理（8 小时超时）
"""

import hashlib
import logging
import os
import time
import threading
from functools import wraps

import bcrypt
import jwt
from flask import request, jsonify, current_app

# ============================================================
# 密码管理 (bcrypt)
# ============================================================

def hash_password(password: str) -> str:
    """
    使用 bcrypt 生成密码哈希。

    bcrypt 自带 salt，每次调用结果不同。
    工作因子默认 12（约 0.3s 计算时间）。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """验证密码是否匹配 bcrypt 哈希。"""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# 运行时密码哈希缓存（支持通过 API 修改）
_cached_password_hash: str | None = None


def get_admin_password_hash() -> str | None:
    """
    获取管理员密码哈希。

    优先级:
        1. 运行时缓存（通过 Web API 修改密码后设置）
        2. 环境变量 ADMIN_PASSWORD_HASH
        3. 环境变量 ADMIN_PASSWORD（明文，自动哈希）
        4. config.json 中的 web_admin_password_hash 字段
        5. 默认密码 "admin123" 的哈希
    """
    global _cached_password_hash
    if _cached_password_hash:
        return _cached_password_hash

    # 环境变量优先
    env_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    if env_hash:
        return env_hash

    env_plain = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_plain:
        return hash_password(env_plain)

    # config.json 中的持久化密码哈希
    try:
        import json as _json
        config_path = os.environ.get("CONFIG_PATH",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            file_hash = cfg.get("web_admin_password_hash", "").strip()
            if file_hash:
                _cached_password_hash = file_hash
                return file_hash
    except Exception:
        pass

    # 未配置密码：记录严重警告但继续运行（Docker Compose 应始终注入 ADMIN_PASSWORD）
    logging.getLogger("web.auth").critical(
        "未设置管理员密码！请设置 ADMIN_PASSWORD 环境变量或 config.json 中的 web_admin_password_hash 字段。"
        " 当前回退为默认密码，这在生产环境中是严重安全隐患。"
    )
    return hash_password("admin123")


def set_admin_password_plain(plain_password: str):
    """
    设置新的管理员密码（明文），内部自动哈希。
    此修改仅在当前进程生命周期内有效。
    """
    global _cached_password_hash
    _cached_password_hash = hash_password(plain_password)


# ============================================================
# JWT Token 管理
# ============================================================

# JWT 签名密钥（从环境变量读取，默认随机生成——重启后所有 token 失效）
_JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not _JWT_SECRET:
    _JWT_SECRET = hashlib.sha256(os.urandom(64)).hexdigest()
    # 注意：随机密钥意味着每次重启服务后所有已签发的 token 失效

JWT_SECRET = _JWT_SECRET
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = int(os.environ.get("JWT_EXPIRATION_HOURS", "8"))


def create_token(username: str = "admin") -> str:
    """
    签发 JWT token。

    payload 字段:
        - sub: 用户名
        - iat: 签发时间
        - exp: 过期时间
        - jti: 唯一 token ID（用于撤销）
    """
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + JWT_EXPIRATION_HOURS * 3600,
        "jti": hashlib.md5(f"{username}{now}{os.urandom(8)}".encode()).hexdigest()[:16],
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """
    验证 JWT token，返回 payload 或 None。

    验证过期时间、签名、算法。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def token_required(f):
    """
    Flask 视图装饰器：要求请求头携带有效的 JWT token。

    Authorization: Bearer <token>

    对于页面请求（text/html），token 可从 cookie 中读取。
    对于 API 请求（application/json），token 从 Authorization 头读取。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None

        # 优先从 Authorization 头获取
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # 回退到 cookie
        if not token:
            token = request.cookies.get("auth_token")

        if not token:
            return jsonify({"success": False, "message": "未提供认证 token"}), 401

        payload = verify_token(token)
        if payload is None:
            return jsonify({"success": False, "message": "token 无效或已过期"}), 401

        # 将用户名注入请求上下文
        request.current_user = payload.get("sub", "admin")
        return f(*args, **kwargs)

    return decorated


# ============================================================
# 登录速率限制（防止暴力破解）
# ============================================================

class RateLimiter:
    """
    基于内存的简单速率限制器。

    规则:
        - 同一 IP 每分钟最多 MAX_REQUESTS 次登录尝试
        - 超过后锁定 LOCKOUT_SECONDS 秒
        - 最多追踪 MAX_ENTRIES 个 IP（防止内存溢出）
    """

    MAX_REQUESTS = int(os.environ.get("LOGIN_RATE_LIMIT", "10"))      # 每分钟
    LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "300"))  # 5 分钟
    MAX_ENTRIES = 10000

    def __init__(self):
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}  # ip → [timestamp, ...]

    def is_allowed(self, ip: str) -> tuple[bool, int]:
        """
        检查 IP 是否允许登录。

        返回: (allowed: bool, retry_after_seconds: int)
        """
        now = time.time()
        window_start = now - 60  # 1 分钟窗口

        with self._lock:
            # 清理过期记录
            if ip in self._attempts:
                self._attempts[ip] = [t for t in self._attempts[ip] if t > window_start]
            else:
                self._attempts[ip] = []

            attempts = self._attempts[ip]

            # 检查是否被锁定
            if len(attempts) >= self.MAX_REQUESTS:
                # 检查最早尝试是否已超出锁定期
                oldest = min(attempts)
                lockout_end = oldest + self.LOCKOUT_SECONDS
                if now < lockout_end:
                    return False, int(lockout_end - now)
                else:
                    # 锁定期已过，清除记录
                    self._attempts[ip] = []

            # 清理整体条目数（防止内存溢出）
            if len(self._attempts) > self.MAX_ENTRIES:
                # 删除最久未活动的条目
                sorted_ips = sorted(
                    self._attempts.keys(),
                    key=lambda k: max(self._attempts[k]) if self._attempts[k] else 0,
                )
                for old_ip in sorted_ips[: len(sorted_ips) // 2]:
                    del self._attempts[old_ip]

            return True, 0

    def record_attempt(self, ip: str):
        """记录一次登录尝试。"""
        with self._lock:
            if ip not in self._attempts:
                self._attempts[ip] = []
            self._attempts[ip].append(time.time())


_rate_limiter = RateLimiter()


def check_rate_limit(ip: str) -> tuple[bool, str]:
    """
    检查登录速率限制。

    返回: (allowed: bool, error_message: str)
    """
    allowed, retry_after = _rate_limiter.is_allowed(ip)
    if not allowed:
        minutes = retry_after // 60
        seconds = retry_after % 60
        return False, f"登录尝试过于频繁，请在 {minutes}分{seconds}秒 后重试"
    return True, ""


def record_login_attempt(ip: str):
    """记录一次登录尝试（成功或失败）。"""
    _rate_limiter.record_attempt(ip)
