import base64
import hashlib
from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from cryptography.fernet import Fernet
from django.conf import settings


def local_password_only():
    return settings.DEBUG and getattr(settings, "LOCAL_PASSWORD_ONLY", False)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False


class MFAAdapter(DefaultMFAAdapter):
    def is_mfa_enabled(self, user, types=None):
        if local_password_only():
            return False
        return super().is_mfa_enabled(user, types=types)

    def cipher(self):
        key = hashlib.sha256((settings.SECRET_KEY + ":mfa").encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, text):
        return self.cipher().encrypt(text.encode()).decode()

    def decrypt(self, encrypted_text):
        return self.cipher().decrypt(encrypted_text.encode()).decode()

    def can_delete_authenticator(self, authenticator):
        return False
