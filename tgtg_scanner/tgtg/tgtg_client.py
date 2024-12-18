# copied and modified from https://github.com/ahivert/tgtg-python

import json
import logging
import random
import re
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from typing import List, Union
from urllib.parse import urljoin, urlparse
import urllib3
import importlib
from dataclasses import dataclass, field

import requests
from fp.fp import FreeProxy
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

urllib3.disable_warnings(category=urllib3.exceptions.InsecureRequestWarning)

from tgtg_scanner.errors import (
    TgtgAPIError,
    TGTGConfigurationError,
    TgtgLoginError,
    TgtgPollingError,
)

log = logging.getLogger("tgtg")
BASE_URL = "https://apptoogoodtogo.com/api/"
API_ITEM_ENDPOINT = "item/v8/"
FAVORITE_ITEM_ENDPOINT = "user/favorite/v1/{}/update"
AUTH_BY_EMAIL_ENDPOINT = "auth/v4/authByEmail"
AUTH_POLLING_ENDPOINT = "auth/v4/authByRequestPollingId"
SIGNUP_BY_EMAIL_ENDPOINT = "auth/v4/signUpByEmail"
REFRESH_ENDPOINT = "auth/v4/token/refresh"
ACTIVE_ORDER_ENDPOINT = "order/v7/active"
INACTIVE_ORDER_ENDPOINT = "order/v7/inactive"
CREATE_ORDER_ENDPOINT = "order/v7/create/"
ABORT_ORDER_ENDPOINT = "order/v7/{}/abort"
ORDER_STATUS_ENDPOINT = "order/v7/{}/status"
MANUFACTURERITEM_ENDPOINT = "manufactureritem/v2/"
ORDER_PAY_ENDPOINT = "order/v7/{}/pay"
PAYMENT_ENDPOINT = "payment/v3/"
USER_AGENTS = [
    "TGTG/{} Dalvik/2.1.0 (Linux; U; Android 9; Nexus 5 Build/M4B30Z)",
    "TGTG/{} Dalvik/2.1.0 (Linux; U; Android 10; SM-G935F Build/NRD90M)",
    "TGTG/{} Dalvik/2.1.0 (Linux; Android 12; SM-G920V Build/MMB29K)",
]
DEFAULT_ACCESS_TOKEN_LIFETIME = 3600 * 4  # 4 hours
DEFAULT_MAX_POLLING_TRIES = 24  # 24 * POLLING_WAIT_TIME = 2 minutes
DEFAULT_POLLING_WAIT_TIME = 5  # Seconds
DEFAULT_APK_VERSION = "24.10.1"

APK_RE_SCRIPT = re.compile(r"AF_initDataCallback\({key:\s*'ds:5'.*?data:([\s\S]*?), sideChannel:.+<\/script")

def validate_proxy_response(response):
    """
    Validate the response from the proxy
    """
    try:
        # Check if response is not empty
        if not response or not response.text:
            return False
        
        # Try to parse as JSON (for httpbin.org/ip)
        try:
            data = response.json()
            if 'origin' in data:
                return True
        except json.JSONDecodeError:
            # If not JSON, check for basic content
            if response.status_code == 200:
                # Check for meaningful content
                content_length = len(response.text)
                has_ip_like_content = any(
                    str(part) in response.text 
                    for part in response.text.replace('.', ' ').split()
                )
                return content_length > 10 and has_ip_like_content
        
        return False
    except Exception as e:
        print(f"Response validation error: {e}")
        return False

def test_proxy(proxy):
    """
    Test a single proxy
    """
    if proxy.startswith('socks4://'):
        proxies = {
            'http': proxy,
            'https': proxy
        }
    elif proxy.startswith('http://'):
        proxies = {
            'http': proxy,
            'https': proxy
        }
    else:
        print(f"Unsupported proxy format: {proxy}")
        return False

    try:
        # Test the proxy with a quick request
        response = requests.get('https://apptoogoodtogo.com/', 
                                proxies=proxies,
                                timeout=15)
        if validate_proxy_response(response):
            print(f"[INFO] Proxy {proxy} works!\n")
            return True
        else:
            return False
    except Exception as e:
        print(f"Proxy {proxy} \n failed: {e}")
        return False

