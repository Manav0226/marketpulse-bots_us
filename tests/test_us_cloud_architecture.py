import datetime as dt
import sys
import types
import unittest
from pathlib import Path

from marketpulse_state import read_bot_state, update_bot_state


class USMarketScheduleTests(unittest.TestCase):
    def test_us_market_open_respects_dst_summer_session(self):
        from core.us_market_scheduler import is_us_market_open

        now = dt.datetime(2026, 7, 6, 14, 0, tzinfo=dt.timezone.utc)

        self.assertTrue(is_us_market_open(now))

    def test_us_market_open_respects_dst_winter_session(self):
        from core.us_market_scheduler import is_us_market_open

        now = dt.datetime(2026, 12, 7, 15, 0, tzinfo=dt.timezone.utc)

        self.assertTrue(is_us_market_open(now))

    def test_us_market_closed_on_observed_holiday(self):
        from core.us_market_scheduler import is_us_market_open

        now = dt.datetime(2026, 7, 3, 15, 0, tzinfo=dt.timezone.utc)

        self.assertFalse(is_us_market_open(now))


class USCloudStateTests(unittest.TestCase):
    def _tmp_dir(self, name: str) -> Path:
        path = Path.cwd() / ".test_tmp" / name
        path.mkdir(parents=True, exist_ok=True)
        for child in path.glob("*"):
            child.unlink()
        return path

    def test_update_bot_state_persists_safe_mode_scheduler_and_bets(self):
        state_dir = self._tmp_dir("us_cloud_state")
        state_path = state_dir / "bot_state.json"
        now = dt.datetime(2026, 5, 5, 12, 0, tzinfo=dt.timezone.utc)

        update_bot_state(
            "us_v4",
            {
                "positions": {
                    "AAPL": {
                        "symbol_or_market": "AAPL",
                        "venue": "alpaca",
                        "strategy_mode": "swing",
                    }
                },
                "bets": {
                    "fed-cut-sep": {
                        "symbol_or_market": "fed-cut-sep",
                        "venue": "polymarket",
                        "strategy_mode": "event",
                    }
                },
                "safe_mode": {"global_pause_new_entries": True, "reason": "news_shock"},
                "scheduler_status": {"last_us_premarket": "2026-05-05T11:00:00+00:00"},
                "performance": {"us_equities": {"win_rate": 0.55}},
                "promotion_status": {"us_equities": {"eligible_for_live": False}},
            },
            path=state_path,
            now=lambda: now,
        )

        state = read_bot_state(path=state_path, now=lambda: now)
        bot = state["bots"]["us_v4"]

        self.assertIn("bets", bot)
        self.assertIn("safe_mode", bot)
        self.assertIn("scheduler_status", bot)
        self.assertIn("performance", bot)
        self.assertIn("promotion_status", bot)
        self.assertTrue(bot["safe_mode"]["global_pause_new_entries"])
        self.assertIn("fed-cut-sep", bot["bets"])


class FatherBotUSSupervisionTests(unittest.TestCase):
    def test_build_father_opinion_includes_us_supervision_and_auto_pause(self):
        from bot_father import build_father_opinion

        opinion = build_father_opinion(
            brain_brief={
                "date": "2026-05-05",
                "market_regime": "RISK_OFF",
                "equity_focus": [{"symbol": "AAPL"}],
                "avoid_symbols": ["TSLA"],
            },
            brain_state={"bot_modes": {"us": "active_supervised"}},
            bot_state={
                "bots": {
                    "india": {"signals": []},
                    "fno": {"rejections": []},
                    "us_v4": {
                        "positions": {"AAPL": {"venue": "alpaca"}},
                        "bets": {"fed-cut-sep": {"venue": "polymarket"}},
                        "health": {"llm_supervisor": "degraded"},
                        "safe_mode": {"global_pause_new_entries": True, "reason": "news_shock"},
                    },
                }
            },
            council_state={"cross_market_bias": {"verdict": "BEARISH"}},
            risk_status={"hold": False},
        )

        self.assertEqual(opinion["us"]["mode"], "paused")
        self.assertTrue(opinion["us"]["safe_mode"]["global_pause_new_entries"])
        self.assertEqual(opinion["us"]["open_positions"], 1)
        self.assertEqual(opinion["polymarket"]["open_bets"], 1)


