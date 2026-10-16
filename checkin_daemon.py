# -*- coding: utf-8 -*-
"""Rainbow Gate AI 自动签到守护进程（无界面版本）

运行逻辑：
  1. 启动后读取 config.json，自动登录（优先复用其中缓存的会话，失效则重新登录）
  2. 按北京时间（UTC+8），在每天 00:00–12:00 之间随机取一个时刻执行签到
  3. 当天签到成功后不再重复执行，自动顺延到次日同一随机机制
  4. 失败会自动重试（最多 3 次，每次间隔 15 分钟），仍失败则等待次日
  5. 全过程输出到控制台并写入 checkin.log

运行：python checkin_daemon.py   （Ctrl+C 退出）
或双击 start_daemon.bat
"""
from __future__ import annotations

import logging
import sys
import time

import checkin_core as core


def main() -> int:
    """启动守护进程并阻塞运行，直到收到 Ctrl+C 或调度线程退出。"""
    core.setup_console_utf8()

    logger = core.build_logger()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    config = core.load_config()
    username = config.get("username") or ""
    password = core.get_saved_password(config)

    logger.info("=" * 44)
    logger.info("自动签到守护进程启动（北京时间 %s）", core.format_beijing(core.beijing_now()))

    if not username or not password:
        logger.error("缺少账号或密码：请先运行 start_gui.bat，填写账号密码并勾选“记住账号密码”保存一次")
        return 2

    scheduler = core.AutoCheckinScheduler(username, password, logger)
    scheduler.start()
    try:
        while scheduler.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到退出信号（Ctrl+C）")
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
