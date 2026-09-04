# 部署到 GitHub + 每日自动推送到微信（含 jsDelivr 图床）

目标：每天 A股收盘后自动「拉数据 → 学形态 → 选股 Top5 → 出图文 → 推送到你微信(Server酱)」，全程免费。

## 0. 前置

- 一个 GitHub 账号（免费即可）；
- 本机装好 git（`git --version` 能出结果）；
- 仓库需设为 **Public**（jsDelivr 图床要求公开仓库）。

## 1. 初始化并推送代码

```bash
cd "C:\Users\Admin\Documents\Agent课程学习\a-share-screener"
git init
git add .
git commit -m "init: A股量价选股 agent"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

> `.gitignore` 已排除 `data/`（数据库）、`output/`（图片）、`config.yaml`（含本地密钥）——它们不上传，符合安全要求。

## 2. 仓库设为 Public

GitHub 仓库页 → Settings → General → Danger Zone → Change visibility → **Public**。
（若必须私有，jsDelivr 图床不可用，需改用七牛/COS，另见文末。）

## 3. 配置 Secrets（密钥）

仓库 → Settings → Secrets and variables → Actions → New repository secret，加：

| 名称 | 值 | 必填 |
|---|---|---|
| `SERVERCHAN_SENDKEY` | 你的 Server酱 SendKey（SCT 开头） | ✅ |
| `DEEPSEEK_API_KEY` | DeepSeek API key | 可选（无则用模板文案） |

## 4. 手动触发第一次（验证 + 预热数据）

仓库 → Actions → 左侧 `daily-screener` → Run workflow → 选 main → Run。

- **第一次会全量回填约 1 年日线**（约 1~2 小时），并把数据库存成 artifact 供次日复用；
- 之后每天 15:45（北京时间）自动跑，数据库每天增量、当天归档。

## 5. 之后每天自动发生什么

1. 15:45 cron 触发；
2. 从 artifact 恢复昨天的数据库；
3. `run_daily.py`：增量拉当日行情 → 学形态 → 选股 Top5 → 生成图文卡片（不推送）；
4. 把图片 commit 回仓库（jsDelivr 图床）；
5. 数据库存回 artifact；
6. `emit_report.py --push-only`：用 jsDelivr 直链把 Top3 图片 + Top5 摘要推到你的微信。

## 数据持久化说明

GitHub Actions 每次是全新机器，所以数据库用 **artifact**（`screener-db`）在两次运行间传递：
- 结束前 upload，下次开始时 download；
- 若哪天 artifact 过期（默认保留 30 天，但每天都会刷新），重新触发一次手动回填即可。

## 图床机制

- 图片提交到仓库 `output/日期/*.png`；
- 直链 = `https://cdn.jsdelivr.net/gh/用户名/仓库名@main/output/日期/xx.png`（jsDelivr 免费 CDN，国内可访问）；
- Server酱 markdown 用 `![图](直链)` 内嵌。

## 常见问题

- **私有仓库能不能图床？** 不能（jsDelivr 只能拉公开仓库）。私有方案：七牛云/腾讯云COS 传图（需另配）。
- **想改推送对象/群推？** 把 `config.yaml` 的 `push.channels` 改成 `["wecom"]` 并用企业微信群机器人。
- **只想自己本机跑？** 依然可用：`python scripts/run_daily.py` + `python scripts/emit_report.py`。

> 仅供研究学习，不构成投资建议。
