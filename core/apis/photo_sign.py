"""
小程序拍照签到模块（改进版）
=============================
封装了拍照签到的完整流程：登录 -> 获取上传凭证 -> 上传图片至 OSS -> 提交签到。
所有签到坐标均经过强制抖动处理，并清除图片隐私信息（EXIF），以降低风控风险。

用法:
    from core.photo_sign import PhotoSignInManager
    import requests

    config = json.load(open("config.json"))
    session = requests.Session()
    mgr = PhotoSignInManager(config, session)

    # 登录（自动复用缓存）
    login_args = mgr.login()

    # 执行拍照签到
    opt = {"action": "sign_in", "image_path": "/path/to/photo.jpg"}
    mgr.photo_sign(login_args, opt)
"""

import json
import logging
import os
import time

import requests

from core.apis.signer import SignInClient
from core.config.common import XCX_REFERER
from core.utils.cache import DEFAULT_CACHE_FILE
from core.utils.logs import _log_http_request, log_record
from core.utils.requests import _form_post

logger = logging.getLogger(__name__)

ACTION_MAP = {
    "sign_in":  "拍照签到",
    "sign_out": "拍照签退",
}
CLOCK_STATUS_MAP = {
    "拍照签到": 2,
    "拍照签退": 1,
}

# 日志中 action 的中文名称
ACTION_LABEL = {
    "sign_in":  "照签到",
    "sign_out": "照签退",
}

ACTION_LABEL_PHOTO = {
    "sign_in":  "photo_sign_in",
    "sign_out": "photo_sign_out",
}

