"""
Tests for Binance copy trading leaderboard service and endpoints.
TDD: tests written to verify existing implementation is correct and covers edge cases.
"""
import json
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ─── helpers ─────────────────────────────────────────────────────────────────

def _mock_urlopen(response_data: dict):
    """Return a context-manager mock that yields a fake HTTP response."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# ─── binance_copytrading() ────────────────────────────────────────────────────

class TestBinanceCopytrading(unittest.TestCase):

    def setUp(self):
        # clear cache so each test starts fresh
        import webapp.backend.service as svc
        svc._bn_ct_cache["ts"] = 0
        svc._bn_ct_cache["data"] = None

    def _rank_response(self, n=3):
        return {
            "data": {
                "rankList": [
                    {
                        "encryptedUid": f"uid{i}",
                        "nickName": f"Trader{i}",
                        "followerCount": 100 * i,
                        "positionShared": True,
                        "value": 0.5 + i * 0.1,   # rank ROI as fraction
                    }
                    for i in range(1, n + 1)
                ]
            }
        }

    def _base_info_response(self, roi=0.42, pnl=1234.5, win=0.68):
        return {
            "data": {
                "roi": roi,
                "pnlValue": pnl,
                "winRate": win,
            }
        }

    def test_returns_trader_list(self):
        from webapp.backend.service import binance_copytrading
        rank_resp = self._rank_response(3)
        base_resp = self._base_info_response()

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                _mock_urlopen(rank_resp),
                _mock_urlopen(base_resp),
                _mock_urlopen(base_resp),
                _mock_urlopen(base_resp),
            ]
            result = binance_copytrading(limit=3)

        self.assertIn("traders", result)
        self.assertIn("source", result)
        self.assertEqual(len(result["traders"]), 3)

    def test_trader_has_required_fields(self):
        from webapp.backend.service import binance_copytrading
        rank_resp = self._rank_response(1)
        base_resp = self._base_info_response(roi=0.42, pnl=999.0, win=0.72)

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                _mock_urlopen(rank_resp),
                _mock_urlopen(base_resp),
            ]
            result = binance_copytrading(limit=1)

        trader = result["traders"][0]
        for field in ("uid", "nickname", "followers", "position_shared",
                      "rank_roi", "roi_7d", "pnl_7d", "win_rate"):
            self.assertIn(field, trader, f"Missing field: {field}")

    def test_roi_7d_is_percentage(self):
        """roi in API response is a fraction (0.42) → should be stored as 42.0."""
        from webapp.backend.service import binance_copytrading
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                _mock_urlopen(self._rank_response(1)),
                _mock_urlopen(self._base_info_response(roi=0.42)),
            ]
            result = binance_copytrading(limit=1)

        self.assertAlmostEqual(result["traders"][0]["roi_7d"], 42.0, places=1)

    def test_win_rate_is_percentage(self):
        """winRate in API response is a fraction (0.68) → should be 68.0."""
        from webapp.backend.service import binance_copytrading
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                _mock_urlopen(self._rank_response(1)),
                _mock_urlopen(self._base_info_response(win=0.68)),
            ]
            result = binance_copytrading(limit=1)

        self.assertAlmostEqual(result["traders"][0]["win_rate"], 68.0, places=1)

    def test_sorted_by_roi_7d_descending(self):
        """Traders must be sorted by 7-day ROI, highest first."""
        from webapp.backend.service import binance_copytrading
        rank_resp = self._rank_response(3)
        base_resps = [
            _mock_urlopen(self._base_info_response(roi=0.10)),
            _mock_urlopen(self._base_info_response(roi=0.50)),
            _mock_urlopen(self._base_info_response(roi=0.30)),
        ]

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [_mock_urlopen(rank_resp)] + base_resps
            result = binance_copytrading(limit=3)

        rois = [t["roi_7d"] for t in result["traders"]]
        self.assertEqual(rois, sorted(rois, reverse=True))

    def test_network_error_returns_empty(self):
        """When Binance API is unreachable, return empty traders list gracefully."""
        import webapp.backend.service as svc
        svc._bn_ct_cache["ts"] = 0
        svc._bn_ct_cache["data"] = None

        from webapp.backend.service import binance_copytrading
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = binance_copytrading(limit=20)

        self.assertIn("traders", result)
        self.assertEqual(result["traders"], [])

    def test_caching(self):
        """Second call within TTL should not make new network requests."""
        import webapp.backend.service as svc
        svc._bn_ct_cache["ts"] = 0
        svc._bn_ct_cache["data"] = None

        from webapp.backend.service import binance_copytrading
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                _mock_urlopen(self._rank_response(2)),
                _mock_urlopen(self._base_info_response()),
                _mock_urlopen(self._base_info_response()),
            ]
            result1 = binance_copytrading(limit=2)

        # second call — cache should be warm, no new urlopen calls
        with patch("urllib.request.urlopen") as mock_open2:
            result2 = binance_copytrading(limit=2)
            mock_open2.assert_not_called()

        self.assertEqual(len(result1["traders"]), len(result2["traders"]))


# ─── binance_copytrader_positions() ──────────────────────────────────────────

class TestBinanceCopytraderPositions(unittest.TestCase):

    def _pos_response(self, positions):
        return {"data": {"otherPositionRetList": positions}}

    def test_empty_uid_returns_empty(self):
        from webapp.backend.service import binance_copytrader_positions
        result = binance_copytrader_positions("")
        self.assertEqual(result, {"positions": []})

    def test_long_position_parsed(self):
        from webapp.backend.service import binance_copytrader_positions
        raw = self._pos_response([{
            "symbol": "BTCUSDT",
            "amount": "0.5",
            "entryPrice": "65000.0",
            "markPrice": "66000.0",
            "pnl": "500.0",
            "roe": "0.15",
            "leverage": 10,
        }])
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(raw)):
            result = binance_copytrader_positions("someuid")

        self.assertEqual(len(result["positions"]), 1)
        pos = result["positions"][0]
        self.assertEqual(pos["symbol"], "BTCUSDT")
        self.assertEqual(pos["direction"], "long")
        self.assertAlmostEqual(pos["size"], 0.5)
        self.assertAlmostEqual(pos["roe"], 15.0, places=1)

    def test_short_position_direction(self):
        from webapp.backend.service import binance_copytrader_positions
        raw = self._pos_response([{
            "symbol": "ETHUSDT",
            "amount": "-2.0",
            "entryPrice": "3000.0",
            "markPrice": "2900.0",
            "pnl": "200.0",
            "roe": "0.10",
            "leverage": 5,
        }])
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(raw)):
            result = binance_copytrader_positions("uid_x")

        pos = result["positions"][0]
        self.assertEqual(pos["direction"], "short")
        self.assertAlmostEqual(pos["size"], 2.0)

    def test_network_error_returns_empty(self):
        from webapp.backend.service import binance_copytrader_positions
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = binance_copytrader_positions("uid_y")
        self.assertEqual(result["positions"], [])


# ─── FastAPI endpoint smoke tests ─────────────────────────────────────────────

class TestCopytradingEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from webapp.backend.main import app
        cls.client = TestClient(app)

    def test_copytraders_endpoint_returns_200(self):
        with patch("webapp.backend.service.binance_copytrading",
                   return_value={"traders": [], "source": "Binance Copy Trading"}):
            resp = self.client.get("/api/copytraders?limit=5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("traders", resp.json())

    def test_copytrader_positions_endpoint_empty_uid(self):
        resp = self.client.get("/api/copytrader-positions?uid=")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["positions"], [])

    def test_copytraders_limit_respected(self):
        """Endpoint passes limit param to service."""
        captured = {}

        def fake_copytrading(limit=20):
            captured["limit"] = limit
            return {"traders": [], "source": "Binance Copy Trading"}

        with patch("webapp.backend.service.binance_copytrading", side_effect=fake_copytrading):
            self.client.get("/api/copytraders?limit=7")

        self.assertEqual(captured.get("limit"), 7)


if __name__ == "__main__":
    unittest.main()
