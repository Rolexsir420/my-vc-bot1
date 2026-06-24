from pyrogram import Client, enums, filters
from pyrogram.raw.functions.phone import GetGroupCall, EditGroupCallParticipant
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
import sqlite3
import re
import asyncio
import pytz
import aiohttp
import os
import tempfile

IST = pytz.timezone('Asia/Kolkata')

def now_ist():
    return datetime.now(IST).strftime("%I:%M:%S %p")

# --- FILL THESE ---
API_ID = 38524810
API_HASH = "af88419ea0782d5644f2dbe7e3561ae2"
OWNER_ID = 8834161906
LOG_CHANNEL = "ghiqty"
ALLOWED_GROUPS = [-1002483433187]
ALLOWED_CHANNELS = []

# --- SIGHTENGINE ---
SIGHTENGINE_USER   = "1297817509"
SIGHTENGINE_SECRET = "DfGeVrNhJQJvBBTehCXkmmgPfru47mhv"
NSFW_THRESHOLD = 0.6

import os
SESSION_STRING = os.environ.get("SESSION_STRING", "")
if SESSION_STRING:
    app = Client("vc_moderator", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
else:
    app = Client("vc_moderator", api_id=API_ID, api_hash=API_HASH)

vc_members = {}
muted_in_vc = {}
vc_channels = {}
vc_video_users = {}
video_muted = {}

# ============================================
# 🛡️ PYROGRAM BUG FIX
# ============================================
import pyrogram.client as _pyro_client
_orig_handle_updates = _pyro_client.Client.handle_updates

async def _patched_handle_updates(self, updates):
    try:
        await _orig_handle_updates(self, updates)
    except (ValueError, KeyError) as e:
        if "Peer id invalid" in str(e) or "ID not found" in str(e):
            pass
        else:
            raise

_pyro_client.Client.handle_updates = _patched_handle_updates

# ============================================
# 🚀 CALL CACHE
# ============================================
call_cache = {}
call_cache_time = {}
CACHE_TTL = 30

async def get_cached_call(chat_id):
    now = asyncio.get_event_loop().time()
    if (chat_id in call_cache and
            now - call_cache_time.get(chat_id, 0) < CACHE_TTL):
        return call_cache[chat_id]
    try:
        peer = await app.resolve_peer(chat_id)
    except (KeyError, ValueError) as e:
        print(f"⚠️ resolve_peer failed for {chat_id}, re-registering: {e}")
        try:
            await app.get_chat(chat_id)
            peer = await app.resolve_peer(chat_id)
        except Exception as e2:
            print(f"❌ Re-register failed: {e2}")
            return None
    try:
        full_chat = await app.invoke(GetFullChannel(channel=peer))
    except Exception as e:
        print(f"❌ GetFullChannel error: {e}")
        return None
    if not full_chat.full_chat.call:
        return None
    call_cache[chat_id] = full_chat.full_chat.call
    call_cache_time[chat_id] = now
    return full_chat.full_chat.call

def invalidate_call_cache(chat_id):
    call_cache.pop(chat_id, None)
    call_cache_time.pop(chat_id, None)

# ============================================
# 💾 DATABASE
# ============================================
def init_db():
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            user_id    INTEGER,
            chat_id    INTEGER,
            warn_count INTEGER DEFAULT 0,
            first_name TEXT,
            last_warned TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            user_id   INTEGER,
            chat_id   INTEGER,
            joined_at TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    conn.commit()
    conn.close()

def add_warning(user_id, chat_id, first_name):
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    now = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    c.execute("""
        INSERT INTO warnings (user_id, chat_id, warn_count, first_name, last_warned)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET
            warn_count  = warn_count + 1,
            first_name  = excluded.first_name,
            last_warned = excluded.last_warned
    """, (user_id, chat_id, first_name, now))
    conn.commit()
    count = c.execute(
        "SELECT warn_count FROM warnings WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()[0]
    conn.close()
    return count

def reset_warnings(user_id, chat_id):
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id=? AND chat_id=?",
              (user_id, chat_id))
    conn.commit()
    conn.close()

def save_group_member(user_id, chat_id):
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    now = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    c.execute(
        "INSERT OR REPLACE INTO group_members "
        "(user_id, chat_id, joined_at) VALUES (?, ?, ?)",
        (user_id, chat_id, now)
    )
    conn.commit()
    conn.close()

def remove_group_member(user_id, chat_id):
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    c.execute(
        "DELETE FROM group_members WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    )
    conn.commit()
    conn.close()

def is_known_member(user_id, chat_id):
    conn = sqlite3.connect("warnings.db")
    c = conn.cursor()
    result = c.execute(
        "SELECT 1 FROM group_members WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()
    conn.close()
    return result is not None

# ============================================
# 📋 LOG
# ============================================
async def send_log(action, user_name, user_id, chat_id, reason, warns=None):
    warn_line = f"⚠️ **Warnings:** `{warns}/3`\n" if warns else ""
    text = (
        f"📋 **VC BOT LOG**\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 **User:** [{user_name}](tg://user?id={user_id}) (`{user_id}`)\n"
        f"👥 **Group:** `{chat_id}`\n"
        f"⚡ **Action:** {action}\n"
        f"📝 **Reason:** {reason}\n"
        f"{warn_line}"
        f"━━━━━━━━━━━━━━━━"
    )
    try:
        await app.send_message(LOG_CHANNEL, text)
        print(f"✅ Log sent: {action} | {user_name}")
    except Exception as e:
        print(f"❌ Log failed: {e}")

def fire_log(action, user_name, user_id, chat_id, reason, warns=None):
    asyncio.create_task(send_log(action, user_name, user_id, chat_id, reason, warns))

# ============================================
# 🔍 BIO CHECK
# ============================================
def has_group_link(bio):
    if not bio:
        return False
    return bool(re.search(
        r"(t\.me/joinchat|t\.me/\+)[a-zA-Z0-9_-]+", bio
    ))

# ============================================
# 🖼️ NSFW DP CHECK
# ============================================
async def check_dp_nsfw(user_id):
    tmp_path = None
    try:
        try:
            photos = []
            async for photo in app.get_chat_photos(user_id, limit=1):
                photos.append(photo)
            if not photos:
                return False, 0.0, "no_photo"
            tmp_path = await app.download_media(
                photos[0],
                file_name=tempfile.mktemp(suffix=".jpg")
            )
        except Exception:
            chat = await app.get_chat(user_id)
            if not chat.photo:
                return False, 0.0, "no_photo"
            tmp_path = await app.download_media(
                chat.photo.big_file_id,
                file_name=tempfile.mktemp(suffix=".jpg")
            )

        if not tmp_path or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            return False, 0.0, "download_failed"

        async with aiohttp.ClientSession() as session:
            with open(tmp_path, "rb") as img_file:
                form = aiohttp.FormData()
                form.add_field("media", img_file, filename="dp.jpg", content_type="image/jpeg")
                form.add_field("models", "nudity-2.0,offensive,gore")
                form.add_field("api_user", SIGHTENGINE_USER)
                form.add_field("api_secret", SIGHTENGINE_SECRET)

                async with session.post(
                    "https://api.sightengine.com/1.0/check.json",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return False, 0.0, "api_error"
                    data = await resp.json()

        scores = {}
        try:
            nudity = data.get("nudity", {})
            scores["sexual_explicit"] = nudity.get("sexual_activity", 0) or nudity.get("explicit", 0) or 0
            scores["suggestive"]      = nudity.get("suggestive", 0) or nudity.get("suggestive_classes", {}).get("bikini", 0) or 0
            scores["very_suggestive"] = nudity.get("very_suggestive", 0) or 0
            scores["offensive"]       = data.get("offensive", {}).get("prob", 0) or 0
            scores["gore"]            = data.get("gore", {}).get("prob", 0) or 0
        except Exception:
            pass

        if not scores:
            return False, 0.0, "parse_error"

        max_label = max(scores, key=scores.get)
        max_score = scores[max_label]

        is_suspicious = max_score >= NSFW_THRESHOLD
        return is_suspicious, round(max_score, 2), max_label

    except Exception as e:
        print(f"❌ NSFW check error for {user_id}: {e}")
        return False, 0.0, "exception"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def send_dp_review(chat_id, user_id, first_name, score, label):
    try:
        tmp_path = None
        try:
            chat = await app.get_chat(user_id)
            if not chat.photo:
                return
            tmp_path = await app.download_media(
                chat.photo.small_file_id,
                file_name=tempfile.mktemp(suffix=".jpg")
            )
        except Exception:
            return

        if not tmp_path or not os.path.exists(tmp_path):
            return

        label_display = label.replace("_", " ").title()
        caption = (
            f"🚨 **Suspicious Profile Photo Detected**\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👤 **User:** [{first_name}](tg://user?id={user_id}) (`{user_id}`)\n"
            f"👥 **Group:** `{chat_id}`\n"
            f"⚠️ **Type:** `{label_display}`\n"
            f"📊 **Score:** `{score}` / `1.0` (threshold: `{NSFW_THRESHOLD}`)\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👮 Admin action required:"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 Ban from Group",
                    callback_data=f"dpban_{chat_id}_{user_id}"
                ),
                InlineKeyboardButton(
                    "✅ Clear (Not NSFW)",
                    callback_data=f"dpclear_{chat_id}_{user_id}"
                ),
            ]
        ])

        await app.send_photo(
            LOG_CHANNEL,
            photo=tmp_path,
            caption=caption,
            reply_markup=keyboard
        )
        print(f"🚨 DP review sent for {first_name} ({user_id}) — score: {score}")

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    except Exception as e:
        print(f"❌ send_dp_review error: {e}")


# ============================================
# 🔘 CALLBACK: Admin taps Ban / Clear on DP review
# ============================================
@app.on_callback_query(filters.regex(r"^dp(ban|clear)_(-?\d+)_(\d+)$"))
async def dp_review_callback(client, callback_query):
    try:
        action   = callback_query.matches[0].group(1)
        chat_id  = int(callback_query.matches[0].group(2))
        user_id  = int(callback_query.matches[0].group(3))
        admin    = callback_query.from_user
        admin_name = admin.first_name or str(admin.id)

        try:
            admin_member = await app.get_chat_member(chat_id, admin.id)
            if admin_member.status not in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ] and admin.id != OWNER_ID:
                await callback_query.answer("⛔ Only admins can do this!", show_alert=True)
                return
        except Exception:
            if admin.id != OWNER_ID:
                await callback_query.answer("⛔ Permission check failed.", show_alert=True)
                return

        if action == "ban":
            try:
                target = await app.get_users(user_id)
                first_name = getattr(target, 'first_name', None) or str(user_id)
            except Exception:
                first_name = str(user_id)

            try:
                await app.ban_chat_member(chat_id, user_id)
                await callback_query.edit_message_caption(
                    f"🚫 **Banned by admin**\n"
                    f"👤 User: [{first_name}](tg://user?id={user_id}) (`{user_id}`)\n"
                    f"👮 Admin: {admin_name} (`{admin.id}`)\n"
                )
                fire_log(
                    "🚫 Banned (NSFW DP — Admin Action)",
                    first_name, user_id, chat_id,
                    f"Admin {admin_name} reviewed and banned for suspicious DP"
                )
                await callback_query.answer("✅ User banned!", show_alert=True)
            except Exception as e:
                await callback_query.answer(f"❌ Ban failed: {e}", show_alert=True)

        elif action == "clear":
            try:
                target = await app.get_users(user_id)
                first_name = getattr(target, 'first_name', None) or str(user_id)
            except Exception:
                first_name = str(user_id)

            await callback_query.edit_message_caption(
                f"✅ **Cleared by admin — Not NSFW**\n"
                f"👤 User: [{first_name}](tg://user?id={user_id}) (`{user_id}`)\n"
                f"👮 Admin: {admin_name} (`{admin.id}`)\n"
            )
            await callback_query.answer("✅ Cleared!", show_alert=True)

    except Exception as e:
        print(f"❌ dp_review_callback error: {e}")
        await callback_query.answer("❌ Error processing action.", show_alert=True)


