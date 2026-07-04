import unittest

from openclaw_runtime.gateway_cron import schedule_to_gateway


class ScheduleToGatewayTimezoneTest(unittest.TestCase):
    def test_daily_uses_configured_timezone(self) -> None:
        result = schedule_to_gateway({"type": "daily", "time": "07:30"}, "Asia/Taipei")
        self.assertEqual(result["tz"], "Asia/Taipei")
        self.assertEqual(result["expr"], "30 7 * * *")

    def test_weekly_uses_configured_timezone(self) -> None:
        result = schedule_to_gateway({"type": "weekly", "weekday": 0, "time": "08:00"}, "Asia/Taipei")
        self.assertEqual(result["tz"], "Asia/Taipei")
        self.assertEqual(result["expr"], "0 8 * * 1")

    def test_monthly_uses_configured_timezone(self) -> None:
        result = schedule_to_gateway({"type": "monthly", "day": "last", "time": "18:00"}, "Europe/London")
        self.assertEqual(result["tz"], "Europe/London")
        self.assertEqual(result["expr"], "0 18 28-31 * *")

    def test_different_timezones_are_not_hardcoded(self) -> None:
        # Regression guard: schedule_to_gateway used to always emit
        # tz="Europe/London" regardless of the timezone argument.
        london = schedule_to_gateway({"type": "daily", "time": "07:30"}, "Europe/London")
        taipei = schedule_to_gateway({"type": "daily", "time": "07:30"}, "Asia/Taipei")
        self.assertNotEqual(london["tz"], taipei["tz"])

    def test_interval_schedule_does_not_require_timezone_field(self) -> None:
        result = schedule_to_gateway({"type": "interval", "seconds": 3600}, "Asia/Taipei")
        self.assertEqual(result["kind"], "every")
        self.assertEqual(result["everyMs"], 3600000)


if __name__ == "__main__":
    unittest.main()
