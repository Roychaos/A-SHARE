"""从 GitHub Actions 产物里挑「最完整」的历史库并恢复。

为什么不直接拿最新的产物：
    产物是滚动快照，而最新那个很可能来自一次「从旧种子库起步」的运行 ——
    它的最新日期很新（快照把当天补上了），但中间缺了一大段交易日
    （整市场快照只补当天，逐股循环又会跳过"最新日期 >= 目标日期"的股票，缺口永不愈合）。
    直接拿最新 → 每天继续在带洞的库上选股。

所以评分标准是「最近 30 个交易日里缺了几天」：缺得越少越优先，同分取更新的。
一旦遇到「0 缺失且最新日期足够新」的候选就立刻采用，不再继续下载。

环境变量：
    GH_TOKEN / GITHUB_TOKEN   必填（需 actions:read 权限）
    GITHUB_REPOSITORY         必填，形如 owner/repo（Actions 里自动有）
    DB_PATH                   可选，默认 data/screener.db
    DB_ARTIFACT_NAME          可选，默认 screener-db
    DB_LOOKBACK_DAYS          可选，默认 8（最多下载几个候选来评分）

用法：python scripts/restore_db.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
REPO = os.environ.get("GITHUB_REPOSITORY") or ""
DB_PATH = os.environ.get("DB_PATH", "data/screener.db")
NAME = os.environ.get("DB_ARTIFACT_NAME", "screener-db")
LOOKBACK = int(os.environ.get("DB_LOOKBACK_DAYS", "12"))
MIN_BARS_PER_DAY = 100      # 绝对下限
MISSING_RATIO = 0.80        # 当日入库 < 参照值 × 0.80 即算该日缺失（正常交易日入库率约 99.7%）
GOOD_ENOUGH_MISSING = 0     # 缺失天数 <= 此值 才可能提前停止扫描
BREAK_LAG_DAYS = 1          # 提前停止还需满足：库内最新日期距今不超过这么多天
                            # （留 1 天余量：昨天的库 + 今天整市场快照 = 完整）


def _freshness(maxd: str | None) -> int:
    """把库内最新日期转成可比较的值，越大越新；无效日期最差。"""
    try:
        return dt.date.fromisoformat(maxd or "").toordinal()
    except (TypeError, ValueError):
        return 0


def log(msg: str) -> None:
    print(msg, flush=True)


def _get(url: str, token: str | None = None, timeout: int = 120) -> bytes:
    headers = {"User-Agent": "restore-db", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def download_artifact(artifact_id: int, dest_zip: str) -> bool:
    """下载产物 zip。GitHub 会 302 到一个带签名的地址，带 token 跟随会失败，故手动处理。"""
    url = f"{API}/repos/{REPO}/actions/artifacts/{artifact_id}/zip"
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "restore-db",
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + TOKEN,
    })
    try:
        with opener.open(req, timeout=120) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code not in (301, 302, 303, 307, 308):
            log(f"  下载失败 HTTP {exc.code}: {exc.reason}")
            return False
        loc = exc.headers.get("Location")
        if not loc:
            log("  下载失败：重定向无 Location")
            return False
        try:
            data = _get(loc, token=None)          # 签名地址，不能再带 Authorization
        except Exception as exc2:  # noqa: BLE001
            log(f"  下载失败(签名地址): {type(exc2).__name__}: {exc2}")
            return False
    except Exception as exc:  # noqa: BLE001
        log(f"  下载失败: {type(exc).__name__}: {exc}")
        return False
    with open(dest_zip, "wb") as fh:
        fh.write(data)
    return True


def probe(db_file: str) -> tuple[int, str | None, int, list[str]]:
    """返回 (最近30个交易日缺失天数, 最新日期, 总根数, 缺失日期列表)。失败返回 (9999, None, 0, [原因])。

    判据：某日入库股票数 < max(中位数, 当日最大值, 股票池只数) × 0.80 即算缺失。

    为什么不用"中位数的 50%"（踩过的坑）：
        一次"补到一半就被中断"的运行，会留下这样一份库 —— 旧日期 5200 只、
        新日期只有 3000 只。中位数仍是 5200，50% 阈值 = 2600，
        于是 3000 只的那些天被误判为"完整"，半成品被当成好库采用。
    为什么不用绝对值：股票池规模会变，且停牌会让正常日也少几十只。
    现在用股票池只数做参照（正常交易日入库率约 99.7%），0.80 的阈值足够安全。
    """
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        maxd = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        if not maxd:
            return 9999, None, 0, ["库内无日线"]
        bars = int(con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0])
        universe = int(con.execute("SELECT COUNT(*) FROM stock_meta").fetchone()[0] or 0)
        start = (dt.date.fromisoformat(maxd) - dt.timedelta(days=30)).isoformat()
        cnt = dict(con.execute(
            "SELECT date, COUNT(*) FROM daily_bar WHERE date >= ? GROUP BY date", (start,)))
        days = [r[0] for r in con.execute(
            "SELECT date FROM trade_cal WHERE date >= ? AND date <= ? ORDER BY date", (start, maxd))]
        con.close()
        vals = [c for c in cnt.values() if c > 0]
        median = int(statistics.median(vals)) if vals else 0
        ref = max(median, max(vals) if vals else 0, universe)
        thr = max(MIN_BARS_PER_DAY, int(ref * MISSING_RATIO))
        missing = [d for d in days if cnt.get(d, 0) < thr]
        return len(missing), maxd, bars, missing
    except Exception as exc:  # noqa: BLE001
        return 9999, None, 0, [f"{type(exc).__name__}: {exc}"]


def main() -> int:
    if not TOKEN or not REPO:
        log("缺少 GH_TOKEN / GITHUB_REPOSITORY，跳过产物恢复")
        return 1

    try:
        payload = json.loads(_get(f"{API}/repos/{REPO}/actions/artifacts?name={NAME}&per_page=30",
                                  token=TOKEN))
    except Exception as exc:  # noqa: BLE001
        log(f"列产物失败: {type(exc).__name__}: {exc}")
        return 1

    arts = [a for a in payload.get("artifacts", []) if not a.get("expired")]
    arts.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    log(f"找到 {len(arts)} 个未过期产物（{NAME}），逐个评分（缺失交易日越少越好）：")
    if not arts:
        return 1

    today = dt.date.today()
    best: tuple | None = None
    tmpdir = tempfile.mkdtemp(prefix="dbrestore")
    try:
        for art in arts[:LOOKBACK]:
            zpath = os.path.join(tmpdir, f"{art['id']}.zip")
            if not download_artifact(int(art["id"]), zpath):
                continue
            xdir = os.path.join(tmpdir, str(art["id"]))
            os.makedirs(xdir, exist_ok=True)
            try:
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(xdir)
            except Exception as exc:  # noqa: BLE001
                log(f"  id={art['id']} 解压失败: {exc}")
                continue
            found = None
            for root, _dirs, files in os.walk(xdir):
                for fn in files:
                    if fn.endswith(".db"):
                        found = os.path.join(root, fn)
                        break
                if found:
                    break
            if not found:
                log(f"  id={art['id']} 压缩包里没有 .db 文件")
                continue

            missing, maxd, bars, miss_list = probe(found)
            tag = "OK" if missing == 0 else f"缺 {missing} 天"
            log(f"  id={art['id']}  创建 {art.get('created_at')}  "
                f"最新日期 {maxd}  {bars} 根  -> {tag}"
                + (f"  缺失: {', '.join(miss_list[:8])}" if missing and missing != 9999 else ""))
            # ★ 排序键 = (缺失天数, -最新日期)：先比"缺得少"，再比"日期新"。
            #   踩过的坑：以前写成 `missing < best[0]`（严格更优才替换），
            #   结果同为"缺 0 天"时先出现的旧库（最新只到 09-03）会赖着不走，
            #   把后面真正完整且最新的库（09-16）挡掉了。
            key = (missing, -_freshness(maxd))
            if best is None or key < best[0]:
                best = (key, art.get("created_at") or "", found, maxd, bars)
            lag = (today - dt.date.fromisoformat(maxd)).days if maxd else 9999
            if missing <= GOOD_ENOUGH_MISSING and lag <= BREAK_LAG_DAYS:
                log(f"  已是最好的情况（缺 {missing} 天、滞后 {lag} 天），停止继续下载")
                break
    finally:
        pass

    if not best or best[0][0] == 9999:
        log("所有候选都不可用")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    shutil.copyfile(best[2], DB_PATH)
    log(f"★ 选中：缺失 {best[0][0]} 天 / 库内最新 {best[3]} / {best[4]} 根 "
        f"（产物 id 见上表，创建于 {best[1]}）→ 已写入 {DB_PATH} "
        f"({os.path.getsize(DB_PATH) / 1048576:.1f} MB)")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
