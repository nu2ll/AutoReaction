import enum
class ReactionEmoji(str, enum.Enum):
    THUMBS_UP="👍"; THUMBS_DOWN="👎"; RED_HEART="❤"; FIRE="🔥"; EYES="👀"
    MAN_TECH="👨‍💻"; HEART_FIRE="❤‍🔥"; PARTY="🎉"; CLAP="👏"
class ChatMemberStatus(str, enum.Enum):
    OWNER="creator"; ADMINISTRATOR="administrator"; MEMBER="member"; LEFT="left"; BANNED="kicked"
class ChatType(str, enum.Enum):
    PRIVATE="private"; GROUP="group"; SUPERGROUP="supergroup"; CHANNEL="channel"
class ParseMode(str, enum.Enum):
    HTML="HTML"
