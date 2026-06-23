"""單檔 HTML 報表 — 把回測結果變成可直接用瀏覽器打開的「版面」。

自包含：圖以 base64 內嵌，不需伺服器、不依賴外部檔。產出 report.html 直接點開即可看。
"""
from __future__ import annotations
import base64
import os
import tempfile
from html import escape

from core.plotting import plot_equity


def _img_b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,'PingFang TC','Microsoft JhengHei',sans-serif;
 margin:0;padding:24px;color:#111827;background:#f9fafb;line-height:1.6}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
h2{font-size:15px;font-weight:600;margin:24px 0 10px;color:#374151}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:120px}
.card .v{font-size:20px;font-weight:600}
.card .k{font-size:12px;color:#6b7280;margin-top:2px}
.pos{color:#16a34a}.neg{color:#dc2626}
img{max-width:100%;border:1px solid #e5e7eb;border-radius:10px;background:#fff}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #f3f4f6}
th{background:#f3f4f6;font-weight:600;color:#374151}
td:first-child,th:first-child{text-align:left}
.foot{color:#9ca3af;font-size:12px;margin-top:20px}
"""


def build_report(result, title: str = "Backtest report", out: str = "report.html",
                 heatmap_path: str | None = None) -> str:
    """產生單檔 HTML 報表。result 為 BacktestResult。回傳 out 路徑。"""
    tmp = tempfile.mkdtemp()
    eq_png = plot_equity(result, os.path.join(tmp, "eq.png"), title=title)
    eq_b64 = _img_b64(eq_png)

    def cls(x):
        return "pos" if x > 0 else ("neg" if x < 0 else "")

    cards = [
        ("Total return", f"{result.total_return * 100:+.2f}%", cls(result.total_return)),
        ("Max drawdown", f"{result.max_drawdown * 100:.2f}%", "neg"),
        ("Win rate", f"{result.win_rate * 100:.1f}%", ""),
        ("Sharpe", f"{result.sharpe:.2f}", cls(result.sharpe)),
        ("Trades", str(len(result.trades)), ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="v {c}">{escape(v)}</div>'
        f'<div class="k">{escape(k)}</div></div>' for k, v, c in cards)

    rows = ""
    for t in result.trades[-25:]:
        pnl = float(t.get("pnl", 0.0))
        rows += (
            f"<tr><td>{escape(str(t.get('ts', '')))}</td>"
            f"<td>{escape(str(t.get('side', '')))}</td>"
            f"<td>{int(t.get('dir', 1))}</td>"
            f"<td>{float(t.get('price', 0.0)):.2f}</td>"
            f"<td>{float(t.get('qty', 0.0)):.6f}</td>"
            f'<td class="{cls(pnl)}">{pnl:+.2f}</td></tr>')
    if not rows:
        rows = '<tr><td colspan="6">（無已平倉交易）</td></tr>'

    heatmap_html = ""
    if heatmap_path and os.path.exists(heatmap_path):
        heatmap_html = ('<h2>Parameter heatmap</h2>'
                        f'<img src="data:image/png;base64,{_img_b64(heatmap_path)}">')

    html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{_CSS}</style></head>
<body>
<h1>{escape(title)}</h1>
<div class="sub">丹丹交易團隊 — 模擬盤回測報表（測試網／虛擬資金；非投資建議）</div>
<div class="cards">{cards_html}</div>
<h2>Equity curve &amp; drawdown</h2>
<img src="data:image/png;base64,{eq_b64}">
{heatmap_html}
<h2>Recent trades (last 25)</h2>
<table><thead><tr><th>time</th><th>side</th><th>dir</th><th>price</th><th>qty</th><th>pnl</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="foot">回測漂亮 ≠ 實盤賺錢。樣本要夠多、小心過度擬合。</div>
</body></html>"""

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out
