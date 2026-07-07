"""
HTTP 请求安全层（统一风控上下文、签名、会话校验）。

本模块参考 ckkkf/sign-sign-in 设计，将原本散落在各 API 模块中的
_require_data / _require_success / _build_security_context / _form_post
统一收口到此处，消除重复代码。

对外暴露：
- _require_data(response, context)          → 解析 JSON，校验会话和 code，提取 data
- _require_success(response, context)        → 解析 JSON，校验会话和 code，返回完整 JSON
- _response_message(data)                    → 从响应中提取 msg 字段
- _build_security_context(data, config, args) → 构建风控签名上下文
- _form_post(url, data, config, args, ...)   → 发送带签名的 POST 请求
- _base_xcx_headers(config)                  → 构建 XCX 基础请求头
"""

import json
import logging
import os
import time

import requests

from core.config.common import XCX_N_HEADER, XCX_REFERER, XCX_VERSION
from core.utils.cache import _check_session_valid
from core.utils.params import (
    create_security_fingerprint,
    get_device_code,
    get_security_params,
    get_security_url_token,
)
from core.utils.logs import _log_http_request

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _response_message(data):
    """从响应 JSON 中提取可读的错误消息。"""
    if isinstance(data, dict):
        msg = data.get("msg", data.get("message"))
        if msg is not None and str(msg).strip():
            return str(msg)
    return str(data)


def _is_success_code(code):
    """判断响应 code 是否为成功（200）。"""
    return code == "200" or code == 200


def _assert_session(response_json):
    """校验 JSESSIONID 有效性；失效则清除缓存并抛出 RuntimeError。"""
    if not _check_session_valid(response_json):
        from core.utils.cache import clear_session_cache
        clear_session_cache()
        logging.getLogger(__name__).warning("JSESSIONID已失效，已清除缓存，请重新获取code")
        raise RuntimeError("JSESSIONID已失效，请重新获取Code")


# ------------------------------------------------------------------
# 风控 / 签名上下文
# ------------------------------------------------------------------


