"""临时调试：直连查看新浪全市场快照接口的真实返回。"""
import akshare as ak

print("akshare 版本:", getattr(ak, "__version__", "?"))
fn = getattr(ak, "stock_zh_a_spot", None)
print("stock_zh_a_spot 是否存在:", callable(fn))
if callable(fn):
    try:
        df = fn()
        print("返回类型:", type(df).__name__)
        print("行数,列数:", getattr(df, "shape", None))
        if hasattr(df, "columns"):
            print("列名:", list(df.columns))
        if hasattr(df, "head"):
            print("前3行:")
            print(df.head(3).to_string())
    except Exception as exc:  # noqa: BLE001
        print("调用异常:", type(exc).__name__, exc)