# ============================================
# 🎙️ GET VC PARTICIPANTS
# ============================================
async def get_vc_participants(chat_id):
    try:
        call = await get_cached_call(chat_id)
        if not call:
            return set(), set(), set()
        result = await app.invoke(
            GetGroupCall(call=call, limit=500)
        )
        user_ids = set()
        channel_ids = set()
        video_users = set()

        for p in result.participants:
            if hasattr(p.peer, 'user_id'):
                uid = p.peer.user_id
                user_ids.add(uid)
                if p.video or p.presentation:
                    video_users.add(uid)
            elif hasattr(p.peer, 'channel_id'):
                channel_ids.add(p.peer.channel_id)

        return user_ids, channel_ids, video_users
    except Exception as e:
        invalidate_call_cache(chat_id)
        print(f"❌ Get VC error: {e}")
        return set(), set(), set()

# ============================================
# 🔇 MUTE IN VC
# ============================================
async def mute_in_vc(chat_id, user_id):
    for attempt in range(3):
        try:
            call = await get_cached_call(chat_id)
            if not call:
                return False
            user_peer = await app.resolve_peer(user_id)
            await app.invoke(
                EditGroupCallParticipant(
                    call=call,
                    participant=user_peer,
                    muted=True
                )
            )
            print(f"🔇 Muted: {user_id}")
            if chat_id not in muted_in_vc:
                muted_in_vc[chat_id] = set()
            muted_in_vc[chat_id].add(user_id)
            return True
        except Exception as e:
            if "PARTICIPANT_JOIN_MISSING" in str(e):
                await asyncio.sleep(1)
            else:
                invalidate_call_cache(chat_id)
                print(f"❌ Mute error: {e}")
                return False
    return False

