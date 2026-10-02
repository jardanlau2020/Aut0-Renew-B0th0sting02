#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot-hosting（bot-hosting.net）自动续期 —— 已迁移到 renew-kit v0.4.2。

迁移要点（与原版的差异，逐条对照）：

1. 退出码从 1 / 2 / 5 / 4 / 4 / 3|6 收敛成 0/1 —— 只有「确定性业务失败」才 1。
     ✅ 续期成功            → RENEWED   → 0
     ⏳ 未到续期时间        → SKIPPED   → 0（且静默，见第 3 点）
     ℹ️ 无需续期（读不到）  → UNKNOWN   → 0（会通知，但不标红）
     ❌ 登录失败 / 续期失败 / 脚本异常中断 → FAILED → 1

2. 通知改由 renewkit.report.RenewReport 统一渲染。原版 hand-roll 的
   send_telegram_message / format_notification / now_local / clip_text /
   fmt_expiry / masked_email / account_label 全部删除，改由 TargetResult
   的 lines() 出稿。TG 凭据由 renewkit.notify.config() 自读
   （TG_BOT_TOKEN / TG_CHAT_ID，或 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 别名），
   少一个就静默跳过通知 —— 通知挂了绝不把续期判成失败。
   代价一处：RENEWED 分支不渲染 detail，所以「可续期倒计时」那句没了；
   成功行本身已带「续期至 MM-DD」+「剩余 N 天」，信息不丢。

3. 「未到续期时间」判 SKIPPED → **静默**（QUIET_OUTCOMES）。
   原版每天都发一条「🟢 状态良好」心跳，但 bot-hosting 是 4 日续期而 cron
   是每日 —— 每 4 次运行里 3 次纯噪音。默认只在这三种情况出声：
   真续成功（✅）/ 真失败（🚨）/ 状态读不到（❓）。
   想要回心跳：给 workflow 加 AUT0_NOTIFY_SKIP: '1' 即可，不用改代码。

4. 一处**有意保留的偏离**：「已點擊但後台未確認」（原 fail_code=3）仍判
   FAILED（红 + 通知），没有收敛成 UNKNOWN。原因：原作者在此处留了明确
   注释 ——「不能只发警告后 return 0，否则 Actions 会显示 success」。
   bot-hosting 漏续会删号，宁可红不可绿；UNKNOWN 会退成绿色，与作者意图相反。

5. 状态字符串（"✅ 续期成功" / "⏳ 未到续期时间" / "❌ 登录失败" …）保留为
   **内部协议**。第 171–793 行整段（Turnstile 五合一偵測、shadow DOM 递归
   探测 token、按钮解锁轮询、OneTrust 弹窗、Discord OAuth）逻辑一个字节
   没动，只把里面几处 os.environ 读法换成等价的 renewkit.env；映射成
   Outcome 只发生在报告边界（_outcome_of）。迁移的风险面就只有文件头和 main()。

   一处变量命名注意：报告名变量故意叫 target 而不是 name —— 正文里注入
   cookie 那段是 `for name, value in COOKIES.items()`，叫 name 会被覆盖成
   最后一个 cookie key（"theme"），整个报告的目标名就全错了。

6. 代理（IS_PROXY / PROXY_SERVER）仍由上游 setup_proxy.sh 写进 GITHUB_ENV，
   app.py 只读不写 —— 与原版一致。

7. 新增 DRY_RUN 支持（workflow_dispatch 的 dry_run 输入）：只挡「回写
   SESSION_TOKEN 到 GitHub Secret」这一件有副作用的事，登录/读 cookie/读
   状态照跑。原版没有演练开关，手动跑一次就会动真实 Secret。
