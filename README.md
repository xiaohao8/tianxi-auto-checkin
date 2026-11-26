# 天禧AI 自动签到 & 任务能量领取

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](requirements.txt)

自动为**联想天禧AI 桌面客户端**完成每日签到、领取任务能量，并把结果弹窗给你看。

**适用对象**：联想天禧AI 桌面客户端（`C:\Program Files\Lenovo\AIAgent\XLSmartUI.exe`，CEF 内核）。
**已验证**：2026-11-24 实跑——签到 +100，三个任务各 +200，总能量 18347 → 19047，退出码 0；
2026-11-26 重写签到判定逻辑后实跑——准确识别「今日格子 11/26 已签到」，报告与结果窗同步显示签到后的真实状态，退出码 0。

全部功能聚合在 **`tianxi_cdp.py` 一个文件里**：签到、任务、端口保障、结果弹窗、桌面快捷方式、计划任务，没有任何外部 .bat/.ps1 依赖。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 每日签到 | 按**日期格子**精确定位今天那一格，点格子内的「签到」按钮，不会误点页面标题 |
| 判定不骗人 | 唯一成功判据 = 今日格子变成「已签到」；能量变了但格子没变 → 报「待确认」，绝不假成功 |
| 抗页面改版 | 路由失效自动兜底 + 文案变体匹配 + 占位页自动「刷新」重试（最多 3 次 / 60 秒） |
| 任务自适应 | 任务列表增减、改名、奖励调整天然免疫；多一步确认弹窗自动点 |
| 结果弹窗 | 签到状态 / 任务 x-y 成功 / 当前总能量，窗口停留可回看 |
| 定时执行 | 一条命令注册 Windows 计划任务，每天静默跑（无黑窗、不弹屏） |
| 自带诊断 | 页面改版时导出按钮清单 JSON，改配置即可校准，不用动代码 |

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `tianxi_cdp.py` | **主程序**，全部功能都在这一个文件里 |
| `config.example.json` | 配置样例，复制为 `config.json` 后按需修改 |
| `requirements.txt` | Python 依赖（`websocket-client` 为主） |
| `MEMORY.md` | 技术笔记：签到页真实 DOM、jsbridge/RSA 签名机制、改版修复记录 |
| `LICENSE` | MIT 协议 |

## 原理（重要）

抓包结论：天禧AI 的「签到中心」**没有可复用的外部网址**——它是客户端内的本地页面
`C:\Program Files\Lenovo\AIAgent\skin\dist\index.html#/SignIn`，所有接口都通过客户端注入的
`window.jsbridge` 原生桥接发出（带网关 token + RSA 签名），纯浏览器打开拿不到任何数据。

因此脚本通过 Chrome DevTools 协议直连客户端：

1. 客户端没带调试端口在跑时，脚本**自动退出并重启客户端**（登录态在磁盘，不丢）；
2. 用原生 CDP WebSocket（websocket-client）找到主界面页面；
3. 导航到 `#/SignIn`，在真实登录态里点「签到」、逐个点任务，等能量到账。

> 注意：Playwright 的 `connect_over_cdp` 在该 CEF 版本上握手失败
> （Browser context management is not supported），所以用的是裸 CDP + `Runtime.evaluate`。

## 一次性准备

```powershell
cd "C:\path\to\tianxiAI"
python -m venv .venv          # 用 python（本机 py 是 3.14 alpha，装不了依赖）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 只需 websocket-client
Copy-Item config.example.json config.json   # 可选：自定义文案/参数时再改
```

> 依赖很轻：`websocket-client`（CDP 通信）即可跑通签到主流程；
> `playwright` 只被旧版纯网页模式用到，`tianxi_cdp.py` 不需要它。

## 桌面快捷方式（推荐入口）

```powershell
.\.venv\Scripts\python.exe tianxi_cdp.py --install-shortcut   # 创建（--remove-shortcut 删除）
```

| 桌面图标 | 双击后 |
| --- | --- |
| **天禧AI自动签到** | 自动检查端口 → 实时滚动每步日志 → 结束弹出结果窗（签到状态 / 任务 x/y 成功 / 当前总能量），窗口停留可回看 |
| **签到结果查看** | 随时弹出最近一次运行结果 |

结果窗状态标签：`[成功]` `[今日已签]` `[待确认]` `[失败]` `[未识别]`；任务区异常会提示「页面可能改版」。

## 命令一览（都收敛在这一个脚本里）

```powershell
$py = ".\.venv\Scripts\python.exe"

& $py tianxi_cdp.py                    # 正式执行：自动保障端口 → 签到 + 全部任务 → 弹结果窗
& $py tianxi_cdp.py --dry-run          # 只识别不点击
& $py tianxi_cdp.py --signin-only      # 只签到不点任务
& $py tianxi_cdp.py --no-gui           # 结束不弹窗（定时任务用）
& $py tianxi_cdp.py --show-result      # 弹窗查看最近一次结果
& $py tianxi_cdp.py --print-result     # 结果打印到控制台
& $py tianxi_cdp.py --inspect          # 页面改版时：导出按钮清单用于校准
& $py tianxi_cdp.py --install-shortcut / --remove-shortcut
& $py tianxi_cdp.py --install-schedule 09:10 / --remove-schedule
& $py tianxi_cdp.py --fix-launcher      # 一次性：桌面/开始菜单/任务栏的天禧AI 启动入口全部带上调试端口，并创建开机自启
```

