import os
import logging
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Owner and Bot Configuration
OWNER_ID = 6377845120  # Your Owner ID

# Minimal Flask app to keep Render free tier awake 24/7 (No interaction required)
app = Flask('')

@app.route('/')
def home():
    return "Bot is active and running 24/7!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# ----------------------------------------------------
# 20 Questions Game Logic & Handlers
# ----------------------------------------------------

# Store active games in memory: {chat_id: {"word": str, "question_count": int, "guesser": int}}
active_games = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and explains how to play."""
    welcome_text = (
        "🤖 **Welcome to the 20 Questions Game Bot!**\n\n"
        "How to play:\n"
        "• One player sets a secret word using `/setword [word]`.\n"
        "• Other players ask Yes/No questions to guess the word within 20 questions!\n\n"
        "Commands:\n"
        "• `/setword <word>` - Set the secret word (Admin/Private or group)\n"
        "• `/guess <word>` - Make a final guess\n"
        "• `/status` - Check current game progress\n"
        "• `/broadcast <message>` - Send a broadcast message (Owner only)"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def set_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets the secret word for the game session."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("⚠️ Please provide a secret word! Example: `/setword apple`", parse_mode="Markdown")
        return

    secret_word = context.args[0].lower()
    active_games[chat_id] = {
        "word": secret_word,
        "question_count": 0,
        "set_by": user_id
    }
    
    # Delete the command message for privacy if possible, then notify
    await update.message.reply_text(
        "🤫 **Secret word has been set!**\n"
        "The game of 20 Questions has begun. Ask your Yes/No questions or make a guess using `/guess [word]`!",
        parse_mode="Markdown"
    )

async def make_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allows players to guess the secret word."""
    chat_id = update.effective_chat.id
    
    if chat_id not in active_games:
        await update.message.reply_text("⚠️ No active game in this chat. Ask someone to set a word using `/setword` first!", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Please provide your guess! Example: `/guess apple`", parse_mode="Markdown")
        return

    guessed_word = context.args[0].lower()
    game = active_games[chat_id]
    game["question_count"] += 1

    if guessed_word == game["word"]:
        winner = update.effective_user.first_name
        word = game["word"]
        del active_games[chat_id]  # Reset game
        await update.message.reply_text(
            f"🎉 **Correct!** {winner} guessed the secret word **'{word}'**!\n"
            f"The game has ended. Start a new one with `/setword`.",
            parse_mode="Markdown"
        )
    else:
        remaining = 20 - game["question_count"]
        if remaining <= 0:
            word = game["word"]
            del active_games[chat_id]
            await update.message.reply_text(
                f"❌ **Game Over!** You've used up all 20 questions.\n"
                f"The secret word was: **'{word}'**.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ Incorrect guess! Questions used: `{game['question_count']}/20` (Remaining: {remaining})",
                parse_mode="Markdown"
            )

async def game_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows current game status."""
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        await update.message.reply_text("No game is currently active in this chat. Use `/setword` to start one.", parse_mode="Markdown")
        return

    game = active_games[chat_id]
    await update.message.reply_text(
        f"📊 **Game Status:**\n"
        f"• Questions asked: `{game['question_count']}/20`\n"
        f"• Active game is running!",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Easy broadcast system for the Owner ID."""
    user_id = update.effective_user.id
    
    if user_id != OWNER_ID:
        await update.message.reply_text("You are not authorized to use the broadcast command.")
        return

    message_text = " ".join(context.args)
    if not message_text:
        await update.message.reply_text("Please provide a message to broadcast. Example:\n`/broadcast Hello everyone!`", parse_mode="Markdown")
        return

    await update.message.reply_text(f"📢 **Broadcast Message Ready:**\n\n{message_text}", parse_mode="Markdown")

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN environment variable not set!")
        
    application = Application.builder().token(token).build()

    # Register all command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setword", set_word))
    application.add_handler(CommandHandler("guess", make_guess))
    application.add_handler(CommandHandler("status", game_status))
    application.add_handler(CommandHandler("broadcast", broadcast))

    # Start the Flask web server thread in the background for 24/7 keep-alive
    server_thread = Thread(target=run_web)
    server_thread.daemon = True
    server_thread.start()

    # Start the bot polling loop
    logger.info("Starting bot polling and 24/7 keep-alive web server...")
    application.run_polling()

if __name__ == '__main__':
    main()

