# -*- coding: utf-8 -*-
"""天禧AI 自动签到 + 任务能量领取（CDP 直连客户端版）

原理：天禧AI 客户端（XLSmartUI.exe）是 CEF 应用，签到中心是本地 H5 页面
（skin/dist/index.html#/SignIn），接口靠页面内注入的 window.jsbridge。
因此用 Chrome DevTools 协议直连客户端内部页面，在真实登录态里模拟点击。

页面改版防护（三层）：
  1. 自动兜底：路由失败自动改点侧边栏「签到中心」/顶部按钮；文案匹配支持前后缀变体
     （如「立即签到」）；找不到签到入口或任务区异常时自动导出诊断文件并置退出码非 0。
  2. 手动校准：python tianxi_cdp.py --inspect 导出按钮清单；文案在 config.json 的
     client.keywords 里改，不用动代码。
  3. 能量对账：以「总能量」变化为最终判据，文案怎么改都能核对。

用法：
  & .venv\Scripts\python.exe tianxi_cdp.py              # 签到 + 任务
  & .venv\Scripts\python.exe tianxi_cdp.py --signin-only
  & .venv\Scripts\python.exe tianxi_cdp.py --dry-run    # 只识别不点击
  & .venv\Scripts\python.exe tianxi_cdp.py --inspect    # 页面改版时：导出按钮清单
"""
import argparse
import base64
import json
import logging
import os
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / ("cdp-%s.log" % time.strftime("%Y%m%d")), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tianxi-cdp")

CDP_HTTP = "http://127.0.0.1:9222"
SCREENSHOTS_ENABLED = False  # 默认不截图（省空间）；config.json 里 client.screenshot: true 可开启

# 匹配规则：等于关键词，或以关键词结尾且总长不超过关键词+4（容忍「立即签到」类变体）。
# 页面改版时优先改 config.json 的 client.keywords，而不是改这里。
DEFAULT_KEYWORDS = {
    "route": "#/SignIn",            # 签到中心路由
    "page_markers": ["能量", "签到"],  # 判断「已到达签到页」的页面特征
    "signin_button": ["签到"],        # 可点击的签到按钮
    "signin_pending": ["待签到", "未签到"],  # 格子状态：未签
    "signin_done": ["已签到"],        # 格子状态：已签
    "signin_exclude": ["本周签到", "连续签到", "每日签到", "签到中心", "签到有礼"],  # 含这些字样的文案不是签到按钮
    "task_action": ["去完成", "领能量", "领取"],  # 任务可点按钮（两段式）
    "task_done": ["已完成", "已领取"],  # 任务完成态（不可再点）
    "task_confirm": ["确定", "知道了", "我知道了", "确认"],  # 任务流程中的确认弹窗
    "sidebar": ["签到中心"],          # 路由失效时的兜底点击目标
    "energy_label": "总能量",         # 能量对账的标签文案
}


def _norm_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def merge_kw_list(base, override):
    """合并关键词：直接列出即追加，前缀“-”表示移除（如 "-已签到"）"""
    items = _norm_list(base) + _norm_list(override)
    add, remove = [], set()
    for item in items:
        if item.startswith("-"):
            stripped = item[1:].strip()
            if stripped:
                remove.add(stripped)
        else:
            add.append(item)
    return [x for x in dict.fromkeys(add) if x not in remove]


def load_keywords():
    kw = dict(DEFAULT_KEYWORDS)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        over = ((cfg.get("client") or {}).get("keywords")) or {}
        for key, val in over.items():
            if key in ("route", "energy_label"):
                kw[key] = str(val or kw[key])
            else:
                kw[key] = merge_kw_list(kw.get(key), val)
        if over:
            log.info("已从 config.json 加载 client.keywords 覆盖")
    except Exception:
        pass
    return kw


def load_client_options():
    """任务流程相关选项（与页面关键词分开）"""
    global SCREENSHOTS_ENABLED
    opts = {
        "auto_fill_prompt": False,
        "task_prompt": "请直接完成这个任务，输出完整结果。",
        "task_skip": [],
    }
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        client = cfg.get("client") or {}
        opts["auto_fill_prompt"] = bool(client.get("auto_fill_prompt", False))
        opts["task_prompt"] = str(client.get("task_prompt") or opts["task_prompt"])
        opts["task_skip"] = [s for s in _norm_list(client.get("task_skip"))]
        SCREENSHOTS_ENABLED = bool(client.get("screenshot", False))
    except Exception:
        pass
    return opts


