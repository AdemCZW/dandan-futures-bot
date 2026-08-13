"""交易提案 — AI 裁判輸出的結構化提案 + 既有風控官夾限。

不重新實作任何風控規則：熔斷/清算守衛/倉位上限全部走既有
core.risk_officer.RiskOfficer（與規則策略同一套、同一實例邏輯）。
AI 的信心分數對風控沒有任何影響力。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from core.risk_officer import RiskDecision, RiskOfficer

DEFAULT_SHORT_MIN_FIB = 0.45
DEFAULT_LONG_MAX_FIB = 0.55
"""進場區位閘門門檻：做空需 fib >= 0.45、做多需 fib <= 0.55（對稱於區間中點）。

為什麼有這道閘門（2026-08-06，56 筆已結算前瞻樣本的實測）：把做空依「進場當下的
Fib 區間位置」分組，勝率呈單調遞增——
    低位 fib<0.30      n=16  勝率 6.2%   平均 -21.18
    中位 0.30~0.45     n=17  勝率 17.6%  平均 -10.04
    高位 fib>=0.45     n=11  勝率 45.5%  平均 +13.22   ← 唯一為正的分組
相關係數 +0.347。反事實：只保留 fib>=0.45 的做空，被砍掉的 33 筆合計 -509.46，
保留的 11 筆合計 +145.45。做多側是同一個病的鏡像（實測 #4 fib=1.095、#8 fib=0.857
都是貼著區間頂做多後停損）。

關鍵在於：Fib 區間位置本來就印在簡報裡給 AI 看，它也常在辯論中自己寫「此處做空已
遲到、不宜追空」，但最終提案仍把進場價設在不利區（44 筆做空有 75% 落在 fib<0.45）。
所以這道閘門補的不是資訊，是紀律——把 AI 自己說過的話變成硬性約束。

⚠️ 誠實界定：這是「唯一一組方向與整體相反、值得優先測試的線索」，**不是已證明的
edge**。保留組的 bootstrap 信賴下界仍為 -1.24（n=11），未通過本專案的正式晉升閘門；
做多側證據更弱（僅 2~3 筆可量測），屬鏡像對稱推論。故預設關閉，需明確開啟。
"""


DEFAULT_MAX_TP_ATR = 3.0
"""停利距離上限（以進場當下的 ATR 為單位）。超過就視為「物理上到不了」。

為什麼有這道閘門（2026-08-13，68 筆已結算前瞻樣本的實測）：依停利距離分組，
勝率單調遞減——
    <2 ATR    n=48  勝率 27.1%  合計 -158.66
    2~3 ATR   n= 9  勝率 11.1%  合計 -222.95
    >=3 ATR   n=11  勝率  0.0%  合計 -278.89   ← 11 戰全敗

真正的診斷點不是這三格本身，而是這個對照：規劃 R/R 中位數 2.23，**純隨機進場
的理論勝率是 1/(1+2.23) = 31%，而實際勝率只有 20.6%**。「沒有 edge」會落在 31%；
掉到 20.6% 代表有東西在系統性地做錯，不只是猜不準方向。

機制：這段期間市場在震盪，裁判卻經常把停利設在 3 個 ATR 以外。在一個約 2 ATR
寬的箱體裡，3+ ATR 的目標在觸及 1.5 ATR 停損之前根本走不到——這不是判斷失誤，
是**規格上就不可能達成的單**。裁判的算術其實是準的（實測 23 筆自稱風報比與
實算對照，21 筆誤差在 3% 內），問題在於沒有任何東西檢查那個目標可不可達。

