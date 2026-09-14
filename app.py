#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 
ACCOUNT_LABEL = os.environ.get("ACCOUNT_LABEL") or ""   # 帳號標識（如 "01"/"02"），通知用嚟區分多個帳號

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知）
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

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    _acct = f" {ACCOUNT_LABEL}" if ACCOUNT_LABEL else ""
    lines = [
        f"🇫🇮 Bot-hosting{_acct} 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
# 检查页面是否存在 Turnstile iframe（无隐式等待）
def _turnstile_iframe_present(sb) -> bool:
    try:
        return len(sb.find_elements('iframe[src*="turnstile"]')) > 0
    except Exception:
        return False


# X11 環境（GHA 用 xvfb-run 跑，有 DISPLAY）才有 uc_gui_click_captcha 可用
IS_X11 = bool(os.environ.get("DISPLAY"))


# Turnstile 已解決的鐵證：cf-turnstile-response input 存在且 value 非空。
# widget 內部文字在 cross-origin iframe 裡，頂層 get_page_source() 根本看不見，
# 舊「整頁無 CF 關鍵字」判據因此永遠假陽性 —— captcha 未過就以為過了。
def _turnstile_solved(sb) -> bool:
    try:
        return bool(sb.execute_script(
            "for (const el of document.querySelectorAll('[name=\"cf-turnstile-response\"]')) {"
            "  if (el.value && el.value.length > 20) return true;"
            "}"
            "return false;"
        ))
    except Exception:
        return False


# 「Renew for 4 days」按鈕是否已解鎖（bot check 已通過）。
# 頁面文案明寫 "Complete the bot check to unlock the button"：
# 掣未解鎖時 click() 不會報錯，只會被後端忽略 —— 這正是「已點擊但未確認」假失敗的來源。
def _renew_button_unlocked(sb) -> bool:
    try:
        found = sb.execute_script(
            "let found = false;"
            "for (const b of document.querySelectorAll('button')) {"
            "  if (b.textContent.includes('Renew for 4 days')) {"
            "    found = true;"
            "    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'locked';"
            "    return 'unlocked';"
            "  }"
            "}"
            "return found ? 'unlocked' : 'missing';"
        )
        if found == "unlocked":
            return True
        # 掣搵唔到：彈窗可能還沒渲染完，或被遮擋 —— 視為未過，等下一輪
        return False
    except Exception:
        return False


# 「Renew for 4 days」掣三態（v3 新增）：unlocked / locked / missing。
# wait_for_turnstile_pass 寬限期結束後用佢判「真無挑戰」定「假陰性」。
def _renew_button_state(sb) -> str:
    try:
        return sb.execute_script(
            "let found = false;"
            "for (const b of document.querySelectorAll('button')) {"
            "  if (b.textContent.includes('Renew for 4 days')) {"
            "    found = true;"
            "    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'locked';"
            "    return 'unlocked';"
            "  }"
            "}"
            "return 'missing';"
        )
    except Exception:
        return "missing"


# Turnstile widget 容器（iframe 未開時嘅宿主 div）—— iframe 遲 render 場景嘅
# 第二信號源（run#17 實證 iframe 可以遲過 27s 先出現）。
def _turnstile_widget_present(sb) -> bool:
    try:
        return bool(sb.execute_script(
            "for (const el of document.querySelectorAll('[class*=\"turnstile\"],[id*=\"turnstile\"],[data-sitekey]')) {"
            "  if (el && el.offsetParent !== null) return true;"
            "}"
            "return false;"
        ))
    except Exception:
        return False


# 診斷 dump：彈窗內 Turnstile 相關 DOM 節點清單（唔掂嗰陣起碼知頁面生咗乜）
def _dump_turnstile_dom(sb) -> None:
    try:
        nodes = sb.execute_script(
            "const out = [];"
            "for (const el of document.querySelectorAll('iframe,[class*=\"turnstile\"],[class*=\"cf\"],[data-sitekey],#onetrust-consent-sdk')) {"
            "  out.push(el.tagName + '#' + (el.id || '-') + '.' + String(el.className).slice(0,60) + ' vis=' + (el.offsetParent !== null));"
            "}"
            "return out.slice(0, 20).join('\\n');"
        )
        print(f"🧬 Turnstile DOM 快照:\n{nodes or '(無相關節點)'}")
    except Exception as e:
        print(f"🧬 DOM dump 失败: {e}")


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
    # 兜底：彈窗還在就移走遮擋（只動 CMP 容器，不碰 Turnstile 本身）
    try:
        removed = sb.execute_script(
            "let n = 0;"
            "for (const id of ['onetrust-consent-sdk','onetrust-banner-sdk','onetrust-pc-sdk']) {"
            "  const el = document.getElementById(id);"
            "  if (el) { el.remove(); n++; }"
            "}"
            "return n;"
        )
        if removed:
            print(f"🍪 移除了 {removed} 個私隱彈窗容器（DOM 兜底）")
            return True
    except Exception:
        pass
    return False


# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=60):
    """判断 Turnstile 是否通过（v2：铁证判据，唔再靠頁面文字）。

    舊實現：只搜整頁文字，但 widget 文字在 cross-origin iframe 裡頂層睇唔到，
    永遠假陽性 —— captcha 未過就以為過了（2026-09-13 實證）。

    新判據（三態）：
    - solved：cf-turnstile-response token 存在且非空（唯一鐵證）
    - present：turnstile iframe 存在 → 等它被解決
    - absent：無 widget 無 token → 真無挑戰
    """
    start = time.time()

    # Step 1: 寬限期等 widget 出現（run#17 實證：芬蘭代理下 iframe 可遲過 27s
    # 先 render，12s grace 會假陰性「頁面無挑戰」→ 掣永遠鎖死）。
    # 修正：寬限期內每 5s dump 一次現場（screenshot + DOM 節點清單），
    # 而且只要「Renew for 4 days」掣存在且鎖住 → 必有 bot check，唔准判 absent。
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
            # 掣鎖住 = 頁面明寫有 bot check —— 假陰性修復（run#17 教訓）：
            # widget render 慢唔等於冇挑戰，硬等，絕不判「頁面無挑戰」
            iframe_seen = True
            print("⚠️ 寬限期未見 widget，但掣鎖住 → 判定挑戰存在，硬等")
        else:
            sb.save_screenshot("diag_no_button_no_widget.png")
            _dump_turnstile_dom(sb)
            print("⚠️ 未見 widget 亦未見掣 —— 彈窗可能未開，繼續硬等 render")
            iframe_seen = True



    # Step 2: 見到挑戰 → 撳 captcha，等 token / 掣解鎖（唔超時就重試撳）
    captcha_i = 0
    while time.time() - start < timeout:
        dismiss_consent_popup(sb)
        if _turnstile_solved(sb):
            print("✅ Turnstile 驗證已通過")
            sb.save_screenshot("turnstile_passed.png")
            return True
        if _renew_button_unlocked(sb):
            print("🔓 Renew 按鈕已解鎖（bot check 已通過）")
            return True
        captcha_i += 1
        if IS_X11:
            # run#17 教訓：capture_output=True 吞晒輸出，8 次即閃即退都唔知原因
            # —— 改為 tee 出嚟，每次撳完即報狀態
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "uc_gui_click_captcha", "--override", "127.0.0.1:9223"],
                    timeout=40, capture_output=True, text=True,
                )
                out = (r.stdout or "").strip()[-200:]
                err = (r.stderr or "").strip()[-200:]
                print(f"🖱️ captcha clicker 第 {captcha_i} 次 rc={r.returncode}"
                      + (f" out: {out}" if out else "")
                      + (f" err: {err}" if err else ""))
                if r.returncode != 0 and captcha_i == 3:
                    sb.save_screenshot(f"diag_captcha_fail_{captcha_i}.png")
                    _dump_turnstile_dom(sb)
            except Exception as e:
                print(f"⚠️ uc_gui_click_captcha 失败: {e}")
        sb.sleep(5)

    print("❌ Turnstile 验证超时未通过")
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
    except:
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
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
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
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

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
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

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
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            print("🔒 检测弹窗中的 Turnstile 验证...")
            # 舊邏輯「先 uc_gui_click_captcha() 再判」打唔中就純粹靠運氣（run#31/32 實證）；
            # v2 已內建「剷 OneTrust 彈窗 → 撳 captcha → 等 token」重試，直接調用即可。
            turnstile_passed = wait_for_turnstile_pass(sb, timeout=90)

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                sb.save_screenshot("turnstile_final_fail.png")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # 点击续期按钮
            print("⏳ 等待续期按钮可用并点击...")
            # 撳掣前鐵證確認：bot check 未過（掣鎖住）就撳，後台一定唔受理，
            # 90 秒輪詢必然等唔到 →「已點擊但未確認」假失敗（run#31/32 教訓）。
            # run#17 教訓：芬蘭代理下 widget 27s 先 render，30s 窗口 8 次撳掣全
            # 打喺 OneTrust 彈窗上就時間到了 —— 擴到 120s 俾足 render＋重撳空間。
            unlock_wait = 0
            while not _renew_button_unlocked(sb) and unlock_wait < 120:
                dismiss_consent_popup(sb)
                try:
                    if IS_X11:
                        subprocess.run(
                            [sys.executable, "-m", "uc_gui_click_captcha", "--override", "127.0.0.1:9223"],
                            timeout=40, capture_output=True,
                        )
                except Exception:
                    pass
                sb.sleep(4)
                unlock_wait += 4
            if not _renew_button_unlocked(sb):
                print("❌ 续期按钮仍处于锁定状态（bot check 未通过），放弃点击")
                sb.save_screenshot("button_still_locked.png")
                send_telegram_message(format_notification(
                    "❌ 续期失败", error="Bot check 未通过，按钮仍锁定"))
                return
            print("✅ 撳掣前確認：按鈕已解鎖（bot check 已通過）")
            time.sleep(1)

            modal_button_clicked = False
            click_error = ""
            try:
                sb.click('button:contains("Renew for 4 days")', timeout=8)
                modal_button_clicked = True
                print("✅ 已点击续期按钮")
            except Exception as e:
                print(f"续期按钮点击失败: {e}")
                click_error = str(e)[:120].replace("\n", " ")
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
            # 轮询页面文字及到期日期；最后再整页导航一次，避免读取面板缓存旧值。
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
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=(f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
                               if new_countdown else "到期日期已确认更新"),
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                # 关键修复：不能只发警告后 return 0，否则 Actions 会显示 success。
                print("❌ 续期未能确认：到期日期/成功提示均未变化")
                try:
                    sb.save_screenshot("renew_result_unknown.png")
                except Exception:
                    pass
                # 如实区分：到底点没点到按钮，不能再笼统说“已点击”
                if modal_button_clicked:
                    extra = (f"按钮已点击但后台未确认"
                             f"（{current_expiry or '?'} → {new_expiry or '?'}），"
                             f"可能续得太早被拒或同账号另一工作流先续了，明日窗口临近会自动重试")
                else:
                    extra = (f"弹窗内「Renew for 4 days」按钮没点着"
                             f"（{click_error or '未知原因'}），需人工检查")
                if toast_hint:
                    extra += f"；页面提示: {toast_hint}"
                if new_countdown:
                    extra += f"；按钮已转入倒计时 {new_countdown}"
                send_telegram_message(
                    format_notification(
                        "❌ 续期失败",
                        extra=extra,
                        expiry_date=new_expiry or current_expiry or "（未获取到）"
                    )
                )
                raise RuntimeError("renewal was clicked but could not be confirmed")

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(
                    format_notification(
                        "⏳ 未到续期时间",
                        extra=f"⏱️ 可续期时间: {friendly}后",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                # 兜底：從 page source 直接提取倒數（run#13 實證：續完後掣會轉做
                # 倒數計時器，selector get_text 可能因遮擋/渲染 miss —— 唔好直接判「狀態未知」）
                src = sb.get_page_source()
                m = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", src)
                if m:
                    countdown_text = m.group(1)
                    friendly = format_countdown(countdown_text)
                    print(f"⏳ 未到续期时间（兜底提取），倒计时: {countdown_text} ({friendly})")
                    send_telegram_message(
                        format_notification(
                            "⏳ 未到续期时间",
                            extra=f"⏱️ 可续期时间: {friendly}后",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )
                else:
                    print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                    send_telegram_message(
                        format_notification(
                            "ℹ️ 无需续期",
                            extra="当前状态未知，请手动检查",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )

        # 更新SESSION_TOKEN 
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
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

if __name__ == "__main__":
    main()
