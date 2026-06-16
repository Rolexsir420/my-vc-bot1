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
API_ID = 36047484
API_HASH = "5e3030298ba584c414678c379c837b58"
LOG_CHANNEL = "@imparthii"
OWNER_ID = 8726414299
ALLOWED_GROUPS = [-1002483433187]
ALLOWED_CHANNELS = []

# --- SIGHTENGINE (free tier: sightengine.com) ---
# Sign up free at sightengine.com → Dashboard → copy api_user and api_secret
SIGHTENGINE_USER   = "1297817509"
SIGHTENGINE_SECRET = "DfGeVrNhJQJvBBTehCXkmmgPfru47mhv"
NSFW_THRESHOLD = 0.6

app = Client("vc_moderator", api_id=API_ID, api_hash=API_HASH)
vc_members = {}
muted_in_vc = {}     # {chat_id: {user_id, ...}}
vc_channels = {}     # {chat_id: {channel_id, ...}}
vc_video_users = {}  # {chat_id: {user_id, ...}} — users with camera/screenshare ON
video_muted = {}     # {chat_id: {user_id, ...}} — muted specifically for camera/screenshare, only admin can unmute

# ============================================
# 🛡️ PYROGRAM BUG FIX
# Pyrogram 2.x crashes handle_updates() with
# "Peer id invalid" when it receives any update
# from a chat not in its session cache (e.g. a
# linked discussion group). This kills ALL event
# handlers silently — including on_chat_member_updated.
# Fix: patch resolve_peer to return None for unknown
# peers instead of raising, so handle_updates survives.
# ============================================
_original_resolve_peer = app.resolve_peer.__func__ if hasattr(app.resolve_peer, '__func__') else None

async def _safe_resolve_peer(self, peer_id):
    try:
        return await type(app).resolve_peer(self, peer_id)
    except (KeyError, ValueError) as e:
        if "Peer id invalid" in str(e) or "ID not found" in str(e):
            return None
        raise

import pyrogram.client as _pyro_client
_orig_handle_updates = _pyro_client.Client.handle_updates

async def _patched_handle_updates(self, updates):
    try:
        await _orig_handle_updates(self, updates)
    except (ValueError, KeyError) as e:
        if "Peer id invalid" in str(e) or "ID not found" in str(e):
            pass  # Silently drop — unknown chat, not our group
        else:
            raise

_pyro_client.Client.handle_updates = _patched_handle_updates
# ============================================

# ============================================
# 🚀 CALL CACHE — avoids repeated GetFullChannel
# ============================================
call_cache = {}
call_cache_time = {}
CACHE_TTL = 30  # seconds

async def get_cached_call(chat_id):
    now = asyncio.get_event_loop().time()
    if (chat_id in call_cache and
            now - call_cache_time.get(chat_id, 0) < CACHE_TTL):
        return call_cache[chat_id]
    try:
        peer = await app.resolve_peer(chat_id)
    except (KeyError, ValueError) as e:
        # Peer not in session cache yet — try get_chat to register it first
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
    now = now_ist()
    warn_line = f"⚠️ **Warnings:** `{warns}/3`\n" if warns else ""
    text = (
        f"📋 **VC BOT LOG**\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 **Time (IST):** `{now}`\n"
        f"👤 **User:** {user_name} (`{user_id}`)\n"
        f"👥 **Group:** `{chat_id}`\n"
        f"⚡ **Action:** {action}\n"
        f"📝 **Reason:** {reason}\n"
        f"{warn_line}"
        f"━━━━━━━━━━━━━━━━"
    )
    try:
                                )
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

    # ✅ RAILWAY FIX — force-register all group peers into session cache
    # Without this, resolve_peer(-100xxx) fails with "Peer id invalid"
    # because a fresh/uploaded session has no peer info for groups it
    # hasn't seen yet in THIS environment.
    print("🔄 Registering group peers...")
    for chat_id in ALLOWED_GROUPS:
        for attempt in range(5):
            try:
                chat_info = await app.get_chat(chat_id)
                # Also join the update feed for this chat
                await app.invoke(
                    GetFullChannel(channel=await app.resolve_peer(chat_id))
                )
                print(f"✅ Peer registered: {chat_info.title} ({chat_id})")
                break
            except Exception as e:
                print(f"⚠️ Peer register attempt {attempt+1}/5 for {chat_id}: {e}")
                await asyncio.sleep(2)

    try:
        chat = await app.get_chat(LOG_CHANNEL)
        print(f"✅ Log channel: {chat.title}")