`--install-schedule` 用 pythonw 静默运行（无黑窗、不弹屏），每天定时执行签到+任务；
想看结果就双击「签到结果查看」。

## 产出

| 位置 | 内容 |
| --- | --- |
| `logs/cdp-YYYYMMDD.log` | 运行日志 |
| `logs/cdp-report-*.json` | 每次结果：签到状态、完整任务列表及完成状况、最终能量 |
| `logs/client-diag-*` | 页面异常/改版时自动导出的按钮清单（JSON 文本） |

默认**不截图**（省空间）；需要现场图时在 config.json 里设 `client.screenshot: true`。

退出码：`0` 全成功 / `1` 有未确认项 / `2` 连不上客户端或页面异常。

## 签到是怎么判定「完成」的（重要）

天禧AI 签到中心的格子结构（`skin/dist/js/SignIn.js`）：

```html
<li class="checkin-day is-today|is-checked|is-past" aria-label="今天，已签到，+100能量">
  <p class="checkin-date">11/26 <strong>周四</strong></p>
  <div class="checkin-reward"><strong>+100</strong></div>
  <button class="checkin-action" aria-label="立即签到">签到</button>  ← 只有「今天且未签」时才有
  <span class="checkin-status">待签到</span>                         ← 已签到 / 待签到 / 未签到
</li>
```

脚本据此工作：

- **定位今日格子**：先按日期文本 `M/D` 严格解析，再退回 `aria-label` 以「今天」开头、最后退回 class `is-today`；
  不会把「昨天已签到」「未来待签到」误当成今天。
- **只点今天那格**：点的是今日格子里的 `<button>签到</button>`，不是页面上任何叫「签到」的文字
  （旧版会点到页面标题 `签到`，看起来点了其实啥也没发生）。
- **以格子状态为准**：点完重新读一次今日格子，只有变成「已签到」才算成功；
  能量变了但格子没变 → 报「待确认」并提示人工核对，绝不假成功。
- **占位页自动恢复**：客户端偶发把签到页渲染成「新活动即将上线，敬请期待 + 刷新」的空页，
  脚本会自动点「刷新」重试最多 3 次（最多等 60 秒）再判定。
- **报告写的是签到之后的状态**：结果窗里的「今日格子：已签到（11/26）」是签到完成后重新读取的实时值，
  不是点击前的旧快照。

## 页面改版怎么办（三层防护）

**第 1 层：自动兜底（内置）** 路由失效自动点侧边栏「签到中心」→ 顶部「签到」；文案支持前后缀变体；异常必导出诊断并返回非零码，绝不静默假成功。

**第 2 层：手动校准（改配置不改代码）** `--inspect` 查看实际按钮文案 → 改 `config.json` 的 `client.keywords`（列出=追加，`-` 前缀=移除默认项）→ `--dry-run` 验证。

**第 3 层：能量对账（最终判据）** 只要点对了按钮「总能量」就会变，报告 `finalEnergy` 与日志前后值直接核对；
能量涨了但今日格子没变成「已签到」，脚本会报「待确认」而不是「成功」。

## 任务变了怎么办

- **列表变化（增减/改名/奖励调整）**：动态识别，天然免疫；
- **多一步确认弹窗**：自动点（`task_confirm`）；
- **需填提示词**：`client.auto_fill_prompt: true` 后自动填并发送（仅 #/chat 页、输入框为空才填）；
- **需人工/花钱**：`client.task_skip: ["充值", "邀请"]` 跳过并标注；
- **全新交互**：12 轮内如实 `unverified` + 导出诊断，不卡死不假成功。

## 常见问题

**Q：双击快捷方式提示连不上 CDP？**
脚本会自动重启客户端带端口；若仍失败，手动退出天禧AI 后重试，或检查是否有权限弹窗（UAC）挡住了重启。

**Q：每次正常启动天禧AI，脚本都要重启一次客户端？**
跑一次 `--fix-launcher`：它会把桌面/开始菜单/任务栏上所有天禧AI 启动入口都加上调试端口，并在「启动」文件夹创建带端口的开机自启。此后正常启动的客户端就自带 CDP，不再需要重启。若开机时被天禧自家服务先拉起（无端口），签到时脚本仍会自动重启一次，全自动无需手动。

**Q：签到显示 already？** 当天已签，正常。

**Q：任务 unverified？** 能量没变化时如实标记，看 `logs/cdp-report-*.json` 和截图人工核对即可。

**Q：Python 3.8 相关？** `py` 启动器指向 3.14 alpha 装不了依赖，务必用 `python -m venv .venv`（3.8.6 已验证）。

## 使用边界与免责声明

- 只用于**你自己账号**的每日打卡，别高频轮询或批量注册。
- 自动签到与领任务奖励**可能不符合联想天禧AI 的用户协议**，是否使用、账号风险自行评估；
  本项目作者不对任何账号封禁或收益损失承担责任。
- `--remote-debugging-port=9222` 开放期间，本机任何进程都能控制客户端页面；别把端口暴露到外网。
- 本项目仅供学习与自用，请遵守联想软件许可协议与相关法律法规。

## License

[MIT](LICENSE) © 2026 Fuzhihao

---

## 附：旧版纯网页模式（tianxi_auto.py）

早期版本基于 Playwright 驱动普通浏览器访问「签到中心网页地址」，适用于**纯网页形态**的签到站点。
**对天禧AI 桌面版无效**——它没有网页版签到地址，签到页只存在于客户端内部。

该文件属于历史遗留，未包含在本仓库中；主程序 `tianxi_cdp.py` 与它互不依赖。

