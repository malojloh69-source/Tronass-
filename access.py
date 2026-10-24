"""Private authorization verifier; no account identifiers or plaintext secret."""
import hashlib
import hmac

_SALT = bytes.fromhex("649ee1dfc2809f985f28c18ba97013c0")
_DIGEST = bytes.fromhex("bb6ffb28fb1f750ff4ff54cdfbe43b4afa35631b34a11382e5e5721f64fff746")
_ITERATIONS = 200000

DENIED_TEXT = "У вас нету Доступа к Tronaccs Трекеру напишите Владельцу"
OWNER_URL = "https://t.me/klundyy"


def is_admin_entry(text):
    token = str(text).strip()
    if not token.startswith("/") or len(token) > 128 or any(x.isspace() for x in token):
        return False
    token = token.split("@", 1)[0].casefold()
    actual = hashlib.pbkdf2_hmac("sha256", token.encode(), _SALT, _ITERATIONS)
    return hmac.compare_digest(actual, _DIGEST)