# 天禧AI 签到中心的真实 DOM（反编译自 skin/dist/js/SignIn.js）：
#   <li class="checkin-day [is-today|is-checked|is-past]" aria-label="今天，已签到，+100能量">
#     <p class="checkin-date"> 11/26 <strong>周四</strong> </p>
#     <div class="checkin-reward"><strong>+100</strong></div>
#     <button class="checkin-status-icon checkin-action" aria-label="立即签到">签到</button>  ← 仅「今天且未签」时存在
#     <span class="checkin-status">待签到</span>
#   </li>
# 状态文案（app.js 计算属性）：已签到 / 待签到（今天或未来）/ 未签到（过去没签）
# 所以「今天未签」时格子里同时有 <button>签到</button> 和 <span>待签到</span>：
# 必须按格子定位再点格子里的按钮，全局找「签到」文案会点到页面标题、点错日期。
HELPER_JS = r"""(() => {
  if (window.__tx && window.__tx.ready) return {ok: true};
  const txt = (el) => (el.innerText || '').trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const hit = (t, arr) => !!t && arr.some(k => t === k || (t.length <= k.length + 4 && t.endsWith(k)));
  const leavesNow = () => Array.from(document.querySelectorAll('*')).filter(el =>
    visible(el) && el.children.length === 0 && txt(el));
  const dateOf = (s) => {
    const m = String(s || '').match(/(\d{1,2})\s*\/\s*(\d{1,2})/);
    return m ? {month: +m[1], day: +m[2]} : null;
  };
  const stateHit = (t, KW) => hit(t, KW.signin_pending) || hit(t, KW.signin_done);
  window.__tx = {
    ready: true, txt: txt, visible: visible, hit: hit, leaves: leavesNow, dateOf: dateOf,
    // 每个签到格子：从状态文案向上找到第一个含日期(M/D)的祖先
    cells: function (KW) {
      const out = [];
      leavesNow().filter(el => stateHit(txt(el), KW)).forEach(sl => {
        let node = sl;
        for (let i = 0; i < 6 && node; i++) {
          node = node.parentElement;
          if (!node) break;
          const d = dateOf(txt(node));
          if (!d) continue;
          out.push({
            el: node, month: d.month, day: d.day, state: txt(sl),
            aria: (node.getAttribute ? (node.getAttribute('aria-label') || '') : ''),
            cls: (typeof node.className === 'string' ? node.className : '')
          });
          break;
        }
      });
      return out;
    },
    // 今天的格子：日期严格匹配 → aria-label 以「今天」开头 → class 含 is-today
    today: function (KW) {
      const now = new Date();
      const M = now.getMonth() + 1, D = now.getDate();
      const cells = this.cells(KW);
      let c = cells.filter(x => x.month === M && x.day === D)[0];
      if (!c) c = cells.filter(x => String(x.aria).indexOf('今天') === 0)[0];
      if (!c) c = cells.filter(x => String(x.cls).indexOf('is-today') !== -1)[0];
      if (!c) return {found: false, cellCount: cells.length};
      const btns = Array.from(c.el.querySelectorAll('button,[role="button"]')).filter(el =>
        visible(el) && (hit(txt(el), KW.signin_button) ||
          ((el.getAttribute && el.getAttribute('aria-label')) || '').indexOf('签到') !== -1));
      return {
        found: true, month: c.month, day: c.day, state: c.state,
        signed: hit(c.state, KW.signin_done),
        hasBtn: btns.length > 0, btnText: btns.length ? txt(btns[0]) : '',
        aria: String(c.aria).slice(0, 60), cls: String(c.cls).slice(0, 80),
        cellCount: cells.length, dateLabel: c.month + '/' + c.day
      };
    }
  };
  return {ok: true};
})()"""


def build_snapshot_js(kw):
    return HELPER_JS + ";\n" + r"""((KW) => {
  const txt = window.__tx.txt;
  const hit = window.__tx.hit;
  const leaves = window.__tx.leaves();
  const bodyText = document.body ? document.body.innerText : '';
  const ei = bodyText.indexOf(KW.energy_label);
  const totalEnergy = ei >= 0 ? ((bodyText.slice(ei).match(/\d+/) || [null])[0]) : null;
  const today = window.__tx.today(KW);
  const cells = window.__tx.cells(KW);
  const signinStates = cells.map(c => c.state);
  const doneNow = !!today.found && today.signed;
  const tasks = [];
  leaves.forEach(el => {
    const t = txt(el);
    if (KW.task_done.some(k => t.indexOf(k) !== -1)) return;
    if (hit(t, KW.task_action)) tasks.push(el);
  });
  const taskDoneCount = leaves.filter(el =>
    KW.task_done.some(k => txt(el).indexOf(k) !== -1)).length;
  const taskInfo = tasks.map(btn => {
    let node = btn, title = '', reward = null;
    for (let i = 0; i < 8 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const t = txt(node);
      if (t.length > 8) {
        const lines = t.split('\n').map(s => s.trim()).filter(Boolean);
        title = lines[0] || '';
        const m = t.match(/\+\s*(\d+)/);
        reward = m ? parseInt(m[1], 10) : null;
        break;
      }
    }
    return { title: title.slice(0, 30), reward: reward, type: txt(btn) };
  });
  // 完整任务清单（含已完成）：按卡片标题去重，状态取自按钮/状态文案
  const rowCount = { n: 0 };
  const rows = {};
  leaves.forEach(el => {
    const t = txt(el);
    let state = null;
    if (KW.task_done.some(k => t.indexOf(k) !== -1)) state = '已完成';
    else if (hit(t, KW.task_action)) state = t;
    if (!state) return;
    let node = el, title = '', reward = null;
    for (let i = 0; i < 8 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const c = txt(node);
      if (/\+\s*\d+/.test(c) && c.length > 8) {
        const ls = c.split('\n').map(s => s.trim()).filter(Boolean);
        title = ls[0] || '';
        const m = c.match(/\+\s*(\d+)/);
        reward = m ? parseInt(m[1], 10) : null;
        break;
      }
    }
    if (!title) title = '任务' + (++rowCount.n);
    const key = title.slice(0, 30);
    if (!rows[key]) rows[key] = { title: key, reward: reward, state: state };
  });
  const taskList = Object.keys(rows).map(k => rows[k]);
  const hasRefresh = leaves.some(el =>
    ['刷新', '重新加载', '重新载入', '刷新重试'].some(k => txt(el) === k));
  return {
    hash: location.hash,
    hasJsbridge: typeof window.jsbridge !== 'undefined',
    bodyText: bodyText.slice(0, 400),
    totalEnergy: totalEnergy,
    signinStates: signinStates,
    todayFound: !!today.found,
    todayState: today.found ? today.state : null,
    todayDate: today.found ? today.dateLabel : null,
    todaySigned: doneNow,
    todayHasButton: !!today.hasBtn,
    cellCount: cells.length,
    hasRefresh: hasRefresh,
    errorPage: hasRefresh && !cells.length,
    taskCount: tasks.length,
    taskDoneCount: taskDoneCount,
    taskInfo: taskInfo,
    taskList: taskList
  };
})""" + "(" + json.dumps(kw, ensure_ascii=False) + ")"


class CDPPage:
    """单个 page target 的 CDP 会话"""

    def __init__(self, ws_url):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=25, suppress_origin=True)
        self.seq = 0

    def send(self, method, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == self.seq:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise RuntimeError("%s timeout" % method)

    def eval_js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS exception: %s" % json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
        return r.get("result", {}).get("value")

    def screenshot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(r["data"]))


