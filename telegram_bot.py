import os
import logging
import requests
from datetime import datetime
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask, request
from threading import Thread

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# States for conversation handler
WAITING_FOR_KEY = 0

# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
DB_NAME = os.getenv('DB_NAME', 'codemycode')
COLLECTION_NAME = os.getenv('COLLECTION_NAME', 'codemycode')

# Telegram Bot Token
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# App URL (will be set when deployed to Render)
APP_URL = os.getenv('APP_URL', 'http://localhost:8080')

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

# Create Flask app
app = Flask(__name__)

@app.route('/')
def index():
    return 'Bot is running!'

@app.route('/health')
def health():
    return {'status': 'ok', 'timestamp': datetime.now().isoformat()}, 200

# Configure the Telegram bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hi {user.first_name}! Welcome to the Image Delivery Bot.\n\n"
        "Please enter your activation key to access your images:"
    )
    return WAITING_FOR_KEY

async def validate_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the activation key against MongoDB."""
    activation_key = update.message.text.strip()
    context.user_data['activation_key'] = activation_key
    
    # Check if key exists in database
    user_doc = collection.find_one({"access_key": activation_key})
    
    if user_doc:
        context.user_data['user_id'] = str(user_doc['_id'])
        context.user_data['user_name'] = user_doc.get('name', 'User')
        context.user_data['user_data'] = user_doc
        
        # Initialize message tracking for image cleanup
        context.user_data['image_message_ids'] = []
        
        # Save the last update timestamp
        context.user_data['last_updated'] = datetime.now()
        
        keyboard = [
            [InlineKeyboardButton("Get Today's Images", callback_data='get_images')],
            [InlineKeyboardButton("Refresh Images", callback_data='refresh_images')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"Welcome {context.user_data['user_name']}! Your activation key has been validated.\n"
            "What would you like to do?",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Invalid activation key. Please try again or contact support."
        )
        return WAITING_FOR_KEY

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'get_images':
        await get_images(update, context)
    elif query.data == 'refresh_images':
        await refresh_images(update, context)
    elif query.data == 'logout':
        await end_session(update, context)

async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """End the current user session."""
    query = update.callback_query
    
    # Clear all user data
    context.user_data.clear()
    
    # Clear previous images if any
    await clear_previous_images(context, query.message.chat_id)
    
    # Update message to confirm logout
    await query.edit_message_text(
        "You have been logged out. Use /start to login again."
    )

async def clear_previous_images(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Clear previously sent images to keep the chat clean."""
    if 'image_message_ids' in context.user_data and context.user_data['image_message_ids']:
        for msg_id in context.user_data['image_message_ids']:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.warning(f"Could not delete message {msg_id}: {e}")
        
        # Reset the list after deletion
        context.user_data['image_message_ids'] = []