# ============================================
# 🔊 UNMUTE IN VC
# ============================================
async def unmute_in_vc(chat_id, user_id):
    for attempt in range(3):
        try:
            call = await get_cached_call(chat_id)
            if not call:
                return False
            user_peer = await app.resolve_peer(user_id)
            await app.invoke(
                EditGroupCallParticipant(
                    call=call,
                    participant=user_peer,
                    muted=False
                )
            )
            print(f"🔊 Unmuted: {user_id}")
            if chat_id in muted_in_vc:
                muted_in_vc[chat_id].discard(user_id)
            return True
        except Exception as e:
            if "PARTICIPANT_JOIN_MISSING" in str(e):
                await asyncio.sleep(1)
            else:
                invalidate_call_cache(chat_id)
                print(f"❌ Unmute error: {e}")
                return False
    return False

# ============================================
# ✅ CHECK IF USER IS REAL MEMBER
# ============================================
async def is_real_member(chat_id, user_id):
    try:
        member = await app.get_chat_member(chat_id, user_id)
        status = member.status
        print(f"📊 Live Status: {status}")

        if status in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]:
            save_group_member(user_id, chat_id)
            return True

        if status == enums.ChatMemberStatus.RESTRICTED:
            if getattr(member, 'is_member', False):
                save_group_member(user_id, chat_id)
                return True
            else:
                remove_group_member(user_id, chat_id)
                return False

        remove_group_member(user_id, chat_id)
        return False

    except Exception as e:
        if "USER_NOT_PARTICIPANT" in str(e):
            remove_group_member(user_id, chat_id)
            return False
        print(f"❌ Status check error: {e}")
        return is_known_member(user_id, chat_id)