def pick_page():
    with urllib.request.urlopen(CDP_HTTP + "/json", timeout=10) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        u = t.get("url", "")
        if "skin" in u and "dist" in u:
            return t
    for t in pages:
        if t.get("url", "").startswith("file://"):
            return t
    return pages[0] if pages else None


def page_ready(page, kw):
    markers = json.dumps(kw["page_markers"], ensure_ascii=False)
    return bool(page.eval_js(
        "(() => { const t = (document.body ? document.body.innerText : '');"
        " const arr = %s; return arr.some(k => t.indexOf(k) !== -1); })()" % markers))


def click_text_any(page, texts):
    return page.eval_js(
        r"""((arr) => {
          const txt = (el) => (el.innerText || '').trim();
          const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const els = Array.from(document.querySelectorAll('*')).filter(el =>
            visible(el) && el.children.length === 0 &&
            arr.some(k => txt(el) === k || (txt(el).length <= k.length + 4 && txt(el).endsWith(k))));
          if (!els.length) return {clicked: false};
          els[0].click();
          return {clicked: true, text: txt(els[0])};
        })""" + "(" + json.dumps(texts, ensure_ascii=False) + ")")


def goto_signin(page, kw):
    """导航到签到页：路由优先，失败自动改点侧边栏 / 顶部按钮"""
    page.eval_js("location.hash = %s" % json.dumps(kw["route"]))
    time.sleep(2.5)
    if page_ready(page, kw):
        return True
    log.warning("路由 %s 未生效，尝试点击侧边栏兜底……", kw["route"])
    if click_text_any(page, kw["sidebar"]).get("clicked"):
        time.sleep(2)
        if page_ready(page, kw):
            return True
    if click_text_any(page, kw["signin_button"]).get("clicked"):
        time.sleep(2)
        if page_ready(page, kw):
            return True
    return False


def goto_signin_hash(page):
    try:
        page.eval_js("location.hash = %s" % json.dumps(DEFAULT_KEYWORDS["route"]))
    except Exception:
        pass


def build_dump_js():
    return r"""(() => {
  const txt = (el) => (el.innerText || '').trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const leaves = Array.from(document.querySelectorAll('*')).filter(el =>
    visible(el) && el.children.length === 0 && txt(el)).slice(0, 200).map(el => ({
      text: txt(el).slice(0, 40),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60)
    }));
  return {
    url: location.href, title: document.title, hash: location.hash,
    hasJsbridge: typeof window.jsbridge !== 'undefined',
    bodyText: (document.body ? document.body.innerText : '').slice(0, 3000),
    leaves: leaves
  };
})()"""


def dump_diagnostics(page, kw, tag):
    """页面异常/改版时导出按钮清单 + 截图，供校准 client.keywords"""
    try:
        data = page.eval_js(build_dump_js())
        ts = time.strftime("%Y%m%d-%H%M%S")
        out = LOG_DIR / ("client-diag-%s-%s.json" % (tag, ts))
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        if SCREENSHOTS_ENABLED:
            try:
                page.screenshot(str(LOG_DIR / ("client-diag-%s-%s.png" % (tag, ts))))
            except Exception:
                pass
        extra = "与截图" if SCREENSHOTS_ENABLED else ""
        log.info("已导出诊断文件：%s（含按钮清单%s，可据此更新 client.keywords）", out.name, extra)
        return str(out)
    except Exception as exc:
        log.debug("诊断导出失败：%s", exc)
        return None


def inspect_page(page, kw):
    """--inspect：打印候选按钮清单，指导修改 client.keywords"""
    dump_diagnostics(page, kw, "inspect")
    matches = page.eval_js(r"""(KW => {
      const txt = (el) => (el.innerText || '').trim();
      const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const hit = (t, arr) => !!t && arr.some(k => t === k || (t.length <= k.length + 4 && t.endsWith(k)));
      const leaves = Array.from(document.querySelectorAll('*')).filter(el =>
        visible(el) && el.children.length === 0 && txt(el));
      const groups = {};
      const states = leaves.map(el => txt(el)).filter(t =>
        hit(t, KW.signin_pending) || hit(t, KW.signin_done));
      [['signin_button', KW.signin_button], ['signin_pending', KW.signin_pending],
       ['signin_done', KW.signin_done], ['task_action', KW.task_action],
       ['task_done', KW.task_done]].forEach(([name, arr]) => {
        groups[name] = leaves.map(el => txt(el)).filter(t =>
          hit(t, arr) && !(name === 'signin_button' &&
            (states.includes(t) || KW.signin_exclude.some(k => t.indexOf(k) !== -1))));
      });
      groups._allLeafTexts = Array.from(new Set(leaves.map(el => txt(el)))).slice(0, 80);
      return groups;
    })""" + "(" + json.dumps(kw, ensure_ascii=False) + ")")
    print("\n===== 页面候选按钮（按关键词分组，命中才会计入流程）=====")
    for name in ("signin_button", "signin_pending", "signin_done", "task_action", "task_done"):
        vals = matches.get(name) or []
        print("  %-14s %s" % (name, vals if vals else "(无命中)"))
    print("\n  页面全部叶子文本（前 80）：")
    for t in matches.get("_allLeafTexts") or []:
        print("    %r" % t)
    print("\n改法：把实际按钮文案填进 config.json 的 client.keywords（结构见 README），再跑 --dry-run 验证。")


