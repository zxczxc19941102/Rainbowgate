# -*- coding: utf-8 -*-
"""Rainbow Gate AI 每日签到 —— 核心逻辑（无 GUI 依赖，可被 GUI / 计划任务调用）

接口来自浏览器抓包（域名 https://rainbowgate.top）：

    登录  POST /api/user/login?turnstile=
          请求体: {"username": "<账号>", "password": "<密码>"}
          成功: HTTP 200，响应头 Set-Cookie: session=...；响应 data 含 id/username/group
    签到  POST /api/user/checkin
          无请求体；鉴权 = Cookie: session + 请求头 New-Api-User: <用户ID>
          每日仅可成功一次，重复请求服务端返回失败提示

命令行用法：
    python checkin_core.py                 # 读取 config.json 执行一次签到
    python checkin_core.py --force         # 忽略本地“今日已签到”记录，强制请求接口
    python checkin_core.py -u 账号 -p 密码  # 临时指定账号（不写入配置）
    python checkin_core.py --status        # 只查看状态（北京时间 / 今日是否已签到 / 随机计划时间）

签到统计：GET /api/user/checkin 可读取签到记录（含当天获得的奖励额度 quota_awarded），
今日已签到时会把当天奖励金额一并输出到日志。

自动模式（AutoCheckinScheduler）：按北京时间（UTC+8，与系统时区无关），
在每天 00:00–12:00 之间随机取一个时刻执行签到；当天签到成功后不再重复执行，
自动顺延到次日。图形界面见 checkin_gui.py，无界面守护进程见 checkin_daemon.py。

注意：随机计划时刻**只存在于内存中用于显示和等待**，不会写入任何文件；
每次程序启动、每完成一轮签到都会重新随机，行为完全随机。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

# ---------------------------------------------------------------- 常量配置
BASE_URL = "https://rainbowgate.top"
LOGIN_URL = f"{BASE_URL}/api/user/login"
CHECKIN_URL = f"{BASE_URL}/api/user/checkin"
SELF_URL = f"{BASE_URL}/api/user/self"

def _resolve_base_dir() -> Path:
    """确定配置/日志所在目录。

    打包成单文件 EXE 后，`__file__` 指向临时解包目录（会被清理），
    因此此时改用 EXE 自身所在目录，保证 config.json / checkin.log 持久保存。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _resolve_base_dir()
CONFIG_PATH = BASE_DIR / "config.json"   # 唯一配置文件：account / state / session 三段
LOG_PATH = BASE_DIR / "checkin.log"
# 旧版遗留文件：首次读取配置时会自动合并进 config.json 并删除
LEGACY_STATE_PATH = BASE_DIR / "state.json"
LEGACY_SESSION_PATH = BASE_DIR / "session.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30
QUOTA_PER_DOLLAR = 500_000  # NewAPI 计费单位换算：500000 额度 = $1

# 北京时间与自动签到窗口
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
WINDOW_START = dt.time(0, 0)      # 每日随机签到窗口起点（北京时间）
WINDOW_END = dt.time(12, 0)       # 每日随机签到窗口终点（北京时间）

# 失败重试策略
MAX_RETRY_PER_DAY = 3             # 当天最多重试次数（不含首次）
RETRY_DELAY_MINUTES = 15          # 失败后等待多久重试

LOGGER_NAME = "rainbowgate.checkin"

# 签到结果状态
SUCCESS = "success"   # 本次签到成功
ALREADY = "already"   # 服务端提示今日已签到
SKIPPED = "skipped"   # 本地记录显示今日已签到，未请求接口
FAILED = "failed"     # 签到失败


class CheckinError(Exception):
    """登录失败、账号密码为空等无法继续的情况。"""


# ---------------------------------------------------------------- 基础工具
def setup_console_utf8() -> None:
    """把 Windows 控制台切换为 UTF-8，保证中文日志不乱码（仅控制台程序需要调用）。"""
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 > nul")
        except Exception:  # noqa: BLE001
            pass
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def beijing_now() -> dt.datetime:
    """当前北京时间（UTC+8，与操作系统时区无关）。"""
    return dt.datetime.now(BEIJING_TZ)