# ============================================
# ⚡ INSTANT UNMUTE
# ============================================
async def instant_unmute_if_in_vc(chat_id, user_id, first_name, source):
    print(f"⚡ [{source}] {first_name} ({user_id}) joined group!")
    save_group_member(user_id, chat_id)

    current_vc = vc_members.get(chat_id, set())
    in_vc = user_id in current_vc
    is_muted = user_id in muted_in_vc.get(chat_id, set())

    print(f"🔍 In VC: {in_vc} | Muted: {is_muted}")

    if in_vc:
        print(f"🔊 {first_name} in VC — unmuting instantly!")
        success = await unmute_in_vc(chat_id, user_id)
        if success:
            await send_log(
                f"🔊 Auto Unmuted ({source})",
                first_name, user_id, chat_id,
                "Joined group while sitting in VC"
            )
    else:
        print(f"ℹ️ {first_name} not in VC")

# ============================================
# 🔄 HANDLER 1: on_chat_member_updated
# ============================================
@app.on_chat_member_updated()
async def on_member_update(client, update):
    try:
        chat_id = update.chat.id
        if chat_id not in ALLOWED_GROUPS:
            return
        if not update.new_chat_member:
            return

        new_status = update.new_chat_member.status
        user = update.new_chat_member.user
        user_id = user.id
        first_name = getattr(user, 'first_name', None) or str(user_id)

        print(f"🔄 [EVENT] {first_name} → {new_status}")

        if new_status not in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER
        ]:
            return

        if user_id == OWNER_ID:
            return

        await instant_unmute_if_in_vc(
            chat_id, user_id, first_name, "Event"
        )

    except Exception as e:
        print(f"❌ Member update error: {e}")

