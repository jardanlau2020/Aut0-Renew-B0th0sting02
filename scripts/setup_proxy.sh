#!/usr/bin/env bash
# 代理引导：装 sing-box → 验证真能出去 → 把结果写进 $GITHUB_ENV。
#
# 为什么是「真验证」而不是看进程：安装脚本本身可失败，装到一半挂掉时
# sing-box 进程可能还在，但代理并不通。只看 `pgrep -f sing-box` 就当通，
# 整个续期会跑在死代理上，最终表现成「面板连不上」这种没法定位的失败。
# 这里的口径是：真经代理连一次 api.ipify.org 拿到 IP，才算通。
#
# 本脚本是 renew-kit composite action 的 setup-command，默认 continue-on-error，
# 所以任何一步失败都不会中断续期 —— 大不了退回直连。
#
# ⚠️ 但「退回直连」不是无害的：bot-hosting 面板前面有 Cloudflare，runner 机房
# IP 在它的重点关照名单里，直连大概率直接撞 Turnstile 过不去。上游 installer
# 的入口是 `export NODE_LINK=${NODE_LINK:-''}`，取到空值就静默走
# 「未配置代理，直连模式」。所以第 0 步先把这件事喊出来。
#
# 与 1cehost 版的一处差异（有意为之）：这里**同时**写 IS_PROXY 和 PROXY_SERVER。
# 上游 installer 自己也会写 IS_PROXY，但它是在「进程起来了」的口径下写的，
# 与本脚本「真探通了」的口径不一致；两边都写时 GITHUB_ENV 后者覆盖前者，
# 于是最终状态由本脚本的探测结果说了算 —— app.py 只认 IS_PROXY=="true"
# （run_all 与 discord_authorize 两处都是这个口径），必须唯一，否则会出现
# 「IS_PROXY=true 但 PROXY_SERVER 是死的」这种自相矛盾的状态。
set -uo pipefail

PORT="${AUT0_SINGBOX_PORT:-1080}"
PROBE_URL="${AUT0_PROXY_PROBE_URL:-https://api.ipify.org}"
INSTALLER="${AUT0_PROXY_INSTALLER:-https://main.ssss.nyc.mn/setup_proxy.sh}"

echo "── 0/4 检查节点配置 ──"
if [ -z "${NODE_LINK:-}" ]; then
  echo "::warning::NODE_LINK 未传入 → 上游 installer 会退成直连，bot-hosting 面板的 Cloudflare 大概率过唔到。"
  echo "⚠️ 请确认 workflow 的 env 里有 NODE_LINK: \${{ secrets.NODE_LINK }}。"
else
  echo "✅ NODE_LINK 已传入（${#NODE_LINK} 字符），交给上游 installer 解析"
fi

echo "── 1/4 安装代理 ──"
if command -v wget >/dev/null 2>&1; then
  bash <(wget -qO- "$INSTALLER") || echo "⚠️ 安装脚本返回非零（继续验证）"
else
  bash <(curl -fsSL "$INSTALLER") || echo "⚠️ 安装脚本返回非零（继续验证）"
fi

echo "── 2/4 验证出口 ──"
proxy=""
if pgrep -f sing-box >/dev/null 2>&1; then
  for i in 1 2 3; do
    ip="$(curl -s --max-time 15 -x "socks5h://127.0.0.1:${PORT}" "$PROBE_URL" || true)"
    if [ -n "$ip" ]; then
      proxy="socks5://127.0.0.1:${PORT}"
      echo "✅ 第 ${i} 次探测成功，出口 IP: ${ip}"
      break
    fi
    echo "⚠️ 第 ${i} 次探测不通，3 秒后重试…"
    sleep 3
  done
  [ -n "$proxy" ] || echo "⚠️ sing-box 进程在但代理 3 次都不通 → 回退直连"
else
  echo "no proxy, direct mode"
fi

echo "── 3/4 导出给后续步骤 ──"
# 走 GITHUB_ENV 而不是 export：export 只活在本 step 的 shell 里，
# 后面的「Run renewal script」step 是另一个进程，看不到。
if [ -n "${GITHUB_ENV:-}" ]; then
  if [ -n "$proxy" ]; then
    echo "IS_PROXY=true" >> "$GITHUB_ENV"
    echo "PROXY_SERVER=${proxy}" >> "$GITHUB_ENV"
    echo "IS_PROXY=true / PROXY_SERVER=${proxy} 已写入 GITHUB_ENV"
  else
    # 明确写 false：上游 installer 可能已经写了 true，本脚本的探测结果才是
    # 最终口径（GITHUB_ENV 同名后者覆盖前者）。
    echo "IS_PROXY=false" >> "$GITHUB_ENV"
    echo "IS_PROXY=false 已写入 GITHUB_ENV（本次直连）"
  fi
else
  echo "⚠️ 非 Actions 环境（无 GITHUB_ENV），仅本 step 内生效"
fi
