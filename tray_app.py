import json
import re
import time
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Deque, List, Tuple, Optional
from collections import deque
from html import unescape

from PySide6 import QtWidgets, QtGui, QtCore, QtMultimedia

from api import USPACYClient
from settings import NotifierSettings


@dataclass
class AppMessage:
    title: str
    text: str
    timestamp: float
    author_user_id: int



class SimpleToast(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setObjectName("SimpleToast")
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)

        cont = QtWidgets.QFrame(self)
        cont.setObjectName("ToastCard")
        lay = QtWidgets.QHBoxLayout(cont)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        self.lbl_icon = QtWidgets.QLabel()
        self.lbl_icon.setFixedSize(32, 32)
        self.lbl_icon.setScaledContents(True)
        lay.addWidget(self.lbl_icon)

        text_box = QtWidgets.QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(4)
        self.lbl_title = QtWidgets.QLabel()
        self.lbl_title.setStyleSheet("font-weight:600;color:#111;")
        self.lbl_title.setWordWrap(True)
        self.lbl_body = QtWidgets.QLabel()
        self.lbl_body.setStyleSheet("color:#222;")
        self.lbl_body.setWordWrap(True)
        text_box.addWidget(self.lbl_title)
        text_box.addWidget(self.lbl_body)
        lay.addLayout(text_box, 1)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(cont)

        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setColor(QtGui.QColor(0, 0, 0, 90))
        shadow.setOffset(0, 6)
        cont.setGraphicsEffect(shadow)

        self.setStyleSheet("""
        #SimpleToast { background: transparent; }
        #ToastCard {
          background: #FFFFFF;
          border-radius: 12px;
          border: 1px solid rgba(0,0,0,0.06);
        }
        """)

    def show_for(self, title: str, body: str, msec: int, anchor_pos: QtCore.QPoint, icon_pm: Optional[QtGui.QPixmap]):
        self.lbl_title.setText(title or "Notification")
        self.lbl_body.setText(body or "")
        if icon_pm and not icon_pm.isNull():
            self.lbl_icon.setPixmap(icon_pm.scaled(32, 32, QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation))
        else:
            self.lbl_icon.clear()

        self.adjustSize()
        rect = self.frameGeometry()
        x = max(8, anchor_pos.x() - rect.width() - 8)
        y = max(8, anchor_pos.y() + 8)
        self.move(x, y)
        self.show()
        self.raise_()
        self._timer.start(max(1500, msec))


