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
import math
import random
import requests
from typing import Dict, Tuple

from core.config.common import AMAP_WEB_KEY, TENCENT_IP_LOCATION_URL, TENCENT_MAP_KEY, XCX_REFERER
from core.utils.params import _normalize_address_text
from core.utils.requests import _get_proxies
from core.utils.logs import _log_http_request

logger = logging.getLogger(__name__)


def resolve_ip_location(ip: str, tencent_key: str, timeout: int = 5, config: dict = None) -> dict:
    """
    通过腾讯 IP 定位 API 将 IP 地址解析为地理位置。

    参数:
        ip:          客户端 IP 地址
        tencent_key: 腾讯地图 WebService API Key
        timeout:     请求超时秒数
        config:      完整 config 字典（用于读取代理配置）

    返回:
        {"province": "...", "city": "...", "district": "...", "adcode": "...", "lat": ..., "lng": ...}
        失败返回空字典
    """
    if not tencent_key or not ip:
        return {}
    if ip in ("127.0.0.1", "::1", "localhost") or ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")):
        return {"city": "内网", "province": "", "district": ""}
    try:
        t0 = time.time()

        proxies = _get_proxies(config) if config else None
        if proxies:
            logger.info("代理IP：" + proxies.get("http", ""))

        resp = requests.get(
            TENCENT_IP_LOCATION_URL,
            params={"ip": ip, "key": tencent_key},
            timeout=timeout,
            proxies=proxies,
        )
        elapsed = int((time.time() - t0) * 1000)
        _log_http_request(
            action="腾讯 IP 定位",
            url=TENCENT_IP_LOCATION_URL,
            method="GET",
            req_params={"ip": ip, "key": tencent_key},
            req_body="",
            resp_status=resp.status_code,
            resp_body=resp.text,
            duration_ms=elapsed,
        )

        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get("status") != 0:
            return {}
        result = data.get("result", {})
        ad_info = result.get("ad_info", {})
        location = result.get("location", {})
        return {
            "province": ad_info.get("province", ""),
            "city": ad_info.get("city", ""),
            "district": ad_info.get("district", ""),
            "adcode": str(ad_info.get("adcode", "")),
            "lat": location.get("lat"),
            "lng": location.get("lng"),
        }
    except (requests.RequestException, KeyError, TypeError):
        return {}


# ------------------------------------------------------------------
# 纯数学工具
# ------------------------------------------------------------------

# 1度纬度对应的近似米数（WGS84 平均），可视为常量
METERS_PER_DEG_LAT = 111320.0

def get_daily_base(anchor_lat: float,
                   anchor_lon: float,
                   drift_range: Tuple[float, float] = (5, 100)) -> Tuple[float, float]:
    """
    围绕锚点随机生成一个新的基准点（无缓存，每次调用均不同）。

    Args:
        anchor_lat: 锚点纬度 (度)
        anchor_lon: 锚点经度 (度)
        drift_range: 偏移距离范围 (最小米, 最大米)，默认 5~100 米。

    Returns:
        (new_lat, new_lon) 新基准点坐标 (度)
    """
    if drift_range[0] < 0 or drift_range[0] > drift_range[1]:
        raise ValueError(f"drift_range 必须满足 0 <= min <= max，当前: {drift_range}")

    # 随机方位与距离
    bearing = random.uniform(0, 2 * math.pi)
    distance = random.uniform(*drift_range)

    # 预计算锚点纬度的余弦，并防范极点除零
    cos_lat = math.cos(math.radians(anchor_lat))
    if abs(cos_lat) < 1e-10:   # 接近极点时强制为极小值
        cos_lat = math.copysign(1e-10, cos_lat)

    # 将距离转换为经纬度增量
    delta_lat = distance * math.cos(bearing) / METERS_PER_DEG_LAT
    delta_lon = distance * math.sin(bearing) / (METERS_PER_DEG_LAT * cos_lat)

    return anchor_lat + delta_lat, anchor_lon + delta_lon


def jitter_location_gaussian(lat: float, lon: float,
                             sigma_meters: float, max_radius: float = 500.0) -> tuple:
    """二维高斯抖动：模拟真实 GPS 漂移（距离服从瑞利分布）。"""
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

    proxies = _get_proxies(config)
    if proxies:
        logger.info("代理IP：" + proxies.get("http", ""))

    if session:
        resp = session.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies)
    elapsed = int((time.time() - t0) * 1000)


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
    proxies = _get_proxies(config)
    if proxies:
        logger.info("代理IP：" + proxies.get("http", ""))

    if session:
        resp = session.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies)
    elapsed = int((time.time() - t0) * 1000)

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