def wait_content(page, kw, timeout=60):
    """等待签到页内容真正加载出来。

    天禧AI 客户端偶发把签到页渲染成占位页（"新活动即将上线，敬请期待" + 刷新按钮，
    见 SignIn.js 的 error-page 分支），此时页面上没有能量、没有格子可点，
    直接点「签到」只会点到页面标题、什么也不会发生——这里自动点刷新重试。"""
    deadline = time.time() + timeout
    refreshes = 0
    snap = {}
    while True:
        snap = page.eval_js(build_snapshot_js(kw))
        if snap.get("todayFound") or snap.get("totalEnergy"):
            return snap
        if time.time() >= deadline:
            log.warning("等待签到页内容超时（%d 秒），仍没加载出今日格子。", timeout)
            return snap
        if snap.get("hasRefresh"):
            if refreshes >= 3:
                log.warning("刷新重试 %d 次仍未加载出签到内容。", refreshes)
                return snap
            refreshes += 1
            log.info("签到页处于活动占位/加载失败状态，点击「刷新」重试（第 %d 次）……", refreshes)
            page.eval_js(click_exact_js(["刷新", "重新加载", "重新载入"]))
            time.sleep(4)
            continue
        log.info("等待签到页内容加载……")
        time.sleep(2)


def click_today_signin_js(kw):
    """只点「今天」那格里的签到按钮（button.checkin-action），不碰别的日期和页面标题。"""
    return HELPER_JS + ";\n" + r"""((KW) => {
  const txt = window.__tx.txt;
  const hit = window.__tx.hit;
  const visible = window.__tx.visible;
  const t = window.__tx.today(KW);
  if (!t.found) return {clicked: false, reason: 'today-cell-not-found'};
  if (t.signed) return {clicked: false, reason: 'already-signed'};
  const cell = window.__tx.cells(KW).filter(c => c.month === t.month && c.day === t.day)[0];
  const el = cell ? cell.el : null;
  if (!el) return {clicked: false, reason: 'cell-missing'};
  let btn = Array.from(el.querySelectorAll('button,[role="button"]')).filter(b =>
    visible(b) && (hit(txt(b), KW.signin_button) ||
      ((b.getAttribute && b.getAttribute('aria-label')) || '').indexOf('签到') !== -1))[0];
  let via = 'button';
  if (!btn) {
    // 兜底：格子内的「签到」叶子
    btn = Array.from(el.querySelectorAll('*')).filter(b =>
      visible(b) && b.children.length === 0 && hit(txt(b), KW.signin_button) &&
      !hit(txt(b), KW.signin_pending) && !hit(txt(b), KW.signin_done))[0];
    via = 'leaf';
  }
  if (!btn) { el.click(); return {clicked: true, via: 'cell', dateLabel: t.dateLabel}; }
  btn.click();
  return {clicked: true, via: via, btnText: txt(btn), dateLabel: t.dateLabel};
})""" + "(" + json.dumps(kw, ensure_ascii=False) + ")"


def wait_signin_done(page, kw, before, timeout=20):
    """等签到结果：今日格子变成「已签到」或总能量变化，谁先到算谁。"""
    deadline = time.time() + timeout
    snap = {}
    after = None
    while True:
        snap = page.eval_js(build_snapshot_js(kw))
        after = snap.get("totalEnergy")
        if snap.get("todaySigned") or (before and after and str(after) != str(before)):
            return snap, after
        if time.time() >= deadline:
            return snap, after
        time.sleep(1.0)


def do_signin(page, kw, dry_run):
    snap = page.eval_js(build_snapshot_js(kw))
    if not snap.get("hasJsbridge"):
        return {"status": "failed", "detail": "页面上没有 jsbridge，客户端可能未登录或页面未就绪"}
    snap = wait_content(page, kw)
    before = snap.get("totalEnergy")
    today_state = snap.get("todayState")
    log.info("当前总能量：%s，今日格子：%s（%s，共 %d 格）",
             before, today_state, snap.get("todayDate"), snap.get("cellCount") or 0)
    if snap.get("todaySigned"):
        return {"status": "already",
                "detail": "今日格子 %s 已是已签到（能量 %s）" % (snap.get("todayDate"), before)}
    if not snap.get("todayFound"):
        dump_diagnostics(page, kw, "signin-not-found")
        return {"status": "not_found",
                "detail": "没找到今天(%s)的签到格子——活动数据没加载出来或页面已改版" % time.strftime("%m/%d")}
    if dry_run:
        return {"status": "dry_run",
                "detail": "今日格子 %s 当前状态「%s」，未点击" % (snap.get("todayDate"), today_state)}
    if not snap.get("todayHasButton"):
        log.warning("今日格子 %s 没找到「签到」按钮（状态：%s），将退化为直接点格子",
                    snap.get("todayDate"), today_state)

    clicked = page.eval_js(click_today_signin_js(kw))
    if not clicked.get("clicked"):
        dump_diagnostics(page, kw, "signin-click-failed")
        return {"status": "failed",
                "detail": "点击今日签到按钮失败：%s" % (clicked.get("reason") or "未找到按钮")}

    log.info("已点击今日(%s)格子的「%s」按钮", clicked.get("dateLabel"), clicked.get("btnText"))
    time.sleep(1.5)
    snap2, after = wait_signin_done(page, kw, before, timeout=20)
    today_state2 = snap2.get("todayState")
    if snap2.get("todaySigned"):
        return {"status": "ok",
                "detail": "今日格子 %s 已签到（总能量 %s -> %s）"
                          % (snap2.get("todayDate") or clicked.get("dateLabel"), before, after)}
    if before and after and str(after) != str(before):
        return {"status": "unverified",
                "detail": "能量 %s -> %s，但今日格子仍是「%s」，没确认签到，请人工核对"
                          % (before, after, today_state2)}
    dump_diagnostics(page, kw, "signin-unverified")
    return {"status": "unverified",
            "detail": "点击后今日格子仍为「%s」，总能量 %s（未变化）" % (today_state2, after)}


def click_task_button(page, text):
    return page.eval_js(r"""((text) => {
      const txt = (el) => (el.innerText || '').trim();
      const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const btns = Array.from(document.querySelectorAll('*')).filter(el =>
        visible(el) && txt(el) === text && el.children.length === 0);
      if (!btns.length) return {clicked: false};
      btns[0].click();
      return {clicked: true, count: btns.length};
    })""" + "(" + json.dumps(text, ensure_ascii=False) + ")")


