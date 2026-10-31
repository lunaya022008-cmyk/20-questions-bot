#!/usr/bin/env python3
"""
Production-Grade Telegram Multiplayer "20 Questions" Game Bot
Single-file implementation using python-telegram-bot v20+ and SQLite.
"""

import asyncio
import html
import logging
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import telegram
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
BOT_TOKEN = "9215739739:AAEZyIWNBgKLHEkviCLVHyndbJlplQpUBMY"
OWNER_ID = 6377845120
DEVELOPER_HANDLE = "@notyrluffy"
DB_PATH = "data/database.db"

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==========================================
# DATABASE MANAGER
# ==========================================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.init_db()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    games_played INTEGER DEFAULT 0,
                    games_won INTEGER DEFAULT 0,
                    games_losses INTEGER DEFAULT 0,
                    questions_asked INTEGER DEFAULT 0,
                    correct_guesses INTEGER DEFAULT 0,
                    incorrect_guesses INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_type TEXT,
                    title TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_history (
                    broadcast_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    message_text TEXT,
                    targets INTEGER,
                    success INTEGER,
                    failed INTEGER,
                    blocked INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.commit()

    def upsert_user(self, user_id: int, first_name: str, username: Optional[str]):
        with self.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username = excluded.username
            """,
                (user_id, first_name, username or ""),
            )
            conn.commit()

    def upsert_chat(self, chat_id: int, chat_type: str, title: Optional[str]):
        with self.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO chats (chat_id, chat_type, title) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type = excluded.chat_type,
                    title = excluded.title
            """,
                (chat_id, chat_type, title or "Private/Unknown"),
            )
            conn.commit()

    def record_game_completion(
        self,
        participants: List[int],
        winner_id: Optional[int],
        questions_stats: Dict[int, int],
        guesses_stats: Dict[int, Dict[str, int]],
    ):
        with self.get_conn() as conn:
            for uid in participants:
                won = 1 if uid == winner_id else 0
                lost = 0 if uid == winner_id else 1
                q_asked = questions_stats.get(uid, 0)
                c_guess = guesses_stats.get(uid, {}).get("correct", 0)
                i_guess = guesses_stats.get(uid, {}).get("incorrect", 0)

                conn.execute(
                    """
                    UPDATE users SET
                        games_played = games_played + 1,
                        games_won = games_won + ?,
                        games_losses = games_losses + ?,
                        questions_asked = questions_asked + ?,
                        correct_guesses = correct_guesses + ?,
                        incorrect_guesses = incorrect_guesses + ?
                    WHERE user_id = ?
                """,
                    (won, lost, q_asked, c_guess, i_guess, uid),
                )
            conn.commit()

    def get_user_profile(self, user_id: int):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            return cursor.fetchone()

    def get_leaderboard(self):
        with self.get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT *, 
                CASE WHEN games_played > 0 THEN (CAST(games_won AS FLOAT) / games_played) * 100 ELSE 0 END as win_rate
                FROM users 
                ORDER BY games_won DESC, win_rate DESC, games_played DESC 
                LIMIT 10
            """
            )
            return cursor.fetchall()

    def get_all_broadcast_targets(self):
        with self.get_conn() as conn:
            users = [row["user_id"] for row in conn.execute("SELECT user_id FROM users").fetchall()]
            chats = [row["chat_id"] for row in conn.execute("SELECT chat_id FROM chats").fetchall()]
            return list(set(users + chats))

    def log_broadcast(self, owner_id: int, text: str, targets: int, success: int, failed: int, blocked: int):
        with self.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO broadcast_history (owner_id, message_text, targets, success, failed, blocked)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (owner_id, text, targets, success, failed, blocked),
            )
            conn.commit()


db = Database(DB_PATH)


# ==========================================
# GAME SESSION & STATE MACHINE
# ==========================================
class GameSession:
    def __init__(self, chat_id: int, host_creator_id: int):
        self.chat_id = chat_id
        self.host_id: Optional[int] = None
        self.host_name: Optional[str] = None
        self.secret_word: Optional[str] = None
        self.players: Dict[str, dict] = {}
        self.status = "CREATED"
        self.question_counter = 0
        self.timer_minutes = 1
        self.deadline = 0.0
        self.pending_question: Optional[dict] = None
        self.qa_history: List[dict] = []
        self.lobby_msg_id: Optional[int] = None
        self.timer_task: Optional[asyncio.Task] = None
        self.pending_word_user_id: Optional[int] = None
        self.lock = asyncio.Lock()
        self.questions_asked_tracker: Dict[int, int] = {}
        self.guesses_tracker: Dict[int, Dict[str, int]] = {}


class SessionManager:
    def __init__(self):
        self.sessions: Dict[int, GameSession] = {}

    def get_session(self, chat_id: int) -> Optional[GameSession]:
        return self.sessions.get(chat_id)

    def create_session(self, chat_id: int, user_id: int) -> GameSession:
        session = GameSession(chat_id, user_id)
        self.sessions[chat_id] = session
        return session

    def remove_session(self, chat_id: int):
        if chat_id in self.sessions:
            session = self.sessions[chat_id]
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            del self.sessions[chat_id]


sessions = SessionManager()


# ==========================================
# HELPERS & FORMATTING
# ==========================================
def format_user_mention(user_id: int, name: str) -> str:
    safe_name = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


DIVIDER = "━━━━━━━━━━━━━━━━━━"


# ==========================================
# COMMAND & HANDLER REGISTRATION
# ==========================================
async def setup_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Initialize bot & main menu"),
        BotCommand("startgame", "Create a new multiplayer 20 Questions lobby"),
        BotCommand("cancelgame", "Terminate active game lobby"),
        BotCommand("endgame", "Force end active game"),
        BotCommand("qm", "Ask a Yes/No question (e.g. /qm Is it living?)"),
        BotCommand("guess", "Submit secret word guess (e.g. /guess Apple)"),
        BotCommand("recap", "View complete Q&A history"),
        BotCommand("gamerecap", "View complete Q&A history"),
        BotCommand("overview", "Display your player profile"),
        BotCommand("leaderboard", "Top 10 players leaderboard"),
        BotCommand("help", "Interactive help menu"),
    ]
    await application.bot.set_my_commands(commands)


# ==========================================
# COMMAND HANDLERS: START & HELP
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    db.upsert_user(user.id, user.first_name, user.username)
    db.upsert_chat(chat.id, chat.type, getattr(chat, "title", None))

    if context.args and context.args[0].startswith("word_"):
        if chat.type != "private":
            await update.message.reply_text("Please open this link in our private chat!")
            return
        try:
            target_chat_id = int(context.args[0].split("_")[1])
            session = sessions.get_session(target_chat_id)
            if session and session.host_id == user.id and session.status == "WAITING_FOR_WORD":
                session.pending_word_user_id = user.id
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Submission", callback_data="cancel_word")]])
                await update.message.reply_text(
                    f"🔒 <b>Secret Word Submission</b>\n{DIVIDER}\n"
                    "Please reply to this chat with your secret word now.\n"
                    "<i>Make sure no one else is looking at your screen!</i>",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            else:
                await update.message.reply_text("❌ This game session is no longer waiting for a secret word.")
                return
        except Exception:
            await update.message.reply_text("❌ Invalid deep link payload.")
            return

    if chat.type != "private":
        await update.message.reply_text(
            f"👑 <b>20 Questions Multiplayer Bot</b>\n{DIVIDER}\n"
            "Welcome! Use /startgame to create a new game lobby in this group.",
            parse_mode="HTML",
        )
        return

    welcome_text = (
        f"🤖 <b>20 Questions Multiplayer Bot</b>\n{DIVIDER}\n"
        "Welcome to the ultimate cyberpunk-styled multiplayer deduction game on Telegram!\n\n"
        "Choose an option below to explore gameplay rules, command references, or developer information."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎮 How to Play", callback_data="menu_how_to_play"),
                InlineKeyboardButton("📜 Commands", callback_data="menu_commands"),
            ],
            [
                InlineKeyboardButton("👑 Developer", callback_data="menu_developer"),
                InlineKeyboardButton("❌ Close", callback_data="menu_close"),
            ],
        ]
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if user:
        db.upsert_user(user.id, user.first_name, user.username)
    if chat:
        db.upsert_chat(chat.id, chat.type, getattr(chat, "title", None))

    help_text = (
        f"📚 <b>Interactive Help & Guide</b>\n{DIVIDER}\n"
        "Navigate through our guides below to master the game:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎮 How to Play", callback_data="menu_how_to_play"),
                InlineKeyboardButton("📜 Commands", callback_data="menu_commands"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Main", callback_data="menu_main"),
                InlineKeyboardButton("❌ Close", callback_data="menu_close"),
            ],
        ]
    )
    if update.message:
        await update.message.reply_text(help_text, parse_mode="HTML", reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode="HTML", reply_markup=keyboard)


# ==========================================
# CALLBACK: MENU NAVIGATIONS
# ==========================================
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_close":
        await query.message.delete()
        return

    if data == "menu_main":
        text = (
            f"🤖 <b>20 Questions Multiplayer Bot</b>\n{DIVIDER}\n"
            "Welcome to the ultimate cyberpunk-styled multiplayer deduction game on Telegram!\n\n"
            "Choose an option below to explore gameplay rules, command references, or developer information."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎮 How to Play", callback_data="menu_how_to_play"),
                    InlineKeyboardButton("📜 Commands", callback_data="menu_commands"),
                ],
                [
                    InlineKeyboardButton("👑 Developer", callback_data="menu_developer"),
                    InlineKeyboardButton("❌ Close", callback_data="menu_close"),
                ],
            ]
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "menu_how_to_play":
        text = (
            f"🎮 <b>How to Play the 20 Questions Game</b>\n{DIVIDER}\n"
            "<b>Step 1:</b> First, use the /startgame command to initiate the game.\n"
            "<b>Step 2:</b> A player must host the game by selecting the Host Game option.\n"
            "<b>Step 3:</b> After hosting the game, the host must go to the bot's DM and submit the secret word.\n"
            "<b>Step 4:</b> The host must then select the lobby closing time to decide when players can no longer join.\n"
            "<b>Step 5:</b> Once the lobby closing time is set, start the game.\n"
            "<b>Step 6:</b> After the game starts, all participating players can ask questions related to the secret word using the /qm command.\n"
            "<b>Step 7:</b> The host must answer every question with either Yes or No so that players can narrow down their guesses.\n"
            "<b>Step 8:</b> If any player thinks they have figured out the secret word, they can submit their answer using the /guess command followed by the word. Example: <code>/guess Apple</code>.\n\n"
            "The main objective is to figure out the secret word by asking a maximum of 20 questions that can be answered with Yes or No. The first player to guess the correct word wins the game!"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📜 Commands", callback_data="menu_commands"),
                    InlineKeyboardButton("🔙 Back", callback_data="menu_main"),
                ],
                [InlineKeyboardButton("❌ Close", callback_data="menu_close")],
            ]
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "menu_commands":
        text = (
            f"📜 <b>Command Reference Guide</b>\n{DIVIDER}\n"
            "• /start - Initialize bot & main menu\n"
            "• /startgame - Create a new multiplayer lobby\n"
            "• /cancelgame / /endgame - Terminate active game\n"
            "• /qm [question] - Ask a Yes/No question\n"
            "• /guess [word] - Submit secret word guess\n"
            "• /recap / /gamerecap - View full Q&A history\n"
            "• /overview - Display your player profile\n"
            "• /leaderboard - Top 10 players leaderboard\n"
            "• /help - Interactive help menu\n"
            "• /broadcast - Owner announcement panel"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎮 How to Play", callback_data="menu_how_to_play"),
                    InlineKeyboardButton("🔙 Back", callback_data="menu_main"),
                ],
                [InlineKeyboardButton("❌ Close", callback_data="menu_close")],
            ]
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "menu_developer":
        text = (
            f"👑 <b>Developer Information</b>\n{DIVIDER}\n"
            f"• <b>Developer / Owner:</b> {DEVELOPER_HANDLE}\n"
            "• <b>Architecture:</b> Python 3, python-telegram-bot v20+, asyncio, SQLite\n"
            "• <b>Design:</b> High-performance production-grade multiplayer game loop engineered for Termux."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
                [InlineKeyboardButton("❌ Close", callback_data="menu_close")],
            ]
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "cancel_word":
        user_id = query.from_user.id
        for session in sessions.sessions.values():
            if session.pending_word_user_id == user_id:
                session.pending_word_user_id = None
        await query.message.edit_text("❌ Secret word submission cancelled.")
        return


# ==========================================
# STAGE A & B & C & D: LOBBY & HOST FLOW
# ==========================================
async def startgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    db.upsert_user(user.id, user.first_name, user.username)
    db.upsert_chat(chat.id, chat.type, getattr(chat, "title", None))

    if chat.type == "private":
        await update.message.reply_text("❌ /startgame can only be executed inside a group chat!")
        return

    if sessions.get_session(chat.id):
        await update.message.reply_text("⚠️ A game session is already active in this group! Use /cancelgame to terminate it first.")
        return

    session = sessions.create_session(chat.id, user.id)
    session.status = "CREATED"

    text = (
        f"🎮 <b>20 Questions Game Lobby</b>\n{DIVIDER}\n"
        "Status: <b>Lobby Initializing</b>\n"
        "👑 Host: <b>Not selected</b>\n"
        "👥 Players: <b>0 / 10</b>\n{DIVIDER}\n"
        "Click below to claim the host seat and start configuring the game!"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👑 Become Host", callback_data="become_host"),
                InlineKeyboardButton("🎮 How to Play", callback_data="menu_how_to_play"),
            ]
        ]
    )
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    session.lobby_msg_id = msg.message_id


async def become_host_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    session = sessions.get_session(chat.id)

    if not session or session.status != "CREATED":
        await query.answer("❌ This lobby is no longer accepting hosts.", show_alert=True)
        return

    async with session.lock:
        if session.host_id is not None:
            await query.answer("❌ Host is already assigned!", show_alert=True)
            return

        session.host_id = user.id
        session.host_name = user.first_name
        session.players[str(user.id)] = {"name": user.first_name, "username": user.username}
        session.status = "WAITING_FOR_WORD"

    bot_username = context.bot.username
    deep_link = f"https://t.me/{bot_username}?start=word_{chat.id}"

    text = (
        f"🎮 <b>20 Questions Game Lobby</b>\n{DIVIDER}\n"
        f"Status: <b>Waiting for Secret Word (DM)</b>\n"
        f"👑 Host: {format_user_mention(user.id, user.first_name)}\n"
        f"👥 Players: <b>1 / 10 (Host registered)</b>\n{DIVIDER}\n"
        "<i>Host has been assigned! Please check your private messages with the bot to submit the secret word.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📥 Send Secret Word in DM", url=deep_link)]]
    )

    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        pass

    await query.answer("👑 You are now the host! Check your DMs with the bot.", show_alert=True)


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        return

    target_session = None
    for session in sessions.sessions.values():
        if session.pending_word_user_id == user.id and session.host_id == user.id and session.status == "WAITING_FOR_WORD":
            target_session = session
            break

    if not target_session:
        return

    word = update.message.text.strip()
    if not word or len(word) > 40:
        await update.message.reply_text("❌ Invalid secret word. Please enter a valid word or short phrase (max 40 characters).")
        return

    target_session.secret_word = word.lower()
    target_session.pending_word_user_id = None
    target_session.status = "WAITING_FOR_TIMER"

    await update.message.reply_text(
        f"✅ <b>Secret Word Confirmed!</b>\n{DIVIDER}\n"
        f"Your secret word <code>{html.escape(word)}</code> has been safely stored in memory.\n"
        "Returning to group chat to select lobby duration...",
        parse_mode="HTML",
    )

    try:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⏱️ 1 Minute", callback_data="timer_1"),
                    InlineKeyboardButton("⏱️ 2 Minutes", callback_data="timer_2"),
                    InlineKeyboardButton("⏱️ 3 Minutes", callback_data="timer_3"),
                ]
            ]
        )
        timer_msg = await context.bot.send_message(
            chat_id=target_session.chat_id,
            text=(
                f"⏱️ <b>Lobby Duration Selection</b>\n{DIVIDER}\n"
                f"Host {format_user_mention(target_session.host_id, target_session.host_name)}, please select how long the lobby will remain open for players to join:"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        target_session.lobby_msg_id = timer_msg.message_id
    except Exception as e:
        logger.error(f"Failed to send timer selection: {e}")


async def timer_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    session = sessions.get_session(chat.id)

    if not session or session.status != "WAITING_FOR_TIMER":
        await query.answer("❌ Invalid action or outdated lobby.", show_alert=True)
        return

    if session.host_id != user.id:
        await query.answer("❌ Only the assigned host can select the lobby duration!", show_alert=True)
        return

    data = query.data
    minutes = 1
    if data == "timer_2":
        minutes = 2
    elif data == "timer_3":
        minutes = 3

    session.timer_minutes = minutes
    session.deadline = time.time() + (minutes * 60)
    session.status = "LOBBY_OPEN"

    await publish_lobby_panel(context, session)
    await query.answer(f"⏱️ Lobby timer set to {minutes} minute(s)!")

    session.timer_task = context.application.create_task(lobby_timer_task(context, session.chat_id, minutes * 60))


async def publish_lobby_panel(context: ContextTypes.DEFAULT_TYPE, session: GameSession):
    players_count = len(session.players)
    host_mention = format_user_mention(session.host_id, session.host_name) if session.host_id else "Not assigned"
    time_left = max(0, int(session.deadline - time.time()))
    mins, secs = divmod(time_left, 60)

    text = (
        f"🎮 <b>20 Questions Game Lobby</b>\n{DIVIDER}\n"
        f"Status: <b>Lobby Open (Join Now!)</b>\n"
        f"👑 Host: {host_mention}\n"
        f"⏱️ Time Remaining: <b>{mins:02d}:{secs:02d}</b>\n"
        f"👥 Players: <b>{players_count} / 10</b>\n{DIVIDER}\n"
        "Click 'Join Game' below to participate!"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎮 Join Game", callback_data="join_game"),
                InlineKeyboardButton("👥 Players List", callback_data="players_list"),
            ],
            [
                InlineKeyboardButton("🚪 Leave Game", callback_data="leave_game"),
                InlineKeyboardButton("🚀 Start Game Now", callback_data="start_game_now"),
            ],
        ]
    )

    try:
        await context.bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.lobby_msg_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        pass


async def lobby_timer_task(context: ContextTypes.DEFAULT_TYPE, chat_id: int, duration_seconds: int):
    await asyncio.sleep(duration_seconds)
    session = sessions.get_session(chat_id)
    if session and session.status == "LOBBY_OPEN":
        await start_game_execution(context, session)


async def lobby_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    session = sessions.get_session(chat.id)

    if not session or session.status != "LOBBY_OPEN":
        await query.answer("❌ This game lobby is closed or not active.", show_alert=True)
        return

    data = query.data

    if data == "join_game":
        if user.id == session.host_id:
            await query.answer("❌ Host is already registered in the game!", show_alert=True)
            return
        if str(user.id) in session.players:
            await query.answer("⚠️ You are already in the game lobby!", show_alert=True)
            return
        if len(session.players) >= 10:
            await query.answer("❌ Game lobby is full (10/10 players)!", show_alert=True)
            return

        async with session.lock:
            session.players[str(user.id)] = {"name": user.first_name, "username": user.username}

        await query.answer("✅ Successfully joined the game!", show_alert=True)
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👤 {format_user_mention(user.id, user.first_name)} joined the game! (Total Players: {len(session.players)}/10)",
            parse_mode="HTML",
        )
        await publish_lobby_panel(context, session)
        return

    if data == "leave_game":
        if user.id == session.host_id:
            await query.answer("❌ The host cannot leave the game lobby!", show_alert=True)
            return
        if str(user.id) not in session.players:
            await query.answer("❌ You are not in this lobby!", show_alert=True)
            return

        async with session.lock:
            del session.players[str(user.id)]

        await query.answer("🚪 You have left the game lobby.", show_alert=True)
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🚪 {format_user_mention(user.id, user.first_name)} left the game. (Total Players: {len(session.players)}/10)",
            parse_mode="HTML",
        )
        await publish_lobby_panel(context, session)
        return

    if data == "players_list":
        players_text = f"👥 <b>Current Players ({len(session.players)}/10)</b>\n{DIVIDER}\n"
        for i, (uid_str, pdata) in enumerate(session.players.items(), 1):
            players_text += f"{i}. {format_user_mention(int(uid_str), pdata['name'])}\n"
        await query.answer(players_text, show_alert=True)
        return

    if data == "start_game_now":
        if user.id != session.host_id:
            await query.answer("❌ Only the host can start the game manually!", show_alert=True)
            return
        if len(session.players) < 2:
            await query.answer("❌ You need at least 1 participating player (besides host) to start!", show_alert=True)
            return

        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()

        await start_game_execution(context, session)
        await query.answer("🚀 Game started!")
        return


async def start_game_execution(context: ContextTypes.DEFAULT_TYPE, session: GameSession):
    session.status = "RUNNING"
    host_mention = format_user_mention(session.host_id, session.host_name)

    text = (
        f"🎮 <b>20 Questions Game Started!</b>\n{DIVIDER}\n"
        f"👑 Host: {host_mention}\n"
        f"👥 Total Players: <b>{len(session.players)}</b>\n"
        f"🔒 Secret word is securely locked in memory.\n{DIVIDER}\n"
        "<b>How to play:</b>\n"
        "• Participating players can ask Yes/No questions using: <code>/qm [question]</code>\n"
        "• Submit your final guess using: <code>/guess [word]</code>\n"
        "• Maximum 20 questions allowed!\n"
        "• Use /recap anytime to view complete Q&A history."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 View Recap", callback_data="inline_recap")]]
    )
    try:
        await context.bot.send_message(
            chat_id=session.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        pass


# ==========================================
# STAGE E & F: GAMEPLAY, QUESTIONS & GUESSES
# ==========================================
async def qm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    session = sessions.get_session(chat.id)
    if not session or session.status != "RUNNING":
        await update.message.reply_text("❌ No active 20 Questions game is running in this group.")
        return

    if user.id == session.host_id:
        await update.message.reply_text("❌ The host cannot ask questions! You are answering them.")
        return

    if str(user.id) not in session.players:
        await update.message.reply_text("❌ You are not a registered participant in this game.")
        return

    if session.question_counter >= 20:
        await update.message.reply_text("❌ Maximum question limit (20/20) has been reached! You can still submit guesses using /guess [word].")
        return

    if session.pending_question is not None:
        await update.message.reply_text("⏳ Please wait for the host to answer the current pending question before asking a new one.")
        return

    question_text = " ".join(context.args).strip()
    if not question_text:
        await update.message.reply_text("❌ Please provide a question! Example: <code>/qm Is it an animal?</code>", parse_mode="HTML")
        return

    if len(question_text) > 250:
        await update.message.reply_text("❌ Question is too long! Please keep it under 250 characters.")
        return

    session.question_counter += 1
    q_num = session.question_counter
    session.questions_asked_tracker[user.id] = session.questions_asked_tracker.get(user.id, 0) + 1

    host_mention = format_user_mention(session.host_id, session.host_name)
    user_mention = format_user_mention(user.id, user.first_name)

    q_display = (
        f"❓ <b>Question [{q_num}/20]</b>\n{DIVIDER}\n"
        f"<b>Asked by:</b> {user_mention}\n"
        f"<b>Question:</b> {html.escape(question_text)}\n"
        f"<b>To Host:</b> {host_mention}\n{DIVIDER}\n"
        "<i>Host, please answer below:</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ YES", callback_data=f"ans_yes_{q_num}"),
                InlineKeyboardButton("❌ NO", callback_data=f"ans_no_{q_num}"),
            ]
        ]
    )

    msg = await update.message.reply_text(q_display, parse_mode="HTML", reply_markup=keyboard)

    session.pending_question = {
        "q_id": q_num,
        "user_id": user.id,
        "name": user.first_name,
        "text": question_text,
        "msg_id": msg.message_id,
    }


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    session = sessions.get_session(chat.id)

    if not session or session.status != "RUNNING":
        await query.answer("❌ Game is not running.", show_alert=True)
        return

    if user.id != session.host_id:
        await query.answer("❌ Only the assigned host can answer questions!", show_alert=True)
        return

    if not session.pending_question:
        await query.answer("❌ There is no pending question.", show_alert=True)
        return

    data = query.data
    answer_str = "✅ YES" if "yes" in data else "❌ NO"
    pq = session.pending_question

    session.qa_history.append(
        {
            "q_num": pq["q_id"],
            "user_id": pq["user_id"],
            "name": pq["name"],
            "question": pq["text"],
            "answer": answer_str,
        }
    )

    user_mention = format_user_mention(pq["user_id"], pq["name"])
    updated_text = (
        f"❓ <b>Question [{pq['q_id']}/20] (Answered)</b>\n{DIVIDER}\n"
        f"<b>Asked by:</b> {user_mention}\n"
        f"<b>Question:</b> {html.escape(pq['text'])}\n"
        f"<b>Answer:</b> {answer_str}"
    )

    try:
        await query.message.edit_text(updated_text, parse_mode="HTML", reply_markup=None)
    except Exception:
        pass

    session.pending_question = None
    await query.answer(f"Answer recorded: {answer_str}")

    if session.question_counter >= 20:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"⚠️ <b>20 Questions Limit Reached!</b>\n{DIVIDER}\nNo more questions can be asked. Players can still submit guesses using <code>/guess [word]</code>!",
            parse_mode="HTML",
        )


async def guess_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    session = sessions.get_session(chat.id)
    if not session or session.status != "RUNNING":
        await update.message.reply_text("❌ No active game session is currently running.")
        return

    if user.id == session.host_id:
        await update.message.reply_text("❌ The host cannot submit guesses!")
        return

    if str(user.id) not in session.players:
        await update.message.reply_text("❌ You are not a registered participant in this game.")
        return

    guess_word = " ".join(context.args).strip().lower()
    if not guess_word:
        await update.message.reply_text("❌ Please provide your guess! Example: <code>/guess Apple</code>", parse_mode="HTML")
        return

    if user.id not in session.guesses_tracker:
        session.guesses_tracker[user.id] = {"correct": 0, "incorrect": 0}

    if guess_word == session.secret_word:
        session.guesses_tracker[user.id]["correct"] += 1
        session.status = "ENDED"
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()

        winner_mention = format_user_mention(user.id, user.first_name)
        host_mention = format_user_mention(session.host_id, session.host_name)

        victory_text = (
            f"🏆 <b>VICTORY! Correct Guess!</b> 🏆\n{DIVIDER}\n"
            f"🎉 <b>Winner:</b> {winner_mention}\n"
            f"🔒 <b>Secret Word:</b> <code>{html.escape(session.secret_word)}</code>\n"
            f"👑 <b>Host:</b> {host_mention}\n"
            f"❓ <b>Total Questions Answered:</b> {session.question_counter}/20\n{DIVIDER}\n"
            "<i>Congratulations! The game has concluded successfully.</i>"
        )

        participant_ids = [session.host_id] + [int(uid) for uid in session.players.keys()]
        db.record_game_completion(
            participants=participant_ids,
            winner_id=user.id,
            questions_stats=session.questions_asked_tracker,
            guesses_stats=session.guesses_tracker,
        )

        await update.message.reply_text(victory_text, parse_mode="HTML")
        sessions.remove_session(chat.id)
    else:
        session.guesses_tracker[user.id]["incorrect"] += 1
        user_mention = format_user_mention(user.id, user.first_name)
        await update.message.reply_text(
            f"❌ {user_mention}, your guess <code>{html.escape(guess_word)}</code> is incorrect! Keep guessing.",
            parse_mode="HTML",
        )


# ==========================================
# STAGE G: RECAP SYSTEM
# ==========================================
async def recap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    session = sessions.get_session(chat.id)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        chat = query.message.chat
        session = sessions.get_session(chat.id)

    if not session:
        text = "❌ No active or recent game session found in this chat."
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.message.reply_text(text)
        return

    if not session.qa_history and not session.pending_question:
        text = f"📊 <b>Game Recap</b>\n{DIVIDER}\nNo questions have been asked yet."
        if update.message:
            await update.message.reply_text(text, parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.message.reply_text(text, parse_mode="HTML")
        return

    recap_lines = [f"📊 <b>Complete Game Recap ({len(session.qa_history)} answered)</b>\n{DIVIDER}"]
    for entry in session.qa_history:
        asker_mention = format_user_mention(entry["user_id"], entry["name"])
        q_text = html.escape(entry["question"])
        ans = entry["answer"]
        recap_lines.append(f"<b>Q[{entry['q_num']}]:</b> {q_text}\n(Asked by {asker_mention})\n<b>Answer:</b> {ans}\n")

    if session.pending_question:
        pq = session.pending_question
        asker_mention = format_user_mention(pq["user_id"], pq["name"])
        recap_lines.append(f"<b>Q[{pq['q_id']}]:</b> {html.escape(pq['text'])}\n(Asked by {asker_mention})\n<b>Answer:</b> ⏳ Pending Answer")

    full_text = "\n".join(recap_lines)

    if len(full_text) > 4000:
        chunks = [full_text[i:i + 4000] for i in range(0, len(full_text), 4000)]
        for chunk in chunks:
            if update.message:
                await update.message.reply_text(chunk, parse_mode="HTML")
            else:
                await update.callback_query.message.reply_text(chunk, parse_mode="HTML")
    else:
        if update.message:
            await update.message.reply_text(full_text, parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.message.reply_text(full_text, parse_mode="HTML")


# ==========================================
# STAGE H: PLAYER STATISTICS & LEADERBOARD
# ==========================================
async def overview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    db.upsert_user(user.id, user.first_name, user.username)
    profile = db.get_user_profile(user.id)

    if not profile:
        await update.message.reply_text("❌ Profile not found.")
        return

    played = profile["games_played"]
    won = profile["games_won"]
    lost = profile["games_losses"]
    q_asked = profile["questions_asked"]
    c_guess = profile["correct_guesses"]
    i_guess = profile["incorrect_guesses"]
    win_rate = (won / played * 100) if played > 0 else 0.0

    user_mention = format_user_mention(user.id, user.first_name)
    text = (
        f"📊 <b>Player Profile Overview</b>\n{DIVIDER}\n"
        f"👤 <b>Player:</b> {user_mention}\n"
        f"🎮 <b>Games Played:</b> {played}\n"
        f"🏆 <b>Victories:</b> {won}\n"
        f"❌ <b>Losses:</b> {lost}\n"
        f"📈 <b>Win Rate:</b> {win_rate:.1f}%\n"
        f"❓ <b>Questions Asked:</b> {q_asked}\n"
        f"✅ <b>Correct Guesses:</b> {c_guess}\n"
        f"❌ <b>Incorrect Guesses:</b> {i_guess}\n{DIVIDER}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = db.get_leaderboard()
    if not leaders:
        await update.message.reply_text("🏆 No leaderboard data available yet.")
        return

    text = f"🏆 <b>Top 10 Leaderboard</b>\n{DIVIDER}\n"
    for i, row in enumerate(leaders, 1):
        name = row["first_name"] or "Player"
        mention = format_user_mention(row["user_id"], name)
        wins = row["games_won"]
        played = row["games_played"]
        rate = row["win_rate"]
        text += f"<b>{i}.</b> {mention} — 🏆 <b>{wins}</b> wins ({played} played, {rate:.1f}% win rate)\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ==========================================
# STAGE I: OWNER-ONLY BROADCAST SYSTEM
# ==========================================
broadcast_sessions: Dict[int, dict] = {}


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if user.id != OWNER_ID:
        await update.message.reply_text("❌ Unauthorized. This command is restricted to the bot owner.")
        return

    if chat.type != "private":
        await update.message.reply_text("❌ Please use the /broadcast command in private chat with the bot.")
        return

    text = (
        f"📢 <b>Broadcast Management Dashboard</b>\n{DIVIDER}\n"
        "Manage and send announcements to all registered users and group chats.\n"
        "Choose an option below:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Send Broadcast", callback_data="bc_start")],
            [InlineKeyboardButton("📊 Broadcast Statistics", callback_data="bc_stats")],
            [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")],
        ]
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def broadcast_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if user.id != OWNER_ID:
        await query.answer("❌ Unauthorized.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "bc_cancel":
        broadcast_sessions.pop(user.id, None)
        await query.message.edit_text("❌ Broadcast session cancelled.")
        return

    if data == "bc_stats":
        with db.get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM broadcast_history")
            row = cursor.fetchone()
            total_bcs = row["total"] if row else 0
        text = (
            f"📊 <b>Broadcast Statistics</b>\n{DIVIDER}\n"
            f"• Total Broadcasts Sent: <b>{total_bcs}</b>\n"
            f"• Target Reach: All registered users and group chats."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="bc_menu")]])
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "bc_menu":
        text = (
            f"📢 <b>Broadcast Management Dashboard</b>\n{DIVIDER}\n"
            "Manage and send announcements to all registered users and group chats.\n"
            "Choose an option below:"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📢 Send Broadcast", callback_data="bc_start")],
                [InlineKeyboardButton("📊 Broadcast Statistics", callback_data="bc_stats")],
                [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")],
            ]
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "bc_start":
        broadcast_sessions[user.id] = {"state": "WAITING_FOR_TEXT"}
        await query.message.edit_text(
            f"📢 <b>Send Broadcast</b>\n{DIVIDER}\n"
            "Please reply to this chat with the announcement text you wish to broadcast.",
            parse_mode="HTML",
        )
        return

    if data == "bc_confirm":
        session_data = broadcast_sessions.get(user.id)
        if not session_data or "text" not in session_data:
            await query.message.edit_text("❌ Broadcast session expired.")
            return

        msg_text = session_data["text"]
        targets = db.get_all_broadcast_targets()
        success = 0
        failed = 0
        blocked = 0

        status_msg = await query.message.edit_text(
            f"📢 Broadcasting in progress...\nTargets: {len(targets)}", parse_mode="HTML"
        )

        for chat_id in targets:
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="HTML")
                success += 1
                await asyncio.sleep(0.05)
            except telegram.error.Forbidden:
                blocked += 1
            except Exception:
                failed += 1

        db.log_broadcast(user.id, msg_text, len(targets), success, failed, blocked)
        broadcast_sessions.pop(user.id, None)

        report = (
            f"✅ <b>Broadcast Completed Successfully!</b>\n{DIVIDER}\n"
            f"• Total Targets: <b>{len(targets)}</b>\n"
            f"• Successful Deliveries: <b>{success}</b>\n"
            f"• Blocked / Inaccessible: <b>{blocked}</b>\n"
            f"• Failed Deliveries: <b>{failed}</b>\n{DIVIDER}"
        )
        await status_msg.edit_text(report, parse_mode="HTML")
        return


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private" or user.id != OWNER_ID:
        return

    session_data = broadcast_sessions.get(user.id)
    if not session_data or session_data.get("state") != "WAITING_FOR_TEXT":
        return

    text = update.message.text
    session_data["text"] = text
    session_data["state"] = "CONFIRM"

    preview = (
        f"📢 <b>Broadcast Preview</b>\n{DIVIDER}\n"
        f"{html.escape(text)}\n{DIVIDER}\n"
        "Do you want to confirm and send this broadcast?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm & Send", callback_data="bc_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel"),
            ]
        ]
    )
    await update.message.reply_text(preview, parse_mode="HTML", reply_markup=keyboard)


# ==========================================
# STAGE L: GAME TERMINATION & CANCEL HANDLERS
# ==========================================
async def cancelgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    session = sessions.get_session(chat.id)
    if not session:
        await update.message.reply_text("❌ No active game session found in this chat.")
        return

    if user.id != session.host_id and user.id != OWNER_ID:
        member = await chat.get_member(user.id)
        if member.status not in ["administrator", "creator"]:
            await update.message.reply_text("❌ Only the game host or group administrators can cancel the game.")
            return

    session.status = "CANCELLED"
    if session.timer_task and not session.timer_task.done():
        session.timer_task.cancel()

    sessions.remove_session(chat.id)
    await update.message.reply_text(f"🛑 Game session has been cancelled by {format_user_mention(user.id, user.first_name)}.", parse_mode="HTML")


async def endgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cancelgame_command(update, context)


# ==========================================
# GLOBAL ERROR HANDLER & TEXT DISPATCHER
# ==========================================
async def text_message_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    db.upsert_user(user.id, user.first_name, user.username)
    db.upsert_chat(chat.id, chat.type, getattr(chat, "title", None))

    if chat.type == "private":
        if user.id == OWNER_ID and user.id in broadcast_sessions:
            await handle_broadcast_text(update, context)
            return
        for session in sessions.sessions.values():
            if session.pending_word_user_id == user.id:
                await handle_private_text(update, context)
                return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


# ==========================================
# MAIN APPLICATION ENTRY POINT
# ==========================================
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please configure your BOT_TOKEN in bot.py!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("startgame", startgame_command))
    application.add_handler(CommandHandler("cancelgame", cancelgame_command))
    application.add_handler(CommandHandler("endgame", endgame_command))
    application.add_handler(CommandHandler("qm", qm_command))
    application.add_handler(CommandHandler("guess", guess_command))
    application.add_handler(CommandHandler("recap", recap_command))
    application.add_handler(CommandHandler("gamerecap", recap_command))
    application.add_handler(CommandHandler("overview", overview_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))

    application.add_handler(CallbackQueryHandler(become_host_callback, pattern="^become_host$"))
    application.add_handler(CallbackQueryHandler(timer_select_callback, pattern="^timer_[123]$"))
    application.add_handler(CallbackQueryHandler(lobby_actions_callback, pattern="^(join_game|leave_game|players_list|start_game_now)$"))
    application.add_handler(CallbackQueryHandler(answer_callback, pattern="^ans_(yes|no)_\\d+$"))
    application.add_handler(CallbackQueryHandler(recap_command, pattern="^inline_recap$"))
    application.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^(menu_.*|cancel_word|bc_.*)$"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_dispatcher))
    application.add_error_handler(error_handler)

    async def post_init(app: Application):
        await setup_bot_commands(app)
        logger.info("Bot successfully initialized and commands registered.")

    application.post_init = post_init

    print("🤖 Starting 20 Questions Multiplayer Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