# 将 3.44 定义为常量，代表使 99.99% 高斯点落在半径内的 σ 系数
GAUSS_SIGMA_RATIO = 3.44

def apply_location_jitter(config: dict) -> Dict[str, str]:
    """
    计算抖动后的经纬度，最终距离不超过 map_radius（重试机制）。

    参数:
        config: 配置字典（需含 location、locationJitterMeters、dailyDriftMinMeters 等）

    返回:
        {"longitude": "xxx.xxxxxx", "latitude": "xxx.xxxxxx"}
    """
    # 1. 提取并校验 location
    location = config.get("location")
    if not isinstance(location, dict):
        raise RuntimeError("经纬度配置缺失，无法进行位置抖动")
    try:
        anchor_lon = float(location["longitude"])
        anchor_lat = float(location["latitude"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("经纬度格式无效，无法进行位置抖动")

    # 2. 读取并规范化参数
    radius = float(config.get("locationJitterMeters", 100))
    radius = max(0.0, min(radius, 500.0))
    if radius <= 0:
        raise RuntimeError("位置抖动已禁用，但风控要求必须启用抖动")

    map_radius = float(config.get("map_radius", 500))
    if map_radius <= 0:
        raise RuntimeError("围栏半径 map_radius 必须大于 0")

    # 确保围栏足够容纳最大抖动半径和最小漂移
    if map_radius <= radius:
        raise RuntimeError(f"围栏半径({map_radius:.1f}米)必须大于抖动半径({radius:.1f}米)")
    drift_upper_limit = max(1.0, map_radius - radius)

    drift_min = float(config.get("dailyDriftMinMeters", 10))
    drift_max = float(config.get("dailyDriftMaxMeters", 100))
    drift_min = max(1.0, min(drift_min, drift_upper_limit))  #
    drift_max = max(drift_min, min(drift_max, drift_upper_limit)) #
    if drift_min > drift_max:  # 显式检查
        raise RuntimeError(f"日漂移范围无效: min={drift_min}, max={drift_max}")



    # 3. 生成每日基准点（无缓存）
    base_lat, base_lon = get_daily_base(anchor_lat, anchor_lon,
                                        drift_range=(drift_min, drift_max))

    # 4. 高斯抖动 + 围栏重试
    sigma = radius / GAUSS_SIGMA_RATIO
    max_retries = 5
    final_lat = final_lon = None
    final_dist = 0.0

    for attempt in range(max_retries):
        lat, lon = jitter_location_gaussian(base_lat, base_lon, sigma,
                                            max_radius=radius)
        dist = haversine_distance(anchor_lat, anchor_lon, lat, lon)
        if dist <= map_radius:
            final_lat, final_lon = lat, lon
            final_dist = dist
            break
        if attempt < max_retries - 1:
            logger.debug(
                "抖动超出围栏 %sm (本次 %.1fm)，重试 (%d/%d)",
                map_radius, dist, attempt + 1, max_retries,
            )
    else:
        # 重试耗尽，强制缩放到围栏边缘
        if dist > 0:
            scale = map_radius / dist
            final_lat = anchor_lat + (lat - anchor_lat) * scale
            final_lon = anchor_lon + (lon - anchor_lon) * scale
            final_dist = map_radius
        else:
            # 降级处理：基准点完全与锚点重合，直接使用锚点
            final_lat, final_lon = anchor_lat, anchor_lon
            final_dist = 0.0
            logger.warning("基准点与锚点重合且重试耗尽，直接使用锚点")
        logger.warning(
            "抖动重试%d次仍超出围栏，已强制锁定在边界 (缩放后 %.1fm)",
            max_retries, final_dist,
        )

    # 5. 格式化输出
    jittered = {
        "longitude": f"{final_lon:.6f}",
        "latitude": f"{final_lat:.6f}",
    }
    # 日志记录（日漂移距离只算一次）
    drift_dist = haversine_distance(anchor_lat, anchor_lon, base_lat, base_lon)
    logger.info(
        "位置抖动 (日漂移≈%.1fm, 本次偏移≈%.1fm, σ≈%.1fm): %s, %s",
        drift_dist, final_dist, sigma, jittered["longitude"], jittered["latitude"],
    )
    return jittered