⚠️ 誠實界定：這是在同一批樣本上測的第 6 個假設（p-hacking 風險高），**尚未
經過任何樣本外驗證**。與區位閘門疊加的 in-sample 數字（n=21 / 33.3% / +100.55）
不能當成證據，只是「值得優先前瞻測試的線索」。故預設關閉。
"""


def zone_gate_enabled() -> bool:
    """進場區位閘門總開關。預設關閉——比照專案既有新過濾器慣例，不默默改變線上行為。"""
    return os.getenv("AI_DESK_ZONE_GATE", "false").lower() == "true"


def target_gate_enabled() -> bool:
    """停利可達性閘門總開關。預設關閉，理由同上。"""
    return os.getenv("AI_DESK_TARGET_GATE", "false").lower() == "true"


def target_is_reachable(entry: float, take_profit: float, atr: float | None, *,
                        max_tp_atr: float = DEFAULT_MAX_TP_ATR) -> bool:
    """停利距離是否落在當前波動下走得到的範圍。純函式，不讀環境變數。

    只看絕對距離，做多做空同一套標準。atr 缺值或非正數一律回 False——
    比照區位閘門與 smc_structure vol/corr 過濾器的既有慣例：寧可少做一筆，
    也不要在不知道當前波動有多大時，放行一個可能永遠走不到的目標。
    """
    if atr is None or atr <= 0:
        return False
    return abs(take_profit - entry) / atr < max_tp_atr


def entry_zone_allows(direction: int, fib_pos: float | None, *,
                      short_min_fib: float = DEFAULT_SHORT_MIN_FIB,
                      long_max_fib: float = DEFAULT_LONG_MAX_FIB) -> bool:
    """進場當下的區間位置是否落在該方向的有利區。純函式，不讀環境變數。

    fib_pos 為 None（算不出來）一律回 False——比照 smc_structure 的 vol/corr
    過濾器慣例：寧可少做一筆，也不要在不知道自己站在區間哪裡時進場。
    """
    if fib_pos is None:
        return False
    if direction == -1:
        return fib_pos >= short_min_fib
    if direction == 1:
        return fib_pos <= long_max_fib
    return False


@dataclass
class TradeProposal:
    symbol: str
    ts: str
    direction: int        # 1 多 / -1 空 / 0 觀望
    confidence: float     # 0~1（僅供人工核准參考，不影響風控）
    entry: float
    stop: float
    take_profit: float
    rationale: str


def proposal_from_judge(data: dict, symbol: str, ts: str) -> TradeProposal:
    """裁判 JSON → TradeProposal。驗證失敗拋 ValueError（該輪提案作廢）。"""
    direction = int(data["direction"])
    if direction not in (-1, 0, 1):
        raise ValueError(f"direction 必須是 -1/0/1，收到 {direction}")
    confidence = float(data["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence 必須在 0~1，收到 {confidence}")
    # 觀望（direction 0）時裁判常把價格給 null；價格無意義，一律歸零、不驗證。
    if direction == 0:
        entry = stop = take_profit = 0.0
    else:
        # 有方向卻缺任一價格 → 不可用的提案，給明確 ValueError（而非 float(None) 的 TypeError）。
        if data.get("entry") is None or data.get("stop") is None or data.get("take_profit") is None:
            raise ValueError("方向非觀望時，entry/stop/take_profit 不可為空")
        entry = float(data["entry"])
        stop = float(data["stop"])
        take_profit = float(data["take_profit"])
        if direction == 1 and not stop < entry:
            raise ValueError(f"多單停損({stop})必須低於進場價({entry})")
        if direction == -1 and not stop > entry:
            raise ValueError(f"空單停損({stop})必須高於進場價({entry})")
    return TradeProposal(symbol=symbol, ts=ts, direction=direction,
                         confidence=confidence, entry=entry, stop=stop,
                         take_profit=take_profit,
                         rationale=str(data["rationale"]))


def clamp_with_risk_officer(proposal: TradeProposal, officer: RiskOfficer,
                            equity: float, atr=None, fib_pos=None) -> RiskDecision:
    """既有風控官夾限：熔斷/清算守衛照走，倉位取「AI 停損」與「風控停損」
    兩種算法中較保守（較小）者。AI 信心分數不參與任何計算。

    fib_pos：進場當下的 Fib 區間位置，供進場區位閘門判斷（見 entry_zone_allows）。
    閘門只決定「進不進場」這個是非題，絕不參與倉位計算——與「信心不影響倉位」
    同一條硬規則的精神。
    """
    if proposal.direction == 0:
        return RiskDecision(False, 0.0, "AI 建議觀望，不進場")
    if zone_gate_enabled() and not entry_zone_allows(proposal.direction, fib_pos):
        where = "資料不足" if fib_pos is None else f"{fib_pos:.2f}"
        side = "做空" if proposal.direction == -1 else "做多"
        return RiskDecision(
            False, 0.0,
            f"進場區位閘門擋下：{side}時 Fib 區間位置 {where} 不在有利區"
            f"（做空需 ≥{DEFAULT_SHORT_MIN_FIB}、做多需 ≤{DEFAULT_LONG_MAX_FIB}）")
    if target_gate_enabled() and not target_is_reachable(
            proposal.entry, proposal.take_profit, atr):
        dist = ("ATR 資料不足" if not atr or atr <= 0
                else f"{abs(proposal.take_profit - proposal.entry) / atr:.2f} 個 ATR")
        return RiskDecision(
            False, 0.0,
            f"停利可達性閘門擋下：停利距離 {dist}，當前波動下走不到"
            f"（需 <{DEFAULT_MAX_TP_ATR} 個 ATR）")
    gate = officer.check_entry(equity, proposal.entry, proposal.ts,
                               direction=proposal.direction, atr=atr)
    if not gate.allow:
        return gate
    qty_ai_stop = officer.position_size(equity, proposal.entry, proposal.stop)
    qty = min(gate.quantity, qty_ai_stop)
    if qty <= 0:
        return RiskDecision(False, 0.0, "風控算出倉位為 0")
    return RiskDecision(True, qty, "ok（倉位取 AI 停損與風控停損較保守者）")
