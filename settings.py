from typing import Tuple
from PySide6 import QtCore


class NotifierSettings:
    """
    Збереження налаштувань через QSettings:
    - авторизація (email/password)
    - прапорці: звук/спливаючі нотифікації
    """
    def __init__(self):
        self.settings = QtCore.QSettings("Uspacy", "NotifierApp")

    def set_credentials(self, email: str, password: str):
        self.settings.setValue("auth/email", email)
        self.settings.setValue("auth/password", password)

    def get_credentials(self) -> Tuple[str, str]:
        email = self.settings.value("auth/email", "", type=str)
        password = self.settings.value("auth/password", "", type=str)
        return email, password

    def set_sound_enabled(self, enabled: bool):
        self.settings.setValue("notifications/sound_enabled", enabled)

    def is_sound_enabled(self) -> bool:
        return self.settings.value("notifications/sound_enabled", True, type=bool)

    def set_toast_enabled(self, enabled: bool):
        self.settings.setValue("notifications/toast_enabled", enabled)

    def is_toast_enabled(self) -> bool:
        return self.settings.value("notifications/toast_enabled", True, type=bool)