# ============================================
# 🔄 HANDLER 2: new_chat_members message
# ============================================
@app.on_message(filters.new_chat_members)
async def handle_new_group_member(client, message):
    chat_id = message.chat.id
    if chat_id not in ALLOWED_GROUPS:
        return

    for new_member in message.new_chat_members:
        user_id = new_member.id
        first_name = new_member.first_name or str(user_id)

        if user_id == OWNER_ID:
            continue

        print(f"👥 [MSG] {first_name} joined group")
        await instant_unmute_if_in_vc(
            chat_id, user_id, first_name, "Message"
        )

# ============================================
# 🔊 ADMIN UNMUTE COMMAND
# ============================================
@app.on_message(filters.command("unmute") & filters.group)
async def admin_unmute(client, message):
    chat_id = message.chat.id
    if chat_id not in ALLOWED_GROUPS:
        return

    try:
        sender = await app.get_chat_member(chat_id, message.from_user.id)
        if sender.status not in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER
        ] and message.from_user.id != OWNER_ID:
            return
    except Exception:
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("↩️ Reply to the user's message and use /unmute")
        return

    target = message.reply_to_message.from_user
    user_id = target.id
    first_name = target.first_name or str(user_id)

    video_muted.get(chat_id, set()).discard(user_id)

    success = await unmute_in_vc(chat_id, user_id)
    if success:
        await message.reply(f"🔊 **{first_name}** has been unmuted by admin.")
        await send_log(
            "🔊 Admin Unmuted",
            first_name, user_id, chat_id,
            f"Manually unmuted by {message.from_user.first_name} ({message.from_user.id})"
        )
    else:
        await message.reply(f"⚠️ Could not unmute **{first_name}** — they may not be in VC.")

@app.on_message(filters.left_chat_member)
async def handle_left_group_member(client, message):
    chat_id = message.chat.id
    if chat_id not in ALLOWED_GROUPS:
        return
    user_id = message.left_chat_member.id
    first_name = message.left_chat_member.first_name or str(user_id)
    print(f"👋 Left group: {first_name} ({user_id})")
    remove_group_member(user_id, chat_id)

# ============================================
# 🖼️ BACKGROUND DP CHECK
# ============================================
async def _background_dp_check(chat_id, user_id, first_name):
    try:
        if SIGHTENGINE_USER == "YOUR_API_USER":
            return

        is_suspicious, score, label = await check_dp_nsfw(user_id)
        if is_suspicious:
            print(f"🚨 Suspicious DP: {first_name} ({user_id}) score={score} label={label}")
            await send_dp_review(chat_id, user_id, first_name, score, label)
        else:
            print(f"✅ DP clean: {first_name} ({user_id}) score={score}")
    except Exception as e:
        print(f"❌ _background_dp_check error {user_id}: {e}")

