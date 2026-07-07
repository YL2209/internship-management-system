"""
实习数据管理系统签到核心模块

本模块基于 ckkkf/sign-sign-in (https://github.com/ckkkf/sign-sign-in) 改造，
移除了 GUI 界面与 mitmproxy 依赖，适配无服务器环境与 Docker 部署。
原项目采用 MIT 许可证。

提供 SignInClient 类，封装完整的签到/签退流程：
- 微信 OAuth 登录（code → openId → JSESSIONID）
- 实习计划查询
- 逆地理编码（高德/腾讯地图）
- 普通签到 / 普通签退
- JSESSIONID 缓存（24 小时有效，过期自动重新登录）
- GPS 位置抖动（随机偏移，防检测）

用法:
    from core.apis.signer import SignInClient
    import requests

    config = json.load(open("config.json"))
    session = requests.Session()
    client = SignInClient(config, session)
    result = client.execute("sign_in")
    # result → {'success': True/False, 'message': '...'}
"""

import json
import logging
import os
import time

import requests

from core.config.common import XCX_N_HEADER
from core.utils.cache import (
    clear_session_cache,
    load_session_cache,
    save_session_cache,
    _check_session_valid,
    DEFAULT_CACHE_FILE
)
from core.utils.location import (
    apply_location_jitter,
    regeo,
)
from core.utils.logs import log_record
from core.utils.params import (
    get_device_code,
)
from core.utils.requests import (
    _base_xcx_headers,
    _build_security_context,
    _form_post,
    _require_data,
)

from core.utils.logs import _log_http_request  # noqa: E402

logger = logging.getLogger(__name__)


# ============================================================
# 常量定义
# ============================================================

# 签到动作 → API clockStatus 值映射
ACTION_MAP = {
    "sign_in":  "普通签到",
    "sign_out": "普通签退",
}

CLOCK_STATUS_MAP = {
    "普通签到": 2,
    "普通签退": 1,
}

ACTION_LABEL = {
    "sign_in":  "签到",
    "sign_out": "签退",
}


# ============================================================
# SignInClient —— 签到客户端（公开 API）
# ============================================================

