class TelegramError(Exception):
    def __init__(self, message=""): super().__init__(message); self.message = message
class InvalidToken(TelegramError): pass
class Forbidden(TelegramError): pass
class NetworkError(TelegramError): pass
class BadRequest(NetworkError): pass
class TimedOut(NetworkError): pass
class RetryAfter(TelegramError):
    def __init__(self, n=0): super().__init__("retry"); self.retry_after = n