def click_exact_js(texts):
    """精确文案点击（确认弹窗、发送按钮等，不用模糊匹配）"""
    return r"""((arr) => {
      const txt = (el) => (el.innerText || '').trim();
      const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const els = Array.from(document.querySelectorAll('*')).filter(el =>
        visible(el) && el.children.length === 0 && arr.indexOf(txt(el)) !== -1);
      if (!els.length) return {clicked: false};
      els[0].click();
      return {clicked: true, text: txt(els[0])};
    })""" + "(" + json.dumps(texts, ensure_ascii=False) + ")"


def fill_chat_input_js(text):
    """在任务对话页自动填提示词：仅限 #/chat 页、输入框可见且为空才填，绝不覆盖已有内容"""
    return r"""((text) => {
      if (location.hash.indexOf('#/chat') !== 0) return {filled: false, reason: 'not-chat'};
      const el = document.querySelector('textarea, input[type=text], [contenteditable=true]');
      if (!el) return {filled: false, reason: 'no-input'};
      const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      if (!visible) return {filled: false, reason: 'hidden'};
      const current = el.isContentEditable ? (el.textContent || '').trim() : (el.value || '').trim();
      if (current) return {filled: false, reason: 'has-text'};
      el.focus();
      if (el.isContentEditable) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent('input', {bubbles: true}));
      } else {
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, text);
        el.dispatchEvent(new Event('input', {bubbles: true}));
      }
      return {filled: true};
    })""" + "(" + json.dumps(text, ensure_ascii=False) + ")"


def settle_back_to_signin(page, kw, timeout_s=45):
    """任务点击后会跳到 AI 对话页；等它结束并回到签到页"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2.5)
        try:
            if page.eval_js("location.hash") == kw["route"]:
                return True
        except Exception:
            pass
        try:
            page.eval_js("history.back()")
        except Exception:
            pass
        time.sleep(1.2)
        try:
            if page.eval_js("location.hash") == kw["route"]:
                return True
        except Exception:
            pass
        goto_signin_hash(page)
    goto_signin(page, kw)
    return page_ready(page, kw)


def poll_task_state(page, kw, title, before_type, before_energy, timeout_s=30):
    """轮询任务状态推进：按钮消失 / 换阶段 / 能量到账"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        snap = page.eval_js(build_snapshot_js(kw))
        entry = None
        for t in snap.get("taskInfo", []):
            if t["title"] == title:
                entry = t
                break
        after = snap.get("totalEnergy")
        if entry is None:
            return "gone", after
        if entry["type"] != before_type:
            return entry["type"], after
        if before_energy and after and after != before_energy:
            return entry["type"], after
    return before_type, page.eval_js(build_snapshot_js(kw)).get("totalEnergy")


def do_tasks(page, kw, opts, dry_run, max_rounds=12):
    """任务两段式：去完成 ->（AI 生成）-> 领能量 -> 已完成。循环推进直到没有可点按钮。
    任务变化自适应：列表/名称/奖励动态识别；确认弹窗自动点；可选自动填提示词；跳过名单。"""
    results = []
    seen = set()
    for _ in range(max_rounds):
        snap = page.eval_js(build_snapshot_js(kw))
        infos = [t for t in snap.get("taskInfo", []) if (t["title"], t["type"]) not in seen]
        if not infos:
            break
        chosen = None
        for info in infos:
            if any(s and s in info["title"] for s in opts.get("task_skip") or []):
                seen.add((info["title"], info["type"]))
                log.info("任务：%s 命中跳过名单 task_skip，不自动处理", info["title"])
            else:
                chosen = info
                break
        if chosen is None:
            continue
        info = chosen
        title, reward, btype = info["title"], info["reward"], info["type"]
        before = snap.get("totalEnergy")
        log.info("任务：%s（+%s）当前按钮：%s", title, reward, btype)

        if dry_run:
            results.append({"task": title, "reward": reward, "status": "dry_run",
                            "detail": "识别到「%s」，未点击" % btype})
            seen.add((title, btype))
            continue

        if not click_task_button(page, btype).get("clicked"):
            results.append({"task": title, "reward": reward, "status": "failed",
                            "detail": "找不到「%s」按钮" % btype})
            seen.add((title, btype))
            continue

        # 任务跳转后的辅助动作：确认弹窗 / 自动填提示词（仅在离开签到页时生效）
        time.sleep(1.5)
        try:
            left_signin = page.eval_js("location.hash") != kw["route"]
        except Exception:
            left_signin = False
        if left_signin:
            if page.eval_js(click_exact_js(kw["task_confirm"])).get("clicked"):
                log.info("  已自动点击确认弹窗")
            if opts.get("auto_fill_prompt"):
                fill = page.eval_js(fill_chat_input_js(opts["task_prompt"]))
                if fill.get("filled"):
                    time.sleep(0.6)
                    sent = page.eval_js(click_exact_js(["发送"]))
                    log.info("  已自动填写提示词%s", "并发送" if sent.get("clicked") else "（未找到发送按钮）")
                elif fill.get("reason") not in ("no-input", "not-chat"):
                    log.info("  输入框状态：%s", fill.get("reason"))
        settle_back_to_signin(page, kw)
        new_type, after = poll_task_state(page, kw, title, btype, before)

        if btype == "去完成" and new_type == "领能量":
            log.info("  生成完成，进入待领取状态")
            seen.add((title, btype))
            continue

        if btype == "领能量":
            got = before and after and str(after) != str(before)
            if got or new_type in ("gone", "已完成"):
                results.append({"task": title, "reward": reward, "status": "ok",
                                "detail": "已领取（能量 %s -> %s）" % (before, after)})
            else:
                results.append({"task": title, "reward": reward, "status": "unverified",
                                "detail": "点击领能量后未见能量变化（%s）" % after})
        else:
            if (before and after and str(after) != str(before)) or new_type in ("gone", "领能量"):
                results.append({"task": title, "reward": reward, "status": "ok",
                                "detail": "状态推进 %s -> %s（能量 %s -> %s）" % (btype, new_type, before, after)})
            else:
                results.append({"task": title, "reward": reward, "status": "unverified",
                                "detail": "点击后无状态变化（能量 %s）" % after})
        seen.add((title, btype))
        time.sleep(random.uniform(1.0, 2.0))
    return results


