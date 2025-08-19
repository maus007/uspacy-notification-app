import requests
import json
import time
import threading
import websocket
from datetime import timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Optional, Dict, Any

from config import BASE_URL


class USPACYClient:
    """
    Клієнт для отримання нотифікацій:
    - HTTP: sign_in/refresh, отримання користувачів, списку нотифікацій
    - WebSocket (notifications):
        * URL: wss://sns.uspacy.ua/notifications-websocket/?EIO=4&transport=websocket
        * Відповідаємо на ping "2" -> "3" (pong)
        * НІКОЛИ не шлемо власні "2" — лише відповідаємо
        * Watchdog слідкує за активністю та ініціює реконект при таймауті
        * Повний RAW-лог усіх вхідних/вихідних WS повідомлень
        * Реконект лише коли з’єднання втрачено
    """

    def __init__(self):
        # Auth
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: float = 0.0

        # Users cache
        self.user_cache: Dict[Any, Dict[str, Any]] = {}
        self.my_user_id: Optional[str] = None
        self.user_tz = timezone(timedelta(hours=2))  # UTC+2 за замовчуванням

        # Notifications WS
        self.ws_notif = None
        self.ws_notif_thread: Optional[threading.Thread] = None
        self.notifications_handler = None

        # Notifications WS keepalive & reconnect
        self._ws2_should_run = False
        self._ping2_interval_sec = 25
        self._ping2_timeout_sec = 60
        self._watchdog2_thread = None
        self._last2_rx_ts = 0.0  # час останнього отриманого кадру
        self._reconnect2_lock = threading.Lock()
        self._reconnect2_attempt = 0
        self._max2_backoff = 30

        # Caching
        self._cache_dir = Path.home() / ".uspacy_chat_client"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # WS debug flag
        self.ws_debug = True

    # ---------------- Public API ----------------

    def set_notifications_handler(self, handler):
        """GUI handler з методом handle(event_type, payload)"""
        self.notifications_handler = handler

    def _make_request(self, method, endpoint, data=None, headers=None, params=None):
        url = f"{BASE_URL}/{endpoint}"

        # Оновлення токена, якщо скоро спливе
        if self.access_token and time.time() > self.token_expiry - 60:
            print("Токен спливає, оновлюю...")
            self.refresh_access_token()

        headers = dict(headers or {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            if method == "POST":
                resp = requests.post(url, json=data, headers=headers)
            elif method == "GET":
                resp = requests.get(url, params=params, headers=headers)
            else:
                raise ValueError("Непідтримуваний HTTP метод")

            resp.raise_for_status()
            if resp.content:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    print(f"Помилка парсингу JSON. URL: {url}")
                    print(f"Статус: {resp.status_code} Тіло: {resp.text}")
                    return None
            return None
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Помилка: {e}")
            try:
                print(f"Відповідь сервера: {e.response.json()}")
            except Exception:
                print(f"Відповідь сервера: {getattr(e.response, 'text', '')}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"Помилка запиту: {e}")
            return None

    def sign_in(self, email, password):
        endpoint = "auth/v1/auth/sign_in"
        headers = {"accept": "application/json", "content-type": "application/json"}
        data = {"email": email, "password": password}
        resp = self._make_request("POST", endpoint, data, headers)
        if resp and resp.get("jwt"):
            self.access_token = resp["jwt"]
            self.refresh_token = resp["refreshToken"]
            self.token_expiry = time.time() + resp["expireInSeconds"]
            print("Успішна авторизація!")

            # Витягуємо базову інформацію
            self.get_me()
            self.get_user_settings()
            self.get_all_users()

            # Початкове отримання нотифікацій і відправка у GUI
            try:
                initial_items = self.get_notifications() or []
                if self.notifications_handler and hasattr(self.notifications_handler, "handle"):
                    self.notifications_handler.handle("bootstrapNotifications", initial_items)
            except Exception as e:
                print(f"[AUTH] Не вдалося отримати початкові нотифікації: {e}")

            # Підключення лише до каналу нотифікацій
            self.connect_notifications_websocket()
            return True
        print("Помилка авторизації.")
        return False

    def refresh_access_token(self):
        if not self.refresh_token:
            print("Відсутній refresh token.")
            return False
        endpoint = "auth/v1/auth/refresh_token"
        data = {"refreshToken": self.refresh_token}
        resp = self._make_request("POST", endpoint, data)
        if resp and resp.get("jwt"):
            self.access_token = resp["jwt"]
            self.token_expiry = time.time() + resp["expireInSeconds"]
            print("Токен успішно оновлено!")
            return True
        print("Не вдалося оновити токен.")
        return False

    def get_me(self):
        endpoint = "company/v1/users/me"
        me = self._make_request("GET", endpoint)
        if me:
            self.my_user_id = me.get("id")
            print(f"Мій ID користувача: {self.my_user_id}")
            self.user_cache[self.my_user_id] = {
                "name": f"{me.get('firstName', '')} {me.get('lastName', '')}".strip(),
                "data": me
            }
        return me

    def get_user_settings(self):
        endpoint = "company/v1/users/me/settings/"
        data = self._make_request("GET", endpoint)
        tzname = ""
        if isinstance(data, dict):
            tzname = (data.get("timezone") or "").strip()
        if tzname:
            try:
                self.user_tz = ZoneInfo(tzname)
            except ZoneInfoNotFoundError:
                print(f"ZoneInfo '{tzname}' не знайдено, використовую UTC+2")
                self.user_tz = timezone(timedelta(hours=2))
        else:
            self.user_tz = timezone(timedelta(hours=2))
        return {"timezone": tzname or "UTC+2"}

    def get_all_users(self):
        """
        Кешуємо користувачів під кількома ключами:
        - authUserId (int і str)
        - id (int і str)
        Це дозволяє шукати як за id з company/v1/users, так і за authUserId.
        """
        endpoint = "company/v1/users"
        params = {"list": "all"}
        users = self._make_request("GET", endpoint, params=params)
        if users and isinstance(users, list):
            self.user_cache = {}
            for u in users:
                auth_uid = u.get("authUserId")
                comp_uid = u.get("id")
                entry = {
                    "name": f"{u.get('firstName', '')} {u.get('lastName', '')}".strip(),
                    "data": u
                }
                for key in (
                    auth_uid,
                    str(auth_uid) if auth_uid is not None else None,
                    comp_uid,
                    str(comp_uid) if comp_uid is not None else None,
                ):
                    if key is not None:
                        self.user_cache[key] = entry
            print(f"[USERS] Закешовано записів: {len(self.user_cache)} (ключі: authUserId/id як int і str)")
        return self.user_cache

    def get_user_info(self, user_id):
        """
        Повертає дані користувача з кешу за user_id (int або str).
        user_id у нотифікаціях частіше відповідає полю 'id' у company/v1/users.
        """
        if user_id is None:
            return None
        return self.user_cache.get(user_id) or self.user_cache.get(str(user_id))

    # ---------- RAW WS send helper ----------
    def _send_ws(self, ws, data: str, channel: str):
        """Відправити фрейм у WS з raw-логом."""
        try:
            print(f"[WS OUT RAW {channel}] {data}")
        except Exception:
            pass
        return ws.send(data)

    # ---------------- Notifications WebSocket ----------------

    def connect_notifications_websocket(self):
        """Підключення до єдиного каналу нотифікацій."""
        if self.ws_notif and getattr(self.ws_notif, "sock", None) and self.ws_notif.sock.connected:
            print("Notifications WS вже підключено — пропускаю connect_notifications_websocket()")
            return

        if self.ws_notif:
            try:
                self.ws_notif.close()
            except Exception:
                pass

        url = "wss://sns.uspacy.ua/notifications-websocket/?EIO=4&transport=websocket"

        self.ws_notif = websocket.WebSocketApp(
            url,
            on_open=self.on_ws2_open,
            on_message=self.on_ws2_message,
            on_error=self.on_ws2_error,
            on_close=self.on_ws2_close,
        )

        self._ws2_should_run = True
        self._reconnect2_attempt = 0
        self.ws_notif_thread = threading.Thread(
            target=self.ws_notif.run_forever, kwargs={"ping_interval": None, "ping_timeout": None}, daemon=True
        )
        self.ws_notif_thread.start()

    def shutdown_notifications(self, join_timeout: float = 2.0):
        """Акуратне завершення Notifications WS."""
        try:
            self._ws2_should_run = False
        except Exception:
            pass
        try:
            if self.ws_notif:
                self.ws_notif.close()
        except Exception:
            pass
        try:
            if getattr(self, "_watchdog2_thread", None) and self._watchdog2_thread.is_alive():
                self._watchdog2_thread.join(timeout=join_timeout)
        except Exception:
            pass
        try:
            if self.ws_notif_thread and self.ws_notif_thread.is_alive():
                self.ws_notif_thread.join(timeout=join_timeout)
        except Exception:
            pass
        self.ws_notif = None
        self.ws_notif_thread = None
        self._watchdog2_thread = None

    def _start_watchdog2(self):
        """
        Watchdog НІЧОГО не шле, тільки відслідковує активність.
        Якщо немає кадрів довше self._ping2_timeout_sec — закриваємо сокет (on_close зробить реконект).
        """
        def loop():
            while self._ws2_should_run and self.ws_notif and getattr(self.ws_notif, "sock", None) and self.ws_notif.sock.connected:
                try:
                    time.sleep(1.0)
                    if self._last2_rx_ts <= 0:
                        continue
                    idle = time.time() - self._last2_rx_ts
                    if idle > self._ping2_timeout_sec:
                        print(f"[NOTIF WS] Watchdog idle={int(idle)}s > timeout={self._ping2_timeout_sec}s — закриваю сокет")
                        try:
                            self.ws_notif.close()
                        except Exception:
                            pass
                        return
                except Exception as e:
                    print(f"[NOTIF WS] Watchdog error: {e}")
                    return

        if self._watchdog2_thread and self._watchdog2_thread.is_alive():
            return
        self._watchdog2_thread = threading.Thread(target=loop, daemon=True)
        self._watchdog2_thread.start()

    def _stop_watchdog2(self):
        self._ws2_should_run = False

    def _schedule2_reconnect(self, immediate: bool = False):
        """Реконект лише коли зʼєднання втрачено. Якщо immediate=True — перша спроба без затримки."""
        if self.ws_notif and getattr(self.ws_notif, "sock", None) and self.ws_notif.sock.connected:
            print("Notifications WS все ще підключений — реконект не потрібен")
            return

        with self._reconnect2_lock:
            if self._ws2_should_run:
                return
            self._ws2_should_run = True

        def do_reconnect():
            # Без затримки для першої спроби або коли явно запитали immediate
            if immediate or self._reconnect2_attempt == 0:
                delay = 0
            else:
                delay = min(self._max2_backoff, 2 ** max(0, self._reconnect2_attempt))
            print(f"[NOTIF WS] Спроба реконекту через {delay} сек...")
            if delay > 0:
                time.sleep(delay)

            if self.ws_notif and getattr(self.ws_notif, "sock", None) and self.ws_notif.sock.connected:
                print("[NOTIF WS] Уже відновлено — скасовую реконект")
                self._ws2_should_run = False
                self._reconnect2_attempt = 0
                return

            if self.access_token and time.time() > (self.token_expiry - 60):
                self.refresh_access_token()

            self._reconnect2_attempt += 1
            try:
                self.connect_notifications_websocket()
            except Exception as e:
                print(f"[NOTIF WS] Помилка реконекту: {e}")
                self._ws2_should_run = False
                # Наступні спроби — із бекофом
                self._schedule2_reconnect(immediate=False)

        threading.Thread(target=do_reconnect, daemon=True).start()

    def on_ws2_open(self, ws):
        print("Notifications WebSocket відкрито.")
        # стартуємо watchdog
        self._last2_rx_ts = time.time()
        self._start_watchdog2()

    def on_ws2_error(self, ws, error):
        # Лише логуємо. Реконект робимо тільки в on_ws2_close
        try:
            print(f"Notifications WS помилка: {error}")
        except Exception:
            pass

    def on_ws2_close(self, ws, close_status_code, close_msg):
        print(f"Notifications WS закрито. Статус: {close_status_code}, повідомлення: {close_msg}")
        self._stop_watchdog2()
        if not getattr(self, "_ws2_should_run", False):
            return
        self._ws2_should_run = False
        # Миттєва спроба реконекту одразу після закриття
        self._schedule2_reconnect(immediate=True)

    def on_ws2_message(self, ws, message: str):
        """Обробка Engine.IO/Socket.IO фреймів каналу нотифікацій."""
        # Логуємо ВСІ вхідні raw і позначаємо активність
        try:
            print(f"[WS IN RAW NOTIF] {message}")
        except Exception:
            pass
        self._last2_rx_ts = time.time()

        try:
            if message.startswith("0"):
                # Engine.IO handshake
                try:
                    info = json.loads(message[1:])
                    self._ping2_interval_sec = max(5, int(info.get("pingInterval", 25000)) // 1000)
                    self._ping2_timeout_sec = max(10, int(info.get("pingTimeout", 60000)) // 1000)
                    print(f"Notifications handshake OK: pingInterval={self._ping2_interval_sec}s pingTimeout={self._ping2_timeout_sec}s")
                except Exception as e:
                    print(f"Не вдалося розпарсити notifications handshake: {e}")
                    self._ping2_interval_sec = 25
                    self._ping2_timeout_sec = 60

                # Socket.IO auth
                if self.access_token:
                    auth_message = f'40{{"token":"{self.access_token}"}}'
                    self._send_ws(ws, auth_message, "NOTIF")

                # Watchdog вже запущений в on_open
                self._reconnect2_attempt = 0
                return

            # Engine.IO ping -> відповідаємо "3" (pong)
            if message.startswith("2"):
                try:
                    self._send_ws(ws, "3", "NOTIF")
                except Exception as e:
                    print(f"Помилка відправки notifications pong: {e}")
                return

            # Engine.IO pong ack
            if message == "3":
                return

            if message.startswith("40"):
                # Socket.IO namespace connected
                return

            if message.startswith("41"):
                # Socket.IO namespace closed
                return

            if message.startswith("42"):
                # Socket.IO event (наприклад, ["pushNotification", {...}])
                try:
                    event_data = json.loads(message[2:])
                    event_type = event_data[0]
                    payload = event_data[1]
                    # Прокидуємо у GUI-обробник
                    if self.notifications_handler:
                        try:
                            self.notifications_handler.handle(event_type, payload)
                        except Exception as e:
                            print(f"Помилка обробки нотифікації: {e}")
                except json.JSONDecodeError as e:
                    print(f"Помилка парсингу notifications payload: {e}")
                return

        except Exception as e:
            print(f"Помилка в on_ws2_message: {e}")

    # ====== Helpers for WS logging ======
    def _log_ws(self, direction: str, event: str = "", payload=None):
        try:
            tag = {"IN": "<--", "OUT": "-->", "STATE": "***"}.get(direction, direction)
            if payload is None:
                print(f"[WS {tag}] {event}")
                return
            compact = self._compact_json(payload)
            print(f"[WS {tag}] {event}: {compact}")
        except Exception:
            try:
                print(f"[WS {direction}] {event}: {payload}")
            except Exception:
                pass

    def _compact_json(self, obj):
        try:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            return str(obj)

    # ---------------- Notifications HTTP ----------------
    def get_notifications(self, params=None):
        """
        Отримати всі нотифікації для поточного користувача.
        Повертає список словників як у відповіді бекенда.
        """
        endpoint = "notifications/v1/notifications"
        return self._make_request("GET", endpoint, params=params)