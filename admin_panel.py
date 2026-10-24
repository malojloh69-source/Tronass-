"""Access management for registered users, using automatically captured identities."""
from menus import button, escaped


class AdminPanel:
    def __init__(self, bot):
        self.bot, self.state, self.menus = bot, bot.state, bot.menus

    def allowed(self, chat_id):
        if self.state.is_admin(chat_id):
            return True
        self.menus.show(chat_id, "Админ-панель недоступна.", [self.menus.back()])
        return False

    def home(self, chat_id, force_new=False):
        if not self.allowed(chat_id):
            return
        self.state.ui_set(chat_id, pending={})
        total, approved, pending = self.state.access_counts()
        text = ("<b>Админ-панель</b>\n\n"
                f"Пользователей: {total}\nС доступом: {approved}\nОжидают доступа: {pending}\n\n"
                "Выберите пользователя, чтобы выдать или отозвать доступ к трекеру.")
        rows = [[button("Ожидают доступа", "adm:list:pending:0")],
                [button("С доступом", "adm:list:allowed:0"), button("Все пользователи", "adm:list:all:0")],
                [button("Открыть трекер", "nav:home")]]
        self.menus.show(chat_id, text, rows, force_new=force_new)

    def users(self, chat_id, scope="pending", page=0):
        if not self.allowed(chat_id):
            return
        users, page, pages, total = self.state.access_list(scope, page)
        titles = {"pending": "Ожидают доступа", "allowed": "С доступом", "all": "Все пользователи"}
        rows = []
        for user in users:
            name = "@" + user["username"] if user["username"] else user["name"] or "Пользователь без username"
            rows.append([button(name[:55], f"adm:user:{user['id']}:{scope}:{page}")])
        if pages > 1:
            rows.append([button("‹", f"adm:list:{scope}:{max(0, page-1)}"),
                         button(f"{page+1} / {pages}", "noop"),
                         button("›", f"adm:list:{scope}:{min(pages-1, page+1)}")])
        rows.append(self.menus.back("nav:admin", "‹ Админ-панель"))
        text = f"<b>{escaped(titles[scope])}</b>\n\nПользователей: {total}"
        if not users:
            text += "\nСписок пуст."
        self.menus.show(chat_id, text, rows)

    def user(self, chat_id, target_id, scope="pending", page=0, notice=""):
        if not self.allowed(chat_id):
            return
        user = self.state.access_user(target_id)
        if not user:
            raise ValueError("Пользователь должен сначала написать боту.")
        title = user["name"] or "Пользователь"
        username = "@" + user["username"] if user["username"] else "Не указан"
        text = (f"<b>{escaped(title)}</b>\n\nUsername: {escaped(username, 70)}\n"
                f"Доступ: {'открыт' if user['has_access'] else 'закрыт'}\n"
                f"Роль: {'администратор' if user['is_admin'] else 'пользователь'}")
        rows = []
        if not user["is_admin"]:
            action = "revoke" if user["has_access"] else "grant"
            rows.append([button("Отозвать доступ" if user["has_access"] else "Выдать доступ",
                                f"adm:{action}:{target_id}:{scope}:{page}")])
        rows.append(self.menus.back(f"adm:list:{scope}:{page}", "‹ К списку"))
        if notice:
            text += "\n\n" + escaped(notice)
        self.menus.show(chat_id, text, rows)

    def callback(self, chat_id, data):
        if not self.allowed(chat_id):
            return
        self.state.ui_set(chat_id, pending={})
        parts = data.split(":")
        try:
            if parts[1] == "list":
                self.users(chat_id, parts[2], int(parts[3]))
            elif parts[1] in ("user", "grant", "revoke"):
                target, scope, page = int(parts[2]), parts[3], int(parts[4])
                if scope not in ("pending", "allowed", "all"):
                    raise ValueError("Неизвестный список")
                notice = ""
                if parts[1] != "user":
                    allowed = parts[1] == "grant"
                    self.state.change_access(chat_id, target, allowed)
                    self.bot.wake.set()
                    notice = "Доступ выдан." if allowed else "Доступ отозван."
                self.user(chat_id, target, scope, page, notice)
            else:
                raise ValueError("Команда устарела.")
        except (ValueError, IndexError, PermissionError, OverflowError) as exc:
            self.menus.show(chat_id, "<b>Не удалось изменить доступ</b>\n\n" + escaped(str(exc), 180),
                            [self.menus.back("nav:admin", "‹ Админ-панель")])
