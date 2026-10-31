"""Log the helper account in once and print its HELPER_SESSION string.

    docker run -it --rm autoreaction python make_session.py

Get api_id / api_hash from https://my.telegram.org (API development tools).
Keep the printed string secret: it gives full access to that account.
"""
from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:  # asks phone/code/2FA
    me = client.loop.run_until_complete(client.get_me())
    print(f"\nlogged in as {me.first_name} (@{me.username})\n")
    print("HELPER_API_ID=" + str(api_id))
    print("HELPER_API_HASH=" + api_hash)
    print("HELPER_SESSION=" + client.session.save())
