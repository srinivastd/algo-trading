"""
IBKR Flex Web Service client (HTTPS, two-step SendRequest -> GetStatement).

Docs: https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

FLEX_BASE_DEFAULT = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_VERSION = "3"
USER_AGENT = "algo-trading-flex-import/1.0 (Python urllib)"


@dataclass
class FlexSendResult:
    reference_code: str | None
    status: str | None
    error_code: str | None
    error_message: str | None


def _http_get(url: str, *, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_send_request_xml(body: str) -> FlexSendResult:
    root = ET.fromstring(body)
    status = error_code = error_message = reference_code = None
    for child in root:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "Status":
            status = (child.text or "").strip() or None
        elif tag == "ReferenceCode":
            reference_code = (child.text or "").strip() or None
        elif tag == "ErrorCode":
            error_code = (child.text or "").strip() or None
        elif tag == "ErrorMessage":
            error_message = (child.text or "").strip() or None
    return FlexSendResult(
        reference_code=reference_code,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def parse_get_statement_error_xml(body: str) -> FlexSendResult:
    """GetStatement failures sometimes return the same envelope as SendRequest."""
    if not body.strip().startswith("<"):
        return FlexSendResult(None, None, None, None)
    try:
        return parse_send_request_xml(body)
    except ET.ParseError:
        return FlexSendResult(None, None, None, None)


_RETRIABLE_CODES = frozenset({"1001", "1003", "1004", "1005", "1009", "1019", "1021"})


def send_flex_request(*, token: str, query_id: str, base_url: str = FLEX_BASE_DEFAULT) -> FlexSendResult:
    params = urllib.parse.urlencode({"t": token, "q": str(query_id), "v": FLEX_VERSION})
    url = f"{base_url.rstrip('/')}/SendRequest?{params}"
    raw = _http_get(url, timeout=60)
    text = raw.decode("utf-8", errors="replace")
    return parse_send_request_xml(text)


def get_flex_statement(
    *,
    token: str,
    reference_code: str,
    base_url: str = FLEX_BASE_DEFAULT,
    max_retries: int = 8,
    initial_sleep_s: float = 5.0,
    retry_sleep_s: float = 12.0,
) -> bytes:
    """
    Fetch report body. IB may need time after SendRequest; retries on 'incomplete' style errors.
    """
    params = urllib.parse.urlencode({"t": token, "q": reference_code, "v": FLEX_VERSION})
    url = f"{base_url.rstrip('/')}/GetStatement?{params}"
    time.sleep(max(0.0, initial_sleep_s))
    last_body: bytes = b""
    for attempt in range(max_retries):
        try:
            last_body = _http_get(url, timeout=180)
        except urllib.error.HTTPError as e:
            last_body = e.read() if e.fp else b""
            if attempt < max_retries - 1:
                time.sleep(retry_sleep_s)
                continue
            raise
        text = last_body.decode("utf-8", errors="replace").strip()
        err = parse_get_statement_error_xml(text)
        if err.status == "Fail" and err.error_code in _RETRIABLE_CODES and attempt < max_retries - 1:
            time.sleep(retry_sleep_s)
            continue
        if err.status == "Fail":
            msg = err.error_message or f"error_code={err.error_code}"
            raise RuntimeError(f"Flex GetStatement failed: {msg}")
        return last_body
    return last_body


def fetch_flex_trade_report(
    *,
    token: str,
    query_id: str,
    base_url: str = FLEX_BASE_DEFAULT,
    initial_sleep_s: float = 5.0,
) -> bytes:
    send = send_flex_request(token=token, query_id=query_id, base_url=base_url)
    if send.status != "Success" or not send.reference_code:
        msg = send.error_message or f"status={send.status} code={send.error_code}"
        raise RuntimeError(f"Flex SendRequest failed: {msg}")
    return get_flex_statement(
        token=token,
        reference_code=send.reference_code,
        base_url=base_url,
        initial_sleep_s=initial_sleep_s,
    )


def flex_rows_look_like_csv(sample: str) -> bool:
    head = sample.lstrip()[:2000]
    if head.startswith("<"):
        return False
    if "\n" not in head and "\r" not in head:
        return False
    first_line = head.splitlines()[0]
    return "," in first_line or "\t" in first_line