"""

import os, re, sys, time, json, requests, subprocess, traceback
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

from renewkit import env
from renewkit.outcome import Outcome
from renewkit.report import RenewReport, shorten

# 环境变量配置（模块级读取，strip 语义与 renewkit.env.get 一致）
EMAIL         = env.get("EMAIL")           # 邮箱，只用于通知（遮罩后进报告名），可随意填写
SESSION_TOKEN = env.get("SESSION_TOKEN")   # session token，默认登录方式
DISCORD_TOKEN = env.get("DISCORD_TOKEN")   # Discord Token 备用登录方式，SESSION_TOKEN 失败时才用
GH_TOKEN      = env.get("GH_TOKEN")        # GitHub PAT，用于自动回写 SESSION_TOKEN，可选
ACCOUNT_LABEL = env.get("ACCOUNT_LABEL")   # 帳號標識（"01"/"02"），通知用嚟區分多個帳號

# TG 凭据不再在这里读 —— renewkit.notify.config() 自己认 TG_BOT_TOKEN / TG_CHAT_ID
# （或 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 别名），少一个就静默跳过通知。

# 解析 DISCORD_TOKEN（兼容 "label,token" 两段式写法）
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于日志）
_LOGIN_METHOD = "SESSION_TOKEN"

# 获取cookie到期时间
def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            return value, expiry_dt
    return None, None

# 检查是否需要更新cookie
def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

# 更新cookie到secrets
def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

# ══════════════════════ renew-kit 报告边界 ══════════════════════
# 下面这层是把内部状态字符串翻译成 renewkit 语义的**唯一**地方。
# 往下的浏览器逻辑只管返回中文状态串，不认 Outcome。
SERVICE = "Bot-hosting"

#: 静默的 Outcome —— 正常/无事可做时不发 TG。
QUIET_OUTCOMES = frozenset({Outcome.SKIPPED, Outcome.ALREADY_MAX, Outcome.TRANSIENT})

#: 「已达续期上限」的文本特征；命中判 ALREADY_MAX 而不是普通 SKIPPED。
_ALREADY_MAX_HINTS = ("已达续期上限", "renew limit", "limit reached", "上限")

#: 详情串截断长度（renewkit 不截 detail，长串会把 TG 那一行撑爆）。
_DETAIL_LIMIT = 100

_RE_ANY_DATE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def _norm_expiry(value) -> str:
    """把面板到期日期归一成 renewkit 认得的 ``YYYY-MM-DD``。

    extract_expiry_date() 吐的是**斜杠**格式（"2026/07/07"），而 renewkit 的
    _ISO_RE 只认连字符 —— 不归一的话 format_expiry 会把 "2026/07/07" 原样
    印出来，days_left 直接返回 None，「剩 N 天」整段消失。
    认不出的值（"（未获取到）" 之类哨兵）一律回空串，不让脏串进报告。
    """
    m = _RE_ANY_DATE.match(str(value or "").strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _masked_email() -> str:
    """EMAIL 遮罩（原 masked_email 的等价物，改成读 renewkit.env）。"""
    email = env.get("EMAIL")
    if "@" in email:
        name, domain = email.split("@", 1)
        return f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    return (email[:2] + "****") if email else ""


def _target_name() -> str:
    """报告里的目标名：ACCOUNT_LABEL（workflow 传 "01"/"02"）优先，退而用遮罩邮箱。"""
    parts = [p for p in (env.get("ACCOUNT_LABEL").strip(), _masked_email()) if p]
    return f"Bot-hosting（{' '.join(parts)}）" if parts else "Bot-hosting"


def _outcome_of(status: str, detail: str = "") -> Outcome:
    """内部状态串 → Outcome。**只有 ❌ 开头算真失败**（会让 job 标红）。"""
    s = (status or "").strip()
    if s.startswith("✅"):
        return Outcome.RENEWED
    if s.startswith("❌"):
        return Outcome.FAILED
    if s.startswith("ℹ️"):          # 「无需续期」= 连按钮/倒计时都读唔到，状态未知
        return Outcome.UNKNOWN
    if s.startswith("⏳") or s.startswith("⏭️"):
        blob = f"{s} {detail or ''}".lower()
        if any(h.lower() in blob for h in _ALREADY_MAX_HINTS):
            return Outcome.ALREADY_MAX
        return Outcome.SKIPPED
    if s.startswith("⚠️") or s.startswith("⏰"):
        return Outcome.UNKNOWN
    return Outcome.FAILED


def _record(report, target, status, *, extra="", error="", expiry="") -> None:
    """把一次结果落到报告里 —— 所有 report.add 都走这里，口径唯一。

    ❌ 开头时把状态里「❌」后面的词当小标题拼进 detail（原 format_notification
    的 ``reason = f"{head}: {detail}"`` 行为），否则「脚本异常中断」这种状态
    词就丢了。error 与 extra 只拼非空的那个；两者都空时退回小标题本身，
    免得通知里只剩 renewkit 的兜底文案「执行失败」。
    """
    s = (status or "").strip()
    body = str(error or extra or "")
    if s.startswith("❌"):
        head = s.lstrip("❌").strip(" :：")
        body = f"{head}: {body}" if body else head
    detail = shorten(" ".join(body.split()), _DETAIL_LIMIT)
    report.add(target, _outcome_of(s, detail),
               expire=_norm_expiry(expiry), detail=detail)

# 检查页面是否存在 Turnstile iframe（无隐式等待）
def _turnstile_iframe_present(sb) -> bool:
    # 五合一偵測（run#22/23 實證：02 帳號個 turnstile iframe src 冇明顯
    # turnstile 字樣，淨係 iframe[src*=turnstile] 匹配唔到 → 誤判無挑戰）
    try:
        return bool(sb.execute_script(
            "const sels = ['iframe[src*=\"turnstile\"]',"
            " 'iframe[src*=\"challenges.cloudflare.com\"]',"
            " '.cf-turnstile-wrapper',"
            " '[data-sitekey]',"
            " '[data-callback=\"onCaptchaSuccess\"]'];"
            "for (const s of sels) {"
            "  for (const el of document.querySelectorAll(s)) {"
            "    if (el.getClientRects().length > 0) return true;"
            "  }"
            "}"
            "return false;"
        ))
    except Exception:
        return False


# X11 環境（GHA 用 xvfb-run 跑，有 DISPLAY）才有 uc_gui_click_captcha 可用
IS_X11 = bool(env.get("DISPLAY"))


# Turnstile 已解決的鐵證：cf-turnstile-response input 存在且 value 非空。
# widget 內部文字在 cross-origin iframe 裡，頂層 get_page_source() 根本看不見，
# 舊「整頁無 CF 關鍵字」判據因此永遠假陽性 —— captcha 未過就以為過了。
def _turnstile_solved(sb) -> bool:
    # probe 開頭先切返頂層 —— driver 停留喺 cross-origin iframe 時
    # execute_script 唔會 throw，只會靜靜哋搵唔到 token（假陰性）。
    try:
        sb.switch_to_default_content()
    except Exception:
        pass
    # run#25 實錘：02 帳號成個 modal（連 turnstile token input）藏喺
    # shadow DOM（DOM dump 見兩個 DIV shadow hosts，light DOM probe 全滅）
    # —— 遞迴行入晒所有 shadowRoot 搵 token。
    try:
        return bool(sb.execute_script(
            "return (() => {"
            "const deepSel = (root, sel) => {"
            "  const walk = (node) => {"
            "    if (!node || !node.querySelectorAll) return null;"
            "    for (const el of node.querySelectorAll(sel)) {"
            "      if (el.value && el.value.length > 20) return el;"
            "    }"
            "    for (const el of node.querySelectorAll('*')) {"
            "      if (el.shadowRoot) { const hit = walk(el.shadowRoot); if (hit) return hit; }"
            "    }"
            "    return null;"
            "  };"
            "  return walk(root);"
            "};"
            "return !!deepSel(document, '[name=\"cf-turnstile-response\"]');"
            "})();"
        ))
    except Exception:
        return False


# 「Renew for 4 days」按鈕是否已解鎖（bot check 已通過）。
# 頁面文案明寫 "Complete the bot check to unlock the button"：
# 掣未解鎖時 click() 不會報錯，只會被後端忽略 —— 這正是「已點擊但未確認」假失敗的來源。
def _renew_button_unlocked(sb) -> bool:
    # probe 開頭先切返頂層（同 _turnstile_solved）。
    try:
        sb.switch_to_default_content()
    except Exception:
        pass
    # shadow-DOM 探測（run#25：OCR 見到掣但 light DOM 返 missing）
    try:
        found = sb.execute_script(
            "return (() => {"
            "const want = 'Renewfor4days';"
            "const walk = (node) => {"
            "  if (!node || !node.querySelectorAll) return 'missing';"
            "  for (const b of node.querySelectorAll('button, [role=\"button\"], a')) {"
            "    const flat = b.textContent.replace(/[^a-zA-Z0-9]/g, '');"
            "    if (flat.includes(want)) {"
            "      if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'locked';"
            "      return 'unlocked';"
            "    }"
            "  }"
            "  for (const el of node.querySelectorAll('*')) {"
            "    if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r !== 'missing') return r; }"
            "  }"
            "  return 'missing';"
            "};"
            "return walk(document);"
            "})();"
        )
        if found == "unlocked":
            return True
        # 掣搵唔到：彈窗可能還沒渲染完，或被遮擋 —— 視為未過，等下一輪
        return False
    except Exception:
        try:
            sb.switch_to_default_content()
        except Exception:
            pass
        return False


# 「Renew for 4 days」掣三態（unlocked / locked / missing）。
# wait_for_turnstile_pass 寬限期結束後用佢判「真無挑戰」定「假陰性」。
def _renew_button_state(sb) -> str:
    # probe 開頭先切返頂層（同 _renew_button_unlocked）。
    try:
        sb.switch_to_default_content()
    except Exception:
        pass
    try:
        return sb.execute_script(
            "return (() => {"
            "const want = 'Renewfor4days';"
            "let texts = [];"
            "const walk = (node) => {"
            "  if (!node || !node.querySelectorAll) return 'missing';"
            "  for (const b of node.querySelectorAll('button, [role=\"button\"], a')) {"
            "    const flat = b.textContent.replace(/[^a-zA-Z0-9]/g, '');"
            "    if (flat.includes(want)) {"
            "      if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'locked';"
            "      return 'unlocked';"
            "    }"
            "    if (texts.length < 8 && flat) texts.push(flat.slice(0, 24));"
            "  }"
            "  for (const el of node.querySelectorAll('*')) {"
            "    if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r !== 'missing') return r; }"
            "  }"
            "  return 'missing btns=' + texts.join('|');"
            "};"
            "return walk(document);"
            "})();"
        )
    except Exception:
        return "missing"


# Turnstile widget 容器（iframe 未開時嘅宿主 div）—— iframe 遲 render 場景嘅
# 第二信號源（run#17 實證 iframe 可以遲過 27s 先出現）。
def _turnstile_widget_present(sb) -> bool:
    try:
        return bool(sb.execute_script(
            "for (const el of document.querySelectorAll("
            "  '[name=\"cf-turnstile-response\"]',"
            "  '.cf-turnstile-wrapper',"
            "  '[data-callback=\"onCaptchaSuccess\"]',"
            "  'iframe[src*=\"challenges.cloudflare.com\"]',"
            "  'iframe[src*=\"/turnstile/\"]')) {"
            "  if (el && el.getClientRects().length > 0) return true;"
            "}"
            "return false;"
        ))
    except Exception:
        return False


# 診斷 dump：彈窗內 Turnstile 相關 DOM 節點清單（唔掂嗰陣起碼知頁面生咩）
def _dump_turnstile_dom(sb) -> None:
    try:
        nodes = sb.execute_script(
            "const out = [];"
            "for (const el of document.querySelectorAll('iframe,[class*=\"turnstile\"],[class*=\"cf\"],[data-sitekey],#onetrust-consent-sdk')) {"
            "  const vis = el.getClientRects().length > 0;"
            "  const src = el.src ? ' src=' + el.src.slice(0,80) : '';"
            "  out.push(el.tagName + '#' + (el.id || '-') + '.' + String(el.className).slice(0,60) + ' vis=' + vis + src);"
            "}"
            "return out.slice(0, 20).join('\\n');"
        )
        print(f"🧬 Turnstile DOM 快照:\n{nodes or '(無相關節點)'}")
    except Exception as e:
        print(f"🧬 DOM dump 失败: {e}")


# 私隱彈窗（OneTrust / Google CMP）仲咪遮住畫面？
# Google CMP 嘅 host #fc-consent-root 喺 light DOM 可以直接探測；佢啲掣就藏喺 shadow DOM。
def _popup_blocking(sb) -> bool:
    try:
        # IIFE wrap：clicker 嘅 disconnect/reconnect cycle 之後，CDP
        # Runtime.evaluate 可以進入非 wrap 模式，頂層 return 會 SyntaxError
        # （run#25 實錘 27 次連環失敗）—— 全部 script 用 (()=>{...})() 包死。
        return bool(sb.execute_script(
            "return (() => {"
            "  for (const sel of ['#onetrust-banner-sdk', '#onetrust-pc-sdk',"
            "   ' #fc-consent-root', '.fc-consent-root']) {"
            "    const el = document.querySelector(sel);"
            "    if (el && el.getClientRects().length > 0) return true;"
            "  }"
            "  return false;"
            "})();"
        ))
    except Exception:
        return False


# 彈窗/Turnstile DOM 診斷 dump：剷唔走彈窗嗰陣，起碼喺 log 留低現場結構。
def _dump_popup_dom(sb) -> None:
    try:
        info = sb.execute_script(
            "return (() => {"
            "const out = {iframes: [], roots: [], shadowHosts: []};"
            "document.querySelectorAll('iframe').forEach(f => {"
            "  const src = f.src || '';"
            "  if (src.includes('cloudflare') || src.includes('turnstile')"
            "    || f.getClientRects().length > 0) out.iframes.push(src.slice(0, 80));"
            "});"
            "for (const id of ['onetrust-consent-sdk', 'fc-consent-root']) {"
            "  const el = document.getElementById(id);"
            "  if (el) out.roots.push(id + ':vis=' + (el.getClientRects().length > 0)"
            "    + (el.shadowRoot ? '+shadow' : ''));"
            "}"
            "document.querySelectorAll('*').forEach(el => {"
            "  if (el.shadowRoot) out.shadowHosts.push((el.id || el.className || el.tagName).toString().slice(0, 30));"
            "});"
            "return JSON.stringify(out);"
            "})();"
        )
        print(f"🔎 彈窗 DOM 診斷: {info}")
    except Exception as e:
        print(f"🔎 彈窗 DOM 診斷失敗: {e}")


# 關閉 OneTrust / Google CMP 私隱彈窗。
# 實證（2026-09-13 run#31/32 OCR）：彈窗會疊在 Turnstile 正上方，
# uc_gui_click_captcha 按座標點擊打在彈窗上，captcha 永遠點不中（間歇性失敗根源）。
# 優先按真實用戶動作點「Reject All」，DOM 移除只作兜底。
def dismiss_consent_popup(sb) -> bool:
    for sel in (
        '#onetrust-reject-all-handler',   # Reject all（不牽涉任何同意，最中性）
        '#onetrust-accept-btn-handler',   # Accept all（部分站點只有這個）
        '#onetrust-close-btn-container',  # 右上 X
    ):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel, timeout=3)
                sb.sleep(1)
                print(f"🍪 已關閉私隱彈窗（{sel}）")
                return True
        except Exception:
            pass
    # Google CMP / Funding Choices：個 UI 藏喺 #fc-consent-root 嘅 shadow DOM，
    # 普通 selector 永遠搵唔到（run#22 OCR 實錘：彈窗蓋住 Turnstile 剷極唔走）。
    # 遞迴走入所有 shadowRoot 搵掣撳：先 Reject/Do not consent，冇先 Accept。
    # IIFE wrap 必須（run#25 實錘）：clicker 嘅 disconnect/reconnect cycle 後，
    # CDP evaluate 進入非 wrap 模式，頂層 return → SyntaxError: Illegal return
    # statement —— 27 次連環失敗，掣其實一次都冇撳過，彈窗返嚟遮住 captcha。
    try:
        clicked = sb.execute_script(
            "return (() => {"
            "const deepAll = (root, sel) => {"
            "  const out = [];"
            "  const walk = (node) => {"
            "    if (!node || !node.querySelectorAll) return;"
            "    node.querySelectorAll(sel).forEach(el => out.push(el));"
            "    node.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });"
            "  };"
            "  walk(root);"
            "  return out;"
            "};"
            "const btns = deepAll(document, 'button, [role=button]');"
            "const prefs = [/do not consent/i, /reject all/i, /reject/i, /accept all/i, /accept/i];"
            "for (const re of prefs) {"
            "  for (const b of btns) {"
            "    const t = ((b.textContent || '') + ' ' + (b.className || '')).trim();"
            "    if (re.test(t) && b.getClientRects().length > 0) {"
            "      b.click();"
            "      return t.slice(0, 40);"
            "    }"
            "  }"
            "}"
            "return null;"
            "})();"
        )
        if clicked:
            sb.sleep(1)
            print(f"🍪 已關閉 Google CMP 私隱彈窗（撳咗「{clicked}」）")
            return True
    except Exception as e:
        print(f"⚠️ Google CMP shadow-DOM 撳掣失敗: {e}")
    # 兜底：彈窗還在就移走遮擋（只動 CMP 容器，不碰 Turnstile 本身）
    try:
        removed = sb.execute_script(
            "return (() => {"
            "let n = 0;"
            "for (const id of ['onetrust-consent-sdk','onetrust-banner-sdk','onetrust-pc-sdk']) {"
            "  const el = document.getElementById(id);"
            "  if (el) { el.remove(); n++; }"
            "}"
            "return n;"
            "})();"
        )
        if removed:
            print(f"🍪 移除了 {removed} 個私隱彈窗容器（DOM 兜底）")
            return True
    except Exception:
        pass
    # Google CMP 容器兜底：撳唔到掣就直接剷走 host 容器
    try:
        removed_fc = sb.execute_script(
            "return (() => {"
            "let n = 0;"
            "for (const el of document.querySelectorAll("
            "  '#fc-consent-root, #fc-consent-root-inner, .fc-consent-root, .fc-dialog-overlay')) {"
            "  el.remove(); n++;"
            "}"
            "return n;"
            "})();"
        )
        if removed_fc:
            print(f"🍪 移除了 {removed_fc} 個 Google CMP 彈窗容器（DOM 兜底）")
            return True
    except Exception:
        pass
    return False


# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=60):
    """判断 Turnstile 是否通过（v2：铁证判据，唔再靠頁面文字）。

    舊實現嘅兩個假陽性來源（2026-09-13 run#31/32 實證）：
    1) widget 內部文字（"verify you are human"）在 cross-origin iframe 裡，
       頂層 get_page_source() 永遠睇唔到 →「無挑戰」假陽性；
    2) iframe 加載寬限期內唔見 iframe 就當無挑戰 → 但 widget 可能仲未加載完。

    新判據（三態）：
    - solved：cf-turnstile-response token 存在且非空（唯一鐵證）
    - present：turnstile iframe 存在 → 等它被解決
    - absent：無 widget 無 token → 真無挑戰
    """
    start = time.time()

    # Step 1: 寬限期等 widget 出現（run#17 實證：芬蘭代理下 iframe 可遲過 27s
    # 先 render，12s grace 會假陰性「頁面無挑戰」→ 掣永遠鎖死）。
    # 每輪先剷走私隱彈窗；寬限期內每逢第 5 輪 dump 一次現場。
    iframe_seen = False
    grace = min(25, timeout)
    diag_i = 0
    while time.time() - start < grace:
        dismiss_consent_popup(sb)
        if _turnstile_iframe_present(sb) or _turnstile_widget_present(sb):
            iframe_seen = True
            print("🔍 Turnstile 挑戰已出現（iframe/widget 容器）...")
            break
        if _turnstile_solved(sb):
            # widget 未見但 token 已有（invisible 模式）—— 直接算過
            print("✅ Turnstile 驗證已通過（token 已存在）")
            return True
        if diag_i % 5 == 4:
            sb.save_screenshot(f"diag_grace_{diag_i}.png")
            _dump_turnstile_dom(sb)
        diag_i += 1
        sb.sleep(1)

    if not iframe_seen:
        btn = _renew_button_state(sb)
        if btn == "unlocked":
            # 無 widget 而掣已解鎖 —— 真無挑戰（或已通過）
            print("✅ Turnstile 驗證已通過（掣已解鎖，無需挑戰）")
            return True
        if btn == "locked":
            # 掣鎖住 = 頁面明寫有 bot check —— widget render 慢唔等於冇挑戰
            # （run#17/23 教訓：假陰性判「無挑戰」→ 掣永遠鎖死）→ 硬等
            iframe_seen = True
            print("⚠️ 寬限期未見 widget，但掣鎖住 → 判定挑戰存在，硬等")
        else:
            sb.save_screenshot("diag_no_button_no_widget.png")
            _dump_turnstile_dom(sb)
            print("⚠️ 未見 widget 亦未見掣 —— 彈窗可能未開，繼續硬等 render")
            iframe_seen = True

    # Step 2: 見到挑戰 → 撳 captcha，等 token / 掣解鎖（唔超時就重試撳）
    # clicker 用 SB 內建 sb.uc_gui_click_captcha()（自己搵 debugger port + widget
    # 座標）—— v4 run#18 實證 `python -m uc_gui_click_captcha` module 根本唔存在，
    # capture_output 吞晒錯誤靜默失敗；01 倉 subprocess 寫法同理從未真正撳過。
    captcha_i = 0
    while time.time() - start < timeout:
        dismiss_consent_popup(sb)
        # context 防護：clicker 撳完可以將 driver 留低喺 turnstile iframe
        # （cross-origin）入面 —— 之後所有 execute_script 打喺錯誤頁面。
        # 每次撳之前先切返頂層 document。
        try:
            sb.switch_to_default_content()
        except Exception:
            pass
        if _turnstile_solved(sb):
            press_time = time.time()
            print(f"✅ Turnstile 驗證已通過（耗時 {press_time - start:.0f}s）")
            sb.save_screenshot("turnstile_passed.png")
            return True
        if _renew_button_unlocked(sb):
            print("🔓 Renew 按鈕已解鎖（bot check 已通過）")
            return True
        captcha_i += 1
        if IS_X11:
            try:
                sb.uc_gui_click_captcha()
                print(f"🖱️ captcha clicker 第 {captcha_i} 次：已撳出")
            except Exception as e:
                print(f"⚠️ uc_gui_click_captcha 失败: {str(e)[:150]}")
                if captcha_i % 3 == 0:
                    sb.save_screenshot(f"diag_captcha_fail_{captcha_i}.png")
                    _dump_turnstile_dom(sb)
        sb.sleep(5)

    # 最後機會輪詢：run#19 實證——clicker 撳中咗 captcha（截圖 Success!），
    # 但 token／掣解鎖喺超時後幾秒先反映到 DOM，超時即跳太早。多等 15s 補救。
    for _lastchance in range(5):
        if _turnstile_solved(sb) or _renew_button_unlocked(sb):
            print("✅ Turnstile 驗證已通過（最後機會輪詢）")
            return True
        sb.sleep(3)

    print(f"❌ Turnstile 验证超时未通过（掣狀態: {_renew_button_state(sb)}）")
    _dump_popup_dom(sb)
    _dump_turnstile_dom(sb)
    sb.save_screenshot("turnstile_timeout.png")
    return False
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except (ValueError, IndexError):
        return countdown_str

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",   # Expires 2026/07/07
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",   # Expires 07/07/2026 (MM/DD/YYYY)
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",        # 2026/07/07 - renew
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",        # 07/07/2026 - renew
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days", # 2026/07/07 - renew manually to extend for 4 days
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            # 如果是 MM/DD/YYYY 格式，转换为 YYYY/MM/DD
            if len(date_str.split('/')[-1]) == 4:  # 年份长度4
                parts = date_str.split('/')
                if len(parts[0]) == 2:  # 第一部分是2位（月）
                    # 修正：将 MM/DD/YYYY 转为 YYYY/MM/DD
                    return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None

#   Discord OAuth 登录（SESSION_TOKEN 失效时的备用方案）
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """用 DC_TOKEN 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )

    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    # 如果配置了代理，Discord API 请求也走代理
    proxies = None
    _is_proxy = env.get("IS_PROXY").lower() == "true"
    _proxy_server = env.get("PROXY_SERVER") or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb) -> bool:
    """通过 Discord Token 走完整 OAuth 流程登录 bot-hosting.net"""
    print("\n🔑 通过 Discord Token 登录...")

    state = capture_discord_state(sb)
    if not state:
        sb.save_screenshot("login_no_state.png")
        return False

    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# 主流程