# --------------------------------------------------------------------------- #
# 一体化运维：端口保障 / 桌面快捷方式 / 计划任务 / 结果弹窗（全部内置，无外部脚本）
# --------------------------------------------------------------------------- #
CLIENT_EXE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Lenovo" / "AIAgent" / "XLSmartUI.exe",
    Path(r"C:\Program Files (x86)\Lenovo\AIAgent\XLSmartUI.exe"),
]
SCHEDULE_NAME = "TianxiAI-AutoCheckin"
SHORTCUT_ITEMS = [
    ("天禧AI自动签到", "--pause", "运行签到+任务：窗口实时显示进度，结束弹出结果"),
    ("签到结果查看", "--show-result", "查看最近一次签到与任务结果"),
]


def cdp_alive():
    try:
        with urllib.request.urlopen(CDP_HTTP + "/json/version", timeout=3):
            return True
    except Exception:
        return False


def _client_running():
    for name in ("XLSmartUI.exe", "XLAUI.exe"):
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name],
                           capture_output=True, text=True)
        if name.lower() in (r.stdout or "").lower():
            return True
    return False


def close_client_ui():
    for name in ("XLSmartUI.exe", "XLAUI.exe"):
        subprocess.run(["taskkill", "/IM", name], capture_output=True)
    time.sleep(3)
    for name in ("XLSmartUI.exe", "XLAUI.exe"):
        subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True)
    time.sleep(2)


def ensure_client_cdp():
    """客户端没带调试端口时自动重启它（登录态在磁盘，不丢）"""
    if cdp_alive():
        return True
    exe = next((p for p in CLIENT_EXE_CANDIDATES if p.exists()), None)
    if exe is None:
        log.error("找不到 XLSmartUI.exe（Lenovo\\AIAgent），无法自动重启客户端。")
        return False
    if _client_running():
        log.info("关闭正在运行的天禧AI 窗口……")
        close_client_ui()
    log.info("带调试端口启动天禧AI……")
    subprocess.Popen([str(exe), "--xlsource=taskbar", "--remote-debugging-port=%d" % 9222])
    for _ in range(20):
        time.sleep(1)
        if cdp_alive():
            log.info("客户端 CDP 就绪（%s）", CDP_HTTP)
            return True
    log.error("等待调试端口超时。")
    return False


def _run_ps(script, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60, env=env)


def _desktop_dir():
    r = _run_ps("[Environment]::GetFolderPath('Desktop')")
    p = (r.stdout or "").strip()
    return p if p else os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def install_shortcut(remove=False):
    """创建/删除桌面快捷方式：自动签到（带进度窗）+ 结果查看，图标用天禧AI favicon"""
    exe = next((p for p in CLIENT_EXE_CANDIDATES if p.exists()), None)
    icon = ""
    if exe:
        ico = exe.parent / "skin" / "dist" / "favicon.ico"
        if ico.exists():
            icon = str(ico) + ",0"
    if not icon:
        icon = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\shell32.dll,22"

    script = r"""
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SC_LNK)
$lnk.TargetPath = $env:SC_TARGET
$lnk.Arguments = $env:SC_ARGS
$lnk.WorkingDirectory = $env:SC_WORKDIR
$lnk.IconLocation = $env:SC_ICON
$lnk.Description = $env:SC_DESC
$lnk.Save()
Write-Output created
"""
    ok = True
    desk = _desktop_dir()
    script_arg = '"%s"' % (ROOT / "tianxi_cdp.py")
    for name, extra_args, desc in SHORTCUT_ITEMS:
        lnk = os.path.join(desk, name + ".lnk")
        if remove:
            try:
                os.remove(lnk)
                log.info("已删除桌面快捷方式：%s", lnk)
            except OSError:
                pass
            continue
        env = {
            "SC_LNK": lnk,
            "SC_TARGET": sys.executable,
            "SC_ARGS": (script_arg + " " + extra_args).strip(),
            "SC_WORKDIR": str(ROOT),
            "SC_ICON": icon,
            "SC_DESC": desc,
        }
        r = _run_ps(script, env)
        if r.returncode == 0 and "created" in (r.stdout or ""):
            log.info("已创建桌面快捷方式：%s（参数 %s）", lnk, extra_args)
        else:
            ok = False
            log.error("创建快捷方式失败：%s", ((r.stderr or "") or (r.stdout or "")).strip()[:200])
    return ok


