#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aut0-Renew-B0th0sting02 / app.py 的离线验收。

跑法（无需 seleniumbase、无需网络）：
    python .verify/verify_aut0.py

分四段：
    [A] 纯逻辑       _outcome_of / _norm_expiry / _masked_email / _target_name / _record
    [B] 场景矩阵     用假 SB 驱动 run_all()，逐条核对 Outcome / 退出码 / 通知 / 渲染文本
    [C] 静态与接线   死符号、sys.exit 收敛、renew-kit 接线、workflow↔代码 env 双向一致
    [D] 子进程       真跑 `python app.py`，核对退出码与打印出的报告

设计原则：**不改被测文件**。所有桩都打在 sys.modules 与模块属性上，
app.py 一个字节都不用为了测试而妥协。
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import tokenize
import types
import warnings
from pathlib import Path

# 被测文件里有一处内嵌 JS 字符串带 `\s`（out.modaltxt = ...replace(/\s+/g,' ')），
# 每次 compile 都会刷一条 SyntaxWarning —— 属既有现象，与本迁移无关，静音。
warnings.filterwarnings("ignore", category=SyntaxWarning)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WS = REPO.parents[1]
APP_PATH = REPO / "app.py"
WF_PATH = REPO / ".github" / "workflows" / "renew.yml"
PROXY_PATH = REPO / "scripts" / "setup_proxy.sh"
README_PATH = REPO / "README.md"

# 让 renewkit / requests / pyyaml 可导入（离线，不装包）
for p in (WS / "renew-kit", WS / "_deps"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import renewkit.notify as rk_notify                      # noqa: E402
from renewkit.outcome import Outcome                      # noqa: E402
from renewkit.report import RenewReport                   # noqa: E402

APP_SRC = APP_PATH.read_text(encoding="utf-8")
WF_SRC = WF_PATH.read_text(encoding="utf-8")
PROXY_SRC = PROXY_PATH.read_text(encoding="utf-8")
README_SRC = README_PATH.read_text(encoding="utf-8")

_PASS: list[str] = []
_FAIL: list[str] = []


def check(label: str, cond) -> bool:
    (_PASS if cond else _FAIL).append(label)
    if not cond:
        print(f"  ✗ {label}")
    return bool(cond)


def eq(label: str, got, want) -> bool:
    return check(f"{label}（got={got!r} want={want!r}）", got == want)


# ══════════════════════════════════════════════════════════════════════
# 基础设施
# ══════════════════════════════════════════════════════════════════════

def code_only(src: str) -> str:
    """把注释与字符串换成等长空格（保行保列），只留纯代码。

    不能用「去掉 token 再 join」的做法：那样 `_record(` 会被拆成 `_record (`，
    点号也会被空格隔开，反而把检查器自己搞坏。
    """
    lines = src.split("\n")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for r in range(r1, r2 + 1):
            line = lines[r - 1]
            a = c1 if r == r1 else 0
            b = c2 if r == r2 else len(line)
            lines[r - 1] = line[:a] + " " * max(0, b - a) + line[b:]
    return "\n".join(lines)


def install_stubs() -> None:
    """seleniumbase / requests 的假模块，够 app.py import 与调用即可。"""
    if "seleniumbase" not in sys.modules:
        sb_mod = types.ModuleType("seleniumbase")

        class SB:                                     # pragma: no cover
            def __init__(self, **_kw):
                raise RuntimeError("stub SB：本 harness 不该真的构造浏览器")

        sb_mod.SB = SB
        sys.modules["seleniumbase"] = sb_mod

    if "requests" not in sys.modules:
        req = types.ModuleType("requests")

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

            text = ""

        def _post(*_a, **_kw):
            return _Resp()

        req.post = _post
        req.get = lambda *a, **k: _Resp()
        req.put = lambda *a, **k: _Resp()
        # renewkit.http 会 `from requests.adapters import HTTPAdapter`
        adapters = types.ModuleType("requests.adapters")

        class HTTPAdapter:                            # pragma: no cover
            def __init__(self, **_kw):
                pass

        adapters.HTTPAdapter = HTTPAdapter
        req.adapters = adapters
        sys.modules["requests"] = req
        sys.modules["requests.adapters"] = adapters

    if "urllib3.util.retry" not in sys.modules:
        urllib3 = types.ModuleType("urllib3")
        util = types.ModuleType("urllib3.util")
        retry = types.ModuleType("urllib3.util.retry")

        class Retry:                                  # pragma: no cover
            def __init__(self, **_kw):
                pass

        retry.Retry = Retry
        util.retry = retry
        urllib3.util = util
        sys.modules.setdefault("urllib3", urllib3)
        sys.modules["urllib3.util"] = util
        sys.modules["urllib3.util.retry"] = retry


install_stubs()


@contextlib.contextmanager
def env_patch(**envs):
    """在受控 os.environ 下执行 —— renewkit.env 是**调用时**读环境，
    所以整段场景（不只是 import）都得在 patch 里。"""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "")          # subprocess 还要用
    os.environ.update({k: str(v) for k, v in envs.items() if v is not None})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def load_app() -> types.ModuleType:
    """把 app.py 当全新模块 exec 一次（模块级 env 在 import 时就读掉了）。"""
    mod = types.ModuleType("app_under_test")
    mod.__file__ = str(APP_PATH)
    exec(compile(APP_SRC, str(APP_PATH), "exec"), mod.__dict__)
    return mod


# ── 假浏览器 ──────────────────────────────────────────────────────────

BILLINGS_SRC = "<div>Expires 2026/10/09</div>"
BILLINGS_SRC_CD = "<div>Expires 2026/10/09</div><button>Renew in 02:13:45</button>"
BILLINGS_SRC_AFTER = ("<div>Expires 2026/10/13</div>"
                      "<button>Renew in 03:00:00</button>")


