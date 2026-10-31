"""Optional helper *user* account (Telethon / MTProto).

The Bot API cannot put a bot that is not in a chat into it. A real account can:
the chat owner makes this account an admin (with the "Add New Admins" right), the
bot then uses it to make the reaction bots admins - even the ones that are not
members yet - and finally the account leaves the chat again.

Telethon is imported lazily, so the project runs without it when no helper is set.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

log = logging.getLogger(__name__)

READY = "ready"  # in the chat, may add admins
NO_RIGHTS = "no_rights"  # in the chat but cannot add admins
ABSENT = "absent"  # not in the chat
# Telegram refuses admin changes from a fresh session / a just-promoted admin for ~24h.
# The leading marker tells the caller to keep the account in the chat and retry later.
FRESH_MARK = "⏳"
FRESH_MSG = (
    f"{FRESH_MARK} تلگرام به اکانتِ تازه‌ادمین/تازه‌لاگین تا حدود ۲۴ ساعت اجازه‌ی تغییر ادمین نمی‌ده.\n"
    "اکانت رو از چت بیرون نکن؛ بعداً دوباره 🛡 رو بزن."
)


class Helper:
    def __init__(self, api_id: int, api_hash: str, session: str, leave_after: bool = True) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self.leave_after = leave_after
        self._client: Any = None
        self._lock = asyncio.Lock()  # one MTProto job at a time keeps flood limits calm
        self.user_id = 0
        self.username: str | None = None
        self.name = ""

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else (self.name or str(self.user_id))

    @property
    def mention(self) -> str:
        """HTML that points at the account: @username, or a clickable name plus the numeric id."""
        if self.username:
            return f"@{html.escape(self.username)}"
        name = html.escape(self.name or str(self.user_id))
        return f'<a href="tg://user?id={self.user_id}">{name}</a> (<code>{self.user_id}</code>)'

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._client.disconnect()
            raise RuntimeError("HELPER_SESSION is not logged in (run make_session.py again)")
        await self._load_me()
        log.info("helper account ready: %s", self.label)

    async def _load_me(self) -> None:
        me = await self._client.get_me()
        self.user_id, self.username = me.id, me.username
        self.name = " ".join(x for x in (me.first_name, me.last_name) if x)

    async def refresh(self) -> None:
        """Re-read name / username / id from Telegram (the owner can change them any time)."""
        if self._client is None:
            raise RuntimeError("اکانت متصل نیست")
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("نشست اکانت دیگه معتبر نیست؛ اکانت رو دوباره اضافه کن")
        await self._load_me()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def logout(self) -> None:
        """Terminate this session on Telegram's side, then disconnect."""
        if self._client is not None:
            try:
                await self._client.log_out()
            except Exception as exc:  # noqa: BLE001
                log.debug("helper log_out: %s", exc)
        await self.close()

    # ---------------------------------------------------------------- helpers
    async def _peer(self, ref: Any) -> Any:
        try:
            return await self._client.get_input_entity(ref)
        except ValueError:
            if isinstance(ref, str):
                raise
            await self._client.get_dialogs()  # fill the entity cache once
            return await self._client.get_input_entity(ref)

    async def status(self, chat_id: int) -> str:
        """READY when the account is in the chat and allowed to add admins."""
        async with self._lock:
            try:
                peer = await self._peer(chat_id)
                perms = await self._client.get_permissions(peer, "me")
            except Exception as exc:  # noqa: BLE001 - not a member / unknown chat
                log.debug("helper status %s: %s", chat_id, exc)
                return ABSENT
            if perms is None:
                return ABSENT
            if perms.is_creator or (perms.is_admin and perms.add_admins):
                return READY
            return NO_RIGHTS

    async def admin_count(self, chat_id: int) -> int | None:
        """How many admins (bots included) the chat has right now; None when it can't be told."""
        from telethon import functions, types

        async with self._lock:
            try:
                peer = await self._peer(chat_id)
                if not isinstance(peer, types.InputPeerChannel):
                    return None  # old-style small group: its limit is far away anyway
                try:
                    admins = await self._client.get_participants(peer, filter=types.ChannelParticipantsAdmins())
                    return len(admins)
                except Exception:  # noqa: BLE001
                    full = await self._client(functions.channels.GetFullChannelRequest(peer))
                    count = getattr(full.full_chat, "admins_count", None)
                    return int(count) if count is not None else None
            except Exception as exc:  # noqa: BLE001
                log.debug("helper admin_count %s: %s", chat_id, exc)
                return None

    # ------------------------------------------------------------------- join
    async def join(self, invite_link: str) -> tuple[bool, str]:
        """Join a chat through an invite link made by the main bot. Returns (ok, reason)."""
        from telethon import errors, functions

        invite_hash = invite_link.rstrip("/").rsplit("/", 1)[-1].lstrip("+")
        async with self._lock:
            try:
                await self._client(functions.messages.ImportChatInviteRequest(invite_hash))
            except errors.UserAlreadyParticipantError:
                return True, ""
            except errors.UserBannedInChannelError:
                return False, "اکانت کمکی توی این چت مسدوده"
            except errors.InviteRequestSentError:
                return False, "چت درخواست عضویت می‌خواد و تأیید نشد"
            except errors.FloodWaitError as exc:
                return False, f"محدودیت تلگرام؛ {exc.seconds} ثانیه صبر کن"
            except errors.RPCError as exc:
                return False, exc.__class__.__name__
            except Exception as exc:  # noqa: BLE001
                log.warning("helper could not join: %s", exc)
                return False, str(exc)[:80]
        return True, ""

    # ---------------------------------------------------------------- promote
    async def promote(self, chat_id: int, username: str | None, bot_id: int) -> tuple[str, str]:
        """Make the bot an admin of the chat. Returns (outcome, reason); outcome is
        "ok" | "fail" (try another bot) | "fatal" (nothing will work)."""
        from telethon import errors, functions, types

        async with self._lock:
            try:
                chat = await self._peer(chat_id)
                bot = await self._peer(f"@{username}" if username else bot_id)
                for attempt in range(3):
                    try:
                        await self._make_admin(functions, types, chat, bot)
                        return "ok", ""
                    except errors.FloodWaitError as exc:
                        if exc.seconds > 30 or attempt == 2:
                            return "fatal", f"محدودیت تلگرام؛ {exc.seconds} ثانیه صبر کن."
                        await asyncio.sleep(exc.seconds + 1)
                    except errors.UserNotParticipantError:
                        if attempt:  # already tried to invite it
                            return "fail", "ربات عضو چت نیست"
                        invite_error = await self._invite(functions, chat, bot)
                        if invite_error:
                            return "fail", invite_error
                return "fail", "نامشخص"
            except errors.FreshChangeAdminsForbiddenError:
                return "fatal", FRESH_MSG
            except errors.AdminsTooMuchError:
                return "fatal", "سقف ادمین‌های این چت پر شده."
            except (errors.ChatAdminRequiredError, errors.RightForbiddenError, errors.ChatAdminInviteRequiredError):
                return "fatal", "اکانت کمکی دسترسی «افزودن ادمین» نداره."
            except errors.RPCError as exc:
                return "fail", exc.__class__.__name__
            except Exception as exc:  # noqa: BLE001
                log.warning("helper promote failed: %s", exc)
                return "fail", str(exc)[:80]

    async def _make_admin(self, functions: Any, types: Any, chat: Any, bot: Any) -> None:
        if isinstance(chat, types.InputPeerChannel):
            await self._client(
                functions.channels.EditAdminRequest(
                    channel=chat,
                    user_id=bot,
                    admin_rights=types.ChatAdminRights(other=True),  # admin, no special rights
                    rank="",
                )
            )
        else:  # old-style small group
            await self._client(
                functions.messages.EditChatAdminRequest(chat_id=chat.chat_id, user_id=bot, is_admin=True)
            )

    async def _invite(self, functions: Any, chat: Any, bot: Any) -> str:
        from telethon import errors, types

        try:
            if isinstance(chat, types.InputPeerChannel):
                await self._client(functions.channels.InviteToChannelRequest(chat, [bot]))
            else:
                await self._client(functions.messages.AddChatUserRequest(chat.chat_id, bot, fwd_limit=0))
        except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
            return "اکانت کمکی اجازه‌ی اضافه‌کردن عضو توی این گروه رو نداره (دسترسی «افزودن کاربران»)."
        except errors.FloodWaitError:
            return "محدودیت تلگرام برای اکانت کمکی؛ کمی بعد دوباره تلاش کن."
        except Exception as exc:  # noqa: BLE001
            log.debug("helper invite failed: %s", exc)
            return "ربات عضو چت نیست"
        return ""

    # ------------------------------------------------------------------ leave
    async def leave(self, chat_id: int) -> bool:
        """Leave the chat (never as its creator - that would orphan it)."""
        async with self._lock:
            try:
                peer = await self._peer(chat_id)
                perms = await self._client.get_permissions(peer, "me")
                if perms is not None and perms.is_creator:
                    return False
                await self._client.delete_dialog(peer)
            except Exception as exc:  # noqa: BLE001
                log.warning("helper could not leave %s: %s", chat_id, exc)
                return False
        return True


