class _Base:
    def __init__(self, *a, **k):
        self.args = a
        self.__dict__.update(k)
class Bot:
    _next = 1000
    def __init__(self, token, request=None, get_updates_request=None):
        Bot._next += 1
        self.id = int(token.split(":")[0]); self.username = f"bot{self.id}"; self.token = token
        self.calls = []; self.reactions = []
    async def initialize(self):
        if token_bad(self.token): 
            from telegram.error import InvalidToken
            raise InvalidToken("bad")
    async def shutdown(self): pass
    async def set_message_reaction(self, chat_id, message_id, reaction=None):
        self.reactions.append((chat_id, message_id, reaction))
class Chat(_Base): pass
class Update(_Base): pass
class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None): self.text=text; self.callback_data=callback_data; self.url=url
class InlineKeyboardMarkup:
    def __init__(self, rows): self.rows = rows
class ReactionType(_Base): pass
class ReactionTypeEmoji(ReactionType):
    def __init__(self, emoji): self.emoji = emoji
class ReactionTypeCustomEmoji(ReactionType):
    def __init__(self, custom_emoji_id): self.custom_emoji_id = custom_emoji_id
class BotCommand(_Base): pass
class BotCommandScopeChat(_Base): pass
def token_bad(t): return t.endswith(":BAD")