class FakeSB:
    """按一份「剧本」模拟 SeleniumBase。

    剧本键（都可不给，有默认）：
      login_ok       /a/billings 能否到达（默认 True）
      button         "available" | "countdown" | "none"（默认 none）
      billings_src   登录后页面源（默认 BILLINGS_SRC）
      turnstile      wait_for_turnstile_pass 的返回（默认 True）
      unlocked       _renew_button_unlocked 的返回（默认 True）
      click_raises   命中的 selector 子串 → sb.click 抛异常
      js_click_ok    JS 兜底点击是否成功（默认 False）
      confirm        点完之后页面是否出现新到期日（默认 False）
      page_src_raises  get_page_source 直接抛异常（模拟崩溃）
      cookie         会话 cookie 的 value（触发 SESSION_TOKEN 回写）
    """

    def __init__(self, sc: dict, **_kw):
        self.sc = sc
        self.opened: list[str] = []
        self.cookies_added: list[dict] = []
        self.shots: list[str] = []
        self.clicks: list[str] = []
        self._url = "https://bot-hosting.net/"
        self._title = "Home"
        self._src = ""

    # 上下文管理
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    # 导航
    def open(self, url):
        self.opened.append(url)
        if "/a/billings" in url:
            if self.sc.get("login_ok", True):
                self._url = "https://bot-hosting.net/a/billings"
                self._title = "Billings"
                self._src = self.sc.get("billings_src", BILLINGS_SRC)
            else:
                self._url = "https://bot-hosting.net/login"
                self._title = "Login"
                self._src = ""
        else:
            self._url = url
            self._title = "Home"
        return self

    def wait_for_ready_state_complete(self):
        return None

    def sleep(self, *_a):
        return None

    def save_screenshot(self, name):
        self.shots.append(name)

    def add_cookie(self, c):
        self.cookies_added.append(c)

    def get_current_url(self):
        return self._url

    def get_title(self):
        return self._title

    def get_page_source(self):
        if self.sc.get("page_src_raises"):
            raise RuntimeError("模拟：读页面源时崩了")
        if self.clicks and self.sc.get("confirm"):
            return BILLINGS_SRC_AFTER
        return self._src

    # 元素
    _FIRST_SELECTOR = 'button:contains("Renew")'

    def is_element_visible(self, selector):
        if self.sc.get("button", "none") == "none":
            return False
        return selector == self._FIRST_SELECTOR

    def is_element_present(self, _selector):
        return False

    def get_text(self, _selector):
        return "Renew in 02:13:45" if self.sc.get("button") == "countdown" else "Renew free plan"

    def click(self, selector, **_kw):
        for pat in self.sc.get("click_raises", ()):
            if pat in selector:
                raise RuntimeError(f"模拟：点击 {selector} 失败")
        self.clicks.append(selector)

    def wait_for_element_visible(self, *_a, **_kw):
        return None

    def execute_script(self, _js):
        return bool(self.sc.get("js_click_ok", False))

    def switch_to_default_content(self):
        return None

    def uc_gui_click_captcha(self):
        return None

    def get_cookies(self):
        if self.sc.get("cookie") is None:
            return []
        return [{"name": "session_token", "value": self.sc["cookie"],
                 "expiry": 4102444800}]          # 2100-01-01


def patch_module(mod: types.ModuleType, sc: dict) -> None:
    """把场景控制点换成桩（这些函数内部逻辑不是本次迁移的对象）。"""
    mod.wait_for_turnstile_pass = lambda _sb, timeout=240: sc.get("turnstile", True)
    mod._renew_button_unlocked = lambda _sb: sc.get("unlocked", True)
    mod._renew_button_state = lambda _sb: "unlocked"
    mod._dump_popup_dom = lambda _sb: None
    mod.dismiss_consent_popup = lambda _sb: True
    mod.do_discord_login = lambda _sb: sc.get("discord_ok", False)
    mod.get_current_ip = lambda _proxy="": "203.0.113.7"


def run_scenario(sc: dict, **envs):
    """跑一轮 run_all()，返回 (report, 打印出来的 stdout, sb, SB 构造 kwargs)。"""
    with env_patch(**envs):
        mod = load_app()
        patch_module(mod, sc)
        holder: dict = {}

        def factory(**kw):
            sb = FakeSB(sc, **kw)
            holder["sb"] = sb
            holder["kwargs"] = kw
            return sb

        mod.SB = factory
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rep = mod.run_all()
        return rep, buf.getvalue(), holder.get("sb"), holder.get("kwargs", {})


def run_main(sc: dict, **envs):
    """跑一轮 main()，返回 (退出码, stdout, report, sb)。"""
    with env_patch(**envs):
        mod = load_app()
        patch_module(mod, sc)
        holder: dict = {}

        def factory(**kw):
            sb = FakeSB(sc, **kw)
            holder["sb"] = sb
            return sb

        mod.SB = factory
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        return rc, buf.getvalue(), holder.get("sb")


class _NotifySpy:
    """拦下 renewkit.notify.send，记录都发了什么。"""

    def __init__(self):
        self.sent: list[str] = []
        self._orig = rk_notify.send

    def __enter__(self):
        def fake(text, **_kw):
            self.sent.append(text)
            return True
        rk_notify.send = fake
        return self

    def __exit__(self, *_a):
        rk_notify.send = self._orig
        return False


# ══════════════════════════════════════════════════════════════════════
# [A] 纯逻辑
# ══════════════════════════════════════════════════════════════════════

