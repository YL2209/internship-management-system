"""
实习数据管理系统周记/月记管理模块 (Headless)

基于 core.signer 的登录和签名能力，提供周记管理功能：
- 加载周记年份和月份
- 加载指定年月的周信息
- 提交周记/月记
- 获取已提交周记列表
- 本地记录和历史管理

用法:
    import requests, json
    from core.journal import JournalManager

    config = json.load(open("config.json"))
    session = requests.Session()
    mgr = JournalManager(config, session)

    login_args = mgr.login()
    year_data = mgr.load_blog_year(login_args)
    week_data = mgr.load_blog_date(login_args, 2026, 6)
    mgr.submit_blog(login_args, "标题", "正文...", "2026-06-15", "2026-06-21", 1)
"""

import copy
import json
import logging
import os
import threading
import time

import requests

from core.apis.signer import SignInClient
from core.config.paths import get_log_dir, get_project_root
from core.utils.cache import _check_session_valid, DEFAULT_CACHE_FILE
from core.utils.requests import _form_post

logger = logging.getLogger(__name__)

# 周记历史文件
JOURNAL_HISTORY_FILE = os.path.join(get_log_dir(), "journal_history.json")

# 周记记录文件（JSONL，供 Web 界面查询）
JOURNAL_RECORDS_FILE = os.path.join(get_log_dir(), "journal_records.jsonl")

# ============================================================
# JournalManager —— 周记管理器
# ============================================================

