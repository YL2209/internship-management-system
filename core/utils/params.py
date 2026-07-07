import hashlib
import hmac
import json
import random
import re
import string
import time
import urllib.parse

from gmssl import sm2

from core.config.common import (
    XCX_APP_ID,
    XCX_EXCLUDED_KEYS,
    XCX_KEY,
    XCX_N_HEADER,
    XCX_SM2_MODE,
    XCX_SM2_PUBLIC_KEY,
)


def rand_str(length=16, chars=string.ascii_letters + string.digits):
    """生成指定长度的随机字符串（字母+数字）。用于 uid 生成。"""
    return ''.join(random.choice(chars) for _ in range(length))


def _get_timestamp() -> int:
    """获取当前毫秒级时间戳。"""
    return int(time.time() * 1000)


def _normalize_header_token_value(value):
    """
    将任意类型的值标准化为字符串，用于签名计算中的字符串拼接。
    - None → ""
    - str → 原值
    - list/tuple/set/dict → JSON 序列化（排序键、无空格）
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict, set)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def _sanitize_sign_text(value):
    return (
        str(value)
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("<", "")
        .replace(">", "")
        .replace("&", "")
        .replace("-", "")
        .replace(r"\uD83C[\uDF00-\uDFFF]", "")
        .replace(r"\uD83D[\uDC00-\uDE4F]", "")
    )


def _normalize_security_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict, set)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)
    return str(value)


def _normalize_address_text(value):
    """
    规范化地址文本。
    支持 str / list / dict 等多种格式，递归提取可用的地址字符串。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        parts = [_normalize_address_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("formatted_address", "address", "name", "value"):
            text = _normalize_address_text(value.get(key))
            if text:
                return text
        return str(value).strip()
    return str(value).strip()


def _djb2(value):
    result = 5381
    for char in str(value or ""):
        result = ((result * 33) + ord(char)) & 0xFFFFFFFF
    return result


def _security_data_sign(data):
    special_char_regex = re.compile(r"[`~!@#$%^&*()+=|{}':;',\[\].<>/?~！@#￥%……&*（）——+|{}【】‘；：”“’。，、？]")
    raw = ""
    for key in sorted(k for k in data if k not in ("h5st", "_stk", "_ste")):
        value_text = _normalize_security_value(data[key])
        if key not in XCX_EXCLUDED_KEYS and not special_char_regex.search(value_text):
            raw += f"{key}{value_text}"
    return urllib.parse.quote(_sanitize_sign_text(raw).replace("[]", ""))


def create_security_fingerprint():
    return hashlib.md5(f"{int(_get_timestamp())}_{random.random()}".encode("utf-8")).hexdigest()


def get_security_url_token(security_token):
    return str(_djb2(security_token))


def get_security_params(data, security_token, fingerprint):
    timestamp = int(_get_timestamp())
    app_sign = hashlib.md5(f"{_djb2(security_token)}{fingerprint}{timestamp}{XCX_APP_ID}".encode("utf-8")).hexdigest()
    data_sign = _security_data_sign(data)
    sign_type = str(security_token or "")[:1]
    st = ""
    if sign_type == "0":
        st = hashlib.md5(f"{data_sign}{app_sign}".encode("utf-8")).hexdigest()
    elif sign_type == "1":
        st = hashlib.sha256(f"{data_sign}{app_sign}".encode("utf-8")).hexdigest()
    elif security_token:
        st = hmac.new(str(app_sign).encode("utf-8"), str(data_sign).encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "st": st,
        "ts": str(timestamp),
        "fp": fingerprint,
    }


def get_device_code(openId: str, device: dict):
    """
    使用 SM2 国密公钥加密设备指纹，生成 devicecode 请求头。
    加密前的明文格式:
        b|<brand>,<model>,<system>,<platform>aid|<appId>t|<timestamp>uid|<rand>oid|<openId>
    参数:
        open_id: 微信 openId（可为空字符串）
        device: 设备信息字典 {brand, model, system, platform}
    返回:
        SM2 加密后的十六进制字符串
    """
    sm2_crypt = sm2.CryptSM2(
        public_key=XCX_SM2_PUBLIC_KEY,
        private_key=None,
        mode=XCX_SM2_MODE,
    )
    plain = (
        f"b|_{device['brand']},{device['model']},{device['system']},{device['platform']}"
        f"aid|_{XCX_APP_ID}"
        f"t|_{int(_get_timestamp())}"
        f"uid|_{rand_str()}"
        f"oid|_{openId}"
    )
    return sm2_crypt.encrypt(plain.encode()).hex().strip()



def get_header_token(data: dict) -> dict:
    """
    生成请求签名头 {m, t, s, n} —— 小程序的核心反爬机制。

    签名算法分步解释:
        第一步 — 随机选取字符：
            从 XCX_KEY（62字符密钥）中按 20 个随机索引取出字符拼接成字符串 g。
            这个 g 是"一次性随机因子"，每次请求都不同，防止重放攻击。

        第二步 — 拼接参数字符串：
            将请求参数字典按键名排序，只拼接「不在排除列表且不含特殊字符」的值。
            排除列表 XCX_EXCLUDED_KEYS 包含所有"用户输入类"字段（如地址、说明等），
            这些字段值不可控，不参与签名。

        第三步 — 组装签名字符串：
            d = (参数字符串) + (当前时间戳秒) + (随机因子g)
            然后清除空格、换行、尖括号、&、- 和 emoji 字符。

        第四步 — URL编码 → MD5：
            对 d 进行 URL 编码（%XX 格式），计算 MD5 哈希作为最终签名值 m。

    最终请求头含义:
        - m: MD5 签名值，服务端用同样算法校验请求合法性
        - t: 时间戳（秒），服务端校验时效性（通常允许几分钟偏差）
        - s: 20 个随机索引（下划线连接），服务端用于重建签名
        - n: 排除字段名列表（逗号拼接），告诉服务端哪些字段不参与签名

    参数:
        data: 请求参数字典（params/data）

    返回:
        {"m": MD5签名, "t": 时间戳秒, "s": 索引列表, "n": 排除字段名列表}
    """

    # --- 第一步：构建随机因子 g ---
    # 映射列表
    n = list(XCX_KEY)

    # 初始化o列表
    o = [str(i) for i in range(62)]

    # 获取当前时间戳（秒）
    l = int(time.time())

    # 随机打乱o列表并选取前20个元素
    p = random.sample(o, 20)

    # 拼接字符串g
    g = "".join(n[int(e)] for e in p)

    # --- 第二步：拼接参数字符串 ---
    # 排序传入字典e的键
    u = {k: data[k] for k in sorted(data)}

    # 初始化结果字符串d
    d = ""

    # 正则表达式：匹配特殊字符
    special_char_regex = re.compile(r"[`~!@#$%^&*()+=|{}':;',\[\].<>/?~！@#￥%……&*（）——+|{}【】‘；：”“’。，、？]")

    # 遍历u字典，构建d字符串
    for c in u:
        value_text = _normalize_header_token_value(u[c])
        # 如果字段值不包含特殊字符且不在排除字段中
        if c not in XCX_EXCLUDED_KEYS and not special_char_regex.search(value_text):
            d += value_text

    # --- 第三步：组装并清理签名字符串 ---
    # 拼接最终的字符串
    d = f"{d}{l}{g}"

    # 清理掉不需要的字符
    d = _sanitize_sign_text(d)

    # --- 第四步：URL编码 → MD5 ---
    # URL 编码
    d = urllib.parse.quote(d)
    # 计算MD5值
    md5_value = hashlib.md5(d.encode('utf-8')).hexdigest()

    return {
        "m": md5_value,                            # MD5 签名值（16进制32字符）
        "t": str(l),                                # 时间戳（秒）
        "s": "_".join(p) if len(p) > 0 else "",     # 索引列表（下划线连接）
        "n": XCX_N_HEADER,                          # 排除字段名列表
    }

