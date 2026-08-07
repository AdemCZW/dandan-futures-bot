"""裁判自我警告語計數 — 純觀察指標，刻意不接進任何執行邏輯。

發現緣由（2026-08-06，58 筆已結算前瞻樣本）：把裁判段落裡的自我警告語數量分組，
績效呈單調遞增——
    0 個   n=15  勝率 13.3%  平均 -19.91   ← 語氣最篤定的一組，表現最差
    1 個   n=16  勝率 18.8%  平均 -13.41
    2 個   n=21  勝率 28.6%  平均  -6.39
    3+ 個  n= 6  勝率 33.3%  平均  +9.73
與進場區位（Fib）的相關係數僅 -0.187，是**獨立**因子而非同一件事的替身。控制住區位
後，在有利區疊加效果很強（2+ 警告語 62.5%/+22.87 vs 0-1 個 16.7%/-8.44），在不利區
則幾乎無效（11.8% vs 11.1%）——也就是「站對位置」是必要條件，警告語是加分項。

這與「信心分數反著跑」（輸的平均信心 0.506 > 贏的 0.469）是同一現象的兩種表現：
**這個模型的自信程度與正確率呈負相關。**

⚠️ 為什麼只做觀察、不做閘門（三個都必須成立才會考慮改成閘門）：
  1. 樣本太薄——最關鍵那格只有 n=8。
  2. 正則抽取脆弱——模型換個說法（「不建議在此追價」而非「不宜追空」）就抓不到，
     計數會系統性低估，穩健性未經檢驗。
  3. 自我實現風險——一旦「警告語多」變成放行條件，模型（或改動後的提示詞）可能
     開始多寫警告語來換取放行，訊號當場失效。觀察指標沒有這個問題。
"""
from __future__ import annotations

import re

JUDGE_SECTION = "【judge】"

HEDGE_MARKERS: dict[str, tuple[str, ...]] = {
    "遲到": (r"已遲到", r"遲到"),
    "不宜追": (r"不宜追", r"不追空", r"不追多", r"不宜在此追", r"不建議追"),
    "接刀": (r"接刀",),
    "賠率差": (r"R/R\s*(?:極)?(?:差|不佳)", r"賠率(?:極)?(?:差|不佳|不划算)",
              r"風報比(?:極)?(?:差|不佳)"),
    "無edge": (r"無\s*edge", r"沒有\s*edge", r"缺乏\s*edge", r"毫無\s*edge"),
    "低信心": (r"信心(?:壓低|不足|偏低|給得?低)", r"不宜過度自信", r"不過度自信"),
    "區間中段": (r"區間(?:正)?中(?:段|央)", r"中段(?:進場|位置)"),
}
"""警告語類別 → 該類別的多個同義寫法。

按「類別」而非「出現次數」計數：衡量的是裁判提了幾種**不同**的顧慮，
不是它話多話少——同一句顧慮講三次不代表更謹慎。
"""


def _judge_section(debate_full_text: str | None) -> str:
    """只取【judge】之後的文字。

    多空研究員被提示詞要求「提出最強的看多/看空論點」，他們的措辭是被指派的立場，
    不代表最終決策者的真實顧慮——把他們算進來會把訊號稀釋成雜訊。
    找不到裁判段落（舊樣本或格式異常）回空字串，計數自然為 0，絕不拋錯。
    """
    if not debate_full_text:
        return ""
    idx = debate_full_text.find(JUDGE_SECTION)
    return debate_full_text[idx:] if idx >= 0 else ""


def judge_hedge_labels(debate_full_text: str | None) -> list[str]:
    """裁判段落中出現的警告語類別名稱（去重、依 HEDGE_MARKERS 定義順序）。"""
    section = _judge_section(debate_full_text)
    if not section:
        return []
    return [name for name, patterns in HEDGE_MARKERS.items()
            if any(re.search(p, section) for p in patterns)]


def count_judge_hedges(debate_full_text: str | None) -> int:
    """裁判提了幾種不同的顧慮。純函式，不碰網路、不讀環境變數。"""
    return len(judge_hedge_labels(debate_full_text))
