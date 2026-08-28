# 📡 财经信息监控系统

监控你选择的 X（推特）博主 → AI 筛选有市场影响的信息 → 自动推送到**企业微信群**。
部署在 GitHub 云端，**7×24 小时自动运行，不需要你的电脑开机**。

**2026-08-28 扩展：多信源 + 随时登录的网站 + 每日晨会**

- **信息源不再只有X**：新增财经日历（未来30天）、SEC 公司公告、中文/英文快讯 RSS、币安公告
- **行情情绪快照**：跨市场行情（美元指数/美债30Y/原油/纳指/KOSPI/币价/黄金）、恐惧贪婪指数、资金费率、Polymarket 降息概率
- **网站「财经信息台」**：手机随时打开看日历（L1-L4定级）+ 事件流 + 行情读数（Cloudflare Pages，邮箱验证码登录）
- **每日晨会**：每天早上7点推一条——今日事件+未来7天看点+隔夜行情+情绪读数+仓位提醒

---

## 🖥️ 日常操作：管理博主名单

1. 双击文件夹里的 **`启动管理页面.bat`**
2. 浏览器自动打开管理页：
   - **添加博主**：点「➕ 添加博主」，handle 填 X 用户名（不带 @，如 `elonmusk`），备注随意
   - **删除博主**：点该行「删」
   - **改关键词**：直接编辑两个文本框（每行一个词）
3. 点「**💾 保存并同步**」→ 显示"已同步到 GitHub"即生效
4. **约 5 分钟后**云端自动按新名单运行

> 💡 新加的博主第一轮只记录位置、**不会**推送历史推文（防轰炸设计），之后发的推文才会推送。

---

## ⚙️ 配置文件说明（config.yaml）

一般只用管理页面就够。想深度调整时可手动编辑：

| 配置 | 说明 |
|---|---|
| `accounts` | 监控名单（管理页面维护） |
| `keywords` | 关键词白名单，命中才提醒 |
| `exclude_keywords` | 黑名单，含这些词直接忽略 |
| `min_score` | AI 影响力评分门槛（1-10，默认6） |
| `provider` | 数据源：`x_graphql`（免费直连）/ `twitterapi_io`（付费备用） |
| `push_channel` | 推送：`wecom`（当前）/ `pushplus`（备用） |

⚠️ `wecom_webhook`、`twitter_auth_token`、AI 密钥**永远不要写进 config.yaml**（该文件会上传公开仓库），密钥只存放在 GitHub Secrets。

---

## 🔧 故障排查

| 症状 | 处理 |
|---|---|
| 收不到推送 | 1) 查 GitHub 仓库 Actions 页有没有红色❌ 2) 企业微信群里机器人还在不在 3) 关键词是否太严（去 Actions 日志看"关键词过滤"数字） |
| 推送标"未评级" | OpenRouter 免费模型当日额度用完（免费档约50次/天），次日恢复；期间关键词命中的照常推送 |
| 某博主一直没消息 | X 接口偶发限流，程序会自动重试；持续失败可到 Actions 看日志 |
| 想换 AI | config.yaml 改 `ai_base_url` 和 `ai_model`（任意 OpenAI 兼容接口） |
| 免费数据源失效 | config.yaml 把 `provider` 改成 `twitterapi_io`，并在 Secrets 加 `TWITTERAPI_IO_KEY`（约$2-5/月） |

---

## 📁 文件结构

```
├── monitor.py            # 主程序（X监控 + 扩展信源 + 行情快照 + 晨会）
├── config.yaml           # 主配置（管理页面自动维护）
├── sources.yaml          # 扩展信息源配置（开关/名单，独立文件防管理页误覆盖）
├── state.json            # 去重状态（云端自动更新，勿手改）
├── data/                 # 生成数据：events.json 事件流 / calendar.json 日历 / market.json 行情
├── site/                 # 网站部署目录（index.html + data/，Cloudflare Pages 发布）
├── web_manager.py        # 本地管理页面服务
├── 启动管理页面.bat       # ← 日常入口
├── samples/              # 本地测试数据（mock 模式用）
└── .github/workflows/    # 财经监控(每5分钟) / 每日晨会(早7点) / AI自检
```

## 🌐 网站「财经信息台」

- 地址：部署 Cloudflare Pages 后生成（见下方配置步骤）
- 内容：跨市场行情、情绪读数、未来30天事件日历（自动定级 L1 消息级 / L2 数据级 / L3 事件级 / L4 格局级）、全部事件流（AI评分+中文翻译）
- 数据每5分钟随监控运行自动更新，网页每分钟自动刷新
- 日历中带「预估日期」标记的是按惯例推算（CPI≈每月10日、PCE≈月末），以官方公告为准；FOMC 日期表在 `monitor.py` 的 `_FOMC_DECISIONS`，每年初更新一次

## 💰 成本

全免费方案：GitHub Actions（公开仓库无限时长）+ X 直连接口 + OpenRouter 免费模型 + 企业微信机器人。
唯一可能的付费项：X 免费接口若失效，切换按量付费接口约 $2-5/月。

---

## ☁️ Cloudflare Pages 配置步骤（一次性，约10分钟）

1. 注册/登录 [dash.cloudflare.com](https://dash.cloudflare.com)
2. 左侧 **Workers & Pages** → **Create** → **Pages** → **Connect to Git** → 授权并选择 `finance-monitor` 仓库 → **Begin setup**
3. 构建设置：Framework preset 选 **None**；**Build output directory 填 `site`**（其余留空）→ **Save and Deploy**
4. 部署完成后得到 `https://finance-monitor.pages.dev`，手机即可访问
5. 加登录门槛（可选但推荐）：该 Pages 项目 → **Settings** → **Access** → **Enable** → 添加一条 Policy（Allow / Emails / 填你自己的邮箱）——之后打开网站需邮箱验证码登录，陌生人进不来

> 每次监控运行提交数据后，Pages 会自动重新部署，无需手动操作。

---

## 🌅 每日晨会

每天北京时间 **7:00** 自动推送一条晨会到企业微信群：今日日历事件（含预期/前值）、未来7天 L2+ 看点、隔夜行情、恐惧贪婪/资金费率/降息概率读数、事件前仓位提醒。也可在 Actions 页手动触发「每日晨会」立即看效果。

## 🔌 扩展信息源开关（sources.yaml）

| 源 | 内容 | 说明 |
|---|---|---|
| `ff_calendar` | 财经日历 | ForexFactory本周精确数据 + 非农/CPI/PCE推算 + FOMC固定表 |
| `binance_ann` | 币安公告 | 上币/合约上线（官方接口，失败自动换备胎） |
| `edgar` | SEC公告 | 美光/Strategy/英伟达/特斯拉等白名单公司 8-K/财报，companies 里可自行加 CIK |
| `rss` | 快讯 | 财联社电报/TheBlock/CoinDesk/律动（每源可配多个地址自动兜底） |
| `market` / `funding` / `fng` / `polymarket` | 读数 | 只进网站快照，不推送 |

非X来源默认**只进网站不推手机**；AI 评分 ≥8 分的重大事件才会推送企微（`push_min_score` 可调）。
