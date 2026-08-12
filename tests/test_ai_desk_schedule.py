"""排程設定測試 — 驗證 launchd plist 與包裝腳本的關鍵設定，避免靜默設錯。

排程是「設定完就沒人再看」的東西：時間算錯、環境變數漏掉、路徑寫錯，
都只會表現成「半夜靜靜地什麼都沒發生」，不會有人通知你。所以這裡把
可驗證的部分鎖住。
"""
import os
import plistlib
import re
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PLIST = os.path.join(ROOT, "scheduling", "com.dandan.aidesk.plist")
SCRIPT = os.path.join(ROOT, "scheduling", "run_ai_desk_scheduled.sh")


@pytest.fixture(scope="module")
def plist():
    with open(PLIST, "rb") as f:
        return plistlib.load(f)


# ── launchd 觸發時間 ────────────────────────────────────────
def test_fires_at_all_six_4h_closes_in_local_time(plist):
    """4h K 棒在 UTC 0/4/8/12/16/20 收盤；本地(Asia/Taipei, UTC+8)= 8/12/16/20/0/4。"""
    hours = sorted(e["Hour"] for e in plist["StartCalendarInterval"])
    assert hours == [0, 4, 8, 12, 16, 20]


def test_fires_a_few_minutes_after_close_not_exactly_on_it(plist):
    """收盤當下 K 棒剛封閉，抓資料要留緩衝；全部設在整點後幾分鐘。"""
    minutes = {e["Minute"] for e in plist["StartCalendarInterval"]}
    assert minutes == {5}


# ── launchd 行為設定 ────────────────────────────────────────
def test_does_not_run_at_load_time(plist):
    """載入排程時不該立刻跑一輪——那會在你設定的當下無預警下單。"""
    assert plist.get("RunAtLoad", False) is False


def test_does_not_auto_restart_on_exit(plist):
    """單輪跑完就該結束；KeepAlive 會讓它一結束就重啟，變成無限迴圈狂打 API。"""
    assert plist.get("KeepAlive", False) is False


def test_points_at_the_wrapper_script(plist):
    assert plist["ProgramArguments"][-1] == SCRIPT


def test_logs_stdout_and_stderr_to_files(plist):
    """無人值守時 log 是唯一的除錯線索，兩邊都要留。"""
    assert plist["StandardOutPath"].endswith(".log")
    assert plist["StandardErrorPath"].endswith(".log")


# ── 包裝腳本 ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def script_text():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def test_script_is_executable():
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_script_enables_auto_approve(script_text):
    """使用者明確要求排程要自動下單。"""
    assert "AI_DESK_AUTO_APPROVE=true" in script_text


def test_script_runs_both_symbols(script_text):
    assert "BTCUSDT" in script_text and "ETHUSDT" in script_text


def test_script_uses_venv_python_not_bare_python(script_text):
    """launchd 的 PATH 極簡，bare `python` 不存在；必須用 venv 絕對路徑。"""
    assert ".venv/bin/python" in script_text
    assert not re.search(r"^\s*python\s+run_ai_desk", script_text, re.M)


def test_script_cds_into_project_root(script_text):
    """相對路徑（.env、ai_desk/memory、SQLite）都依賴工作目錄正確。"""
    assert "cd " in script_text


def test_script_has_no_syntax_errors():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_script_continues_to_second_symbol_even_if_first_fails(script_text):
    """BTC 那輪失敗（例如 CLI 逾時）不該讓 ETH 整個被跳過。

    只看實際程式碼行，不看註解——註解裡說明「為何不用 set -e」是合理的。
    """
    code_lines = [ln.split("#", 1)[0] for ln in script_text.splitlines()]
    assert not any(re.match(r"\s*set\s+-\w*e", ln) for ln in code_lines)


def test_script_makes_claude_findable_in_minimal_launchd_path(script_text):
    """launchd 的 PATH 不含 /opt/homebrew/bin。實際故障：2026-07-24~27 排程連續
    多輪 exit 1，錯誤 No such file or directory: 'claude'。腳本必須自己解決。"""
    assert "/opt/homebrew/bin" in script_text


def test_script_passes_absolute_claude_path_to_client(script_text):
    """除了補 PATH，也直接指定絕對路徑（雙層保險）。"""
    assert "AI_DESK_CLAUDE_BIN" in script_text


def test_claude_binary_actually_exists_at_configured_path(script_text):
    """設定的絕對路徑要真的存在，否則等於沒修。"""
    m = re.search(r'AI_DESK_CLAUDE_BIN="([^"]+)"', script_text)
    assert m, "腳本未設定 AI_DESK_CLAUDE_BIN"
    assert os.path.exists(m.group(1)), f"claude 不在 {m.group(1)}"


# ── 帳號層級自動記憶隔離（2026-08-12）───────────────────────
#
# claude CLI 每次啟動都會自動載入帳號層級的持久記憶（~/.claude/projects/.../
# memory/），不是 ai_desk 專屬、也關不掉（實測 --tools none / --system-prompt /
# --setting-sources / 換模型全部無效）——導致四角色辯論讀到使用者對這套系統
# 本身下的評語，判斷不再是純粹從 briefing 推論。唯一有效的隔離是讓記憶資料夾
# 在呼叫期間物理上不存在。這裡鎖住修復的關鍵行為，避免以後被誤刪或改壞。

def test_script_hides_memory_before_and_restores_after_llm_calls(script_text):
    hide_at = script_text.index("hide_claude_memory\n")
    restore_at = script_text.index("restore_claude_memory\n", hide_at)
    loop_at = script_text.index("for SYMBOL in")
    assert hide_at < loop_at < restore_at, "隱藏/還原沒有正確包住 LLM 呼叫的迴圈"


def test_script_traps_exit_signals_to_guarantee_restore(script_text):
    """腳本中途被中斷或崩潰時，記憶也一定要還原——不能只靠正常結束那條路徑。"""
    assert re.search(r"trap\s+restore_claude_memory\s+EXIT\s+INT\s+TERM", script_text)


def test_restore_handles_leftover_backup_from_previous_crash(script_text):
    """上次排程若中途當掉、記憶還卡在隱藏狀態，這次啟動前必須先自我修復，
    不可以在已經隱藏的狀態上再隱藏一次（會把備份蓋掉）。"""
    hide_fn = re.search(r"hide_claude_memory\(\)\s*\{(.*?)\n\}", script_text, re.S)
    assert hide_fn, "找不到 hide_claude_memory 函式定義"
    assert "restore_claude_memory" in hide_fn.group(1), \
        "hide_claude_memory 沒有在隱藏前先處理殘留的舊備份"


def test_restore_does_not_nest_into_cli_recreated_directory(script_text):
    """2026-08-12 實測踩到：記憶被隱藏後，claude CLI 找不到資料夾會自己重建一個
    新的；若還原時目標路徑已存在，mv 備份回去會變成巢狀一層而非取代，
    當場弄丟一層真正的記憶內容。還原邏輯必須先處理這個已存在的情況。"""
    restore_fn = re.search(r"restore_claude_memory\(\)\s*\{(.*?)\n\}", script_text, re.S)
    assert restore_fn, "找不到 restore_claude_memory 函式定義"
    body = restore_fn.group(1)
    assert "CLAUDE_MEMORY_DIR" in body and re.search(r'-d\s+"\$CLAUDE_MEMORY_DIR"', body), \
        "還原前沒有檢查目標路徑是否已被 CLI 重新建立"