# ============================================
# 🤖 HANDLE VC JOIN
# ============================================
async def handle_vc_join(chat_id, user_id):
    try:
        if user_id == OWNER_ID:
            return

        try:
            user_info = await app.get_users(user_id)
            first_name = getattr(user_info, 'first_name', None) or str(user_id)
            bio = getattr(user_info, 'bio', '') or ''
        except Exception:
            first_name = str(user_id)
            bio = ''

        print(f"👤 VC Join: {first_name} ({user_id})")

        try:
            member = await app.get_chat_member(chat_id, user_id)
            if member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER
            ]:
                print(f"⏭️ Admin — skipping: {first_name}")
                save_group_member(user_id, chat_id)
                return
        except Exception:
            pass

        if has_group_link(bio):
            await mute_in_vc(chat_id, user_id)
            warns = add_warning(user_id, chat_id, first_name)
            if warns >= 3:
                await app.ban_chat_member(chat_id, user_id)
                await app.unban_chat_member(chat_id, user_id)
                reset_warnings(user_id, chat_id)
                await send_log("🚫 Kicked", first_name, user_id,
                    chat_id, "Group link in bio — 3 warnings",
                    warns=warns)
            else:
                await send_log(f"⚠️ Warned ({warns}/3)",
                    first_name, user_id, chat_id,
                    "Group link in bio", warns=warns)
            return

        asyncio.create_task(_background_dp_check(chat_id, user_id, first_name))

        member_ok = await is_real_member(chat_id, user_id)

        if member_ok:
            print(f"🔊 Member — unmuting: {first_name}")
            await unmute_in_vc(chat_id, user_id)
        else:
            print(f"🔇 Not a member — muting: {first_name}")
            await mute_in_vc(chat_id, user_id)
            await send_log("🔇 VC Muted", first_name, user_id,
                chat_id, "User is not a group member")

    except Exception as e:
        print(f"❌ handle_vc_join error {user_id}: {e}")

# ============================================
# 📢 HANDLE CHANNEL JOINING VC
# ============================================
async def handle_channel_vc_join(chat_id, channel_id):
    try:
        peer_chat_id = int(f"-100{channel_id}")
        channel_name = str(channel_id)

        try:
            channel_info = await app.get_chat(peer_chat_id)
            channel_name = channel_info.title or channel_name
        except Exception:
            channel_info = None

        print(f"📢 Channel joined VC: {channel_name} ({channel_id})")

        if channel_id in ALLOWED_CHANNELS:
            print(f"⏭️ Channel {channel_name} is whitelisted — skipping!")
            return

        banned = False
        kicked = False

        try:
            await app.ban_chat_member(chat_id, peer_chat_id)
            banned = True
            print(f"🚫 Banned channel from group: {channel_name}")
        except Exception as e:
            print(f"⚠️ Ban failed (not a group member?): {e}")

        try:
            call = await get_cached_call(chat_id)
            if call:
                from pyrogram.raw.types import InputPeerChannel
                channel_peer = InputPeerChannel(
                    channel_id=channel_id,
                    access_hash=0
                )
                try:
                    channel_peer = await app.resolve_peer(peer_chat_id)
                except Exception:
                    pass

                await app.invoke(
                    EditGroupCallParticipant(
                        call=call,
                        participant=channel_peer,
                        muted=True,
                        volume=0,
                    )
                )
                await asyncio.sleep(0.5)
                await app.invoke(
                    EditGroupCallParticipant(
                        call=call,
                        participant=channel_peer,
                        muted=True,
                    )
                )
                kicked = True
                print(f"👢 Kicked channel from VC: {channel_name}")
        except Exception as e:
            print(f"⚠️ VC kick error: {e}")

        if banned and kicked:
            action = "🚫 Channel Banned + Kicked from VC"
            reason = "Channel account joined Voice Chat — banned from group and removed from VC"
        elif banned:
            action = "🚫 Channel Banned from Group"
            reason = "Channel account joined Voice Chat — banned from group (VC kick failed)"
        elif kicked:
            action = "👢 Channel Kicked from VC"
            reason = "Channel account joined Voice Chat — not a group member, removed from VC only"
        else:
            action = "⚠️ Channel Detected in VC (action failed)"
            reason = "Channel account joined Voice Chat — both ban and kick failed"

        await send_log(action, f"📢 {channel_name}", channel_id, chat_id, reason)

    except Exception as e:
        print(f"❌ handle_channel_vc_join error {channel_id}: {e}")