def _security_fingerprint(config):
    """生成或获取已有的安全指纹（内存 → .session_cache.json → 新建）。"""
    # 1. 内存缓存
    fp = config.get("securityFingerprint")
    if fp:
        return fp

    # 2. 从持久化缓存文件加载
    from core.utils.cache import DEFAULT_CACHE_FILE
    try:
        if os.path.exists(DEFAULT_CACHE_FILE):
            with open(DEFAULT_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.loads(f.read())
            fp = cached.get("securityFingerprint")
            if fp:
                config["securityFingerprint"] = fp
                return fp
    except Exception:
        pass

    # 3. 新建并持久化
    fp = create_security_fingerprint()
    config["securityFingerprint"] = fp
    _save_fingerprint_to_cache(fp)
    return fp


def _save_fingerprint_to_cache(fp: str):
    """将 securityFingerprint 写入持久化缓存文件（清除旧 token 因为 fingerprint 变了）。"""
    from core.utils.cache import DEFAULT_CACHE_FILE
    try:
        cached = {}
        if os.path.exists(DEFAULT_CACHE_FILE):
            with open(DEFAULT_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.loads(f.read())
        cached["securityFingerprint"] = fp
        cached.pop("securityToken", None)  # fingerprint 变了，旧 token 失效
        os.makedirs(os.path.dirname(DEFAULT_CACHE_FILE), exist_ok=True)
        with open(DEFAULT_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps(cached, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _base_xcx_headers(config):
    """构建小程序小程序的基础请求头。"""
    return {
        "v": XCX_VERSION,
        "xweb_xhr": "1",
        "content-type": "application/x-www-form-urlencoded",
        "referer": XCX_REFERER,
        "User-Agent": config["userAgent"],
    }


def _fetch_security_token(config, fingerprint, args=None, timeout=5, force=False):
    """
    从小程序风控接口获取安全 token（优先从缓存加载）。
    https://xcx.xybsyw.com/common/GetToken.action

    force=True 时跳过缓存，强制重新请求。
    """
    # 1. 尝试从 .session_cache.json 加载（fingerprint 必须匹配）
    if not force:
        from core.utils.cache import DEFAULT_CACHE_FILE
        try:
            if os.path.exists(DEFAULT_CACHE_FILE):
                with open(DEFAULT_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.loads(f.read())
                if cached.get("securityFingerprint") == fingerprint and cached.get("securityToken"):
                    logger.debug("从缓存加载安全 token")
                    return cached["securityToken"]
        except Exception:
            pass

    url = "https://xcx.xybsyw.com/common/GetToken.action"
    headers = _base_xcx_headers(config)
    cookies = (
        {"JSESSIONID": args["sessionId"]}
        if args and args.get("sessionId")
        else None
    )
    request_data = {"fp": fingerprint}
    logger.debug(
        "准备请求小程序风控Token: url:%s, headers:%s, data:{'fp': '***'}, cookies:%s",
        url, headers, cookies,
    )
    t0 = time.time()
    response = requests.post(
        url, headers=headers, cookies=cookies,
        data=request_data, timeout=timeout,
    )
    elapsed = int((time.time() - t0) * 1000)
    _log_http_request(
        action="获取安全 token", url=url, method="POST",
        req_headers=headers, req_params={},
        req_body=json.dumps(request_data),
        resp_status=int(response.json().get("code")) or response.status_code,
        resp_body=response.text, duration_ms=elapsed,
    )
    logger.debug("收到风控Token响应: %s %s", response, response.text)
    data = _require_data(response, "获取小程序风控Token失败")
    if not data:
        raise RuntimeError("获取小程序风控Token失败: data为空")
    token = str(data)
    _save_token_to_cache(token, fingerprint)
    return token


def _save_token_to_cache(token: str, fingerprint: str):
    """将 securityToken 写入持久化缓存文件（fingerprint 匹配时有效）。"""
    from core.utils.cache import DEFAULT_CACHE_FILE
    try:
        cached = {}
        if os.path.exists(DEFAULT_CACHE_FILE):
            with open(DEFAULT_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.loads(f.read())
        cached["securityToken"] = token
        cached["securityFingerprint"] = fingerprint
        os.makedirs(os.path.dirname(DEFAULT_CACHE_FILE), exist_ok=True)
        with open(DEFAULT_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps(cached, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _build_security_context(data, config, args=None):
    """构建签名字段 st / ts / fp 和 URL 参数 token。"""
    fingerprint = _security_fingerprint(config)
    security_token = _fetch_security_token(config, fingerprint, args=args)
    return {
        "params": get_security_params(data, security_token, fingerprint),
        "url_token": get_security_url_token(security_token),
    }


# ------------------------------------------------------------------
# HTTP 请求封装
# ------------------------------------------------------------------


def _require_data(response, context):
    """解析 JSON → 校验 session → 校验成功 → 返回 data 字段。"""
    try:
        res = response.json()
    except Exception as exc:
        raise RuntimeError(f"{context}: 响应解析失败 {exc}") from exc
    _assert_session(res)
    if (
        response.status_code != 200
        or not _is_success_code(res.get("code"))
        or "data" not in res
    ):
        raise RuntimeError(f"{context}: {_response_message(res)}")
    return res["data"]


def _require_success(response, context):
    """解析 JSON → 校验 session → 校验成功 → 返回完整 JSON。"""
    try:
        res = response.json()
    except Exception as exc:
        raise RuntimeError(f"{context}: 响应解析失败 {exc}") from exc
    _assert_session(res)
    if response.status_code != 200 or not _is_success_code(res.get("code")):
        raise RuntimeError(f"{context}: {_response_message(res)}")
    return res


def _form_post_headers(config, args, include_device_code=False):
    """构建 _form_post 使用的请求头（供日志记录复用）。"""
    headers = {
        **_base_xcx_headers(config),
        "encryptvalue": args.get("encryptValue", ""),
        "n": XCX_N_HEADER,
        "wechat": "1",
    }
    if include_device_code:
        headers["devicecode"] = get_device_code(
            openId=args.get("openId", ""), device=config["device"]
        )
    return headers


def _form_post(url, data, config, args, include_device_code=False, timeout=5, action=""):
    """发送携带签名头的 POST 请求，自动附加 st/ts/fp 参数并记录日志。
    若服务端返回 604（请求异常），自动刷新 securityToken 后重试一次。
    """

    def _do_post():
        security = _build_security_context(data, config, args=args)
        request_data = {**data, **security["params"]}
        headers = _form_post_headers(config, args, include_device_code=include_device_code)
        cookies = {"JSESSIONID": args.get("sessionId", "")}

        logger.debug(
            "准备发起小程序请求。url:%s, headers:%s, data:%s, cookies:%s",
            url, headers, request_data, cookies,
        )

        t0 = time.time()
        response = requests.post(
            url, headers=headers, cookies=cookies,
            data=request_data,
            params={"t": security["url_token"]},
            timeout=timeout,
        )
        elapsed = int((time.time() - t0) * 1000)
        logger.debug("收到响应:%s %s", response, response.text)

        res = response.json()
        _log_http_request(
            action=action or url.split("/")[-1],
            url=url, method="POST",
            req_headers=headers, req_params={"t": security["url_token"]},
            req_cookies=cookies, req_body=json.dumps(request_data),
            resp_status=int(res.get("code") or res.get("status") or 0) or response.status_code,
            resp_body=response.text, duration_ms=elapsed,
        )

        return response, res

    response, res = _do_post()

    # securityToken 过期 → 强制刷新并重试一次
    if str(res.get("code")) == "604" or str(res.get("msg")) == "请求异常，请联系客服":
        fingerprint = config.get("securityFingerprint") or _security_fingerprint(config)
        logger.warning("服务端返回 604/请求异常，强制刷新 securityToken 后重试")
        _fetch_security_token(config, fingerprint, args=args, force=True)
        response, res = _do_post()

    return response
