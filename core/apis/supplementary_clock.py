"""
实习数据管理系统补签管理模块

用法:
    from core.supplementary_clock import SupplementaryClockManager
    mgr = SupplementaryClockManager(config, session)
    login_args = mgr.login()
    abnormal_dates = mgr.load_abnormal_dates(login_args)
    # 根据异常日期查询可补签的详情
    detail = mgr.load_supplementary_data(login_args, date_str="2026.06.14")
    # 提交补签签到（clockType=0）或签退（clockType=2）
    mgr.submit_supplementary_clock(login_args,
                                   clock_date="2026.06.14",
                                   clock_type="0",
                                   clock_time="09:00",
                                   clock_reason="忘记打卡",
                                   address="北京市朝阳区",
                                   location_id="110105",
                                   longitude="116.397128",
                                   latitude="39.916527")
"""

import logging
import time

import requests

from core.apis.signer import SignInClient
from core.utils.cache import _check_session_valid, DEFAULT_CACHE_FILE
from core.utils.location import apply_location_jitter
from core.utils.logs import log_record
from core.utils.requests import _form_post

logger = logging.getLogger(__name__)

ACTION_MAP = {
    "supplementary_sign_in":  "补签签到",
    "supplementary_sign_out": "补签签退",
}

class SupplementaryClockManager:
    def __init__(self, config: dict, session: requests.Session):
        self.config = config
        self.session = session
        self.cache_file = None  # 复用缓存？通常不需要独立缓存

    def login(self) -> dict:
        """复用 SignInClient 登录获取参数。"""
        client = SignInClient(self.config, self.session, cache_file=str(DEFAULT_CACHE_FILE))
        login_args = client.login()
        # 获取 traineeId
        plan_data = client._get_plan(login_args)
        if plan_data and plan_data[0].get("dateList"):
            login_args["traineeId"] = str(plan_data[0]['dateList'][0]['traineeId'])
        return login_args

    def load_abnormal_dates(self, args: dict) -> list:
        """
        加载异常签到日期列表。返回示例: ["2026.06.13", "2026.06.14", ...]

        严格对照 sign-sign-in-master：使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/practiceapply/loadAbnormalClockDate.action"
        data = {"traineeId": str(args["traineeId"])}


        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="异常签到数据查询")
        res = resp.json()
        if not _check_session_valid(res):
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"获取异常日期失败: {res.get('msg')}")

    def load_supplementary_data(self, args: dict, date_str: str) -> dict:
        """
        加载指定日期的补签详情。

        严格对照 sign-sign-in-master：使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/practiceapply/loadSupplementaryClockData.action"
        data = {
            "traineeId": str(args["traineeId"]),
            "clockDate": date_str,
        }

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="补签数据查询")
        res = resp.json()
        if not _check_session_valid(res):
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"获取补签数据失败: {res.get('msg')}")

    def submit_supplementary_clock(self, args: dict, clock_date: str, clock_type: str,
                                clock_time: str, clock_reason: str, address: str,
                                location_id: str, longitude: str, latitude: str,
                                teacher_role_str: str = '[{"teacherRole":0,"seque":1}]') -> bool:
        """
        提交补签申请。

        严格对照 sign-sign-in-master：使用 _form_post（范式 A），
        st/ts/fp 注入 POST body，?t=DJB2 附加 URL。
        """
        started_at = time.time()
        action = "supplementary_sign_in" if clock_type == "0" else "supplementary_sign_out"

        try:
            url = "https://xcx.xybsyw.com/student/practiceapply/submitSupplementaryClock.action"
            data = {
                "traineeId": str(args["traineeId"]),
                "clockDate": clock_date,
                "clockType": clock_type,
                "clockTime": clock_date + " " + clock_time,
                "clockReason": clock_reason,
                "imageStr": "",
                "teacherRoleStr": teacher_role_str,
                "sendTeacherStr": "",
                "clockAddress": address,
                "clockLocationId": location_id,
                "clockLong": longitude,
                "clockLat": latitude,
            }

            timeout = int(self.config.get("requestTimeout", 10))
            resp = _form_post(
                url, data, config=self.config, args=args,
                include_device_code=True, timeout=timeout, action=ACTION_MAP[action],
            )
            res = resp.json()
            if not _check_session_valid(res):
                raise RuntimeError("JSESSIONID 已失效")
            if res.get("code") == "200":
                elapsed = time.time() - started_at
                message = "✅ 补签签到申请成功" if clock_type == "0" else "✅ 补签签退申请成功"
                logger.info("%s (耗时 %.1fs): %s", message, elapsed, res.get('msg'))
                log_record(
                    config=self.config,
                    action=action,
                    success=True,
                    message=message,
                    address=address,
                    latitude=latitude,
                    longitude=longitude,
                    adcode=location_id,
                    trainee_id=str(args["traineeId"]),
                    elapsed_ms=int(elapsed * 1000),
                )
                return True
            else:
                raise RuntimeError(f"补签失败: {res.get('msg')}")

        except Exception as e:
            elapsed = time.time() - started_at
            message = "补签签到" if clock_type == "0" else "补签签退"
            logger.error("%s 失败 (耗时 %.1fs): %s", message, elapsed, e)
            log_record(
                config=self.config,
                action=action,
                success=False,
                message=str(e),
                elapsed_ms=int(elapsed * 1000),
            )
            raise

    def load_student_clock_detail(self, args: dict, startDate: str, endDate: str,
                                   page: str = "1", pageSize: str = "10") -> list:
        """
        加载签到详情列表（分页）。

        严格对照 sign-sign-in-master：使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/clock/loadStudentClockDetail.action"
        data = {
            "page": str(page),
            "pageSize": str(pageSize),
            "planId": str(args.get("planId", "")),
            "status": "",
            "startDate": str(startDate),
            "endDate": str(endDate),
            "sort": "",
            "traineeId": str(args["traineeId"]),
        }


        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="签到详情列表加载")
        res = resp.json()
        if not _check_session_valid(res):
            raise RuntimeError("JSESSIONID 已失效")

        result = res.get("data", {})
        if isinstance(result, dict) and "list" in result:
            return result["list"]
        raise RuntimeError(f"响应数据结构异常: {res}")
    
    
    def load_ranking_list(self, args: dict, months: str = None) -> dict:
        """
        获取奋斗排行榜（月度平均工时）。

        严格对照 sign-sign-in-master：使用 _form_post（范式 A）。
        """
        if months is None:
            from datetime import datetime
            months = datetime.now().strftime("%Y-%m")

        url = "https://xcx.xybsyw.com/student/clock/PunchIn!rankingList.action"
        data = {
            "traineeId": str(args["traineeId"]),
            "months": months,
        }

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="奋斗排行榜")
        res = resp.json()

        if not _check_session_valid(res):
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200":
            return res
        raise RuntimeError(f"获取排行榜失败: {res.get('msg')}")
    
    def load_today_clock_status(self, args: dict) -> dict:
        """
        获取当天签到状态详情。

        严格对照 sign-sign-in-master：使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/clock/GetPlan!detail.action"
        data = {"traineeId": str(args["traineeId"])}

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="当天签到状态详情")
        res = resp.json()

        if not _check_session_valid(res):
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"获取当天签到状态失败: {res.get('msg')}")

    def _apply_location_jitter(self) -> dict | None:
        """位置抖动 → 委托给 core.utils.location.apply_location_jitter。"""
        return apply_location_jitter(self.config)