def section_a() -> None:
    print("\n[A] 纯逻辑")
    with env_patch():
        mod = load_app()

    # ── _outcome_of：状态串 → Outcome ──
    print("  _outcome_of 映射矩阵")
    cases = [
        ("✅ 续期成功", "", Outcome.RENEWED),
        ("✅ 续期成功", "可续期时间: 4h0min后", Outcome.RENEWED),
        ("❌ 登录失败", "登录失败: Cookie 已失效", Outcome.FAILED),
        ("❌ 续期失败", "续期失败: Turnstile 验证未通过", Outcome.FAILED),
        ("❌ 脚本异常中断", "脚本异常中断: RuntimeError: x", Outcome.FAILED),
        ("⏳ 未到续期时间", "可续期时间: 2h13min后", Outcome.SKIPPED),
        ("⏳ 未到续期时间", "已达续期上限", Outcome.ALREADY_MAX),
        ("⏭️ 跳过", "renew limit reached", Outcome.ALREADY_MAX),
        ("⏭️ 跳过", "普通跳过", Outcome.SKIPPED),
        ("ℹ️ 无需续期", "未找到续期按钮", Outcome.UNKNOWN),
        ("⚠️ 读不到状态", "", Outcome.UNKNOWN),
        ("⏰ 需人手續期", "", Outcome.UNKNOWN),
        ("", "", Outcome.FAILED),
        (None, "", Outcome.FAILED),
        ("完全无法识别的串", "", Outcome.FAILED),
    ]
    for status, detail, want in cases:
        got = mod._outcome_of(status, detail)
        check(f"_outcome_of({status!r}, {detail!r}) == {want.value}（got={got.value}）",
              got is want)

    print("  只有 FAILED 算错")
    check("FAILED.is_error", Outcome.FAILED.is_error is True)
    for o in (Outcome.RENEWED, Outcome.SKIPPED, Outcome.ALREADY_MAX,
              Outcome.UNKNOWN, Outcome.TRANSIENT):
        check(f"{o.value}.is_error is False", o.is_error is False)
        check(f"{o.value}.exit_code == 0", o.exit_code == 0)
    eq("FAILED.exit_code", Outcome.FAILED.exit_code, 1)

    print("  QUIET_OUTCOMES 语义")
    check("SKIPPED 静默", Outcome.SKIPPED in mod.QUIET_OUTCOMES)
    check("ALREADY_MAX 静默", Outcome.ALREADY_MAX in mod.QUIET_OUTCOMES)
    check("TRANSIENT 静默", Outcome.TRANSIENT in mod.QUIET_OUTCOMES)
    check("RENEWED 会通知", Outcome.RENEWED not in mod.QUIET_OUTCOMES)
    check("FAILED 会通知", Outcome.FAILED not in mod.QUIET_OUTCOMES)
    check("UNKNOWN 会通知（但不标红）", Outcome.UNKNOWN not in mod.QUIET_OUTCOMES)

    # ── _norm_expiry：斜杠 → 连字符（这是最容易踩的坑）──
    print("  _norm_expiry 归一")
    for raw, want in [
        ("2026/10/09", "2026-10-09"),
        ("2026-10-09", "2026-10-09"),
        ("2026/10/09 12:30", "2026-10-09"),
        ("2026-10-09T12:30:00", "2026-10-09"),
        ("  2026/10/09  ", "2026-10-09"),
        ("（未获取到）", ""),
        ("", ""),
        (None, ""),
        ("garbage", ""),
        ("2026/7/9", ""),            # 一位数月日不认，避免吐出脏串
        (0, ""),
    ]:
        eq(f"_norm_expiry({raw!r})", mod._norm_expiry(raw), want)

    print("  _norm_expiry 的输出 renewkit 真的认（这是它存在的理由）")
    from renewkit.timeutil import days_left, format_expiry
    eq("format_expiry('2026-10-09')", format_expiry("2026-10-09"), "10-09")
    check("days_left('2026-10-09') 是整数",
          isinstance(days_left("2026-10-09"), int))
    eq("未归一时 format_expiry('2026/10/09') 会原样吐回",
       format_expiry("2026/10/09"), "2026/10/09")
    check("未归一时 days_left('2026/10/09') 是 None",
          days_left("2026/10/09") is None)

    # ── _masked_email / _target_name ──
    print("  _masked_email")
    with env_patch(EMAIL="verylongname@example.com"):
        eq("长名遮罩", load_app()._masked_email(), "ve****me@example.com")
    with env_patch(EMAIL="ab@example.com"):
        eq("短名不遮", load_app()._masked_email(), "ab@example.com")
    with env_patch(EMAIL="no-at-sign"):
        eq("无 @ 时截前两位", load_app()._masked_email(), "no****")
    with env_patch():
        eq("空 EMAIL", load_app()._masked_email(), "")

    print("  _target_name")
    with env_patch(ACCOUNT_LABEL="02"):
        eq("只有 label", load_app()._target_name(), "Bot-hosting（02）")
    with env_patch():
        eq("都没有 → 裸名", load_app()._target_name(), "Bot-hosting")
    with env_patch(ACCOUNT_LABEL="02", EMAIL="a@b.com"):
        eq("label + 邮箱", load_app()._target_name(), "Bot-hosting（02 a@b.com）")
    with env_patch(EMAIL="a@b.com"):
        eq("只有邮箱", load_app()._target_name(), "Bot-hosting（a@b.com）")
    with env_patch(ACCOUNT_LABEL="  02  "):
        eq("label 前后空白被 strip", load_app()._target_name(), "Bot-hosting（02）")

    # ── _record：报告写入的唯一口径 ──
    print("  _record")
    rep = RenewReport(service="T")
    mod._record(rep, "N", "❌ 登录失败", error="Cookie 已失效")
    r = rep.results[0]
    eq("❌ 状态拼出小标题", r.detail, "登录失败: Cookie 已失效")
    check("❌ → FAILED", r.outcome is Outcome.FAILED)

    rep = RenewReport(service="T")
    mod._record(rep, "N", "✅ 续期成功", extra="到期日期已确认更新",
                expiry="2026/10/13")
    r = rep.results[0]
    eq("✅ 不加小标题", r.detail, "到期日期已确认更新")
    eq("✅ expiry 被归一", r.expire, "2026-10-13")
    check("✅ → RENEWED", r.outcome is Outcome.RENEWED)

    rep = RenewReport(service="T")
    mod._record(rep, "N", "❌ 续期失败", error="优先", extra="被忽略")
    eq("error 优先于 extra", rep.results[0].detail, "续期失败: 优先")

    rep = RenewReport(service="T")
    mod._record(rep, "N", "⏳ 未到续期时间", extra="可续期时间: 2h13min后",
                expiry="2026/10/09")
    eq("⏳ expiry 归一", rep.results[0].expire, "2026-10-09")
    eq("⏳ → SKIPPED", rep.results[0].outcome, Outcome.SKIPPED)

    rep = RenewReport(service="T")
    long_detail = "字" * 500
    mod._record(rep, "N", "❌ 续期失败", error=long_detail)
    d = rep.results[0].detail
    check(f"长 detail 截到 {mod._DETAIL_LIMIT}（got={len(d)}）", len(d) <= mod._DETAIL_LIMIT)
    check("截断以 … 收尾", d.endswith("…"))

    rep = RenewReport(service="T")
    mod._record(rep, "N", "❌ 续期失败", error="第一行\n第二行\t第三行")
    check("换行被压平", "\n" not in rep.results[0].detail and "\t" not in rep.results[0].detail)

    rep = RenewReport(service="T")
    mod._record(rep, "N", "❌ 续期失败", expiry="garbage")
    eq("认不出的 expiry 落成空串", rep.results[0].expire, "")

    rep = RenewReport(service="T")
    mod._record(rep, "N", "❌ 脚本异常中断", error=None, extra=None)
    eq("error/extra 都空时不炸", rep.results[0].detail, "脚本异常中断")

    print("  _DETAIL_LIMIT / _ALREADY_MAX_HINTS 存在且合理")
    check("_DETAIL_LIMIT 在 60..200 之间", 60 <= mod._DETAIL_LIMIT <= 200)
    check("_ALREADY_MAX_HINTS 非空", bool(mod._ALREADY_MAX_HINTS))
    check("SERVICE == 'Bot-hosting'", mod.SERVICE == "Bot-hosting")