def install_schedule(time_str, remove=False):
    """注册/删除每日计划任务（pythonw 静默运行，结果窗 90 秒自动关闭）"""
    if remove:
        r = subprocess.run(["schtasks", "/Delete", "/TN", SCHEDULE_NAME, "/F"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log.info("已删除计划任务 %s", SCHEDULE_NAME)
            return True
        log.error("删除计划任务失败：%s", (r.stderr or "").strip()[:200])
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    python_bin = pythonw if pythonw.exists() else Path(sys.executable)
    script = ROOT / "tianxi_cdp.py"
    tr_value = '\\"%s\\" \\"%s\\"' % (python_bin, script)
    r = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", SCHEDULE_NAME, "/SC", "DAILY", "/ST", time_str, "/TR", tr_value],
        capture_output=True, text=True)
    if r.returncode == 0:
        log.info("已注册计划任务 %s：每天 %s 自动签到+任务", SCHEDULE_NAME, time_str)
        return True
    log.error("注册计划任务失败：%s", (r.stderr or r.stdout or "").strip()[:200])
    return False


def fix_launcher():
    """把所有指向 XLSmartUI.exe 的快捷方式加上调试端口参数，并创建开机自启项，
    以后正常启动天禧AI 就自带 CDP，不再需要脚本重启"""
    args = "--xlsource=taskbar --remote-debugging-port=9222"
    scan_ps = r"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
  'C:\ProgramData\Microsoft\Windows\Start Menu\Programs',
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar')
)
foreach ($d in $dirs) {
  if (-not $d) { continue }
  Get-ChildItem $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $t = $sh.CreateShortcut($_.FullName).TargetPath
      if ($t -like '*XLSmartUI.exe*') { Write-Output $_.FullName }
    } catch {}
  }
}
"""
    r = _run_ps(scan_ps)
    targets = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    fix_ps = r"""
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($env:SC_LNK)
$lnk.Arguments = $env:SC_ARGS
$lnk.Save()
Write-Output fixed
"""
    fixed = 0
    for lnk in targets:
        rr = _run_ps(fix_ps, {"SC_LNK": lnk, "SC_ARGS": args})
        if rr.returncode == 0 and "fixed" in (rr.stdout or ""):
            fixed += 1
            log.info("已为快捷方式加调试参数：%s", lnk)
    exe = next((p for p in CLIENT_EXE_CANDIDATES if p.exists()), None)
    if exe:
        startup = (_run_ps("[Environment]::GetFolderPath('Startup')").stdout or "").strip()
        if startup:
            lnk = os.path.join(startup, "天禧AI(带调试端口).lnk")
            rr = _run_ps(r"""
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SC_LNK)
$lnk.TargetPath = $env:SC_TARGET
$lnk.Arguments = $env:SC_ARGS
$lnk.Save()
Write-Output created
""", {"SC_LNK": lnk, "SC_TARGET": str(exe), "SC_ARGS": args})
            if rr.returncode == 0 and "created" in (rr.stdout or ""):
                fixed += 1
                log.info("已创建开机自启：%s", lnk)
    return fixed


STATUS_TAGS = {
    "ok": "[成功]", "already": "[今日已签]", "dry_run": "[演练]",
    "failed": "[失败]", "not_found": "[未识别]", "unverified": "[待确认]",
}


def _status_tag(s):
    return STATUS_TAGS.get(str(s), "[" + str(s) + "]")


def build_result_text(report, report_name=None, exit_code=None):
    lines = ["运行时间：" + str(report.get("started", ""))]
    signin = report.get("signin") or {}
    if signin:
        lines.append("")
        lines.append("签到 " + _status_tag(signin.get("status")))
        lines.append("    " + str(signin.get("detail", "")))
    if report.get("today_state"):
        d = report.get("today_date")
        lines.append("今日格子：%s%s" % (report["today_state"], ("（%s）" % d) if d else ""))
    task_list = report.get("task_list") or []
    if task_list:
        lines.append("")
        lines.append("任务列表 %d 项：" % len(task_list))
        for t in task_list:
            rw = "（+%s）" % t["reward"] if t.get("reward") is not None else ""
            lines.append("   %s  %s%s" % (t.get("state", "?"), t.get("title", ""), rw))
    tasks = report.get("tasks") or []
    if tasks:
        ok_n = sum(1 for t in tasks if t.get("status") == "ok")
        lines.append("")
        lines.append("任务 %d/%d 成功" % (ok_n, len(tasks)))
        for t in tasks:
            rw = "（+%s）" % t["reward"] if t.get("reward") else ""
            lines.append("   %s %s%s" % (_status_tag(t.get("status")), t.get("task", ""), rw))
            lines.append("        %s" % t.get("detail", ""))
    lines.append("")
    lines.append("当前总能量：" + str(report.get("finalEnergy", "?")))
    if report.get("suspect_page_changed"):
        lines.append("注意：任务区异常，页面可能改版（详见诊断文件）")
    if report_name:
        lines.append("报告：" + report_name)
    if report.get("screenshot"):
        lines.append("截图：" + str(report["screenshot"]))
    if exit_code is not None:
        lines.append("本次退出码：" + str(exit_code))
    return "\n".join(lines)


def find_latest_report():
    reports = sorted(LOG_DIR.glob("cdp-report-*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def show_result_window(text, auto_close_ms=None):
    """弹结果窗（tkinter，90 秒自动关）；失败退回系统对话框"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("天禧AI 自动签到结果")
        root.geometry("660x480")
        frame = tk.Frame(root)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, font=("Microsoft YaHei UI", 10), wrap="word")
        bar = tk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        if auto_close_ms:
            root.after(auto_close_ms, root.destroy)
        root.mainloop()
        return True
    except Exception as exc:
        log.debug("窗口显示失败（%s），改用系统对话框", exc)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "天禧AI 自动签到结果", 0)
            return True
        except Exception:
            return False


