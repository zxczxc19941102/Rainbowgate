# -*- coding: utf-8 -*-
"""Rainbow Gate AI 每日签到 —— 图形界面（Tkinter）

运行方式：
    python checkin_gui.py     或双击 start_gui.bat

界面功能：
    * 自动签到（默认开启）：启动后自动登录，并按北京时间在每天 00:00–12:00 的随机时刻
      执行签到；当天成功后不再重复执行，自动顺延到次日
    * 手动签到：点击“立即签到”立即调用接口（每次会二次确认）
    * 所有过程实时显示在界面并写入 checkin.log
"""
from __future__ import annotations

import datetime as dt
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import checkin_core as core

FONT_FAMILY = "Microsoft YaHei UI"

# 各状态对应的标题与颜色
STATUS_STYLE = {
    core.SUCCESS: ("签到成功", "#16a34a"),
    core.ALREADY: ("今日已签到", "#d97706"),
    core.SKIPPED: ("已跳过（今日已签到）", "#2563eb"),
    core.FAILED: ("签到失败", "#dc2626"),
}


class CheckinApp(tk.Tk):
    """签到工具主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("Rainbow Gate AI 每日签到")
        self.geometry("780x620")
        self.minsize(680, 480)

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._result_queue: "queue.Queue[dict]" = queue.Queue()
        self._auto_result_queue: "queue.Queue[dict]" = queue.Queue()
        self._schedule_queue: "queue.Queue[dt.datetime]" = queue.Queue()
        self.logger = core.build_logger(callback=self._log_queue.put)
        self.scheduler: "core.AutoCheckinScheduler | None" = None

        self._build_style()
        self._build_widgets()
        self._load_config()
        self._show_today_state()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain_queues)
        self.after(600, self._auto_start_if_ready)  # 启动后自动登录并进入自动签到模式

    # ------------------------------------------------------------ 界面构建
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", font=(FONT_FAMILY, 10))
        style.configure("TFrame", background="#f5f6f8")
        style.configure("TLabel", background="#f5f6f8", foreground="#111827")
        style.configure("Title.TLabel", font=(FONT_FAMILY, 15, "bold"), foreground="#1e3a8a")
        style.configure("Hint.TLabel", foreground="#6b7280", font=(FONT_FAMILY, 9))
        style.configure("Status.TLabel", font=(FONT_FAMILY, 14, "bold"))
        style.configure("TLabelframe", background="#f5f6f8", bordercolor="#d1d5db")
        style.configure(
            "TLabelframe.Label",
            background="#f5f6f8",
            foreground="#374151",
            font=(FONT_FAMILY, 10, "bold"),
        )
        style.configure(
            "Primary.TButton",
            font=(FONT_FAMILY, 11, "bold"),
            padding=(18, 10),
            background="#2563eb",
            foreground="white",
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#93b4f5")],
            foreground=[("disabled", "#f3f4f6")],
        )
        style.configure("TButton", padding=(12, 8))
        style.configure("TEntry", fieldbackground="white")

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="Rainbow Gate AI 每日签到", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="每天 00:00–12:00（北京时间）随机时刻自动签到，每天仅可成功一次。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # 账号信息
        account = ttk.Labelframe(root, text=" 账号信息 ", padding=10)
        account.grid(row=1, column=0, sticky="ew")
        account.columnconfigure(1, weight=1)

        ttk.Label(account, text="账号 / 邮箱").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.username_var = tk.StringVar()
        ttk.Entry(account, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(account, text="密码").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.password_var = tk.StringVar()
        ttk.Entry(account, textvariable=self.password_var, show="●").grid(row=1, column=1, sticky="ew", pady=3)

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            account,
            text="记住账号密码（保存到本地 config.json）",
            variable=self.remember_var,
        ).grid(row=2, column=1, sticky="w", pady=(1, 0))

        # 操作按钮
        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky="ew", pady=8)
        self.checkin_btn = ttk.Button(actions, text="立即签到", style="Primary.TButton", command=self._on_checkin)
        self.checkin_btn.pack(side="left")
        ttk.Button(actions, text="打开日志文件", command=self._open_log).pack(side="left", padx=6)
        ttk.Button(actions, text="清空显示", command=self._clear_log_view).pack(side="left")
        ttk.Button(actions, text="退出", command=self._on_close).pack(side="right")

        # 自动签到
        auto_box = ttk.Labelframe(root, text=" 自动签到 ", padding=10)
        auto_box.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            auto_box,
            text="启用自动签到（每天 00:00–12:00 内的随机时刻自动执行）",
            variable=self.auto_var,
            command=self._toggle_auto,
        ).pack(anchor="w")
        self.auto_state_var = tk.StringVar(value="自动签到未启动")
        ttk.Label(auto_box, textvariable=self.auto_state_var, style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        # 签到状态
        status_box = ttk.Labelframe(root, text=" 签到状态 ", padding=10)
        status_box.grid(row=4, column=0, sticky="ew")
        self.status_label = ttk.Label(status_box, text="尚未签到", style="Status.TLabel", foreground="#374151")
        self.status_label.pack(anchor="w")
        self.detail_label = ttk.Label(status_box, text="", style="Hint.TLabel", wraplength=660, justify="left")
        self.detail_label.pack(anchor="w", pady=(4, 0))

        # 运行日志
        log_box = ttk.Labelframe(root, text=" 运行日志（同时写入 checkin.log） ", padding=6)
        log_box.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_box,
            height=14,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            background="#0f172a",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
        )
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 数据加载
    def _load_config(self) -> None:
        config = core.load_config()
        if config.get("username"):
            self.username_var.set(config["username"])
        saved_password = core.get_saved_password(config)
        if saved_password:
            self.password_var.set(saved_password)
        self.remember_var.set(bool(config.get("remember", True)))

    def _show_today_state(self) -> None:
        state = core.load_state()
        if state.get("last_success_date") == core.today_str():
            detail = (
                f"上次结果：{state.get('last_message') or '签到成功'}；"
                f"奖励：{state.get('last_reward') if state.get('last_reward') not in (None, '') else '以服务端为准'}"
            )
            self._set_status(core.ALREADY, "今日已签到（本地记录）", detail)
        else:
            last = state.get("last_success_date") or "无记录"
            self._set_status(None, "今日尚未签到", f"上次成功签到日期：{last}")

    # ------------------------------------------------------------ 自动签到
    def _auto_start_if_ready(self) -> None:
        """启动后：若已勾选自动签到且已保存账号密码，则自动登录并进入自动模式。"""
        if not self.auto_var.get():
            return
        if self.username_var.get().strip() and self.password_var.get():
            self._start_scheduler(auto=True)
        else:
            self.auto_state_var.set("未启动：缺少账号或密码，请填写后勾选“记住账号密码”并点击“立即签到”保存")
            self.logger.warning("自动签到未启动：缺少账号或密码（请先填写账号密码）")

    def _toggle_auto(self) -> None:
        """勾选框切换：启动或停止自动签到。"""
        if self.auto_var.get():
            self._start_scheduler()
        else:
            self._stop_scheduler()

    def _start_scheduler(self, auto: bool = False) -> None:
        """启动自动签到调度线程。"""
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            self.auto_state_var.set("未启动：请先填写账号和密码（并勾选“记住账号密码”）")
            if not auto:
                messagebox.showwarning("提示", "启用自动签到前，请先填写账号和密码。", parent=self)
                self.auto_var.set(False)
            return
        if self.scheduler and self.scheduler.is_running():
            return

        core.save_config(username, password, self.remember_var.get())
        self.scheduler = core.AutoCheckinScheduler(
            username,
            password,
            self.logger,
            on_schedule=self._schedule_queue.put,
            on_result=self._auto_result_queue.put,
        )
        self.scheduler.start()
        self.auto_state_var.set("自动签到运行中：正在计算今日随机时刻…")

    def _stop_scheduler(self) -> None:
        """停止自动签到调度。"""
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
            self.logger.info("自动签到已停止")
        self.auto_state_var.set("自动签到已停止")

    def _refresh_scheduler(self) -> None:
        """手动签到成功后重新规划下次自动签到时间。"""
        if self.scheduler and self.scheduler.is_running():
            self.scheduler.stop()
            self.scheduler = None
            self._start_scheduler(auto=True)

    def _on_close(self) -> None:
        """关闭窗口前先停止调度线程。"""
        if self.scheduler:
            self.scheduler.stop()
        self.destroy()

    # ------------------------------------------------------------ 交互逻辑
    def _on_checkin(self) -> None:
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            messagebox.showwarning("提示", "请先填写账号和密码。", parent=self)
            return

        if core.is_checked_in_today():
            tip = "本地记录显示今日已签到。\n\n仍要请求接口吗？（服务端会返回“已签到”提示）"
        else:
            tip = "每日仅可成功签到一次，确认现在执行签到吗？"
        if not messagebox.askyesno("确认签到", tip, parent=self):
            return

        core.save_config(username, password, self.remember_var.get())
        self.checkin_btn.state(["disabled"])
        self._set_status("running", "正在签到…", "正在登录并调用签到接口，请稍候")
        threading.Thread(target=self._worker, args=(username, password), daemon=True).start()

    def _worker(self, username: str, password: str) -> None:
        """后台线程：执行签到，日志与结果通过队列回传主线程。"""
        self.logger.info("-" * 46)
        self.logger.info("手动触发签到")
        try:
            result = core.run_checkin(username, password, self.logger, force=True)
        except core.CheckinError as exc:
            self.logger.error("%s", exc)
            result = {"status": core.FAILED, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 兜底，保证界面能恢复
            self.logger.error("发生未预期错误：%s", exc)
            result = {"status": core.FAILED, "message": f"发生未预期错误：{exc}"}
        self._result_queue.put(result)

    # ------------------------------------------------------------ 队列轮询
    def _drain_queues(self) -> None:
        try:
            while True:
                self._append_log(self._log_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._on_result(self._result_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                target = self._schedule_queue.get_nowait()
                self.auto_state_var.set(
                    f"自动签到运行中；下次执行：{core.format_beijing_cn(target)}（北京时间）"
                )
        except queue.Empty:
            pass

        try:
            while True:
                self._on_auto_result(self._auto_result_queue.get_nowait())
        except queue.Empty:
            pass

        self.after(120, self._drain_queues)

    def _on_result(self, result: dict) -> None:
        """手动签到的结果：更新状态卡片并弹窗提示。"""
        self.checkin_btn.state(["!disabled"])
        status, message, reward = self._update_status_from_result(result)

        if status in (core.SUCCESS, core.ALREADY):
            self._refresh_scheduler()

        if status == core.SUCCESS:
            messagebox.showinfo(
                "签到成功",
                f"{message}\n\n奖励：{reward if reward not in (None, '') else '以服务端为准'}",
                parent=self,
            )
        elif status == core.FAILED:
            messagebox.showerror("签到失败", message or "请查看日志了解详情", parent=self)

    def _on_auto_result(self, result: dict) -> None:
        """自动签到的结果：只更新状态与提示文本，不弹窗打断。"""
        status, _, _ = self._update_status_from_result(result)
        moment = core.format_beijing(core.beijing_now())
        if status == core.SUCCESS:
            self.auto_state_var.set(f"自动签到成功（{moment}），今日任务完成，等待明日随机时刻")
        elif status == core.ALREADY:
            self.auto_state_var.set(f"今日已签到（{moment}），等待明日随机时刻")
        elif status == core.FAILED:
            self.auto_state_var.set("自动签到失败，稍后将自动重试…")

    def _update_status_from_result(self, result: dict) -> tuple[str, str, object]:
        """把签到结果渲染到状态卡片，返回 (状态, 消息, 奖励)。"""
        status = result.get("status", core.FAILED)
        message = result.get("message", "")
        reward = result.get("reward")

        details = [f"接口返回：{message}"] if message else []
        if reward not in (None, ""):
            details.append(f"本次奖励：{reward}")
        quota = (result.get("profile") or {}).get("quota")
        if isinstance(quota, (int, float)):
            details.append(f"当前余额：${quota / core.QUOTA_PER_DOLLAR:.4f}")

        title, _ = STATUS_STYLE.get(status, ("签到失败", "#dc2626"))
        self._set_status(status, title, "；".join(details))
        return status, message, reward

    def _set_status(self, status, title: str, detail: str = "") -> None:
        if status == "running":
            color = "#2563eb"
        elif status is None:
            color = "#374151"
        else:
            _, color = STATUS_STYLE.get(status, ("", "#dc2626"))
        self.status_label.configure(text=title, foreground=color)
        self.detail_label.configure(text=detail)

    # ------------------------------------------------------------ 日志显示
    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log_view(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_log(self) -> None:
        if not core.LOG_PATH.exists():
            messagebox.showinfo("提示", "日志文件尚未生成，请先执行一次签到。", parent=self)
            return
        try:
            os.startfile(core.LOG_PATH)  # type: ignore[attr-defined]  # Windows 专用
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self)


def main() -> None:
    """启动图形界面。"""
    app = CheckinApp()
    app.mainloop()


if __name__ == "__main__":
    main()
