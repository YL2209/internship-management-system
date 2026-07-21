#!/usr/bin/env python3
"""
实习数据管理系统 — Web 管理后台

本模块基于 ckkkf/sign-sign-in (https://github.com/ckkkf/sign-sign-in) 改造，
提供浏览器端配置管理与签到控制界面。原项目采用 MIT 许可证。

提供:
- 管理员登录（JWT + bcrypt + 速率限制）
- 配置查看与修改（原子写入）
- 签到日志查看
- 美观的 Bootstrap 5 管理界面

用法:
    python web/app.py                          # 默认端口 5000
    ADMIN_PASSWORD=xxx python web/app.py        # 设置管理员密码
    CONFIG_PATH=/app/config.json python web/app.py  # 指定配置文件路径

环境变量:
    ADMIN_PASSWORD        管理员明文密码（首次启动时自动哈希）
    ADMIN_PASSWORD_HASH   管理员密码哈希（优先级高于 ADMIN_PASSWORD）
    JWT_SECRET            JWT 签名密钥（随机生成 → 重启后 token 全部失效）
    JWT_EXPIRATION_HOURS  Token 过期小时数（默认 8）
    LOGIN_RATE_LIMIT      每分钟最大登录尝试次数（默认 10）
    LOGIN_LOCKOUT_SECONDS 触发限制后的锁定时长（默认 300）
    CONFIG_PATH           配置文件路径（默认 ../config.json）
    LOG_DIR               日志目录（默认 ../logs）
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time as _time

import requests as _requests
import re as _re

from core.apis.photo_sign import PhotoSignInManager

logger = logging.getLogger(__name__)


def _get_project_root() -> str:
    """返回项目根目录的绝对路径（web/ 的上级目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_log_dir() -> str:
    """返回日志目录，优先使用 LOG_DIR 环境变量。"""
    return os.environ.get("LOG_DIR", os.path.join(_get_project_root(), "logs"))

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
)
from flask_cors import CORS

try:
    from .auth import (
        get_admin_password_hash,
        verify_password,
        create_token,
        verify_token,
        token_required,
        check_rate_limit,
        record_login_attempt,
        set_admin_password_plain,
    )
    from .config_manager import read_config, write_config, validate_config_structure, CONFIG_PATH
except ImportError:
    # Fallback for direct execution: python web/app.py
    from auth import (  # type: ignore
        get_admin_password_hash,
        verify_password,
        create_token,
        verify_token,
        token_required,
        check_rate_limit,
        record_login_attempt,
        set_admin_password_plain,
    )
    from config_manager import read_config, write_config, validate_config_structure, CONFIG_PATH  # type: ignore

from core.utils.location import resolve_ip_location

from core.utils.cache import (
    load_clock_detail_cache,
    save_clock_detail_cache,
    invalidate_clock_detail_cache,
    load_journal_list_cache,
    save_journal_list_cache,
    invalidate_journal_cache,
    load_ranking_cache,
    save_ranking_cache,
    _backup_expired_entries,  # 替代 _backup_expired_cache
    _get_cache_ttl,  # 替代 _JOURNAL_LIST_CACHE_TTL 常量
    get_journal_list_cache_stats,
    get_clock_detail_cache_stats,
    get_ranking_cache_stats,
    _read_json, _JOURNAL_LIST_CACHE_FILE, _CLOCK_DETAIL_CACHE_FILE, _RANKING_CACHE_FILE
)

# ============================================================
# 应用初始化
# ============================================================

SIGN_LOG_PATH = os.path.join(
    _get_log_dir(),
    "sign.log",
)

