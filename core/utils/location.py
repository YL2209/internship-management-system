"""
位置抖动、逆地理编码模块 —— 独立函数，不依赖类实例。

功能:
- 每日基准点漂移：基于锚点坐标，生成随机偏移
- 高斯抖动：以二维高斯分布（瑞利距离）生成签到最终坐标
- Haversine 距离计算
- 逆地理编码：高德/腾讯地图，经纬度 → 地址
- 完整位置抖动流程：含围栏重试

用法:
    from core.utils.location import (
        get_daily_base, jitter_location_gaussian, haversine_distance,
        regeo, apply_location_jitter,
    )
"""

import json
import logging
import time

import requests

from core.config.common import AMAP_WEB_KEY, TENCENT_MAP_KEY, XCX_REFERER
from core.utils.params import _normalize_address_text

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 纯数学工具
# ------------------------------------------------------------------

def get_daily_base(anchor_lat: float, anchor_lon: float,
                   drift_range: tuple = (5, 100)) -> tuple:
    """每次调用均在锚点周围随机生成一个全新的基准点（无缓存）。"""
    import math
    import random
    bearing = random.uniform(0, 2 * math.pi)
    shift = random.uniform(*drift_range)
    delta_lat = shift * math.cos(bearing) / 111320.0
    delta_lon = shift * math.sin(bearing) / (111320.0 * math.cos(math.radians(anchor_lat)))
    return anchor_lat + delta_lat, anchor_lon + delta_lon


