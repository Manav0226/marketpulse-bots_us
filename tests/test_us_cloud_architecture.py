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

    def test_india_market_open_during_regular_session(self):
        from core.india_market_scheduler import is_india_market_open

        now = dt.datetime(2026, 5, 6, 5, 0, tzinfo=dt.timezone.utc)

        self.assertTrue(is_india_market_open(now))


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

    def test_refresh_supervision_ignores_stale_father_pause_when_supervision_allows_entries(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        import json
        import bot_us_crypto_v4 as usbot

        state_dir = Path.cwd() / ".test_tmp" / "us_supervision_refresh"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "us_supervision.json").write_text(
            json.dumps(
                {
                    "allow_new_entries": True,
                    "forced_safe_mode": False,
                    "source_warnings": ["earnings calendar unavailable"],
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "father_opinion.json").write_text(
            json.dumps(
                {
                    "us": {
                        "safe_mode": {
                            "global_pause_new_entries": True,
                            "reason": "earnings calendar unavailable, price data stale",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        original_state_dir = usbot.STATE_DIR
        try:
            usbot.STATE_DIR = state_dir
            bot = usbot.USCryptoBot4.__new__(usbot.USCryptoBot4)
            bot.safe_mode = {"global_pause_new_entries": True, "reason": "old_pause"}
            bot.scheduler_status = {}

            bot._maybe_refresh_supervision()

            self.assertFalse(bot.safe_mode["global_pause_new_entries"])
        finally:
            usbot.STATE_DIR = original_state_dir

    def test_build_us_holding_profile_prefers_swing_for_top_ranked_focus_name(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        class Signal:
            symbol = "CRWD"
            signal = "STRONG BUY"
            confidence = 69.4

        bot = USCryptoBot4.__new__(USCryptoBot4)
        bot.us_weekly_brief = {
            "weekly_candidates": [
                {"symbol": "CRWD", "score": 17},
                {"symbol": "GOOGL", "score": 17},
            ]
        }

        profile = bot._build_us_holding_profile(Signal(), {"qty_multiplier": 1.0}, now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc))

        self.assertEqual(profile["holding_style"], "swing")
        self.assertTrue(profile["overnight_allowed"])
        self.assertEqual(profile["planned_hold_days"], 3)

    def test_build_us_holding_profile_keeps_weaker_name_intraday(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        class Signal:
            symbol = "XYZ"
            signal = "BUY"
            confidence = 54.0

        bot = USCryptoBot4.__new__(USCryptoBot4)
        bot.us_weekly_brief = {"weekly_candidates": [{"symbol": "CRWD", "score": 17}]}

        profile = bot._build_us_holding_profile(Signal(), {"qty_multiplier": 0.5}, now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc))

        self.assertEqual(profile["holding_style"], "intraday")
        self.assertFalse(profile["overnight_allowed"])
        self.assertEqual(profile["planned_hold_days"], 0)

    def test_should_run_us_close_cycle_after_market_close_once_per_day(self):
        sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
        from bot_us_crypto_v4 import USCryptoBot4

        bot = USCryptoBot4.__new__(USCryptoBot4)

        now = dt.datetime(2026, 5, 5, 20, 15, tzinfo=dt.timezone.utc)

        self.assertTrue(bot._should_run_us_close_cycle(now, None))
        self.assertFalse(bot._should_run_us_close_cycle(now, dt.date(2026, 5, 5)))

    def test_sync_polymarket_snapshot_tracks_watchlist_items(self):
        import json
        import bot_us_crypto_v4 as usbot

        state_dir = Path.cwd() / ".test_tmp" / "polymarket_watch"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "polymarket_watchlist.json").write_text(
            json.dumps(
                {
                    "watchlist": [
                        {
                            "market_id": "election-2028",
                            "strategy_mode": "event",
                            "entry_reason": "copy_tracked_watch",
                            "confidence": 71,
                            "risk_budget": 25,
                            "side": "yes",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        original_state_dir = usbot.STATE_DIR
        original_watch_path = usbot.POLYMARKET_WATCHLIST_PATH
        try:
            usbot.STATE_DIR = state_dir
            usbot.POLYMARKET_WATCHLIST_PATH = state_dir / "polymarket_watchlist.json"
            bot = usbot.USCryptoBot4.__new__(usbot.USCryptoBot4)
            bot.polymarket_bets = {}
            bot.scheduler_status = {}
            bot.performance = {"polymarket": {"bets": 0}}
            bot._sync_state = lambda: None

            bot.sync_polymarket_snapshot()

            self.assertIn("election-2028", bot.polymarket_bets)
            self.assertEqual(bot.polymarket_bets["election-2028"]["side"], "yes")
            self.assertEqual(bot.scheduler_status["last_polymarket_watch_items"], 1)
        finally:
            usbot.STATE_DIR = original_state_dir
            usbot.POLYMARKET_WATCHLIST_PATH = original_watch_path


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

    def test_build_us_supervision_allows_entries_when_only_earnings_calendar_missing(self):
        from us_supervisor import build_us_supervision

        supervision = build_us_supervision(
            father_opinion={"us": {"mode": "active_supervised", "safe_mode": {"global_pause_new_entries": False}}},
            weekly_brief={
                "weekly_candidates": [{"symbol": "CRWD", "score": 22}],
                "earnings_setups": [],
                "source_health": {"degraded": True, "warnings": ["earnings calendar unavailable"]},
            },
            bot_state={"health": {"llm_supervisor": "available"}},
            now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc),
        )

        self.assertTrue(supervision["allow_new_entries"])
        self.assertFalse(supervision["forced_safe_mode"])

    def test_build_us_supervision_ignores_stale_father_pause_from_noncritical_warning(self):
        from us_supervisor import build_us_supervision

        supervision = build_us_supervision(
            father_opinion={
                "us": {
                    "mode": "paused",
                    "safe_mode": {
                        "global_pause_new_entries": True,
                        "reason": "earnings calendar unavailable",
                    },
                }
            },
            weekly_brief={
                "weekly_candidates": [{"symbol": "CRWD", "score": 22}],
                "earnings_setups": [],
                "source_health": {"degraded": True, "warnings": ["earnings calendar unavailable"]},
            },
            bot_state={"health": {"llm_supervisor": "available"}},
            now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc),
        )

        self.assertTrue(supervision["allow_new_entries"])
        self.assertFalse(supervision["forced_safe_mode"])

    def test_build_us_supervision_ignores_stale_father_pause_with_old_critical_phrase_when_current_brief_is_healthy(self):
        from us_supervisor import build_us_supervision

        supervision = build_us_supervision(
            father_opinion={
                "us": {
                    "mode": "paused",
                    "safe_mode": {
                        "global_pause_new_entries": True,
                        "reason": "earnings calendar unavailable, price data stale",
                    },
                }
            },
            weekly_brief={
                "weekly_candidates": [{"symbol": "CRWD", "score": 22}],
                "earnings_setups": [],
                "source_health": {"degraded": True, "warnings": ["earnings calendar unavailable"]},
            },
            bot_state={"health": {"llm_supervisor": "available"}},
            now=dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.timezone.utc),
        )

        self.assertTrue(supervision["allow_new_entries"])
        self.assertFalse(supervision["forced_safe_mode"])


if __name__ == "__main__":
    unittest.main()