def create_app() -> Flask:
    """创建并配置 Flask 应用。"""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # CORS（允许前端独立部署时跨域访问）
    CORS(app, supports_credentials=True, origins=os.environ.get("CORS_ORIGINS", "*"))

    # Session 配置（Flask 内置 session，用于 cookie 存储 token）
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 日志
    # ---------- 日志配置（同时输出到控制台和 sign.log）----------
    # 设置根 logger 级别
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 控制台 Handler（Docker logs 可见）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(console_handler)

    # 文件 Handler（写入 sign.log，带 [WEB] 标签）
    # SIGN_LOG_PATH 已在模块顶部定义：SIGN_LOG_PATH = os.path.join(_get_log_dir(), "sign.log")
    file_handler = RotatingFileHandler(
        SIGN_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [WEB] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(file_handler)

    # 避免 Flask 内部日志重复（Flask 默认会添加自己的 handler，这里我们统一管理）
    app.logger.handlers = []
    app.logger.propagate = True

    @app.before_request
    def restrict_static():
        if request.path.startswith('/static/protected/') and 'auth_token' not in request.cookies:
            return jsonify({"success": False, "message": "未登录"}), 401
    
    return app


app = create_app()

# ---- 定时签到守护线程 ----
# DAEMON_ENABLED=true 时在后台启动定时签到调度器
# 模块级引用，供 /api/daemon/status 端点查询状态
_daemon_scheduler = None


def _try_start_daemon():
    """尝试启动定时签到守护线程。模块加载时调用一次，也可在配置变更后重试。"""
    global _daemon_scheduler
    if _daemon_scheduler is not None:
        return  # 已在运行，不重复启动

    if os.environ.get("DAEMON_ENABLED", "").lower() not in ("1", "true", "yes"):
        return

    try:
        c = read_config()
        # 延迟导入，避免循环依赖
        if not os.path.dirname(__file__) in sys.path:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from run import start_daemon_thread

        _daemon_scheduler = start_daemon_thread(c, CONFIG_PATH)
        if _daemon_scheduler:
            import atexit
            atexit.register(_daemon_scheduler.stop)
            app.logger.info("✅ 定时签到守护线程已启动")
        else:
            app.logger.warning("⚠️ 定时签到守护线程未启动（配置条件不满足）")
    except Exception as _e:
        app.logger.error(f"❌ 守护线程启动失败（不影响 Web 服务）: {_e}")


# 模块加载时自动尝试启动
_try_start_daemon()

# ============================================================
# 页面路由（HTML 模板）
# ============================================================

@app.route("/")
def index():
    """首页 → 已登录跳转管理页，否则跳转登录页。"""
    token = request.cookies.get("auth_token")
    if token and verify_token(token):
        return redirect("/admin/dashboard")
    return render_template("login.html")


@app.route("/login")
def login_page():
    """登录页面（传递腾讯地图 Key 给前端）。"""
    config = read_config()
    tencent_key = (
        os.environ.get("TENCENT_MAP_KEY", "")
        or config.get("tencent_map_key", "")
        or (config.get("mapApiKeys", {}) or {}).get("tencent", "")
    )
    return render_template("login.html", tencent_map_key=tencent_key)


@app.route("/admin")
def admin_page():
    """管理首页 → 重定向到仪表板。"""
    return redirect("/admin/dashboard")


@app.route("/admin/dashboard")
def admin_dashboard():
    """仪表板页面。"""
    return render_template("dashboard.html")


@app.route("/admin/config")
def admin_config():
    """配置管理页面。"""
    return render_template("config.html")


@app.route("/admin/config/view")
def admin_config_view():
    """配置文件原始查看页面。"""
    return render_template("config-view.html")


@app.route("/admin/records")
def admin_records():
    """签到签退记录页面（records.jsonl）。"""
    return render_template("records.html")


@app.route("/admin/logs")
def admin_logs():
    """系统日志页面（sign.log）。"""
    return render_template("system-log.html")


@app.route("/admin/requests")
def admin_requests():
    """请求日志页面（requests.log.jsonl）。"""
    return render_template("requests.html")


@app.route("/admin/backups")
def admin_backups():
    """备份管理页面。"""
    return render_template("backups.html")


@app.route("/admin/security")
def admin_security():
    """账户安全页面。"""
    return render_template("security.html")


@app.route("/admin/about")
def admin_about():
    """关于页面。"""
    return render_template("about.html")


@app.route("/admin/journal")
def admin_journal():
    """周记管理页面。"""
    return render_template("journal.html")

@app.route("/admin/supplementary")
def admin_supplementary():
    """补签管理页面。"""
    return render_template("supplementary.html")

@app.route("/admin/map")
def admin_map():
    config = read_config()
    tencent_key = (
        os.environ.get("TENCENT_MAP_KEY", "")
        or config.get("tencent_map_key", "")
        or (config.get("mapApiKeys", {}) or {}).get("tencent", "")
    )
    location = config.get("location", {})
    center_lat = float(location.get("latitude", 39.916527))
    center_lng = float(location.get("longitude", 116.397128))
    # 可从配置文件读取 map_radius，若无则默认500
    map_radius = config.get("map_radius", 500)
    return render_template("map.html",
                           tencent_map_key=tencent_key,
                           center_lat=center_lat,
                           center_lng=center_lng,
                           map_radius=map_radius)

# ============================================================
# API 路由 — 认证
# ============================================================

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """
    管理员登录接口。

    请求: {"password": "xxx"}
    响应: {"success": true, "token": "jwt...", "message": "..."}
    错误: 401 + {"success": false, "message": "..."}

    安全机制:
        - 速率限制（默认 10次/分钟，超出锁定 5 分钟）
        - bcrypt 密码验证
        - JWT token 签发（8 小时有效）
    """
    data = request.get_json(silent=True) or {}

    # 速率限制检查
    client_ip = request.remote_addr or "127.0.0.1"
    allowed, err_msg = check_rate_limit(client_ip)
    if not allowed:
        return jsonify({"success": False, "message": err_msg}), 429

    # 验证密码
    password = data.get("password", "")
    stored_hash = get_admin_password_hash()

    if not password or not verify_password(password, stored_hash):
        record_login_attempt(client_ip)
        return jsonify({"success": False, "message": "密码错误"}), 401

    # 签发 token
    token = create_token("admin")

    # 记录登录信息（IP、UA）
    # IP 优先级: 前端检测的公网 IP > X-Forwarded-For > remote_addr
    client_public_ip = (data.get("public_ip") or "").strip()
    proxy_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    direct_ip = request.remote_addr or "127.0.0.1"
    login_ip = client_public_ip or proxy_ip or direct_ip
    login_ua = request.headers.get("User-Agent", "")[:200]
    _record_login_history(login_ip, login_ua)

    resp = jsonify({"success": True, "token": token, "message": "登录成功"})
    resp.set_cookie(
        "auth_token",
        token,
        httponly=True,
        samesite="Lax",
        max_age=int(os.environ.get("JWT_EXPIRATION_HOURS", "8")) * 3600,
    )
    return resp


@app.route("/api/auth/verify", methods=["GET"])
@token_required
def api_verify():
    """验证 token 是否有效。"""
    return jsonify({
        "success": True,
        "username": request.current_user,
        "message": "token 有效",
    })


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    """登出（清除 cookie）。"""
    resp = jsonify({"success": True, "message": "已登出"})
    resp.delete_cookie("auth_token")
    return resp


@app.route("/api/auth/change-password", methods=["POST"])
@token_required
def api_change_password():
    """
    修改管理员密码——持久化到 config.json 的 web_admin_password_hash 字段。

    请求: {"old_password": "...", "new_password": "..."}
    需要当前密码验证。
    """
    try:
        data = request.get_json(silent=True) or {}
        old_pw = data.get("old_password", "")
        new_pw = data.get("new_password", "")

        if not new_pw or len(new_pw) < 6:
            return jsonify({"success": False, "message": "新密码至少 6 位"}), 400

        stored_hash = get_admin_password_hash()
        if old_pw and not verify_password(old_pw, stored_hash):
            return jsonify({"success": False, "message": "当前密码错误"}), 401

        # 生成新哈希并运行时生效
        set_admin_password_plain(new_pw)

        # 持久化到 config.json
        config = read_config()
        config["web_admin_password_hash"] = get_admin_password_hash()
        if write_config(config):
            logger.info("管理员密码已更新并持久化")
            return jsonify({"success": True, "message": "密码已更新并持久化到配置文件"})
        else:
            return jsonify({"success": False, "message": "密码运行时已生效，但写入配置文件失败"}), 500
    except (OSError, ValueError) as e:
        logger.error("修改密码时发生错误: %s", e)
        return jsonify({"success": False, "message": "修改密码失败，请检查配置文件是否可写"}), 500


# ============================================================
# API 路由 — 配置管理
# ============================================================

@app.route("/api/config", methods=["GET"])
@token_required
def api_get_config():
    """获取完整配置。"""
    try:
        config = read_config()
        # 移除注释字段（以 _ 开头的键）
        clean = {k: v for k, v in config.items() if not str(k).startswith("_")}
        return jsonify({"success": True, "config": clean})
    except FileNotFoundError:
        logger.error("配置文件不存在: %s", CONFIG_PATH)
        return jsonify({"success": False, "message": "配置文件未找到"}), 500
    except json.JSONDecodeError as e:
        logger.error("配置文件格式损坏: %s", e)
        return jsonify({"success": False, "message": "配置文件格式错误"}), 500
    except OSError as e:
        logger.error("读取配置文件失败: %s", e)
        return jsonify({"success": False, "message": "无法读取配置文件"}), 500


@app.route("/api/config", methods=["PUT"])
@token_required
def api_update_config():
    """
    更新完整配置（需要密码二次确认）。

    请求: {"password": "...", "config": {...}}
    """
    data = request.get_json(silent=True) or {}

    # 二次密码确认
    password = data.get("password", "")
    stored_hash = get_admin_password_hash()
    if not password or not verify_password(password, stored_hash):
        return jsonify({"success": False, "message": "密码验证失败，配置未保存"}), 401

    new_config = data.get("config")
    if not isinstance(new_config, dict):
        return jsonify({"success": False, "message": "config 必须是对象"}), 400

    # 结构校验
    errors = validate_config_structure(new_config)
    if errors:
        return jsonify({
            "success": False,
            "message": "配置校验失败",
            "errors": errors,
        }), 400

    # 写入
    if write_config(new_config):
        return jsonify({"success": True, "message": "配置已保存"})
    else:
        return jsonify({"success": False, "message": "写入配置文件失败"}), 500


@app.route("/api/config/section/<path:key_path>", methods=["PUT"])
@token_required
def api_update_config_section(key_path: str):
    """
    更新配置的部分字段（用于表单逐个字段保存）。

    请求: {"password": "...", "value": {...}}

    key_path 示例: "location", "device", "schedule"
    """
    data = request.get_json(silent=True) or {}

    # 二次密码确认
    password = data.get("password", "")
    if not verify_password(password, get_admin_password_hash()):
        return jsonify({"success": False, "message": "密码验证失败"}), 401

    value = data.get("value")
    if value is None:
        return jsonify({"success": False, "message": "缺少 value"}), 400

    config = read_config()

    # 按路径设置值
    keys = key_path.split("/")
    target = config
    for key in keys[:-1]:
        if key not in target:
            target[key] = {}
        target = target[key]
    target[keys[-1]] = value

    if write_config(config):
        return jsonify({"success": True, "message": f"{key_path} 已更新"})
    else:
        return jsonify({"success": False, "message": "写入失败"}), 500


# ============================================================
# API 路由 — 日志查看
# ============================================================

@app.route("/api/logs", methods=["GET"])
@token_required
def api_get_logs():
    """
    获取最近的签到日志。

    查询参数:
        lines: 返回行数（默认 100）
        level: 过滤级别（INFO, WARNING, ERROR），默认全部
    """
    log_dir = _get_log_dir()
    lines = request.args.get("lines", 100, type=int)
    level = request.args.get("level", "").upper()

    # 查找日志文件
    log_files = []
    if os.path.isdir(log_dir):
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith((".log", ".txt"))],
            reverse=True,
        )

    log_content = ""
    if log_files:
        # 读取最新的日志文件
        latest_log = os.path.join(log_dir, log_files[0])
        with open(latest_log, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            # 取最后 N 行
            selected = all_lines[-lines:] if len(all_lines) > lines else all_lines
            # 按级别过滤
            if level:
                selected = [l for l in selected if f"[{level}]" in l]
            log_content = "".join(selected)

    return jsonify({
        "success": True,
        "content": log_content,
        "files": log_files[:5],  # 最近的 5 个日志文件
    })


# ============================================================
# API 路由 — 系统状态
# ============================================================

@app.route("/api/status", methods=["GET"])
@token_required
def api_status():
    """获取系统状态（配置状态、缓存状态等）。"""

    config = read_config()

    # 获取服务器 IP 和主机名
    import socket as _socket
    hostname = _socket.gethostname()
    try:
        server_ip = _socket.gethostbyname(hostname)
    except _socket.gaierror:
        server_ip = "127.0.0.1"

    # 读取 schedule 配置状态
    schedule_cfg = config.get("schedule", {})
    if isinstance(schedule_cfg, dict):
        schedule_tasks = schedule_cfg.get("tasks", [])
        schedule_config = {
            "enabled": schedule_cfg.get("enabled", True),
            "has_tasks": bool(schedule_tasks),
            "task_count": len(schedule_tasks) if isinstance(schedule_tasks, list) else 0,
            "workdays": schedule_cfg.get("workdays", [1, 2, 3, 4, 5]),
        }
    else:
        schedule_config = {"enabled": True, "has_tasks": False, "task_count": 0, "workdays": []}

    return jsonify({
        "success": True,
        "auth_mode": config.get("auth_mode", "auto"),
        "schedule_config": schedule_config,
        "config_keys": [k for k in config.keys() if not str(k).startswith("_")],
        "server": {
            "hostname": hostname,
            "ip": server_ip,
        },
    })


# ============================================================
# API 路由 — 测试签到/签退
# ============================================================

@app.route("/api/test_sign", methods=["POST"])
@token_required
def api_test_sign():
    """
    手动测试签到/签退 — 使用当前 config.json 立即执行一次，验证配置是否有效。

    请求: {"action": "sign_in" | "sign_out"}
    响应: {"success": true/false, "message": "...", "elapsed_ms": 1234, "action": "sign_in"}

    说明:
        - 直接复用 core.signer.SignInClient，共享配置加载逻辑
        - 测试操作不干扰守护进程（独立进程空间）
        - 超时时间 20 秒，防止请求挂起
        - 结果自动记录到测试日志文件
    """
    import time as _time
    import logging as _logging
    from core.apis.signer import SignInClient
    import requests as _requests

    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    # 参数校验
    if action not in ("sign_in", "sign_out"):
        return jsonify({
            "success": False,
            "message": f"无效的 action: {action}，支持 sign_in | sign_out",
        }), 400

    label = {"sign_in": "签到", "sign_out": "签退"}.get(action, action)

    # 测试日志记录器
    test_logger = _logging.getLogger("test_sign")
    test_logger.info(f"管理员 {request.current_user} 发起手动测试: {label}")

    started = _time.time()
    try:
        # 读取当前配置
        config = read_config()

        # 创建 HTTP 会话
        session = _requests.Session()

        # 实例化 SignInClient（使用与守护进程相同的 CACHE_DIR 路径）
        cache_dir = os.environ.get("CACHE_DIR", _get_project_root())
        cache_file = os.path.join(cache_dir, ".session_cache.json")
        client = SignInClient(config, session, cache_file=cache_file)

        # 执行签到/签退
        result = client.execute(action)

        elapsed_ms = int((_time.time() - started) * 1000)

        # 记录结果
        outcome = "成功" if result["success"] else f"失败: {result['message']}"
        test_logger.info(f"测试 {label} → {outcome} (耗时 {elapsed_ms}ms)")

        # 清理
        session.close()

        if result["success"]:
            invalidate_clock_detail_cache()

        return jsonify({
            "success": result["success"],
            "message": result["message"],
            "elapsed_ms": elapsed_ms,
            "action": action,
            "tested_by": request.current_user,
        })

    except (OSError, ValueError, _requests.RequestException) as e:
        elapsed_ms = int((_time.time() - started) * 1000)
        test_logger.error(f"测试 {label} 异常 (耗时 {elapsed_ms}ms): {e}")
        return jsonify({
            "success": False,
            "message": "测试执行异常，请检查网络或配置",
            "elapsed_ms": elapsed_ms,
            "action": action,
        }), 500


@app.route("/api/photo_sign", methods=["POST"])
@token_required
def api_photo_sign():
    """
    拍照签到/签退接口（带 EXIF 清除 + 文件校验 + 日志记录）。

    接收前端上传的照片，去除隐私元数据后提交到小程序。
    签到成功后自动刷新详情缓存，确保前端状态实时更新。

    请求格式: multipart/form-data
        - image: 图片文件 (jpg/png/gif/bmp, ≤10MB)
        - action: "sign_in" | "sign_out"

    返回:
        {"success": bool, "message": "描述", "elapsed_ms": 1234, "action": "sign_in"}
    """
    import tempfile
    from PIL import Image
    import time as _time
    import logging as _logging

    # 校验上传文件
    if 'image' not in request.files:
        return jsonify({"success": False, "message": "未上传图片"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"success": False, "message": "文件名为空"}), 400

    # 文件类型白名单
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'bmp', 'gif'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"success": False, "message": "不支持的图片格式，仅允许 jpg/png/gif/bmp"}), 400

    # 文件大小限制（10MB）
    MAX_SIZE = 10 * 1024 * 1024
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_SIZE:
        return jsonify({"success": False, "message": "图片大小不能超过 10MB"}), 400

    action = request.form.get("action", "sign_in")
    if action not in ("sign_in", "sign_out"):
        return jsonify({"success": False, "message": "action 必须为 sign_in 或 sign_out"}), 400

    # 日志记录器
    photo_logger = _logging.getLogger("photo_sign")
    label = {"sign_in": "拍照签到", "sign_out": "拍照签退"}.get(action, action)
    photo_logger.info(f"管理员 {request.current_user} 发起{label}")

    started = _time.time()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    try:
        # 去除 EXIF 并保存为临时文件
        img = Image.open(file.stream)
        img.save(tmp.name, "JPEG", quality=85, exif=b"")
        tmp.close()

        config = read_config()
        session = _requests.Session()
        mgr = PhotoSignInManager(config, session)
        login_args = mgr.login()
        opt = {"action": action, "image_path": tmp.name}
        result = mgr.photo_sign(login_args, opt)
        session.close()

        elapsed_ms = int((_time.time() - started) * 1000)
        photo_logger.info(f"{label}成功 (耗时 {elapsed_ms}ms)")

        # 清除签到详情缓存，确保页面刷新后可见最新状态
        if result:
            invalidate_clock_detail_cache()

        return jsonify({
            "success": True,
            "message": f"{label}提交成功",
            "elapsed_ms": elapsed_ms,
            "action": action,
            "tested_by": request.current_user,
        })

    except Exception as e:
        elapsed_ms = int((_time.time() - started) * 1000)
        photo_logger.error(f"{label}失败 (耗时 {elapsed_ms}ms): {e}")
        return jsonify({
            "success": False,
            "message": str(e),
            "elapsed_ms": elapsed_ms,
            "action": action,
        }), 500
    finally:
        # 清理临时文件
        if os.path.exists(tmp.name):
            try:
                os.remove(tmp.name)
            except OSError:
                pass