class TgtgSession(requests.Session):
    http_adapter = HTTPAdapter(
        max_retries=Retry(
            total=5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            backoff_factor=1,
        )
    )

    def __init__(
        self,
        user_agent: Union[str, None] = None,
        language: str = "en-UK",
        timeout: Union[int, None] = None,
        proxies: Union[dict, None] = None,
        datadome_cookie: Union[str, None] = None,
        base_url: str = BASE_URL,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.mount("https://", self.http_adapter)
        self.mount("http://", self.http_adapter)
        self.headers = {
            "accept-language": language,
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip",
        }
        if user_agent:
            self.headers["user-agent"] = user_agent
        self.timeout = timeout
        if proxies:
            self.proxies = proxies
        if datadome_cookie:
            domain = urlparse(base_url).netloc.split(":")[0]
            domain = f".{'local' if domain == 'localhost' else domain}"
            self.cookies.set("datadome", datadome_cookie, domain=domain, path="/", secure=True)

    def post(self, *args, access_token: Union[str, None] = None, **kwargs) -> requests.Response:
        headers = kwargs.get("headers")
        if headers is None and getattr(self, "headers"):
            kwargs["headers"] = getattr(self, "headers")
        if "headers" in kwargs and access_token:
            kwargs["headers"]["authorization"] = f"Bearer {access_token}"
        return super().post(*args, **kwargs)

    def send(self, request, **kwargs):
        for key in ["timeout", "proxies"]:
            val = kwargs.get(key)
            if val is None and hasattr(self, key):
                kwargs[key] = getattr(self, key)
        return super().send(request, **kwargs)


class TgtgClient:
    def __init__(
        self,
        base_url=BASE_URL,
        email=None,
        access_token=None,
        refresh_token=None,
        datadome_cookie=None,
        user_agent=None,
        language="en-GB",
        proxies=None,
        timeout=None,
        access_token_lifetime=DEFAULT_ACCESS_TOKEN_LIFETIME,
        max_polling_tries=DEFAULT_MAX_POLLING_TRIES,
        polling_wait_time=DEFAULT_POLLING_WAIT_TIME,
        device_type="ANDROID",
        
    ):
        if base_url != BASE_URL:
            log.warn("Using custom tgtg base url: %s", base_url)

        self.base_url = base_url

        self.email = email
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.datadome_cookie = datadome_cookie

        self.last_time_token_refreshed = None
        self.access_token_lifetime = access_token_lifetime
        self.max_polling_tries = max_polling_tries
        self.polling_wait_time = polling_wait_time

        self.device_type = device_type
        self.fixed_user_agent = user_agent
        self.user_agent = user_agent
        self.language = language
        self.proxies = proxies
        self.timeout = timeout
        self.session = None

        self.captcha_error_count = 0
        from tgtg_scanner.notifiers import Notifiers
        self.notifiers: Union[Notifiers, None] = None

    def __del__(self) -> None:
        if self.session:
            self.session.close()

    def _get_url(self, path) -> str:
        return urljoin(self.base_url, path)

    def _create_session(self) -> TgtgSession:
        if not self.user_agent:
            self.user_agent = self._get_user_agent()
        return TgtgSession(
            self.user_agent,
            self.language,
            self.timeout,
            self.proxies,
            self.datadome_cookie,
            self.base_url,
        )

    def get_credentials(self) -> dict:
        """Returns current tgtg api credentials.

        Returns:
            dict: Dictionary containing access token, refresh token and user id
        """
        self.login()
        return {
            "email": self.email,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "datadome_cookie": self.datadome_cookie,
        }

    def _post(self, path, **kwargs) -> requests.Response:  
        if not self.session:
            self.session = self._create_session()
        self.session.verify = False
        if self.proxies:
            self.session.proxies = self.proxies

        response = self.session.post(
            self._get_url(path),
            access_token=self.access_token,
            **kwargs
        )
        self.datadome_cookie = self.session.cookies.get("datadome")
        try:
            response.raise_for_status()
            if response.status_code in (HTTPStatus.OK, HTTPStatus.ACCEPTED):
                if response.headers["content-type"].strip().startswith("application/json"):
                    self.captcha_error_count = 0
                    return response
                else:
                    # seems to be a proxy error, try a new one
                    self.captcha_error_count = max(self.captcha_error_count+1, 10)
            else:
                print(f"bad response {response}")
        except Exception as e:
                print(f"bad response exception: {e}")
                self.captcha_error_count += 1
        # Status Code == 403
        # --> Blocked due to rate limit / wrong user_agent.
        # 1. Try: Get latest APK Version from google
        # 2. Try: Reset session
        # 3. Try: Delete datadome cookie and reset session
        # 10.Try: Sleep 10 minutes, and reset session
        if response.status_code == 403:
            log.debug("Connection Error!")
            self.captcha_error_count += 1
            if self.captcha_error_count == 1:
                self.user_agent = self._get_user_agent()
            elif self.captcha_error_count == 2:
                self.session = self._create_session()
            elif self.captcha_error_count == 4:
                self.datadome_cookie = None
                self.session = self._create_session()
        
        if self.captcha_error_count >= 10:
            if self.captcha_error_count > 100:
                log.warning("No proxy is useful, So sleep for 15 minutes")
                if self.notifiers:
                    from tgtg_scanner.models import Item
                    message = Item({
                        "display_name": "tgtg Connection failed. Sleep for 15 minutes.",})
                    self.notifiers.send(message)
                    time.sleep(15 * 60)
                    message = Item({
                        "display_name": "tgtg Connection restarts now after sleep for 15 minutes.",})
                    self.notifiers.send(message)
                else:
                    time.sleep(15 * 60)
                self.proxies = None
                self.captcha_error_count = 0
            elif self.captcha_error_count == 100:
                self.proxies = None
            else:
                log.info("Find a new proxy...")
                try:
                    proxy = FreeProxy(url='http://apptoogoodtogo.com', rand=True, elite=True).get()
                    if proxy:
                        proxies = {
                            'http': proxy,
                            'https': proxy
                        }
                        log.info(f"Attempt {self.captcha_error_count}: trying with proxy {proxy}")
                        self.proxies = proxies
                except Exception as e:
                    print(f"No such Proxy: {e}")
                    self.proxies = None

            self.session = self._create_session()
            return self._post(path, **kwargs)
   
        time.sleep(1)
        return self._post(path, **kwargs)
        raise TgtgAPIError(response.status_code, response.content)

    def _get_user_agent(self) -> str:
        if self.fixed_user_agent:
            return self.fixed_user_agent
        version = DEFAULT_APK_VERSION
        try:
            version = self.get_latest_apk_version()
        except Exception:
            log.warning("Failed to get latest APK version!")
        log.debug("Using APK version %s.", version)
        return random.choice(USER_AGENTS).format(version)

    @staticmethod
    def get_latest_apk_version() -> str:
        """Returns latest APK version of the official Android TGTG App.

        Returns:
            str: APK Version string
        """
        response = requests.get(
            "https://play.google.com/store/apps/details?id=com.app.tgtg&hl=en&gl=US",
            timeout=30,
        )
        match = APK_RE_SCRIPT.search(response.text)
        if not match:
            raise TgtgAPIError("Failed to get latest APK version from Google Play Store.")
        data = json.loads(match.group(1))
        return data[1][2][140][0][0][0]

    @property
    def _already_logged(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    def _refresh_token(self) -> None:
        if (
            self.last_time_token_refreshed
            and (datetime.now() - self.last_time_token_refreshed).seconds <= self.access_token_lifetime
        ):
            return
        try:
            response = self._post(REFRESH_ENDPOINT, json={"refresh_token": self.refresh_token})
            self.access_token = response.json().get("access_token")
            self.refresh_token = response.json().get("refresh_token")
            self.last_time_token_refreshed = datetime.now()
        except Exception as e:
            print(f"bad response: {e}")
            return

    def login(self) -> None:
        if not (self.email or self.access_token and self.refresh_token):
            raise TGTGConfigurationError("You must provide at least email or access_token and refresh_token")
        if self._already_logged:
            self._refresh_token()
        else:
            log.info("Starting login process ...")
            response = self._post(
                AUTH_BY_EMAIL_ENDPOINT,
                json={
                    "device_type": self.device_type,
                    "email": self.email,
                },
            )
            first_login_response = response.json()
            if first_login_response["state"] == "TERMS":
                raise TgtgPollingError(
                    f"This email {self.email} is not linked to a tgtg account. Please signup with this email first."
                )
            if first_login_response.get("state") == "WAIT":
                self.start_polling(first_login_response.get("polling_id"))
            else:
                raise TgtgLoginError(response.status_code, response.content)

    def start_polling(self, polling_id) -> None:
        for _ in range(self.max_polling_tries):
            response = self._post(
                AUTH_POLLING_ENDPOINT,
                json={
                    "device_type": self.device_type,
                    "email": self.email,
                    "request_polling_id": polling_id,
                },
            )
            if response.status_code == HTTPStatus.ACCEPTED:
                log.warning(
                    "Check your mailbox on PC to continue... (Mailbox on mobile won't work, if you have installed tgtg app.)"
                )
                time.sleep(self.polling_wait_time)
                continue
            if response.status_code == HTTPStatus.OK:
                log.info("Logged in!")
                login_response = response.json()
                self.access_token = login_response.get("access_token")
                self.refresh_token = login_response.get("refresh_token")
                self.last_time_token_refreshed = datetime.now()
                return
        raise TgtgPollingError("Max polling retries reached. Try again.")

    def get_items(
        self,
        *,
        latitude=0.0,
        longitude=0.0,
        radius=21,
        page_size=20,
        page=1,
        discover=False,
        favorites_only=True,
        item_categories=None,
        diet_categories=None,
        pickup_earliest=None,
        pickup_latest=None,
        search_phrase=None,
        with_stock_only=False,
        hidden_only=False,
        we_care_only=False,
    ) -> List[dict]:
        self.login()
        # fields are sorted like in the app
        data = {
            "origin": {"latitude": latitude, "longitude": longitude},
            "radius": radius,
            "page_size": page_size,
            "page": page,
            "discover": discover,
            "favorites_only": favorites_only,
            "item_categories": item_categories if item_categories else [],
            "diet_categories": diet_categories if diet_categories else [],
            "pickup_earliest": pickup_earliest,
            "pickup_latest": pickup_latest,
            "search_phrase": search_phrase if search_phrase else None,
            "with_stock_only": with_stock_only,
            "hidden_only": hidden_only,
            "we_care_only": we_care_only,
        }
        response = self._post(API_ITEM_ENDPOINT, json=data)
        return response.json().get("items", [])

    def get_item(self, item_id: str) -> dict:
        self.login()
        response = self._post(
            f"{API_ITEM_ENDPOINT}/{item_id}",
            json={"origin": None},
        )
        return response.json()

    def get_favorites(self) -> List[dict]:
        """Returns favorites of the current tgtg account

        Returns:
            List: List of items
        """
        items = []
        page = 1
        page_size = 100
        while True:
            new_items = self.get_items(favorites_only=True, page_size=page_size, page=page)
            items += new_items
            if len(new_items) < page_size:
                break
            page += 1
        return items

    def set_favorite(self, item_id: str, is_favorite: bool) -> None:
        self.login()
        self._post(
            FAVORITE_ITEM_ENDPOINT.format(item_id),
            json={"is_favorite": is_favorite},
        )

    def create_order(self, item_id: str, item_count: int) -> dict[str, str]:
        self.login()
        response = self._post(f"{CREATE_ORDER_ENDPOINT}/{item_id}", json={"item_count": item_count})
        if response.json().get("state") != "SUCCESS":
            raise TgtgAPIError(response.status_code, response.content)
        return response.json().get("order", {})

    def pay_order(self, order_id: str) -> str:
        self.login()
        log.warning("paying %s", order_id)
        response = self._post(ORDER_PAY_ENDPOINT.format(order_id), json={"authorization":{"authorization_payload":{"save_payment_method":False,"payment_type":"PAYPAL","type":"adyenAuthorizationPayload","payload":"{\"configuration\":{\"merchantId\":\"<retracted>\",\"intent\":\"authorize\"},\"name\":\"PayPal\",\"type\":\"paypal\"}"},"payment_provider":"ADYEN","return_url":"adyencheckout://com.app.tgtg.itemview"}})
        log.warning("pay res %s", response.json())
        payment_id = response.json().get("payment_id")
        time.sleep(1)
        for _ in range(32):
            response = self._post(f"{PAYMENT_ENDPOINT}/{payment_id}")
            log.warning("payment res %s", response.json())
            if response.json().get("payload") != "":
                url_pattern = re.compile(r'"(https?://\S+)"')
                url = url_pattern.findall(response.json().get("payload"))[0]
                if url != "":
                    log.warning("open url for payment %s", url)
                    webbrowser.open(url)
                    return url

        return ""

    def get_order_status(self, order_id: str) -> dict[str, str]:
        self.login()
        response = self._post(ORDER_STATUS_ENDPOINT.format(order_id))
        return response.json()

    def abort_order(self, order_id: str) -> None:
        """Use this when your order is not yet paid"""
        self.login()
        response = self._post(ABORT_ORDER_ENDPOINT.format(order_id), json={"cancel_reason_id": 1})
        if response.json().get("state") != "SUCCESS":
            raise TgtgAPIError(response.status_code, response.content)

    def get_manufactureritems(self) -> dict:
        self.login()
        response = self._post(
            MANUFACTURERITEM_ENDPOINT,
            json={
                "action_types_accepted": ["QUERY"],
                "display_types_accepted": ["LIST", "FILL"],
                "element_types_accepted": [
                    "ITEM",
                    "HIGHLIGHTED_ITEM",
                    "MANUFACTURER_STORY_CARD",
                    "DUO_ITEMS",
                    "DUO_ITEMS_V2",
                    "TEXT",
                    "PARCEL_TEXT",
                    "NPS",
                    "SMALL_CARDS_CAROUSEL",
                    "ITEM_CARDS_CAROUSEL",
                ],
            },
        )
        return response.json()