class NotificationsPopup(QtWidgets.QFrame):
    request_open_detail = QtCore.Signal(dict)
    request_mark_read = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self.setObjectName("NotifPopup")

        self.user_lookup = None  # type: Optional[callable]
        self._avatar_cache: dict[str, QtGui.QPixmap] = {}
        self._avatars_dir = Path("cache") / "avatars"
        try:
            self._avatars_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        self.AVATAR_SIZE = 36
        self.INDENT_LEFT = self.AVATAR_SIZE + 10
        self.TOP_BOTTOM_SPACING = 12

        self.container = QtWidgets.QFrame(self)
        self.container.setObjectName("Container")
        self.container.setContentsMargins(0, 0, 0, 0)
        self.vbox = QtWidgets.QVBoxLayout(self.container)
        self.vbox.setContentsMargins(12, 12, 12, 12)
        self.vbox.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Notifications")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.btn_settings = QtWidgets.QToolButton()
        self.btn_settings.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
        self.btn_settings.setAutoRaise(True)
        header.addWidget(self.btn_settings)
        self.vbox.addLayout(header)

        chips = QtWidgets.QHBoxLayout()
        self.btn_all = QtWidgets.QPushButton("All notifications")
        self.btn_unread = QtWidgets.QPushButton("Unread")
        self.badge_unread = QtWidgets.QLabel("0")
        self.badge_unread.setObjectName("Badge")

        unread_wrap = QtWidgets.QWidget()
        unread_wrap.setLayout(QtWidgets.QHBoxLayout())
        unread_wrap.layout().setContentsMargins(0, 0, 0, 0)
        unread_wrap.layout().setSpacing(6)
        unread_wrap.layout().addWidget(self.btn_unread)
        unread_wrap.layout().addWidget(self.badge_unread)

        self.btn_mentions = QtWidgets.QPushButton("@ Mentions")
        self.badge_mentions = QtWidgets.QLabel("0")
        self.badge_mentions.setObjectName("Badge")

        mentions_wrap = QtWidgets.QWidget()
        mentions_wrap.setLayout(QtWidgets.QHBoxLayout())
        mentions_wrap.layout().setContentsMargins(0, 0, 0, 0)
        mentions_wrap.layout().setSpacing(6)
        mentions_wrap.layout().addWidget(self.btn_mentions)
        mentions_wrap.layout().addWidget(self.badge_mentions)


        for b in (self.btn_all, self.btn_unread, self.btn_mentions):
            b.setCheckable(True)
        self.btn_unread.setChecked(True)

        chips.addWidget(self.btn_all)
        chips.addWidget(unread_wrap)
        chips.addWidget(mentions_wrap)
        chips.addStretch(1)
        self.vbox.addLayout(chips)

        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("List")
        self.list.itemClicked.connect(self._emit_detail)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.vbox.addWidget(self.list)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.container)

        self.setStyleSheet("""
        #NotifPopup { background:#FFFFFF; border-radius:12px; border:none; }
        #Container { background:#FFFFFF; border-radius:12px; border:none; }
        #Title { font-weight:600; font-size:16px; color:#222; }
        QPushButton { border:none; background:#F4F5F7; border-radius:16px; padding:6px 12px; color:#444; }
        QPushButton:checked { background:#ECECFF; color:#3F51B5; }
        #Badge { background:#6A5AE0; color:white; border-radius:10px; padding:2px 6px; font-weight:600; min-width:14px; qproperty-alignment:'AlignCenter'; }
        #List { border:none; background:#FFFFFF; }
        QListWidget::item { margin:6px; border:none; }
        """)

        self.list.viewport().setAutoFillBackground(True)
        pal = self.list.viewport().palette()
        pal.setColor(self.list.viewport().backgroundRole(), QtGui.QColor("#FFFFFF"))
        self.list.viewport().setPalette(pal)

        self.btn_all.clicked.connect(lambda: self._switch_tab("all"))
        self.btn_unread.clicked.connect(lambda: self._switch_tab("unread"))
        self.btn_mentions.clicked.connect(lambda: self._switch_tab("mentions"))

        self._all_items: List[dict] = []
        self._current_tab = "unread"
        self._my_user_id: Optional[str] = None

        self.resize(400, 560)

    def get_avatar_pixmap(self, user_id: Optional[object], size: int = 32) -> QtGui.QPixmap:
        pm = self._avatar_pixmap_for(user_id)
        if pm.isNull():
            return pm
        return pm.scaled(size, size, QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation)

    def get_avatar_icon(self, user_id: Optional[object], size: int = 32) -> QtGui.QIcon:
        pm = self.get_avatar_pixmap(user_id, size=size)
        return QtGui.QIcon(pm)

    def _emit_detail(self, item: QtWidgets.QListWidgetItem):
        data = item.data(QtCore.Qt.UserRole) or {}
        try:
            self.request_mark_read.emit(data)
        except Exception:
            pass
        url = self._build_task_url(data)
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        else:
            self.request_open_detail.emit(data)

    def _switch_tab(self, name: str):
        self._current_tab = name
        self.btn_all.setChecked(name == "all")
        self.btn_unread.setChecked(name == "unread")
        self.btn_mentions.setChecked(name == "mentions")
        self._render()

    def _build_task_url(self, n: dict) -> Optional[str]:
        try:
            data = n.get("data") or {}
            entity = data.get("entity") or {}
            ntype = (n.get("type") or "").strip()
            base = "https://team.uspacy.ua/tasks/"
            if ntype == "comment":
                task_id = entity.get("entity_id") or entity.get("entityId")
                if task_id:
                    return f"{base}{task_id}"
            task_id = entity.get("id")
            if task_id:
                return f"{base}{task_id}"
        except Exception:
            pass
        return None

    def _debug_avatar(self, user_id, user, url, cache_hit: bool, used_fallback: bool, disk: bool = False, path: Optional[Path] = None):
        try:
            pstr = f"{path}" if path else "-"
            print(f"[AVATAR] user_id={user_id} cache_hit={cache_hit} disk={disk} path={pstr} "
                  f"user_found={'yes' if user else 'no'} url={'-' if not url else url} "
                  f"fallback={'yes' if used_fallback else 'no'}")
        except Exception:
            pass

    def _circle_pixmap(self, src_pm: QtGui.QPixmap, size: int) -> QtGui.QPixmap:
        dst = QtGui.QPixmap(size, size)
        dst.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(dst)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        path = QtGui.QPainterPath()
        path.addEllipse(0, 0, size, size)
        p.setClipPath(path)
        p.drawPixmap(0, 0, src_pm.scaled(size, size, QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation))
        p.end()
        return dst

    def _avatar_disk_path(self, user_id, url: Optional[str]) -> Path:
        if url:
            key = hashlib.sha1(url.encode("utf-8")).hexdigest()
            fname = f"{key}.png"
        else:
            fname = f"user_{str(user_id)}.png"
        return self._avatars_dir / fname

    def _avatar_pixmap_for(self, user_id) -> QtGui.QPixmap:
        key = str(user_id) if user_id is not None else ""
        if key in self._avatar_cache:
            self._debug_avatar(user_id, None, None, cache_hit=True, used_fallback=False)
            return self._avatar_cache[key]

        user = None
        if self.user_lookup:
            try:
                user = self.user_lookup(user_id)
            except Exception:
                user = None

        url = None
        try:
            d = (user or {}).get("data") or {}
            avatar_val = d.get("avatar") or (user or {}).get("avatar")
            if isinstance(avatar_val, str) and avatar_val.startswith("http"):
                url = avatar_val
        except Exception:
            pass
        if not url:
            for k in ("avatarUrl", "photoUrl", "imageUrl", "photo"):
                try:
                    val = ((user or {}).get("data") or {}).get(k) or (user or {}).get(k)
                    if isinstance(val, str) and val.startswith("http"):
                        url = val
                        break
                except Exception:
                    pass

        disk_path = self._avatar_disk_path(user_id, url)
        if disk_path.exists():
            pm = QtGui.QPixmap()
            if pm.load(str(disk_path)):
                self._avatar_cache[key] = pm
                self._debug_avatar(user_id, user, url, cache_hit=False, used_fallback=False, disk=True, path=disk_path)
                return pm

        if url:
            try:
                import requests
                r = requests.get(url, timeout=5)
                if r.ok:
                    pm = QtGui.QPixmap()
                    if pm.loadFromData(r.content):
                        pm2 = self._circle_pixmap(pm, self.AVATAR_SIZE)
                        try:
                            pm2.save(str(disk_path), "PNG")
                        except Exception as e:
                            print(f"[AVATAR] save to disk failed: {e}")
                        self._avatar_cache[key] = pm2
                        self._debug_avatar(user_id, user, url, cache_hit=False, used_fallback=False, disk=False)
                        return pm2
            except Exception as e:
                print(f"[AVATAR] завантаження помилка для user_id={user_id}: {e}")

        first = last = ""
        try:
            d = (user or {}).get("data") or {}
            first = str(d.get("firstName") or "")[:1]
            last = str(d.get("lastName") or "")[:1]
        except Exception:
            pass
        initials = (first + last).upper() or (str(user_id)[:1].upper() if user_id else "U")
        pm_base = QtGui.QPixmap(self.AVATAR_SIZE, self.AVATAR_SIZE)
        pm_base.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm_base)
        h = hash(str(user_id)) % 360
        color = QtGui.QColor.fromHsv(h, 140, 220)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setBrush(color)
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(0, 0, self.AVATAR_SIZE, self.AVATAR_SIZE)
        p.setPen(QtCore.Qt.white)
        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(12)
        p.setFont(font)
        p.drawText(pm_base.rect(), QtCore.Qt.AlignCenter, initials)
        p.end()
        self._avatar_cache[key] = pm_base
        try:
            fb_path = self._avatar_disk_path(user_id, None)
            pm_base.save(str(fb_path), "PNG")
        except Exception:
            pass
        self._debug_avatar(user_id, user, url, cache_hit=False, used_fallback=True, disk=False)
        return pm_base

    def update_data(self, items: List[dict], my_user_id: Optional[str]):
        try:
            print(f"[NotificationsPopup] update_data: {len(items or [])} items")
        except Exception:
            pass
        self._all_items = items or []
        self._my_user_id = my_user_id

        # Лічильники
        unread_count = sum(1 for n in (items or []) if not bool(n.get("read")))
        self.badge_unread.setText(str(unread_count))
        self.badge_unread.setVisible(unread_count > 0)

        # Непрочитані згадки (@mentions) для мого user_id
        def _has_mention(n: dict, uid: Optional[str]) -> bool:
            try:
                if n.get("mentioned_me") is True and uid is not None:
                    return True
            except Exception:
                pass
            try:
                users = ((((n.get("data") or {}).get("entity") or {}).get("mentioned") or {}).get("users") or [])
                return uid is not None and str(uid) in {str(u) for u in users}
            except Exception:
                return False

        mentions_unread = sum(1 for n in (items or []) if (not bool(n.get("read")) and _has_mention(n, my_user_id)))
        self.badge_mentions.setText(str(mentions_unread))
        self.badge_mentions.setVisible(mentions_unread > 0)

        self._render()


    def _render(self):
        self.list.clear()
        if not self._all_items:
            empty = QtWidgets.QListWidgetItem("Немає нотифікацій для відображення")
            font = empty.font()
            font.setItalic(True)
            empty.setFont(font)
            self.list.addItem(empty)
            return

        def to_ts(n: dict) -> int:
            ts = (((n or {}).get("data") or {}).get("timestamp") or "")
            if isinstance(ts, str) and ts:
                try:
                    ts2 = ts.replace("Z", "+00:00")
                    dt = QtCore.QDateTime.fromString(ts2, QtCore.Qt.ISODateWithMs)
                    if dt.isValid():
                        return dt.toMSecsSinceEpoch()
                except Exception:
                    pass
            return int(n.get("createdAt") or 0)

        def has_mention(n: dict, uid: Optional[str]) -> bool:
            try:
                if n.get("mentioned_me") is True and uid is not None:
                    return True
            except Exception:
                pass
            try:
                users = ((((n.get("data") or {}).get("entity") or {}).get("mentioned") or {}).get("users") or [])
                return uid is not None and str(uid) in {str(u) for u in users}
            except Exception:
                return False

        def strip_html(text: str) -> str:
            if not text:
                return ""
            s = re.sub(r"<[^>]+>", " ", text)
            s = unescape(s)
            return " ".join(s.split())

        def card_title(n: dict) -> str:
            ntype = (n.get("type") or "").strip()
            if ntype == "comment":
                parent_type = (((n.get("data") or {}).get("root_parent") or {}).get("type") or "").strip()
                if parent_type == "task":
                    return "You were mentioned in the task" if has_mention(n, self._my_user_id) else "A new comment was added to the task"
                return f"A new comment was added to {parent_type or 'entity'}"
            action = (((n.get("data") or {}).get("action")) or "").strip()
            if ntype == "task":
                return "The task has been assigned" if action == "create" else "The task has been changed"
            return "Notification"

        def card_subtitle(n: dict) -> str:
            data = n.get("data") or {}
            root = data.get("root_parent") or {}
            if isinstance(root, dict):
                rt = ((root.get("data") or {}).get("title")) or ""
                if rt:
                    return str(rt)
            entity = data.get("entity") or {}
            return str(entity.get("title", "") or "")

        def one_line_elide(text: str, max_chars: int) -> str:
            text = " ".join((text or "").split())
            return text if len(text) <= max_chars else (text[:max_chars - 1] + "…")

        def card_message(n: dict) -> str:
            entity = ((n.get("data") or {}).get("entity") or {})
            raw = str(entity.get("message", "") or "")
            raw = strip_html(raw)
            return one_line_elide(raw, 140)

        def card_time(n: dict) -> str:
            ms = to_ts(n)
            dt = QtCore.QDateTime.fromMSecsSinceEpoch(ms)
            now = QtCore.QDateTime.currentDateTime()
            if dt.date() == now.date():
                return dt.toString("HH:mm")
            return dt.toString("d MMMM, HH:mm")

        items_sorted = sorted(self._all_items, key=to_ts, reverse=True)
        for n in items_sorted:
            if self._current_tab == "unread" and bool(n.get("read")):
                continue
            if self._current_tab == "mentions" and not has_mention(n, self._my_user_id):
                continue

            card = QtWidgets.QFrame()
            card.setObjectName("Bubble")
            card.setStyleSheet("QFrame#Bubble { background:#FFFFFF; border:none; border-radius:16px; }")
            shadow = QtWidgets.QGraphicsDropShadowEffect(card)
            shadow.setBlurRadius(18)
            shadow.setColor(QtGui.QColor(0, 0, 0, 28))
            shadow.setOffset(0, 4)
            card.setGraphicsEffect(shadow)

            lay = QtWidgets.QVBoxLayout(card)
            lay.setContentsMargins(12, 12, 12, 12)
            lay.setSpacing(self.TOP_BOTTOM_SPACING)

            top = QtWidgets.QHBoxLayout()
            top.setSpacing(10)
            top.setContentsMargins(0, 0, 0, 0)

            avatar = QtWidgets.QLabel()
            avatar.setFixedSize(self.AVATAR_SIZE, self.AVATAR_SIZE)
            try:
                user_id = ((n.get("data") or {}).get("user_id"))
                pm = self._avatar_pixmap_for(user_id)
                avatar.setPixmap(pm)
            except Exception:
                avatar.setPixmap(self._avatar_pixmap_for(None))
            top.addWidget(avatar)

            title_lbl = QtWidgets.QLabel(f"{card_title(n)}")
            title_lbl.setStyleSheet("font-weight:600;color:#222;")
            title_lbl.setWordWrap(True)
            title_lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
            top.addWidget(title_lbl, 1)

            dot = QtWidgets.QLabel(" ")
            dot.setFixedSize(10, 10)
            dot.setStyleSheet("background:#6A5AE0;border-radius:5px;")
            if bool(n.get("read")):
                dot.setStyleSheet("background:#D1D5DB;border-radius:5px;")
            top.addWidget(dot)

            time_lbl = QtWidgets.QLabel(card_time(n))
            time_lbl.setStyleSheet("color:#666;")
            top.addSpacing(6)
            top.addWidget(time_lbl)

            lay.addLayout(top)

            st = card_subtitle(n)
            if st:
                sub = QtWidgets.QLabel(st)
                sub.setStyleSheet("color:#616161;")
                sub.setWordWrap(True)
                sub.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                row_sub = QtWidgets.QHBoxLayout()
                row_sub.setContentsMargins(self.INDENT_LEFT, 0, 0, 0)
                row_sub.addWidget(sub)
                lay.addLayout(row_sub)

            msg = card_message(n)
            if msg:
                msg_lbl = QtWidgets.QLabel(msg)
                msg_lbl.setWordWrap(True)
                msg_lbl.setStyleSheet("""
                    background:#E8F0FF;
                    border:none;
                    border-radius:10px;
                    padding:8px 10px;
                    color:#1E293B;
                """)
                msg_lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

                row_msg = QtWidgets.QHBoxLayout()
                row_msg.setContentsMargins(self.INDENT_LEFT, 0, 0, 0)
                row_msg.addWidget(msg_lbl)
                lay.addLayout(row_msg)

            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(card.sizeHint())
            item.setData(QtCore.Qt.UserRole, n)
            self.list.addItem(item)
            self.list.setItemWidget(item, card)