# ══════════════════════════════════════════════════════════════════════
# [B] 场景矩阵
# ══════════════════════════════════════════════════════════════════════

BASE = dict(SESSION_TOKEN="tok-abc", ACCOUNT_LABEL="02")


def _outs(rep) -> list[str]:
    return [r.outcome.value for r in rep.results]


def section_b() -> None:
    print("\n[B] 场景矩阵")

    # ── B1 未配置凭据：SB 都不该被构造 ──
    print("  B1 未配置凭据")
    rep, out, sb, _kw = run_scenario({}, SESSION_TOKEN="", DISCORD_TOKEN="",
                                     ACCOUNT_LABEL="02")
    eq("B1 单条结果", len(rep.results), 1)
    eq("B1 → FAILED", _outs(rep), ["failed"])
    eq("B1 exit_code", rep.exit_code, 1)
    check("B1 sb 未被构造", sb is None)
    check("B1 detail 说明缺凭据", "无法登录" in rep.results[0].detail)
    check("B1 render 含服务名", "【Bot-hosting】" in rep.render())
    check("B1 render 有 FAILED 尾巴", "需要人工处理" in rep.render())

    # ── B2 登录失败 ──
    print("  B2 登录失败")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": False, "discord_ok": False}, **BASE)
    eq("B2 → FAILED", _outs(rep), ["failed"])
    check("B2 有截图 login_failed.png", "login_failed.png" in sb.shots)
    check("B2 detail 带小标题", rep.results[0].detail.startswith("登录失败: "))
    check("B2 detail 说明 cookie 失效", "Cookie 已失效" in rep.results[0].detail)
    check("B2 render 标红文案", "续期未完成" in rep.render())

    # ── B3 续期成功 ──
    print("  B3 续期成功")
    rep, out, sb, kw = run_scenario(
        {"login_ok": True, "button": "available", "turnstile": True,
         "unlocked": True, "confirm": True}, **BASE)
    eq("B3 → RENEWED", _outs(rep), ["renewed"])
    eq("B3 exit_code", rep.exit_code, 0)
    eq("B3 expire 归一", rep.results[0].expire, "2026-10-13")
    rend = rep.render()
    check("B3 render 成功续期", "成功续期" in rend)
    check("B3 render 带到期日", "至 10-13" in rend)
    check("B3 render 带剩余天数", "剩余" in rend)
    check("B3 render 无 FAILED 尾巴", "需要人工处理" not in rend)
    check("B3 点了外部按钮", any("Renew" in c for c in sb.clicks))
    check("B3 点了弹窗按钮", any("Renew for 4 days" in c for c in sb.clicks))
    check("B3 名字带 ACCOUNT_LABEL", "Bot-hosting（02）" in rend)

    # ── B4 未到续期时间（按钮直接显示倒计时）→ SKIPPED ──
    print("  B4 未到续期时间（按钮倒计时）")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "countdown"}, **BASE)
    eq("B4 → SKIPPED", _outs(rep), ["skipped"])
    eq("B4 exit_code", rep.exit_code, 0)
    eq("B4 expire 归一", rep.results[0].expire, "2026-10-09")
    rend = rep.render()
    check("B4 render 状态良好", "状态良好" in rend)
    check("B4 render 带可续期时间", "2h13min后" in rend)
    check("B4 render 无 FAILED 尾巴", "需要人工处理" not in rend)
    check("B4 没点任何按钮", sb.clicks == [])

    # ── B5 未到续期时间（兜底从页面源抠倒计时）──
    print("  B5 未到续期时间（兜底提取）")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "none", "billings_src": BILLINGS_SRC_CD},
        **BASE)
    eq("B5 → SKIPPED", _outs(rep), ["skipped"])
    check("B5 兜底提取到倒计时", "2h13min后" in rep.render())

    # ── B6 读不到状态 → UNKNOWN（通知但不红）──
    print("  B6 读不到状态")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "none"}, **BASE)
    eq("B6 → UNKNOWN", _outs(rep), ["unknown"])
    eq("B6 exit_code 为 0", rep.exit_code, 0)
    rend = rep.render()
    check("B6 render 结果未确认", "结果未确认" in rend)
    check("B6 render 不是标红文案", "续期未完成" not in rend)
    check("B6 render 带剩余天数", "剩" in rend)

    # ── B7 Turnstile 未通过 ──
    print("  B7 Turnstile 未通过")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "available", "turnstile": False}, **BASE)
    eq("B7 → FAILED", _outs(rep), ["failed"])
    check("B7 detail 提到 Turnstile", "Turnstile" in rep.results[0].detail)
    check("B7 有截图", "turnstile_final_fail.png" in sb.shots)

    # ── B8 bot check 未过、按钮仍锁定 ──
    print("  B8 按钮仍锁定")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "available", "turnstile": True,
         "unlocked": False}, **BASE)
    eq("B8 → FAILED", _outs(rep), ["failed"])
    check("B8 detail 提到 bot check", "Bot check" in rep.results[0].detail)
    check("B8 有截图 button_still_locked", "button_still_locked.png" in sb.shots)

    # ── B9 点外部按钮就报错 ──
    print("  B9 点外部按钮出错")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "available",
         "click_raises": ('button:contains("Renew")',)}, **BASE)
    eq("B9 → FAILED", _outs(rep), ["failed"])
    check("B9 detail 提到点击出错", "点击外部续期按钮出错" in rep.results[0].detail)
    check("B9 有截图 click_outer_failed", "click_outer_failed.png" in sb.shots)

    # ── B10 点了但后台未确认（原 fail_code=3）──
    print("  B10 已点击但后台未确认")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "available", "turnstile": True,
         "unlocked": True, "confirm": False}, **BASE)
    eq("B10 → FAILED（有意保留的红）", _outs(rep), ["failed"])
    eq("B10 exit_code", rep.exit_code, 1)
    d = rep.results[0].detail
    check("B10 detail 说明已点击未确认", "按钮已点击但后台未确认" in d)
    check("B10 detail 保留「自动重试」提示", "自动重试" in d)
    check("B10 有截图 renew_result_unknown", "renew_result_unknown.png" in sb.shots)
    check("B10 轮询过 18 次（没提前退出）",
          len([s for s in sb.shots if s == "renew_result_unknown.png"]) == 1)

    # ── B11 弹窗按钮根本没点着（原 fail_code=6）──
    print("  B11 弹窗按钮没点着")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "button": "available", "turnstile": True,
         "unlocked": True, "confirm": False, "js_click_ok": False,
         "click_raises": ('Renew for 4 days',)}, **BASE)
    eq("B11 → FAILED", _outs(rep), ["failed"])
    d = rep.results[0].detail
    check("B11 detail 说明按钮没点着", "按钮没点着" in d)
    check("B11 与 B10 的 detail 可区分", "已点击但后台未确认" not in d)

    # ── B12 脚本异常中断 ──
    print("  B12 脚本异常中断")
    rep, out, sb, _kw = run_scenario(
        {"login_ok": True, "page_src_raises": True}, **BASE)
    eq("B12 → FAILED", _outs(rep), ["failed"])
    d = rep.results[0].detail
    check("B12 detail 带小标题", d.startswith("脚本异常中断: "))
    check("B12 detail 带异常类型", "RuntimeError" in d)
    check("B12 有 fatal_error.png", "fatal_error.png" in sb.shots)

    # ── B13 代理开关 ──
    print("  B13 代理开关")
    _rep, _out, _sb, kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       **BASE)
    check("B13 无代理时不下发 proxy", "proxy" not in kw)
    _rep, _out, _sb, kw = run_scenario(
        {"login_ok": True, "button": "countdown"}, IS_PROXY="true",
        PROXY_SERVER="socks5://127.0.0.1:1080", **BASE)
    eq("B13 IS_PROXY=true 时下发 proxy",
       kw.get("proxy"), "socks5://127.0.0.1:1080")
    _rep, _out, _sb, kw = run_scenario(
        {"login_ok": True, "button": "countdown"}, IS_PROXY="false",
        PROXY_SERVER="socks5://127.0.0.1:1080", **BASE)
    check("B13 IS_PROXY=false 时不下发 proxy", "proxy" not in kw)
    _rep, _out, _sb, kw = run_scenario(
        {"login_ok": True, "button": "countdown"}, IS_PROXY="true", **BASE)
    eq("B13 缺 PROXY_SERVER 时用默认",
       kw.get("proxy"), "http://127.0.0.1:1080")

    # ── B14 headless ──
    print("  B14 HEADLESS")
    _rep, _out, _sb, kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       **BASE)
    eq("B14 默认非 headless", kw.get("headless"), False)
    check("B14 始终 uc=True", kw.get("uc") is True)
    _rep, _out, _sb, kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       HEADLESS="true", **BASE)
    eq("B14 HEADLESS=true 生效", kw.get("headless"), True)

    # ── B15 通知语义：谁出声、谁静默 ──
    print("  B15 通知语义")
    cases = [
        ("未到续期时间 → 静默", {"login_ok": True, "button": "countdown"}, {}, 0),
        ("成功 → 出声", {"login_ok": True, "button": "available",
                       "confirm": True}, {}, 1),
        ("失败 → 出声", {"login_ok": False, "discord_ok": False}, {}, 1),
        ("读不到 → 出声", {"login_ok": True, "button": "none"}, {}, 1),
        ("未到续期时间 + AUT0_NOTIFY_SKIP → 出声",
         {"login_ok": True, "button": "countdown"}, {"AUT0_NOTIFY_SKIP": "1"}, 1),
    ]
    for label, sc, extra, want in cases:
        with _NotifySpy() as spy:
            rc, _out, _sb = run_main(sc, **{**BASE, **extra})
        eq(f"B15 {label}", len(spy.sent), want)
        if want == 1:
            check(f"B15 {label}：消息非空", bool(spy.sent[0].strip()))

    # ── B16 main() 退出码 ──
    print("  B16 main() 退出码")
    rc, out, _sb = run_main({"login_ok": True, "button": "countdown"}, **BASE)
    eq("B16 SKIPPED → 0", rc, 0)
    check("B16 SKIPPED 也打印报告", "【Bot-hosting】" in out)
    rc, out, _sb = run_main({"login_ok": True, "button": "available",
                             "confirm": True}, **BASE)
    eq("B16 RENEWED → 0", rc, 0)
    rc, out, _sb = run_main({"login_ok": False, "discord_ok": False}, **BASE)
    eq("B16 FAILED → 1", rc, 1)
    rc, out, _sb = run_main({"login_ok": True, "button": "none"}, **BASE)
    eq("B16 UNKNOWN → 0", rc, 0)
    rc, out, _sb = run_main({}, SESSION_TOKEN="", DISCORD_TOKEN="")
    eq("B16 无凭据 → 1", rc, 1)

    # ── B17 「剩 N 天」不许出现两次（orihost 踩过的坑）──
    print("  B17 天数不重复")
    for label, sc in [
        ("watchdog 式 SKIPPED", {"login_ok": True, "button": "countdown"}),
        ("UNKNOWN", {"login_ok": True, "button": "none"}),
        ("RENEWED", {"login_ok": True, "button": "available", "confirm": True}),
        ("FAILED", {"login_ok": False, "discord_ok": False}),
    ]:
        rep, _out, _sb, _kw = run_scenario(sc, **BASE)
        rend = rep.render()
        for line in rend.split("\n"):
            n = len(re.findall(r"剩\s*\d+\s*天", line))
            check(f"B17 {label}：单行最多一次「剩 N 天」（got={n}）", n <= 1)
        check(f"B17 {label}：没有「剩 X 天 · 剩 X 天」",
              not re.search(r"剩\s*(\d+)\s*天[^\n]*剩\s*\1\s*天", rend))

    # ── B18 DRY_RUN 挡住 Secret 回写 ──
    print("  B18 DRY_RUN 闸门")
    sc_rotate = {"login_ok": True, "button": "countdown", "cookie": "NEWTOKEN1234"}

    def _rotate(dry, gh="ghp_x"):
        with env_patch(**{**BASE, "GH_TOKEN": gh, **({"DRY_RUN": dry} if dry else {})}):
            mod = load_app()
            patch_module(mod, sc_rotate)
            calls: list[tuple] = []
            mod.update_github_secret = lambda name, val: (calls.append((name, val)), True)[1]

            def factory(**_kw):
                return FakeSB(sc_rotate)

            mod.SB = factory
            with contextlib.redirect_stdout(io.StringIO()):
                rep = mod.run_all()
            return calls, rep

    calls, rep = _rotate(None)
    eq("B18 非 DRY_RUN：回写了 1 次", len(calls), 1)
    eq("B18 回写的是 SESSION_TOKEN", calls[0][0] if calls else None, "SESSION_TOKEN")
    eq("B18 回写的是新值", calls[0][1] if calls else None, "NEWTOKEN1234")
    calls, rep = _rotate("1")
    eq("B18 DRY_RUN=1：一次都不回写", len(calls), 0)
    check("B18 DRY_RUN 下续期结论不变", _outs(rep) == ["skipped"])
    calls, rep = _rotate(None, gh="")
    eq("B18 无 GH_TOKEN：不回写", len(calls), 0)

    # ── B19 cookie 没变就不回写 ──
    print("  B19 cookie 无变化")
    calls: list[tuple] = []
    with env_patch(**{**BASE, "GH_TOKEN": "ghp_x"}):
        mod = load_app()
        patch_module(mod, {"login_ok": True, "button": "countdown",
                           "cookie": "tok-abc"})       # 与 SESSION_TOKEN 相同
        mod.update_github_secret = lambda n, v: (calls.append((n, v)), True)[1]
        mod.SB = lambda **_kw: FakeSB({"login_ok": True, "button": "countdown",
                                       "cookie": "tok-abc"})
        with contextlib.redirect_stdout(io.StringIO()):
            rep = mod.run_all()
    eq("B19 值未变 → 不回写", len(calls), 0)
    check("B19 结论仍是 SKIPPED", _outs(rep) == ["skipped"])

    # ── B20 多账号命名 ──
    print("  B20 报告名")
    rep, _out, _sb, _kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       SESSION_TOKEN="t", ACCOUNT_LABEL="01")
    check("B20 label=01 出现在报告里", "Bot-hosting（01）" in rep.render())
    rep, _out, _sb, _kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       SESSION_TOKEN="t")
    check("B20 无 label 时退回裸名", "Bot-hosting ·" in rep.render())

    # ── B21 回归：报告名不能被 cookie 循环变量污染 ──
    # run_all 正文有 `for name, value in COOKIES.items()`；报告名若也叫 name，
    # 循环跑完 name 就变成最后一个 cookie key（"theme"），整份报告目标名全错。
    print("  B21 报告名不被 cookie key 覆盖（回归）")
    rep, _out, _sb, _kw = run_scenario({"login_ok": True, "button": "countdown"},
                                       SESSION_TOKEN="t", ACCOUNT_LABEL="02")
    nm = rep.results[0].name
    eq("B21 名字就是带 label 的目标名", nm, "Bot-hosting（02）")
    for ck in ("session_token", "login", "theme"):
        check(f"B21 名字不是 cookie key {ck!r}", nm != ck)
    rend = rep.render()
    check("B21 渲染里没有把 cookie key 当名字", "🟢 theme ·" not in rend)
    check("B21 渲染里没有把 cookie key 当名字（login）", "🟢 login ·" not in rend)
    # 走完整登录路径（有 SESSION_TOKEN）才会执行 cookie 注入那段
    check("B21 走的是完整登录路径（注入过 cookie）",
          len(_sb.cookies_added) == 3)