# ============================================================
# API 路由 — 小程序登录
# ============================================================

@app.route("/api/auth/wechat-login", methods=["POST"])
@token_required
def api_wechat_login():
    """
    使用前端传入的 code 触发微信登录，登录成功后自动将
    openId/unionId/encryptValue/sessionId 以及当前 code 保存到 config.json。
    """
    from core.apis.signer import SignInClient
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"success": False, "message": "缺少 code 参数"}), 400

    try:
        config = read_config()
        session = _requests.Session()
        from core.utils.cache import DEFAULT_CACHE_FILE,clear_session_cache
        cache_file = str(DEFAULT_CACHE_FILE)

        if cache_file:
            clear_session_cache(cache_file)
            logger.info("已手动清除旧会话缓存，将使用 code 重新登录")

        # 临时设置 code 用于本次登录
        config["code"] = code
        client = SignInClient(config, session, cache_file=cache_file)
        try:
            login_args = client.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        # 登录成功 → 将凭证和 code 写入配置文件
        try:
            current_config = read_config()
            current_config["openId"] = login_args.get("openId", "")
            current_config["unionId"] = login_args.get("unionId", "")
            current_config["encryptValue"] = login_args.get("encryptValue", "")
            current_config["sessionId"] = login_args.get("sessionId", "")
            current_config["code"] = code   # 保存新 code
            write_config(current_config)
        except Exception as e:
            logger.error(f"更新 config.json 失败: {e}")

        session.close()

        safe_args = {
            "openId": str(login_args.get("openId", ""))[:8] + "***",
            "unionId": str(login_args.get("unionId", ""))[:8] + "***",
            "sessionId": str(login_args.get("sessionId", ""))[:20] + "...",
        }
        return jsonify({
            "success": True,
            "message": "微信登录成功，认证信息已保存到配置文件",
            "data": safe_args
        })
    except Exception as e:
        logger.error("微信登录接口异常: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500

# ============================================================
# API 路由 — 周记/月记管理
# ============================================================

@app.route("/api/journal/submit", methods=["POST"])
@token_required
def api_journal_submit():
    """
    提交周记/月记到小程序。

    请求: {
        "title": "周记标题",
        "body": "周记正文",
        "start_date": "2026-06-15",
        "end_date": "2026-06-21",
        "type": "daily"|"weekly"|"monthly",
        "blog_open_type": 1
    }
    """
    try:
        from datetime import date as _date
        from core.apis.journal import JournalManager
        import calendar

        data = request.get_json(silent=True) or {}
        title = data.get("title", "").strip()
        body = data.get("body", "")
        start_date = data.get("start_date", "").strip().replace("-", ".")
        end_date = data.get("end_date", "").strip().replace("-", ".")
        journal_type = data.get("type", "weekly").strip()
        blog_open_type = data.get("blog_open_type", 2)

        if not title or not body:
            return jsonify({"success": False, "message": "标题和内容不能为空"}), 400
        if len(body) < 200:
            return jsonify({"success": False, "message": f"正文至少200字（当前{len(body)}字）"}), 400

        config = read_config()
        session = _requests.Session()
        mgr = JournalManager(config, session)

        # 登录
        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            msg = str(e)
            logger.error("周记登录失败: %s", msg)
            return jsonify({"success": False, "message": f"JSESSIONID 已过期或无效: {msg}"})

        # -------------- 自动获取日期范围 --------------
        if not start_date or not end_date:
            try:
                year_data = mgr.load_blog_year(login_args)
                if isinstance(year_data, list) and year_data:
                    today = _date.today()
                    year_item = year_data[0]
                    months = year_item.get("months", []) if isinstance(year_item, dict) else []

                    if months:
                        month_ids = sorted(
                            m.get("id", 0) for m in months if isinstance(m, dict)
                        )
                        # 修复空 max 异常
                        month = max(
                            (mid for mid in month_ids if mid <= today.month),
                            default=today.month
                        )

                        if journal_type == "monthly":
                            # ---- 月记：调用无参月份接口，直接按今天所在区间匹配 ----
                            month_list = mgr.load_blog_month_list(login_args)
                            if isinstance(month_list, list) and month_list:
                                today_dot = today.strftime("%Y.%m.%d")
                                target_month = None
                                for m in month_list:
                                    ms = m.get("startDate", "").replace("-", ".")
                                    me = m.get("endDate", "").replace("-", ".")
                                    if ms and me and ms <= today_dot <= me:
                                        target_month = m
                                        break
                                if target_month:
                                    start_date = target_month.get("startDate", "")
                                    end_date = target_month.get("endDate", "")
                                    logger.info(f"自动获取月记日期: {start_date} ~ {end_date}")
                            # 如果匹配失败，降级为计算当月起止日期
                            if not start_date or not end_date:
                                last_day = calendar.monthrange(today.year, month)[1]
                                start_date = f"{today.year}.{month:02d}.01"
                                end_date = f"{today.year}.{month:02d}.{last_day:02d}"
                                logger.warning("月记接口匹配失败，使用计算日期")

                        else:
                            # ---- 周记：原有逻辑不变 ----
                            week_list = mgr.load_blog_date(login_args, today.year, month)
                            if isinstance(week_list, list) and week_list:
                                today_dot = today.strftime("%Y.%m.%d")
                                target_week = None
                                for w in week_list:
                                    if not isinstance(w, dict):
                                        continue
                                    ws = w.get("startDate", "").replace("-", ".")
                                    we = w.get("endDate", "").replace("-", ".")
                                    if ws and we and ws <= today_dot <= we:
                                        target_week = w
                                        break
                                if not target_week:
                                    for w in week_list:
                                        if str(w.get("status", "")) != "1":
                                            target_week = w
                                            break
                                    if not target_week:
                                        target_week = week_list[-1]

                                start_date = target_week.get("startDate", "")
                                end_date = target_week.get("endDate", "")
                                logger.info(
                                    f"自动获取周记日期: {start_date} ~ {end_date} "
                                    f"(week={target_week.get('week', '?')})"
                                )
            except Exception as e:
                logger.warning(f"自动获取日期失败: {e}")

        if not start_date or not end_date:
            return jsonify({"success": False, "message": "无法获取日期范围，请手动填写"}), 400

        # -------------- 提交周日志 --------------
        blog_type = "0" if journal_type == "daily" else ("2" if journal_type == "monthly" else "1")
        try:
            result = mgr.submit_blog(
                login_args, title, body, start_date, end_date,
                blog_open_type, blog_type=blog_type,
            )
        except Exception as e:
            session.close()
            msg = str(e)
            logger.error("周记提交失败: %s", msg)
            return jsonify({"success": False, "message": msg})

        session.close()

        # ★ 提交成功 → 清除该类别的周记/月记列表缓存
        invalidate_journal_cache(blog_type)

        return jsonify({"success": True, "message": "提交成功", "data": result})

    except Exception as e:
        logger.error("周记请求异常: %s", e)
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/journal/library", methods=["GET"])
@token_required
def api_journal_library():
    """
    获取周记/月记素材库（来自 journals_db.json）。

    查询参数:
        type:  "all" | "weekly" | "monthly"（默认 all）
        page:  页码（默认 1，不传则返回全部）
        per_page: 每页条数（默认 20）
    """
    try:
        db_path = os.path.join(
            _get_project_root(),
            "journals", "journals_db.json",
        )
        if not os.path.exists(db_path):
            return jsonify({"success": True, "library": {"meta": {}, "weekly": [], "monthly": []}})

        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)

        filter_type = request.args.get("type", "all").strip()
        page = request.args.get("page", type=int)
        per_page = request.args.get("per_page", 20, type=int)

        result = {"meta": db.get("meta", {})}

        if filter_type == "all":
            result["daily"] = db.get("daily", [])
            result["weekly"] = db.get("weekly", [])
            result["monthly"] = db.get("monthly", [])
        elif filter_type == "daily":
            result["daily"] = db.get("daily", [])
        elif filter_type == "weekly":
            result["weekly"] = db.get("weekly", [])
        elif filter_type == "monthly":
            result["monthly"] = db.get("monthly", [])

        # 分页（仅对 single type 时生效）
        if page and filter_type in ("daily", "weekly", "monthly"):
            key = filter_type
            items = result.get(key, [])
            start = (page - 1) * per_page
            result[key] = items[start:start + per_page]
            result["total"] = len(items)
            result["page"] = page
            result["total_pages"] = max(1, (len(items) + per_page - 1) // per_page)

        response = jsonify({"success": True, "library": result})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response
    except (_requests.RequestException, OSError, ValueError, KeyError) as e:
        logger.error("API 请求失败: %s", e)
        return jsonify({"success": False, "message": "请求失败，请稍后重试"}), 500


@app.route("/api/journal/library", methods=["PUT"])
@token_required
def api_journal_update_entry():
    """
    更新素材库中指定周记/月记的内容。

    请求: {
        "id": "weekly_1",
        "title": "修改后的标题",
        "body": "修改后的正文"
    }

    自动备份原文件为 .bak。
    """
    try:
        data = request.get_json(silent=True) or {}
        entry_id = data.get("id", "").strip()
        new_title = data.get("title", "").strip()
        new_body = data.get("body", "")
        start_date = data.get("start_date", "").strip().replace("-", ".")
        end_date = data.get("end_date", "").strip().replace("-", ".")

        if not entry_id or not new_body:
            return jsonify({"success": False, "message": "缺少 id 或 body"}), 400
        if len(new_body) < 200:
            return jsonify({"success": False, "message": f"正文至少200字（当前{len(new_body)}字）"}), 400

        db_path = os.path.join(
            _get_project_root(),
            "journals", "journals_db.json",
        )
        if not os.path.exists(db_path):
            return jsonify({"success": False, "message": "素材库文件不存在"}), 404

        # 备份
        import shutil
        backup_path = db_path + ".bak"
        shutil.copy2(db_path, backup_path)

        # 读取-修改-写回
        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)

        kind = 'daily' if entry_id.startswith('daily_') else ('weekly' if entry_id.startswith('weekly_') else 'monthly')
        items = db.get(kind, [])
        found = False
        for item in items:
            if item.get("id") == entry_id:
                if new_title:
                    item["title"] = new_title
                item["body"] = new_body
                item["char_count"] = len(new_body)
                if start_date:
                    item["start_date"] = start_date
                if end_date:
                    item["end_date"] = end_date
                found = True
                break

        if not found:
            # 条目不存在则创建新条目
            new_entry = {
                "id": entry_id,
                "title": new_title or "",
                "body": new_body,
                "char_count": len(new_body),
                "start_date": start_date,
                "end_date": end_date,
            }
            # 生成默认标题
            if not new_title:
                type_names = {'daily': '日记', 'weekly': '周记', 'monthly': '月记'}
                match = _re.search(r'\d+', entry_id)
                num = match.group() if match else '?'
                new_entry['title'] = f"{type_names[kind]} 第{num}篇"

            # 按类型添加编号字段
            if kind == 'daily':
                match = _re.search(r'daily_(\d+)', entry_id)
                new_entry['day'] = int(match.group(1)) if match else 999
            elif kind == 'weekly':
                match = _re.search(r'weekly_(\d+)', entry_id)
                new_entry['week'] = int(match.group(1)) if match else 999
            else:
                match = _re.search(r'monthly_(\d+)', entry_id)
                new_entry['month'] = int(match.group(1)) if match else 999

            items.append(new_entry)
            # 按编号排序
            sort_key = 'day' if kind == 'daily' else ('week' if kind == 'weekly' else 'month')
            items.sort(key=lambda x: x.get(sort_key, 0))

        # 更新 meta 计数
        if "meta" in db:
            db["meta"]["daily_count"] = len(db.get("daily", []))
            db["meta"]["weekly_count"] = len(db.get("weekly", []))
            db["meta"]["monthly_count"] = len(db.get("monthly", []))

        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        logger.info(f"素材库条目已更新: {entry_id}")
        return jsonify({"success": True, "message": f"{entry_id} 已更新", "char_count": len(new_body)})
    except (_requests.RequestException, OSError, ValueError, KeyError) as e:
        logger.error("API 请求失败: %s", e)
        return jsonify({"success": False, "message": "请求失败，请稍后重试"}), 500


