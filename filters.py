"""Telegram filter catalog and validation, using Tronaccs' published field schema."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_FILTERS = {"raitingLevelFrom": 2}
GROUPS = {
    "main": ("🔎 Поиск и цена", ["search", "sellerUsername", "price"]),
    "rating": ("⭐ Рейтинг и Stars", ["ratingLevel", "ratingStars", "stars"]),
    "country": ("🌍 Страна и происхождение", ["country", "excludeCountry", "origin", "excludeOrigin"]),
    "security": ("🛡 Premium и ограничения", ["premium", "premiumDaysRemaining", "spamblock", "spamblockGeo", "twoFactor"]),
    "activity": ("📊 Аккаунт и активность", ["age", "idLength", "settle", "contacts", "dialogs", "channels", "chats"]),
    "admin": ("📣 Каналы и чаты", ["adminChannels", "adminChannelsSubscribers", "adminChats", "adminChatsSubscribers"]),
    "gifts": ("🎁 Подарки", ["giftsRegular", "giftsNft"]),
}
SHORT = {"sellerUsername": "Продавец", "premiumDaysRemaining": "Остаток Premium, дней",
         "twoFactor": "Пароль 2FA", "settle": "Отлёжка, дней",
         "adminChannelsSubscribers": "Подписчики каналов", "adminChatsSubscribers": "Участники чатов"}
ANY = {"", "-", "любое", "любой", "все", "нет ограничения"}


def canonical(query):
    return json.dumps(query, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Catalog:
    def __init__(self, path=None):
        self.path = Path(path or Path(__file__).with_name("filter_catalog.json"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.fields = {x["key"]: x for x in data["schema"]["filters"]}
        self.countries = data["countries"]
        self.origins = data["origins"]
        self.group_map = {key: group for group, (_, keys) in GROUPS.items() for key in keys}

    def field(self, key):
        if key not in self.fields:
            raise ValueError("Этот фильтр больше не поддерживается. Откройте настройки заново.")
        return self.fields[key]

    def title(self, key):
        return SHORT.get(key, self.field(key)["label"])

    def group(self, key):
        return self.group_map.get(key, "extra")

    def groups(self):
        result = [(key, title, [x for x in keys if x in self.fields]) for key, (title, keys) in GROUPS.items()]
        extra = [x for x in self.fields if x not in self.group_map]
        if extra:
            result.append(("extra", "⚙️ Другие фильтры", extra))
        return result

    def keys(self, key):
        field = self.field(key)
        if field["type"] == "number-range":
            return [field["queries"]["from"], field["queries"]["to"]]
        return [field["query"]]

    def options(self, key, search=""):
        if key in ("country", "excludeCountry"):
            options = self.countries
        elif key in ("origin", "excludeOrigin"):
            options = self.origins
        else:
            options = self.field(key).get("options", [])
        needle = search.strip().casefold()
        return [x for x in options if not needle or needle in str(x.get("label", "")).casefold()
                or needle == str(x.get("code", "")).casefold() or needle == str(x.get("value", ""))]

    @staticmethod
    def number(raw, key, minimum=0):
        raw = str(raw).strip().replace(",", ".")
        if raw.casefold() in ANY:
            return None
        if len(raw) > 32:
            raise ValueError("Слишком большое число.")
        try:
            value = Decimal(raw)
            if not value.is_finite() or value < Decimal(str(minimum)) or value > 10 ** 15:
                raise ValueError()
            if key != "price" and value != value.to_integral_value():
                raise ValueError("Для этого фильтра нужно целое число.")
        except (InvalidOperation, ValueError):
            raise ValueError(f"Введите число от {minimum}; для снятия границы — знак -.") from None
        return int(value) if value == value.to_integral_value() else float(value)

    def apply(self, query, key, value):
        field = self.field(key)
        result = dict(query)
        params = self.keys(key)
        for param in params:
            result.pop(param, None)
        if value is None:
            return self.validate(result)
        if field["type"] == "number-range":
            parts = value if isinstance(value, (list, tuple)) else str(value).split()
            if len(parts) != 2:
                raise ValueError("Введите две границы через пробел, например: 2 5. Без верхней границы: 2 -.")
            low, high = [self.number(x, key, field.get("min", 0)) for x in parts]
            if low is not None and high is not None and low > high:
                raise ValueError("Нижняя граница должна быть не больше верхней.")
            for param, number in zip(params, (low, high)):
                if number is not None:
                    result[param] = number
        elif field["type"] == "number":
            number = self.number(value, key, field.get("min", 0))
            if number is not None:
                result[params[0]] = number
        elif field["type"] == "boolean":
            if str(value) not in {"0", "1"}:
                raise ValueError("Выберите «Да», «Нет» или «Не важно».")
            result[params[0]] = int(value)
        elif field["type"] == "select":
            choices = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
            known = {str(x["value"]) for x in self.options(key)}
            picked = sorted({str(x).strip() for x in choices if str(x).strip()})
            if any(x not in known for x in picked):
                raise ValueError("Выберите значение из списка.")
            if picked:
                result[params[0]] = ",".join(picked)
        else:
            value = str(value).strip()
            if len(value) > 120:
                raise ValueError("Введите не больше 120 символов.")
            if value.casefold() not in ANY:
                result[params[0]] = value
        return self.validate(result)

    def validate(self, query):
        if not isinstance(query, dict):
            raise ValueError("Некорректные настройки фильтров.")
        allowed = {param for key in self.fields for param in self.keys(key)}
        if set(query) - allowed:
            raise ValueError("В настройках есть неизвестный фильтр. Сбросьте его в меню.")
        for value in query.values():
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError("Некорректное значение фильтра.")
            if isinstance(value, (int, float)) and not Decimal(str(value)).is_finite():
                raise ValueError("Некорректное число.")
        return query

    def value(self, key, query):
        field = self.field(key)
        params = self.keys(key)
        if not any(x in query for x in params):
            return "Любое"
        if field["type"] == "number-range":
            low, high = [query.get(x) for x in params]
            if low is not None and high is not None:
                return f"{low} — {high}"
            return f"от {low}" if low is not None else f"до {high}"
        value = query.get(params[0])
        if field["type"] == "boolean":
            return "Да" if str(value) == "1" else "Нет"
        if field["type"] == "select":
            labels = {str(x["value"]): x["label"] for x in self.options(key)}
            return ", ".join(labels.get(x, x) for x in str(value).split(","))
        return str(value)

    def summary(self, query, limit=6):
        lines = [f"{self.title(key)}: {self.value(key, query)}" for key in self.fields
                 if any(x in query for x in self.keys(key))]
        return lines[:limit] + ([f"Ещё условий: {len(lines) - limit}"] if len(lines) > limit else [])


def matches(item, query, catalog=None):
    """Check public fields locally too; remaining fields are filtered by Tronaccs."""
    attrs = getattr(item, "attributes", {})
    numbers = {"price": Decimal(item.price), "raitingLevel": item.level,
               "raitingStars": attrs.get("raiting_stars"), "stars": attrs.get("stars"),
               "contacts": attrs.get("contacts"), "dialogs": attrs.get("dialogs"),
               "channels": attrs.get("channels"), "chats": attrs.get("chats"),
               "giftsRegular": attrs.get("gifts_count"), "giftsNft": attrs.get("unique_gifts_count"),
               "adminChannels": attrs.get("admin_channels_count"), "adminChats": attrs.get("admin_chats_count")}
    for name, value in numbers.items():
        if value is None:
            continue
        try:
            value = Decimal(str(value))
            if name + "From" in query and value < Decimal(str(query[name + "From"])):
                return False
            if name + "To" in query and value > Decimal(str(query[name + "To"])):
                return False
        except InvalidOperation:
            continue
    for param, value in (("premium", int(item.premium)), ("two_fa", attrs.get("two_factor"))):
        if param in query and value is not None and str(query[param]) != str(value):
            return False
    spam = attrs.get("spamblock")
    if spam is not None:
        if "spamblock_geo" in query and (int(spam) == 2) != bool(int(query["spamblock_geo"])):
            return False
        # A geo block may be allowed separately by the marketplace's spam filter.
        if "spamblock" in query and int(spam) != 2 and bool(int(spam)) != bool(int(query["spamblock"])):
            return False
    if "searchString" in query and str(query["searchString"]).casefold() not in item.title.casefold():
        return False
    if "seller_username" in query and attrs.get("seller"):
        if attrs["seller"].casefold() not in {x.strip().casefold() for x in str(query["seller_username"]).split(",")}:
            return False
    country = attrs.get("country_id")
    if country is None and catalog:
        country = next((x["value"] for x in catalog.countries
                        if x["label"] == item.country or x.get("code") == attrs.get("country_code")), None)
    for included, excluded, value in (("country", "not_country", country),
                                      ("origin", "not_origin", attrs.get("origin_id"))):
        if value is None:
            continue
        if included in query and str(value) not in str(query[included]).split(","):
            return False
        if excluded in query and str(value) in str(query[excluded]).split(","):
            return False
    return True
