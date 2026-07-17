from core.utils.requests import _form_post, _require_data
from core.utils.location import resolve_ip_location

import logging
logger = logging.getLogger(__name__)


def proxy_checker(args: dict, config: dict) -> dict:
    """测试代理连接是否生效，并返回出口 IP 的地理位置。"""
    url = 'https://xcx.xybsyw.com/behavior/Duration!getIp.action'
    p = config.get("proxy", {})
    proxy_ip = p.get("proxy_ip", "")
    proxy_port = p.get("proxy_port", "")

    keys = config.get("mapApiKeys", {})
    tencent_key = (keys.get("tencent") or "").strip()

    try:
        timeout = int(config.get("requestTimeout", 10))
        resp = _form_post(url, data={}, config=config, args=args,
                          include_device_code=False, timeout=timeout,
                          action="获取代理 IP 状态")
        data = _require_data(resp, "获取代理 IP 状态失败")
        if not data:
            return {"proxy_status": False}

        real_ip = data.get("ip", "")
        if real_ip == proxy_ip:
            ip_location = resolve_ip_location(proxy_ip, tencent_key, config=config)
            return {
                "proxy_status": True,
                "proxy_ip": f"{proxy_ip}:{proxy_port}",
                **ip_location,
            }
        return {"proxy_status": False}

    except Exception as e:
        logger.error("代理检测失败: %s", e)
        return {"proxy_status": False, "error": str(e)}