@app.route("/api/journal/library", methods=["DELETE"])
@token_required
def api_journal_delete_entry():
    """删除素材库中指定条目。"""
    try:
        data = request.get_json(silent=True) or {}
        entry_id = data.get("id", "").strip()
        if not entry_id:
            return jsonify({"success": False, "message": "缺少 id"}), 400

        db_path = os.path.join(_get_project_root(), "journals", "journals_db.json")
        if not os.path.exists(db_path):
            return jsonify({"success": False, "message": "素材库文件不存在"}), 404

        import shutil
        shutil.copy2(db_path, db_path + ".bak")

        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)

        kind = 'daily' if entry_id.startswith('daily_') else ('weekly' if entry_id.startswith('weekly_') else 'monthly')
        items = db.get(kind, [])
        new_items = [item for item in items if item.get("id") != entry_id]
        if len(new_items) == len(items):
            return jsonify({"success": False, "message": f"未找到条目: {entry_id}"}), 404

        db[kind] = new_items
        if "meta" in db:
            db["meta"]["daily_count"] = len(db.get("daily", []))
            db["meta"]["weekly_count"] = len(db.get("weekly", []))
            db["meta"]["monthly_count"] = len(db.get("monthly", []))

        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        logger.info(f"素材库条目已删除: {entry_id}")
        return jsonify({"success": True, "message": f"{entry_id} 已删除"})
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.error("删除素材条目失败: %s", e)
        return jsonify({"success": False, "message": "请求失败，请稍后重试"}), 500


@app.route("/api/journal/records", methods=["GET"])
@token_required
def api_journal_records():
    """获取周记/月记提交记录。"""
    try:
        from core.apis.journal import JournalManager
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        records = JournalManager.get_journal_records(limit, offset)
        return jsonify({"success": True, "records": records})
    except Exception as e:
        logger.error("获取提交记录失败: %s", e)
        return jsonify({"success": False, "message": "请求失败，请稍后重试"}), 500