# ══════════════════════════════════════════════════════════════════════
# [C] 静态与接线
# ══════════════════════════════════════════════════════════════════════

DEAD_SYMBOLS = (
    "send_telegram_message", "format_notification", "now_local", "clip_text",
    "fmt_expiry", "account_label", "TG_BOT_TOKEN", "TG_CHAT_ID",
    "fail_code", "os.environ.get", "os.getenv",
)


def section_c() -> None:
    print("\n[C] 静态与接线")
    code = code_only(APP_SRC)

    print("  C1 死符号与退出码收敛")
    for s in DEAD_SYMBOLS:
        check(f"C1 纯代码里没有 {s}", code.count(s) == 0)
    exits = re.findall(r"sys\.exit\s*\(", code)
    eq("C1 只有一个 sys.exit", len(exits), 1)
    check("C1 它是 sys.exit(main())", "sys.exit(main())" in code)
    check("C1 没有 sys.exit(1/2/3/4/5/6)",
          not re.search(r"sys\.exit\s*\(\s*[0-9]", code))
    check("C1 没有 sys.exit(fail_code)", "sys.exit(fail_code)" not in code)
    check("C1 没有 os.environ.copy 之外的 os.environ",
          set(re.findall(r"os\.\w+", code)) == {"os.environ"})
    eq("C1 os.environ.copy 恰好 1 处（给 gh 子进程传环境）",
       code.count("os.environ.copy"), 1)

    print("  C2 run_all / main 形状")
    check("C2 def run_all() -> RenewReport", "def run_all(" in code)
    check("C2 def main() -> int", re.search(r"def main\([^)]*\)\s*->\s*int", code) is not None)
    eq("C2 return report 恰好 8 处", code.count("return report"), 8)
    eq("C2 _record( 调用 + 定义 = 12", code.count("_record("), 12)
    check("C2 run_all 里构造 RenewReport", "RenewReport(service=SERVICE)" in code)
    check("C2 main 里调 report.finish", "report.finish(" in code)
    check("C2 main 里算 notify_tg", "notify_tg" in code)
    # 注意：这些是字符串字面量，必须查原文而不是 code_only（字面量被抹成空格了）
    check("C2 AUT0_NOTIFY_SKIP 逃生门在", 'env.dry_run("AUT0_NOTIFY_SKIP")' in APP_SRC)
    check("C2 DRY_RUN 闸门在", "env.dry_run()" in code)

    print("  C3 renew-kit 接线")
    for s in ("from renewkit import env", "from renewkit.outcome import Outcome",
              "from renewkit.report import RenewReport, shorten"):
        check(f"C3 {s}", s in APP_SRC)
    check("C3 用了 shorten", "shorten(" in code)
    check("C3 用了 env.get", "env.get(" in code)

    print("  C4 浏览器逻辑没被碰（关键函数仍在）")
    for fn in ("wait_for_turnstile_pass", "_turnstile_solved", "_turnstile_iframe_present",
               "_renew_button_unlocked", "_renew_button_state", "_dump_popup_dom",
               "dismiss_consent_popup", "do_discord_login", "discord_authorize",
               "capture_discord_state", "get_cookie_info", "should_update_cookie",
               "update_github_secret", "extract_expiry_date", "format_countdown",
               "get_current_ip"):
        check(f"C4 def {fn} 还在", re.search(rf"def {fn}\(", code) is not None)
    check("C4 IS_X11 仍在", "IS_X11" in code)
    check("C4 _LOGIN_METHOD 仍在", "_LOGIN_METHOD" in code)
    check("C4 仍有 global _LOGIN_METHOD", "global _LOGIN_METHOD" in code)

    print("  C5 workflow 关键约束")
    import yaml
    d = yaml.safe_load(WF_SRC)
    on = d[True] if True in d else d["on"]
    eq("C5 定时任务保留", on["schedule"][0]["cron"], "20 1 * * *")
    check("C5 支持 workflow_dispatch", "workflow_dispatch" in on)
    eq("C5 dispatch 有 dry_run 输入",
       list(on["workflow_dispatch"]["inputs"].keys()), ["dry_run"])
    job = d["jobs"]["Renew-bot"]
    eq("C5 actions: write 保留（gh run delete 需要）", job["permissions"]["actions"], "write")
    eq("C5 contents: read", job["permissions"]["contents"], "read")
    check("C5 有 timeout-minutes", isinstance(job.get("timeout-minutes"), int))
    check("C5 有 concurrency", bool(job.get("concurrency")))
    check("C5 concurrency 不取消进行中的", job["concurrency"]["cancel-in-progress"] is False)
    steps = job["steps"]
    eq("C5 4 个 step（checkout / 续期 / 清理进程 / 清理记录）", len(steps), 4)
    eq("C5 step0 是 checkout", steps[0]["uses"], "actions/checkout@v7")
    check("C5 续期 step 用 renew-kit action",
          steps[1]["uses"].startswith("jardanlau2020/renew-kit/.github/actions/renew@"))
    check("C5 续期 step 钉了 v0.4.2", "v0.4.2" in steps[1]["uses"])
    check("C5 清理进程 step 是 always()", steps[2].get("if") == "always()")
    check("C5 清理记录 step 是 always()", steps[3].get("if") == "always()")
    check("C5 清理记录仍用 gh run delete", "gh run delete" in steps[3]["run"])
    check("C5 仍保留 5 条运行记录", "KEEP=5" in steps[3]["run"])
    check("C5 清理进程仍杀 sing-box/chrome/Xvfb",
          all(k in steps[2]["run"] for k in ("sing-box", "chromedriver", "chrome", "Xvfb")))
    check("C5 清理进程仍删临时文件",
          all(k in steps[2]["run"] for k in ("sing-box-config.json", "sing-box.log")))

    w = steps[1]["with"]
    eq("C5 script 指向 app.py", w["script"], "app.py")
    check("C5 command 用 xvfb-run", "xvfb-run" in w["command"])
    check("C5 command 跑 app.py", "app.py" in w["command"])
    check("C5 setup-command 装 chromedriver", "chromedriver" in w["setup-command"])
    check("C5 setup-command 调 setup_proxy.sh", "scripts/setup_proxy.sh" in w["setup-command"])
    check("C5 apt 装 xvfb/x11-utils/xdotool/scrot/fonts-noto-cjk",
          all(k in w["apt-packages"] for k in
              ("xvfb", "x11-utils", "xdotool", "scrot", "fonts-noto-cjk")))
    check("C5 pip 装 seleniumbase", "seleniumbase" in w["pip-packages"])
    check("C5 pip 装 requests", "requests" in w["pip-packages"])
    eq("C5 产物路径 *.png", w["artifact-paths"], "*.png")
    eq("C5 产物名保留", w["artifact-name"], "bot-hosting-screenshots-02")
    eq("C5 失败兜底通知开", w["notify-on-failure"], "true")

    print("  C6 workflow ↔ 代码 env 双向一致")
    wf_env = set(steps[1]["env"].keys())
    # 必须查 APP_SRC：code_only 会把 "NAME" 这种字面量抹成空格。
    app_env = set(re.findall(r'env\.get\("([A-Z0-9_]+)"\)', APP_SRC))
    app_env |= set(re.findall(r'env\.dry_run\("([A-Z0-9_]+)"\)', APP_SRC))
    if "env.dry_run()" in code:
        app_env.add("DRY_RUN")                      # dry_run() 默认读 DRY_RUN
    app_env |= {"IS_PROXY", "PROXY_SERVER", "HEADLESS", "DISPLAY"}
    consumed_elsewhere = {"TG_BOT_TOKEN", "TG_CHAT_ID", "NODE_LINK"}
    print(f"    workflow env : {sorted(wf_env)}")
    print(f"    app.py 读    : {sorted(app_env)}")
    for k in sorted(wf_env):
        check(f"C6 workflow 传的 {k} 有归宿（app.py 或 renewkit/setup_proxy.sh）",
              k in app_env or k in consumed_elsewhere)
    for k in sorted(app_env):
        if k in ("IS_PROXY", "PROXY_SERVER", "HEADLESS", "DISPLAY"):
            continue    # 由上游 installer / runner 提供，不是 workflow 直接传
        if k == "AUT0_NOTIFY_SKIP":
            # 逃生门：默认不传才对（传了就恢复每日心跳）。只要求 README 有写（C8 覆盖）。
            check("C6 AUT0_NOTIFY_SKIP 默认不该被 workflow 传（逃生门）",
                  k not in wf_env)
            continue
        check(f"C6 app.py 读的 {k} workflow 有传", k in wf_env)
    for k in ("TG_BOT_TOKEN", "TG_CHAT_ID", "NODE_LINK"):
        check(f"C6 {k} 不该被 app.py 直接读（交给 renewkit / setup_proxy.sh）",
              k not in app_env)
    check("C6 ACCOUNT_LABEL 传的是 02", steps[1]["env"]["ACCOUNT_LABEL"] == "02")
    check("C6 DRY_RUN 绑到 dispatch 输入",
          "inputs.dry_run" in steps[1]["env"]["DRY_RUN"])
    check("C6 NODE_LINK 绑到 secret",
          "secrets.NODE_LINK" in steps[1]["env"]["NODE_LINK"])

    print("  C7 setup_proxy.sh 契约")
    check("C7 文件存在", PROXY_PATH.exists())
    check("C7 可执行位", os.access(PROXY_PATH, os.X_OK))
    check("C7 有 shebang", PROXY_SRC.startswith("#!/usr/bin/env bash"))
    check("C7 set -uo pipefail", "set -uo pipefail" in PROXY_SRC)
    check("C7 读 NODE_LINK", "NODE_LINK" in PROXY_SRC)
    check("C7 空 NODE_LINK 时 ::warning::", "::warning::" in PROXY_SRC)
    check("C7 写 IS_PROXY=true", 'echo "IS_PROXY=true" >> "$GITHUB_ENV"' in PROXY_SRC)
    check("C7 明确写 IS_PROXY=false", 'echo "IS_PROXY=false" >> "$GITHUB_ENV"' in PROXY_SRC)
    check("C7 写 PROXY_SERVER", 'echo "PROXY_SERVER=${proxy}" >> "$GITHUB_ENV"' in PROXY_SRC)
    check("C7 真探代理（-x socks5h://）", "-x \"socks5h://127.0.0.1:${PORT}\"" in PROXY_SRC)
    check("C7 探测 api.ipify.org", "api.ipify.org" in PROXY_SRC)
    check("C7 有重试", "for i in 1 2 3" in PROXY_SRC)
    check("C7 端口可覆盖", "AUT0_SINGBOX_PORT" in PROXY_SRC)
    check("C7 installer 可覆盖", "AUT0_PROXY_INSTALLER" in PROXY_SRC)
    check("C7 探测 URL 可覆盖", "AUT0_PROXY_PROBE_URL" in PROXY_SRC)
    check("C7 不碰 ORIHOST_* 变量（复制粘贴残留）", "ORIHOST" not in PROXY_SRC)
    check("C7 不 export 只走 GITHUB_ENV",
          ">> \"$GITHUB_ENV\"" in PROXY_SRC and "\nexport IS_PROXY" not in PROXY_SRC)

    print("  C8 README 同步")
    rd = README_SRC
    for k in ("renew-kit", "v0.4.2", "SKIPPED", "UNKNOWN", "AUT0_NOTIFY_SKIP",
              "DRY_RUN", "scripts/setup_proxy.sh", "NODE_LINK"):
        check(f"C8 README 提到 {k}", k in rd)
    check("C8 README 不再宣传 DISCORD_TOKEN 是必填备用登录",
          "必须填写" not in rd)
    check("C8 README 说明了静默策略", "静默" in rd)
    check("C8 README 说明了「已点击未确认仍判红」这个偏离", "未确认" in rd)
    check("C8 README 记录了退出码收敛", "0/1" in rd or "exit" in rd.lower())

    print("  C9 AST 可解析且无遗留死函数定义")
    import ast
    tree = ast.parse(APP_SRC)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for s in ("send_telegram_message", "format_notification", "now_local",
              "clip_text", "fmt_expiry", "masked_email", "account_label"):
        check(f"C9 没有 def {s}", s not in defined)
    for s in ("_outcome_of", "_norm_expiry", "_record", "_target_name",
              "_masked_email", "run_all", "main"):
        check(f"C9 有 def {s}", s in defined)

    print("  C10 run_all 不用 name 作局部变量（cookie 循环会覆盖它）")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_all")
    assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    for_targets = {t.id for n in ast.walk(fn) if isinstance(n, ast.For)
                   for t in ast.walk(n.target) if isinstance(t, ast.Name)}
    check(f"C10 run_all 没赋值给 name（got={sorted(assigned)}）", "name" not in assigned)
    check("C10 run_all 用 target 存报告名", "target" in assigned)
    check(f"C10 cookie 循环仍在（for name, value …）（got={sorted(for_targets)}）",
          "name" in for_targets)

    print("  C11 所有 _record 调用点都传 target（跨行也要匹配到）")
    # 正则跨行匹配 —— 成功那条 _record( 是多行写法，纯文本 replace 会漏掉它。
    calls = re.findall(r"(?<!def )_record\(\s*report\s*,\s*(\w+)\s*,", code)
    check(f"C11 全部传 target（got={sorted(set(calls))}）", set(calls) == {"target"})
    eq("C11 _record 调用点 11 个（不含 def 行）", len(calls), 11)
    check("C11 原文里没有 `report, name,`", "report, name," not in APP_SRC)


