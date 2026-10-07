#!/usr/bin/env python3
import base64
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import secrets
import sys
from typing import Any, Optional, Union
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

# ==========================================
# CONFIGURATION
# ==========================================
SPP_BASE_URL = "https://moje.spp.sk"
CLIENT_ID = "nzp"
REDIRECT_URI = "https://moje.spp.sk/authentication/success"
SCOPE = "openid customer"
LOCAL_TZ = ZoneInfo("Europe/Bratislava")
TARGET_EIC = "24ZVS0000788066T"

# High tariff (VT) windows in local time
HIGH_TARIFF_WINDOWS = [
    (time(8, 30), time(9, 30)),
    (time(18, 30), time(19, 30)),  # Handles midnight rollover
]

USER_EMAIL = "gurnik@icloud.com"
USER_PASSWORD = "Nu9TpBbzNFdXaXfqQzPL"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


# ==========================================
# AUTHENTICATION ENGINE (OAUTH2 + PKCE)
# ==========================================
def generate_pkce_pair() -> tuple[str, str]:
    """Generates PKCE code_verifier and S256 code_challenge per RFC 7636."""
    code_verifier = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = (
        base64.urlsafe_b64encode(hashed).decode("ascii").rstrip("=")
    )
    return code_verifier, code_challenge


def authenticate_and_get_tokens(username: str, password: str) -> dict:
    """Executes the Authorization Code Flow with PKCE and returns token payload."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_hex(16)

    # Step 1: Initialize OAuth2 PKCE authorization request
    auth_params = {
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "state": state,
        "scope": SCOPE,
        "locale": "sk",
    }

    init_url = f"{SPP_BASE_URL}/prihlasenie/oauth/authorize"
    res = session.get(
        init_url, params=auth_params, allow_redirects=True, timeout=15
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"Initial GET to authorize endpoint failed: HTTP {res.status_code}"
        )

    login_page_url = res.url

    # Step 2: Parse login form inputs and CSRF state
    soup = BeautifulSoup(res.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError(
            "Login form not found (possible CAPTCHA or WAF interception)."
        )

    action_url = urljoin(login_page_url, form.get("action", ""))

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")

    username_field_found = False
    password_field_found = False

    for name in list(payload.keys()):
        lower_name = name.lower()
        if any(x in lower_name for x in ["user", "login", "email"]):
            payload[name] = username
            username_field_found = True
        elif any(x in lower_name for x in ["pass", "heslo"]):
            payload[name] = password
            password_field_found = True

    if not username_field_found:
        payload["username"] = username
    if not password_field_found:
        payload["password"] = password

    # Step 3: POST credentials and follow redirects to capture authorization code
    login_headers = {
        "Referer": login_page_url,
        "Origin": SPP_BASE_URL,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    post_res = session.post(
        action_url,
        data=payload,
        headers=login_headers,
        allow_redirects=False,
        timeout=15,
    )

    auth_code: Optional[str] = None
    curr_res = post_res

    while curr_res.is_redirect or curr_res.status_code in (301, 302, 303, 307):
        redirect_url = urljoin(
            curr_res.url, curr_res.headers.get("Location", "")
        )
        parsed = urlparse(redirect_url)

        if redirect_url.startswith(REDIRECT_URI):
            qs = parse_qs(parsed.query)
            if "code" in qs:
                auth_code = qs["code"][0]
                break

        curr_res = session.get(redirect_url, allow_redirects=False, timeout=15)

    if not auth_code:
        raise RuntimeError(
            "Failed to retrieve authorization code (invalid credentials or 2FA challenge)."
        )

    # Step 4: Exchange code for Bearer token using code_verifier
    token_url = f"{SPP_BASE_URL}/prihlasenie/oauth/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }

    token_res = session.post(token_url, data=token_payload, timeout=15)
    if token_res.status_code == 404:
        token_url = f"{SPP_BASE_URL}/oauth/token"
        token_res = session.post(token_url, data=token_payload, timeout=15)

    if token_res.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: HTTP {token_res.status_code} - {token_res.text}"
        )

    return token_res.json()


# ==========================================
# CONSUMPTION API CLIENT & PROCESSOR
# ==========================================
def fetch_spp_consumption(
    access_token: str,
    contract_id: str,
    date_from: Union[datetime, str],
    date_to: Union[datetime, str],
    interval: int = 1400,
    units: str = "KW",
    reading_type: str = "INTERVAL_METER_READING",
) -> dict:
    """Dispatches POST search request to MojeSPP consumption API."""
    url = f"{SPP_BASE_URL}/api/delivery-points/contracts/{contract_id}/consumptions/search"

    def format_iso_utc(dt: Union[datetime, str]) -> str:
        if isinstance(dt, str):
            return dt
        utc_dt = (
            dt.astimezone(timezone.utc)
            if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc)
        )
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "readAt": {
            "from": format_iso_utc(date_from),
            "to": format_iso_utc(date_to),
        },
        "type": reading_type,
        "interval": interval,
        "units": units,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-AppType": "WEB",
        "Origin": SPP_BASE_URL,
        "Referer": f"{SPP_BASE_URL}/",
        "User-Agent": USER_AGENT,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def is_time_in_window(check_time: time, start: time, end: time) -> bool:
    """Checks whether check_time falls within [start, end) interval."""
    if start <= end:
        return start <= check_time < end
    else:
        # Range spanning midnight
        return check_time >= start or check_time < end


def is_high_tariff(local_dt: datetime) -> bool:
    """Evaluates whether timestamp falls into configured high tariff window."""
    sample_time = local_dt.time()
    for start, end in HIGH_TARIFF_WINDOWS:
        if is_time_in_window(sample_time, start, end):
            return True
    return False


def process_spp_consumption(api_data: dict[str, Any]) -> dict[str, float]:
    """Processes MojeSPP JSON response and computes total, low, and high tariff kWh."""
    total_kwh = 0.0
    high_kwh = 0.0
    low_kwh = 0.0

    tariffs = api_data.get("tariffs", [])
    if not tariffs:
        return {"total_kwh": 0.0, "low_tariff_kwh": 0.0, "high_tariff_kwh": 0.0}

    for tariff in tariffs:
        for entry in tariff.get("values", []):
            kw_val = float(entry.get("value", 0.0))
            kwh = kw_val * 0.25  # 15-minute interval = 0.25 hours

            period_from_str = entry["period"]["from"]
            if not period_from_str.endswith("Z"):
                period_from_str += "Z"

            dt_utc = datetime.fromisoformat(period_from_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(LOCAL_TZ)

            if is_high_tariff(dt_local):
                high_kwh += kwh
                print(f"{kwh} kWh at {dt_local} is HIGH tariff.")
            else:
                low_kwh += kwh
                print(f"{kwh} kWh at {dt_local} is LOW tariff.")

            total_kwh += kwh

    return {
        "total_kwh": round(total_kwh, 4),
        "low_tariff_kwh": round(low_kwh, 4),
        "high_tariff_kwh": round(high_kwh, 4),
    }


def extract_customer_id_from_token(token: str) -> str:
    """Extracts the 'sub' (customer UUID) claim from the JWT access token

    without external cryptography dependencies.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("Malformed JWT token string")

        # Handle Base64 URL-safe padding
        payload_b64 = parts[1]
        padded_b64 = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        customer_id = payload.get("sub")
        if not customer_id:
            raise KeyError("JWT payload missing 'sub' claim")
        return customer_id
    except Exception as exc:
        raise ValueError(
            f"Failed to extract customer ID from token: {exc}"
        ) from exc