@app.route("/api/journal/list", methods=["GET"])
@token_required
def api_journal_list():
    try:
        from core.apis.journal import JournalManager
        page = request.args.get("page", 1, type=int)
        blog_type = request.args.get("blog_type", "1").strip()
        force = request.args.get("force", "0").strip() == "1"

        if blog_type not in ("0", "1", "2"):
            return jsonify({"success": False, "message": "blog_type 必须为 0 或 1 或 2"}), 400

        # 缓存键提前定义（确保任何分支都可用）
        cache_key = f"blog_type_{blog_type}_page_{page}"

        # 检查缓存（非强制刷新时）
        if not force:
            cache = load_journal_list_cache()
            cached = cache.get(cache_key)
            if cached and cached.get("expire_at", 0) > _time.time():
                return jsonify({
                    "success": True,
                    "data": cached["data"],
                    "from_cache": True,
                    "cache_time": cached.get("cached_at", "")
                })

        config = read_config()
        session = _requests.Session()
        mgr = JournalManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {e}"}), 500

        try:
            result = mgr.blog_list(login_args, page=page, blog_type=blog_type)
            session.close()

            # 写入缓存（journal_list 不备份旧条目）
            now_ts = _time.time()
            cache = load_journal_list_cache()  # 重新加载，确保拿到最新数据
            cache[cache_key] = {
                "data": result,
                "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expire_at": now_ts + _get_cache_ttl("journal_list")
            }
            save_journal_list_cache(cache)
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

        return jsonify({"success": True, "data": result, "from_cache": False})
    except Exception as e:
        logger.error("获取周记列表异常: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500

# ============================================================
# API 路由 — 签到记录（历史查询）
# ============================================================

# 记录文件路径（与 signer.py 中的 RECORDS_FILE 保持一致）
RECORDS_FILE = os.path.join(
    _get_log_dir(),
    "records.jsonl",
)


@app.route("/api/records", methods=["GET"])
@token_required
def api_get_records():
    """
    查询签到/签退历史记录（支持筛选和分页）。

    查询参数:
        page:       页码（默认 1）
        per_page:   每页条数（默认 20）
        start_date: 开始日期 (YYYY-MM-DD)，可选
        end_date:   结束日期 (YYYY-MM-DD)，可选
        action:     类型筛选 (sign_in / sign_out)，可选
        success:    状态筛选 (true / false)，可选

    响应:
        {"records": [...], "total": 100, "page": 1, "per_page": 20, "total_pages": 5}

    说明:
        逐行读取 JSON Lines 文件，在内存中筛选、排序、分页。
        记录量通常不大（几千条），内存操作完全可行。
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    filter_action = request.args.get("action", "").strip()
    filter_success = request.args.get("success", "").strip()

    # 参数校验
    page = max(1, page)
    per_page = min(max(1, per_page), 2000)

    # 逐行读取 JSON Lines 文件
    all_records = []
    if os.path.exists(RECORDS_FILE):
        try:
            with open(RECORDS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        all_records.append(rec)
                    except json.JSONDecodeError:
                        continue  # 跳过损坏的行
        except (FileNotFoundError, OSError) as e:
            logger.error("读取记录文件失败: %s", e)
            return jsonify({"success": False, "message": "读取记录文件失败"}), 500

    # ---- 筛选 ----
    if start_date:
        all_records = [r for r in all_records if str(r.get("timestamp", ""))[:10] >= start_date]
    if end_date:
        all_records = [r for r in all_records if str(r.get("timestamp", ""))[:10] <= end_date]
    if filter_action:
        all_records = [r for r in all_records if r.get("action") == filter_action]
    if filter_success in ("true", "false"):
        target = filter_success == "true"
        all_records = [r for r in all_records if r.get("success") == target]

    # ---- 排序（按时间降序，最新在前）----
    all_records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # ---- 分页 ----
    total = len(all_records)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_records = all_records[start_idx:end_idx]

    return jsonify({
        "success": True,
        "records": page_records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@app.route("/api/records/stats", methods=["GET"])
@token_required
def api_get_records_stats():
    """
    签到记录统计摘要。

    响应:
        {"total": 100, "success_count": 95, "fail_count": 5,
         "today_count": 2, "last_sign_in": "2026-06-21 09:00:05"}
    """
    now = _time.strftime("%Y-%m-%d")
    total = 0
    success_count = 0
    fail_count = 0
    today_count = 0
    last_sign_in = None

    if os.path.exists(RECORDS_FILE):
        try:
            with open(RECORDS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        total += 1
                        if rec.get("success"):
                            success_count += 1
                        else:
                            fail_count += 1
                        ts = str(rec.get("timestamp", ""))
                        if ts.startswith(now):
                            today_count += 1
                        if rec.get("action") == "sign_in" and rec.get("success"):
                            if last_sign_in is None or ts > last_sign_in:
                                last_sign_in = ts
                    except json.JSONDecodeError:
                        continue
        except (FileNotFoundError, OSError):
            pass

    return jsonify({
        "success": True,
        "total": total,
        "success_count": success_count,
        "fail_count": fail_count,
        "today_count": today_count,
        "last_sign_in": last_sign_in,
    })


# ============================================================
# API 路由 — 系统日志（sign.log 文本日志）
# ============================================================

@app.route("/api/logs/sign", methods=["GET"])
@token_required
def api_get_sign_log():
    """
    读取 sign.log 文本日志，支持筛选、搜索、分页。
    格式: YYYY-MM-DD HH:MM:SS [LEVEL] message
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    search = request.args.get("search", "").strip().lower()
    level = request.args.get("level", "").strip().upper()

    page = max(1, page)
    per_page = min(max(1, per_page), 100)

    # 读取所有行
    all_lines = []
    if os.path.exists(SIGN_LOG_PATH):
        try:
            with open(SIGN_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                all_lines = list(f.readlines())
        except FileNotFoundError:
            all_lines = []
        except OSError as e:
            logger.error("读取签到日志失败: %s", e)
            return jsonify({"success": False, "message": "读取日志失败"}), 500

    # 解析并筛选
    import re as _re
    parsed = []
    ts_pattern = _re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.+)$")
    for line in all_lines:
        line = line.rstrip("\n\r")
        m = ts_pattern.match(line)
        if m:
            timestamp, lvl, msg = m.group(1), m.group(2), m.group(3)
            # 日期筛选
            if start_date and timestamp[:10] < start_date:
                continue
            if end_date and timestamp[:10] > end_date:
                continue
            # 级别筛选
            if level and lvl != level:
                continue
            # 关键词搜索
            if search and search not in line.lower():
                continue
            parsed.append({"timestamp": timestamp, "level": lvl, "message": msg, "raw": line})
        else:
            # 无时间戳的行（如多行消息）附加到上一条
            if search and search not in line.lower():
                continue
            parsed.append({"timestamp": "", "level": "", "message": line, "raw": line})

    # 倒序（最新在前）
    parsed.reverse()

    total = len(parsed)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    page_items = parsed[start_idx:start_idx + per_page]

    return jsonify({
        "success": True,
        "logs": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


# ============================================================
# API 路由 — HTTP 请求日志
# ============================================================

HTTP_LOG_PATH = os.path.join(
    _get_log_dir(),
    "requests.log.jsonl",
)


def _read_http_logs():
    """读取所有 HTTP 请求日志（含轮转备份文件），按时间排序。"""
    import glob as _glob
    all_logs = []
    # 收集主文件 + 所有 .1 .2 .3 .4 .5 备份
    pattern = HTTP_LOG_PATH + "*"
    for path in sorted(_glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except (FileNotFoundError, OSError):
            continue
    return all_logs


@app.route("/api/requests", methods=["GET"])
@token_required
def api_get_requests():
    """
    查询 HTTP 请求日志（结构化 JSON Lines）。支持筛选、搜索、分页。

    参数: page, per_page, action, start_date, end_date, search
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    filter_action = request.args.get("action", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    search = request.args.get("search", "").strip().lower()

    page = max(1, page)
    per_page = min(max(1, per_page), 50)

    all_logs = _read_http_logs()

    # 筛选
    if start_date:
        all_logs = [r for r in all_logs if str(r.get("timestamp", ""))[:10] >= start_date]
    if end_date:
        all_logs = [r for r in all_logs if str(r.get("timestamp", ""))[:10] <= end_date]
    if filter_action:
        all_logs = [r for r in all_logs if r.get("action") == filter_action]
    if search:
        if search.isdigit():
            # 纯数字搜索 → 仅匹配状态码
            all_logs = [r for r in all_logs if search in str(r.get("resp_status", ""))]
        else:
            all_logs = [r for r in all_logs if
                        search in str(r.get("action", "")).lower()
                        or search in str(r.get("url", "")).lower()
                        or search in str(r.get("resp_status", ""))
                        or search in str(r.get("req_body", "")).lower()
                        or search in str(r.get("resp_body", "")).lower()
                        or search in str(r.get("error", "")).lower()
                        or search in str(r.get("timestamp", "")).lower()]

    # 按时间降序（毫秒级 stamp），相同时间按 duration_ms 降序
    all_logs.sort(key=lambda r: (r.get("timestamp", ""), r.get("duration_ms", 0)), reverse=True)

    total = len(all_logs)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    page_logs = all_logs[start_idx:start_idx + per_page]

    return jsonify({
        "success": True,
        "logs": page_logs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


# ============================================================
# API 路由 — 配置备份与恢复
# ============================================================

BACKUP_DIR = os.environ.get("BACKUP_DIR",
    os.path.join(_get_project_root(), "backups"))


@app.route("/api/config/backup", methods=["POST"])
@token_required
def api_backup_config():
    """备份当前配置到 backups/config.backup.<timestamp>.json"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _time.strftime("%Y%m%d_%H%M%S")
    data = request.get_json(silent=True) or {}
    remark = data.get("remark", "").strip()

    fname = f"config.backup.{ts}.json"
    fpath = os.path.join(BACKUP_DIR, fname)

    config = read_config()
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

    if remark:
        meta = _load_backup_meta()
        meta[fname] = {"remark": remark, "created": _time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_backup_meta(meta)

    logger.info(f"配置已备份: {fname}" + (f" (备注: {remark})" if remark else ""))
    return jsonify({"success": True, "message": f"备份成功: {fname}", "filename": fname})


@app.route("/api/config/backups", methods=["GET"])
@token_required
def api_list_backups():
    """列出所有备份文件（含备注）"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    meta = _load_backup_meta()
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("config.backup.") and f.endswith(".json")],
        reverse=True,
    )
    backups = []
    for f in files:
        fpath = os.path.join(BACKUP_DIR, f)
        size = os.path.getsize(fpath)
        mtime = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(os.path.getmtime(fpath)))
        remark = meta.get(f, {}).get("remark", "")
        backups.append({"filename": f, "size": size, "mtime": mtime, "remark": remark})
    return jsonify({"success": True, "backups": backups, "total": len(backups)})


@app.route("/api/config/restore", methods=["POST"])
@token_required
def api_restore_config():
    """
    从备份恢复配置。需要密码验证。

    请求: {"password": "...", "filename": "config.backup.20260621_120000.json"}
    """
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    fname = data.get("filename", "")

    if not verify_password(password, get_admin_password_hash()):
        return jsonify({"success": False, "message": "密码验证失败"}), 401

    fpath = os.path.join(BACKUP_DIR, os.path.basename(fname))
    if not os.path.exists(fpath):
        return jsonify({"success": False, "message": f"备份文件不存在: {fname}"}), 404

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            backup_config = json.load(f)
        if write_config(backup_config):
            logger.info(f"配置已从备份恢复: {fname}")
            return jsonify({"success": True, "message": f"已从 {fname} 恢复配置"})
        return jsonify({"success": False, "message": "写入配置失败"}), 500
    except (OSError, json.JSONDecodeError) as e:
        logger.error("备份恢复失败: %s", e)
        return jsonify({"success": False, "message": "恢复失败，备份文件可能已损坏"}), 500


@app.route("/api/config/reset_default", methods=["POST"])
@token_required
def api_reset_default_config():
    """
    恢复默认配置。从 config.default.json 读取并写入 config.json。

    请求: {"password": "..."}
    """
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")

    if not verify_password(password, get_admin_password_hash()):
        return jsonify({"success": False, "message": "密码验证失败"}), 401

    default_path = os.path.join(os.path.dirname(CONFIG_PATH), "config.default.json")
    if not os.path.exists(default_path):
        return jsonify({"success": False, "message": "默认配置文件 config.default.json 不存在"}), 404

    try:
        with open(default_path, "r", encoding="utf-8") as f:
            default_config = json.load(f)
        if write_config(default_config):
            logger.info("配置已恢复为默认值")
            return jsonify({"success": True, "message": "已恢复默认配置"})
        return jsonify({"success": False, "message": "写入默认配置失败"}), 500
    except (OSError, json.JSONDecodeError) as e:
        logger.error("恢复默认配置失败: %s", e)
        return jsonify({"success": False, "message": "恢复默认配置失败"}), 500


# ============================================================
# 登录历史记录
# ============================================================

LOGIN_HISTORY_FILE = os.path.join(_get_log_dir(), "login_history.jsonl")
MAX_LOGIN_HISTORY = 50

# 腾讯 IP 定位 API
def _record_login_history(ip: str, ua: str):
    """记录一次登录到历史文件（保留最近 50 条），同时通过腾讯 IP 定位解析地理位置。"""
    # 读取腾讯地图 Key
    tencent_key = ""
    try:
        cfg = read_config()
        keys = cfg.get("mapApiKeys", {}) if isinstance(cfg, dict) else {}
        tencent_key = (keys.get("tencent") or "").strip()
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # 解析 IP 地理位置（API 超时 5s，不影响登录流程）
    ip_location = resolve_ip_location(ip, tencent_key)

    record = {
        "time": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "ip_location": ip_location,
        "user_agent": ua,
    }
    try:
        log_dir = os.path.dirname(LOGIN_HISTORY_FILE)
        os.makedirs(log_dir, exist_ok=True)
        existing = []
        if os.path.exists(LOGIN_HISTORY_FILE):
            with open(LOGIN_HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try: existing.append(json.loads(line))
                        except json.JSONDecodeError: continue
        existing.append(record)
        if len(existing) > MAX_LOGIN_HISTORY:
            existing = existing[-MAX_LOGIN_HISTORY:]
        with open(LOGIN_HISTORY_FILE, "w", encoding="utf-8") as f:
            for rec in existing:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        loc_desc = ip_location.get("city", "") or ip_location.get("province", "") or ""
        logger.info(f"📝 登录记录: IP={ip}" + (f" ({loc_desc})" if loc_desc else ""))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning(f"⚠️ 写入登录记录失败: {e}")


@app.route("/api/login-history", methods=["GET"])
@token_required
def api_login_history():
    """获取登录历史列表（最近 50 条，最新在前）。"""
    records = []
    if os.path.exists(LOGIN_HISTORY_FILE):
        try:
            with open(LOGIN_HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try: records.append(json.loads(line))
                        except json.JSONDecodeError: continue
        except (FileNotFoundError, OSError) as e:
            logger.error("读取登录历史失败: %s", e)
    records.sort(key=lambda r: r.get("time", ""), reverse=True)
    return jsonify({"success": True, "records": records, "total": len(records)})


@app.route("/api/login-history", methods=["DELETE"])
@token_required
def api_clear_login_history():
    """清空登录历史。"""
    try:
        if os.path.exists(LOGIN_HISTORY_FILE):
            os.remove(LOGIN_HISTORY_FILE)
        return jsonify({"success": True, "message": "登录历史已清空"})
    except OSError as e:
        return jsonify({"success": False, "message": f"清空失败: {e}"}), 500


# ============================================================
# 备份对比与备注元数据
# ============================================================

META_FILE = os.path.join(BACKUP_DIR, ".backup_meta.json")


def _load_backup_meta() -> dict:
    """加载备份元数据，文件损坏时自动恢复。"""
    if not os.path.exists(META_FILE):
        return {}
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("备份元数据文件损坏，将重置: %s", META_FILE)
        try:
            damaged = META_FILE + ".corrupted." + str(int(_time.time()))
            os.rename(META_FILE, damaged)
        except OSError:
            pass
        return {}
    except OSError as e:
        logger.error("无法读取备份元数据: %s", e)
        return {}


def _save_backup_meta(meta: dict):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _json_diff(a: dict, b: dict, path: str = "") -> list:
    """递归对比两个配置，返回差异列表 [{path, type, old?, new?, value?}]"""
    diffs = []
    all_keys = set(a.keys()) | set(b.keys())
    for key in sorted(all_keys):
        if str(key).startswith("_"): continue
        cur = f"{path}.{key}" if path else key
        va, vb = a.get(key), b.get(key)
        if key not in b: diffs.append({"path": cur, "type": "removed", "value": va})
        elif key not in a: diffs.append({"path": cur, "type": "added", "value": vb})
        elif isinstance(va, dict) and isinstance(vb, dict):
            diffs.extend(_json_diff(va, vb, cur))
        elif isinstance(va, list) and isinstance(vb, list):
            if json.dumps(va, sort_keys=True, ensure_ascii=False) != json.dumps(vb, sort_keys=True, ensure_ascii=False):
                diffs.append({"path": cur, "type": "changed", "old": va, "new": vb})
        elif str(va) != str(vb):
            diffs.append({"path": cur, "type": "changed", "old": va, "new": vb})
    return diffs


@app.route("/api/config/backups/<filename>", methods=["GET"])
@token_required
def api_view_backup(filename: str):
    """查看指定备份文件的 JSON 内容。"""
    fpath = os.path.join(BACKUP_DIR, os.path.basename(filename))
    if not os.path.exists(fpath):
        return jsonify({"success": False, "message": "备份文件不存在"}), 404
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = json.load(f)
        return jsonify({"success": True, "filename": filename, "content": content})
    except json.JSONDecodeError as e:
        logger.error("备份文件 JSON 解析失败: %s", e)
        return jsonify({"success": False, "message": "备份文件 JSON 格式损坏"}), 500
    except OSError as e:
        logger.error("读取备份文件失败: %s", e)
        return jsonify({"success": False, "message": "读取备份文件失败"}), 500


@app.route("/api/config/backups/<filename>", methods=["DELETE"])
@token_required
def api_delete_backup(filename: str):
    """删除指定备份文件。"""
    fpath = os.path.join(BACKUP_DIR, os.path.basename(filename))
    if not os.path.exists(fpath):
        return jsonify({"success": False, "message": "文件不存在"}), 404
    try:
        os.remove(fpath)
        logger.info(f"备份已删除: {filename}")
        return jsonify({"success": True, "message": f"已删除 {filename}"})
    except (_requests.RequestException, OSError, ValueError, KeyError) as e:
        logger.error("API 请求失败: %s", e)
        return jsonify({"success": False, "message": "请求失败，请稍后重试"}), 500


@app.route("/api/config/backups/compare", methods=["POST"])
@token_required
def api_compare_backup():
    """对比备份与当前配置的差异。请求: {"filename": "config.backup.xxx.json"}"""
    data = request.get_json(silent=True) or {}
    fname = data.get("filename", "")
    fpath = os.path.join(BACKUP_DIR, os.path.basename(fname))
    if not os.path.exists(fpath):
        return jsonify({"success": False, "message": "备份文件不存在"}), 404
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            backup_config = json.load(f)
        current_config = read_config()
        diff = _json_diff(backup_config, current_config)
        return jsonify({"success": True, "diff": diff})
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        logger.error("备份对比失败: %s", e)
        return jsonify({"success": False, "message": "对比失败，备份文件可能已损坏"}), 500


# 增强已有的 api_backup_config 和 api_list_backups —— 添加备注支持
# （前面已定义的备份API需要改造：api_backup_config 已有 remark 支持，
#   api_list_backups 需要加载备注元数据）

# ============================================================
# 健康检查
# ============================================================

@app.route("/api/health", methods=["GET"])
def api_health():
    """健康检查端点（无需认证）。"""
    return jsonify({"status": "ok", "service": "sign-in-web-admin"})


@app.route("/api/daemon/status", methods=["GET"])
def api_daemon_status():
    """守护进程状态查询 — 动态检测 + 自动重试启动。"""
    # 自动修复：条件满足但线程未启动时，尝试拉起
    if _daemon_scheduler is None:
        _try_start_daemon()

    # 1. 调度器已启动
    if _daemon_scheduler:
        return jsonify({
            "running": True,
            "reason": "定时签到守护线程运行中",
            "scheduler_enabled": _daemon_scheduler._enabled,
            "workdays": _daemon_scheduler._workdays,
        })

    # 2. DAEMON_ENABLED 未启用
    if os.environ.get("DAEMON_ENABLED", "").lower() not in ("1", "true", "yes"):
        return jsonify({
            "running": False,
            "reason": "DAEMON_ENABLED 未启用（环境变量未设置，Docker 中默认自动开启）",
        })

    # 3. DAEMON_ENABLED=true 但启动失败 → 读配置诊断原因
    try:
        c = read_config()
        s = c.get("schedule", {})
        if not s:
            reason = "配置中缺少 schedule 字段"
        elif not s.get("enabled", True):
            reason = "schedule.enabled = false（请在配置管理页面开启后刷新本页自动重试）"
        elif not s.get("tasks"):
            reason = "schedule.tasks 为空（请添加签到/签退任务后刷新本页）"
        else:
            reason = "调度器启动失败，请查看系统日志"
    except Exception:
        reason = "无法读取配置文件"

    return jsonify({"running": False, "reason": reason})


# ============================================================
# PWA 静态资源路由
# ============================================================
# Service Worker 与 manifest 必须在根路径提供，
# 否则 Service Worker 的默认 scope 会限制在 /static/，
# 无法拦截 /admin/* 等页面请求。

@app.route("/sw.js")
def pwa_service_worker():
    """提供 Service Worker 脚本（根路径，scope 覆盖全站）。"""
    return app.send_static_file("sw.js"), 200, {
        "Service-Worker-Allowed": "/",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }


@app.route("/manifest.json")
def pwa_manifest():
    """提供 PWA manifest（在根路径，浏览器可发现性更好）。"""
    return app.send_static_file("manifest.json"), 200, {
        "Content-Type": "application/manifest+json; charset=utf-8",
        "Cache-Control": "public, max-age=3600",
    }


@app.route("/offline.html")
def pwa_offline():
    """离线回退页（与 sw.js precache 中的 URL 对齐）。"""
    return app.send_static_file("offline.html"), 200, {
        "Cache-Control": "public, max-age=86400",
    }


@app.route("/api/proxy/check", methods=["POST"])
@token_required
def api_proxy_check():
    """测试代理连接是否生效。"""
    data = request.get_json(silent=True) or {}
    proxy_ip = data.get("proxy_ip", "").strip()
    proxy_port = data.get("proxy_port", "").strip()
    proxy_proto = data.get("proxy_proto", "http").strip()
    if not proxy_ip or not proxy_port:
        return jsonify({"success": False, "message": "缺少 proxy_ip 或 proxy_port"}), 400
    try:
        from core.utils.proxy_checker import proxy_checker
        from core.apis.signer import SignInClient
        config = read_config()
        p = config.setdefault("proxy", {})
        p["proxy_ip"] = proxy_ip
        p["proxy_port"] = proxy_port
        p["proxy_proto"] = proxy_proto
        p["enabled"] = True
        # 先登录获取有效会话
        session = _requests.Session()
        client = SignInClient(config, session)
        args = client.login()
        result = proxy_checker(args, config)
        session.close()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error("代理检测失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


# ============================================================
# API 路由 — 补签管理
# ============================================================

import random as _random
from datetime import datetime

REASON_COOLDOWN = 30  # 最近使用过的 3 条理由不会被选中

def _load_reason_usage() -> dict:
    """加载理由使用状态文件（位于 CACHE_DIR）"""
    usage_file = os.path.join(
        os.environ.get("CACHE_DIR", _get_project_root()),
        ".reason_usage.json"
    )
    if not os.path.exists(usage_file):
        return {}
    try:
        with open(usage_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_reason_usage(usage: dict):
    usage_file = os.path.join(
        os.environ.get("CACHE_DIR", _get_project_root()),
        ".reason_usage.json"
    )
    try:
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存理由使用状态失败: %s", e)

def _get_random_reason() -> str:
    """从所有理由中随机选一条，排除最近 REASON_COOLDOWN 条使用过的"""
    reasons_file = os.path.join(_get_project_root(), "journals", "supplementary_reasons.json")
    if not os.path.exists(reasons_file):
        return ""
    try:
        with open(reasons_file, "r", encoding="utf-8") as f:
            reasons = json.load(f)
    except Exception:
        return ""

    if not isinstance(reasons, list) or not reasons:
        return ""

    usage = _load_reason_usage()
    # 取出最近使用的 REASON_COOLDOWN 个理由ID
    recent_ids = set()
    sorted_usage = sorted(usage.items(), key=lambda x: x[1], reverse=True)
    for rid, _ in sorted_usage[:REASON_COOLDOWN]:
        recent_ids.add(rid)

    available = [r for r in reasons if str(r.get("id")) not in recent_ids]
    if not available:
        available = reasons  # 如果全部被过滤，放宽限制

    chosen = _random.choice(available)
    return chosen.get("content", "")

def _find_reason_id_by_content(content: str) -> int | None:
    """通过理由内容反向查找理由ID"""
    reasons_file = os.path.join(_get_project_root(), "journals", "supplementary_reasons.json")
    if not os.path.exists(reasons_file):
        return None
    try:
        with open(reasons_file, "r", encoding="utf-8") as f:
            reasons = json.load(f)
        for r in reasons:
            if r.get("content") == content:
                return r.get("id")
    except Exception:
        pass
    return None

def _record_reason_usage(reason_id: int):
    """记录一次理由使用（更新最后使用时间）"""
    usage = _load_reason_usage()
    usage[str(reason_id)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_reason_usage(usage)

@app.route("/api/supplementary/abnormal_dates", methods=["GET"])
@token_required
def api_supplementary_abnormal_dates():
    """
    获取可补签的异常日期列表。
    响应: {"success": true, "dates": ["2026.06.14", "2026.06.20"]}
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        dates = mgr.load_abnormal_dates(login_args)
        session.close()
        return jsonify({"success": True, "dates": dates})
    except Exception as e:
        logger.error("获取异常日期失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500
    
@app.route("/api/supplementary/detail", methods=["GET"])
@token_required
def api_supplementary_detail():
    """
    获取指定日期的补签详情（可补签的时段、类型、随机补签理由等）。

    查询参数:
        date: 日期，格式 YYYY.MM.DD（必填）
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        date_str = request.args.get("date", "").strip()
        if not date_str:
            return jsonify({"success": False, "message": "缺少 date 参数"}), 400

        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        try:
            detail = mgr.load_supplementary_data(login_args, date_str)

            # 随机补签理由（带冷却）
            random_reason = _get_random_reason()
            if isinstance(detail, dict):
                detail["random_reason"] = random_reason

            session.close()
            return jsonify({"success": True, "data": detail})
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        logger.error("获取补签详情失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/supplementary/submit", methods=["POST"])
@token_required
def api_supplementary_submit():
    """
    提交补签申请。

    请求示例:
    {
        "clock_date": "2026.06.14",
        "clock_type": "0",          // 0=签到，2=签退
        "clock_time": "09:00",      // 仅时间部分
        "clock_reason": "忘记打卡",
        "address": "北京市朝阳区",
        "location_id": "110105",
        "longitude": "116.397128",
        "latitude": "39.916527",
        "teacher_role_str": "审核角色" // [{"teacherRole":0,"seque":1}]
    }
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        data = request.get_json(silent=True) or {}

        clock_date = data.get("clock_date", "").strip()
        clock_type = str(data.get("clock_type", "")).strip()
        clock_time = data.get("clock_time", "").strip()
        clock_reason = data.get("clock_reason", "").strip()
        address = data.get("address", "").strip()
        location_id = str(data.get("location_id", "")).strip()
        longitude = str(data.get("longitude", "")).strip()
        latitude = str(data.get("latitude", "")).strip()
        teacher_role_str = data.get("teacher_role_str", '[{"teacherRole":0,"seque":1}]').strip()

        if not clock_date or not clock_type or not clock_time:
            return jsonify({"success": False, "message": "缺少必要参数"}), 400

        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        try:
            success = mgr.submit_supplementary_clock(
                login_args,
                clock_date=clock_date,
                clock_type=clock_type,
                clock_time=clock_time,
                clock_reason=clock_reason,
                address=address,
                location_id=location_id,
                longitude=longitude,
                latitude=latitude,
                teacher_role_str=teacher_role_str
            )
            # 记录理由使用状态（通过 clock_reason 匹配理由ID）
            if success:
                reason_id = _find_reason_id_by_content(clock_reason)
                if reason_id is not None:
                    _record_reason_usage(reason_id)

            session.close()
            return jsonify({"success": True, "message": "补签申请已提交"})
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        logger.error("补签提交失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


# ============================================================
# 签到详情全量查询缓存（与已有变量无冲突）
# ============================================================

@app.route("/api/student/clock_detail", methods=["GET"])
@token_required
def api_student_clock_detail():
    """
    加载签到是否通过详情（支持分页或全量获取，24h 缓存）。

    查询参数:
        startDate : 开始日期，格式 YYYY.MM.DD（必填）
        endDate   : 结束日期，格式 YYYY.MM.DD（必填，传给接口用）
        page      : 页码（默认 1），传 "all" 则获取从 startDate 到今天的所有数据
        pageSize  : 每页条数（默认 10，最大 10）
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        import random
        from datetime import datetime, date

        start_Date = request.args.get("startDate", "").strip()
        end_Date = request.args.get("endDate", "").strip()
        if not start_Date or not end_Date:
            return jsonify({"success": False, "message": "startDate 和 endDate 不能为空"}), 400

        _page = request.args.get("page", "1").strip()
        page_size = request.args.get("pageSize", "10").strip()
        fetch_all = (_page.lower() == "all")

        # 参数校验
        if not fetch_all:
            if not _page or not page_size:
                return jsonify({"success": False, "message": "page 和 pageSize 不能为空"}), 400
            try:
                page_int = int(_page)
                page_size_int = int(page_size)
            except ValueError:
                return jsonify({"success": False, "message": "page 和 pageSize 必须为整数"}), 400
        else:
            page_size_int = 10  # 全量获取时固定每页 10 条（接口限制）

        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            if fetch_all:
                # -------- 检查缓存 --------
                cache = load_clock_detail_cache()
                cache_key = f"{start_Date}_{end_Date}"
                cached = cache.get(cache_key)

                if cached and cached.get("expire_at", 0) > _time.time() and cached.get("data"):
                    logger.info(f"命中签到详情缓存: {start_Date}~{end_Date}, 记录数 {len(cached['data'])}")
                    session.close()
                    return jsonify({
                        "success": True,
                        "data": cached["data"],
                        "from_cache": True,
                        "cache_time": cached.get("cached_at", "")
                    })

                try:
                    login_args = mgr.login()
                except Exception as e:
                    session.close()
                    return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

                # -------- 无缓存，全量请求（带随机延迟）--------
                date_fmt = "%Y.%m.%d"
                start_dt = datetime.strptime(start_Date, date_fmt).date()
                today = date.today()

                if start_dt > today:
                    session.close()
                    return jsonify({"success": True, "data": [], "from_cache": False})

                days_count = (today - start_dt).days + 1
                total_pages = (days_count + page_size_int - 1) // page_size_int

                logger.info(f"全量获取签到详情: {start_Date} 至 {today.strftime(date_fmt)}，"
                            f"共 {days_count} 天，预计 {total_pages} 页")

                all_data = []
                try:
                    for page_num in range(1, total_pages + 1):
                        if page_num > 1:
                            delay = random.uniform(1.5, 3.0)
                            logger.info(f"延迟 {delay:.1f}s 后请求第 {page_num} 页...")
                            _time.sleep(delay)

                        page_data = mgr.load_student_clock_detail(
                            login_args, start_Date, end_Date,
                            str(page_num), str(page_size_int)
                        )
                        if not page_data:
                            logger.info(f"第 {page_num} 页无数据，停止获取")
                            break
                        all_data.extend(page_data)
                        logger.info(f"第 {page_num} 页获取成功，累计 {len(all_data)} 条")

                    session.close()

                    # -------- 更新缓存（clock_detail 不备份旧条目）--------
                    now_ts = _time.time()

                    cache[cache_key] = {
                        "start_date": start_Date,
                        "end_date": end_Date,
                        "data": all_data,
                        "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "expire_at": now_ts + _get_cache_ttl("clock_detail")  # 独立的 TTL
                    }
                    # 保存（内部自动清理过期、线程安全写入）
                    save_clock_detail_cache(cache)
                except Exception as e:
                    session.close()
                    return jsonify({"success": False, "message": str(e)}), 500

                return jsonify({
                    "success": True,
                    "data": all_data,
                    "from_cache": False,
                    "cache_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

            else:

                try:
                    login_args = mgr.login()
                except Exception as e:
                    session.close()
                    return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

                # 普通分页（不缓存）
                detail = mgr.load_student_clock_detail(
                    login_args, start_Date, end_Date,
                    str(page_int), str(page_size_int)
                )
                session.close()
                return jsonify({"success": True, "data": detail, "from_cache": False})

        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        logger.error("获取签到是否通过详情失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/supplementary/ranking_list", methods=["GET"])
@token_required
def api_supplementary_ranking_list():
    """
    获取奋斗排行榜（支持月份参数，带24小时缓存）。
    查询参数:
        months: 月份，格式 YYYY-MM，默认当前月份
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        from datetime import datetime

        months = request.args.get("months", "").strip()
        if not months:
            months = datetime.now().strftime("%Y-%m")

        # -------- 缓存检查 --------
        cache = load_ranking_cache()
        cached_entry = cache.get(months)
        if cached_entry and cached_entry.get("expire_at", 0) > _time.time() and cached_entry.get("data"):
            logger.info(f"命中排行榜缓存: {months}")
            return jsonify({
                "success": True,
                "data": cached_entry["data"],
                "msg": "操作成功",
                "from_cache": True,
                "cache_time": cached_entry.get("cached_at", "")
            })

        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        try:
            result = mgr.load_ranking_list(login_args, months)
            data = result.get("data", {})
            session.close()

            # -------- 写入缓存（覆盖前备份旧条目）--------
            now_ts = _time.time()

            # 关键：如果该月份已有旧数据，先备份再覆盖
            if months in cache:
                _backup_expired_entries({months: cache[months]}, "ranking")

            cache[months] = {
                "data": data,
                "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expire_at": now_ts + _get_cache_ttl("ranking")   # 独立的排行榜缓存 TTL
            }
            save_ranking_cache(cache)


            return jsonify({
                "success": True,
                "data": data,
                "msg": result.get("msg", ""),
                "from_cache": False,
                "cache_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        logger.error("获取排行榜失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500
    
@app.route("/api/student/today_clock_status", methods=["GET"])
@token_required
def api_today_clock_status():
    """
    获取当天签到状态（包含位置抖动检查和范围判断）。

    返回:
    {
        "success": true,
        "data": {
            "clockRuleType": 1,
            "clockInfo": { ... },       // 当天签到/签退详情
            "postInfo": { ... },        // 打卡地点规则
            "canSign": true,
            "canApplyClockOut": false,
            "jitteredLocation": {       // 抖动后的坐标（可选）
                "longitude": "116.397128",
                "latitude": "39.916527"
            },
            "inRange": true,            // 是否在签到范围内
            "distanceToCenter": 15.3    // 距离中心点的米数
        }
    }
    """
    try:
        from core.apis.supplementary_clock import SupplementaryClockManager
        from core.utils.location import haversine_distance
        from datetime import date as _date

        date_str = request.args.get("date", "").strip()
        if not date_str:
            date_str = _date.today().strftime("%Y.%m.%d")

        config = read_config()
        session = _requests.Session()
        mgr = SupplementaryClockManager(config, session)

        try:
            login_args = mgr.login()
        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": f"登录失败: {str(e)}"}), 500

        try:
            # 1. 获取当天签到状态原始数据
            result = mgr.load_today_clock_status(login_args)

            # 2. 应用位置抖动，获取抖动后的坐标
            jittered = mgr._apply_location_jitter()  # 返回 None 或 {"longitude": ..., "latitude": ...}
            if jittered:
                result["jitteredLocation"] = jittered
                # 3. 读取打卡中心点
                post_info = result.get("postInfo", {})
                center_lat = post_info.get("lat")
                center_lng = post_info.get("lng")
                distance_radius = post_info.get("distance")

                if center_lat is not None and center_lng is not None and distance_radius:
                    try:
                        jit_lat = float(jittered["latitude"])
                        jit_lon = float(jittered["longitude"])
                        c_lat = float(center_lat)
                        c_lon = float(center_lng)
                        dist = haversine_distance(jit_lat, jit_lon, c_lat, c_lon)
                        result["distanceToCenter"] = round(dist, 2)
                        result["inRange"] = dist <= float(distance_radius)
                    except (TypeError, ValueError):
                        logger.warning("⚠️ 距离计算失败：坐标转换异常")
            else:
                # 抖动禁用或配置无效，使用原始配置坐标
                location = config.get("location", {})
                if location:
                    result["jitteredLocation"] = {
                        "longitude": location.get("longitude", ""),
                        "latitude": location.get("latitude", "")
                    }
                    # 同样可以进行范围检查（使用原始坐标）
                    post_info = result.get("postInfo", {})
                    center_lat = post_info.get("lat")
                    center_lng = post_info.get("lng")
                    distance_radius = post_info.get("distance")
                    if center_lat is not None and center_lng is not None and distance_radius:
                        try:
                            orig_lat = float(location.get("latitude", 0))
                            orig_lon = float(location.get("longitude", 0))
                            c_lat = float(center_lat)
                            c_lon = float(center_lng)
                            dist = haversine_distance(orig_lat, orig_lon, c_lat, c_lon)
                            result["distanceToCenter"] = round(dist, 2)
                            result["inRange"] = dist <= float(distance_radius)
                        except (TypeError, ValueError):
                            pass

            session.close()
            return jsonify({"success": True, "data": result})

        except Exception as e:
            session.close()
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        logger.error("获取当天签到状态失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500



@app.route("/api/cache/stats", methods=["GET"])
@token_required
def api_cache_stats():
    """返回各缓存文件的统计信息"""
    try:
        stats = {
            "journal_list": get_journal_list_cache_stats(),
            "clock_detail": get_clock_detail_cache_stats(),
            "ranking": get_ranking_cache_stats(),
        }
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error("获取缓存统计失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/cache/details", methods=["GET"])
@token_required
def api_cache_details():
    try:
        now = _time.time()
        def get_entries(filepath, label):
            entries = []
            cache = _read_json(filepath)          # 直接使用新模块的读取函数
            for k, v in cache.items():
                size = 0
                if isinstance(v.get("data"), list):
                    size = len(v["data"])
                elif isinstance(v.get("data"), dict):
                    if "rankingList" in v["data"]:
                        size = len(v["data"]["rankingList"])
                    elif "list" in v["data"]:
                        size = len(v["data"]["list"])
                    else:
                        size = 1
                entries.append({
                    "key": k,
                    "data_size": size,
                    "cached_at": v.get("cached_at", ""),
                    "expire_at": v.get("expire_at", 0),
                    "expired": v.get("expire_at", 0) < now,
                })
            return entries

        details = {
            "journal_list": get_entries(_JOURNAL_LIST_CACHE_FILE, "journal_list"),
            "clock_detail": get_entries(_CLOCK_DETAIL_CACHE_FILE, "clock_detail"),
            "ranking": get_entries(_RANKING_CACHE_FILE, "ranking"),
        }
        return jsonify({"success": True, "details": details})
    except Exception as e:
        logger.error("获取缓存详情失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/cache/data", methods=["GET"])
@token_required
def api_cache_data():
    """
    获取缓存的实际数据（用于表格展示）。
    参数:
        type : "journal_list" | "clock_detail" | "ranking"
        limit: 返回记录数上限（默认 200）
    """
    try:
        cache_type = request.args.get("type", "").strip()
        limit = request.args.get("limit", 200, type=int)

        if cache_type not in ("journal_list", "clock_detail", "ranking"):
            return jsonify({"success": False, "message": "type 参数不正确"}), 400

        elif cache_type == "journal_list":
            cache = _read_json(_JOURNAL_LIST_CACHE_FILE)
            items = []
            for key, entry in cache.items():
                # 从键 blog_type_1_page_1 中提取 1 或 2
                blog_type = "1"
                if key.startswith("blog_type_"):
                    parts = key.split("_")
                    blog_type = parts[2] if len(parts) > 2 else "1"
                blog_type_label = "daily" if blog_type == "0" else ("weekly" if blog_type == "1" else "monthly")

                data = entry.get("data", {})
                if not isinstance(data, dict):
                    page_list = []  # data 是字符串（如 "列表为空"），无数据
                else:
                    page_list = data.get("list", [])

                for item in page_list:
                    items.append({
                        "blogTitle": item.get("blogTitle", ""),
                        "startDate": item.get("startDate", ""),
                        "endDate": item.get("endDate", ""),
                        "blogReviewed": item.get("blogReviewed", 0),
                        "commitDate": item.get("commitDate", ""),
                        "blogType": blog_type_label   # 新增
                    })
            # 简单去重
            seen = set()
            unique_items = []
            for item in items:
                key = (item["startDate"], item["endDate"], item["blogTitle"])
                if key not in seen:
                    seen.add(key)
                    unique_items.append(item)
            items = unique_items[:limit]

        elif cache_type == "clock_detail":

            cache = _read_json(_CLOCK_DETAIL_CACHE_FILE)
            items = []

            # ---- 选择 end_date 最大的缓存条目 ----

            latest_entry = None
            latest_end_date = ""
            for key, entry in cache.items():
                if "_" not in key:
                    continue
                parts = key.split("_")
                if len(parts) != 2:
                    continue
                # 跳过空数据
                if not entry.get("data"):
                    continue
                end_date = parts[1]
                if end_date > latest_end_date:
                    latest_end_date = end_date
                    latest_entry = entry

            if latest_entry:
                data_list = latest_entry.get("data", [])
                seen = set()
                for row in data_list:
                    d = row.get("clockDate", "")
                    sid = str(row.get("studentId", ""))
                    unique_key = f"{d}_{sid}"
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)

                    items.append({
                        "clockDate": d,
                        "studentId": sid,
                        "clockInStatus": row.get("clockInStatus") or "无",
                        "clockDInTime": row.get("clockDInTime", ""),
                        "clockOutStatus": row.get("clockOutStatus") or "无",
                        "clockOutTime": row.get("clockOutTime", ""),
                        "unClockNum": row.get("unClockNum", 0),
                        "supplementaryNum": row.get("supplementaryNum", 0),
                        "auditStatus": row.get("auditStatus", ""),
                        "clockStatus": row.get("clockStatus", 0),
                    })

            items = items[:limit]

        else:  # ranking
            cache = _read_json(_RANKING_CACHE_FILE)
            items = []
            for month_key, entry in cache.items():
                data = entry.get("data", {})
                rank_list = data.get("rankingList", [])
                for row in rank_list:
                    items.append({
                        "month": month_key,
                        "ranking": row.get("ranking", 0),
                        "userName": row.get("userName", ""),
                        "postName": row.get("postName", ""),
                        "avgWorks": row.get("avgWorks", 0),
                    })
            # 按月份+排名排序
            items.sort(key=lambda x: (x["month"], x["ranking"]))
            items = items[:limit]

        return jsonify({"success": True, "data": items})

    except Exception as e:
        logger.error("获取缓存数据失败: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500

# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "5000"))
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    app.logger.info(f"Web 管理后台启动: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