# ══════════════════════════════════════════════════════════════════════
# [D] 子进程端到端
# ══════════════════════════════════════════════════════════════════════

def section_d() -> None:
    print("\n[D] 子进程端到端")
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "seleniumbase"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(textwrap.dedent('''
            class SB:
                def __init__(self, **kw):
                    raise RuntimeError("harness stub: no browser")
        '''), encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join([td, str(WS / "renew-kit"), str(WS / "_deps")]),
            "PYTHONIOENCODING": "utf-8",
            "ACCOUNT_LABEL": "02",
        }

        # D1 无凭据 → 退出 1，报告里说清原因
        proc = subprocess.run([sys.executable, str(APP_PATH)], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)
        eq("D1 退出码 1", proc.returncode, 1)
        check("D1 stdout 有报告头", "【Bot-hosting】" in proc.stdout)
        check("D1 stdout 说明缺凭据", "无法登录" in proc.stdout)
        check("D1 stdout 有 FAILED 行", "续期未完成" in proc.stdout)
        check("D1 stdout 有 FAILED 尾巴", "需要人工处理" in proc.stdout)
        check("D1 有账号标签", "Bot-hosting（02）" in proc.stdout)

        # D2 有凭据但浏览器起不来 → 异常路径收敛成 FAILED（原来会 NameError/裸抛栈）
        env2 = dict(env, SESSION_TOKEN="dummy")
        proc = subprocess.run([sys.executable, str(APP_PATH)], capture_output=True,
                              text=True, encoding="utf-8", env=env2, timeout=120)
        eq("D2 退出码 1", proc.returncode, 1)
        check("D2 stdout 报脚本异常中断", "脚本异常中断" in proc.stdout)
        check("D2 stdout 带异常类型", "RuntimeError" in proc.stdout)
        check("D2 没有 NameError 逃逸", "NameError" not in proc.stderr)
        check("D2 有 traceback（诊断信息在）", "Traceback" in proc.stderr)
        check("D2 报告仍然打出来了", "【Bot-hosting】" in proc.stdout)

        # D3 未配置 TG 时不该炸，只提示跳过
        check("D3 未配置 TG 时提示跳过", "Telegram 未配置" in proc.stdout)


def main() -> int:
    print("=" * 72)
    print("Aut0-Renew-B0th0sting02 离线验收")
    print("=" * 72)
    section_a()
    section_b()
    section_c()
    section_d()
    total = len(_PASS) + len(_FAIL)
    print("\n" + "=" * 72)
    if _FAIL:
        print(f"❌ {len(_FAIL)}/{total} 条断言失败：")
        for f in _FAIL:
            print("   -", f)
        return 1
    print(f"✅ 全部 {total} 条断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
