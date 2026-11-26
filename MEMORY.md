# Project memory

## 天禧AI（联想 AIAgent 客户端）自动化要点

- 客户端：`C:\Program Files\Lenovo\AIAgent\XLSmartUI.exe`（CEF，Chrome/124）；签到中心是本地页面
  `skin/dist/index.html#/SignIn`（组件 `SignIn.js`，侧边栏 `LayoutEnergyCenter.js`），无外部网址可复用。
- 所有接口走客户端注入的 `window.jsbridge`（网关 token + RSA 签名）→ 纯浏览器/纯 HTTP 方案不可行，
  必须直连客户端。Playwright `connect_over_cdp` 在该 CEF 上握手失败（Browser context management is not supported），
  用裸 CDP + `websocket-client`（`suppress_origin=True`）可行。
- 启动参数：`XLSmartUI.exe --xlsource=taskbar --remote-debugging-port=9222`；不带端口启动的第二实例会被单实例守卫吞掉。
- 签到页交互（已实测）：顶部/格子里的叶子文本「签到」= 签到按钮；任务两段式「去完成」→（跳转 AI 对话页自动生成，
  需 `history.back()` 返回）→「领能量」→「已完成」；奖励通常自动到账，「领能量」只是 UI 确认。
- 主脚本 `tianxi_cdp.py`（CDP 模式），`run_cdp.bat`/`ensure_cdp.ps1` 负责端口保障，`setup_schedule.ps1` 注册计划任务。
- 单文件一体化：所有辅助脚本（run_cdp/run_visible/view_result/show_result/ensure_cdp/
  create_desktop_shortcut/setup_schedule 的 .bat/.ps1）已聚合进 `tianxi_cdp.py`，
  对应命令行：--install-shortcut / --install-schedule / --show-result / --print-result / --no-gui / --pause。
  桌面快捷方式指向 python.exe，参数必须含脚本路径+选项（曾因漏掉脚本路径导致 python.exe 收到裸 --pause 而闪退）；
  `__main__` 有崩溃兑底：异常写 logs/crash-*.txt，--pause 时窗口必停留。
- 页面改版防护三层：路由/侧边栏/顶部按钮自动兑底 + 文案变体匹配 + 异常自动导出 client-diag 诊断；
  文案可在 config.json `client.keywords` 配置（直接列出=追加，`-` 前缀=移除默认），`--inspect` 导出按钮清单。
- 任务变化自适应：列表/名称/奖励每次运行动态识别（不写死清单）；确认弹窗自动点（task_confirm）；
  可选自动填提示词（client.auto_fill_prompt，默认关，仅 #/chat 页且输入框为空才填）；task_skip 跳过名单；
  未知新流程 12 轮内 unverified + 诊断导出，不假成功。
- 用户偏好：默认不截图省空间（client.screenshot=false，诊断只导文本 JSON）；已有截图已清理。
- 跨天已签判断：必须按今天日期定位格子（卡片含 mm/dd）才能判已签，否则昨天的「已签到」格子会误判（2026-11-25 修复）。
- 签到格子真实 DOM（反编译 skin/dist/js/SignIn.js）：`<li class="checkin-day is-today|is-checked|is-past" aria-label="今天，已签到，+100能量">`
  内含 `<p class="checkin-date">11/26 <strong>周四</strong></p>` + `<button class="checkin-action" aria-label="立即签到">签到</button>`
  （只有「今天且未签」才有这个 button）+ `<span class="checkin-status">待签到/已签到/未签到</span>`。
  状态语义（app.js 计算属性）：已签到 / 待签到（今天或未来）/ 未签到（过去没签）——
  所以「今天未签」时格子里同时有 button 和「待签到」两个文案。
- 2026-11-26 重写签到判定（旧版把页面标题 h2「签到」当成按钮、且报告写的是点击前的旧快照）：
  · 今日格子 = 日期文本 M/D 严格解析 → aria-label「今天」→ class is-today；
  · 只点今日格子里的 button（不做全局文案匹配，避免点到标题/别的日期）；
  · 判成功 = 点完今日格子变成「已签到」，能量变了但格子没变 → unverified 不假成功；
  · 报告/结果窗的「今日格子」改为签到后重新读取的实时值；
  · 占位页（SignIn.js 的 error-page 分支：新活动即将上线 + 刷新）自动点刷新重试 3 次。
- 2026-11-26 实跑复核：13:41 那次其实签到成功（能量 18809→18909，11/26 已签到），只是报告里显示
  点击前的「待签到」把人和脚本都误导了；8:23/8:25 两次是客户端数据加载失败走了占位页（点标题无用）。
- 客户端启动：天禧自家服务开机拉起时无调试端口；已建 Startup\天禧AI(带调试端口).lnk，
  脚本 ensure_client_cdp 在无端口时自动重启客户端，--fix-launcher 可修所有启动入口。
- 旧 `tianxi_auto.py`（纯网页 Playwright 模式）对天禧AI 无效，仅留作纯网页站点备用。
- 本机 `py` 是 Python 3.14.0a5（装不了 playwright），建 venv 用 `python`（3.8.6）；`.venv` 已就绪。
  沙箱内 `.venv\Scripts\python.exe` 起不来（会去找 base python 失败），调试时用
  `C:\Program Files\python\python.exe` + `sys.path.insert(0,'.venv/Lib/site-packages')` 代替。
- 2026-11-24 实跑：签到 +100、三任务各 +200，总能量 18347 → 19047，退出码 0。
