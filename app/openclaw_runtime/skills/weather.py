import re
import urllib.parse

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import get_json
from openclaw_runtime.skills.base import SkillResult


DEFAULT_LOCATION_MAP = {
    "英國": "London,United Kingdom",
    "倫敦": "London,United Kingdom",
    "台灣": "Taipei,Taiwan",
    "臺灣": "Taipei,Taiwan",
    "台湾": "Taipei,Taiwan",
    "台北": "Taipei,Taiwan",
    "臺北": "Taipei,Taiwan",
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
    "紐約": "New York,USA",
    "舊金山": "San Francisco,USA",
}

LOCATION_LABELS = {
    "London,United Kingdom": "英國（以倫敦為代表）",
    "Taipei,Taiwan": "台灣（以台北為代表）",
    "New Taipei,Taiwan": "新北",
    "Taoyuan,Taiwan": "桃園",
    "Hsinchu,Taiwan": "新竹",
    "Taichung,Taiwan": "台中",
    "Tainan,Taiwan": "台南",
    "Kaohsiung,Taiwan": "高雄",
    "Keelung,Taiwan": "基隆",
    "Yilan,Taiwan": "宜蘭",
    "Hualien,Taiwan": "花蓮",
    "Taitung,Taiwan": "台東",
    "Penghu,Taiwan": "澎湖",
}


class WeatherSkill:
    name = "weather"

    def __init__(self, settings: Settings, config: dict) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["天氣", "氣溫", "下雨", "降雨", "weather"]))
        self.location_map = dict(DEFAULT_LOCATION_MAP)
        self.location_map.update(config.get("locations", {}))

    def can_handle(self, text: str) -> bool:
        return any(keyword in text for keyword in self.keywords)

    def run(self, text: str) -> SkillResult:
        location = self._extract_location(text)
        index = self._day_index(text)
        url = "https://wttr.in/" + urllib.parse.quote(location) + "?format=j1"
        data = get_json(url, timeout=self.settings.web_timeout)
        days = data.get("weather", [])
        if not days:
            return SkillResult(self.name, "我查到了天氣服務，但沒有取得可用的預報資料。")

        day = days[min(index, len(days) - 1)]
        hourly = day.get("hourly", [])
        noon = hourly[len(hourly) // 2] if hourly else {}
        desc = ""
        if noon.get("weatherDesc"):
            desc = noon["weatherDesc"][0].get("value", "")
        chance_rain = noon.get("chanceofrain", "未知")
        label = "後天" if index == 2 else "明天" if index == 1 else "今天"
        place = LOCATION_LABELS.get(location, location)
        answer = (
            f"{place}{label}天氣：{desc or '天氣狀況未明'}，"
            f"最高約 {day.get('maxtempC', '?')}°C、最低約 {day.get('mintempC', '?')}°C，"
            f"中午體感約 {noon.get('FeelsLikeC', '?')}°C，降雨機率約 {chance_rain}%。"
        )
        return SkillResult(self.name, answer)

    def _extract_location(self, text: str) -> str:
        for keyword, location in self.location_map.items():
            if keyword in text:
                return location
        match = re.search(r"(.+?)(?:明天|今天|後天|本日|今日|現在|目前)?(?:天氣|氣溫|會下雨)", text)
        if match:
            cleaned = self._clean_location(match.group(1))
            if cleaned:
                return cleaned
        return "London,United Kingdom"

    @staticmethod
    def _clean_location(raw: str) -> str:
        cleaned = raw.strip(" ，,。?？")
        cleaned = re.sub(r"(的|地區|附近|這邊|那邊)$", "", cleaned)
        cleaned = cleaned.strip(" ，,。?？")
        return cleaned

    @staticmethod
    def _day_index(text: str) -> int:
        if "後天" in text:
            return 2
        if "明天" in text or "tomorrow" in text.lower():
            return 1
        return 0
