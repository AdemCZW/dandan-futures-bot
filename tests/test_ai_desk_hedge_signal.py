"""ai_desk.hedge_signal 測試 — 裁判自我警告語計數（純觀察指標，不接執行邏輯）。

發現緣由（2026-08-06，58 筆已結算前瞻樣本）：把裁判段落裡的自我警告語數量分組，
績效呈單調遞增——0 個 13.3%/-19.91、1 個 18.8%/-13.41、2 個 28.6%/-6.39、
3+ 個 33.3%/+9.73。與進場區位（Fib）相關係數僅 -0.187，是獨立因子；控制住區位後，
在有利區疊加效果很強（2+ 警告語 62.5%/+22.87 vs 0-1 個 16.7%/-8.44）。

刻意只做觀察欄位、不做閘門：關鍵分組僅 n=8、正則抽取脆弱、且一旦變成放行條件
就有自我實現風險（模型可能多寫警告語來換放行）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.hedge_signal import HEDGE_MARKERS, count_judge_hedges, judge_hedge_labels


def wrap(judge_body: str, other: str = "") -> str:
    """組出 desk 存進資料庫的那種完整辯論文字。"""
    return (f"【analyst】\n技術結構描述。{other}\n\n"
            f"【bull】\n多方論述。{other}\n\n"
            f"【bear】\n空方論述。{other}\n\n"
            f"【judge】\n{judge_body}")


def test_no_hedge_language_counts_zero():
    assert count_judge_hedges(wrap("方向明確看空，直接進場。")) == 0


def test_counts_distinct_marker_categories_not_repetitions():
    """同一類警告講三次只算一次——衡量的是「提了幾種顧慮」，不是話多話少。"""
    body = "不宜追空。這裡不宜追空。總之不宜追空。"
    assert count_judge_hedges(wrap(body)) == 1


def test_counts_multiple_distinct_categories():
    body = "此處做空已遲到、不宜追空，現價進場等於接刀，R/R 極差。"
    n = count_judge_hedges(wrap(body))
    assert n >= 3
    labels = judge_hedge_labels(wrap(body))
    assert len(labels) == n
    assert len(set(labels)) == n          # 不重複計數


def test_only_scans_judge_section():
    """多空研究員被要求硬講立場，他們的措辭不代表最終決策者的顧慮。"""
    debate = ("【analyst】\n無。\n\n"
              "【bull】\n空方那套是接刀、賠率極差、不宜追空。\n\n"
              "【bear】\n多方在區間中段進場毫無 edge。\n\n"
              "【judge】\n方向明確，直接進場。")
    assert count_judge_hedges(debate) == 0


def test_missing_judge_section_counts_zero_not_crash():
    """舊樣本或格式異常時回 0，不可讓整輪分析爆掉。"""
    assert count_judge_hedges("完全沒有角色標記的一段文字") == 0
    assert count_judge_hedges("") == 0
    assert count_judge_hedges(None) == 0


def test_labels_are_stable_marker_names():
    labels = judge_hedge_labels(wrap("不宜追空，且賠率不佳。"))
    assert set(labels) <= set(HEDGE_MARKERS)


def test_real_world_judge_excerpt():
    """取自實際樣本 #51 的裁判段落措辭。"""
    body = ("空方唯一站不住的地方，正是多方唯一對的地方：現價已緊貼 MA20。"
            "此刻市價追空 = 在近端支撐正上方接刀，R/R 極差。"
            "結論：方向看空，但不在此刻追空。confidence 給 0.58，不宜過度自信。")
    assert count_judge_hedges(wrap(body)) >= 3
