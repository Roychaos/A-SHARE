# a-share-screener —— A股量价选股 Agent（MVP / 路线A）

零成本、每日收盘后自动运行的 A股量价选股流水线：
**形态模板匹配（学习历史起步型赢家）+ 量价信号打分 → TopN → LLM 形态文案 → 图文卡片 → 企业微信/Server酱推送。**

架构与设计细节见同级目录主文档：
- 需求与开源对标：《A股量价选股Agent_GitHub调研与可行性报告.md》
- 本仓库实现依据：《路线A_MVP技术架构与执行方案.md》

## 目录

```text
.github/workflows/daily-screener.yml   # 每日 15:45(北京时间) 定时任务
config.example.yaml                    # 全部可调参数（复制为 config.yaml）
requirements.txt
scripts/          # init_db / backfill / update_templates / run_daily / validate
                  # (Phase3+: test_push.py)
src/              # config / data / patterns / signals / screen / utils
                  # (Phase3+: report / push)
data/  output/    # SQLite 库与每日产物（.gitignore）
```

## 当前进度

**Phase 0 数据底座 ✅ + Phase 1 形态学习 ✅ + Phase 2 筛选引擎 ✅（回测验证通过）**，共 91 项离线冒烟测试。

> 回测结论（20/40 天样本外、上涨+下跌两种行情）：**纯形态相似度命中率 ~69%、超额 +17pct，稳健为正**；
> 追涨因子(F1/F4/F5)与趋势层为反指，已从评分剔除（仅保留避雷针/爆量/暴热板块做排雷硬过滤）。
> 当前 `scoring.weights = {pattern_sim: 1.0, signal: 0, trend: 0}`。

```
python tests/test_phase0.py     # 41项: 存储层/板块分类/重试/交易日历/配置助手
python tests/test_phase1.py     # 19项: 锚点定义/模板提取/ST排除/幂等落库
python tests/test_phase2.py     # 19项: 相似度/5个量价信号/打分/选股/scan_result存储
```

代码结构（第三方库 akshare/pandas 均为函数内惰性导入，离线可编译测试）：

```text
src/config.py               # YAML 配置加载（config.yaml → 自动回退 example）
src/data/store.py           # SQLite 建表与读写（仅标准库 sqlite3）
src/data/universe.py        # 板块分类/ST识别/股票列表（纯函数）
src/data/fetcher.py         # akshare 日线抓取：重试/复权降级/增量更新
src/patterns/similarity.py   # ★相似度匹配: 当前窗口 vs 模板(Pearson+量比)
src/signals/rules.py         # ★5个量价信号(温和放量/平台突破/均线金叉/回踩企稳/OBV新高)
src/screen/scorer.py         # ★加权打分+TopN选股+涨停过滤+行业去重
scripts/init_db.py          # 建库（幂等）
scripts/backfill.py         # 首次回填/续跑（--limit 联调）
scripts/update_templates.py # ★模板库更新 + 统计报告 output/templates_report_*.md
scripts/validate.py         # ★样本外回放验证器(命中率 vs 随机基线) -> Phase 3 门槛
scripts/run_daily.py        # 每日入口：非交易日跳过→增量更新→选股落库
```

模板学习的设计要点（详见主文档 §6.2）：
- 「起步型赢家」锚点 = 未来10日涨≥9% 且 当日涨≥2% 且 前10日没怎么涨（真正的上涨初期）；
- 只保存**启动前** 25 根K线的归一化窗口（价格 zscore + 量比序列），无未来函数；
- 每日限 20 条、剔除 ST、过滤单调上涨噪声；重跑幂等（按锚点区间替换）。

## 本地快速起步（需要装依赖后执行）

```bash
pip install -r requirements.txt
copy config.example.yaml config.yaml        # 按需修改（密钥走环境变量，不写进文件）
python scripts/init_db.py                   # 建表
python scripts/backfill.py --limit 20       # 联调：先拉20只看数据是否正常
python scripts/backfill.py                  # 全量：近 fetch.years_back(默认1)年日线
python scripts/update_templates.py          # ★学习形态: 生成赢家模板库+统计报告
python scripts/run_daily.py                 # 每日增量 + 选股落库（scan_result）
python scripts/validate.py --days 40        # ★样本外回放: 验证命中率 vs 随机基线
                                            # (首次可 --days 5 --limit 300 快速冒烟)
```

> 注意：本仓库代码在受限沙箱中已完成离线验证，但沙箱禁止安装 pip 依赖，
> 请在**你自己电脑**执行上面 `pip install` 后跑真实数据（akshare 拉全A约需 20–60 分钟）。

## 需要的密钥（环境变量；CI 里放 GitHub Secrets）

| 变量 | 必填 | 说明 |
|---|---|---|
| `WECOM_WEBHOOK_KEY` | 推送用 | **主通道**：企业微信里建群（把想推送的人拉进来）→ 群机器人 → 复制的 webhook key |
| `DEEPSEEK_API_KEY` | 可选 | 形态文案 LLM；缺失时自动用模板文案兜底 |
| `SERVERCHAN_SENDKEY` | 可选 | Server酱备用通道（只推自己微信） |

> 企业微信群机器人开通（2 分钟）：企业微信 App → 发起群聊（拉入想推送的人）→ 群设置 → 群机器人 → 添加 → 复制 webhook 地址中的 key。

## 验收节奏（对应主文档 §9）

- Phase 0：全A近3年日线入库，手动跑通 workflow；
- Phase 1：模板库 + 自证测试（起步型赢家应被自身模板匹配进 Top-5%）；
- Phase 2：6个月回放验证器（scripts/validate.py），命中率须显著高于随机基线；
- Phase 3：微信图文推送上线。

> 仅供研究学习，不构成投资建议。