def beijing_today_str() -> str:
    """北京时间的今天，格式 YYYY-MM-DD。"""
    return beijing_now().date().isoformat()


def today_str() -> str:
    """返回“今天”的日期（以北京时间为准）。"""
    return beijing_today_str()


def format_beijing(moment: dt.datetime) -> str:
    """把时间格式化为可读的北京时间字符串（2026-10-16 16:35:12）。"""
    return moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def format_beijing_cn(moment: dt.datetime) -> str:
    """把时间格式化为中文样式的北京时间（2026年10月16日 16:35:12）。"""
    local = moment.astimezone(BEIJING_TZ)
    return f"{local.year}年{local.month}月{local.day}日 {local.strftime('%H:%M:%S')}"


def humanize_seconds(seconds: float) -> str:
    """把秒数转成“x 小时 y 分”这类易读文本。"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def build_session() -> requests.Session:
    """构造带浏览器常用请求头的会话。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/sign-in",
            "Cache-Control": "no-store",
        }
    )
    return session


def _parse_json(resp: requests.Response) -> dict:
    """把响应体解析为 dict，兼容非 JSON 响应。"""
    try:
        data = resp.json()
    except ValueError:
        return {"success": False, "message": (resp.text or "")[:300]}
    if isinstance(data, dict):
        return data
    return {"success": True, "data": data}


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """统一发起请求：若系统代理不可用，自动切换为直连重试一次。

    某些环境里系统代理会拦截/破坏到站点的 TLS 连接（SSL EOF、连接重置），
    这里做一次无感兜底，避免无人值守的自动签到因此失败。
    """
    try:
        return session.request(method, url, **kwargs)
    except (
        requests.exceptions.ProxyError,
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
    ) as exc:
        if session.trust_env is False:
            raise
        logging.getLogger(LOGGER_NAME).warning("系统代理不可用（%s），已自动切换为直连重试", type(exc).__name__)
        session.trust_env = False
        return session.request(method, url, **kwargs)


# ---------------------------------------------------------------- 配置（单一 config.json）
def _read_store() -> dict:
    """读取唯一的 config.json 全文。

    结构：
        {
          "account": {"username": "...", "password": "<base64>", "remember": true},
          "state":   {"last_success_date": "...", "last_message": "...", ...},
          "session": {"cookies": {...}, "user_id": 1000, "saved_at": "..."}
        }

    首次运行时会把旧版分散的 state.json / session.json 合并进来并删除旧文件。
    """
    store: dict = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                store = loaded
        except (OSError, ValueError):
            store = {}

    migrated = False

    # 迁移旧版：账号字段原本直接放在顶层，统一收进 account 段
    if not store.get("account") and any(k in store for k in ("username", "password", "remember")):
        account: dict = {
            "username": store.pop("username", ""),
            "remember": store.pop("remember", True),
        }
        password = store.pop("password", None)
        if password:
            account["password"] = password
        store["account"] = account
        migrated = True

    for key, legacy_path in (("state", LEGACY_STATE_PATH), ("session", LEGACY_SESSION_PATH)):
        if not legacy_path.exists():
            continue
        if not store.get(key):
            try:
                store[key] = json.loads(legacy_path.read_text(encoding="utf-8"))
                migrated = True
            except (OSError, ValueError):
                pass
        # config.json 已成为唯一数据源，旧文件无论是否被用到都删除，避免残留
        try:
            legacy_path.unlink(missing_ok=True)
        except OSError:
            pass

    if migrated:
        _write_store(store)
    return store


def _write_store(store: dict) -> None:
    """把整个配置写回 config.json（写入失败静默忽略，不影响主流程）。"""
    try:
        CONFIG_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_config() -> dict:
    """读取账号配置（config.json 的 account 段）。"""
    account = _read_store().get("account")
    return account if isinstance(account, dict) else {}


