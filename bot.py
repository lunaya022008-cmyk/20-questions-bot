import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot Configuration
OWNER_ID = 6377845120
active_games = {}  # chat_id: {"word": str, "question_count": int, "setter": str}

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎮 Play 20 Questions", callback_data="help_play")],
        [InlineKeyboardButton("📢 Channel / Updates", url="https://t.me/")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🤖 **Welcome to the 20 Questions Game Bot!**\n\n"
        "How to play:\n"
        "• One player sets a secret word using `/setword [word]`.\n"
        "• Other players ask Yes/No questions or guess the word within 20 questions!\n\n"
        "Commands:\n"
        "• `/setword <word>` - Set the secret word\n"
        "• `/guess <word>` - Make a final guess\n"
        "• `/status` - Check current game progress\n"
        "• `/broadcast <message>` - Send a broadcast message (Owner only)"
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

# /setword command
async def setword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if not context.args:
        await update.message.reply_text("⚠️ Please specify a word! Example: `/setword apple`", parse_mode="Markdown")
        return
        
    word = " ".join(context.args).lower()
    active_games[chat_id] = {
        "word": word,
        "question_count": 0,
        "setter": user.first_name
    }
    
    keyboard = [[InlineKeyboardButton("🎯 Make a Guess", callback_data="make_guess_prompt")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🤫 **Secret word has been set by {user.first_name}!**\n"
        f"The game of 20 Questions has begun. Ask your Yes/No questions or make a guess using `/guess [word]`!",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# /guess command
async def guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in active_games:
        await update.message.reply_text("⚠️ No active game in this chat! Set a word first using `/setword [word]`.", parse_mode="Markdown")
        return
        
    if not context.args:
        await update.message.reply_text("⚠️ Please provide your guess! Example: `/guess apple`", parse_mode="Markdown")
        return
        
    guessed_word = " ".join(context.args).lower()
    game = active_games[chat_id]
    
    if guessed_word == game["word"]:
        winner = user.first_name
        del active_games[chat_id]
        await update.message.reply_text(f"🎉 **Correct!** {winner} guessed the secret word **'{game['word']}'** correctly!", parse_mode="Markdown")
    else:
        game["question_count"] += 1
        remaining = 20 - game["question_count"]
        if remaining <= 0:
            word_reveal = game["word"]
            del active_games[chat_id]
            await update.message.reply_text(f"❌ **Game Over!** Out of 20 questions/guesses. The secret word was **'{word_reveal}'**.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ **Incorrect guess!** Questions/Attempts remaining: **{remaining}/20**", parse_mode="Markdown")

# /status command
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        await update.message.reply_text("ℹ️ No active game running right now. Use `/setword [word]` to start one!", parse_mode="Markdown")
    else:
        game = active_games[chat_id]
        remaining = 20 - game["question_count"]
        await update.message.reply_text(
            f"📊 **Current Game Status:**\n"
            f"• Word Setter: {game['setter']}\n"
            f"• Attempts/Questions Used: {game['question_count']}/20\n"
            f"• Remaining: {remaining}",
            parse_mode="Markdown"
        )

# /broadcast command (Owner only)
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return
        
    if not context.args:
        await update.message.reply_text("⚠️ Provide a message to broadcast. Example: `/broadcast Hello everyone!`")
        return
        
    msg = " ".join(context.args)
    await update.message.reply_text(f"📢 **Broadcast Message:**\n\n{msg}", parse_mode="Markdown")

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("Error: BOT_TOKEN environment variable not set!")
        return

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setword", setword))
    application.add_handler(CommandHandler("guess", guess))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("broadcast", broadcast))

    logger.info("Bot is starting polling...")
    application.run_polling()

if __name__ == "__main__":
    main()