class JournalManager:
    """
    实习数据管理系统周记/月记管理器。

    复用 core.signer.SignInClient 完成登录和认证，
    在此基础上提供周记/月记的加载、提交、查询功能。
    """

    _record_lock = threading.Lock()

    def __init__(self, config: dict, session: requests.Session,
                 cache_file: str = None):
        self.config = copy.deepcopy(config)
        self.session = session

        self.cache_file = cache_file or str(DEFAULT_CACHE_FILE)

        self.auth_mode = str(self.config.get("auth_mode", "auto")).strip().lower()
        if self.auth_mode not in ("auto", "manual"):
            self.auth_mode = "auto"

    # ------------------------------------------------------------------
    # 登录 —— 复用 SignInClient
    # ------------------------------------------------------------------

    def login(self) -> dict:
        """
        通过 SignInClient 获取登录参数。
        SignInClient 内部已处理缓存读写，JournalManager 不再维护独立的缓存副本。

        返回:
            {"openId": str, "unionId": str, "encryptValue": str, "sessionId": str, "traineeId": str}
        """


        try:
            client = SignInClient(self.config, self.session, cache_file=self.cache_file)
            login_args = client.login()
            if isinstance(login_args, dict) and "openId" in login_args:
                result = dict(login_args)
            else:
                raise RuntimeError(f"SignInClient.login() 返回了意外的数据类型: {type(login_args)}")

            # 获取 traineeId（同时验证 JSESSIONID 有效性）
            plan_data = client._get_plan(result)
            if plan_data and plan_data[0].get("dateList"):
                result["traineeId"] = plan_data[0]["dateList"][0]["traineeId"]
            return result
        except Exception as e:
            msg = str(e).lower()
            # 如果是 session 相关错误，手动模式一样无效，直接失败
            if any(kw in msg for kw in ("jsessionid", "sessionid", "已过期", "已失效", "无效")):
                logger.error("JSESSIONID 已失效，跳过手动回退: %s", e)
                raise
            logger.warning(f"SignInClient 登录失败，回退到手动模式: {e}")
            return self._manual_login()

    def _manual_login(self) -> dict:
        """手动模式登录（从配置中直接读取参数，并验证 JSESSIONID 有效性）。"""
        result = {
            "openId": self.config.get("openId", "").strip(),
            "unionId": self.config.get("unionId", "").strip(),
            "encryptValue": self.config.get("encryptValue", "").strip(),
            "sessionId": self.config.get("sessionId", "").strip(),
        }
        missing = [k for k, v in result.items() if not v]
        if missing:
            raise RuntimeError(f"手动模式缺少参数: {', '.join(missing)}")
        # 获取 traineeId（同时验证 JSESSIONID 有效性）
        client = SignInClient(self.config, self.session, cache_file=self.cache_file)
        plan_data = client._get_plan(result)
        if plan_data and plan_data[0].get("dateList"):
            result["traineeId"] = plan_data[0]["dateList"][0]["traineeId"]
        return result

    def _clear_session_cache(self):
        """清除 JSESSIONID 缓存文件。"""
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
            logger.info("已清除会话缓存")

    # ------------------------------------------------------------------
    # 周记年份月份加载
    # ------------------------------------------------------------------

    def load_blog_year(self, args: dict) -> list:
        """
        加载周记年份和月份。
        使用 _form_post（范式 A），st/ts/fp 注入 POST body。
        """
        url = "https://xcx.xybsyw.com/student/blog/LoadBlogDate!weekYear.action"
        data = {"traineeId": str(args.get("traineeId", ""))}

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="加载周记年份和月份")
        res = resp.json()

        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"加载年份月份失败: {res.get('msg', 'Unknown error')}")

    # ------------------------------------------------------------------
    # 周信息加载
    # ------------------------------------------------------------------

    def load_blog_date(self, args: dict, year: int, month: int) -> list:
        """
        加载指定年月下的周信息。
        使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/blog/LoadBlogDate!week.action"
        data = {
            "year": str(year), "month": str(month),
            "traineeId": str(args.get("traineeId", "")), "id": "",
        }

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="加载指定年月下的周信息")
        res = resp.json()

        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"加载周信息失败: {res.get('msg', 'Unknown error')}")

    # ------------------------------------------------------------------
    # 月记日期加载（全量）
    # ------------------------------------------------------------------

    def load_blog_month_list(self, args: dict) -> list:
        """
        加载月记起止日期列表（整个实习周期所有月份）。
        使用 _form_post（范式 A）。
        """
        url = "https://xcx.xybsyw.com/student/blog/LoadBlogDate!month.action"
        data = {"traineeId": str(args.get("traineeId", "")), "id": ""}

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(url, data, config=self.config, args=args, include_device_code=False, timeout=timeout, action="月记日期加载")
        res = resp.json()

        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            return res["data"]
        raise RuntimeError(f"加载月记日期失败: {res.get('msg', 'Unknown error')}")

    # ------------------------------------------------------------------
    # 提交周记
    # ------------------------------------------------------------------

    def submit_blog(self, args: dict, blog_title: str, blog_body: str,
                    start_date: str, end_date: str, blog_open_type: int = 1,
                    trainee_id: str = "", blog_type: str = "1") -> dict:
        """
        提交周记/月记到小程序。
        使用 _form_post（范式 A），include_device_code=True。
        """
        tid = trainee_id or str(args.get("traineeId", ""))
        label_map = {"0": "提交日记", "1": "提交周记", "2": "提交月记"}
        action_label = label_map.get(blog_type, "提交日记")  # 第二个参数可设默认值
        url = "https://xcx.xybsyw.com/student/blog/Blog!save.action"

        data = {
            "blogType": blog_type,
            "blogTitle": blog_title,
            "blogBody": blog_body,
            "blogOpenType": str(blog_open_type),
            "traineeId": str(tid),
            "isDraft": "0",
            "startDate": start_date,
            "endDate": end_date,
            "backgroundTemplateId": "0",
            "fileJson": '[{"fileName":""}]',
            "blogId": "undefined",
        }

        try:
            timeout = int(self.config.get("requestTimeout", 10))
            resp = _form_post(
                url, data, config=self.config, args=args,
                include_device_code=True, timeout=timeout, action=action_label
            )
            res = resp.json()
            if not _check_session_valid(res):
                self._clear_session_cache()
                raise RuntimeError("JSESSIONID 已失效")
            if res.get("code") == "200":
                self._log_journal_record(
                    blog_type=blog_type, title=blog_title, body=blog_body,
                    start_date=start_date, end_date=end_date, success=True,
                )
                raw_data = res.get("data")
                if raw_data is None:
                    return {"blogId": "", "raw": None}
                if isinstance(raw_data, dict):
                    return raw_data
                return {"blogId": raw_data, "raw": raw_data}
            raise RuntimeError(f"提交失败: {res.get('msg', 'Unknown error')}")
        except Exception as e:
            self._log_journal_record(
                blog_type=blog_type, title=blog_title, body=blog_body,
                start_date=start_date, end_date=end_date, success=False, error=str(e),
            )
            raise

    # ------------------------------------------------------------------
    # 获取周记列表
    # ------------------------------------------------------------------

    def blog_list(self, args: dict, page: int = 1, blog_type: str = "1") -> dict:
        """
        获取已提交的周记/月记列表。
        使用 _form_post（范式 A），include_device_code=True。
        """
        url = "https://xcx.xybsyw.com/student/blog/BlogList.action"
        data = {
            "blogType": blog_type, "planId": "",
            "reviewStatus": "null", "page": str(page),
        }

        timeout = int(self.config.get("requestTimeout", 10))
        resp = _form_post(
            url, data, config=self.config, args=args,
            include_device_code=True, timeout=timeout, action="获取周/月记列表"
        )
        res = resp.json()

        if not _check_session_valid(res):
            self._clear_session_cache()
            raise RuntimeError("JSESSIONID 已失效")
        if res.get("code") == "200" and "data" in res:
            data = res["data"]
            # 外部接口可能返回 None, 字符串 "None" 或空字符串，均视为无数据
            if data is None or data == "None" or data == "":
                logger.info(f"日志列表信息：{res.get('msg', '无数据')}")
                # 返回一个空数据字典，格式与有数据时保持一致
                return {
                    "list": [],
                    "page": str(page),
                    "total": "0",
                    "maxPage": "1"
                }
            return data
        raise RuntimeError(f"获取列表失败: {res.get('msg', 'Unknown error')}")

    # ------------------------------------------------------------------
    # 周记记录持久化
    # ------------------------------------------------------------------

    def _log_journal_record(self, blog_type: str, title: str, body: str,
                            start_date: str, end_date: str, success: bool,
                            error: str = ""):
        """记录周记提交历史到 journal_records.jsonl。"""
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "weekly" if blog_type == "1" else "monthly",
            "title": title,
            "body": body[:500] + "..." if len(body) > 500 else body,
            "start_date": start_date,
            "end_date": end_date,
            "success": success,
            "error": error,
        }
        try:
            log_dir = os.path.dirname(JOURNAL_RECORDS_FILE)
            os.makedirs(log_dir, exist_ok=True)
            with self._record_lock:
                with open(JOURNAL_RECORDS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"写入周记记录失败: {e}")

    # ------------------------------------------------------------------
    # 周记历史管理
    # ------------------------------------------------------------------

    @staticmethod
    def load_journal_history() -> dict:
        """加载本地周记历史记录。"""
        if not os.path.exists(JOURNAL_HISTORY_FILE):
            return {"generated": [], "submitted": []}
        try:
            with open(JOURNAL_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"generated": [], "submitted": []}

    @staticmethod
    def save_journal_entry(section: str, content: str, meta: dict = None) -> dict:
        """保存周记条目到历史。"""
        history = JournalManager.load_journal_history()
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "content": content,
        }
        if meta:
            entry.update(meta)

        if section not in history:
            history[section] = []
        history[section].insert(0, entry)
        history[section] = history[section][:50]

        try:
            log_dir = os.path.dirname(JOURNAL_HISTORY_FILE)
            os.makedirs(log_dir, exist_ok=True)
            with open(JOURNAL_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存周记历史失败: {e}")

        return entry

    @staticmethod
    def get_journal_records(limit: int = 50, offset: int = 0) -> list:
        """获取周记提交记录。"""
        if not os.path.exists(JOURNAL_RECORDS_FILE):
            return []
        records = []
        try:
            with open(JOURNAL_RECORDS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception:
            return []
        records.reverse()
        return records[offset:offset + limit]

    @staticmethod
    def save_to_library(journal_type: str, title: str, body: str,
                        start_date: str = "", end_date: str = ""):
        """将提交成功的周记/月记追加到素材库（journals/journals_db.json）。

        供 run.py 和 web/app.py 共用，避免重复逻辑。
        """
        import shutil  # 仅此方法需要

        db_path = os.path.join(get_project_root(), "journals", "journals_db.json")
        if os.path.exists(db_path):
            with open(db_path, "r", encoding="utf-8") as f:
                db = json.load(f)
        else:
            db = {"meta": {}, "weekly": [], "monthly": []}

        kind = "weekly" if journal_type == "weekly" else "monthly"
        prefix = "weekly_" if kind == "weekly" else "monthly_"
        items = db.get(kind, [])

        max_num = max(
            (int(str(i.get("id", "")).replace(prefix, "")) or 0 for i in items),
            default=0,
        )
        new_id = f"{prefix}{max_num + 1}"

        new_entry = {"id": new_id, "title": title, "body": body, "char_count": len(body),
                     "start_date": start_date.replace("-", "."),
                     "end_date": end_date.replace("-", ".")}
        if kind == "weekly":
            new_entry["week"] = max_num + 1
        else:
            new_entry["month"] = max_num + 1
        items.append(new_entry)
        items.sort(key=lambda x: x.get("week" if kind == "weekly" else "month", 0))
        db[kind] = items
        if "meta" in db:
            db["meta"]["weekly_count"] = len(db.get("weekly", []))
            db["meta"]["monthly_count"] = len(db.get("monthly", []))

        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        if os.path.exists(db_path):
            shutil.copy2(db_path, db_path + ".bak")
        logger.info(f"素材库已追加: {new_id}")