def run_all() -> RenewReport:
    """跑一轮续期，把结果收进 RenewReport。

    所有出口都是 ``return report``（原来散落的 sys.exit(1/2/4/5/3|6) 已收敛），
    退出码由 RenewReport.exit_code 统一决定：只有 FAILED 才是 1。
    """
    report = RenewReport(service=SERVICE)
    # ⚠️ 变量名故意不叫 name：下面注入 cookie 那段有
    #    `for name, value in COOKIES.items()`，会把 name 覆盖成最后一个 key
    #    （"theme"），报告名就会全变成「theme」。改叫 target 把这个坑堵死。
    target = _target_name()

    if not SESSION_TOKEN and not DC_TOKEN:
        # 原版在模块顶层直接 sys.exit(1)：既没通知也没报告，日志只有一行 print。
        # 现在走 FAILED → 会通知 + 标红，诊断信息完整。
        _record(report, target, "❌ 登录失败",
                error="未配置 SESSION_TOKEN 和 DISCORD_TOKEN，无法登录")
        return report

    IS_PROXY = env.get("IS_PROXY").lower() == "true"
    PROXY_SERVER = env.get("PROXY_SERVER") or "http://127.0.0.1:1080"
    HEADLESS = env.get("HEADLESS").lower() == "true"

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    try:
        with SB(**sb_kwargs) as sb:
            try:
                ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
                print(f"📍 当前出口IP: {ip}")
            except Exception as e:
                print(f"⚠️ 获取出口 IP 失败: {e}")

            login_ok = False

            # 方式1: SESSION_TOKEN Cookie 登录（默认）
            if SESSION_TOKEN:
                print("🚀 启动浏览器...")
                sb.open("https://bot-hosting.net/")
                sb.wait_for_ready_state_complete()
                sb.sleep(2)

                print("📝 注入 Cookie...")
                for name, value in COOKIES.items():
                    if value:
                        sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                # CF 擋截頁兜底：URL 對但 Title 係「Access denied」= Cloudflare 瞬間擋截
                # （02 run#14 實證），唔係 cookie 問題 —— reload 重試最多 3 次先判死。
                for _retry in range(3):
                    if "/a/billings" in current_url and "Access denied" not in current_title and "Just a moment" not in current_title:
                        break
                    print(f"⚠️ 檢測到 CF 擋截/挑戰頁（Title: {current_title}），第 {_retry+1} 次 reload 重試...")
                    sb.sleep(8)
                    sb.open("https://bot-hosting.net/a/billings")
                    sb.wait_for_ready_state_complete()
                    sb.sleep(5)
                    current_url = sb.get_current_url()
                    current_title = sb.get_title()
                    print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url and "Access denied" not in current_title:
                    login_ok = True
                    print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
                    sb.save_screenshot("logged_in_token.png")
                else:
                    print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}, 当前标题: {current_title}")

            # 方式2: Discord OAuth 登录（备用）
            if not login_ok and DC_TOKEN:
                _LOGIN_METHOD = "Discord Token"
                print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
                if do_discord_login(sb):
                    print("🌐 访问 https://bot-hosting.net/a/billings ...")
                    sb.open("https://bot-hosting.net/a/billings")
                    sb.wait_for_ready_state_complete()
                    sb.sleep(3)
                    current_url = sb.get_current_url()
                    current_title = sb.get_title()
                    print(f"📝 当前URL: {current_url}, Title: {current_title}")

                    if "a/billings" in current_url:
                        login_ok = True
                        print("✅ Discord OAuth 登录成功,当前已到达账单页")
                    else:
                        print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
                else:
                    print("❌ Discord OAuth 登录失败")

            if not login_ok:
                error_msg = "Cookie 已失效或页面异常"
                if not SESSION_TOKEN and DC_TOKEN:
                    error_msg = "Discord OAuth 登录失败"
                elif SESSION_TOKEN and DC_TOKEN:
                    error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
                sb.save_screenshot("login_failed.png")
                _record(report, target, "❌ 登录失败", error=error_msg)
                return report

            if _LOGIN_METHOD == "Discord Token":
                print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新到 Secrets")

            # 提取当前到期日期
            sb.sleep(2)
            page_source = sb.get_page_source()
            current_expiry = extract_expiry_date(page_source)
            if current_expiry:
                print(f"📅 当前到期日期: {current_expiry}")
            else:
                print("⚠️ 未能提取当前到期日期")

            # 寻找外部续期按钮
            outer_renew_selector = None
            countdown_text = None
            possible_selectors = [
                'button:contains("Renew")',
                'button:contains("Renew free plan")',
                'a:contains("Renew")',
                '[class*="renew"]',
                '[class*="Renew"]',
            ]

            for selector in possible_selectors:
                try:
                    if sb.is_element_visible(selector):
                        button_text = sb.get_text(selector)
                        if "Renew in" in button_text:
                            match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                            if match:
                                countdown_text = match.group(1)
                            break
                        elif "Renew" in button_text and "in" not in button_text.lower():
                            outer_renew_selector = selector
                            print(f"✅ 续期按钮可用: '{button_text}'")
                            break
                except Exception as e:
                    pass

            # 点击外部续期按钮等待弹窗
            if outer_renew_selector:
                print("🔄 点击外部续期按钮，等待验证窗口...")
                try:
                    sb.sleep(2)
                    sb.save_screenshot("before_renew_click.png")
                    sb.click(outer_renew_selector)
                    # 等弹窗里的 turnstile widget 或确认按钮出现，取代固定 sleep(15)
                    try:
                        sb.wait_for_element_visible(
                            'iframe[src*="turnstile"], button:contains("Renew for 4 days")',
                            timeout=20,
                        )
                        sb.sleep(3)
                    except Exception as we:
                        print(f"⚠️ 等待弹窗元素超时（弹窗内可能无 turnstile）: {we}")
                except Exception as e:
                    print(f"❌ 点击外部按钮失败: {e}")
                    sb.save_screenshot("click_outer_failed.png")
                    _record(report, target, "❌ 续期失败",
                            error=f"点击外部续期按钮出错（{e}）")
                    return report

                # 处理弹窗中的 Turnstile
                print("🔒 检测弹窗中的 Turnstile 验证...")
                _dump_popup_dom(sb)  # run#22 教訓：留低彈窗現場結構，方便對症落藥
                # 舊邏輯「先 uc_gui_click_captcha() 再判」打唔中就純粹靠運氣（run#31/32 實證）；
                # v2 已內建「剷 OneTrust 彈窗 → 撳 captcha → 等 token」重試，直接調用即可。
                turnstile_passed = wait_for_turnstile_pass(sb, timeout=240)

                if not turnstile_passed:
                    print("❌ Turnstile 验证最终未通过，脚本退出")
                    sb.save_screenshot("turnstile_final_fail.png")
                    _record(report, target, "❌ 续期失败", error="Turnstile 验证未通过")
                    return report

                # 点击续期按钮
                print("⏳ 等待弹窗续期按钮可用并点击...")
                # 撳掣前鐵證確認：bot check 未過（掣鎖住）就撳，後台一定唔受理，
                # 90 秒輪詢必然等唔到 →「已點擊但未確認」假失敗（run#31/32 教訓）。
                unlock_wait = 0
                while not _renew_button_unlocked(sb) and unlock_wait < 30:
                    dismiss_consent_popup(sb)
                    # v4/v6 實證：subprocess `python -m uc_gui_click_captcha` module
                    # 唔存在（run#18 No module named）—— SB 內建 method 先啱。
                    try:
                        sb.switch_to_default_content()
                    except Exception:
                        pass
                    if IS_X11:
                        try:
                            sb.uc_gui_click_captcha()
                        except Exception:
                            pass
                    sb.sleep(4)
                    unlock_wait += 4
                if not _renew_button_unlocked(sb):
                    print("❌ 续期按钮仍处于锁定状态（bot check 未通过），放弃点击")
                    _dump_popup_dom(sb)
                    sb.save_screenshot("button_still_locked.png")
                    _record(report, target, "❌ 续期失败",
                            error="Bot check 未通过，按钮仍锁定")
                    return report
                print("✅ 撳掣前確認：按鈕已解鎖（bot check 已通過）")
                try:
                    sb.wait_for_element_visible('button:contains("Renew for 4 days")', timeout=15)
                except Exception as we:
                    print(f"⚠️ 等待弹窗续期按钮超时: {we}")

                modal_button_clicked = False
                click_error = ""
                try:
                    sb.save_screenshot("before_modal_confirm.png")
                    sb.click('button:contains("Renew for 4 days")', timeout=8)
                    modal_button_clicked = True
                    print("✅ 已点击续期按钮")
                except Exception as e:
                    print(f"续期按钮点击失败: {e}")
                    click_error = str(e)[:120].replace("\n", " ")
                    sb.save_screenshot("modal_confirm_failed.png")
                    # JS 兜底：选择器点不动（被遮罩挡住/按钮被重渲染）时直接 DOM 派发 click
                    try:
                        clicked = sb.execute_script(
                            "for (const b of document.querySelectorAll('button')) {"
                            " if (b.textContent.includes('Renew for 4 days')) { b.click(); return true; }"
                            " } return false;"
                        )
                        if clicked:
                            modal_button_clicked = True
                            print("🧟 JS 兜底点击已发出")
                    except Exception as je:
                        print(f"❌ JS 兜底点击也失败: {je}")

                print("⏳ 等待后台确认续期（最多 90 秒，轮询到期日期/成功提示）...")
                # 原版只等 6 秒，页面/API 未及时刷新便误报“结果未知”。
                # 轮询页面文字及到期日期；最后再整页重载一次，避免读取旧 DOM。
                new_page_text = ""
                new_expiry = None
                new_countdown = None
                renewal_confirmed = False
                toast_hint = ""
                success_markers = (
                    "renewal successful", "renewed successfully", "successfully renewed",
                    "续期成功", "renewed for 4 days", "renew for 4 days"
                )
                for poll in range(1, 19):
                    sb.sleep(5)
                    new_page_text = sb.get_page_source()
                    new_expiry = extract_expiry_date(new_page_text)
                    new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
                    new_countdown = new_match.group(1) if new_match else None
                    lowered = new_page_text.lower()
                    if (new_expiry and new_expiry != current_expiry) or any(
                        marker in lowered for marker in success_markers[:-2]
                    ):
                        renewal_confirmed = True
                        print(f"✅ 第 {poll} 次检查确认续期已生效")
                        break
                    # 顺手抓一次性提示（toast/alert），后台拒绝时能看到原因
                    if not toast_hint:
                        for sel in ('[role="alert"]', '.toast', '.Toastify',
                                    '[class*="notif"]', '[class*="alert"]', '.swal2-popup'):
                            try:
                                if sb.is_element_present(sel):
                                    t = " ".join(sb.get_text(sel).split())
                                    if t and len(t) < 160:
                                        toast_hint = t
                                        print(f"💬 页面提示: {t}")
                                        break
                            except Exception:
                                pass
                    print(f"⏳ 第 {poll}/18 次检查：页面尚未确认续期")

                if not renewal_confirmed:
                    print("🔄 重新打开账单页作最后确认（整页导航，绕开面板缓存）...")
                    try:
                        sb.open("https://bot-hosting.net/a/billings")
                        sb.wait_for_ready_state_complete()
                        sb.sleep(5)
                        new_page_text = sb.get_page_source()
                        new_expiry = extract_expiry_date(new_page_text)
                        new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
                        new_countdown = new_match.group(1) if new_match else None
                        lowered = new_page_text.lower()
                        renewal_confirmed = bool(
                            (new_expiry and new_expiry != current_expiry) or
                            any(marker in lowered for marker in success_markers[:-2])
                        )
                    except Exception as e:
                        print(f"⚠️ 刷新确认失败: {e}")

                if renewal_confirmed:
                    print("✅ 续期成功！")
                    if new_countdown:
                        print(f"⏱️ 新的倒计时: {new_countdown}")
                    if new_expiry:
                        print(f"📅 新的到期日期: {new_expiry}")
                    _record(
                        report, target, "✅ 续期成功",
                        extra=(f"可续期时间: {format_countdown(new_countdown)}后"
                               if new_countdown else "到期日期已确认更新"),
                        expiry=new_expiry or current_expiry,
                    )
                else:
                    # 关键：不能只发警告后 return，否则 Actions 会显示 success。
                    print("❌ 续期未能确认：到期日期/成功提示均未变化")
                    sb.save_screenshot("renew_result_unknown.png")
                    # 如实区分：到底点没点到按钮，不能再笼统说“已点击”
                    if modal_button_clicked:
                        extra = (f"按钮已点击但后台未确认"
                                 f"（{current_expiry or '?'} → {new_expiry or '?'}），"
                                 f"可能续得太早被拒，明日窗口临近会自动重试")
                    else:
                        extra = (f"弹窗内「Renew for 4 days」按钮没点着"
                                 f"（{click_error or '未知原因'}），需人工检查")
                    if toast_hint:
                        extra += f"；页面提示: {toast_hint}"
                    if new_countdown:
                        extra += f"；按钮已转入倒计时 {new_countdown}"
                    # 原版这里 sys.exit(3 或 6)：已点击未确认 / 按钮没点着。
                    # 收敛后两者都是 FAILED → exit 1，Actions 一样标红；区别落在
                    # detail 文字里（见文件头第 4 点：这是有意保留的偏离）。
                    _record(report, target, "❌ 续期失败", extra=extra,
                            expiry=new_expiry or current_expiry)
                    return report

            else:
                if countdown_text:
                    friendly = format_countdown(countdown_text)
                    print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                    _record(report, target, "⏳ 未到续期时间",
                            extra=f"可续期时间: {friendly}后",
                            expiry=current_expiry)
                else:
                    # 兜底：從 page source 直接提取倒數（02 run#13 實證：續完後掣會轉做
                    # 倒數計時器，selector get_text 可能 miss —— 唔好直接判「狀態未知」）
                    src = sb.get_page_source()
                    m = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", src)
                    if m:
                        countdown_text = m.group(1)
                        friendly = format_countdown(countdown_text)
                        print(f"⏳ 未到续期时间（兜底提取），倒计时: {countdown_text} ({friendly})")
                        _record(report, target, "⏳ 未到续期时间",
                                extra=f"可续期时间: {friendly}后",
                                expiry=current_expiry)
                    else:
                        print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                        _record(report, target, "ℹ️ 无需续期",
                                extra="未找到续期按钮或倒计时，状态未知",
                                expiry=current_expiry)

            # 更新SESSION_TOKEN 
            print("🔄 检查 SESSION_TOKEN 是否需要更新")
            new_token, token_expiry = get_cookie_info(sb, "session_token")
            old_token = SESSION_TOKEN

            if should_update_cookie(new_token, old_token, token_expiry):
                print("🔄 SESSION_TOKEN 需要更新")
                if env.dry_run():
                    # DRY_RUN 只挡「写真实 Secret」这一件有副作用的事：演练时
                    # 照样登录、照样读 cookie，但不把新 token 推上 GitHub。
                    print(f"ℹ️ DRY_RUN 演练，跳过回写 Secret（新值 {new_token[:4]}...{new_token[-4:]}）")
                elif GH_TOKEN:
                    if update_github_secret("SESSION_TOKEN", new_token):
                        print("✅ SESSION_TOKEN 更新成功")
                    else:
                        print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
                else:
                    print("⚠️ 未设置 GH_TOKEN，无法自动更新")
                    print(f"📋 请手动设置 SESSION_TOKEN = {new_token[:4]}...{new_token[-4:]}")
            else:
                print("✅ SESSION_TOKEN 无需更新")
        
            print("🏁 脚本执行完毕")
    except Exception as e:
        # 原代码这个 try 没有 except，Selenium 一崩就直接抛栈退出：
        # 没截图、没通知，日志只剩一截 traceback，等于零诊断信息。
        traceback.print_exc()
        try:
            sb.save_screenshot("fatal_error.png")
        except Exception:
            pass
        _record(report, target, "❌ 脚本异常中断", error=f"{type(e).__name__}: {e}")
    return report


def main() -> int:
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    try:
        report = run_all()
    except Exception as exc:            # run_all 内部已兜底；这里防的是它自己出意外
        traceback.print_exc()
        report = RenewReport(service=SERVICE)
        report.add(_target_name(), Outcome.FAILED,
                   detail=f"{type(exc).__name__}: {shorten(str(exc), 200)}")

    # 默认静默 SKIPPED（见文件头第 3 点）；AUT0_NOTIFY_SKIP=1 恢复每日心跳。
    notify_tg = env.dry_run("AUT0_NOTIFY_SKIP") or any(
        r.outcome not in QUIET_OUTCOMES for r in report.results)
    return report.finish(notify_tg=notify_tg)


if __name__ == "__main__":
    sys.exit(main())
