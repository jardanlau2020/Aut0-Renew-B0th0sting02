# 一次性探測：bot-hosting email+password 登入喺 GitHub runner IP 會唔會免 captcha
import os, re, sys
from playwright.sync_api import sync_playwright

ACCTS = [(os.environ.get("EMAIL_A",""), os.environ.get("PASS_A","")),
         (os.environ.get("EMAIL_B",""), os.environ.get("PASS_B",""))]
res = []
with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox","--disable-dev-shm-usage"], headless=True)
    for em, pw in ACCTS:
        if not em: continue
        ctx = br.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
        pg = ctx.new_page()
        tag = em
        try:
            pg.goto("https://bot-hosting.net/login", wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(2500)
            pg.fill("input#email", em, timeout=10000)
            for sel in ["button:has-text('Continue with email')", "button[type=submit]"]:
                try: pg.click(sel, timeout=4000); break
                except Exception: pass
            pg.wait_for_timeout(3500)
            has_pw = pg.locator("input[type=password]").count() > 0
            print(f"[{tag}] 密碼欄={has_pw} url={pg.url}")
            if not has_pw:
                body = re.sub(r"\s+"," ",pg.inner_text("body"))
                print(f"[{tag}] 冇密碼欄（可能未註冊） body={body[:200]}")
                res.append(f"{tag}: 無密碼欄（疑似未註冊）"); ctx.close(); continue
            pg.fill("input[type=password]", pw, timeout=8000)
            for sel in ["button[type=submit]","button:has-text('Sign in')","button:has-text('Continue')"]:
                try: pg.click(sel, timeout=4000); break
                except Exception: pass
            pg.wait_for_timeout(6000)
            body = re.sub(r"\s+"," ",pg.inner_text("body"))
            captcha = "Please complete the captcha" in body
            print(f"[{tag}] 提交後 url={pg.url} captcha={captcha}")
            print(f"[{tag}] body={body[:260]}")
            tok = next((c["value"] for c in ctx.cookies() if c["name"]=="session_token"), "")
            if tok:
                print(f"[{tag}] ✅ 有 session_token（長度 {len(tok)}）")
            if pg.url.rstrip("/").endswith("/a") or "/a/" in pg.url:
                pg.goto("https://bot-hosting.net/a/billings", wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(2500)
                t2 = re.sub(r"\s+"," ",pg.inner_text("body"))
                print(f"[{tag}] billings: 有bot={'No renewal needed' not in t2}")
            res.append(f"{tag}: captcha={captcha} token={'有' if tok else '無'} url={pg.url[:60]}")
        except Exception as e:
            print(f"[{tag}] ✗ {type(e).__name__}: {str(e)[:120]}")
            res.append(f"{tag}: 例外 {type(e).__name__}")
        finally:
            ctx.close()
    br.close()
print("\n=== 結論 ===")
for r in res: print(" -", r)