def jitter_location_gaussian(lat: float, lon: float,
                             sigma_meters: float, max_radius: float = 500.0) -> tuple:
    """二维高斯抖动：模拟真实 GPS 漂移（距离服从瑞利分布）。"""
    import math
    import random
    angle = random.uniform(0, 2 * math.pi)
    u = random.random()
    distance = sigma_meters * math.sqrt(-2 * math.log(u))
    distance = min(distance, max_radius)
    earth_radius = 6378137.0
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    ang_dist = distance / earth_radius
    lat2 = math.asin(
        math.sin(lat_rad) * math.cos(ang_dist)
        + math.cos(lat_rad) * math.sin(ang_dist) * math.cos(angle)
    )
    lon2 = lon_rad + math.atan2(
        math.sin(angle) * math.sin(ang_dist) * math.cos(lat_rad),
        math.cos(ang_dist) - math.sin(lat_rad) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两点间的球面距离（米），使用 Haversine 公式。"""
    import math
    _EARTH_RADIUS = 6378137.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lat = lat2_rad - lat1_rad
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lon / 2) ** 2)
    return _EARTH_RADIUS * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ------------------------------------------------------------------
# 逆地理编码（经纬度 → 地址）
# ------------------------------------------------------------------

def regeo(config: dict, lon=None, lat=None, session=None) -> dict:
    """
    逆地理编码：将经纬度转换为可读地址和行政区划代码（adcode）。

    参数:
        config:  配置字典（需含 mapProvider、location、mapApiKeys、userAgent）
        lon:     经度
        lat:     纬度
        session: requests.Session 实例（可选，不传则使用裸 requests）

    返回:
        {"formatted_address": str, "addressComponent": {"adcode": str, ...}}
    """
    provider = str(config.get("mapProvider", "amap")).strip().lower()
    if provider in ("tencent", "qq", "qqmap"):
        return regeo_tencent(config, lon, lat, session=session)
    return regeo_amap(config, lon, lat, session=session)


def regeo_amap(config: dict, lon=None, lat=None, session=None) -> dict:
    """高德地图逆地理编码。"""
    use_lon = lon
    use_lat = lat
    map_keys = config.get("mapApiKeys", {}) or {}
    amap_key = (map_keys.get("amap") or "").strip() or AMAP_WEB_KEY

    url = "https://restapi.amap.com/v3/geocode/regeo"
    headers = {
        "xweb_xhr": "1",
        "Content-Type": "application/json",
        "Referer": XCX_REFERER,
        "User-Agent": config.get("userAgent", ""),
    }
    params = {
        "s": "rsx",
        "platform": "WXJS",
        "logversion": "2.0",
        "extensions": "all",
        "sdkversion": "1.2.0",
        "key": amap_key,
        "appname": amap_key,
        "location": f"{use_lon},{use_lat}",
    }

    t0 = time.time()
    timeout = int(config.get("requestTimeout", 10))
    if session:
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    elapsed = int((time.time() - t0) * 1000)

    from core.utils.logs import _log_http_request
    res = resp.json()
    _log_http_request(
        action="高德地图逆地理编码", url=url, method="GET",
        req_headers=headers, req_params=params,
        req_body=json.dumps(params),
        resp_status=int(res.get("infocode")) or resp.status_code,
        resp_body=resp.text, duration_ms=elapsed,
    )

    if "regeocode" in res:
        regeocode = dict(res["regeocode"] or {})
        formatted = _normalize_address_text(regeocode.get("formatted_address"))
        if not formatted:
            formatted = f"{use_lon},{use_lat}"
        regeocode["formatted_address"] = formatted
        logger.info("解析位置: %s", formatted)
        return regeocode
    raise RuntimeError(f"高德位置解析失败: {res}")


def regeo_tencent(config: dict, lon=None, lat=None, session=None) -> dict:
    """腾讯地图逆地理编码（输出统一为高德格式）。"""
    use_lon = lon
    use_lat = lat
    map_keys = config.get("mapApiKeys", {}) or {}
    tenc_key = (map_keys.get("tencent") or "").strip() or TENCENT_MAP_KEY

    url = "https://apis.map.qq.com/ws/geocoder/v1/"
    headers = {
        "xweb_xhr": "1",
        "Referer": XCX_REFERER,
        "User-Agent": config.get("userAgent", ""),
    }
    params = {
        "location": f"{use_lat},{use_lon}",
        "key": tenc_key,
        "get_poi": "1",
    }

    t0 = time.time()
    timeout = int(config.get("requestTimeout", 10))
    if session:
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    elapsed = int((time.time() - t0) * 1000)

    from core.utils.logs import _log_http_request
    res = resp.json()
    _log_http_request(
        action="腾讯地图逆地理编码", url=url, method="GET",
        req_headers=headers, req_params=params,
        req_body=json.dumps(params),
        resp_status=int(res.get("status")) or resp.status_code,
        resp_body=resp.text, duration_ms=elapsed,
    )

    if resp.status_code == 200 and res.get("status") == 0 and res.get("result"):
        address_component = (res["result"].get("address_component") or {})
        ad_info = (res["result"].get("ad_info") or {})
        formatted_addresses = (res["result"].get("formatted_addresses") or {})
        formatted_address = _normalize_address_text(
            formatted_addresses.get("recommend")
            or formatted_addresses.get("rough")
            or res["result"].get("address")
            or formatted_addresses.get("standard_address")
        )
        regeocode = {
            "formatted_address": formatted_address,
            "addressComponent": {
                "province": address_component.get("province", ""),
                "city": address_component.get("city") or address_component.get("province", ""),
                "district": address_component.get("district", ""),
                "street": address_component.get("street", ""),
                "streetNumber": address_component.get("street_number", ""),
                "adcode": ad_info.get("adcode", ""),
            },
        }
        if not regeocode["formatted_address"]:
            regeocode["formatted_address"] = f"{use_lat},{use_lon}"
        logger.info("解析位置: %s", regeocode["formatted_address"])
        return regeocode
    raise RuntimeError(f"腾讯位置解析失败: {res}")


# ------------------------------------------------------------------
# 完整位置抖动流程（含围栏重试）
# ------------------------------------------------------------------

def apply_location_jitter(config: dict) -> dict:
    """
    计算抖动后的经纬度，最终距离不超过 map_radius（重试机制）。

    参数:
        config: 配置字典（需含 location、locationJitterMeters、dailyDriftMinMeters 等）

    返回:
        {"longitude": "xxx.xxxxxx", "latitude": "xxx.xxxxxx"}
    """
    location = config.get("location")
    if not isinstance(location, dict):
        raise RuntimeError("经纬度配置缺失，无法进行位置抖动")

    try:
        anchor_lon = float(location.get("longitude"))
        anchor_lat = float(location.get("latitude"))
    except (TypeError, ValueError):
        raise RuntimeError("经纬度格式无效，无法进行位置抖动")

    radius = float(config.get("locationJitterMeters", 100))
    radius = max(0.0, min(radius, 500.0))
    if radius <= 0:
        raise RuntimeError("位置抖动已禁用，但风控要求必须启用抖动")

    drift_min = float(config.get("dailyDriftMinMeters", 10))
    drift_max = float(config.get("dailyDriftMaxMeters", 100))
    drift_min = max(1.0, min(drift_min, 400.0))
    drift_max = max(drift_min, min(drift_max, 400.0))

    base_lat, base_lon = get_daily_base(anchor_lat, anchor_lon,
                                        drift_range=(drift_min, drift_max))
    sigma = radius / 3.44
    map_radius = float(config.get("map_radius", 500))
    max_retries = 5
    new_lat = new_lon = 0.0
    final_dist = 0.0

    for attempt in range(max_retries):
        new_lat, new_lon = jitter_location_gaussian(base_lat, base_lon, sigma,
                                                    max_radius=radius)
        final_dist = haversine_distance(anchor_lat, anchor_lon, new_lat, new_lon)
        if final_dist <= map_radius:
            break
        if attempt < max_retries - 1:
            logger.debug(
                "抖动超出围栏 %sm (本次 %.1fm)，重试 (%d/%d)",
                map_radius, final_dist, attempt + 1, max_retries,
            )
    else:
        if final_dist > 0:
            scale = map_radius / final_dist
            new_lat = anchor_lat + (new_lat - anchor_lat) * scale
            new_lon = anchor_lon + (new_lon - anchor_lon) * scale
        logger.warning(
            "抖动重试%d次仍超出围栏，已强制锁定在边界 (原 %.1fm)",
            max_retries, final_dist,
        )

    jittered = {
        "longitude": f"{new_lon:.6f}",
        "latitude": f"{new_lat:.6f}",
    }
    dist = haversine_distance(anchor_lat, anchor_lon, new_lat, new_lon)
    drift_dist = haversine_distance(anchor_lat, anchor_lon, base_lat, base_lon)
    logger.info(
        "位置抖动 (日漂移≈%.1fm, 本次偏移≈%.1fm, σ≈%.1fm): %s, %s",
        drift_dist, dist, sigma, jittered["longitude"], jittered["latitude"],
    )
    return jittered
