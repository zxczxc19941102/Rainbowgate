# Rainbow Gate AI 每日签到工具

自动完成 [rainbowgate.top](https://rainbowgate.top) 的每日签到：**启动即自动登录**，并按**北京时间**在每天 `00:00–12:00` 之间随机取一个时刻执行签到；当天成功后自动顺延到次日。

> 接口参数（登录 / 签到 / 签到统计）均通过浏览器抓包得到，详见 `checkin_core.py` 顶部文档字符串。

---

## 功能特性

| 功能 | 说明 |
|---|---|
| 自动登录 | 使用保存的账号密码登录；优先复用本地缓存会话，失效时自动重新登录 |
| 随机时刻签到 | 每天在 `00:00–12:00`（北京时间）随机取一个时刻执行；**随机时刻只存内存、绝不落盘**，每次启动/每轮结束都重新随机 |
| 每天仅一次 | 当天签到成功后不再重复执行；服务端返回「已签到」也会被识别为完成而非失败 |
| 失败重试 | 签到失败 15 分钟后重试，当天最多 3 次，之后等待次日 |
| 奖励记录 | 已签到时读取**当天获得的奖励金额**与累计收益并写入日志 |
| 三种运行方式 | 图形界面 / 后台守护进程 / 命令行 |
| 网络兜底 | 系统代理不可用时自动降级为直连重试，保证无人值守也能签到 |
| 全程日志 | 界面实时输出，同时写入 `checkin.log` |

---

## 目录结构

```
Rainbowgate/
├── checkin_core.py      # 核心逻辑：接口调用、配置读写、随机调度、日志
├── checkin_gui.py       # 图形界面（Tkinter）
├── checkin_daemon.py    # 后台守护进程（无界面，适合常驻）
├── build_exe.py         # 一键打包 EXE（PyInstaller + UPX）
├── requirements.txt     # 运行依赖
├── config.json          # 运行后生成：账号 / 状态 / 会话（已在 .gitignore 中忽略）
└── checkin.log          # 运行后生成：签到日志（已在 .gitignore 中忽略）
```

---

## 环境要求

- Windows（脚本使用 `chcp 65001` 输出中文；核心逻辑跨平台）
- Python 3.10+（实测 3.12.10）
- 依赖：`requests`

## 安装

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 使用方法

### 1. 图形界面（推荐）

```bat
python checkin_gui.py
```

界面说明：

- **账号信息**：填写账号与密码，勾选「记住账号密码」后会保存到 `config.json`
- **自动签到**：默认勾选，启动即自动登录并进入随机调度；取消勾选可停止
- **立即签到**：手动触发一次签到（会有二次确认，用于测试）
- **签到状态**：显示结果、接口返回、本次奖励、当前余额
- **运行日志**：实时输出全部过程，同时写入 `checkin.log`

### 2. 后台守护进程

```bat
python checkin_daemon.py
```

无界面常驻运行（`Ctrl+C` 退出），适合长期挂机或配合开机自启。

### 3. 命令行

```bat
python checkin_core.py --status          :: 查看状态：北京时间 / 今日是否已签到 / 本次随机计划时刻
python checkin_core.py                   :: 执行一次签到（本地已签到时自动跳过）
python checkin_core.py --force           :: 忽略本地记录，强制请求接口
python checkin_core.py -u 账号 -p 密码    :: 临时指定账号（不写入配置）
```

退出码：`0` 成功或已签到，`1` 签到失败，`2` 账号/密码缺失，`3` 网络异常。

---

## 自动签到规则

| 情况 | 行为 |
|---|---|
| 程序启动 | 立即自动登录（复用缓存会话，失效则重新登录），随后计算随机时刻 |
| 当天未签到 | 在 `[当前时间, 12:00]` 内（若已过 12:00 则立即）随机取一个时刻，等待到点执行 |
| 随机时刻已过 | 立即执行，不浪费当天机会 |
| 当天已签到 | 不再执行，自动顺延到次日 `00:00–12:00` 的随机时刻 |
| 签到失败 | 15 分钟后重试，当天最多 3 次，仍失败则等待次日 |
| 计划时刻存储 | **只保存在内存中**（用于显示与等待），不写任何文件；重启即重新随机 |

---

## 配置文件 `config.json`

程序会把所有数据整合在这一个文件里（旧版分散的 `state.json` / `session.json` 会在首次运行时自动合并进来并删除）：

```json
{
  "account": {
    "username": "your-account@example.com",
    "password": "<base64 编码后的密码>",
    "remember": true
  },
  "state": {
    "last_success_date": "2026-01-01",
    "last_message": "今日已签到",
    "last_reward": null
  },
  "session": {
    "cookies": { "session": "<登录会话 Cookie>" },
    "user_id": 1000,
    "saved_at": "2026-01-01 08:30:00"
  }
}
```

> `password` 使用 base64 保存，仅避免明文一眼可见，**并非安全加密**；`session` 中含登录凭据，
> 请勿把 `config.json` 提交到版本库（已在 `.gitignore` 中排除）。

---

## 打包 EXE（含 UPX 压缩）

```bat
.venv\Scripts\python.exe build_exe.py            :: 默认启用 UPX
.venv\Scripts\python.exe build_exe.py --noupx    :: 不使用 UPX
```

产物（`dist/`）：

| 文件 | 大小 | 说明 |
|---|---|---|
| `RainbowGateCheckin.exe` | ≈11.4 MB | 图形界面版（无控制台窗口） |
| `RainbowGateDaemon.exe` | ≈8.8 MB | 后台守护进程（控制台版） |
| `RainbowGateCli.exe` | ≈8.8 MB | 命令行版（`--status` / `--force`） |

说明：

- 需先 `pip install pyinstaller`，并保证 `upx` 在 PATH 中（`build_exe.py` 会自动探测 `upx --version`）
- UPX 用于压缩打包进 EXE 的 DLL / pyd（`upx --lzma`），相比不压缩体积减少约 13%
- 外层 EXE 无法再压缩：PyInstaller 引导程序带 CFG 保护，UPX 会报 `GUARD_CF`（属预期，脚本会提示并跳过）
- EXE 的 `config.json` / `checkin.log` 位于 **EXE 所在目录**，可与源码目录独立共存
- 若某个 EXE 正在运行，该目标会被自动跳过（Windows 不允许覆盖运行中的文件），关闭后重新打包即可

---

## 逆向接口说明

| 用途 | 请求 | 鉴权 |
|---|---|---|
| 登录 | `POST /api/user/login?turnstile=`，JSON `{"username","password"}` | 响应下发 `session` Cookie |
| 签到 | `POST /api/user/checkin`，无请求体 | Cookie + 请求头 `New-Api-User: <用户ID>` |
| 签到统计 | `GET /api/user/checkin`，返回 `stats.records[].quota_awarded` | 同上 |

额度换算：`500000 额度 = $1`（与站点页面展示一致）。

---

## 更新说明

### v1.0.0 — 2026-10-16

- 初版发布：抓包定位登录 / 签到接口，实现 CLI + Tkinter GUI 签到工具
- 新增自动模式：按北京时间在 `00:00–12:00` 随机时刻签到，当天一次、成功后顺延次日、失败自动重试
- 随机计划时刻改为**仅存内存**（显示用），不再写入状态文件，保证每次运行都是全新随机
- 新增签到奖励读取：已签到时输出当天奖励金额与累计收益
- 支持复用本地会话，省去重复登录
- 配置整合：`state.json` / `session.json` 合并进单一 `config.json`，旧文件自动迁移删除
- 网络健壮性：系统代理不可用时自动切换直连重试
- 发布打包脚本 `build_exe.py`（PyInstaller + UPX），产出三个 EXE
- 界面优化：窗口尺寸精简为 780×620，日志区自适应

---

## 免责声明

本项目仅供个人学习与自用（管理自己的账号签到），请遵守目标站点的服务条款。账号密码仅保存在本机 `config.json`，请自行妥善保管。