def save_config(username: str, password: str, remember: bool) -> None:
    """保存账号配置到 config.json。密码 base64 仅避免明文一眼可见，并非安全加密。"""
    store = _read_store()
    account: dict[str, Any] = {"username": username, "remember": bool(remember)}
    if remember and password:
        account["password"] = base64.b64encode(password.encode("utf-8")).decode("ascii")
    store["account"] = account
    _write_store(store)


def get_saved_password(config: dict) -> str:
    """从配置中还原密码，失败返回空串。"""
    raw = config.get("password") or ""
    if not raw:
        return ""
    try:
        return base64.b64decode(raw.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def load_state() -> dict:
    """读取运行状态（config.json 的 state 段：上次签到日期 / 消息 / 奖励等）。"""
    state = _read_store().get("state")
    return state if isinstance(state, dict) else {}


def save_state(**fields: Any) -> None:
    """更新 config.json 的 state 段。"""
    store = _read_store()
    state = store.get("state")
    if not isinstance(state, dict):
        state = {}
    state.update(fields)
    store["state"] = state
    _write_store(store)


def is_checked_in_today() -> bool:
    """本地记录是否显示今日已签到。"""
    return load_state().get("last_success_date") == today_str()


# ---------------------------------------------------------------- 日志
class CallbackHandler(logging.Handler):
    """把日志推送给 GUI 回调（例如放入队列刷新界面）。"""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._callback(self.format(record))
        except Exception:  # noqa: BLE001 - 日志回调不允许影响主流程
            pass


def build_logger(callback: Optional[Callable[[str], None]] = None) -> logging.Logger:
    """构造写入 checkin.log 的 logger，可选把日志同时转发给回调。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(file_handler)

    for handler in list(logger.handlers):
        if isinstance(handler, CallbackHandler):
            logger.removeHandler(handler)
    if callback is not None:
        logger.addHandler(CallbackHandler(callback))
    return logger


# ---------------------------------------------------------------- 接口调用
def login(session: requests.Session, username: str, password: str, logger: logging.Logger) -> dict:
    """登录并返回服务端 data（含 id / username / group 等）。"""
    resp = _request(
        session,
        "POST",
        LOGIN_URL,
        params={"turnstile": ""},
        json={"username": username, "password": password},
        timeout=TIMEOUT,
    )
    payload = _parse_json(resp)
    if not payload.get("success"):
        message = payload.get("message") or f"HTTP {resp.status_code}"
        raise CheckinError(f"登录失败：{message}")

    info = payload.get("data") or {}
    logger.info(
        "登录成功：%s（用户 ID %s，分组 %s）",
        info.get("username") or username,
        info.get("id", "?"),
        info.get("group", "?"),
    )
    return info


def checkin(session: requests.Session, user_id: Any, logger: logging.Logger) -> tuple[str, str, Any]:
    """调用签到接口，返回 (状态, 提示信息, 奖励数据)。"""
    headers = {"New-Api-User": str(user_id)} if user_id else {}
    resp = _request(session, "POST", CHECKIN_URL, headers=headers, timeout=TIMEOUT)
    payload = _parse_json(resp)

    message = str(payload.get("message") or "").strip()
    data = payload.get("data")

    if payload.get("success"):
        return SUCCESS, message or "签到成功", data

    lowered = message.lower()
    if any(k in message for k in ("已签到", "已经签到", "重复签到")) or "already" in lowered:
        return ALREADY, message or "今日已签到", data
    return FAILED, message or f"签到失败（HTTP {resp.status_code}）", data


def fetch_self(session: requests.Session, user_id: Any) -> dict:
    """读取账户信息（余额等），失败返回空字典，不影响签到流程。"""
    try:
        resp = _request(session, "GET", SELF_URL, headers={"New-Api-User": str(user_id)}, timeout=TIMEOUT)
        payload = _parse_json(resp)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    except requests.RequestException:
        return {}


def fetch_checkin_stats(session: requests.Session, user_id: Any) -> dict:
    """读取签到统计（GET /api/user/checkin），失败返回空字典。

    返回结构示例：
        {"enabled": true, "min_quota": 20000, "max_quota": 100000,
         "stats": {"checked_in_today": true, "checkin_count": 5, "total_checkins": 8,
                   "total_quota": 487611,
                   "records": [{"checkin_date": "2026-10-16", "quota_awarded": 70862}, ...]}}
    """
    try:
        resp = _request(
            session,
            "GET",
            CHECKIN_URL,
            headers={"New-Api-User": str(user_id)} if user_id else {},
            timeout=TIMEOUT,
        )
        payload = _parse_json(resp)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    except requests.RequestException:
        return {}


def today_reward_quota(stats: dict, today: Optional[str] = None) -> Optional[float]:
    """从签到统计中取出“今天获得的额度”，没有当天记录时返回 None。"""
    today = today or today_str()
    records = ((stats or {}).get("stats") or {}).get("records") or []
    for item in records:
        if str(item.get("checkin_date")) == today:
            value = item.get("quota_awarded")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def quota_to_dollars(quota: Any) -> Optional[float]:
    """把额度换算成美元（按 500000 额度 = $1）。"""
    if isinstance(quota, (int, float)):
        return float(quota) / QUOTA_PER_DOLLAR
    return None


# ---------------------------------------------------------------- 会话缓存
def save_session(session: requests.Session, user_id: Any) -> None:
    """把会话 Cookie 缓存到 config.json 的 session 段，供后续运行复用。"""
    if not user_id:
        return
    store = _read_store()
    store["session"] = {
        "cookies": requests.utils.dict_from_cookiejar(session.cookies),
        "user_id": user_id,
        "saved_at": format_beijing(beijing_now()),
    }
    _write_store(store)


def clear_session() -> None:
    """清除 config.json 中缓存的会话。"""
    store = _read_store()
    store.pop("session", None)
    _write_store(store)


def has_cached_session() -> bool:
    """本地是否保存了可用的会话缓存。"""
    session_data = _read_store().get("session")
    return bool(isinstance(session_data, dict) and session_data.get("cookies"))


def load_cached_session(logger: logging.Logger) -> Optional[tuple[requests.Session, dict]]:
    """尝试复用本地缓存会话；有效则返回 (session, 用户信息)，否则返回 None。"""
    cached = _read_store().get("session")
    if not isinstance(cached, dict):
        return None

    cookies = cached.get("cookies") or {}
    user_id = cached.get("user_id")
    if not cookies or not user_id:
        return None

    session = build_session()
    session.cookies.update(cookies)
    profile = fetch_self(session, user_id)
    if not profile:
        return None

    logger.info("复用本地会话成功（用户 ID %s，缓存时间 %s）", user_id, cached.get("saved_at", "未知"))
    return session, {"id": user_id, "username": profile.get("username", "")}


def ensure_login(
    username: str,
    password: str,
    logger: logging.Logger,
    prefer_cache: bool = True,
) -> tuple[requests.Session, dict]:
    """确保已登录：优先复用本地会话，失败或未登录则用账号密码登录。"""
    if prefer_cache:
        cached = load_cached_session(logger)
        if cached:
            return cached
        logger.info("本地会话不存在或已失效，执行登录…")

    session = build_session()
    info = login(session, username, password, logger)
    save_session(session, info.get("id"))
    return session, info


def _looks_like_auth_error(message: str) -> bool:
    """判断服务端提示是否属于“未登录/会话失效”类错误。"""
    text = (message or "").lower()
    return any(key in text for key in ("unauthorized", "未登录", "请先登录", "token"))


# ---------------------------------------------------------------- 主流程
def run_checkin(
    username: str,
    password: str,
    logger: logging.Logger,
    force: bool = False,
    prefer_cached_session: bool = True,
) -> dict:
    """执行一次完整签到，返回结果字典。

    force=False 时，若本地 config.json 的 state 记录今日已签到则直接跳过，不再请求接口。
    会话优先复用 config.json 中缓存的会话，失效时自动重新登录。
    """
    today = today_str()
    state = load_state()

    if not force and state.get("last_success_date") == today:
        message = f"本地记录显示今日（{today}）已签到，无需重复操作"
        logger.info("跳过：%s", message)
        return {"status": SKIPPED, "message": message, "reward": state.get("last_reward")}

    if not username or not password:
        raise CheckinError("账号或密码为空，请先填写")

    session, info = ensure_login(username, password, logger, prefer_cache=prefer_cached_session)
    user_id = info.get("id")

    status, message, data = checkin(session, user_id, logger)
    if status == FAILED and _looks_like_auth_error(message):
        logger.warning("会话可能已失效，清除缓存后重新登录并重试一次")
        clear_session()
        session, info = ensure_login(username, password, logger, prefer_cache=False)
        user_id = info.get("id")
        status, message, data = checkin(session, user_id, logger)
    reward = data if isinstance(data, (int, float, str)) else None
    previous_reward = state.get("last_reward")

    if status == SUCCESS:
        logger.info("签到成功：%s", message)
        if reward not in (None, ""):
            logger.info("本次奖励：%s", reward)
        save_state(last_success_date=today, last_message=message, last_reward=reward)
    elif status == ALREADY:
        logger.warning("今日已签到：%s", message)
        save_state(last_success_date=today, last_message=message, last_reward=previous_reward)
    else:
        logger.error("签到失败：%s", message)

    profile = fetch_self(session, user_id) if status in (SUCCESS, ALREADY) else {}
    quota = profile.get("quota")
    if isinstance(quota, (int, float)):
        logger.info("当前余额：$%.4f", quota / QUOTA_PER_DOLLAR)

    # 今天已签到（含本次刚签到成功）时，读取当天实际获得的奖励金额
    day_reward_dollars = None
    if status in (SUCCESS, ALREADY):
        stats = fetch_checkin_stats(session, user_id)
        reward_quota = today_reward_quota(stats)
        if reward_quota is None:
            logger.info("未读取到今日签到奖励记录（接口未返回今天的记录）")
        else:
            day_reward_dollars = quota_to_dollars(reward_quota)
            label = "今日已签到奖励" if status == ALREADY else "本次签到奖励"
            logger.info("%s：$%.4f（%d 额度）", label, day_reward_dollars, int(reward_quota))

        stats_detail = (stats or {}).get("stats") or {}
        total_quota = stats_detail.get("total_quota")
        total_times = stats_detail.get("total_checkins")
        if isinstance(total_quota, (int, float)) and total_times:
            logger.info(
                "累计签到 %s 次，累计获得：$%.4f",
                total_times,
                quota_to_dollars(total_quota) or 0.0,
            )

    return {
        "status": status,
        "message": message,
        "reward": reward,
        "day_reward": day_reward_dollars,
        "user": info,
        "profile": profile,
    }


# ---------------------------------------------------------------- 自动签到调度
def random_time_in_window(target_date: dt.date, now: Optional[dt.datetime] = None) -> dt.datetime:
    """在目标日期 00:00–12:00（北京时间）内随机取一个不早于 now 的时刻。

    - 若 now 已晚于窗口终点（12:00），返回 now，表示“立即执行”。
    - 否则在 [max(now, 00:00), 12:00] 区间内均匀随机。
    """
    now = now or beijing_now()
    window_start = dt.datetime.combine(target_date, WINDOW_START, tzinfo=BEIJING_TZ)
    window_end = dt.datetime.combine(target_date, WINDOW_END, tzinfo=BEIJING_TZ)
    start = max(window_start, now)
    if start >= window_end:
        return now
    span = (window_end - start).total_seconds()
    planned = start + dt.timedelta(seconds=random.uniform(0, span))
    return planned.replace(microsecond=0)


def next_run_time(
    now: Optional[dt.datetime] = None,
    planned: Optional[dt.datetime] = None,
) -> dt.datetime:
    """计算下一次自动签到的北京时间（**不写入任何文件，纯随机**）。

    规则：
    - 今天已签到（last_success_date）或已放弃（skip_date）→ 顺延到明天的窗口内随机时刻
    - 今天未签到 → 今天的窗口内随机时刻；若已过 12:00 则返回 now（立即执行）
    - 传入 planned（内存中已掷出的计划）时优先沿用，避免同一轮运行内反复重掷

    :param now: 参照时间，默认当前北京时间
    :param planned: 内存中的计划时刻（仅用于本轮运行，不落盘）
    """
    now = now or beijing_now()
    state = load_state()
    today = now.date()
    done_today = state.get("last_success_date") == today.isoformat()
    given_up_today = state.get("skip_date") == today.isoformat()
    target_date = today + dt.timedelta(days=1) if (done_today or given_up_today) else today

    if planned is not None and planned.date() == target_date and planned > now:
        return planned

    rolled = random_time_in_window(target_date, now)
    return rolled if rolled > now else now


class AutoCheckinScheduler:
    """自动签到调度器：按北京时间在每日 00:00–12:00 的随机时刻执行签到。

    计划时刻只存在于内存中用于显示与等待，**不会写入任何文件**：
    每次程序启动、每完成一轮签到，都会重新随机，行为完全随机。

    对外回调：
        on_schedule(datetime) —— 通知界面“下次执行时间”
        on_result(dict)       —— 通知界面签到结果
    """

    def __init__(
        self,
        username: str,
        password: str,
        logger: logging.Logger,
        on_schedule: Optional[Callable[[dt.datetime], None]] = None,
        on_result: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.username = username
        self.password = password
        self.logger = logger
        self.on_schedule = on_schedule
        self.on_result = on_result
        self._plan: Optional[dt.datetime] = None  # 仅内存中的计划时刻，不持久化
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        """在后台线程启动调度（已在运行则忽略）。"""
        if self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run_forever, name="auto-checkin", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """请求停止并等待线程退出。"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """调度线程是否仍在运行。"""
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    # ---------------------------------------------------------- 主循环
    def run_forever(self) -> None:
        """调度主循环：等待随机时刻 → 执行签到 → 成功后顺延到次日。"""
        logger = self.logger
        logger.info("自动签到调度已启动（当前北京时间：%s）", format_beijing(beijing_now()))

        # 启动后先自动登录一次：验证账号密码并缓存会话，失败也不退出，到点仍会重试
        try:
            _, info = ensure_login(self.username, self.password, logger)
            logger.info("自动登录完成：用户 ID %s", info.get("id", "?"))
        except CheckinError as exc:
            logger.error("自动登录失败：%s（将在计划时刻重试）", exc)
        except requests.RequestException as exc:
            logger.error("自动登录网络异常：%s（将在计划时刻重试）", exc)

        retry_date: Optional[dt.date] = None
        retry_count = 0

        while not self._stop_event.is_set():
            now = beijing_now()
            today = now.date()
            if retry_date != today:
                retry_date, retry_count = today, 0

            # 1) 内存中没有计划就现掷一个随机时刻（只用于显示与等待，不写入文件）
            if self._plan is None:
                try:
                    self._plan = self._roll(now)
                except Exception as exc:  # noqa: BLE001 - 调度不允许因异常退出
                    logger.error("计算下次执行时间失败：%s", exc)
                    if not self._sleep(60):
                        break
                    continue
                if self.on_schedule:
                    self.on_schedule(self._plan)
                logger.info(
                    "下次自动签到：%s（北京时间），还有 %s",
                    format_beijing(self._plan),
                    humanize_seconds((self._plan - now).total_seconds()),
                )

            # 2) 等待到点
            wait_seconds = (self._plan - now).total_seconds()
            if wait_seconds > 0.5:
                if not self._sleep(wait_seconds):
                    break
                continue

            # 3) 到点执行；执行后清空计划，让下一轮重新随机
            logger.info("到达计划时刻（%s），开始执行签到", format_beijing(now))
            self._plan = None
            result = self._execute_once()
            if self.on_result:
                self.on_result(result)

            status = result.get("status")
            if status in (SUCCESS, ALREADY, SKIPPED):
                continue  # 已完成，下一轮自动顺延到明天的随机时刻

            if status == FAILED:
                retry_count += 1
                if retry_count > MAX_RETRY_PER_DAY:
                    logger.error("今日自动签到已失败 %s 次，放弃并等待明日", retry_count - 1)
                    save_state(skip_date=today.isoformat())
                    continue
                logger.warning(
                    "签到失败，%s 分钟后重试（第 %s/%s 次）",
                    RETRY_DELAY_MINUTES,
                    retry_count,
                    MAX_RETRY_PER_DAY,
                )
                if not self._sleep(RETRY_DELAY_MINUTES * 60):
                    break
                continue

            if not self._sleep(60):
                break

        logger.info("自动签到调度已停止")

    # ---------------------------------------------------------- 内部方法
    def _roll(self, now: dt.datetime) -> dt.datetime:
        """随机生成下一次执行时刻（北京时间当天/次日的 00:00–12:00 窗口内，仅存内存）。"""
        return next_run_time(now)

    def _execute_once(self) -> dict:
        """执行一次签到并兜底所有异常，保证调度线程不会中断。"""
        logger = self.logger
        logger.info("-" * 46)

        try:
            return run_checkin(self.username, self.password, logger)
        except CheckinError as exc:
            logger.error("%s", exc)
            return {"status": FAILED, "message": str(exc)}
        except requests.RequestException as exc:
            logger.error("网络请求异常：%s", exc)
            return {"status": FAILED, "message": f"网络请求异常：{exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.error("发生未预期错误：%s", exc)
            return {"status": FAILED, "message": f"发生未预期错误：{exc}"}

    def _sleep(self, seconds: float) -> bool:
        """可被 stop() 打断的睡眠；返回 False 表示收到停止信号。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self._stop_event.wait(min(remaining, 5.0))
        return False


def print_status(logger: logging.Logger) -> None:
    """输出当前状态：北京时间、今日签到情况、下次自动签到时间（随机、不记忆）。"""
    now = beijing_now()
    state = load_state()
    logger.info("当前北京时间：%s（本机时区：%s）", format_beijing(now), dt.datetime.now().astimezone().tzname())
    logger.info("自动签到窗口：每日 00:00–12:00（北京时间）内随机时刻（每次运行重新随机，不记忆）")
    logger.info("今日是否已签到：%s", "是" if state.get("last_success_date") == now.date().isoformat() else "否")
    logger.info("上次签到：%s（%s）", state.get("last_success_date") or "无记录", state.get("last_message") or "-")
    logger.info("会话缓存：%s", "有" if has_cached_session() else "无")
    logger.info("本次随机计划时间：%s（北京时间）", format_beijing(next_run_time(now)))


# ---------------------------------------------------------------- 命令行入口
def main(argv: Optional[list[str]] = None) -> int:
    """命令行模式：适合 Windows 计划任务定时调用。"""
    setup_console_utf8()

    parser = argparse.ArgumentParser(description="Rainbow Gate AI 每日签到（命令行模式）")
    parser.add_argument("-u", "--username", help="账号（默认读取 config.json）")
    parser.add_argument("-p", "--password", help="密码（默认读取 config.json）")
    parser.add_argument("--force", action="store_true", help="忽略本地“今日已签到”记录，强制请求接口")
    parser.add_argument("--status", action="store_true", help="只显示状态，不执行签到")
    parser.add_argument("-q", "--quiet", action="store_true", help="不在控制台输出日志")
    args = parser.parse_args(argv)

    config = load_config()
    username = args.username or config.get("username") or ""
    password = args.password or get_saved_password(config)

    logger = build_logger()
    if not args.quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        logger.addHandler(console)

    if args.status:
        print_status(logger)
        return 0

    logger.info("=" * 44)
    logger.info("开始执行签到任务（账号：%s）", username or "未配置")

    try:
        result = run_checkin(username, password, logger, force=args.force)
    except CheckinError as exc:
        logger.error("%s", exc)
        return 2
    except requests.RequestException as exc:
        logger.error("网络请求异常：%s", exc)
        return 3

    if result["status"] == SUCCESS:
        print(f"[OK] 签到成功：{result['message']}")
    elif result["status"] in (ALREADY, SKIPPED):
        print(f"[SKIP] {result['message']}")
    else:
        print(f"[FAIL] {result['message']}")
    return 0 if result["status"] in (SUCCESS, ALREADY, SKIPPED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