def find_delivery_point_by_eic(
    access_token: str,
    target_eic: str = "24ZVS0000788066T",
    customer_id: Optional[str] = None,
    spp_base_url: str = "https://moje.spp.sk",
) -> Optional[dict[str, Any]]:
    """Calls united-delivery-points/search and returns the matching deliveryPoint dictionary.

    :param access_token: Bearer JWT acquired from the OAuth2 PKCE flow.
    :param target_eic: The EIC identifier to look for (e.g. '24ZVS0000788066T').
    :param customer_id: Optional customer UUID. If None, derived from JWT
      'sub'.
    :param spp_base_url: Base domain of MojeSPP.
    :return: The matching deliveryPoint dict, or None if not found.
    """
    if not customer_id:
        customer_id = extract_customer_id_from_token(access_token)

    search_url = (
        f"{spp_base_url}/api/customers/{customer_id}/united-delivery-points/search"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-AppType": "WEB",
        "Origin": spp_base_url,
        "Referer": f"{spp_base_url}/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
    }

    payload = {
        "pairingDone": True,
        "includeInactive": True,
        "paging": {"size": 1000},
        "shared": False,
    }

    res = requests.post(search_url, json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    data = res.json()

    # Traverse accounts -> deliveryPoints
    for entry in data.get("result", []):
        for dp in entry.get("deliveryPoints", []):
            if dp.get("eic") == target_eic:
                return dp

    return None

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    try:
        print("[*] Authenticating against MojeSPP IdP...")
        tokens = authenticate_and_get_tokens(USER_EMAIL, USER_PASSWORD)
        jwt_token = tokens["access_token"]
        print("[+] Authentication successful, token acquired.")

        customer_id = extract_customer_id_from_token(jwt_token)
        print(f"[+] Customer ID extracted from token: {customer_id}")

        delivery_point = find_delivery_point_by_eic(access_token=jwt_token, target_eic=TARGET_EIC).get("contract").get("id")
        print(f"[+] Delivery point found for EIC {TARGET_EIC}: {delivery_point}")

        # Determine target period: full previous day (00:00:00 to 23:59:59 local time)
        now_local = datetime.now(LOCAL_TZ)
        yesterday = now_local.date() - timedelta(days=1)

        two_days_ago = now_local.date() - timedelta(days=2)
        local_start = datetime.combine(two_days_ago, time.min, tzinfo=LOCAL_TZ)
        local_end = datetime.combine(
            two_days_ago, time(23, 59, 59, 999000), tzinfo=LOCAL_TZ
        )

        # local_start = datetime.combine(yesterday, time.min, tzinfo=LOCAL_TZ)
        # local_end = datetime.combine(yesterday, time(23, 59, 59, 999000), tzinfo=LOCAL_TZ)

        print(
            f"[*] Querying consumption for contract: {delivery_point} ({local_start} -> {local_end})..."
        )
        raw_data = fetch_spp_consumption(
            access_token=jwt_token,
            contract_id=delivery_point,
            date_from=local_start,
            date_to=local_end,
            interval=1400,
            units="KW",
        )

        print("[+] Raw payload received successfully.")

        # Pass parsed dictionary directly to avoid AttributeError
        metrics = process_spp_consumption(raw_data)

        print("\nEnergy Balance Summary:")
        print(f"  Total Energy:       {metrics['total_kwh']} kWh")
        print(f"  Low Tariff (NT):    {metrics['low_tariff_kwh']} kWh")
        print(f"  High Tariff (VT):   {metrics['high_tariff_kwh']} kWh")

    except Exception as err:
        print(f"[!] Error: {err}", file=sys.stderr)
        sys.exit(1)