def show_latest_result():
    rep = find_latest_report()
    if not rep:
        log.warning("未找到运行报告（logs/cdp-report-*.json）。")
        return 1
    try:
        report = json.loads(rep.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("报告读取失败：%s", exc)
        return 1
    show_result_window(build_result_text(report, rep.name))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="天禧AI 签到+任务（CDP 版，一体化：端口保障/结果弹窗/快捷方式/计划任务）")
    parser.add_argument("--signin-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inspect", action="store_true", help="导出页面按钮清单，用于页面改版后校准关键词")
    parser.add_argument("--show-result", action="store_true", help="查看最近一次运行结果（弹窗）")
    parser.add_argument("--print-result", action="store_true", help="把最近一次结果打印到控制台")
    parser.add_argument("--install-shortcut", action="store_true", help="创建桌面快捷方式（自动签到+结果查看）")
    parser.add_argument("--remove-shortcut", action="store_true", help="删除桌面快捷方式")
    parser.add_argument("--install-schedule", metavar="HH:MM", nargs="?", const="09:10",
                        help="注册每天计划任务（默认 09:10）")
    parser.add_argument("--remove-schedule", action="store_true", help="删除计划任务")
    parser.add_argument("--no-gui", action="store_true", help="结束时不弹结果窗")
    parser.add_argument("--fix-launcher", action="store_true",
                        help="把桌面/开始菜单/任务栏的天禧AI 快捷方式全部加上调试端口，并创建开机自启（一劳永逸）")
    parser.add_argument("--pause", action="store_true", help="结束后等待回车（桌面快捷方式用）")
    args = parser.parse_args()

    kw = load_keywords()
    opts = load_client_options()

    if args.install_shortcut or args.remove_shortcut:
        return 0 if install_shortcut(remove=args.remove_shortcut) else 2
    if args.install_schedule or args.remove_schedule:
        return 0 if install_schedule(args.install_schedule or "09:10", remove=args.remove_schedule) else 2
    if args.print_result:
        rep = find_latest_report()
        if not rep:
            log.warning("未找到运行报告。")
            return 1
        report = json.loads(rep.read_text(encoding="utf-8"))
        print(build_result_text(report, rep.name))
        return 0
    if args.show_result:
        return show_latest_result()
    if args.fix_launcher:
        n = fix_launcher()
        return 0 if n > 0 else 2

    if not ensure_client_cdp():
        return 2

    target = None
    for attempt in range(8):
        try:
            target = pick_page()
        except Exception as exc:
            log.warning("连接 CDP 失败（%s），重试……", exc)
            target = None
        if target:
            break
        log.info("主界面页面尚未就绪，等待加载（%d/8）……", attempt + 1)
        time.sleep(3)
    if not target:
        log.error("等待主界面页面超时（客户端可能正在更新，稍后重试）。")
        return 2

    try:
        page = CDPPage(target["webSocketDebuggerUrl"])
        page.send("Page.enable")
    except Exception as exc:
        log.error("连接客户端页面失败：%s", exc)
        return 2

    if not goto_signin(page, kw):
        dump_diagnostics(page, kw, "nav-failed")
        log.error("无法到达签到页（路由和兜底点击都失败），页面可能改版。")
        return 2

    if args.inspect:
        inspect_page(page, kw)
        return 0

    snap0 = page.eval_js(build_snapshot_js(kw))
    task_list_at_start = snap0.get("taskList") or []
    if task_list_at_start:
        log.info("当前任务列表（%d 项）：", len(task_list_at_start))
        for t in task_list_at_start:
            rw = "+%s" % t["reward"] if t.get("reward") is not None else "—"
            log.info("  · %s  %s  %s", t["title"], rw, t["state"])

    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "route": kw["route"],
              "task_list_at_start": task_list_at_start,
              "today_state": snap0.get("todayState"),
              "today_date": snap0.get("todayDate")}
    signin = do_signin(page, kw, args.dry_run)
    log.info("签到：%s — %s", signin["status"], signin["detail"])
    report["signin"] = signin
    # 签到后重新读一次今日格子：报告里显示的是「签到之后」的真实状态，而不是点击前的旧值
    try:
        snap_s = page.eval_js(build_snapshot_js(kw))
        report["today_state"] = snap_s.get("todayState")
        report["today_date"] = snap_s.get("todayDate")
        report["today_signed"] = bool(snap_s.get("todaySigned"))
        log.info("签到后今日格子状态：%s（%s）", report["today_state"], report["today_date"])
    except Exception as exc:
        log.debug("签到后状态刷新失败：%s", exc)

    tasks = [] if args.signin_only else do_tasks(page, kw, opts, args.dry_run)
    for t in tasks:
        log.info("任务 %s：%s — %s", t["task"], t["status"], t["detail"])
    report["tasks"] = tasks

    try:
        snap = page.eval_js(build_snapshot_js(kw))
        report["finalEnergy"] = snap.get("totalEnergy")
        report["today_state"] = snap.get("todayState")
        report["today_date"] = snap.get("todayDate")
        report["today_signed"] = bool(snap.get("todaySigned"))
        suspect = bool(
            not tasks and not args.signin_only
            and not args.dry_run
            and not snap.get("taskDoneCount")
            and signin["status"] in ("ok", "already")
        )
        report["suspect_page_changed"] = suspect
        report["task_list"] = snap.get("taskList") or task_list_at_start
        if report["task_list"]:
            log.info("最终任务状态：")
            for t in report["task_list"]:
                rw = "+%s" % t["reward"] if t.get("reward") is not None else "—"
                log.info("  · %s  %s  %s", t["title"], rw, t["state"])
        if suspect:
            log.warning("任务区既无可点按钮也无已完成标记——页面可能改版，建议跑 --inspect 校准。")
            dump_diagnostics(page, kw, "tasks-empty")
        log.info("结束，当前总能量：%s", snap.get("totalEnergy"))
    except Exception as exc:
        log.error("执行异常：%s（已导出诊断）", exc)
        try:
            dump_diagnostics(page, kw, "exception")
        except Exception:
            pass
        return 2

    ts = time.strftime("%Y%m%d-%H%M%S")
    if SCREENSHOTS_ENABLED:
        try:
            shot = LOG_DIR / ("cdp-%s.png" % ts)
            page.screenshot(str(shot))
            report["screenshot"] = str(shot)
        except Exception:
            pass
    (LOG_DIR / ("cdp-report-%s.json" % ts)).write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    bad = [t for t in tasks if t["status"] not in ("ok", "dry_run")]
    signin_ok = signin["status"] in ("ok", "already", "dry_run")
    exit_code = 0 if (signin_ok and not bad and not report["suspect_page_changed"]) else 1

    result_text = build_result_text(report, "cdp-report-%s.json" % ts, exit_code)
    if not args.no_gui:
        show_result_window(result_text, auto_close_ms=90000)
    return exit_code


if __name__ == "__main__":
    # 任何结局（成功/报错/崩溃）都让 --pause 的窗口停留，绝不闪退
    _pause_requested = "--pause" in sys.argv
    _rc = 2
    try:
        _rc = main()
    except BaseException:
        import traceback

        err = traceback.format_exc()
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / ("crash-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))).write_text(err, encoding="utf-8")
        except Exception:
            pass
        print(err, file=sys.stderr)
    if _pause_requested:
        try:
            input("按回车退出…")
        except Exception:
            time.sleep(5)
    sys.exit(_rc or 0)
