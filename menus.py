"""Photo-backed Telegram menus for every tracker screen."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import urlencode

from filters import ANY, DEFAULT_FILTERS

SITE = "https://tronaccs.market"


def button(text, data):
    if len(data.encode()) > 64:
        raise ValueError("Слишком длинная команда кнопки")
    return {"text": text, "callback_data": data}


def escaped(value, length=100):
    value = str(value)
    return html.escape(value[:length] + ("…" if len(value) > length else ""))


class Menus:
    def __init__(self, bot):
        self.bot = bot
        self.state = bot.state
        self.catalog = bot.catalog
        self.searches = {}

    def show(self, chat_id, text, rows):
        keyboard = {"inline_keyboard": rows}
        saved = self.state.ui_get(chat_id)
        if hasattr(self.bot.tg, "screen"):
            result = self.bot.tg.screen(chat_id, text, keyboard, saved["message_id"])
        else:
            result = self.bot.tg.send(chat_id, text, keyboard)
        if isinstance(result, dict) and result.get("message_id"):
            self.state.ui_set(chat_id, message_id=result["message_id"])

    @staticmethod
    def back(data="nav:home", label="‹ В меню"):
        return [button(label, data)]

    def summary(self, chat_id, limit=6):
        lines = self.catalog.summary(self.state.chat_filters(chat_id), limit)
        return "\n".join(escaped(x, 85) for x in lines) if lines else "Все аккаунты Telegram · без ограничений"

    def home(self, chat_id, notice=""):
        active = self.state.is_active(chat_id)
        text = ("<b>Tronaccs Трекер</b>\n"
                f"{'🟢 Мониторинг включён' if active else '⏸ Мониторинг выключен'}\n\n"
                f"<b>Что отслеживаем</b>\n{self.summary(chat_id, 5)}\n\n"
                f"Проверка каждые {self.bot.cfg['poll_seconds']} сек.")
        if notice:
            text += "\n\n" + escaped(notice, 110)
        rows = [[button("⚙️ Настроить фильтры", "nav:filters")],
                [button("⏸ Остановить" if active else "▶️ Включить", "monitor:off" if active else "monitor:on"),
                 button("🔄 Проверить", "check")],
                [button("📊 Статус", "nav:status"), button("❔ Помощь", "nav:help")]]
        self.show(chat_id, text, rows)

    def filters(self, chat_id):
        rows = [[button(title, "grp:" + key + ":0")] for key, title, _ in self.catalog.groups()]
        rows += [[button("⭐ Рейтинг 2+", "quick:rating"), button("🌐 Все аккаунты", "quick:all")],
                 [button("🔗 Поиск на сайте", "nav:link"), button("↺ Сбросить", "reset:ask")], self.back()]
        text = "<b>⚙️ Фильтры мониторинга</b>\n\n" + self.summary(chat_id, 5) + "\n\nВыберите раздел. Изменения сохраняются сразу."
        self.show(chat_id, text, rows)

    def group(self, chat_id, group, page=0):
        groups = {key: (title, keys) for key, title, keys in self.catalog.groups()}
        if group not in groups:
            raise ValueError("Раздел не найден.")
        title, keys = groups[group]
        count = max(1, (len(keys) + 5) // 6)
        page = max(0, min(int(page), count - 1))
        query = self.state.chat_filters(chat_id)
        rows = [[button(f"{self.catalog.title(key)} · {self.catalog.value(key, query)[:35]}", "fld:" + key)]
                for key in keys[page * 6:(page + 1) * 6]]
        if count > 1:
            rows.append([button("‹", f"grp:{group}:{max(0, page - 1)}"),
                         button(f"{page + 1} / {count}", "noop"),
                         button("›", f"grp:{group}:{min(count - 1, page + 1)}")])
        rows += [self.back("nav:filters", "‹ Все фильтры")]
        self.show(chat_id, f"<b>{escaped(title)}</b>\n\nНажмите на условие, чтобы изменить его.", rows)

    def field(self, chat_id, key, notice=""):
        field = self.catalog.field(key)
        if field["type"] == "select":
            self.select(chat_id, key)
            return
        query = self.state.chat_filters(chat_id)
        text = f"<b>{escaped(self.catalog.title(key))}</b>\n\nСейчас: <b>{escaped(self.catalog.value(key, query), 180)}</b>"
        rows = []
        if field["type"] == "boolean":
            rows.append([button("Да", f"set:{key}:1"), button("Нет", f"set:{key}:0"), button("Не важно", "clear:" + key)])
        else:
            rows.append([button("✏️ Изменить", "input:" + key), button("Любое", "clear:" + key)])
            if key == "ratingLevel":
                rows.append([button("2+", "preset:2"), button("3+", "preset:3"), button("5+", "preset:5")])
        rows.append(self.back(f"grp:{self.catalog.group(key)}:0", "‹ В раздел"))
        if notice:
            text += "\n\n" + escaped(notice)
        self.show(chat_id, text, rows)

    def select(self, chat_id, key, page=0):
        field = self.catalog.field(key)
        search = self.searches.get((chat_id, key), "")
        options = self.catalog.options(key, search)
        pages = max(1, (len(options) + 7) // 8)
        page = max(0, min(int(page), pages - 1))
        query = self.state.chat_filters(chat_id)
        selected = set(str(query.get(field["query"], "")).split(",")) - {""}
        rows = [[button(("✓ " if str(x["value"]) in selected else "") + x["label"],
                        f"toggle:{key}:{x['value']}:{page}")] for x in options[page * 8:(page + 1) * 8]]
        if pages > 1:
            rows.append([button("‹", f"select:{key}:{max(0, page - 1)}"), button(f"{page + 1} / {pages}", "noop"),
                         button("›", f"select:{key}:{min(pages - 1, page + 1)}")])
        rows.append([button("🔎 Поиск", "search:" + key), button("Все варианты", "allopts:" + key)])
        rows.append([button("Очистить выбор", "clear:" + key)])
        rows.append(self.back(f"grp:{self.catalog.group(key)}:0", "‹ В раздел"))
        text = (f"<b>{escaped(self.catalog.title(key))}</b>\n\n"
                f"Выбрано: {escaped(self.catalog.value(key, query), 230)}\n\n"
                "Можно отметить несколько вариантов. Пустой выбор означает «любое».")
        if search:
            text += "\nПоиск: " + escaped(search, 50)
        if not options:
            text += "\n\nНичего не найдено. Нажмите «Все варианты»."
        self.show(chat_id, text, rows)

    def prompt(self, chat_id, key, mode="value", error=""):
        field = self.catalog.field(key)
        self.state.ui_set(chat_id, pending={"key": key, "mode": mode})
        if mode == "search":
            instruction = "Напишите название или код страны, например: Россия или RU. Для происхождения — часть названия."
        elif field["type"] == "number-range":
            instruction = "Напишите две границы через пробел.\nНапример: <code>2 5</code>\nТолько от: <code>2 -</code>\nТолько до: <code>- 5</code>\nЛюбое значение: <code>-</code>"
        elif field["type"] == "number":
            instruction = "Напишите число. Для снятия условия: <code>-</code>"
        else:
            instruction = "Напишите значение сообщением. До 120 символов.\nДля снятия условия: <code>-</code>"
        text = f"<b>{escaped(self.catalog.title(key))}</b>\n\n{instruction}"
        if error:
            text += "\n\n⚠️ " + escaped(error, 150)
        self.show(chat_id, text, [self.back("fld:" + key, "‹ Отменить ввод")])

    def save(self, chat_id, key, value):
        query = self.catalog.apply(self.state.chat_filters(chat_id), key, value)
        self.state.set_filters(chat_id, query)
        self.state.ui_set(chat_id, pending={})
        self.bot.wake.set()

    def input(self, chat_id, text):
        pending = self.state.ui_get(chat_id)["pending"]
        key, mode = pending.get("key"), pending.get("mode", "value")
        try:
            self.catalog.field(key)
            if mode == "search":
                if len(text) > 80:
                    raise ValueError("Поисковый запрос: не больше 80 символов.")
                self.searches[(chat_id, key)] = "" if text.casefold() in ANY else text
                self.state.ui_set(chat_id, pending={})
                self.select(chat_id, key)
            else:
                self.save(chat_id, key, None if text.strip().casefold() in ANY else text)
                self.field(chat_id, key, "Сохранено. Новые объявления будут проверяться по этим условиям.")
        except ValueError as exc:
            self.prompt(chat_id, key, mode, str(exc))

    def status(self, chat_id):
        profile = self.state.chat_profile(chat_id)
        last = self.state.get(self.state.key(profile, "last_success"))
        stamp = datetime.fromtimestamp(float(last), timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC") if last else "ещё не было"
        error = self.state.get(self.state.key(profile, "error")) or self.bot.error
        text = ("<b>📊 Статус мониторинга</b>\n\n"
                f"Уведомления: {'включены' if self.state.is_active(chat_id) else 'выключены'}\n"
                f"Проверка каждые {self.bot.cfg['poll_seconds']} сек.\n"
                f"Успешная проверка: {stamp}\n"
                f"Сайт: {escaped(error, 170) if error else 'работает' if last else 'ожидает первой проверки'}\n"
                f"Доставка: {escaped(self.bot.delivery_error, 100) if self.bot.delivery_error else 'без ошибок'}\n\n"
                f"Ваш ID: <code>{chat_id}</code>")
        self.show(chat_id, text, [[button("🔄 Проверить", "check"), button("⚙️ Фильтры", "nav:filters")], self.back()])

    def help(self, chat_id):
        text = ("<b>❔ Как пользоваться</b>\n\n"
                "1. Откройте «Настроить фильтры».\n2. Выберите условия, как на Tronaccs.\n"
                "3. Включите мониторинг.\n\n"
                "Уведомления приходят о новых подходящих объявлениях. Текущий каталог при первом подключении не рассылается.\n\n"
                "Настройки сохраняются отдельно для каждого пользователя. После смены набора условий сначала устанавливается новая точка отсчёта.\n\n"
                "/settings — фильтры\n/status — состояние\n/stop — пауза\n/start — включить\n/menu — главное меню")
        self.show(chat_id, text, [self.back()])

    def callback(self, chat_id, data):
        parts = data.split(":")
        action = parts[0]
        if action == "noop":
            return
        self.state.ui_set(chat_id, pending={})
        try:
            if action == "nav":
                target = parts[1]
                if target in ("home", "filters", "status", "help"):
                    getattr(self, target)(chat_id)
                elif target == "link":
                    params = {"category": "telegram", **self.state.chat_filters(chat_id), "orderBy": "time_add", "orderType": "DESC"}
                    self.show(chat_id, "<b>🔗 Ваш поиск на Tronaccs</b>\n\nКнопка откроет страницу с выбранными условиями.",
                              [[{"text": "Открыть поиск", "url": SITE + "/market?" + urlencode(params)}], self.back("nav:filters", "‹ Фильтры")])
                else:
                    raise ValueError("Раздел не найден.")
            elif action == "grp":
                self.group(chat_id, parts[1], int(parts[2]))
            elif action == "fld":
                self.field(chat_id, parts[1])
            elif action in ("input", "search"):
                self.prompt(chat_id, parts[1], "search" if action == "search" else "value")
            elif action == "select":
                self.select(chat_id, parts[1], int(parts[2]))
            elif action == "allopts":
                self.searches.pop((chat_id, parts[1]), None)
                self.select(chat_id, parts[1])
            elif action == "toggle":
                key, value, page = parts[1], parts[2], int(parts[3])
                field = self.catalog.field(key)
                selected = set(str(self.state.chat_filters(chat_id).get(field["query"], "")).split(",")) - {""}
                selected.symmetric_difference_update({value})
                self.save(chat_id, key, selected)
                self.select(chat_id, key, page)
            elif action == "set":
                self.save(chat_id, parts[1], parts[2])
                self.field(chat_id, parts[1], "Сохранено")
            elif action == "clear":
                self.save(chat_id, parts[1], None)
                self.field(chat_id, parts[1], "Ограничение снято")
            elif action == "preset":
                self.save(chat_id, "ratingLevel", [parts[1], "-"])
                self.field(chat_id, "ratingLevel", "Сохранено")
            elif action == "quick":
                self.state.set_filters(chat_id, dict(DEFAULT_FILTERS) if parts[1] == "rating" else {})
                self.bot.wake.set()
                self.filters(chat_id)
            elif action == "monitor":
                self.state.subscribe(chat_id, parts[1] == "on")
                self.bot.wake.set()
                self.home(chat_id)
            elif action == "check":
                self.bot.request_check(chat_id)
                self.home(chat_id, "Проверка запрошена.")
            elif action == "reset":
                if parts[1] == "yes":
                    self.state.set_filters(chat_id, dict(DEFAULT_FILTERS))
                    self.bot.wake.set()
                    self.filters(chat_id)
                else:
                    self.show(chat_id, "<b>Сбросить настройки?</b>\n\nВернётся исходный фильтр: рейтинг от 2. Остальные условия будут сняты.",
                              [[button("Да, сбросить", "reset:yes")], self.back("nav:filters", "‹ Отмена")])
            else:
                raise ValueError("Команда устарела. Откройте настройки заново.")
        except (ValueError, IndexError, KeyError) as exc:
            self.show(chat_id, "<b>Не удалось изменить настройку</b>\n\n" + escaped(str(exc), 200), [self.back("nav:filters", "‹ Фильтры")])
