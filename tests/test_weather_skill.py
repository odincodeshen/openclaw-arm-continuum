import unittest

from openclaw_runtime.skills.weather import DEFAULT_LOCATION_MAP, WeatherSkill


def build_weather_skill() -> WeatherSkill:
    skill = WeatherSkill.__new__(WeatherSkill)
    skill.location_map = dict(DEFAULT_LOCATION_MAP)
    return skill


class ExtractLocationTest(unittest.TestCase):
    def test_recognizes_today_phrasing(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("劍橋今天天氣如何"), "劍橋")

    def test_recognizes_formal_today_phrasing(self) -> None:
        # Regression guard: "本日" (formal "today") was not in the day-word
        # regex, so it leaked into the extracted location as "劍橋本日",
        # which wttr.in cannot resolve and answers with HTTP 500.
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

    def test_unrecognized_query_falls_back_to_default(self) -> None:
        skill = build_weather_skill()
        self.assertEqual(skill._extract_location("天氣如何"), "London,United Kingdom")


if __name__ == "__main__":
    unittest.main()
