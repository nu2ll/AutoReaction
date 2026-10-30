class ApplicationHandlerStop(Exception): pass
class ContextTypes:
    DEFAULT_TYPE = object
class _H:
    def __init__(self,*a,**k): pass
Application=CallbackQueryHandler=ChatMemberHandler=CommandHandler=MessageHandler=TypeHandler=Defaults=_H
ChatMemberHandler.MY_CHAT_MEMBER = 1
class _F:
    def __and__(s,o): return s
    def __or__(s,o): return s
    def __invert__(s): return s
    def __getattr__(s,n): return _F()
    def __call__(s,*a,**k): return _F()
filters=_F()
