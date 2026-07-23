import re
import urllib.parse

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import get_json
from openclaw_runtime.skills.base import SkillResult


DEFAULT_LOCATION_MAP = {
    "英國": "London,United Kingdom",
    "倫敦": "London,United Kingdom",
    "united kingdom": "London,United Kingdom",
    "london": "London,United Kingdom",
    "台灣": "Taipei,Taiwan",
    "臺灣": "Taipei,Taiwan",
    "台湾": "Taipei,Taiwan",
    "taiwan": "Taipei,Taiwan",
    "台北": "Taipei,Taiwan",
    "臺北": "Taipei,Taiwan",
    "taipei": "Taipei,Taiwan",
    "新北": "New Taipei,Taiwan",
    "桃園": "Taoyuan,Taiwan",
    "新竹": "Hsinchu,Taiwan",
    "台中": "Taichung,Taiwan",
    "臺中": "Taichung,Taiwan",
    "台南": "Tainan,Taiwan",
    "臺南": "Tainan,Taiwan",
    "高雄": "Kaohsiung,Taiwan",
    "基隆": "Keelung,Taiwan",
    "宜蘭": "Yilan,Taiwan",
    "花蓮": "Hualien,Taiwan",
    "台東": "Taitung,Taiwan",
    "臺東": "Taitung,Taiwan",
    "澎湖": "Penghu,Taiwan",
    "東京": "Tokyo,Japan",
    "tokyo": "Tokyo,Japan",
    "紐約": "New York,USA",
    "new york": "New York,USA",
    "舊金山": "San Francisco,USA",
    "san francisco": "San Francisco,USA",
}

LOCATION_LABELS = {
    "London,United Kingdom": "the UK (London)",
    "Taipei,Taiwan": "Taiwan (Taipei)",
    "New Taipei,Taiwan": "New Taipei",
    "Taoyuan,Taiwan": "Taoyuan",
    "Hsinchu,Taiwan": "Hsinchu",
    "Taichung,Taiwan": "Taichung",
    "Tainan,Taiwan": "Tainan",
    "Kaohsiung,Taiwan": "Kaohsiung",
    "Keelung,Taiwan": "Keelung",
    "Yilan,Taiwan": "Yilan",
    "Hualien,Taiwan": "Hualien",
    "Taitung,Taiwan": "Taitung",
    "Penghu,Taiwan": "Penghu",
}


class WeatherSkill:
    name = "weather"

    def __init__(self, settings: Settings, config: dict) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["天氣", "氣溫", "下雨", "降雨", "weather"]))
        self.location_map = dict(DEFAULT_LOCATION_MAP)
        self.location_map.update(config.get("locations", {}))

    def can_handle(self, text: str) -> bool:
        lowered_text = text.casefold()
        return any(keyword.casefold() in lowered_text for keyword in self.keywords)

    def run(self, text: str) -> SkillResult:
        location = self._extract_location(text)
        if not location:
            return SkillResult(
                self.name,
                'Please include a location, for example: "What is the weather in Berlin tomorrow?"',
            )
        index = self._day_index(text)
        url = "https://wttr.in/" + urllib.parse.quote(location) + "?format=j1"
        data = get_json(url, timeout=self.settings.web_timeout)
        days = data.get("weather", [])
        if not days:
            return SkillResult(self.name, "I reached the weather service, but no usable forecast data came back.")

        day = days[min(index, len(days) - 1)]
        hourly = day.get("hourly", [])
        noon = hourly[len(hourly) // 2] if hourly else {}
        desc = ""
        if noon.get("weatherDesc"):
            desc = noon["weatherDesc"][0].get("value", "")
        chance_rain = noon.get("chanceofrain", "unknown")
        label = "the day after tomorrow" if index == 2 else "tomorrow" if index == 1 else "today"
        place = LOCATION_LABELS.get(location, location)
        answer = (
            f"Weather in {place} {label}: {desc or 'conditions unclear'}, "
            f"high around {day.get('maxtempC', '?')}°C, low around {day.get('mintempC', '?')}°C, "
            f"midday feels-like around {noon.get('FeelsLikeC', '?')}°C, chance of rain about {chance_rain}%."
        )
        return SkillResult(self.name, answer)

    def _extract_location(self, text: str) -> str:
        lowered_text = text.lower()
        for keyword, location in self.location_map.items():
            if keyword.lower() in lowered_text:
                return location
        # English: "weather in X", "forecast for X", optionally followed by
        # today/tomorrow/now/a trailing question mark, etc.
        match = re.search(
            r"(?:weather|forecast|temperature).{0,15}?\b(?:in|for|at)\s+"
            r"([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|now|please|thanks)\b|[?.!,]|$)",
            text,
            re.IGNORECASE,
        )
        if match:
            cleaned = self._clean_location(match.group(1))
            if cleaned:
                return cleaned
        # English: "X weather" (location named before the word "weather")
        match = re.search(r"^([A-Za-z][A-Za-z .'-]*?)\s+weather\b", text.strip(), re.IGNORECASE)
        if match:
            cleaned = self._clean_location(match.group(1))
            if cleaned:
                return cleaned
        # Chinese natural-language fallback, e.g. "劍橋今天天氣如何"
        match = re.search(r"(.+?)(?:明天|今天|後天|本日|今日|現在|目前)?(?:天氣|氣溫|會下雨)", text)
        if match:
            cleaned = self._clean_location(match.group(1))
            if cleaned:
                return cleaned
        return self.settings.default_weather_location or ""

    @staticmethod
    def _clean_location(raw: str) -> str:
        cleaned = raw.strip(" ，,。?？")
        cleaned = re.sub(r"(的|地區|附近|這邊|那邊)$", "", cleaned)
        cleaned = cleaned.strip(" ，,。?？")
        return cleaned

    @staticmethod
    def _day_index(text: str) -> int:
        lowered = text.lower()
        if "後天" in text or "day after tomorrow" in lowered:
            return 2
        if "明天" in text or "tomorrow" in lowered:
            return 1
        return 0