# ============================================
# 📷 HANDLE CAMERA / SCREENSHARE
# ============================================
async def handle_video_screenshare(chat_id, user_id):
    try:
        if user_id == OWNER_ID:
            return

        try:
            user_info = await app.get_users(user_id)
            first_name = getattr(user_info, 'first_name', None) or str(user_id)
        except Exception:
            first_name = str(user_id)

        print(f"📷 {first_name} ({user_id}) turned on camera/screenshare — muting!")
        success = await mute_in_vc(chat_id, user_id)
        if success:
            if chat_id not in video_muted:
                video_muted[chat_id] = set()
            video_muted[chat_id].add(user_id)
            await send_log(
                "🔇 Muted (Camera/Screenshare)",
                first_name, user_id, chat_id,
                "User turned on camera or screen share in VC — only admin can unmute"
            )

    except Exception as e:
        print(f"❌ handle_video_screenshare error {user_id}: {e}")

# ============================================
# 🎙️ VC POLLING — 2 seconds
# ============================================
async def poll_vc():
    print("🎙️ VC Polling started!")
    for chat_id in ALLOWED_GROUPS:
        initial_users, initial_channels, initial_video = await get_vc_participants(chat_id)
        vc_members[chat_id] = initial_users
        vc_channels[chat_id] = initial_channels
        vc_video_users[chat_id] = initial_video
        muted_in_vc[chat_id] = set()
        print(f"📌 Startup: {len(initial_users)} users, {len(initial_channels)} channels in VC")

    while True:
        try:
            for chat_id in ALLOWED_GROUPS:
                current_ids, current_channels, current_video = await get_vc_participants(chat_id)

                previous_ids = vc_members.get(chat_id, set())
                new_joiners = current_ids - previous_ids
                left_vc = previous_ids - current_ids

                for uid in left_vc:
                    muted_in_vc.get(chat_id, set()).discard(uid)
                    vc_video_users.get(chat_id, set()).discard(uid)
                    video_muted.get(chat_id, set()).discard(uid)

                for user_id in new_joiners:
                    print(f"🆕 New VC joiner: {user_id}")
                    asyncio.create_task(handle_vc_join(chat_id, user_id))

                vc_members[chat_id] = current_ids

                previous_channels = vc_channels.get(chat_id, set())
                new_channels = current_channels - previous_channels

                for channel_id in new_channels:
                    print(f"📢 Channel joined VC: {channel_id} — banning!")
                    asyncio.create_task(handle_channel_vc_join(chat_id, channel_id))

                vc_channels[chat_id] = current_channels

                previous_video = vc_video_users.get(chat_id, set())
                new_video = current_video - previous_video

                for user_id in new_video:
                    print(f"📷 Video/screenshare detected: {user_id}")
                    asyncio.create_task(handle_video_screenshare(chat_id, user_id))

                vc_video_users[chat_id] = current_video

        except Exception as e:
            print(f"❌ Poll error: {e}")

        await asyncio.sleep(2)

