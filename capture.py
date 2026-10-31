import os, json, time
from playwright.sync_api import sync_playwright

TOK=os.environ.get("SESSION_TOKEN","")
LOG=[]
def rec(**kw): LOG.append(kw)

with sync_playwright() as p:
    br=p.chromium.launch(args=["--no-sandbox","--disable-dev-shm-usage"], headless=True)
    ctx=br.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
    for n,v in (("session_token",TOK),("login","true"),("theme","system")):
        ctx.add_cookies([{"name":n,"value":v,"domain":"bot-hosting.net","path":"/"}])
    pg=ctx.new_page()

    def on_req(r):
        if "bot-hosting.net" in r.url and (r.method!="GET"):
            rec(kind="REQ", method=r.method, url=r.url, post=(r.post_data or "")[:300])
    def on_resp(r):
        if "bot-hosting.net" in r.url and ("renew" in r.url or "billing" in r.url):
            try: body=r.text()[:400]
            except Exception: body="<no body>"
            rec(kind="RESP", status=r.status, url=r.url, body=body)
    pg.on("request", on_req); pg.on("response", on_resp)

    pg.goto("https://bot-hosting.net/a/billings", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    print("  ① URL:", pg.url)
    body=pg.inner_text("body")
    import re
    m=re.search(r'(?:(\d{2}:\d{2}:\d{2}) 后?可续期)', body)
    print("  ② 冷卻狀態:", m.group(1) if m else "（唔見倒數，可能可續期）")
    print("  ③ 到期:", ((re.search(r'(\d{4}/\d{1,2}/\d{1,2})\s*到期', body).group(1) if re.search(r'(\d{4}/\d{1,2}/\d{1,2})\s*到期', body) else '?'))

    # 撳 Renew free plan
    try:
        btns=pg.locator("button").all_inner_texts()
        print("  ③b 頁面所有按鈕:", [b.strip()[:20] for b in btns][:8])
    except Exception: pass
    clicked=False
    for sel in ["button:has-text('Renew free plan')","a:has-text('Renew free plan')","button:has-text('Renew')","text=Renew free plan"]:
        try:
            el=pg.locator(sel).first
            if el.is_visible(timeout=4000):
                print("  ④ 撳:", sel, "| disabled=", el.get_attribute("disabled"))
                el.click(timeout=8000); clicked=True; break
        except Exception as e:
            print("     ✗", sel, str(e)[:60])
    print("  ⑤ clicked:", clicked)
    pg.wait_for_timeout(4000)

    # 等 turnstile token
    tok=""
    for i in range(30):
        try:
            tok=pg.evaluate("""() => { const e=document.querySelector('[name=cf-turnstile-response]'); return e? e.value : '' }""") or ""
        except Exception: tok=""
        if tok: break
        # 試撳 widget
        try:
            for fr in pg.frames:
                if "challenges.cloudflare.com" in (fr.url or ""):
                    try: fr.click("input[type=checkbox]", timeout=1500)
                    except Exception: pass
        except Exception: pass
        pg.wait_for_timeout(2000)
    print("  ⑥ turnstileToken: 長度=%d 前綴=%s" % (len(tok), tok[:16]))
    # 撳確認
    for sel in ["dialog button.btn-primary","button:has-text('Confirm')","button:has-text('Renew')"]:
        try:
            el=pg.locator(sel).last
            if el.is_visible(timeout=2500):
                print("  ⑦ 撳確認:", sel, "| disabled=", el.get_attribute("disabled"))
                el.click(timeout=8000); break
        except Exception: continue
    pg.wait_for_timeout(12000)
    print("  ⑧ 頁面提示:", [l for l in pg.inner_text("body").splitlines() if 'fail' in l.lower() or 'retry' in l.lower()][:3])
    print("\n  ════ 抓到嘅請求 ════")
    for e in LOG:
        if e["kind"]=="REQ":
            print(f'   → {e["method"]} {e["url"][:90]}')
            if e.get("post"): print(f'      body: {e["post"]}')
        else:
            print(f'   ← {e["status"]} {e["url"][:90]}')
            print(f'      {e["body"][:260]}')
    br.close()
