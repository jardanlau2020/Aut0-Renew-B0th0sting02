## 🚀 Bot-hosting 自动续期（GitHub Actions）

基于 GitHub Actions 定时登录 [Bot-hosting](https://bot-hosting.net) 自动续期（每 4 天可续一次）。

⚠️ 面板前面有 Cloudflare Turnstile，机房 IP 基本过不了，**必须挂干净点的节点**（住宅代理）。
建议用 [B2proxy 住宅代理](https://www.b2proxy.com/signup?code=0F5133)。

> **本仓库已迁移到共享骨架 [renew-kit](https://github.com/jardanlau2020/renew-kit) `v0.4.2`。**
> 续期逻辑（Turnstile 检测、shadow DOM 探 token、按钮解锁轮询、Discord OAuth）没动，
> 换掉的是**通知排版 + 退出码口径**，并把代理引导收敛成 `scripts/setup_proxy.sh`。

━━━━━━━━━━━━━━━━━━━━━━

## 一、退出码与结果语义（迁移后）

原版退出码是 `1 / 2 / 5 / 4 / 4 / 3|6`，各代表一种失败细节；现在**收敛成 0/1**，
由 renew-kit 的 `Outcome` 统一决定：

| 状态串（内部协议）      | Outcome     | 退出码 | 会发 TG？ | 说明                          |
|------------------------|-------------|--------|-----------|-------------------------------|
| `✅ 续期成功`           | `RENEWED`   | 0      | ✅        | 到期日/成功提示已确认变化        |
| `⏳ 未到续期时间`       | `SKIPPED`   | 0      | ❌ 静默   | 还在 4 天冷却窗口内，正常        |
| `ℹ️ 无需续期`           | `UNKNOWN`   | 0      | ✅        | 连按钮/倒计时都读不到，需留意     |
| `❌ 登录失败`           | `FAILED`    | 1      | ✅        | 红。Cookie 失效且 Discord 也登不上 |
| `❌ 续期失败`           | `FAILED`    | 1      | ✅        | 红。见下方细分                  |
| `❌ 脚本异常中断`       | `FAILED`    | 1      | ✅        | 红。未捕获异常，已附 traceback   |

**只有 `FAILED` 会把 job 标红**；`UNKNOWN` 会通知但不标红（避免把「读不到状态」误判成业务失败）。

### 为什么「未到续期时间」默认静默？

bot-hosting 是 **4 天** 续一次，而 cron 是**每天**跑 —— 每 4 次运行里有 3 次纯属噪音。
所以默认只在这三种情况出声：**真续成功**（✅）、**真失败**（🚨）、**状态读不到**（❓）。

想把每日心跳要回来，不用改代码，给 workflow 的续期 step 加一个环境变量即可：

```yaml
env:
  AUT0_NOTIFY_SKIP: '1'      # 连 SKIPPED 也发通知
```

### 一处**有意保留的偏离**：点了但后台未确认 → 仍然标红

原脚本在「已点击但后台未确认」（原 `fail_code=3`）处留了明确注释：
*「不能只发警告后 return 0，否则 Actions 会显示 success」*。
bot-hosting 漏续会**删号**，宁可红不可绿 —— 所以这里保持 `FAILED`（exit 1），
没有收敛成 `UNKNOWN`。区别体现在通知的 detail 文字里：

- 按钮点着了、后台没确认 → `按钮已点击但后台未确认（旧日 → 新日），可能续得太早被拒，明日窗口临近会自动重试`
- 按钮压根没点着 → `弹窗内「Renew for 4 days」按钮没点着（原因），需人工检查`

━━━━━━━━━━━━━━━━━━━━━━

## 二、Secrets 配置

| Secret 名称    | 是否必填 | 说明 |
|----------------|----------|------|
| `SESSION_TOKEN`| **推荐** | Bot-hosting `session_token`（cookie 里取）。**建议**配，否则只能靠 Discord 备用登录 |
| `DISCORD_TOKEN`| 可选     | Discord Token，`SESSION_TOKEN` 失效时走 OAuth 重新登录 |
| `GH_TOKEN`     | 可选     | GitHub **classic** PAT（`ghp_` 开头），用于自动回写 `SESSION_TOKEN` |
| `NODE_LINK`    | **强烈建议** | 代理链接（`vless://` `vmess://` `trojan://` `hysteria2://` `tuic://` `anytls://` `socks5://`） |
| `TG_BOT_TOKEN` | 可选     | Telegram Bot Token |
| `TG_CHAT_ID`   | 可选     | Telegram Chat ID |
| `EMAIL`        | 可选     | 只用于通知里显示遮罩邮箱（如 `ab****yz@x.com`），随便填 |

> ⚠️ **`SESSION_TOKEN` 与 `DISCORD_TOKEN` 至少要有一个**，两个都空会直接报
> `❌ 登录失败: 未配置 SESSION_TOKEN 和 DISCORD_TOKEN` 并 exit 1。
>
> ⚠️ **本仓库当前没有配置 `DISCORD_TOKEN`**。原 README 把它标成必填，但仓库 Secrets 里
> 并不存在这个 key，workflow 里那行 `DISCORD_TOKEN: ${{ secrets.DISCORD_TOKEN }}`
> 解析成空串 —— 也就是说 Discord 备用登录目前是**死配置**，实际只跑 `SESSION_TOKEN`。
> 要么补上这个 Secret，要么就别指望 `SESSION_TOKEN` 失效后能自动救回来。

### 通知

`TG_BOT_TOKEN` / `TG_CHAT_ID` 两个都配上才会发通知，少一个就**静默跳过**
（兼容别名 `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`）。
通知失败只算通知失败 —— 绝不会把续期结果判成失败。

━━━━━━━━━━━━━━━━━━━━━━

## 三、代理（sing-box）与 `NODE_LINK`

代理由 `scripts/setup_proxy.sh` 引导，作为 renew-kit action 的 `setup-command` 执行：

1. **检查 `NODE_LINK`** —— 空值时打 `::warning::` 并提示去配 Secret
2. **装 sing-box** —— 调上游 installer（`https://main.ssss.nyc.mn/setup_proxy.sh`）
3. **真验证出口** —— 经 `socks5h://127.0.0.1:1080` 真连一次 `api.ipify.org`，
   拿到 IP 才算通（**不看进程**：装到一半挂掉时进程可能还在但代理是死的）
4. **写 `$GITHUB_ENV`** —— 同时写 `IS_PROXY` 和 `PROXY_SERVER` 给后续 step

口径说明：上游 installer 自己也会写 `IS_PROXY`，但那是「进程起来了」的口径；
本脚本「真探通了」的口径才是最终值（`GITHUB_ENV` 同名后者覆盖前者）。
`app.py` 只认 `IS_PROXY=="true"`，两处（`run_all` 与 `discord_authorize`）口径必须一致。

可覆盖的环境变量：`AUT0_SINGBOX_PORT`（默认 1080）、`AUT0_PROXY_PROBE_URL`、
`AUT0_PROXY_INSTALLER`。

> **`NODE_LINK` 为空 = 直连 = 大概率撞 Turnstile 过不去。** 这是最常见的失败原因，
> 日志里看到 `##[warning]NODE_LINK 未传入` 就先去配 Secret。

━━━━━━━━━━━━━━━━━━━━━━

## 四、手动运行 / 演练

Actions → *Auto Renew Bot-hosting* → **Run workflow**，有 `dry_run` 开关：

- `dry_run = true` → 照常登录、照常读状态，**但不回写 `SESSION_TOKEN` 到 Secret**
  （原版没有演练开关，手动跑一次就会动真实 Secret）

命令行本地跑（需要 `DISPLAY` 或 `xvfb-run`）：

```bash
# 只读状态、不发 TG（未配 TG 时 notify 会自动跳过）
SESSION_TOKEN=xxx ACCOUNT_LABEL=02 xvfb-run --auto-servernum python3 app.py

# 演练：不回写 Secret
SESSION_TOKEN=xxx DRY_RUN=1 xvfb-run --auto-servernum python3 app.py
```

━━━━━━━━━━━━━━━━━━━━━━

## 五、环境变量全表

| 变量              | 默认值                   | 说明 |
|-------------------|--------------------------|------|
| `SESSION_TOKEN`   | —                        | 默认登录方式 |
| `DISCORD_TOKEN`   | —                        | 备用登录（支持 `"label,token"` 两段式） |
| `GH_TOKEN`        | —                        | 回写 `SESSION_TOKEN` 用；不填就只打印新值 |
| `EMAIL`           | —                        | 通知里的遮罩邮箱 |
| `ACCOUNT_LABEL`   | —                        | 账号标识（本 workflow 传 `"02"`），进报告名 `Bot-hosting（02）` |
| `IS_PROXY`        | `false`                  | 由 `setup_proxy.sh` 写；`"true"` 才走代理 |
| `PROXY_SERVER`    | `http://127.0.0.1:1080`  | 同上 |
| `HEADLESS`        | `false`                  | 无头模式 |
| `DRY_RUN`         | 空                       | 非空即演练：跳过回写 `SESSION_TOKEN` |
| `AUT0_NOTIFY_SKIP`| 空                       | 非空则连 `SKIPPED` 也发通知（恢复每日心跳） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | —            | 由 `renewkit.notify` 直接读（支持 `TELEGRAM_*` 别名） |

> 迁移时删掉的死变量：`TG_BOT`（`"chat_id,token"` 兼容写法）、以及原版散在
> `main()` 里的 `fail_code` 之类内部标志 —— 它们已不再被读取。

━━━━━━━━━━━━━━━━━━━━━━

## 六、部署步骤

1. fork 本项目，在 Actions 菜单允许工作流
2. 在 `Settings` → `Secrets and variables` → `Actions` 里加上方表格里的 Secrets
   （**至少 `SESSION_TOKEN` 或 `DISCORD_TOKEN` 之一，强烈建议配 `NODE_LINK`**）
3. 去 Actions 菜单手动试运行工作流；按自己的到期日调整
   [renew.yml](.github/workflows/renew.yml) 里的 cron

### SESSION_TOKEN 获取

登录你的账号 → F12 或右键「检查」→ 应用程序（Application）→ Cookies → 找到
`session_token` 字段取值。详见下图：

<img width="1200" height="600" alt="image" src="https://github.com/user-attachments/assets/e532b0d6-9f12-45fd-8af9-69e1029a1a92" />

### DISCORD_TOKEN 获取（`SESSION_TOKEN` 失效后的备用登录）

1. 浏览器登录 Discord（网页版）
2. 按 F12 → 网络（Network）→ 点击任意频道 → 选左侧任意 api 请求
3. 找到 `authorization` 字段的值，即为 Discord Token

<img width="1200" height="600" alt="image" src="https://github.com/user-attachments/assets/7276d62d-31ff-452c-9e13-165af8323f53" />

> **作用**：`SESSION_TOKEN` 过期导致登录失败时，脚本自动用 Discord Token 走 OAuth
> 重新登录，并回写新的 `SESSION_TOKEN`（需要 `GH_TOKEN`）。

### 获取 `GH_TOKEN`（GitHub Personal Access Token）

1. 右上角头像 → Settings
2. 左侧菜单底部 → Developer settings
3. Personal access tokens → **Tokens (classic)**
4. Generate new token → Generate new token (classic)

填写信息：
- Note：起个描述性名称（如 `bot-hosting-renew`）
- Expiration：建议选 No expiration
- Select scopes：勾选 `repo` + `workflow`（不确定就全勾）
- 生成后立即复制保存（离开页面就看不到了）

━━━━━━━━━━━━━━━━━━━━━━

## 七、cron 与运行记录

- 定时：`20 1 * * *`（UTC 01:20 = 北京 09:20）。**故意避开 0 点整** ——
  GitHub schedule 在整点排队高峰常有数小时延迟（实测迟过 4h47m）。
- 运行记录只保留**最近 5 条**（`gh run delete`），所以出问题时**尽快回看日志**。
- 失败截图（`*.png`）作为 artifact 上传，保留 **3 天**（由 renew-kit action 统一设定）。

━━━━━━━━━━━━━━━━━━━━━━

## 八、离线验收

不需要 seleniumbase、不需要网络，也不需要浏览器：

```bash
python .verify/verify_aut0.py
```

覆盖四段：

- **[A] 纯逻辑** —— `_outcome_of` 映射矩阵、`_norm_expiry`（斜杠→连字符这个坑）、
  `_masked_email`、`_target_name`、`_record`（小标题/优先级/截断/归一）
- **[B] 场景矩阵** —— 用假 SB 驱动 `run_all()`，逐条核对 Outcome / 退出码 / 通知 /
  渲染文本，含「天数不重复」「报告名不被 cookie 循环变量污染」等回归断言
- **[C] 静态与接线** —— 死符号、`sys.exit` 收敛、renew-kit 接线、
  **workflow ↔ 代码 env 双向一致**、`setup_proxy.sh` 契约
- **[D] 子进程端到端** —— 真跑 `python app.py`，核对退出码与打印出的报告

━━━━━━━━━━━━━━━━━━━━━━

## 注意事项

* `NODE_LINK` 支持的代理协议：`vmess` `vless` `hysteria2` `tuic` `anytls` `socks5` 等
* 自动续期不代表可以无底线地薅羊毛，不建议多账号
* cron 运行时间不一定准确，可根据实际到期时间修改；也可在设置里暂停 Actions 再开启

## ⚠️ 免责声明

* 本程序仅供学习了解，非盈利目的，如转载须注明来源。
* 使用本程序必须遵守部署服务器所在地、所在国家和用户所在国家的法律法规，
  程序作者不对使用者的任何不当行为负责。
