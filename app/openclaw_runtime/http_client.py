import ipaddress
import json
import socket
import urllib.parse
import urllib.request


USER_AGENT = "OpenClaw-Arm-Continuum/0.1"


def assert_public_url(url: str) -> None:
    """Reject URLs that point at private, loopback, or link-local addresses.

    Only call this on URLs that come from user input (e.g. a Telegram
    message). It resolves the hostname and checks every returned address so a
    public hostname that DNS-rebinds to an internal IP is still blocked.
    Internal service calls (Ollama, Qdrant, vLLM on 127.0.0.1) must not go
    through this check.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("url is missing a host")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host: {parsed.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError(f"refusing to fetch a private or reserved address: {address}")


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