class TrayNotifierApp(QtWidgets.QApplication):
    message_received = QtCore.Signal(object)
    ws_event = QtCore.Signal(str, object)

    def __init__(self, argv: List[str]):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        QtWidgets.QApplication.setOrganizationName("Uspacy")
        QtWidgets.QApplication.setApplicationName("NotifierApp")

        self._last_messages: Deque[AppMessage] = deque(maxlen=5)
        self._notifications: List[dict] = []
        self._popup: Optional[NotificationsPopup] = None

        self.client = USPACYClient()
        self.client.set_notifications_handler(self)

        self.settings = NotifierSettings()

        self.tray = QtWidgets.QSystemTrayIcon(self)
        self._TRAY_BASE_SIZE = 128
        self._icon_base = self._load_app_icon()
        self.setWindowIcon(self._icon_base)
        self.tray.setIcon(self._compose_tray_icon(0))
        self.tray.setToolTip("Uspacy Notifier")

        self.menu = QtWidgets.QMenu()
        self._messages_section = self.menu.addSection("Останні повідомлення")
        self._messages_actions: List[QtGui.QAction] = []
        self.menu.addSeparator()

        self.action_open_panel = self.menu.addAction("Панель нотифікацій…")
        self.action_open_panel.triggered.connect(self._toggle_popup)

        self.action_show_settings = self.menu.addAction("Налаштування…")
        self.action_show_settings.triggered.connect(self._open_settings_dialog)

        self.menu.addSeparator()
        self.action_quit = self.menu.addAction("Вихід")
        self.action_quit.triggered.connect(self._cleanup_and_quit)

        self.tray.setContextMenu(None)
        self.tray.activated.connect(self._on_tray_activated)

        self._sound = QtMultimedia.QSoundEffect()
        self._sound.setVolume(0.25)
        try:
            self._sound.setSource(QtCore.QUrl.fromLocalFile("sounds/1.wav"))
        except Exception:
            pass

        self.message_received.connect(self._on_message_received)
        self.ws_event.connect(self._handle_event_on_main)

        self.tray.show()
        self.tray.setVisible(True)

        self.aboutToQuit.connect(self._cleanup)

        self._popup_update_timer = QtCore.QTimer(self)
        self._popup_update_timer.setSingleShot(True)
        self._popup_update_timer.setInterval(150)
        self._popup_update_timer.timeout.connect(self._refresh_popup_data)

        self._fallback_toast = SimpleToast()

        self._toast_avatars: dict[str, QtGui.QPixmap] = {}
        self._toast_avatar_dir = Path("cache") / "avatars"
        try:
            self._toast_avatar_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        email, password = self.settings.get_credentials()
        if email and password:
            QtCore.QTimer.singleShot(0, lambda: self._try_sign_in(email, password))

        self._rebuild_last_messages_menu()
        QtCore.QTimer.singleShot(1500, lambda: self._show_tray_toast("Вітаємо", "Застосунок працює в системному треї.", 4000))

    def _load_app_icon(self) -> QtGui.QIcon:
        pm = QtGui.QPixmap("icon.png")
        if pm.isNull():
            size = getattr(self, "_TRAY_BASE_SIZE", 128)
            pm = QtGui.QPixmap(size, size)
            pm.fill(QtGui.QColor("#3F51B5"))
            p = QtGui.QPainter(pm)
            p.setPen(QtCore.Qt.white)
            font = QtGui.QFont("Arial", max(10, int(size * 0.44)), QtGui.QFont.Bold)
            p.setFont(font)
            p.drawText(pm.rect(), QtCore.Qt.AlignCenter, "U")
            p.end()
        return QtGui.QIcon(pm)

    def _compose_tray_icon(self, unread_count: int) -> QtGui.QIcon:
        size = getattr(self, "_TRAY_BASE_SIZE", 128)
        base_pm = self._icon_base.pixmap(size, size)
        try:
            print(f"[TRAY] compose: unread={unread_count} base_null={base_pm.isNull()} size={size}")
        except Exception:
            pass

        if unread_count <= 0 or base_pm.isNull():
            return QtGui.QIcon(base_pm)

        screen = QtWidgets.QApplication.primaryScreen() or (QtWidgets.QApplication.screens()[0] if QtWidgets.QApplication.screens() else None)
        dpr = float(getattr(screen, "devicePixelRatio", lambda: 1.0)()) if screen else 1.0
        canvas = QtGui.QPixmap(int(size * dpr), int(size * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(QtCore.Qt.transparent)

        p = QtGui.QPainter(canvas)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(0, 0, base_pm)

        text = str(unread_count) if unread_count < 100 else "99+"

        diam = int(size * 0.52)
        margin = int(size * 0.06)
        x = size - diam - margin
        y = size - diam - margin
        rect = QtCore.QRectF(x, y, diam, diam)

        shadow_offset = max(2, int(diam * 0.08))
        shadow_rect = QtCore.QRectF(x + shadow_offset, y + shadow_offset, diam, diam)
        p.setBrush(QtGui.QColor(0, 0, 0, 90))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(shadow_rect)

        p.setBrush(QtGui.QColor(34, 197, 94))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(rect)

        font = QtGui.QFont()
        font.setBold(True)
        font_size = max(10, int(diam * (0.60 if len(text) <= 2 else 0.50)))
        font.setPointSize(font_size)
        p.setFont(font)
        p.setPen(QtGui.QColor(255, 255, 255))

        fm = QtGui.QFontMetrics(p.font())
        text_width = fm.horizontalAdvance(text)
        max_width = rect.width() * 0.84
        while text_width > max_width and font_size > 9:
            font_size -= 1
            font.setPointSize(font_size)
            p.setFont(font)
            fm = QtGui.QFontMetrics(p.font())
            text_width = fm.horizontalAdvance(text)

        text_rect = rect.adjusted(0, -rect.height() * 0.05, 0, rect.height() * 0.05)
        p.drawText(text_rect, QtCore.Qt.AlignCenter, text)

        p.end()
        return QtGui.QIcon(canvas)

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason):
        if reason == QtWidgets.QSystemTrayIcon.Context:
            self.menu.popup(QtGui.QCursor.pos())
            return
        if reason in (QtWidgets.QSystemTrayIcon.Trigger, QtWidgets.QSystemTrayIcon.DoubleClick):
            self._toggle_popup()

    def _toggle_popup(self):
        if self._popup and self._popup.isVisible():
            self._popup.hide()
            return
        if self._popup is None:
            self._popup = NotificationsPopup()
            def _lookup(uid):
                if uid is None:
                    return None
                return self.client.get_user_info(uid) or self.client.get_user_info(str(uid))
            self._popup.user_lookup = _lookup
            self._popup.request_open_detail.connect(self._show_notif_detail)
            self._popup.request_mark_read.connect(self._mark_notification_read)
        self._popup.update_data(self._notifications, self.client.my_user_id)
        self._place_popup_near_tray(self._popup)
        self._popup.show()
        self._popup.raise_()
        self._popup.activateWindow()

    def _place_popup_near_tray(self, widget: QtWidgets.QWidget):
        cursor_pos = QtGui.QCursor.pos()
        screen = QtWidgets.QApplication.screenAt(cursor_pos) or QtWidgets.QApplication.primaryScreen()
        geo = screen.availableGeometry()
        w = widget.width()
        h = widget.height()
        x = max(geo.left() + 8, min(cursor_pos.x() - w // 2, geo.right() - w - 8))
        top_bar_zone = cursor_pos.y() - geo.top() < (geo.height() * 0.15)
        if top_bar_zone:
            y = min(geo.bottom() - h - 8, cursor_pos.y() + 8)
        else:
            y = max(geo.top() + 8, cursor_pos.y() - h - 8)
        widget.move(x, y)

    def _refresh_popup_data(self):
        if self._popup and self._popup.isVisible():
            self._popup.update_data(self._notifications, self.client.my_user_id)
        self._update_tray_icon_badge()

    def _show_notif_detail(self, n: dict):
        try:
            pretty = json.dumps(n, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(n)
        dlg = QtWidgets.QMessageBox()
        dlg.setWindowTitle("Деталі нотифікації")
        dlg.setText(pretty)
        dlg.setIcon(QtWidgets.QMessageBox.Information)
        dlg.exec()

    def _mark_notification_read(self, n: dict):
        try:
            created_at = int(n.get("createdAt") or 0)
        except Exception:
            created_at = 0
        if not created_at:
            return
        try:
            for item in self._notifications:
                if int(item.get("createdAt") or 0) == created_at:
                    item["read"] = True
                    break
        except Exception:
            pass
        self._update_tray_icon_badge()
        self._popup_update_timer.start()
        def do_post():
            try:
                self.client.mark_notifications_read([created_at])
            except Exception as e:
                print(f"Помилка позначення прочитаного: {e}")
        QtCore.QTimer.singleShot(0, do_post)

    def _rebuild_last_messages_menu(self):
        for act in self._messages_actions:
            self.menu.removeAction(act)
        self._messages_actions.clear()
        for msg in list(self._last_messages)[::-1]:
            text_val = str(getattr(msg, "text", ""))
            preview = (text_val[:60] + "…") if len(text_val) > 60 else text_val
            action = QtGui.QAction(f"{getattr(msg, 'title', 'Повідомлення')}: {preview}", self.menu)
            action.setToolTip(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(getattr(msg, "timestamp", time.time()))))
            action.triggered.connect(lambda _=False, m=msg: self._show_message_detail(m))
            self.menu.insertAction(self._messages_section, action)
            self._messages_actions.append(action)

    def _show_message_detail(self, msg: AppMessage):
        dlg = QtWidgets.QMessageBox(self.activeWindow())
        dlg.setWindowTitle(msg.title)
        dlg.setText(msg.text)
        dlg.setIcon(QtWidgets.QMessageBox.Information)
        dlg.exec()

    def _load_user_avatar(self, user_id: Optional[object], size: int = 32) -> QtGui.QPixmap:
        key = str(user_id) if user_id is not None else "anon"
        if key in self._toast_avatars:
            return self._toast_avatars[key]

        user = self.client.get_user_info(user_id) if user_id is not None else None
        url = None
        try:
            d = (user or {}).get("data") or {}
            for k in ("avatar", "avatarUrl", "photoUrl", "imageUrl", "photo"):
                v = d.get(k) or (user or {}).get(k)
                if isinstance(v, str) and v.startswith("http"):
                    url = v
                    break
        except Exception:
            pass

        if url:
            try:
                import requests
                r = requests.get(url, timeout=5)
                if r.ok:
                    pm = QtGui.QPixmap()
                    if pm.loadFromData(r.content):
                        pm = pm.scaled(size, size, QtCore.Qt.KeepAspectRatioByExpanding, QtCore.Qt.SmoothTransformation)
                        pm2 = QtGui.QPixmap(size, size)
                        pm2.fill(QtCore.Qt.transparent)
                        p = QtGui.QPainter(pm2)
                        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
                        path = QtGui.QPainterPath()
                        path.addEllipse(0, 0, size, size)
                        p.setClipPath(path)
                        p.drawPixmap(0, 0, pm)
                        p.end()
                        self._toast_avatars[key] = pm2
                        return pm2
            except Exception:
                pass

        pm2 = QtGui.QPixmap(size, size)
        pm2.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm2)
        h = hash(str(user_id)) % 360
        color = QtGui.QColor.fromHsv(h, 140, 220)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setBrush(color)
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(0, 0, size, size)
        p.setPen(QtCore.Qt.white)
        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(10)
        p.setFont(font)
        initials = (str(user_id)[:2] if user_id else "U").upper()
        p.drawText(pm2.rect(), QtCore.Qt.AlignCenter, initials)
        p.end()
        self._toast_avatars[key] = pm2
        return pm2

    def _compose_toast_icon(self, author_user_id: Optional[object]) -> QtGui.QIcon:
        size = getattr(self, "_TRAY_BASE_SIZE", 128)
        base_pm = self._icon_base.pixmap(size, size)
        pm = QtGui.QPixmap(base_pm)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        circle_size = int(size * 0.5)
        avatar_size = circle_size - 8
        avatar = self._load_user_avatar(author_user_id, size=avatar_size)
        circle = QtGui.QPixmap(circle_size, circle_size)
        circle.fill(QtCore.Qt.transparent)
        p2 = QtGui.QPainter(circle)
        p2.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p2.setBrush(QtGui.QBrush(QtCore.Qt.white))
        p2.setPen(QtCore.Qt.NoPen)
        p2.drawEllipse(0, 0, circle_size, circle_size)
        p2.end()
        margin = int(size * 0.06)
        px = margin
        py = size - margin - circle_size
        p.drawPixmap(px, py, circle)
        p.drawPixmap(px + 4, py + 4, avatar_size, avatar_size, avatar)
        p.end()
        return QtGui.QIcon(pm)

    @staticmethod
    def _strip_html(text: str) -> str:
        if not text:
            return ""
        s = re.sub(r"<[^>]+>", " ", text)
        s = unescape(s)
        return " ".join(s.split())

    def _get_avatar_pixmap(self, user_id: Optional[object], size: int = 32) -> QtGui.QPixmap:
        pm = self._load_user_avatar(user_id, size=size)
        if pm and not pm.isNull():
            return pm
        initials = "U"
        try:
            u = self.client.get_user_info(user_id) if user_id is not None else None
            d = (u or {}).get("data") or {}
            first = str(d.get("firstName") or "")[:1]
            last = str(d.get("lastName") or "")[:1]
            initials = (first + last).upper() or initials
        except Exception:
            pass
        pm2 = QtGui.QPixmap(size, size)
        pm2.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm2)
        h = hash(str(user_id)) % 360
        color = QtGui.QColor.fromHsv(h, 140, 220)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setBrush(color)
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(0, 0, size, size)
        p.setPen(QtCore.Qt.white)
        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(max(9, int(size * 0.38)))
        p.setFont(font)
        p.drawText(pm2.rect(), QtCore.Qt.AlignCenter, initials)
        p.end()
        return pm2

    def _title_for_notification(self, n: dict) -> str:
        ntype = str((n or {}).get("type") or "").strip()
        data = (n or {}).get("data") or {}
        if ntype == "comment":
            root_parent = data.get("root_parent") or {}
            parent_type = str((root_parent.get("type") or (root_parent.get("data") or {}).get("type") or "")).strip()
            if not parent_type:
                parent_type = str(((data.get("entity") or {}).get("type") or "")).strip()
            if parent_type == "task":
                mentioned = False
                try:
                    users = ((((data.get("entity") or {}).get("mentioned") or {}).get("users")) or [])
                    my_uid = getattr(self.client, "my_user_id", None)
                    mentioned = (my_uid is not None and str(my_uid) in {str(u) for u in users})
                except Exception:
                    mentioned = bool(n.get("mentioned_me"))
                return "You were mentioned in the task" if mentioned else "A new comment was added to the task"
            target = parent_type or "entity"
            return f"A new comment was added to {target}"
        if ntype == "task":
            action = str((data.get("action") or "")).strip()
            if action == "create":
                return "The task has been assigned"
            return "The task has been changed"
        return "Notification"

    def _toast_title_and_body(self, notif: dict) -> Tuple[str, str, Optional[object]]:
        data = (notif or {}).get("data") or {}
        entity = data.get("entity") or {}
        header = self._title_for_notification(notif)

        task_title = str(entity.get("title") or ((data.get("root_parent") or {}).get("data") or {}).get("title") or "").strip()
        raw_text = str(entity.get("message") or entity.get("description") or "")
        text = self._strip_html(raw_text)
        if len(text) > 180:
            text = text[:179] + "…"

        lines = []
        if task_title:
            lines.append(task_title)
        if text:
            lines.append(text)
        body = "\n".join(lines) if lines else " "

        author_user_id = (data.get("user_id") or None)
        return header, body, author_user_id

    def _show_tray_toast(self, title: str, body: str, msec: int = 5000, author_user_id: Optional[object] = None):
        title = (title or "").strip() or "Notification"
        body = self._strip_html(body or "") or " "

        try:
            if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                icon_pm = self._compose_toast_icon(author_user_id).pixmap(32, 32)
                screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
                geo = screen.availableGeometry()
                anchor = QtCore.QPoint(geo.right() - 16, geo.top() + 16)
                self._fallback_toast.show_for(title, body, msec, anchor, icon_pm)
                return

            if QtWidgets.QSystemTrayIcon.supportsMessages():
                icon_size = 32
                avatar_pm = self._get_avatar_pixmap(author_user_id, size=icon_size)
                print(avatar_pm, author_user_id)
                if not avatar_pm or avatar_pm.isNull():
                    avatar_pm = self._icon_base.pixmap(icon_size, icon_size)
                toast_icon = QtGui.QIcon(avatar_pm)

                self.tray.setIcon(self._compose_tray_icon(
                    sum(1 for n in (self._notifications or []) if not bool(n.get("read")))
                ))
                try:
                    self.tray.showMessage(title, body, toast_icon, msec)
                except TypeError:
                    icon_pm = avatar_pm
                    screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
                    geo = screen.availableGeometry()
                    anchor = QtCore.QPoint(geo.right() - 16, geo.top() + 16)
                    self._fallback_toast.show_for(title, body, msec, anchor, icon_pm)
            else:
                icon_pm = self._get_avatar_pixmap(author_user_id, size=32)
                if not icon_pm or icon_pm.isNull():
                    icon_pm = self._icon_base.pixmap(32, 32)
                screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
                geo = screen.availableGeometry()
                anchor = QtCore.QPoint(geo.right() - 16, geo.top() + 16)
                self._fallback_toast.show_for(title, body, msec, anchor, icon_pm)
        except Exception as e:
            print(f"Показ тосту не вдався: {e}")

    def show_notification(self, title: str, text: str, author_user_id: int):
        msg = AppMessage(title=title, text=text, timestamp=time.time(), author_user_id=author_user_id)
        QtCore.QTimer.singleShot(0, lambda m=msg: self._on_message_received(m))

    @QtCore.Slot(object)
    def _on_message_received(self, msg_obj: object):
        msg = msg_obj if isinstance(msg_obj, AppMessage) else AppMessage("Подія", str(msg_obj), time.time(), author_user_id)
        try:
            print(f"[NOTIFY] _on_message_received: '{msg.title}' (len={len(msg.text)})")
        except Exception:
            pass
        self._last_messages.append(msg)
        self._rebuild_last_messages_menu()
        if self.settings.is_toast_enabled():
            self._show_tray_toast(msg.title, msg.text, 5000, msg.author_user_id)
        if self.settings.is_sound_enabled() and self._sound:
            try:
                self._sound.play()
            except Exception as e:
                print(f"Не вдалося програти звук: {e}")

    def _open_settings_dialog(self):
        dlg = QtWidgets.QDialog()
        dlg.setWindowTitle("Налаштування")
        dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        dlg.setModal(True)
        layout = QtWidgets.QFormLayout(dlg)

        email_edit = QtWidgets.QLineEdit()
        password_edit = QtWidgets.QLineEdit()
        password_edit.setEchoMode(QtWidgets.QLineEdit.Password)

        email, password = self.settings.get_credentials()
        email_edit.setText(email)
        password_edit.setText(password)

        sound_chk = QtWidgets.QCheckBox("Відтворювати звук при нотифікації")
        sound_chk.setChecked(self.settings.is_sound_enabled())

        toast_chk = QtWidgets.QCheckBox("Показувати спливаючі повідомлення")
        toast_chk.setChecked(self.settings.is_toast_enabled())

        layout.addRow("Email:", email_edit)
        layout.addRow("Пароль:", password_edit)
        layout.addRow("", sound_chk)
        layout.addRow("", toast_chk)

        btn_box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        layout.addRow(btn_box)

        def on_save():
            self.settings.set_credentials(email_edit.text().strip(), password_edit.text())
            self.settings.set_sound_enabled(sound_chk.isChecked())
            self.settings.set_toast_enabled(toast_chk.isChecked())
            if email_edit.text().strip() and password_edit.text():
                self._try_sign_in(email_edit.text().strip(), password_edit.text())
            dlg.accept()

        btn_box.accepted.connect(on_save)
        btn_box.rejected.connect(dlg.reject)
        dlg.exec()

    def _try_sign_in(self, email: str, password: str):
        self._show_tray_toast("Авторизація", "Виконується вхід…", 3000)
        ok = self.client.sign_in(email, password)
        if ok:
            self._show_tray_toast("Авторизація", "Успішно!", 2500)
            QtCore.QTimer.singleShot(0, self._load_notifications_http)
        else:
            self._show_tray_toast("Авторизація", "Не вдалося виконати вхід", 4000)

    def _augment_mentions(self, n: dict):
        try:
            entity = ((n.get("data") or {}).get("entity") or {})
            users_list = list((entity.get("mentioned") or {}).get("users") or [])
            try:
                print(f"[_augment_mentions] users_list={users_list}")
            except Exception:
                pass
            users_list_str = [str(u) for u in users_list if u is not None]
            n["mentioned_users"] = users_list_str
            my_uid = getattr(self.client, "my_user_id", None)
            try:
                print(f"[_augment_mentions] my_uid={my_uid}")
            except Exception:
                pass
            n["mentioned_me"] = (my_uid is not None and str(my_uid) in set(users_list_str))
        except Exception as e:
            try:
                print(f"[_augment_mentions] Exception: {e}")
            except Exception:
                pass

    def _load_notifications_http(self):
        try:
            items = self.client.get_notifications() or []
            def parse_ts(n):
                ts = (((n or {}).get("data") or {}).get("timestamp") or "")
                if isinstance(ts, str) and ts:
                    try:
                        ts2 = ts.replace("Z", "+00:00")
                        return QtCore.QDateTime.fromString(ts2, QtCore.Qt.ISODateWithMs).toMSecsSinceEpoch()
                    except Exception:
                        pass
                return ((n or {}).get("createdAt") or 0)
            for it in items:
                self._augment_mentions(it)
            self._notifications = sorted(items, key=parse_ts, reverse=True)
            if self._popup and self._popup.isVisible():
                self._popup.update_data(self._notifications, self.client.my_user_id)
            self._update_tray_icon_badge()
        except Exception as e:
            print(f"Помилка завантаження нотифікацій: {e}")

    def _normalize_ws_notification(self, event_type: str, payload: dict) -> Optional[dict]:
        supported_events = {"pushNotification", "message", "notification", "notify"}
        if event_type not in supported_events:
            pass

        if isinstance(payload, list):
            payload = next((p for p in payload if isinstance(p, dict)), {}) if payload else {}
        if not isinstance(payload, dict):
            return None

        if "data" not in payload and "entity" in payload:
            payload = {**payload, "data": {"entity": payload.get("entity")}}

        ntype = (payload.get("type") or "").strip()
        data = payload.get("data") or {}
        nid = payload.get("id")
        read = bool(payload.get("read"))
        recipient = payload.get("recipient")
        metadata = payload.get("metadata") or []

        entity = data.get("entity") or {}
        mblock = (entity.get("mentioned") or {})
        users_list = list((mblock.get("users") or []))
        users_list_str = [str(u) for u in users_list if u is not None]

        my_uid = self.client.my_user_id if hasattr(self, "client") else None
        mentioned_me = (my_uid is not None and str(my_uid) in set(users_list_str))

        created_at = payload.get("createdAt")
        if not created_at:
            ts_iso = (data.get("timestamp") or "").strip()
            if ts_iso:
                try:
                    ts2 = ts_iso.replace("Z", "+00:00")
                    qdt = QtCore.QDateTime.fromString(ts2, QtCore.Qt.ISODateWithMs)
                    if qdt.isValid():
                        created_at = int(qdt.toMSecsSinceEpoch())
                except Exception:
                    created_at = None
            if not created_at:
                try:
                    secs = int(data.get("date") or 0)
                    if secs:
                        created_at = secs * 1000
                except Exception:
                    created_at = None
        if not created_at:
            created_at = int(time.time() * 1000)

        norm = {
            "id": nid,
            "type": ntype,
            "data": data,
            "read": read,
            "createdAt": created_at,
            "recipient": recipient,
            "metadata": metadata,
            "mentioned_me": mentioned_me,
            "mentioned_users": users_list_str,
            "topic": payload.get("topic"),
            "env": payload.get("env"),
            "domain": (data.get("domain") or payload.get("domain")),
            "service": data.get("service") or payload.get("service"),
        }
        try:
            print(f"[WS->GUI] normalized: {json.dumps({k: norm[k] for k in ('id','type','read','createdAt','recipient','mentioned_me')}, ensure_ascii=False)}")
        except Exception:
            pass
        return norm

    def handle(self, event_type: str, payload):
        try:
            self.ws_event.emit(event_type, payload)
        except Exception as e:
            print(f"[WS HANDLE] emit failed: {e}")
            QtCore.QTimer.singleShot(0, lambda: self._handle_event_on_main(event_type, payload))

    @QtCore.Slot(str, object)
    def _handle_event_on_main(self, event_type: str, payload):
        try:
            print(f"[WS EVENT] event_type='{event_type}' payload_is_dict={isinstance(payload, dict)}")
        except Exception:
            pass

        if event_type == "bootstrapNotifications":
            try:
                items = payload or []
                for it in items:
                    self._augment_mentions(it)
                def parse_ts(n):
                    ts = (((n or {}).get("data") or {}).get("timestamp") or "")
                    if isinstance(ts, str) and ts:
                        try:
                            ts2 = ts.replace("Z", "+00:00")
                            return QtCore.QDateTime.fromString(ts2, QtCore.Qt.ISODateWithMs).toMSecsSinceEpoch()
                        except Exception:
                            pass
                    return int((n or {}).get("createdAt") or 0)
                self._notifications = sorted(items, key=parse_ts, reverse=True)
                if self._popup and self._popup.isVisible():
                    self._popup.update_data(self._notifications, self.client.my_user_id)
                self._update_tray_icon_badge()
                return
            except Exception as e:
                print(f"[BOOTSTRAP] Помилка обробки початкових нотифікацій: {e}")
                return

        try:
            norm = self._normalize_ws_notification(event_type, payload)
            if not norm:
                base = payload if isinstance(payload, dict) else {}
                data = base.get("data") or {}
                if not data and "entity" in base:
                    data = {"entity": base.get("entity")}
                norm = {
                    "id": base.get("id"),
                    "type": (base.get("type") or event_type or "message"),
                    "data": data,
                    "read": bool(base.get("read")),
                    "createdAt": int(time.time() * 1000),
                    "recipient": base.get("recipient"),
                    "metadata": base.get("metadata") or [],
                    "mentioned_me": False,
                    "mentioned_users": [],
                }
                try:
                    print(f"[WS->GUI Fallback] built: {json.dumps({'type': norm['type'], 'createdAt': norm['createdAt']}, ensure_ascii=False)}")
                except Exception:
                    pass

            self._notifications.insert(0, norm)
            if len(self._notifications) > 500:
                self._notifications = self._notifications[:500]

            try:
                t_title, t_body, _author_id = self._toast_title_and_body(norm)
                print(t_body)
                self.show_notification(t_title, t_body, _author_id)
            except Exception as e:
                print(f"Помилка формування повідомлення: {e}")

            if self._popup and self._popup.isVisible():
                self._popup_update_timer.start()
            self._update_tray_icon_badge()
            return
        except Exception as e:
            print(f"Помилка обробки pushNotification: {e}")

        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(payload)
        self.show_notification(f"Подія: {event_type}", text, _author_id)

    def _update_tray_icon_badge(self):
        try:
            unread = sum(1 for n in (self._notifications or []) if not bool(n.get("read")))
        except Exception:
            unread = 0
        self.tray.setIcon(self._compose_tray_icon(unread))
        self.tray.setToolTip(f"Uspacy Notifier — Unread: {unread}")

    def _cleanup(self):
        try:
            self.tray.hide()
        except Exception:
            pass
        try:
            if hasattr(self.client, "shutdown_notifications"):
                self.client.shutdown_notifications()
        except Exception as e:
            print(f"Помилка при зупинці нотифікацій: {e}")

    def _cleanup_and_quit(self):
        self._cleanup()
        QtCore.QCoreApplication.quit()


def run_tray_app():
    app = TrayNotifierApp([])
    return app.exec()