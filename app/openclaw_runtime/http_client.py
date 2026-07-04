import json
import urllib.request


USER_AGENT = "OpenClaw-Arm-Continuum/0.1"


def request_json(method: str, url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def is_reachable(url: str, timeout: int = 3) -> bool:
    try:
        get_json(url, timeout=timeout)
        return True
    except Exception:
        return False
