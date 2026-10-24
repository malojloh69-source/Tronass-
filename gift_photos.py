"""Download original gift artwork and convert it to a Telegram-compatible JPEG."""
import io
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

from gifts import safe_photo_url


class PhotoError(Exception):
    def __init__(self, code=0, retry_after=0):
        super().__init__("Не удалось получить фотографию подарка")
        self.code, self.retry_after = code, retry_after


def photo_jpeg(url, telegram_file=False):
    def allowed(target):
        if telegram_file:
            parsed = urlsplit(target)
            return parsed.scheme == "https" and parsed.hostname == "api.telegram.org" and parsed.path.startswith("/file/bot")
        return bool(safe_photo_url(target))

    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not allowed(newurl):
                raise PhotoError(400)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if not allowed(url):
        raise PhotoError(400)
    try:
        with build_opener(Redirect()).open(Request(url, headers={"User-Agent": "TronRatingWatcher/2.0"}), timeout=15) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise PhotoError(400)
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 16_000_000:
                    raise PhotoError(400)
                image.thumbnail((1600, 1600))
                rgba = image.convert("RGBA")
                side = max(64, rgba.width, rgba.height)
                background = Image.new("RGB", (side, side), (28, 28, 30))
                background.paste(rgba, ((side-rgba.width)//2, (side-rgba.height)//2), rgba.getchannel("A"))
                output = io.BytesIO()
                background.save(output, "JPEG", quality=92)
                return output.getvalue()
        except (UnidentifiedImageError, ValueError, Image.DecompressionBombError):
            raise PhotoError(400) from None
    except HTTPError as exc:
        try:
            retry = max(0, float(exc.headers.get("Retry-After", 0)))
        except (ValueError, TypeError):
            retry = 60 if exc.code == 429 else 0
        raise PhotoError(exc.code, retry) from None
    except (URLError, TimeoutError, OSError):
        raise PhotoError() from None
    except ImportError:
        raise PhotoError(400) from None
