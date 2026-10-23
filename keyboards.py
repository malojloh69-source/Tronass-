"""Native red Telegram buttons, without changing labels or adding icons."""


def red_keyboard(markup):
    if not isinstance(markup, dict) or "inline_keyboard" not in markup:
        return markup
    return {**markup, "inline_keyboard": [
        [{**item, "style": "danger"} for item in row]
        for row in markup["inline_keyboard"]
    ]}