# ------------------------------------------------------------------ login flow
class LoginError(Exception):
    """User-facing (Persian) message. retry=True: the same step can be answered again."""

    def __init__(self, message: str, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


class HelperLogin:
    """Interactive phone login (code, then 2FA password if the account has one)."""

    def __init__(self, api_id: int, api_hash: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self._client: Any = None
        self._phone = ""
        self._code_hash = ""

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def send_code(self, phone: str) -> None:
        from telethon import TelegramClient, errors
        from telethon.sessions import StringSession

        await self.close()
        self._client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            await self._client.connect()
            sent = await self._client.send_code_request(phone)
        except errors.ApiIdInvalidError:
            await self.close()
            raise LoginError("❌ api_id یا api_hash اشتباهه.") from None
        except errors.PhoneNumberInvalidError:
            await self.close()
            raise LoginError("❌ شماره نامعتبره. با کد کشور بفرست، مثل +98...", retry=True) from None
        except errors.FloodWaitError as exc:
            await self.close()
            raise LoginError(f"❌ محدودیت تلگرام؛ {exc.seconds} ثانیه بعد دوباره تلاش کن.") from None
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise LoginError(f"❌ {str(exc)[:100]}") from None
        self._phone, self._code_hash = phone, sent.phone_code_hash

    async def sign_in_code(self, code: str) -> bool:
        """True when the account also needs its 2FA password."""
        from telethon import errors

        try:
            await self._client.sign_in(self._phone, code, phone_code_hash=self._code_hash)
        except errors.SessionPasswordNeededError:
            return True
        except errors.PhoneCodeInvalidError:
            raise LoginError("❌ کد اشتباهه. دوباره بفرست.", retry=True) from None
        except (errors.PhoneCodeExpiredError, errors.PhoneCodeEmptyError):
            await self.close()
            raise LoginError("❌ کد منقضی شد. از اول شروع کن.") from None
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise LoginError(f"❌ {str(exc)[:100]}") from None
        return False

    async def sign_in_password(self, password: str) -> None:
        from telethon import errors

        try:
            await self._client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise LoginError("❌ رمز اشتباهه. دوباره بفرست.", retry=True) from None
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise LoginError(f"❌ {str(exc)[:100]}") from None

    def session_string(self) -> str:
        return self._client.session.save()