class USExecutionResearchIntegrationTests(unittest.TestCase):
    def test_prioritize_signals_prefers_us_research_focus(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        class Signal:
            def __init__(self, symbol, score):
                self.symbol = symbol
                self.total_score = score

        bot = USCryptoBot4.__new__(USCryptoBot4)
        bot.us_weekly_brief = {
            "weekly_candidates": [
                {"symbol": "AAPL", "score": 14},
                {"symbol": "TSLA", "score": -11},
            ]
        }
        signals = [Signal("MSFT", 15), Signal("TSLA", -10), Signal("AAPL", 12)]

        ordered = bot._prioritize_us_signals(signals)

        self.assertEqual([item.symbol for item in ordered], ["AAPL", "TSLA", "MSFT"])

    def test_research_adjustment_blocks_wrong_side_pre_earnings_trade(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        class Signal:
            symbol = "TSLA"
            signal = "BUY"
            quantity = 10

        bot = USCryptoBot4.__new__(USCryptoBot4)
        bot.us_weekly_brief = {
            "earnings_setups": [
                {"symbol": "TSLA", "earnings_date": "2026-05-08", "pre_result_bias": "BEARISH", "result_day_bias": "VOLATILE_BEARISH"}
            ]
        }

        decision = bot._research_trade_adjustment(Signal(), now=dt.datetime(2026, 5, 6, 12, 0, tzinfo=dt.timezone.utc))

        self.assertFalse(decision["allow"])
        self.assertEqual(decision["qty_multiplier"], 0.0)

    def test_research_adjustment_reduces_size_on_result_day(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        class Signal:
            symbol = "AAPL"
            signal = "BUY"
            quantity = 10

        bot = USCryptoBot4.__new__(USCryptoBot4)
        bot.us_weekly_brief = {
            "earnings_setups": [
                {"symbol": "AAPL", "earnings_date": "2026-05-08", "pre_result_bias": "BULLISH", "result_day_bias": "VOLATILE_BULLISH"}
            ]
        }

        decision = bot._research_trade_adjustment(Signal(), now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc))

        self.assertTrue(decision["allow"])
        self.assertEqual(decision["qty_multiplier"], 0.5)


class USSupervisorTests(unittest.TestCase):
    def test_build_us_supervision_blocks_symbols_and_sets_size_multipliers(self):
        from us_supervisor import build_us_supervision

        supervision = build_us_supervision(
            father_opinion={"us": {"mode": "active_supervised", "safe_mode": {"global_pause_new_entries": False}}},
            weekly_brief={
                "weekly_candidates": [{"symbol": "AAPL", "score": 14}, {"symbol": "TSLA", "score": -10}],
                "earnings_setups": [
                    {"symbol": "TSLA", "pre_result_bias": "BEARISH", "result_day_bias": "VOLATILE_BEARISH", "earnings_date": "2026-05-08"},
                    {"symbol": "AAPL", "pre_result_bias": "BULLISH", "result_day_bias": "VOLATILE_BULLISH", "earnings_date": "2026-05-08"},
                ],
                "source_health": {"degraded": True, "warnings": ["news unavailable"]},
            },
            bot_state={"health": {"llm_supervisor": "available"}},
            now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc),
        )

        self.assertFalse(supervision["allow_new_entries"])
        self.assertIn("TSLA", supervision["blocked_symbols"])
        self.assertEqual(supervision["size_multipliers"]["AAPL"], 0.5)
        self.assertTrue(supervision["forced_safe_mode"])


if __name__ == "__main__":
    unittest.main()