class PhotoSignInManager:
    """
    拍照签到管理器
    ---------------
    负责整个拍照打卡流程：
        1. 复用 SignInClient 完成登录，获取 traineeId / openId 等。
        2. 调用位置抖动 + 逆地理编码，获得安全坐标和地址。
        3. 获取阿里云 OSS 上传凭证，将本地图片上传至 OSS。
        4. 向小程序提交拍照签到请求（PostNew.action）。
    """

    def __init__(self, config: dict, session: requests.Session):
        """
        初始化管理器
        :param config: 配置字典（包含认证、设备、位置等信息）
        :param session: requests.Session 实例，用于维持 HTTP 会话和 Cookie
        """
        self.config = config
        self.session = session
        # 嵌入一个 SignInClient 实例，复用其登录、位置抖动、逆地理等功能
        self.sign_client = SignInClient(config, session, cache_file=str(DEFAULT_CACHE_FILE))

    # ------------------------------------------------------------------
    # 登录（复用 SignInClient）
    # ------------------------------------------------------------------
    def login(self) -> dict:
        """
        登录并获取 traineeId、openId 等必要参数。
        内部调用 SignInClient.login()，若返回结果缺少 traineeId 则手动补充。

        :return: 包含 openId, unionId, encryptValue, sessionId, traineeId 的字典
        """
        args = self.sign_client.login()
        # 如果 login 返回的结果中没有 traineeId（如手动模式），手动获取一次（内部已缓存）
        if "traineeId" not in args:
            plan_data = self.sign_client._get_plan(args)
            if plan_data and plan_data[0].get("dateList"):
                args["traineeId"] = str(plan_data[0]["dateList"][0]["traineeId"])
        return args

    # ------------------------------------------------------------------
    # 拍照签到主流程
    # ------------------------------------------------------------------
    def photo_sign(self, args: dict, opt: dict) -> bool:
        """
        执行一次拍照签到（签到或签退），成功返回 True，失败抛出异常。
        """
        started_at = time.time()
        action = opt.get("action")
        if action not in ACTION_MAP:
            raise ValueError(f"action 必须是 sign_in 或 sign_out，收到: {action}")

        # 校验图片路径
        image_path = opt.get("image_path")
        if not image_path:
            raise ValueError("缺少图片路径 (image_path)")

        # 获取 traineeId（如果 args 中没有）
        trainee_id = args.get("traineeId")
        if not trainee_id:
            plan_data = self.sign_client._get_plan(args)
            if plan_data and plan_data[0].get("dateList"):
                trainee_id = str(plan_data[0]["dateList"][0]["traineeId"])
                args["traineeId"] = trainee_id
            else:
                raise RuntimeError("无法获取 traineeId，请检查实习计划")

        # 位置信息（必须使用抖动坐标）
        geo = opt.get("geo")
        jittered_location = None
        if not geo:
            jittered = self.sign_client._apply_location_jitter()
            if not jittered or 'longitude' not in jittered or 'latitude' not in jittered:
                raise RuntimeError("位置抖动失败，无法获取有效坐标，已中断拍照签到以保护风控")
            geo = self.sign_client._regeo(lon=jittered["longitude"], lat=jittered["latitude"])
            jittered_location = jittered

        # 确定 clockStatus：1=签退，2=签到
        clock_status = str(CLOCK_STATUS_MAP[ACTION_MAP[action]])
        opt["code"] = clock_status

        try:
            result_msg = self._photo_sign_in_or_out(args, geo, trainee_id, opt, jittered_location)
            elapsed = time.time() - started_at
            log_record(
                config=self.config,
                action=ACTION_LABEL_PHOTO[action],
                success=True,
                message=result_msg,
                geo=geo,
                elapsed=elapsed,
                trainee_id=str(trainee_id),
                lat_lng=jittered_location,
                auth_mode=self.config.get("auth_mode", "auto"),
            )
            return True
        except Exception as e:
            elapsed = time.time() - started_at
            logger.error(f"❌ {ACTION_LABEL[action]}失败 (耗时 {elapsed:.1f}s): {e}")
            log_record(
                config=self.config,
                action=ACTION_LABEL_PHOTO[action],
                success=False,
                message=str(e),
                elapsed=elapsed,
            )
            raise

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _photo_sign_in_or_out(self, args: dict, geo: dict, trainee_id: str,
                              opt: dict, jittered_location: dict = None) -> str:
        """
        内部流程：获取上传凭证 → 构造文件 → 上传 OSS → 提交签到。

        :param args: 登录参数
        :param geo: 逆地理编码结果（含地址、adcode、pois 等）
        :param trainee_id: 实习生 ID
        :param opt: 操作选项
        :param jittered_location: 抖动后的经纬度字典（{"longitude": ..., "latitude": ...}）
        """
        logger.info("正在执行拍照签到流程...")

        # 1. 获取阿里云 OSS 上传凭证（policy、签名等）
        policy_data = self._common_post_policy(args)

        # 2. 构造要上传的文件对象
        timestamp = int(time.time() * 1000)
        files = self._get_img_file(timestamp, opt.get("image_path"))

        try:
            # 3. 上传至阿里云 OSS
            oss_data = self._aliyun_oss(files, timestamp, policy_data)
            img_url = oss_data["key"]   # OSS 上的图片路径，后续提交时需要

            # 4. 提交拍照签到（使用抖动后的坐标）
            result_msg = self._post_new(args, trainee_id, geo, img_url, opt, jittered_location)
            return result_msg

        finally:
            # 安全关闭文件句柄，防止资源泄露
            if isinstance(files, dict) and 'file' in files:
                file_info = files['file']
                if isinstance(file_info, tuple) and len(file_info) >= 2:
                    file_obj = file_info[1]
                    if file_obj:
                        file_obj.close()

    def _common_post_policy(self, args: dict) -> dict:
        """
        获取阿里云 OSS 上传凭证。

        严格对照 sign-sign-in-master commonPostPolicy()：
        使用 _form_post（范式 A），include_device_code=True。
        """
        url = "https://xcx.xybsyw.com/uploadfile/commonPostPolicy.action"
        data = {
            "customerType": "STUDENT",
            "uploadType": "UPLOAD_STUDENT_CLOCK_IMGAGES",
            "publicRead": "true",
        }

        t0 = time.time()
        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=True, timeout=timeout, action="获取阿里云 OSS 上传凭证"
        )
        res = resp.json()
        if resp.status_code != 200 or res.get("code") != "200":
            raise RuntimeError(f"获取上传凭证失败: {resp.text}")
        return res["data"]

    def _get_img_file(self, timestamp: int, image_path: str) -> dict:
        """
        根据本地图片路径构造用于 requests 上传的文件字典。

        :param timestamp: 毫秒时间戳，用作文件名前缀
        :param image_path: 图片文件绝对路径
        :return: {"file": ("filename", file_object, "image/jpeg")}
        """
        if not image_path or not os.path.exists(image_path):
            raise FileNotFoundError(f"图片文件不存在: {image_path}")

        f = open(image_path, "rb")
        filename = f"{timestamp}.jpg"
        return {"file": (filename, f, "image/jpeg")}

    def _aliyun_oss(self, files: dict, timestamp: int, policy_data: dict) -> dict:
        """
        上传图片至阿里云 OSS。

        :param files: 文件字典（由 _get_img_file 返回）
        :param timestamp: 时间戳
        :param policy_data: 上传凭证（由 _common_post_policy 返回）
        :return: OSS 返回的 JSON，包含 key 等信息
        """
        logger.info("正在上传图片至阿里云 OSS...")
        url = policy_data["host"]           # OSS 上传地址
        headers = {
            "Referer": XCX_REFERER,
            "User-Agent": self.config.get("userAgent", ""),
        }
        # 图片在 OSS 上的存储路径
        key = f"{policy_data['dir']}/{timestamp}.jpg"
        logger.info(f"OSS key: {key}")

        # 构造上传表单数据（包含 policy、签名等）
        data = {
            "key": key,
            "policy": policy_data["policy"],
            "OSSAccessKeyId": policy_data["accessid"],
            "signature": policy_data["signature"],
            "success_action_status": "200",
            "customerType": policy_data["customParams"]["x:customer_type_key"],
            "uploadType": policy_data["customParams"]["x:upload_type_key"],
            "callback": policy_data["callback"],
        }
        t0 = time.time()
        timeout = int(self.config.get("requestTimeout", 15))
        resp = requests.post(url, data=data, files=files, headers=headers, timeout=timeout)
        elapsed = int((time.time() - t0) * 1000)
        _log_http_request(action="上传图片至阿里云 OSS", url=url, method="POST",
                          req_headers=headers, req_body=str(data),
                          resp_status=resp.status_code, resp_body=resp.text, duration_ms=elapsed)
        if resp.status_code != 200:
            raise RuntimeError(f"OSS 上传失败: {resp.text}")
        res = resp.json()
        return res["vo"]   # vo 中包含 key 等信息

    def _post_new(self, args: dict, trainee_id: str, geo: dict, img_url: str,
                  opt: dict, jittered_location: dict = None) -> str:
        """
        提交拍照签到（PostNew.action）。

        严格对照 sign-sign-in-master post_new()：
        使用 _form_post（范式 A），include_device_code=True。
        """
        if jittered_location:
            lat = jittered_location.get("latitude")
            lng = jittered_location.get("longitude")
        else:
            raise RuntimeError("位置抖动失败，无法获取有效坐标，已中断签到以保护风控")

        url = "https://xcx.xybsyw.com/student/clock/PostNew.action"
        data = {
            "traineeId": str(trainee_id),
            "adcode": geo["addressComponent"]["adcode"],
            "lat": lat,
            "lng": lng,
            "address": geo["formatted_address"],
            "deviceName": self.config.get("device", {}).get("model", ""),
            "punchInStatus": "0",
            "clockStatus": str(opt["code"]),
            "imgUrl": img_url,
            "reason": "",
            "addressId": "null",
        }

        t0 = time.time()
        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=True, timeout=timeout, action="提交拍照签到"
        )
        res = resp.json()

        if resp.status_code != 200 or res.get("code") != "200":
            raise RuntimeError(f"拍照签到提交失败: {res.get('msg', 'Unknown')}")
        return f"✅ 拍照签到成功: {res.get('msg', 'success')}"