class SignInClient:
    """
    实习数据管理系统签到客户端。

    封装完整的签到/签退流程，支持:
    - 普通签到 / 普通签退
    - JSESSIONID 本地缓存（24 小时有效）
    - GPS 位置抖动（随机偏移，避免被识别为脚本）

    用法:
        import requests, json
        from core.apis.signer import SignInClient

        config = json.load(open("config.json"))
        session = requests.Session()
        client = SignInClient(config, session)

        result = client.execute("sign_in")
        print(result)  # {'success': True, 'message': '签到成功！'}
    """

    def __init__(self, config: dict, session: requests.Session,
                 cache_file: str = None):
        """初始化签到客户端。"""
        self.config = config
        self.session = session

        self.cache_file = cache_file or str(DEFAULT_CACHE_FILE)

        self.auth_mode = str(self.config.get("auth_mode", "auto")).strip().lower()
        if self.auth_mode not in ("auto", "manual"):
            logger.warning("未知的 auth_mode: %s，回退为 auto", self.auth_mode)
            self.auth_mode = "auto"


        err = self._validate_config()
        if err:
            logger.warning("配置校验警告: %s", err)

        logger.info("SignInClient 初始化完成 (auth_mode=%s)", self.auth_mode)


    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def execute(self, action: str) -> dict:
        """
        执行签到或签退动作。

        参数:
            action: "sign_in"（上班打卡）或 "sign_out"（下班打卡）

        返回:
            {"success": bool, "message": str}
        """
        if action not in ACTION_MAP:
            supported = ", ".join(ACTION_MAP.keys())
            return {"success": False, "message": f"不支持的操作: {action}，支持: {supported}"}

        sign_action = ACTION_MAP[action]
        label = ACTION_LABEL.get(action, action)
        started_at = time.time()

        try:
            logger.info("开始执行: %s (auth_mode=%s)", label, self.auth_mode)

            # 1. 登录
            t0 = time.time()
            login_args = self._login()
            logger.info("登录完成 (%.1fs)", time.time() - t0)

            # 2. 获取实习计划
            t0 = time.time()
            plan_data = self._get_plan(login_args)
            if not plan_data or not plan_data[0].get("dateList"):
                raise RuntimeError("实习计划为空，可能尚未分配实习任务")
            trainee_id = str(plan_data[0]["dateList"][0]["traineeId"])
            logger.info("获取实习计划成功，traineeId=%s (%.1fs)", trainee_id, time.time() - t0)

            # 3. 位置抖动
            t0 = time.time()
            jittered = self._apply_location_jitter()
            if not jittered or "longitude" not in jittered or "latitude" not in jittered:
                raise RuntimeError("位置抖动失败，无法获取有效坐标，已中断签到以保护风控")
            logger.info(
                "位置抖动成功，lon=%s，lat=%s (%.1fs)",
                jittered["longitude"], jittered["latitude"], time.time() - t0,
            )

            # 4. 逆地理编码
            geo = self._regeo(lon=jittered["longitude"], lat=jittered["latitude"])
            logger.info("逆地理编码: %s (%.1fs)", geo.get("formatted_address", "未知"), time.time() - t0)

            # 5. 签到 / 签退
            result_msg = self._simple_sign(login_args, geo, trainee_id, sign_action, jittered)

            elapsed_total = time.time() - started_at
            logger.info("%s (总耗时 %.1fs)", result_msg, elapsed_total)

            _log_record = log_record(
                config=self.config,
                action=action,
                success=True,
                message=result_msg,
                geo=geo,
                elapsed=elapsed_total,
                trainee_id=str(trainee_id),
                lat_lng=jittered,
                auth_mode=self.auth_mode,
            )
            return {"success": True, "message": result_msg}

        except Exception as e:
            elapsed_total = time.time() - started_at
            logger.error("%s失败 (耗时 %.1fs): %s", label, elapsed_total, e)
            log_record(
                config=self.config,
                action=action,
                success=False,
                message=str(e),
                elapsed=elapsed_total,
                auth_mode=self.auth_mode,
            )
            return {"success": False, "message": str(e)}


    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------

    def login(self) -> dict:
        """公开登录接口，供 JournalManager 等模块复用。返回 {openId, unionId, encryptValue, sessionId}。"""
        return self._login()

    def _login(self) -> dict:
        """登录流程：auto 模式查缓存→code 登录，manual 模式直接用配置。"""
        if self.auth_mode == "manual":
            manual_params = {
                "openId": self.config.get("openId", "").strip(),
                "unionId": self.config.get("unionId", "").strip(),
                "encryptValue": self.config.get("encryptValue", "").strip(),
                "sessionId": self.config.get("sessionId", "").strip(),
            }
            missing = [k for k, v in manual_params.items() if not v]
            if missing:
                raise RuntimeError(
                    f"手动模式缺少必需参数: {', '.join(missing)}。"
                    f"请将这些值填入 config.json。"
                )
            logger.info("手动模式：使用配置中的固定参数（跳过登录，不写缓存）")
            logger.debug(
                "openId=%s*** unionId=%s*** sessionId=%s...",
                manual_params["openId"][:8],
                manual_params["unionId"][:8],
                manual_params["sessionId"][:20],
            )
            return manual_params

        # 自动模式
        expire_seconds = int(self.config.get("sessionExpireSeconds", 24 * 3600))
        cached = load_session_cache(cache_file=self.cache_file, expire_seconds=expire_seconds)
        if cached:
            logger.info("使用缓存的 JSESSIONID")
            return cached

        code = self.config.get("code", "")
        if not code:
            raise RuntimeError("缺少 'code'，请在 config.json 中填入微信 OAuth code。")

        try:
            open_id_data = self._get_open_id(code)
        except Exception:
            self._clear_session_cache()
            raise

        login_data = self._wx_login(open_id_data)
        plan_default_data = self._get_plan_default(login_data)

        if not plan_default_data or not plan_default_data.get("clockVo"):
            raise RuntimeError("默认实习计划为空，可能尚未分配实习任务")

        result = {
            "openId": open_id_data["openId"],
            "unionId": open_id_data["unionId"],
            "encryptValue": login_data["encryptValue"],
            "sessionId": login_data["sessionId"],
        }

        save_session_cache(
            args=result,
            plan_data=plan_default_data,
            cache_file=self.cache_file,
            expire_seconds=expire_seconds,
        )
        logger.info("登录成功，已缓存 JSESSIONID（%dh 有效）", expire_seconds / 3600)
        return result


    # ------------------------------------------------------------------
    # OAuth / 登录子步骤
    # ------------------------------------------------------------------

    def _get_open_id(self, code: str) -> dict:
        """
        用微信 OAuth code 换取 openId 和 unionId。

        严格对照 sign-sign-in-master get_open_id()：
        - 使用 _base_xcx_headers + devicecode 头
        - POST body 携带 st/ts/fp 安全参数，URL 附加 ?t={DJB2 token}
        - 固定 timeout=5，整体 try/except 包装
        """
        url = "https://xcx.xybsyw.com/common/getOpenId.action"
        data = {"code": code}

        try:
            security = _build_security_context(data, self.config)
            headers = {
                **_base_xcx_headers(self.config),
                "devicecode": get_device_code("", self.config.get("device", {})),
                "n": XCX_N_HEADER,
                "wechat": "1",
            }
            request_data = {**data, **security["params"]}

            t0 = time.time()
            resp = requests.post(
                url=url,
                headers=headers,
                data=request_data,
                params={"t": security["url_token"]},
                allow_redirects=False,
                timeout=5,
            )
            elapsed = int((time.time() - t0) * 1000)
            res = resp.json()
            _log_http_request(
                action="code 换取 openId 和 unionId", url=url, method="POST",
                req_headers=headers, req_params={"t": security["url_token"]},
                req_body=json.dumps(request_data),
                resp_status=int(res.get("code")) or resp.status_code,
                resp_body=resp.text, duration_ms=elapsed,
            )

            if res.get("code") == "202":
                raise RuntimeError(f"code已失效，请重启小程序。接口响应: {res}")
            return _require_data(resp, "获取OpenID失败")
        except Exception as e:
            raise RuntimeError(f"获取OpenID失败: {e}")


    def _wx_login(self, open_id_data: dict) -> dict:
        """
        小程序登录 — 用 openId/unionId 换取 encryptValue 和 sessionId。

        严格对照 sign-sign-in-master wx_login()：
        使用 _form_post（范式 A），自动注入 st/ts/fp + ?t=DJB2。
        """
        data = {
            "openId": open_id_data["openId"],
            "unionId": open_id_data["unionId"],
        }
        url = "https://xcx.xybsyw.com/login/login!wx.action"

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=open_id_data,
            include_device_code=True, timeout=timeout, action="小程序登录",
        )
        return _require_data(resp, "登录失败")


    # ------------------------------------------------------------------
    # 实习计划
    # ------------------------------------------------------------------

    def _get_plan(self, args: dict) -> list:
        """
        获取实习计划。

        严格对照 sign-sign-in-master get_plan()：
        使用 _form_post（范式 A），自动注入 st/ts/fp + ?t=DJB2。
        """
        cached = load_session_cache(
            cache_file=self.cache_file,
            expire_seconds=int(self.config.get("sessionExpireSeconds", 86400)),
        )
        if cached and cached.get("traineeId"):
            logger.info("使用缓存中的 traineeId，跳过 GetPlan 请求")
            return [{"dateList": [{"traineeId": str(cached["traineeId"])}]}]

        url = "https://xcx.xybsyw.com/student/clock/GetPlan.action"
        data = {}

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=False, timeout=timeout, action="GetPlan",
        )
        logger.debug("响应: %s %s", resp.status_code, resp.text[:500])
        res = resp.json()

        # 校验 session（Reference get_plan 在 _form_post 后调用 _assert_session）
        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID已失效，请重新获取Code")

        if "data" in res and res["data"]:
            return res["data"]
        raise RuntimeError(f"获取计划失败: {res.get('msg', 'Unknown error')}")


    def _get_plan_default(self, args: dict) -> dict:
        """
        获取实习计划默认值（含 planId）。

        严格对照 sign-sign-in-master get_default_plan()：
        使用 _form_post（范式 A）。
        """
        cached = load_session_cache(
            cache_file=self.cache_file,
            expire_seconds=int(self.config.get("sessionExpireSeconds", 86400)),
        )
        if cached and cached.get("traineeId") and cached.get("planId"):
            logger.info("使用缓存中的 traineeId、planId，跳过 GetPlan!getDefault 请求")
            return {
                "clockVo": {
                    "traineeId": str(cached["traineeId"]),
                    "planId": str(cached["planId"]),
                }
            }

        url = "https://xcx.xybsyw.com/student/clock/GetPlan!getDefault.action"
        data = {}

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=False, timeout=timeout, action="获取默认实习计划",
        )
        return _require_data(resp, "获取默认实习计划失败") or {}


    # ------------------------------------------------------------------
    # 普通签到 / 签退
    # ------------------------------------------------------------------

    def _simple_sign(self, args: dict, geo: dict, trainee_id: str,
                     action: str, lat_lng: dict) -> str:
        """
        普通签到 / 普通签退。

        严格对照 sign-sign-in-master simple_sign_in_or_out()：
        使用 _form_post（范式 A），st/ts/fp 注入 POST body，?t=DJB2 在 URL。
        """
        url = "https://xcx.xybsyw.com/student/clock/Post.action"
        device = self.config.get("device", {})
        clock_status = CLOCK_STATUS_MAP.get(action, 2)

        data = {
            "punchInStatus": "0",
            "clockStatus": str(clock_status),
            "traineeId": str(trainee_id),
            "adcode": geo["addressComponent"]["adcode"],
            "model": device.get("model", ""),
            "brand": device.get("brand", ""),
            "platform": device.get("platform", ""),
            "system": device.get("system", ""),
            "openId": args["openId"],
            "unionId": args["unionId"],
            "lng": lat_lng.get("longitude", ""),
            "lat": lat_lng.get("latitude", ""),
            "address": geo["formatted_address"],
            "deviceName": device.get("model", ""),
        }

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=True, timeout=timeout, action=action,
        )
        logger.debug("响应: %s %s", resp.status_code, resp.text)
        res = resp.json()

        # 校验 session（Reference 在 _form_post 后立即调用 _assert_session）
        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID已失效，请重新获取Code")

        msg = res.get("msg", "")
        code = res.get("code", "")

        if code == "200":
            if msg == "success":
                return f"✅ {action}成功！"
            elif msg == "已经签到":
                return f"✅ 已经{action}过了。"
            return f"✅ {msg}"
        elif code == "403":
            logger.warning("%s", msg)
            return f"⚠️ {msg}"
        elif code == "202":
            raise RuntimeError(f"配置错误，请检查device和userAgent参数 (Code 202): {msg}")
        else:
            raise RuntimeError(f"操作失败: {msg}")


    # ------------------------------------------------------------------
    # 逆地理编码
    # ------------------------------------------------------------------

    def _regeo(self, lon=None, lat=None) -> dict:
        """逆地理编码 → 委托给 core.utils.location.regeo。"""
        return regeo(self.config, lon, lat, session=self.session)


    # ------------------------------------------------------------------
    # 位置抖动
    # ------------------------------------------------------------------

    def _apply_location_jitter(self) -> dict | None:
        """位置抖动 → 委托给 core.utils.location.apply_location_jitter。"""
        return apply_location_jitter(self.config)


    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def _check_session_valid(self, response_json: dict) -> bool:
        """检查响应 JSON 是否表示会话仍有效（封装 _check_session_valid）。"""
        return _check_session_valid(response_json)

    def _load_session_cache(self) -> dict | None:
        """加载本地 JSESSIONID 缓存。"""
        expire_seconds = int(self.config.get("sessionExpireSeconds", 86400))
        return load_session_cache(cache_file=self.cache_file, expire_seconds=expire_seconds)

    def _save_session_cache(self, args: dict, plan_data: dict):
        """保存 JSESSIONID 缓存到本地。"""
        expire_seconds = int(self.config.get("sessionExpireSeconds", 86400))
        save_session_cache(
            args=args, plan_data=plan_data,
            cache_file=self.cache_file, expire_seconds=expire_seconds,
        )

    def _clear_session_cache(self):
        """清除本地 JSESSIONID 缓存。"""
        clear_session_cache(cache_file=self.cache_file)

    def _handle_session_invalid(self, context: str = ""):
        """处理失效会话：清除缓存 + 记录警告。"""
        if self.auth_mode == "manual":
            raise RuntimeError(
                "JSESSIONID 已失效，请重新获取新的 sessionId 并填入配置。\n"
                "   手动模式下 sessionId 有效期约 1 小时，过期后需更新 config.json 中的 sessionId 字段。"
            )
        self._clear_session_cache()
        logger.warning("[%s] JSESSIONID 已失效，已清除缓存，请重新获取 code", context)


    # ------------------------------------------------------------------
    # 配置校验
    # ------------------------------------------------------------------

    def _validate_config(self, config: dict = None) -> str | None:
        """校验配置字典的完整性和合法性。通过返回 None。"""
        cfg = config if config is not None else getattr(self, "config", {})

        # 认证模式
        auth_mode = str(cfg.get("auth_mode", "auto")).strip().lower()
        if auth_mode == "manual":
            manual_keys = ["unionId", "encryptValue", "openId", "sessionId"]
            missing = [k for k in manual_keys if not str(cfg.get(k, "")).strip()]
            if missing:
                return f"手动模式 (auth_mode=manual) 缺少参数: {', '.join(missing)}"
        else:
            code = str(cfg.get("code", "")).strip()
            cache_exists = os.path.exists(self.cache_file)
            if not code and not cache_exists:
                return "自动模式 (auth_mode=auto) 缺少 code 且无缓存。请填入 code 或改为手动模式。"

        # location
        loc = cfg.get("location")
        if not isinstance(loc, dict):
            return "缺少 location 配置（经纬度）"
        try:
            lng = float(loc.get("longitude"))
            lat = float(loc.get("latitude"))
        except (TypeError, ValueError):
            return "经纬度格式错误：必须为数字"
        if not (-180 <= lng <= 180):
            return f"经度 {lng} 超出范围 [-180, 180]"
        if not (-90 <= lat <= 90):
            return f"纬度 {lat} 超出范围 [-90, 90]"

        # mapProvider
        provider = str(cfg.get("mapProvider", "amap")).strip().lower()
        if provider not in ("amap", "tencent", "qq", "qqmap"):
            return f"mapProvider 无效: {provider}，支持 amap 或 tencent"

        # device
        dev = cfg.get("device")
        if not isinstance(dev, dict):
            return "缺少 device 配置"
        for key in ("brand", "model", "system", "platform"):
            if not str(dev.get(key, "")).strip():
                return f"device.{key} 不能为空"

        # userAgent
        ua = cfg.get("userAgent", "")
        if not ua or not str(ua).strip():
            return "userAgent 不能为空"

        # locationJitterMeters
        jitter = cfg.get("locationJitterMeters", 100)
        try:
            jitter = float(jitter)
        except (TypeError, ValueError):
            return "locationJitterMeters 格式错误：必须为数字"
        if not (0 <= jitter <= 500):
            return f"locationJitterMeters {jitter} 超出范围 [0, 500]"

        return None


