import unittest
from types import SimpleNamespace

from openclaw_runtime.skills.weather import DEFAULT_LOCATION_MAP, WeatherSkill


def build_weather_skill(default_location: str = "") -> WeatherSkill:
    skill = WeatherSkill.__new__(WeatherSkill)
    skill.location_map = dict(DEFAULT_LOCATION_MAP)
    skill.settings = SimpleNamespace(default_weather_location=default_location)
    return skill


class ExtractLocationTest(unittest.TestCase):
    def test_weather_keyword_matching_is_case_insensitive(self) -> None:
        skill = build_weather_skill()
        skill.keywords = ("weather",)
        self.assertTrue(skill.can_handle("Weather tomorrow"))

    def test_query_without_location_has_no_implicit_regional_default(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("weather tomorrow"), "")

    def test_query_without_location_uses_configured_default(self) -> None:
        skill = build_weather_skill("Singapore")
        self.assertEqual(skill._extract_location("weather tomorrow"), "Singapore")

    def test_recognizes_english_weather_in_phrasing(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("what is the weather in Cambridge today"), "Cambridge")

    def test_recognizes_english_forecast_for_phrasing(self) -> None:
        # "Tokyo" is a known alias, so this resolves to the wttr.in-ready
        # string rather than the raw regex-extracted city name.
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("forecast for Tokyo"), "Tokyo,Japan")

    def test_recognizes_english_location_before_weather_phrasing(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("Cambridge weather"), "Cambridge")

    def test_recognizes_multi_word_english_location(self) -> None:
        # "New York" is a known alias, so this resolves to the wttr.in-ready
        # string rather than the raw regex-extracted city name.
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("what is the weather like in New York?"), "New York,USA")

    def test_known_english_city_alias_uses_location_map(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("Taiwan weather tomorrow"), "Taipei,Taiwan")

    def test_run_requests_location_before_contacting_weather_service(self) -> None:
        skill = build_weather_skill()
        result = skill.run("weather tomorrow")
        self.assertIn("include a location", result.answer)


class MultilingualExtractLocationTest(unittest.TestCase):
    def test_recognizes_today_phrasing(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("劍橋今天天氣如何"), "劍橋")

    def test_recognizes_formal_today_phrasing(self) -> None:
        # Regression guard for the formal Chinese word for "today".
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("劍橋本日天氣如何"), "劍橋")

    def test_recognizes_alternate_today_phrasings(self) -> None:
        skill = build_weather_skill()
        for phrase in ["劍橋今日天氣如何", "劍橋現在天氣如何", "劍橋目前天氣如何"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(skill._extract_location(phrase), "劍橋")

    def test_recognizes_tomorrow_phrasing(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("劍橋明天天氣如何"), "劍橋")

    def test_known_city_alias_uses_location_map(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("英國今天天氣如何"), "London,United Kingdom")


class DayIndexTest(unittest.TestCase):
    def test_english_today_defaults_to_zero(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._day_index("weather in Cambridge today"), 0)

    def test_english_tomorrow(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._day_index("weather in Cambridge tomorrow"), 1)

    def test_english_day_after_tomorrow(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._day_index("weather in Cambridge the day after tomorrow"), 2)


class MultilingualDayIndexTest(unittest.TestCase):
    def test_chinese_day_after_tomorrow(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._day_index("劍橋後天天氣如何"), 2)


if __name__ == "__main__":
    unittest.main()