# ============================================
# 🔄 MUTED USER POLLER — checks every 3s
# ============================================
async def poll_muted_users():
    print("🔄 Muted-user poller started!")
    while True:
        try:
            for chat_id in ALLOWED_GROUPS:
                muted_set = muted_in_vc.get(chat_id, set()).copy()
                for user_id in muted_set:
                    if user_id in video_muted.get(chat_id, set()):
                        continue

                    if is_known_member(user_id, chat_id):
                        try:
                            user_info = await app.get_users(user_id)
                            first_name = getattr(user_info, 'first_name', None) or str(user_id)
                        except Exception:
                            first_name = str(user_id)
                        print(f"🔄 [POLL] {first_name} ({user_id}) is now a member — unmuting!")
                        success = await unmute_in_vc(chat_id, user_id)
                        if success:
                            await send_log(
                                "🔊 Auto Unmuted (Poll)",
                                first_name, user_id, chat_id,
                                "Joined group while sitting in VC (detected by poller)"
                            )
                        continue

                    try:
                        member = await app.get_chat_member(chat_id, user_id)
                        status = member.status
                        is_restricted_member = (
                            status == enums.ChatMemberStatus.RESTRICTED
                            and getattr(member, 'is_member', False)
                        )
                        if status in [
                            enums.ChatMemberStatus.MEMBER,
                            enums.ChatMemberStatus.ADMINISTRATOR,
                            enums.ChatMemberStatus.OWNER,
                        ] or is_restricted_member:
                            try:
                                user_info = await app.get_users(user_id)
                                first_name = getattr(user_info, 'first_name', None) or str(user_id)
                            except Exception:
                                first_name = str(user_id)
                            print(f"🔄 [POLL] {first_name} ({user_id}) became a member — unmuting!")
                            save_group_member(user_id, chat_id)
                            success = await unmute_in_vc(chat_id, user_id)
                            if success:
                                await send_log(
                                    "🔊 Auto Unmuted (Poll)",
                                    first_name, user_id, chat_id,
                                    "Joined group while sitting in VC (detected by poller)"
                                )
                    except Exception as e:
                        if "USER_NOT_PARTICIPANT" not in str(e):
                            print(f"❌ Muted poller check error for {user_id}: {e}")

        except Exception as e:
            print(f"❌ Muted poller error: {e}")

        await asyncio.sleep(3)

# ============================================
# 🚀 MAIN
# ============================================
async def main():
    await app.start()
    me = await app.get_me()
    print(f"✅ Logged in as: {me.first_name} ({me.id})")
    print(f"✅ Bot is running!")
    print(f"✅ Monitoring: {ALLOWED_GROUPS}")

    # ============================================
    # ✅ PERMANENT FIX — Load ALL dialogs first
    # This caches every chat peer including private
    # log channel — fixes "Peer id invalid" forever
    # ============================================
    print("🔄 Loading all dialogs to cache peers...")
    async for _ in app.get_dialogs():
        pass
    print("✅ All peers cached!")

    # ✅ Register group peers
    print("🔄 Registering group peers...")
    for chat_id in ALLOWED_GROUPS:
        for attempt in range(5):
            try:
                chat_info = await app.get_chat(chat_id)
                await app.invoke(
                    GetFullChannel(channel=await app.resolve_peer(chat_id))
                )
                print(f"✅ Peer registered: {chat_info.title} ({chat_id})")
                break
            except Exception as e:
                print(f"⚠️ Peer register attempt {attempt+1}/5 for {chat_id}: {e}")
                await asyncio.sleep(2)

    # ✅ Register log channel peer
    print("🔄 Registering log channel peer...")
    for attempt in range(5):
        try:
            log_chat = await app.get_chat(LOG_CHANNEL)
            print(f"✅ Log channel registered: {log_chat.title}")
            break
        except Exception as e:
            print(f"⚠️ Log channel register attempt {attempt+1}/5: {e}")
            await asyncio.sleep(2)

    try:
        await app.send_message(LOG_CHANNEL, "✅ **VC Bot Started!** Now monitoring.")
        print("✅ Startup message sent to log channel!")
    except Exception as e:
        print(f"⚠️ Startup message error: {e}")

    asyncio.create_task(poll_vc())
    asyncio.create_task(poll_muted_users())
    await asyncio.Event().wait()

init_db()
app.run(main())