async def get_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and send images to the user."""
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if 'activation_key' not in context.user_data:
        await query.edit_message_text(
            "Your session has expired. Please use /start to begin again."
        )
        return
    
    # Refresh user data
    user_doc = collection.find_one({"access_key": context.user_data['activation_key']})
    
    if not user_doc:
        await query.edit_message_text(
            "Your activation key is no longer valid. Please contact support."
        )
        return
    
    # Check if user has image links field
    image_links = user_doc.get('image_links', [])
    
    if not image_links:
        await query.edit_message_text(
            "No images found for your account. Please contact support or try again later.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh Images", callback_data='refresh_images')]
            ])
        )
        return
    
    # Get today's date
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Filter images for today if they have date information
    today_images = []
    for img in image_links:
        if isinstance(img, dict) and 'date' in img and img['date'] == today:
            today_images.append(img['url'])
        elif isinstance(img, str):
            # If it's just a string URL, include it
            today_images.append(img)
    
    if not today_images:
        await query.edit_message_text(
            "No images available for today. Check back later!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh Images", callback_data='refresh_images')]
            ])
        )
        return
    
    # Send a status message
    status_message = await query.edit_message_text(f"Sending {len(today_images)} images...")
    
    # Clear previous images before sending new ones
    await clear_previous_images(context, chat_id)
    
    # Send each image and track message IDs
    new_message_ids = []
    for i, img_url in enumerate(today_images):
        try:
            # Download the image
            response = requests.get(img_url)
            response.raise_for_status()
            
            # Send the image
            sent_message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=response.content,
                caption=f"Image {i+1}/{len(today_images)}"
            )
            # Track the message ID for potential future deletion
            new_message_ids.append(sent_message.message_id)
        except Exception as e:
            logger.error(f"Error sending image {img_url}: {e}")
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"Failed to retrieve image {i+1}: {str(e)}"
            )
            new_message_ids.append(error_msg.message_id)
    
    # Store new message IDs for future cleanup
    context.user_data['image_message_ids'] = new_message_ids
    
    # Update completion message with buttons
    keyboard = [
                    [InlineKeyboardButton("Get Today's Images", callback_data='get_images')],
                    [InlineKeyboardButton("Refresh Images", callback_data='refresh_images')],
                    [InlineKeyboardButton("Logout", callback_data='logout')]  # Add logout button
                ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
    control_message = await context.bot.send_message(
        chat_id=chat_id,
        text="All images delivered! Use the buttons below for more options.",
        reply_markup=reply_markup
    )

async def refresh_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh and check for new images in the database."""
    query = update.callback_query
    chat_id = query.message.chat_id
    
    if 'activation_key' not in context.user_data:
        await query.edit_message_text(
            "Your session has expired. Please use /start to begin again."
        )
        return
    
    # Get fresh data from database
    user_doc = collection.find_one({"access_key": context.user_data['activation_key']})
    
    if not user_doc:
        await query.edit_message_text(
            "Your activation key is no longer valid. Please contact support."
        )
        return
    
    # Update user data
    old_user_data = context.user_data.get('user_data', {})
    context.user_data['user_data'] = user_doc
    
    # Compare image lists
    old_images = old_user_data.get('image_links', [])
    new_images = user_doc.get('image_links', [])
    
    # Check if there are new images or changes
    if len(new_images) != len(old_images) or new_images != old_images:
        await query.edit_message_text(
            "Found new or updated images! Fetching updates..."
        )
        # Clear old images and get new ones
        await clear_previous_images(context, chat_id)
        await get_images(update, context)
    else:
        # Send a message that no new images were found
        keyboard = [
                    [InlineKeyboardButton("Get Today's Images", callback_data='get_images')],
                    [InlineKeyboardButton("Refresh Images", callback_data='refresh_images')],
                    [InlineKeyboardButton("Logout", callback_data='logout')]  # Add logout button
                ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "No new images found. You're up to date!",
            reply_markup=reply_markup
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "How to use this bot:\n\n"
        "1. Start with /start and enter your activation key\n"
        "2. Once validated, use the buttons to get images\n"
        "3. 'Get Today's Images' will show all images for today\n"
        "4. 'Refresh Images' will check for new updates\n\n"
        "If you need assistance, please contact support."
    )

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /logout command."""
    # Clear all user data
    context.user_data.clear()
    
    # Clear previous images if any
    await clear_previous_images(context, update.effective_chat.id)
    
    await update.message.reply_text(
        "You have been logged out. Use /start to login again."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "Operation cancelled. Use /start to begin again."
    )
    return ConversationHandler.END

def keep_alive():
    """Function to keep the server alive by pinging itself."""
    while True:
        try:
            # Make a request to the health endpoint
            requests.get(f"https://bot-yjrr.onrender.com/health")
            logger.info(f"Keep-alive ping sent to https://bot-yjrr.onrender.com/health")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
        
        # Sleep for 14 minutes (less than the 15-minute Render sleep timeout)
        time.sleep(840)  # 14 minutes = 840 seconds

def run_bot():
    """Start the bot in a separate thread."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()
    
    # Add conversation handler for activation key
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, validate_key)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("logout", logout_command))
    
    # Start the Bot
    application.run_polling()

def run_flask():
    """Run the Flask app."""
    # Get port from environment variable for Render
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Start the bot in a separate thread
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Start the keep-alive mechanism in a separate thread
    keep_alive_thread = Thread(target=keep_alive)
    keep_alive_thread.daemon = True
    keep_alive_thread.start()
    
    # Run Flask app in the main thread
    run_flask()
