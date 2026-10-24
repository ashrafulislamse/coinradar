import logging
import requests
import time
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.colors as mcolors
import os
import json
import sqlite3
import numpy as np
from datetime import datetime, timedelta, time as dt_time
import sys
import shutil
import re
import atexit
import pytz
from typing import Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, ChatMember
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler, CallbackContext
from telegram.error import BadRequest
from decimal import Decimal, InvalidOperation
from typing import Union
from threading import Timer



# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Set the level to INFO or higher to reduce verbosity
)
logger = logging.getLogger(__name__)



# API keys are loaded from environment variables (comma-separated fallback lists).
# NEVER hardcode keys in source. Set e.g. CMC_API_KEYS="key1,key2"
def _load_keys(env_name: str, service: str) -> list:
    raw = os.environ.get(env_name, "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        logger.warning("No API key set for %s (env %s) — related features disabled.", service, env_name)
    return keys

API_KEYS = {
    'cmc': _load_keys('CMC_API_KEYS', 'coinmarketcap'),
    'whale': _load_keys('WHALE_API_KEYS', 'whale-alert'),
    'cc': _load_keys('CRYPTOCOMPARE_API_KEYS', 'cryptocompare'),
    'etherscan': _load_keys('ETHERSCAN_API_KEYS', 'etherscan'),
    'coinmarketcal': _load_keys('COINMARKETCAL_API_KEYS', 'coinmarketcal'),
}

current_api_key_index = {service: 0 for service in API_KEYS}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    sys.exit("Set the TELEGRAM_BOT_TOKEN environment variable before running.")

# Admin user IDs from environment (comma-separated Telegram IDs)
PRIMARY_ADMIN_USER_IDS = [i.strip() for i in os.environ.get("ADMIN_USER_IDS", "").split(",") if i.strip()]

# Define the base URLs for the APIs
COINMARKETCAP_API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
COINMARKETCAP_TOP10_API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
COINPAPRIKA_API_URL = 'https://api.coinpaprika.com/v1'
COINGECKO_API_URL = 'https://api.coingecko.com/api/v3'
CRYPTOCOMPARE_API_URL = 'https://min-api.cryptocompare.com/data'
WHALE_ALERT_API_URL = 'https://api.whale-alert.io/v1/transactions'
CRYPTOCOMPARE_NEWS_API_URL = 'https://min-api.cryptocompare.com/data/v2/news/'
CRYPTOCOMPARE_SOCIAL_TRENDS_API_URL = 'https://min-api.cryptocompare.com/data/social/coin/latest'
COINMARKETCAL_API_URL = 'https://developers.coinmarketcal.com/v1/events'



# Constants
DB_FILE = 'crypto_bot.db'  # Path to the database file
BACKUP_FOLDER = 'backups'  # Folder to store backups
restore_mode = False  # Flag to indicate if restore mode is active
restore_admin_id = None  # Store the admin who activated restore mode
RESTORE_TIMEOUT = 300  # Timeout in seconds (e.g., 5 minutes)


def init_db() -> None:
    logger.info("Initializing database...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create other tables
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY,
        is_primary INTEGER
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_symbols (
        user_id TEXT,
        symbol TEXT,
        PRIMARY KEY (user_id, symbol)
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS alerts (
        user_id TEXT,
        chat_id TEXT,
        symbol TEXT,
        price REAL,
        status TEXT DEFAULT 'Active',
        PRIMARY KEY (user_id, chat_id, symbol, price)
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS api_keys (
        service TEXT,
        api_key TEXT,
        status INTEGER,
        PRIMARY KEY (service, api_key)
    )
    ''')
    
    # Add the portfolio table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        amount DECIMAL(10, 3) NOT NULL,
        initial_price DECIMAL(10, 3) NOT NULL
    )
    ''')

    # Add the new_portfolio table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS new_portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        amount DECIMAL(10, 3) NOT NULL,
        initial_price DECIMAL(10, 3) NOT NULL,
        timestamp TEXT NOT NULL
    )
    ''')

    # Check and add 'timestamp' column to tables
    tables = ['user_symbols', 'alerts', 'portfolio']

    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'timestamp' not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN timestamp TEXT")
            cursor.execute(f"UPDATE {table} SET timestamp = datetime('now') WHERE timestamp IS NULL")

    conn.commit()
    conn.close()
    logger.info("Database initialized.")




# Function to initialize the API keys in the database
def init_api_keys() -> None:
    logger.info("Initializing API keys...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM api_keys")

    for service, keys in API_KEYS.items():
        for key in keys:
            cursor.execute("INSERT OR IGNORE INTO api_keys (service, api_key, status) VALUES (?, ?, 1)", (service, key))

    conn.commit()
    conn.close()

    load_api_keys_from_db()
    logger.info("API keys initialized.")

def load_api_keys_from_db() -> None:
    logger.info("Loading API keys from database...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT service, api_key FROM api_keys WHERE status = 1")
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        service, api_key = row
        if service in API_KEYS:
            if api_key not in API_KEYS[service]:
                API_KEYS[service].append(api_key)
        else:
            API_KEYS[service] = [api_key]
    logger.info("API keys loaded from database.")

# Function to initialize the database and API keys
def init_db_and_keys() -> None:
    init_db()
    init_api_keys()

 
# Function to save user symbols to the database
def save_user_symbols(user_id, symbols):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Fetch current symbols for the user
    cursor.execute("SELECT symbol, timestamp FROM user_symbols WHERE user_id = ? ORDER BY timestamp ASC", (user_id,))
    current_symbols = cursor.fetchall()

    # Add new symbols and prevent duplicates
    for symbol in symbols:
        # Insert new symbol with current timestamp, ignoring duplicates
        cursor.execute("INSERT OR IGNORE INTO user_symbols (user_id, symbol, timestamp) VALUES (?, ?, datetime('now'))", (user_id, symbol))

    # Fetch updated symbols count after insertion
    cursor.execute("SELECT COUNT(*) FROM user_symbols WHERE user_id = ?", (user_id,))
    symbol_count = cursor.fetchone()[0]

    # If the total count exceeds 10, delete the oldest entries
    if symbol_count > 10:
        # Calculate how many symbols need to be deleted
        symbols_to_delete = symbol_count - 10
        cursor.execute("DELETE FROM user_symbols WHERE user_id = ? AND symbol IN (SELECT symbol FROM user_symbols WHERE user_id = ? ORDER BY timestamp ASC LIMIT ?)", (user_id, user_id, symbols_to_delete))

    conn.commit()
    conn.close()


def get_user_symbols(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol FROM user_symbols WHERE user_id = ? ORDER BY timestamp ASC", (user_id,))
    symbols = [row[0] for row in cursor.fetchall()]
    conn.close()
    return symbols


# Function to save user Alerts to the database

# Function to save alert to the database
def save_alert(user_id, chat_id, symbol, price):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO alerts (user_id, chat_id, symbol, price, status) VALUES (?, ?, ?, ?, 'Active')", (user_id, chat_id, symbol, price))
    conn.commit()
    conn.close()

# Function to get all alerts from the database
def get_alerts():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, chat_id, symbol, price, status FROM alerts")
    alerts = cursor.fetchall()
    conn.close()

    alerts_dict = {'Active': {}, 'Previous': {}}
    for user_id, chat_id, symbol, price, status in alerts:
        if user_id not in alerts_dict[status]:
            alerts_dict[status][user_id] = []
        alerts_dict[status][user_id].append((chat_id, symbol, price))
    return alerts_dict


# Function to update the alert status
def update_alert_status(user_id, chat_id, symbol, price):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE alerts SET status = 'Previous' WHERE user_id = ? AND chat_id = ? AND symbol = ? AND price = ?", (user_id, chat_id, symbol, price))
    conn.commit()
    conn.close()


# Function to get database statistics
def get_database_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Fetch unique user_ids from each table and convert them to strings
    cursor.execute("SELECT DISTINCT user_id FROM user_symbols")
    user_symbols_users = {str(row[0]) for row in cursor.fetchall()}
    logger.info(f"User symbols users: {user_symbols_users}")

    cursor.execute("SELECT DISTINCT user_id FROM alerts")
    alerts_users = {str(row[0]) for row in cursor.fetchall()}
    logger.info(f"Alerts users: {alerts_users}")

    cursor.execute("SELECT DISTINCT user_id FROM portfolio")
    portfolio_users = {str(row[0]) for row in cursor.fetchall()}
    logger.info(f"Portfolio users: {portfolio_users}")

    # Combine and deduplicate user_ids to count unique users
    all_users = user_symbols_users | alerts_users | portfolio_users
    num_unique_users = len(all_users)
    logger.info(f"Unique users: {all_users}")

    # Count users with alerts
    num_alert_users = len(alerts_users)

    # Count total alerts
    cursor.execute("SELECT COUNT(*) FROM alerts")
    num_alerts = cursor.fetchone()[0]

    # Count portfolio users
    num_portfolio_users = len(portfolio_users)

    # Identify lost users
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("SELECT DISTINCT user_id FROM alerts WHERE timestamp < ?", (thirty_days_ago,))
    lost_alert_users = {str(row[0]) for row in cursor.fetchall()}

    cursor.execute("SELECT DISTINCT user_id FROM user_symbols WHERE timestamp < ?", (thirty_days_ago,))
    lost_user_symbols_users = {str(row[0]) for row in cursor.fetchall()}

    cursor.execute("SELECT DISTINCT user_id FROM portfolio WHERE timestamp < ?", (thirty_days_ago,))
    lost_portfolio_users = {str(row[0]) for row in cursor.fetchall()}

    lost_users = lost_alert_users | lost_user_symbols_users | lost_portfolio_users
    num_lost_users = len(lost_users)
    logger.info(f"Lost users: {lost_users}")

    conn.close()
    return num_unique_users, num_alert_users, num_alerts, num_portfolio_users, num_lost_users


# Portfolio Managaement  Database 
def execute_query(query, parameters=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        logger.debug(f"Executing query: {query} with parameters: {parameters}")
        cursor.execute(query, parameters)
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}. Query: {query}, Parameters: {parameters}")
        raise
    finally:
        conn.close()

def fetch_all(query, parameters=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, parameters)
    rows = cursor.fetchall()
    conn.close()
    return rows



# API Management Section

# Global cache to store the last valid API key for each service
last_valid_api_key = {}


# Function to get the current API key for a given service
def get_api_key(service: str) -> Tuple[str, int]:
    index = current_api_key_index[service]
    return API_KEYS[service][index], index

# Function to invalidate an API key for a given service
def invalidate_api_key(service: str, key: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE api_keys SET status = 0 WHERE service = ? AND api_key = ?", (service, key))
    conn.commit()
    conn.close()

    if key in API_KEYS[service]:
        API_KEYS[service].remove(key)

    if service in last_valid_api_key and last_valid_api_key[service] == key:
        last_valid_api_key[service] = None

    logger.info(f"Invalidated API key for {service}: {key}")

# Function to rotate the API key for a given service
def rotate_api_key(service: str) -> str:
    valid_keys = API_KEYS[service]
    if not valid_keys:
        logger.error(f"No valid API keys left for service: {service}")
        return None  # No valid keys left

    current_key_index = current_api_key_index.get(service, 0)
    next_index = (current_key_index + 1) % len(valid_keys)
    next_key = valid_keys[next_index]
    current_api_key_index[service] = next_index

    logger.info(f"Rotated API key for {service}, new key: {next_key}")
    return next_key


# Function to get a valid API key for a given service
def get_valid_api_key(service: str) -> Tuple[str, int]:
    if service in last_valid_api_key and last_valid_api_key[service]:
        return last_valid_api_key[service], current_api_key_index[service]

    next_key = rotate_api_key(service)
    last_valid_api_key[service] = next_key
    return next_key, current_api_key_index[service]

# Periodic synchronization function
async def periodic_sync_keys(context) -> None:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT service, api_key FROM api_keys WHERE status = 1")
    rows = cursor.fetchall()
    conn.close()

    db_keys = {service: [] for service in API_KEYS.keys()}
    for row in rows:
        service, api_key = row
        db_keys[service].append(api_key)
    
    for service in API_KEYS.keys():
        API_KEYS[service] = db_keys[service]

    logger.info("API keys have been synchronized with the database.")

class APIKeyExhaustedException(Exception):
    pass



# Admin User and Group Management Section

# Function to add a primary admin
def add_primary_admin(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (user_id, is_primary) VALUES (?, 1)", (user_id,))
    conn.commit()
    conn.close()

# Function to check if a user is a primary admin
def is_primary_admin(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT is_primary FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None and result[0] == 1


# Function to check if bot is an admin in the chat
async def is_bot_admin(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> bool:
    bot = context.bot

    # Handle the chat_id for both Update and CallbackQuery
    if isinstance(update, Update):
        chat_id = update.effective_chat.id
    elif isinstance(update, CallbackQuery):
        chat_id = update.message.chat.id  # CallbackQuery's chat id is in the message
    else:
        return False

    # Skip admin check for private chats
    if isinstance(update, Update) and update.effective_chat.type == "private":
        return True
    elif isinstance(update, CallbackQuery) and update.message.chat.type == "private":
        return True

    # Check if the bot is an admin in the group
    bot_member = await bot.get_chat_member(chat_id, bot.id)
    return bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]



# Function to notify primary admins
async def notify_primary_admins(context: ContextTypes.DEFAULT_TYPE, message: str):
    for admin_id in PRIMARY_ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send message to primary admin {admin_id}: {e}")



# Backup and Restore Section

# Function to backup the database
async def backup_database(context: ContextTypes.DEFAULT_TYPE) -> str:
    try:
        backup_file = os.path.join(BACKUP_FOLDER, f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
        
        # Create a temporary folder to store the database and API keys
        temp_folder = os.path.join(BACKUP_FOLDER, 'temp')
        if not os.path.exists(temp_folder):
            os.makedirs(temp_folder)
        
        # Backup the database file
        shutil.copy(DB_FILE, os.path.join(temp_folder, DB_FILE))
        
        # Create a zip archive of the backup
        shutil.make_archive(backup_file.replace('.zip', ''), 'zip', temp_folder)
        
        # Remove the temporary folder
        shutil.rmtree(temp_folder)
        
        # Send the backup file to primary admins
        caption_message = f"<b>Database backup completed successfully on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.</b>"
        for admin_id in PRIMARY_ADMIN_USER_IDS:
            try:
                with open(backup_file, 'rb') as f:
                    await context.bot.send_document(chat_id=admin_id, document=f, filename=os.path.basename(backup_file), caption=caption_message, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Failed to send backup file to primary admin {admin_id}: {e}")
                # Notify admin of the failure to send the backup file
                await context.bot.send_message(chat_id=admin_id, text=f"Failed to send the backup file: {e}", parse_mode="HTML")
        
        return backup_file
    
    except Exception as e:
        # Log the error and notify primary admins about the failure
        logger.error(f"Backup process failed: {e}")
        for admin_id in PRIMARY_ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"<b>Error:</b> Backup failed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. Reason: {str(e)}", parse_mode="HTML")
            except Exception as notify_error:
                logger.error(f"Failed to notify admin {admin_id}: {notify_error}")
        return None




# Function to handle the /restore command
async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global restore_mode, restore_admin_id

    user_id = str(update.message.from_user.id)
    
    # Check if the user is a primary admin to proceed with the restore
    if not is_primary_admin(user_id):
        await update.message.reply_text("You do not have permission to execute this command.")
        return
    
    # Activate restore mode
    restore_mode = True
    restore_admin_id = user_id  # Store the admin's user ID who activated restore mode
    logger.info(f"Restore mode activated by admin: {user_id}")
    
    # Send a message indicating that restore mode is activated
    message = await update.message.reply_text("Restore mode activated. Please upload the .zip backup file to restore the database.")
    
    # Start a timer to automatically deactivate restore mode after timeout
    Timer(RESTORE_TIMEOUT, deactivate_restore_mode).start()

# Function to deactivate restore mode after timeout
def deactivate_restore_mode():
    global restore_mode, restore_admin_id
    restore_mode = False
    restore_admin_id = None
    logger.info("Restore mode deactivated due to inactivity.")

# Function to handle file uploads (for restoring)
async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global restore_mode, restore_admin_id

    user_id = str(update.message.from_user.id)

    if restore_mode:
        if user_id == restore_admin_id:  # Only the admin who activated the restore can upload files
            if update.message.document and update.message.document.file_name.endswith('.zip'):
                # Proceed with the restore
                document = update.message.document
                logger.info(f"Received .zip file for restore: {document.file_name}")
                
                # File download logic here
                file = await context.bot.get_file(document.file_id)
                restore_file_path = os.path.join(BACKUP_FOLDER, f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
                logger.info(f"Downloading file to {restore_file_path}")
                await file.download_to_drive(restore_file_path)
                logger.info("File downloaded successfully")
                
                # You can add logic here to actually restore the database using the downloaded file
                await update.message.reply_text("Database restore started.")
        else:
            # If the user is not the admin who activated restore mode, ignore the message
            logger.info(f"Unauthorized file upload attempt by user {user_id}.")
            return
    else:
        # If restore mode is not active, silently ignore
        logger.info("Restore mode is not active. Ignoring file upload.")
        return



# All Functions Section

def format_dynamic_price(price_value):
    try:
        # Handle edge cases based on value ranges and remove unnecessary zeros
        if price_value < 0.0001:
            # For very small values like 0.000001 or 0.00000004
            formatted_price = f"{price_value:.8f}"
        elif price_value < 0.01:
            # For small values like 0.000209
            formatted_price = f"{price_value:.6f}"
        elif price_value < 1:
            # For values less than 1 like 0.47
            formatted_price = f"{price_value:.4f}"
        elif price_value < 1000:
            # For medium values like 200.12
            formatted_price = f"{price_value:.2f}"
        else:
            # For large values like 60000.81
            formatted_price = f"{price_value:.2f}"

        # Remove trailing zeros and unnecessary decimal points
        return formatted_price.rstrip('0').rstrip('.')
    
    except Exception as e:
        # Fallback in case of unexpected value formats
        logger.error(f"Unexpected price format: {price_value}, Error: {e}")
        return str(price_value)


# Function to get cryptocurrency price from CoinMarketCap
def get_crypto_price_cmc(symbol: str) -> dict:
    for _ in range(len(API_KEYS['cmc'])):
        api_key, index = None, None
        try:
            api_key, index = get_valid_api_key('cmc')
            headers = {
                'Accepts': 'application/json',
                'X-CMC_PRO_API_KEY': api_key,
            }
            params = {'symbol': symbol}
            
            response = requests.get(COINMARKETCAP_API_URL, headers=headers, params=params)
            data = response.json()

            # Checking for successful API response
            if data.get('status', {}).get('error_code') == 0:
                price = data['data'][symbol]['quote']['USD']['price']
                market_cap = data['data'][symbol]['quote']['USD']['market_cap']
                volume_24h = data['data'][symbol]['quote']['USD']['volume_24h']
                percent_change_1h = data['data'][symbol]['quote']['USD']['percent_change_1h']
                percent_change_24h = data['data'][symbol]['quote']['USD']['percent_change_24h']
                percent_change_7d = data['data'][symbol]['quote']['USD']['percent_change_7d']
                circulating_supply = data['data'][symbol]['circulating_supply']
                fdv = data['data'][symbol]['quote']['USD'].get('fully_diluted_market_cap', 'N/A')

                # Format the price dynamically
                formatted_price = format_dynamic_price(price)

                return {
                    'name': data['data'][symbol]['name'],
                    'price': formatted_price,  # formatted price
                    'market_cap': market_cap,
                    'volume_24h': volume_24h,
                    'percent_change_1h': percent_change_1h,
                    'percent_change_24h': percent_change_24h,
                    'percent_change_7d': percent_change_7d,
                    'circulating_supply': circulating_supply,
                    'fdv': fdv
                }
            else:
                error_message = data.get('status', {}).get('error_message', 'An unknown error occurred')
                logger.error(f"Failed to fetch data from CoinMarketCap API with key {api_key}: {error_message}")
                invalidate_api_key('cmc', api_key)
        except Exception as e:
            logger.error(f"Exception occurred while fetching data from CoinMarketCap API with key {api_key}: {e}")
            if api_key is not None:
                invalidate_api_key('cmc', api_key)
    
    return {'error': 'Failed to fetch data from CoinMarketCap API with all keys.'}


# Function to get top 10 cryptocurrencies by market cap from CoinMarketCap
def get_top10_cryptos() -> dict:
    for _ in range(len(API_KEYS['cmc'])):
        try:
            api_key, index = get_valid_api_key('cmc')
            headers = {
                'Accepts': 'application/json',
                'X-CMC_PRO_API_KEY': api_key,
            }
            params = {
                'start': '1',
                'limit': '10',
                'convert': 'USD'
            }
            response = requests.get(COINMARKETCAP_TOP10_API_URL, headers=headers, params=params)
            data = response.json()
            if data.get('status', {}).get('error_code') == 0:
                return {'data': data.get('data', [])}
            else:
                error_message = data.get('status', {}).get('error_message', 'An unknown error occurred')
                logger.error(f"Failed to fetch data from CoinMarketCap API with key {api_key}: {error_message}")
                invalidate_api_key('cmc', api_key)
        except Exception as e:
            logger.error(f"Exception occurred while fetching data from CoinMarketCap API with key {api_key}: {e}")
            invalidate_api_key('cmc', api_key)
            rotate_api_key('cmc')
    return {'error': 'Failed to fetch data from CoinMarketCap API with all keys.'}



# Function to get market cap details from CoinGecko
def get_market_cap_details() -> dict:
    try:
        response = requests.get(f'{COINGECKO_API_URL}/global')
        data = response.json()
        return {'data': data['data']}
    except requests.RequestException:
        logger.error("Failed to fetch data from CoinGecko API.")
        return {'error': 'Failed to fetch data from CoinGecko API.'}

# Function to get Ethereum gas price
def get_eth_gas_price() -> dict:
    for _ in range(len(API_KEYS['etherscan'])):
        try:
            api_key, index = get_valid_api_key('etherscan')
            response = requests.get(f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}')
            data = response.json()
            if data['status'] == '1':
                return data
            else:
                logger.info(f"Invalidating API key for etherscan due to status {data['status']}")
                invalidate_api_key('etherscan', api_key)
                rotate_api_key('etherscan')
                continue
        except requests.RequestException:
            logger.error("Failed to fetch data from Etherscan API.")
            invalidate_api_key('etherscan', api_key)
            rotate_api_key('etherscan')
            continue

    return {'error': 'Failed to fetch data from Etherscan API.'}



# Function to get historical cryptocurrency data from CryptoCompare
def get_historical_data_cmc(symbol: str, time_period: str = '7d') -> dict:
    for _ in range(len(API_KEYS['cc'])):
        api_key = get_valid_api_key('cc')
        params = {
            'fsym': symbol,
            'tsym': 'USD',
            'limit': 7,
            'api_key': api_key
        }

        if time_period == '24h':
            url = f"{CRYPTOCOMPARE_API_URL}/v2/histohour"
            params['limit'] = 24
        elif time_period == '30d':
            url = f"{CRYPTOCOMPARE_API_URL}/v2/histoday"
            params['limit'] = 30
        else:
            url = f"{CRYPTOCOMPARE_API_URL}/v2/histoday"

        try:
            response = requests.get(url, params=params)
            data = response.json()

            if data.get('Response') == 'Error':
                invalidate_api_key('cc', current_api_key_index['cc'])
                rotate_api_key('cc')
                continue

            return {'prices': data['Data']['Data']}
        except requests.RequestException:
            logger.error("Failed to fetch data from CryptoCompare API.")
            invalidate_api_key('cc', current_api_key_index['cc'])
            rotate_api_key('cc')
            continue
    
    return {'error': 'Failed to fetch data from CryptoCompare API.'}

# Function to get all-time high price from CryptoCompare
def get_all_time_high_cc(symbol: str) -> dict:
    for _ in range(len(API_KEYS['cc'])):
        api_key, index = get_valid_api_key('cc')
        url = f"{CRYPTOCOMPARE_API_URL}/v2/histoday"
        params = {
            'fsym': symbol,
            'tsym': 'USD',
            'limit': 2000,
            'api_key': api_key
        }
        try:
            response = requests.get(url, params=params)
            data = response.json()

            if data.get('Response') == 'Error':
                invalidate_api_key('cc', api_key)
                rotate_api_key('cc')
                continue

            prices = data['Data']['Data']
            ath_data = max(prices, key=lambda x: x['high'])

            return {
                'ath': ath_data['high'],
                'ath_date': datetime.utcfromtimestamp(ath_data['time']).strftime('%Y-%m-%d')
            }
        except requests.RequestException as e:
            logger.error(f"Failed to fetch data from CryptoCompare API: {e}")
            invalidate_api_key('cc', api_key)
            rotate_api_key('cc')
            continue
    
    return {'error': 'All API keys for cc are invalid or exhausted.'}


# Function to get all-time low price from CryptoCompare
def get_all_time_low_cc(symbol: str) -> dict:
    for _ in range(len(API_KEYS['cc'])):
        api_key, index = get_valid_api_key('cc')
        url = f"{CRYPTOCOMPARE_API_URL}/v2/histoday"
        params = {
            'fsym': symbol,
            'tsym': 'USD',
            'limit': 2000,
            'api_key': api_key
        }
        try:
            response = requests.get(url, params=params)
            data = response.json()

            if data.get('Response') == 'Error':
                invalidate_api_key('cc', api_key)
                rotate_api_key('cc')
                continue

            prices = data['Data']['Data']
            atl_data = min(prices, key=lambda x: x['low'])

            return {
                'atl': atl_data['low'],
                'atl_date': datetime.utcfromtimestamp(atl_data['time']).strftime('%Y-%m-%d')
            }
        except requests.RequestException:
            logger.error("Failed to fetch data from CryptoCompare API.")
            invalidate_api_key('cc', api_key)
            rotate_api_key('cc')
            continue
    
    return {'error': 'Failed to fetch data from CryptoCompare API.'}

# Function to get cryptocurrency price from CryptoCompare
def get_crypto_price_cc(symbol: str) -> dict:
    global current_cc_api_key

    for _ in range(len(API_KEYS['cc'])):
        try:
            api_key = get_valid_api_key('cc')
            headers = {
                'Accepts': 'application/json',
                'authorization': f'Apikey {api_key}',
            }
            url = f"https://min-api.cryptocompare.com/data/price?fsym={symbol}&tsyms=USD"

            response = requests.get(url, headers=headers)
            data = response.json()

            if 'USD' in data:
                return {'price': data['USD']}
            else:
                error_message = data.get('Message', 'Unknown error').lower()

                if 'apikey' in error_message or 'invalid' in error_message:
                    logger.info(f"Invalidating API key {api_key} for service cc due to error: {error_message}")
                    invalidate_api_key('cc', api_key)
                else:
                    logger.info(f"Non-API key related error for {symbol}: {error_message}")
                    break

        except requests.RequestException as e:
            logger.error(f"Request exception for API key {api_key}: {e}")
            invalidate_api_key('cc', api_key)
        except Exception as e:
            logger.error(f"Exception occurred: {e}")
            break

    return {'error': 'Failed to fetch data from CryptoCompare API.'}

# Function to get the latest whale alerts
def get_latest_whale_alerts(min_value) -> dict:
    for _ in range(len(API_KEYS['whale'])):
        try:
            api_key, index = get_valid_api_key('whale')
            url = WHALE_ALERT_API_URL
            params = {
                'api_key': api_key,
                'min_value': min_value,
                'limit': 10
            }
            response = requests.get(url, params=params)
            data = response.json()
            if 'transactions' in data:
                return data
            else:
                invalidate_api_key('whale', api_key)
                rotate_api_key('whale')
                continue
        except requests.RequestException as e:
            logger.error(f"Failed to fetch data from Whale Alert API: {e}")
            invalidate_api_key('whale', api_key)
            rotate_api_key('whale')
            continue
    return {'error': 'All API keys for Whale Alert API are invalid or exhausted.'}


# Helper function to format large numbers into readable strings
def format_number(n):
    if n is None:
        return 'N/A'
    if n >= 1_000_000_000_000:
        return f'{n / 1_000_000_000_000:.2f}T'
    elif n >= 1_000_000_000:
        return f'{n / 1_000_000_000:.2f}B'
    elif n >= 1_000_000:
        return f'{n / 1_000_000:.2f}M'
    elif n >= 1_000:
        return f'{n / 1_000:.2f}K'
    else:
        return f'{n:.2f}'

def set_tradingview_style():
    plt.style.use('dark_background')
    plt.rcParams.update({
        'figure.facecolor': '#131722',
        'axes.facecolor': '#131722',
        'axes.edgecolor': '#31363a',
        'axes.labelcolor': '#8c8c8e',
        'xtick.color': '#8c8c8e',
        'ytick.color': '#8c8c8e',
        'grid.color': '#31363a',
        'grid.linestyle': '--',
        'grid.linewidth': 0.5,
        'font.size': 12,
    })

def reset_style():
    plt.style.use('default')

# Function to sanitize exchange names by removing unwanted characters
def sanitize_name(name):
    return name.replace('.', '').replace('-', '').replace('_', '')

# Function to fetch the list of exchanges for a given coin from CoinPaprika API
def get_top_exchanges(symbol: str, top_n: int = 15, volume_threshold: float = 5000) -> list:
    try:
        # Get the coin ID from the symbol
        response = requests.get(f'{COINPAPRIKA_API_URL}/coins')
        response.raise_for_status()
        coins = response.json()

        coin_id = None
        for coin in coins:
            if coin['symbol'].upper() == symbol.upper():
                coin_id = coin['id']
                break

        if not coin_id:
            logger.warning(f"No coin ID found for symbol: {symbol}")
            return []

        # Implement a short delay to handle rate limiting
        time.sleep(1)

        # Get exchange information for the coin ID
        response = requests.get(f'{COINPAPRIKA_API_URL}/coins/{coin_id}/markets')
        response.raise_for_status()
        markets = response.json()

        # Filter to only include spot markets with volume above the threshold and sort by volume
        spot_markets = [
            {'exchange_name': market['exchange_name'], 'volume_24h': market['quotes']['USD']['volume_24h']}
            for market in markets if
            market.get('category') == 'Spot' and market['quotes']['USD']['volume_24h'] >= volume_threshold
        ]
        sorted_markets = sorted(spot_markets, key=lambda x: x['volume_24h'], reverse=True)

        # Filter to only include known legitimate exchanges
        filtered_legit_exchanges = [market for market in sorted_markets if market['exchange_name'] in LEGIT_EXCHANGES]

        # Ensure no duplicates and limit to top_n exchanges
        seen = set()
        unique_exchanges = []
        for exchange in filtered_legit_exchanges:
            if exchange['exchange_name'] not in seen:
                seen.add(exchange['exchange_name'])
                unique_exchanges.append(exchange)
            if len(unique_exchanges) == top_n:
                break

        # If there are less than top_n legitimate exchanges, fill with other exchanges
        if len(unique_exchanges) < top_n:
            for market in sorted_markets:
                if market['exchange_name'] not in seen:
                    unique_exchanges.append(market)
                    seen.add(market['exchange_name'])
                if len(unique_exchanges) == top_n:
                    break

        # Sort the final list by volume in descending order
        unique_exchanges.sort(key=lambda x: x['volume_24h'], reverse=True)

        return unique_exchanges
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from CoinPaprika API: {e}")
        return []
    
# Function to get the Crypto Fear and Greed Index
def get_fear_and_greed_index() -> dict:
    url = 'https://api.alternative.me/fng/'
    try:
        response = requests.get(url)
        data = response.json()
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to fetch data from Fear and Greed Index API: {e}")
        return {'error': 'Failed to fetch data from Fear and Greed Index API.'}

# Function to get the Crypto Fear and Greed Index
def get_fear_and_greed_index() -> dict:
    url = 'https://api.alternative.me/fng/'
    try:
        response = requests.get(url)
        data = response.json()
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to fetch data from Fear and Greed Index API: {e}")
        return {'error': 'Failed to fetch data from Fear and Greed Index API.'}

# Function to plot Fear and Greed Index as a gauge
def plot_fear_and_greed_gauge(value, status):
    try:
        # Ensure the directory exists
        output_dir = '/mnt/data'
        os.makedirs(output_dir, exist_ok=True)

        fig, ax = plt.subplots(figsize=(8, 4), subplot_kw={'projection': 'polar'})
        fig.patch.set_facecolor('white')

        # Define the colormap
        cmap = mcolors.LinearSegmentedColormap.from_list("fear_greed", ["red", "orange", "yellow", "green"])

        start_angle = np.pi + np.pi / 4  # 225 degrees, bottom left
        end_angle = -np.pi / 4  # -45 degrees, bottom right

        # Create a gradient color for the gauge
        for i in range(100):
            start = start_angle + i * (end_angle - start_angle) / 100
            end = start_angle + (i + 1) * (end_angle - start_angle) / 100
            color = cmap(i / 100)
            ax.barh(1, end - start, left=start, height=0.3, color=color, alpha=0.6)

        # Draw the needle
        needle_angle = start_angle + value * (end_angle - start_angle) / 100
        ax.plot([needle_angle, needle_angle], [0, 1], color='gray', linewidth=3)

        # Hide the grid and axis lines
        ax.set_yticklabels([])
        ax.set_xticks([])
        ax.spines['polar'].set_visible(False)
        ax.grid(False)

        # Set the labels
        ax.text(0.5, 1.3, 'Fear & Greed Index', horizontalalignment='center', verticalalignment='center', transform=ax.transAxes, fontsize=22, fontweight='bold')
        ax.text(0.005, 1.1, f'Now: {status}', horizontalalignment='left', verticalalignment='center', transform=ax.transAxes, fontsize=14, fontweight='bold')

        # Add the value without a bounding box
        ax.text(0.5, 0.5, str(value), horizontalalignment='center', verticalalignment='center', transform=ax.transAxes, fontsize=28, fontweight='bold', color='gray')

        # Add watermark
        fig.text(0.5, 0.05, '@CoinradarAlerts', fontsize=12, color='gray', ha='center', va='center', alpha=0.5)

        plt.tight_layout()

        # Save the plot as an image file with high quality
        plot_filename = os.path.join(output_dir, 'fear_and_greed_gauge.png')
        plt.savefig(plot_filename, bbox_inches='tight', dpi=300)
        plt.close()

        return plot_filename
    except Exception as e:
        logger.error(f"Error in plot_fear_and_greed_gauge: {e}")
        return None
    
# Function to get the latest crypto news from CryptoCompare API
def get_crypto_news() -> dict:
    for _ in range(len(API_KEYS['cc'])):
        try:
            api_key, index = get_valid_api_key('cc')
            headers = {'authorization': api_key}
            params = {'lang': 'EN', 'sortOrder': 'latest'}
            response = requests.get(CRYPTOCOMPARE_NEWS_API_URL, headers=headers, params=params)
            data = response.json()
            logger.debug(f"News API response: {data}")
            if 'Data' in data:
                return {'news': data['Data']}
            else:
                invalidate_api_key('cc', api_key)
                rotate_api_key('cc')
        except requests.RequestException as e:
            logger.error(f"Failed to fetch data from CryptoCompare API: {e}")
            invalidate_api_key('cc', api_key)
            rotate_api_key('cc')
    return {'error': 'Failed to fetch news from CryptoCompare API with all keys.'}


# Function to fetch upcoming events from CoinMarketCal API with key rotation
def fetch_coinmarketcal_events() -> dict:
    for _ in range(len(API_KEYS['coinmarketcal'])):
        api_key, index = get_valid_api_key('coinmarketcal')
        headers = {
            'x-api-key': api_key,
            'Accept': 'application/json'
        }
        params = {
            'max': 10,
            'dateRangeStart': datetime.now().strftime('%Y-%m-%d'),
            'dateRangeEnd': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
        }
        try:
            response = requests.get(COINMARKETCAL_API_URL, headers=headers, params=params)
            response.raise_for_status()  # Raises an HTTPError if the response was unsuccessful
            data = response.json()
            if 'body' in data:
                return data['body']
            else:
                logger.error(f"Unexpected response structure: {data}")
                invalidate_api_key('coinmarketcal', api_key)
                rotate_api_key('coinmarketcal')
        except requests.HTTPError as e:
            if response.status_code == 403:
                logger.error(f"Failed to fetch events from CoinMarketCal API: {e}")
                invalidate_api_key('coinmarketcal', api_key)
                rotate_api_key('coinmarketcal')
            else:
                logger.error(f"HTTP error occurred: {e}")
                break
        except requests.RequestException as e:
            logger.error(f"Failed to fetch events from CoinMarketCal API: {e}")
            invalidate_api_key('coinmarketcal', api_key)
            rotate_api_key('coinmarketcal')

    return {'error': 'Failed to fetch events from CoinMarketCal API with all keys.'}


# Function to get trending coins from CoinGecko API
def get_trending_coins_coingecko() -> dict:
    url = f'{COINGECKO_API_URL}/search/trending'
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return {'coins': data.get('coins', [])}
    except requests.RequestException as e:
        logger.error(f"Failed to fetch data from CoinGecko API: {e}")
        return {'error': 'Failed to fetch data from CoinGecko API.'}


# Function to send daily report
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    num_users, num_alert_users, num_alerts = get_database_stats()

    message = (
        f"📊 <b>Daily Report</b>\n"
        f"<b>Number of Users:</b> {num_users}\n"
        f"<b>Number of Users with Alerts:</b> {num_alert_users}\n"
        f"<b>Total Number of Alerts:</b> {num_alerts}\n\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"👉@CoinradarAlerts"
    )

    for admin_id in PRIMARY_ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=message, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send daily report to primary admin {admin_id}: {e}")

def get_next_sunday_10am_singapore():
    now = datetime.now(SINGAPORE_TZ)
    next_sunday = now + timedelta((6 - now.weekday()) % 7)  # Get next Sunday
    return next_sunday.replace(hour=10, minute=0, second=0, microsecond=0)


# Function to schedule weekly backups
async def send_weekly_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        logger.info("Starting weekly backup process...")
        await backup_database(context)
        logger.info("Weekly backup completed successfully.")
    except Exception as e:
        logger.error(f"Failed to complete the weekly backup: {e}")
        # Notify primary admins of the failure
        notification_message = f"<b>Error:</b> Weekly backup failed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. Reason: {str(e)}"
        for admin_id in PRIMARY_ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_message, parse_mode="HTML")
            except Exception as notify_error:
                logger.error(f"Failed to notify admin {admin_id}: {notify_error}")


def schedule_weekly_backup(application):
    next_sunday_10am = get_next_sunday_10am_singapore()
    seconds_until_backup = (next_sunday_10am - datetime.now(SINGAPORE_TZ)).total_seconds()

    # Schedule the weekly backup at 10:00 AM Singapore time every Sunday
    application.job_queue.run_repeating(
        send_weekly_backup,
        interval=timedelta(weeks=1),
        first=seconds_until_backup
    )
    logger.info(f"Scheduled weekly backup at {next_sunday_10am.strftime('%Y-%m-%d %H:%M:%S')} Singapore time")



#All Commands Section

## Define the start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("Help", callback_data='inline_help'),
            InlineKeyboardButton("Add Group", url='https://t.me/Coinradarprobot?startgroup=addtogroup'),  # Replace with your bot's username
        ],
        [
            InlineKeyboardButton("Join Channel", url='https://t.me/CoinradarAlerts'),
            InlineKeyboardButton("Contact Us", url='https://techswift.com/contact'),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text('Welcome to the Crypto Bot! Please choose an option:', reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text('Welcome to the Crypto Bot! Please choose an option:', reply_markup=reply_markup)

# Help command handler for inline button
async def inline_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "<b>Help Menu</b>\n\n"
        "/p - Get the latest price of a cryptocurrency\n"
        "/top10 - Get the top 10 cryptocurrencies by market cap\n"
        "/mp - Get prices of multiple coins\n"
        "/my - Save your favorite symbols and view their prices\n\n"
        "/calc - Convert to USD and BTC\n"
        "/ath - Get all-time high prices\n"
        "/atl - Get all-time low prices\n"
        "/h - Get historical price data\n"
        "/alert - Set a price alert\n"
        "/check_alerts - Check your alerts\n"
        "/cap - Get global market cap details\n"
        "/market - Get market details for a coin\n"
        "/gas - Get the latest Ethereum gas prices\n"
        "/portfolio - Manage your portfolio\n"
        "/whales - Get the latest whale alerts\n"
        "/fng - Get the Crypto Fear and Greed Index\n\n"
        "/news - Get the latest crypto news\n"
        "events - Get upcoming events for a coin\n"
        "For more details, visit our Channel. @CoinradarAlerts"
    )

    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data='start')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(message, reply_markup=reply_markup, parse_mode='HTML', disable_web_page_preview=True)
    elif update.message:
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='HTML', disable_web_page_preview=True)


# Command handler to handle the /my command
async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    args = context.args

    if not args:
        # Fetch and display saved symbols
        saved_symbols = get_user_symbols(user_id)
        if not saved_symbols:
            await update.message.reply_text(
                "You haven't saved any symbols yet. Use /my <symbol1> <symbol2> ... to save symbols."
            )
            return
        
        message = "<b>Your saved Coins:</b>\n"
        for symbol in saved_symbols:
            # Fetch symbol data from API (CMC in this case)
            data = None
            for _ in range(len(API_KEYS['cmc'])):
                data = get_crypto_price_cmc(symbol)
                if 'error' not in data:
                    break
                if rotate_api_key('cmc') is None:
                    break

            if data is None or 'error' in data:
                message += f"🔴 Error fetching data for {symbol}: {'No valid API keys left for CMC' if data is None else data['error']}\n"
                continue

            # Extracting the required information
            name = data['name']
            price = float(data['price'])  # Current price from CMC
            market_cap = data['market_cap']
            volume_24h = data['volume_24h']
            percent_change_1h = data['percent_change_1h']
            percent_change_24h = data['percent_change_24h']
            percent_change_7d = data['percent_change_7d']

            # Handling emojis based on percent changes
            emoji_1h = "😊" if percent_change_1h > 0 else "😞"
            emoji_24h = "🚀" if percent_change_24h > 0 else "😢"
            emoji_7d = "🎉" if percent_change_7d > 0 else "😭"

            # Formatting the message for each symbol
            message += (
                f"<b><a href='https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/' disable_web_page_preview='true'>{name}</a> ({symbol})</b>\n"
                f"Price: ${format_dynamic_price(price)}\n"
                f"Market Cap: ${format_number(market_cap)}\n"
                f"24h Volume: ${format_number(volume_24h)}\n"
                f"1h Change: {percent_change_1h:.2f}% {emoji_1h}\n"
                f"24h Change: {percent_change_24h:.2f}% {emoji_24h}\n"
                f"7d Change: {percent_change_7d:.2f}% {emoji_7d}\n"
                "--------------------------------\n"
            )

        await update.message.reply_text(message + "👉@CoinradarAlerts", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    
    else:
        # Saving new symbols
        new_symbols = [arg.upper() for arg in args]
        saved_symbols = get_user_symbols(user_id)
        
        # Add only symbols that are not already saved
        symbols_to_add = [symbol for symbol in new_symbols if symbol not in saved_symbols]
        
        if symbols_to_add:
            # Save non-duplicate symbols
            save_user_symbols(user_id, saved_symbols + symbols_to_add)
            await update.message.reply_text(
                f"✅ Saved symbols: {', '.join(symbols_to_add)}\nUse /my to view the current prices of your saved symbols.\n\n👉@CoinradarAlerts"
            )
        else:
            await update.message.reply_text("⚠️ All the symbols you entered are already saved.")


# Command handler to get cryptocurrency price
async def get_price(symbol: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        # Fetch price data from CoinMarketCap
        data = get_crypto_price_cmc(symbol)
        if 'error' in data:
            await update.message.reply_text(f"Error: {data['error']}")
            return

        name = data['name']
        price = float(data['price'])  # Current price from CMC
        market_cap = data['market_cap']
        volume_24h = data['volume_24h']
        percent_change_1h = data['percent_change_1h']
        percent_change_24h = data['percent_change_24h']
        percent_change_7d = data['percent_change_7d']
        circulating_supply = data['circulating_supply']
        fdv = data['fdv']

        # Fetch high and low data from CryptoCompare
        historical_data = get_historical_data(symbol)  # Implement this function
        if 'error' in historical_data:
            await update.message.reply_text("Failed to fetch high and low prices. Please try again later.")
            return

        high_value = historical_data['high']
        low_value = historical_data['low']

        # Prepare the message
        message = (
            f"<b><a href='https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/' "
            f"disable_web_page_preview='true'>{name}</a> ({symbol})</b>\n"
            f"<b>Price</b>: ${format_dynamic_price(price)} USD\n"
            f"H|L: ${format_dynamic_price(high_value)} | ${format_dynamic_price(low_value)}\n"
            f"1h:   {percent_change_1h:.2f}%\n"
            f"24h:  {percent_change_24h:.2f}%\n"
            f"7d:   {percent_change_7d:.2f}%\n"
            f"Supply: {format_number(circulating_supply)}\n"
            f"FDV: ${format_number(fdv)} USD\n"
            f"Cap: ${format_number(market_cap)}\n"
            f"Vol: ${format_number(volume_24h)}\n"
            f"👉@CoinradarAlerts"
        )

        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f'refresh_{symbol}')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Send the message
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error fetching price: {e}")
        await update.message.reply_text("Failed to fetch the price. Please try again later.")


# Function to get high and low prices from CryptoCompare
def get_historical_data(symbol: str) -> dict:
    try:
        url = f"https://min-api.cryptocompare.com/data/v2/histoday"
        params = {
            'fsym': symbol,
            'tsym': 'USD',
            'limit': 1,  # Get data for the last day
            'toTs': datetime.now().timestamp()  # Current timestamp
        }
        response = requests.get(url, params=params)
        data = response.json()

        if 'Data' in data and 'Data' in data['Data']:
            # Retrieve high and low values from the historical data
            high_price = max(entry['high'] for entry in data['Data']['Data'])
            low_price = min(entry['low'] for entry in data['Data']['Data'])
            return {
                'high': high_price,
                'low': low_price,
            }
        return {'high': 0, 'low': 0}  # Default values if no data found

    except Exception as e:
        logger.error(f"Error fetching historical data for {symbol}: {e}")
        return {'error': 'Failed to fetch historical data.'}



# Command handler for /p command
async def p_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    args = context.args
    if not args:
        args = ['BTC']  # Default to BTC if no symbol is provided
    symbol = args[0].upper()
    await get_price(symbol, update, context)

# Command handler to get Ethereum gas prices
async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    try:
        data = get_eth_gas_price()
        if 'error' in data:
            await update.message.reply_text(f"Error: {data['error']}")
            return

        gas_data = data['result']
        base_fee = float(gas_data.get('suggestBaseFee', 0.0))

        message = (
            f"<b>Ethereum Gas Prices</b>🚥\n"
            f"🚶‍♂️Safe: {float(gas_data['SafeGasPrice']):.2f} Gwei\n"
            f"🚗Propose: {float(gas_data['ProposeGasPrice']):.2f} Gwei\n"
            f"🚀Fast: {float(gas_data['FastGasPrice']):.2f} Gwei\n"
            f"Base Fee: {base_fee:.2f} Gwei\n"
            f"👉@CoinradarAlerts"
        )

        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data='refresh_gas')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error fetching gas prices: {e}")
        await update.message.reply_text("Failed to fetch the gas prices. Please try again later.")



# Command handler to get top 10 cryptocurrencies by market cap
async def top10(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    try:
        data = get_top10_cryptos()
        if 'error' in data:
            await update.message.reply_text(f"Error: {data['error']}")
            return

        top10_coins = data.get('data', [])[:10]
        message = "<b>Top 10 Cryptocurrencies by Market Cap:</b>\n\n"
        for coin in top10_coins:
            name = coin['name']
            symbol = coin['symbol']
            price = coin['quote']['USD']['price']
            market_cap = coin['quote']['USD']['market_cap']
            percent_change_24h = coin['quote']['USD']['percent_change_24h']
            emoji_24h = "📈" if percent_change_24h > 0 else "📉"

            message += (
                f"<b><a href='https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/' disable_web_page_preview='true'>{name}</a> ({symbol})</b>\n"
                f"Price: ${price:.2f}\n"
                f"Market Cap: ${format_number(market_cap)}\n"
                f"24h Change: {percent_change_24h:.2f}% {emoji_24h}\n\n"
            )

        await update.message.reply_text(message + "@CoinradarAlerts", parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

    except sqlite3.OperationalError as e:
        if 'database is locked' in str(e):
            logger.error("Database is locked. Retrying in a moment.")
            time.sleep(1)
            await top10(update, context)
        else:
            logger.error(f"Unexpected error: {e}")
            await update.message.reply_text("Failed to fetch the top 10 cryptocurrencies. Please try again later.")
    except Exception as e:
        logger.error(f"Error fetching top 10 cryptocurrencies: {e}")
        await update.message.reply_text("Failed to fetch the top 10 cryptocurrencies. Please try again later.")


# Command handler to get historical price data
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
   
    reset_style()  # Reset style before setting a new one
    args = context.args
    if not args or len(args) != 2:
        await update.message.reply_text("Usage: /h <symbol> <24h|7d|30d>\nExample: /h BTC 7d")
        return

    symbol = args[0].upper()
    time_period = args[1]

    if time_period not in ['24h', '7d', '30d']:
        await update.message.reply_text("Invalid time period. Use one of: 24h, 7d, 30d")
        return

    data = get_historical_data_cmc(symbol, time_period)
    if 'error' in data:
        await update.message.reply_text(f"Error: {data['error']}")
        return

    try:
        prices = data['prices']

        # Filter data for every 4 hours if the time period is 24h
        if time_period == '24h':
            filtered_prices = []
            for i in range(0, len(prices), len(prices) // 6):
                filtered_prices.append(prices[i])
            prices = filtered_prices

        dates = [datetime.utcfromtimestamp(price['time']).strftime(
            '%Y-%m-%d %H:%M:%S' if time_period == '24h' else '%Y-%m-%d') for price in prices]
        close_prices = [price['close'] for price in prices]

        text_message = (
            f"<b>{symbol} Price History ({time_period})</b>\n"
            f"Date and Price (USD):\n"
        )

        # Ensure message length does not exceed limits
        for date, close_price in zip(dates, close_prices):
            text_message += f"{date}: ${close_price:.2f}\n"
            if len(text_message) > 4000:
                break

        set_tradingview_style()

        fig, ax = plt.subplots(figsize=(14, 7))
        ax.plot(dates, close_prices, color='white', linewidth=2, label=symbol)
        ax.fill_between(dates, close_prices, color='skyblue', alpha=0.4)

        ax.set_xlabel('Date')
        ax.set_ylabel('Price (USD)')
        ax.set_title(f'{symbol} Price History ({time_period})')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M' if time_period == '24h' else '%Y-%m-%d'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=4) if time_period == '24h' else mdates.DayLocator())
        ax.grid(True, which='both')
        ax.legend()

        # Add watermark
        fig.text(0.5, 0.5, '@CoinradarAlerts', fontsize=30, color='gray', ha='center', va='center', alpha=0.3)

        graph_filename = f'{symbol}_history.png'
        plt.savefig(graph_filename, bbox_inches='tight')
        plt.close()

        with open(graph_filename, 'rb') as photo:
            await update.message.reply_photo(photo=photo, caption=text_message[:1024] + "\n@CoinradarAlerts",
                                             parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error generating graph: {e}")
        await update.message.reply_text("Failed to generate price history graph. Please try again later.")
    

# Command handler to convert cryptocurrency to USD and BTC
async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol=None, amount=None) -> None:
  
    if not symbol or not amount:
        args = context.args
        if not args or len(args) != 2:
            await update.message.reply_text("Usage: /calc <symbol> <amount>\nExample: /calc BTC 1")
            return

        symbol = args[0].upper()
        try:
            amount = float(args[1])
        except ValueError:
            await update.message.reply_text("Invalid amount. Please enter a number.")
            return

    price_data = get_crypto_price_cmc(symbol)
    if 'error' in price_data:
        await update.message.reply_text(f"Error: {price_data['error']}")
        return

    btc_data = get_crypto_price_cmc('BTC')
    if 'error' in btc_data:
        await update.message.reply_text(f"Error: {btc_data['error']}")
        return

    coin_data = price_data
    btc_price = float(btc_data['price'])  # Convert btc_price to a float
    coin_price = float(coin_data['price'])  # Ensure this is a float
    usd_value = coin_price * amount
    btc_value = usd_value / btc_price

    percent_change_24h = coin_data['percent_change_24h']
    percent_change_7d = coin_data['percent_change_7d']

    emoji_24h = "📈" if percent_change_24h > 0 else "📉"
    emoji_7d = "📈" if percent_change_7d > 0 else "📉"

    message = (
        f"Calculating {symbol} {amount:.4f}\n"
        f"<b>{symbol} Current Price</b>\n"
        f"<b>Price</b>: ${format_dynamic_price(coin_price)} | {usd_value / btc_price:.8f}₿\n\n"
        f"{btc_value:.8f} BTC\n"
        f"24h%: {percent_change_24h:.2f}% {emoji_24h} | 7d%: {percent_change_7d:.2f}% {emoji_7d}\n\n"
        f"${usd_value:.2f} USD\n"
        f"24h%: {percent_change_24h:.2f}% {emoji_24h} | 7d%: {percent_change_7d:.2f}% {emoji_7d}\n\n"
        f"{amount:.4f} {symbol}\n"
        f"24h%: 0.00% | 7d%: 0.00%\n"
        f"👉@CoinradarAlerts"
    )

    keyboard = [
        [
            InlineKeyboardButton("Double", callback_data=f'double_{symbol}_{amount}'),
            InlineKeyboardButton("Half", callback_data=f'half_{symbol}_{amount}')
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f'refresh_calc_{symbol}_{amount}')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
                                                      disable_web_page_preview=True)
    else:
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

# Command handler to get global market cap details
async def cap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    
    reset_style()
    try:
        data = get_market_cap_details()
        if 'data' not in data:
            await update.message.reply_text("Error fetching market cap details.")
            return

        market_data = data['data']
        market_cap = market_data['total_market_cap']['usd']
        volume_24h = market_data['total_volume']['usd']
        market_cap_change_percentage_24h = market_data.get('market_cap_change_percentage_24h_usd', 0.0)
        bitcoin_dominance = market_data['market_cap_percentage']['btc']
        active_cryptos = market_data['active_cryptocurrencies']
        volume_24h_change_percentage = market_data.get('volume_24h_change_percentage', 0.0)

        text_message = (
            f"<b>Global Market Cap Details</b>\n"
            f"<b>Market Cap</b>: ${format_number(market_cap)}\n"
            f"<b>24h Mcap Change</b>: {market_cap_change_percentage_24h:.2f}%\n"
            f"<b>Bitcoin Dominance</b>: {bitcoin_dominance:.2f}%\n"
            f"<b>Active Cryptos</b>: {format_number(active_cryptos)}\n"
            f"<b>24h Volume Change</b>: {volume_24h_change_percentage:.2f}%\n"
            f"<b>24h Volume</b>: ${format_number(volume_24h)}\n\n"
            f"👉@CoinradarAlerts"
        )

        set_tradingview_style()

        # Generate dummy data for the last 12 months
        dates = [datetime.now() - timedelta(days=30 * i) for i in range(12)][::-1]
        market_caps = [market_cap - i * 100000000 for i in range(12)]
        volumes = [volume_24h - i * 10000000 for i in range(12)]

        fig, ax1 = plt.subplots(figsize=(14, 7))

        ax2 = ax1.twinx()
        ax1.plot(dates, market_caps, color='white', linewidth=2, label='Market Cap')
        ax2.plot(dates, volumes, color='skyblue', linewidth=2, label='Trading Volume')

        ax1.set_xlabel('Date')
        ax1.set_ylabel('Market Cap in Billions', color='white')
        ax2.set_ylabel('Trading Volume in Billions', color='skyblue')
        ax1.set_title('Market Cap and Trading Volume')
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax1.grid(True, which='both', color='gray', linestyle='--', linewidth=0.5)
        ax1.legend(loc='upper left')
        ax2.legend(loc='upper right')

        # Add watermark
        fig.text(0.5, 0.5, '@CoinradarAlerts', fontsize=30, color='gray', ha='center', va='center', alpha=0.3)

        graph_filename = 'marketcap.png'
        plt.savefig(graph_filename, bbox_inches='tight')
        plt.close()

        with open(graph_filename, 'rb') as photo:
            await update.message.reply_photo(photo=photo, caption=text_message, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error fetching market cap details: {e}")
        await update.message.reply_text("Failed to fetch market cap details. Please try again later.")



async def ath_command_cc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /ath <symbol>\nExample: /ath BTC")
        return

    symbol = args[0].upper()
    try:
        # Fetch all-time high and current price data
        data = get_all_time_high_cc(symbol)
        price_data = get_crypto_price_cc(symbol)

        current_price_usd = float(price_data['price'])
        ath_usd = float(data['ath'])
        ath_date = data['ath_date']  # Ensure this is a string in 'YYYY-MM-DD' format

        # Calculate the percentage below ATH (drop)
        percentage_below_ath = ((ath_usd - current_price_usd) / ath_usd) * 100

        # Calculate the percentage needed to reach ATH
        percentage_needed_to_ath = ((ath_usd - current_price_usd) / current_price_usd) * 100

        # Calculate the number of days since ATH
        ath_datetime = datetime.strptime(ath_date, "%Y-%m-%d")
        days_since_ath = (datetime.now() - ath_datetime).days

        # Format the message
        message = (
            f"<b>{symbol} ATH Info</b>\n"
            f"Date of ATH: {ath_datetime.strftime('%m-%d-%Y')}\n"
            f"Current Price: ${format_dynamic_price(current_price_usd)}\n"
            f"ATH Price: ${format_dynamic_price(ath_usd)}\n"
            f"{percentage_below_ath:.2f}% below ATH\n"
            f"{percentage_needed_to_ath:.2f}% to ATH\n"
            f"⏳Days Since ATH: {days_since_ath} days\n"
            f"👉@CoinradarAlerts"
        )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    except APIKeyExhaustedException as e:
        logger.error(f"Error fetching all-time high price for {symbol}: {e}")
        await update.message.reply_text("All API keys are currently exhausted or invalid. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error fetching all-time high price for {symbol}: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")



async def atl_command_cc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /atl <symbol>\nExample: /atl BTC")
        return

    symbol = args[0].upper()
    try:
        # Fetch all-time low and current price data
        data = get_all_time_low_cc(symbol)
        price_data = get_crypto_price_cc(symbol)

        # Check if data is valid
        if not data or not price_data:
            await update.message.reply_text("Data not found.\n👉@CoinradarAlerts", parse_mode=ParseMode.HTML)
            return

        current_price = float(price_data['price'])
        atl = data['atl']  # ATL might be None or "N/A"
        atl_date = data['atl_date']  # Check if this is a valid date

        # Prepare the output message based on ATL value
        if atl is None or atl == 'N/A' or atl <= 0:
            await update.message.reply_text("Data not found.\n👉@CoinradarAlerts", parse_mode=ParseMode.HTML)
            return

        # Calculate ROI since ATL is valid
        roi = ((current_price - atl) / atl) * 100  # Calculate ROI

        # Calculate days since ATL
        try:
            atl_date_obj = datetime.strptime(atl_date, '%Y-%m-%d')  # Parse ATL date into a datetime object
            days_since_atl = (datetime.now() - atl_date_obj).days  # Calculate days since ATL
        except ValueError:
            days_since_atl = "N/A"  # If the date format is wrong, return N/A

        message = (
            f"<b>{symbol} ATL Info</b>\n"
            f"Date of ATL: {atl_date}\n"  # Display ATL date
            f"⏳ Days Since ATL: {days_since_atl} days\n"  # Display days since ATL
            f"Current Price: ${format_dynamic_price(current_price)}\n"
            f"ATL Price: ${format_dynamic_price(atl)}\n"  # Display ATL price
            f"ROI: {roi:.2f}%\n"  # Display ROI
            f"👉@CoinradarAlerts"
        )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    except APIKeyExhaustedException as e:
        logger.error(f"Error fetching all-time low price for {symbol}: {e}")
        await update.message.reply_text("All API keys are currently exhausted or invalid. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error fetching all-time low price for {symbol}: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")


# Command handler to list available commands
async def command_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Available commands:</b>\n\n"
        "/p - Get the latest price of a cryptocurrency\n"
        "/gas - Get the latest Ethereum gas prices\n"
        "/mp - Get prices of multiple coins\n"
        "/top10 - Get the top 10 cryptocurrencies by market cap\n"
        "/my - Save your favorite symbols and view their prices\n"
        "/help - List available commands\n"
        "/calc - Convert to USD and BTC\n"
        "/portfolio - Manage your portfolio\n"
        "/ath - Get all-time high prices\n"
        "/atl - Get all-time low prices\n"
        "/cap - Get global market cap details\n"
        "/start - Start the bot\n"
        "/h - Get historical price data\n"
        "/alert - Set a price alert\n"
        "/check_alerts - Check your alerts\n"
        "/market - Get market details for a coin\n"
        "/whales - Get the latest whale alerts\n"
        "/fng - Get the Crypto Fear and Greed Index\n"
        "/news - Get the latest crypto news\n"
        "/events - Get upcoming events for a coin\n"
        "@CoinradarAlerts",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

       
       
        
        
        
        
        
   

# Define a dictionary of blockchain explorers
EXPLORERS = {
    'bitcoin': 'https://www.blockchain.com/btc/tx/',
    'ethereum': 'https://etherscan.io/tx/',
    'ripple': 'https://xrpscan.com/tx/',
    'litecoin': 'https://blockchair.com/litecoin/transaction/',
    'bitcoin-cash': 'https://blockchair.com/bitcoin-cash/transaction/',
    'binancecoin': 'https://bscscan.com/tx/',
    'dogecoin': 'https://dogechain.info/tx/',
    'polkadot': 'https://polkascan.io/polkadot/transaction/',
    'solana': 'https://explorer.solana.com/tx/',
    'tron': 'https://tronscan.org/#/transaction/',
    'dai': 'https://etherscan.io/tx/',  # DAI transactions are on Ethereum
    # Add more explorers as needed
}

DEFAULT_EXPLORER = 'https://www.blockchain.com/btc/tx/'  # Default explorer if specific one is not listed

def get_explorer_url(blockchain, tx_hash):
    base_url = EXPLORERS.get(blockchain.lower(), DEFAULT_EXPLORER)
    return f"{base_url}{tx_hash}"

def format_whale_alerts(transactions: list) -> str:
    message = "🚨<b>Latest Whale Alerts</b>\n"
    for tx in transactions:
        blockchain = tx['blockchain'].capitalize()
        symbol = tx['symbol'].upper()
        amount = tx['amount']
        usd_value = tx['amount_usd']
        from_owner = tx['from'].get('owner', 'Unknown')
        to_owner = tx['to'].get('owner', 'Unknown')
        timestamp = datetime.utcfromtimestamp(tx['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        tx_hash = tx['hash']

        # Get the explorer URL
        explorer_url = get_explorer_url(blockchain, tx_hash)

        # Format large numbers
        formatted_usd_value = format_number(usd_value)

        message += (
            f"\n<b>{blockchain} ({symbol})</b>\n"
            f"<b>Amount</b>: {amount} {symbol}\n"
            f"<b>USD Value</b>: ${formatted_usd_value}\n"
            f"From: {from_owner}\n"
            f"To: {to_owner}\n"
            f"Timestamp: {timestamp}\n"
            f"<b>Transaction Hash</b>: <a href='{explorer_url}' disable_web_page_preview='true'>View on Explorer</a>\n"
        )

        if len(message) > 4000:  # Ensure the message length does not exceed Telegram's limit
            break

    return message + "\n@CoinradarAlerts"

# Command handler to get whale alerts
async def whales_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    args = context.args
    min_value = 500000  # Default to 500,000 USD if no value is provided
    if args:
        try:
            min_value = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid value. Please enter a number.")
            return

    try:
        data = get_latest_whale_alerts(min_value)
        if 'error' in data:
            await update.message.reply_text(f"Error: {data['error']}")
            return

        transactions = data.get('transactions', [])
        if not transactions:
            await update.message.reply_text(f"No transactions found for value over ${min_value}.\n\n@CoinradarAlerts")
            return

        message = format_whale_alerts(transactions)
        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error fetching whale alerts: {e}")
        await update.message.reply_text("Failed to fetch whale alerts. Please try again later.")


# List of known legitimate exchanges
LEGIT_EXCHANGES = {
    'Binance', 'Coinbase Pro', 'Kraken', 'Bitfinex', 'Huobi Global', 'Bittrex',
    'Gemini', 'KuCoin', 'Bitstamp', 'Poloniex', 'Gate io', 'OKEx', 'Bibox', 'BitMart',
    'HitBTC', 'Upbit', 'ProBit Exchange', 'Bithumb', 'Coinone', 'Liquid', 'crypto com'
}


# Command handler to get the list of top spot exchanges for a given coin
async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    args = context.args
    if not args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/market &lt;symbol&gt;</code>\n<i>Example:</i> <code>/market BTC</code>\n\n@CoinradarAlerts",
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        return

    symbol = args[0].upper()
    exchanges = get_top_exchanges(symbol)

    if exchanges:
        num_exchanges = len(exchanges)
        message = f"<b>Top {num_exchanges} spot exchanges for {symbol} by volume:</b>\n\n"
        message += "\n".join(
            [f"{i + 1}. <b>{sanitize_name(exchange['exchange_name'])}</b>: ${format_number(exchange['volume_24h'])}" for
             i, exchange in enumerate(exchanges)])
    else:
        message = f"<b>No spot exchanges with healthy volume found for {symbol}</b>\n\n@CoinradarAlerts"

    try:
        await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)
    except BadRequest as e:
        logger.error(f"Failed to send message: {e}")
        await update.message.reply_text(
            "<b>Error:</b> The message is too long to send. Please try with a different symbol or reduce the length of the response.\n\n@CoinradarAlerts",
            parse_mode='HTML',
            disable_web_page_preview=True

        )

# Command handler to get prices of multiple coins
async def mp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    args = context.args
    if not args or len(args) > 8:
        await update.message.reply_text(
            "Usage: /mp <symbol1> <symbol2> ... <symbol5>\nExample: /mp BTC ETH BNB XRP DOGE\n\n@CoinraderPro")
        return

    message = ""
    for symbol in args:
        symbol = symbol.upper()
        data = get_crypto_price_cmc(symbol)
        if 'error' in data:
            message += f"Error: {data['error']}\n"
            continue

        name = data['name']
        price = float(data['price'])
        market_cap = data['market_cap']
        volume_24h = data['volume_24h']
        percent_change_1h = data['percent_change_1h']
        percent_change_24h = data['percent_change_24h']
        percent_change_7d = data['percent_change_7d']

        emoji_1h = "😊" if percent_change_1h > 0 else "😞"
        emoji_24h = "🚀" if percent_change_24h > 0 else "😢"
        emoji_7d = "🎉" if percent_change_7d > 0 else "😭"

        message += (
            f"<b><a href='https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/' disable_web_page_preview='true'>{name}</a> ({symbol})</b>\n"
            f"<b>Price</b>: ${format_dynamic_price(price)} USD\n"
            f"Market Cap: ${format_number(market_cap)} USD\n"
            f"24h Volume: {format_number(volume_24h)} USD\n"
            f"1h Change: {percent_change_1h:.2f}% {emoji_1h}\n"
            f"24h Change: {percent_change_24h:.2f}% {emoji_24h}\n"
            f"7d Change: {percent_change_7d:.2f}% {emoji_7d}\n"
            "\n"
        )

    await update.message.reply_text(message + "👉@CoinradarAlerts", parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)

# Command handler to backup the database
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    
    # Check if the user is a primary admin
    if not is_primary_admin(user_id):
        await update.message.reply_text("You do not have permission to execute this command.")
        return

    # Attempt to create the backup
    backup_file = await backup_database(context)
    
    # Notify the user whether the backup was successful or not
    if backup_file:
        await update.message.reply_text(f"Backup completed successfully. File saved as: {os.path.basename(backup_file)}")
    else:
        await update.message.reply_text("Backup failed. Please check the logs for more details.")



# Command handler to clean the database
async def clean_database(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    if not is_primary_admin(user_id):
        await update.message.reply_text("You do not have permission to execute this command.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_symbols")
    cursor.execute("DELETE FROM alerts")
    cursor.execute("DELETE FROM api_keys")
    cursor.execute("DELETE FROM portfolio")  
    conn.commit()
    conn.close()

    # Reinitialize the api_keys table
    init_api_keys()

    await update.message.reply_text("Database cleaned and API keys reinitialized successfully.")


# Command handler to reload the bot
async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    if not is_primary_admin(user_id):
        await update.message.reply_text("You do not have permission to execute this command.")
        return

    # Notify the bot's private chat about the restart attempt
    for admin_id in PRIMARY_ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text="Bot is restarting...")
        except Exception as e:
            logger.error(f"Failed to send restart notification to primary admin {admin_id}: {e}")

    # Restart the bot
    os.execv(sys.executable, ['python'] + sys.argv)


# Singapore timezone
SINGAPORE_TZ = pytz.timezone('Asia/Singapore')

# Function to schedule nightly reload
def schedule_nightly_reload(application):
    # Define the time for 4:00 AM in Singapore timezone
    reload_time = dt_time(hour=4, minute=0, tzinfo=SINGAPORE_TZ)
    
    # Schedule the reload job using job_queue
    application.job_queue.run_daily(reload_bot_job, time=reload_time)
    logging.info("Scheduled nightly reload at 4:00 AM Singapore time.")

# Define the job function to reload the bot
def reload_bot_job(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Reloading the bot at 4:00 AM Singapore Time.")
    os.execv(sys.executable, ['python'] + sys.argv)

# Command handler for /fng command
async def fng_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    
    reset_style()  # Reset style before setting a new one
    data = get_fear_and_greed_index()
    if 'error' in data:
        await update.message.reply_text(f"Error: {data['error']}")
        return

    index_data = data.get('data', [])
    if not index_data:
        await update.message.reply_text("Failed to retrieve the Fear and Greed Index data.")
        return

    current_data = index_data[0]
    value = int(current_data['value'])
    status = current_data['value_classification']

    last_update_time = datetime.fromtimestamp(int(current_data['timestamp']))
    time_since_update = datetime.now() - last_update_time
    next_update_time = last_update_time + timedelta(hours=24)

    # Generate the plot
    plot_filename = plot_fear_and_greed_gauge(value, status)

    message = (
        f"😱 <b>Crypto Fear and Greed Index</b>\n\n"
        f"Current Index: {current_data['value']} ({status})\n"
        f"Updated: {time_since_update.total_seconds() // 3600:.0f} hours ago\n"
        f"<b>Next Update:</b> in {(next_update_time - datetime.now()).total_seconds() // 3600:.0f} hours\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"👉@CoinradarAlerts"
    )

    if plot_filename is None:
        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    else:
        with open(plot_filename, 'rb') as photo:
            await update.message.reply_photo(photo=photo, caption=message, parse_mode=ParseMode.HTML)


# Command handler for /gas command
async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    logger.info("Fetching latest crypto news...")
    data = get_crypto_news()
    if 'error' in data:
        logger.error(f"Error fetching news: {data['error']}")
        await update.message.reply_text(f"Error: {data['error']}")
        return

    news_items = data.get('news', [])
    if not news_items:
        logger.info("No news available.")
        await update.message.reply_text("No news available.")
        return

    message = "📰 <b>Latest Crypto News</b> 📰\n\n"
    for item in news_items[:5]:  # Limit to the first 5 news articles
        title = item.get('title', 'No title')
        body = item.get('body', 'No content')
        source = item.get('source_info', {}).get('name', 'Unknown source')
        url = item.get('url', '#')

        message += f"🗞 <b>{title}</b>\n"
        message += f"Source: {source}\n"
        message += f"<a href='{url}'>Read more</a>\n\n"

    logger.debug(f"News message: {message}")
    await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# Command handler for /events command
async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    logger.info("Fetching upcoming events from CoinMarketCal...")
    data = fetch_coinmarketcal_events()
    if 'error' in data:
        logger.error(f"Error fetching events: {data['error']}")
        await update.message.reply_text(f"Error: {data['error']}")
        return

    events = data
    if not events:
        logger.info("No upcoming events available.")
        await update.message.reply_text("No upcoming events available.")
        return

    message = "📅 <b>Upcoming Crypto Events</b> 📅\n\n"
    max_length = 4096 - len(message) - 50  # Reserve some buffer for HTML tags and formatting
    message_body = ""

    events_added = 0
    max_events = 7  # Define the maximum number of events you want to add

    for event in events[:15]:  # Fetch more events but process only up to 15
        title = event.get('title', {}).get('en', 'No title')
        date_event = event.get('date_event', 'No date')
        categories = ', '.join([category['name'] for category in event.get('categories', [])])
        proof = event.get('proof', 'No proof')
        source = event.get('source', 'No source')
        created_date = event.get('created_date', 'No date added')

        event_text = (
            f"🗓 <b>{title}</b>\n"
            f"Date: {date_event}\n"
            f"Category: {categories}\n"
            f"<a href='{proof}'>Screenshot proof</a>\n"
            f"Source: <a href='{source}'>{source}</a>\n"
            f"Date added: {created_date}\n\n"
        )

        if len(message_body + event_text) > max_length or events_added >= max_events:
            break

        message_body += event_text
        events_added += 1

    message += message_body
    logger.debug(f"Events message: {message}")
    await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)





# Command handler for /price command
async def trend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    data = get_trending_coins_coingecko()
    if 'error' in data:
        await update.message.reply_text(f"Error: {data['error']}")
        return

    coins = data.get('coins', [])
    if not coins:
        await update.message.reply_text("No trending coins available.")
        return

    message = "🚀 <b>Top 20 Trending Coins on CoinGecko</b> 🚀\n\n"
    for i, coin_info in enumerate(coins[:20], start=1):
        coin = coin_info.get('item', {})
        name = coin.get('name', 'N/A')
        symbol = coin.get('symbol', 'N/A').upper()
        market_cap_rank = coin.get('market_cap_rank', 'N/A')
        price_btc = coin.get('price_btc', 'N/A')
        coin_id = coin.get('id', '')

        price_data = get_crypto_price_cc(symbol)
        last_price = f"${price_data.get('price', 'N/A'):.2f} USD" if 'price' in price_data else "N/A"

        message += (
            f"{i}. <b>{name}</b> ({symbol})\n"
            f"🌐 <a href='https://www.coingecko.com/en/coins/{coin_id}' disable_web_page_preview='true'>CoinGecko</a>\n"
            f"💹 Market Cap Rank: {market_cap_rank}\n"
            f"💰 Price in BTC: {price_btc:.8f} BTC\n"
            f"💲 Last Price: {last_price}\n\n"
        )

    await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)






# Symbol alerts dictionary to store user alerts

# Load the last known prices from the JSON file
def load_last_prices():
    try:
        with open("last_prices.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# Save the last known prices to the JSON file
def save_last_prices(prices):
    with open("last_prices.json", "w") as f:
        json.dump(prices, f)

# Initialize last known prices
last_prices = load_last_prices()

# Function to save the last known price for a symbol
def save_last_price(symbol, price):
    global last_prices
    last_prices[symbol] = price
    save_last_prices(last_prices)


# Command handler to set price alerts
async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    args = context.args
    if not args or len(args) != 2:
        await update.message.reply_text("Usage: /alert <symbol> <price>\nExample: /alert BTC 50000")
        return

    symbol = args[0].upper()
    try:
        price = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid price. Please enter a number.")
        return

    user_id = str(update.message.from_user.id)
    chat_id = str(update.message.chat_id)

    # Save the last known price for the symbol when the alert is set
    data = get_crypto_price_cc(symbol)
    if 'error' in data:
        await update.message.reply_text(f"Error: {data['error']}")
        return

    current_price = data.get('price', 'N/A')
    set_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_alert(user_id, chat_id, symbol, price)

    try:
        current_price_formatted = f"${float(current_price):.2f} USD" if isinstance(current_price, (int, float)) else current_price
    except ValueError:
        current_price_formatted = "N/A"

    await update.message.reply_text(f"🚨 <b>Alert Set!</b> 🚨\n\n"
                                    f"Symbol: {symbol}\n"
                                    f"Alert Price: ${price:.2f} USD\n"
                                    f"<b>Current Price: {current_price_formatted}\n"
                                    f"Time:</b> {set_time}\n\n"
                                    f"🔔 You will be notified when the price reaches your target.\n\n"
                                    f"👉@CoinradarAlerts", parse_mode=ParseMode.HTML)


# Optimized function to check alerts
async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = get_alerts()

    # Group alerts by symbol
    grouped_alerts = {}
    for status in ['Active']:
        for user_id, user_alerts in list(alerts[status].items()):
            for chat_id, symbol, alert_price in user_alerts:
                if symbol not in grouped_alerts:
                    grouped_alerts[symbol] = []
                grouped_alerts[symbol].append((user_id, chat_id, alert_price))

    # Fetch prices in batches
    for symbol, alerts_for_symbol in grouped_alerts.items():
        data = get_crypto_price_cc(symbol)
        if 'error' in data:
            logger.error(f"Failed to fetch price data for symbol {symbol}: {data['error']}")
            continue

        current_price = data['price']

        for user_id, chat_id, alert_price in alerts_for_symbol:
            last_price = last_prices.get(symbol, current_price)

            if last_price < alert_price <= current_price:
                trigger_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                await context.bot.send_message(chat_id=chat_id, 
                    text=(f"🚨 <b>Price Alert!</b> 🚨\n\n"
                          f"Symbol: {symbol}\n"
                          f"<b>Alert Price:</b> ${alert_price:.2f} USD\n"
                          f"Previous Price: ${last_price:.2f} USD\n"
                          f"Current Price: ${current_price:.2f} USD\n"
                          f"Time: {trigger_time}\n\n"
                          f"🎯 Your price alert has been triggered.\n\n"
                          f"@CoinraderPro"), 
                    parse_mode=ParseMode.HTML)
                # Update the status to 'Previous' once the alert is triggered
                update_alert_status(user_id, chat_id, symbol, alert_price)

            # Update the last known price for the symbol
            save_last_price(symbol, current_price)


# Command handler to check alerts
async def check_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id = str(update.message.from_user.id)
    alerts = get_alerts()

    message = "🔍 <b>Your Active Alerts:</b>\n\n"
    for chat_id, symbol, price in alerts['Active'].get(user_id, []):
        message += f"💰 <b>{symbol}</b> at <b>${price:.2f} USD</b>\n"

    message += "\n🗂️ <b>Your Previous Alerts:</b>\n\n"
    for chat_id, symbol, price in alerts['Previous'].get(user_id, []):
        message += f"📉 <b>{symbol}</b> at <b>${price:.2f} USD</b>\n"

    await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# Function to check the status of all API keys

# Function to check the status of a single API key
def check_single_api_key(service, api_key):
    try:
        if service == 'cc':
            url = "https://min-api.cryptocompare.com/data/blockchain/list"
            params = {'api_key': api_key}
            response = requests.get(url, params=params, timeout=5)
        elif service == 'cmc':
            url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
            headers = {
                'Accepts': 'application/json',
                'X-CMC_PRO_API_KEY': api_key,
            }
            params = {'symbol': 'BTC'}
            response = requests.get(url, headers=headers, params=params, timeout=5)
        elif service == 'whale':
            url = "https://api.whale-alert.io/v1/transactions"
            params = {'api_key': api_key, 'min_value': 500000, 'limit': 1}
            response = requests.get(url, params=params, timeout=5)
        elif service == 'etherscan':
            url = "https://api.etherscan.io/api"
            params = {'module': 'account', 'action': 'balance', 'address': '0xde0B295669a9FD93d5F28D9Ec85E40f4cb697BAe', 'apikey': api_key}
            response = requests.get(url, params=params, timeout=5)
        elif service == 'coinmarketcal':  # Example additional service
            url = "https://developers.coinmarketcal.com/v1/events"
            headers = {'x-api-key': api_key}
            response = requests.get(url, headers=headers, timeout=5)
        else:
            return False  # Unsupported service

        response.raise_for_status()  # Raise an HTTPError for bad responses
        data = response.json()

        # Check if the response contains an error message
        if service == 'cc' and data.get('Response') == 'Error':
            return False
        if service == 'cmc' and data.get('status', {}).get('error_code') != 0:
            return False
        if service == 'whale' and 'error' in data:
            return False
        if service == 'etherscan' and data.get('status') != '1':
            return False
        if service == 'coinmarketcal' and 'error' in data:  # Example additional service
            return False

        return True  # No errors found, key is working

    except requests.exceptions.HTTPError as http_err:
        # Catch specific HTTP errors to identify unauthorized or invalid API keys
        if http_err.response.status_code in {401, 403}:
            logger.error(f"Unauthorized or invalid API key for {service} API key {api_key}: {http_err}")
            return False
        else:
            logger.error(f"HTTP error for {service} API key {api_key}: {http_err}")
            return False
    except requests.RequestException as e:
        logger.error(f"Request exception for {service} API key {api_key}: {e}")
        return False

# Function to notify the primary admins about API key issues
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, messages: dict):
    notification_message = (
        "<b>🚨 API Key Alerts 🚨</b>\n"
        "----------------------------------------\n"  # Separator line
    )
    
    for service, issues in messages.items():
        notification_message += f"<b>🔧 {service.upper()}:</b>\n"  # Start of section for service issues
        for issue in issues:
            # Highlight the API key and make it bold
            issue_text = issue.rsplit(' ', 1)[0]
            api_key = issue.rsplit(' ', 1)[1]
            highlighted_key = f"<b><code>{api_key}</code></b>"
            notification_message += f"• ❌ {issue_text} {highlighted_key}\n"  # Bullet point for each issue
        notification_message += "----------------------------------------\n"  # Separator line

    notification_message += (
        "<b>Please check the API keys and take necessary actions.</b>\n"
        "<i>Thank you for your prompt attention to this matter.</i>"
    )
    
    for admin_id in PRIMARY_ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=notification_message, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send message to primary admin {admin_id}: {e}")

# Function to periodically check the status of all API keys
async def periodic_check_api_status(context: ContextTypes.DEFAULT_TYPE):
    services = ['cc', 'cmc', 'whale', 'etherscan', 'coinmarketcal']  # Include additional services here
    notifications = {}
    for service in services:
        key_status = await check_api_status(service)
        service_issues = []
        for key, is_working in key_status.items():
            if not is_working:
                service_issues.append(f"The API key {key[:6]}... is either invalid or has reached its limit.")
        if service_issues:
            notifications[service] = service_issues

    if notifications:
        logger.info("Sending notification with the following issues: %s", notifications)
        await notify_admins(context, notifications)
    else:
        logger.info("All API keys are working fine.")

# Check the status of all API keys for a given service
async def check_api_status(service):
    keys = API_KEYS.get(service, [])
    key_status = {}
    for key in keys:
        is_working = check_single_api_key(service, key)
        key_status[key] = is_working

    return key_status


from datetime import datetime, timedelta


# Command handler to check the bot status
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id = str(update.message.from_user.id)
    if not is_primary_admin(user_id):
        await update.message.reply_text("You do not have permission to execute this command.")
        return

    try:
        bot = context.bot
        start_time = context.bot_data.get("start_time", datetime.now())
        uptime = datetime.now() - start_time

        # Get database stats
        num_unique_users, num_alert_users, num_alerts, num_portfolio_users, num_lost_users = get_database_stats()
        logger.info(f"Database stats: {num_unique_users}, {num_alert_users}, {num_alerts}, {num_portfolio_users}, {num_lost_users}")

        # Check the status of each API
        api_status = {
            "CoinMarketCap": await check_api_status('cmc'),
            "Whale Alert": await check_api_status('whale'),
            "CryptoCompare": await check_api_status('cc'),
            "Etherscan": await check_api_status('etherscan'),
            "CoinMarketCal": await check_api_status('coinmarketcal')  # Example additional service
        }

        # Summarize the status for each API
        api_status_summary = {}
        for api, statuses in api_status.items():
            if isinstance(statuses, dict):
                working_keys = sum(status for status in statuses.values())
                total_keys = len(statuses)
                api_status_summary[api] = f"{working_keys}/{total_keys} keys working"
            else:
                api_status_summary[api] = "✅ Working" if statuses else "❌ Not Working"

        # Format the API status message
        api_status_message = ""
        for api, status in api_status_summary.items():
            if api in api_status and isinstance(api_status[api], dict):
                api_status_message += f"🔧 <b>{api}:</b> {status}\n"
                for key, key_status in api_status[api].items():
                    api_status_message += f"  - {key[:6]}...: {'✅ Working' if key_status else '❌ Not Working'}\n"
            else:
                api_status_message += f"🔧 <b>{api}:</b> {status}\n"

        # Format the uptime and bot start time
        bot_start_time = start_time.strftime('%Y-%m-%d %H:%M:%S')
        uptime_str = str(uptime).split('.')[0]

        # Improved status message with emojis and formatting
        message = (
            f"🤖 <b>Bot Status</b>\n"
            f"=========================\n\n"
            f"⏱️ <b>Uptime:</b> {uptime_str}\n\n"
            f"👥 <b>Unique Users:</b> {num_unique_users}\n"
            f"🔔 <b>Users with Alerts:</b> {num_alert_users}\n"
            f"📈 <b>Total Alerts:</b> {num_alerts}\n"
            f"🗂️ <b>Portfolio Users:</b> {num_portfolio_users}\n"
            f"🚫 <b>Lost Users:</b> {num_lost_users}\n\n"
            f"=========================\n"
            f"🖥️ <b>API Status:</b>\n{api_status_message}\n"
            f"=========================\n\n"
            f"🚀 <b>Bot Started On:</b> {bot_start_time}\n\n"
            f"👉@CoinradarAlerts"
        )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error fetching bot status: {e}")
        await update.message.reply_text("Failed to fetch the bot status. Please try again later.")


#portfolio management

# List of known cryptocurrency symbols
known_symbols = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "BNB": "Binance Coin",
    "XRP": "Ripple",
    "DOGE": "Dogecoin",
    "ADA": "Cardano",
    "SOL": "Solana",
    "DOT": "Polkadot",
    "MATIC": "Polygon",
    "LTC": "Litecoin"
}



# Function to execute queries (for updates, inserts, deletes)
def execute_query(query, params):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    conn.close()

# Function to fetch data from the database
def fetch_all(query, params):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


# Function to reset the state after adding/removing coins

# Ensure reset of adding mode
def reset_add_state(chat_data: dict):
    """Reset the add mode."""
    try:
        if 'awaiting_add' in chat_data:
            chat_data['awaiting_add'] = False
            chat_data['adding_user_id'] = None
            logging.info("Add mode reset.")
        else:
            logging.warning("Add mode was not active.")
    except Exception as e:
        logging.error(f"Error resetting add state: {e}")


# Ensure reset of removing mode
def reset_remove_state(chat_data: dict):
    """Reset the remove mode."""
    try:
        if 'awaiting_remove' in chat_data:
            chat_data['awaiting_remove'] = False
            chat_data['removing_user_id'] = None
            logging.info("Remove mode reset.")
        else:
            logging.warning("Remove mode was not active.")
    except Exception as e:
        logging.error(f"Error resetting remove state: {e}")




# Step 1: Start Portfolio Handler
async def portfolio_handler(update: Update, context: CallbackContext) -> None:
    await show_portfolio(update, context)

# Step 2: Show initial options to view portfolio
# Step 2: Show initial options to view portfolio
async def show_portfolio(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id

    # Check if the interaction is in a private chat or group chat
    is_private_chat = (
        update.message.chat.type == 'private' 
        if update.message 
        else update.callback_query.message.chat.type == 'private'
    )

    # Create keyboard based on chat type
    keyboard = []
    if is_private_chat:
        # If in private chat, only show the view option
        keyboard.append([ 
            InlineKeyboardButton("👁️ View Here", callback_data=f"view_portfolio_group:{user_id}")
        ])
    else:
        # If in group chat, show both options
        keyboard.append([
            InlineKeyboardButton("👁️ View Here", callback_data=f"view_portfolio_group:{user_id}"),
            InlineKeyboardButton("🔒 Send Privately", callback_data=f"view_portfolio_private:{user_id}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Construct message text
    message_text = (
        "🎯 <b>Portfolio Access Options</b> 🎯\n\n"
        "🔎 <i>Choose how you'd like to view your portfolio:</i>\n\n"
        "➤ <b>👁️ View Here</b> — Check your portfolio right here in this chat.\n"
        + ("" if is_private_chat else "➤ <b>🔒 Send Privately</b> — Receive it privately for your eyes only.\n") +
        "<i>💡 Tip:</i> Stay on top of your investments anytime, anywhere!\n"
        "<b>Which option suits you best?</b>👇"
    )

    # Send or edit the message based on the update type
    if update.message:
        sent_message = await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    else:
        sent_message = await update.callback_query.message.edit_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

    # Schedule message to be deleted after 30 seconds if no interaction occurs
    job_name = f"delete_portfolio_{sent_message.chat.id}_{sent_message.message_id}"
    context.job_queue.run_once(
        delete_portfolio_message_job, 
        30,  # Increased wait time to 30 seconds for more interaction time
        name=job_name, 
        data={"chat_id": sent_message.chat.id, "message_id": sent_message.message_id}
    )
    context.chat_data['delete_job'] = job_name



# Step 3: Handle the button interactions and back navigation
async def handle_portfolio_choice(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    choice = query.data.split(":")[0]

    # Cancel the delete job since the user interacted
    job_name = context.chat_data.get('delete_job')
    if job_name:
        current_job = context.job_queue.get_jobs_by_name(job_name)
        if current_job:
            try:
                current_job[0].schedule_removal()
                context.chat_data.pop('delete_job', None)
            except Exception as e:
                logger.error(f"Failed to cancel delete job: {e}")

    # Handle the choice (view in group or private)
    if choice == 'view_portfolio_group':
        await send_portfolio(user_id, query, context, show_in_group=True)
        
        # Schedule to hide portfolio after interaction (new timeout)
        context.job_queue.run_once(hide_portfolio_job, 60, data={"chat_id": query.message.chat.id, "message_id": query.message.message_id, "user_id": user_id})

    elif choice == 'view_portfolio_private':
        try:
            # Send the portfolio privately to the user
            await send_portfolio(user_id, query, context, show_in_group=False)

            # Notify the user that the portfolio has been sent
            sent_message = await query.message.edit_text(
                "✅ <b>Your portfolio is on its way!</b> 📬\n\n"
                "🔒 <i>Check your private messages for the details.</i>\n\n"
                "We value your privacy — your portfolio has been delivered securely.\n\n"
                "You can access your private messages here: "
                f"<a href='https://t.me/Coinradarprobot'>Go to Private</a>",
                parse_mode='HTML',
                disable_web_page_preview=True  # Disable link preview
            )

            # Schedule message removal after 10 seconds
            context.job_queue.run_once(delete_message_job, 19, data={"chat_id": sent_message.chat.id, "message_id": sent_message.message_id})

        except Exception as e:
            # Handle the error if sending the portfolio privately fails
            sent_message = await query.message.edit_text(
                "❌ <b>Oops, something went wrong!</b> 😔\n\n"
                f"<i>We couldn't send your portfolio privately.</i>\n\n"
                f"Error: {e}\n\n"
                "Please try again later or contact support if the issue persists.",
                parse_mode='HTML'
            )

            # Schedule message removal after 10 seconds
            context.job_queue.run_once(delete_message_job, 13, data={"chat_id": sent_message.chat.id, "message_id": sent_message.message_id})


# Step 6: Hide portfolio (edit message to show options)
async def hide_portfolio_job(context: CallbackContext, chat_id: int = None, message_id: int = None, user_id: int = None) -> None:
    # Handle job data if the function is called as part of a job
    if context.job and not chat_id and not message_id and not user_id:
        job_data = context.job.data
        if not job_data:
            logger.error("Job data is missing.")
            return
        chat_id = job_data.get("chat_id")
        message_id = job_data.get("message_id")
        user_id = job_data.get("user_id")

    # If chat_id, message_id, or user_id are not provided or found in the job data, raise an error
    if not (chat_id and message_id and user_id):
        logger.error("Missing parameters for chat_id, message_id, or user_id.")
        return

    # Add a log to ensure the function is being triggered
    logger.info(f"Running hide_portfolio_job for chat_id: {chat_id}, message_id: {message_id}, user_id: {user_id}")

    # Check if the chat type is private
    try:
        chat = await context.bot.get_chat(chat_id)  # Await fetching the chat information
        is_private_chat = chat.type == 'private'
    except Exception as e:
        logger.error(f"Failed to fetch chat info: {e}")
        return

    # Create buttons dynamically based on chat type
    buttons = [
        InlineKeyboardButton("👁️ View Again", callback_data=f"view_portfolio_group:{user_id}")
    ]
    if not is_private_chat:
        buttons.append(InlineKeyboardButton("🔒 Private", callback_data=f"view_portfolio_private:{user_id}"))

    # Edit message to show options
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "🔒 <b>Your portfolio is now hidden!</b> 🕵️‍♂️\n\n"
                "<i>You can:</i>\n"
                "1️⃣ <b>View it again</b> 👁️\n"
                + ("" if is_private_chat else "2️⃣ <b>Send it privately</b> 🔒\n") +  # Only show if not in private
                "<i>Select one of the options below to continue:</i>"
            ),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([buttons])  # Create the keyboard with valid buttons
        )

        # Reset add state here by passing chat_data instead of context
        reset_add_state(context.chat_data)

    except Exception as e:
        logger.error(f"Failed to edit message: {e}")









# Step 7: Delete portfolio message after 30 seconds of inactivity
async def delete_portfolio_message_job(context: CallbackContext) -> None:
    job_data = context.job.data
    if not job_data:
        logger.error("Job data is missing.")
        return

    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Deleted portfolio message in chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to delete portfolio message: {e}")

    # Check if context.chat_data is not None before accessing it
    if context.chat_data is not None:
        job_key = 'delete_job'
        if job_key in context.chat_data:
            context.chat_data.pop(job_key, None)
    else:
        logger.error("context.chat_data is None, cannot pop 'delete_job'.")


# Step 8: Handle the hide button to manually delete the message
async def handle_hide_button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    try:
        await query.message.delete()
        await query.answer("Portfolio message deleted successfully.")
    except Exception as e:
        logger.error(f"Failed to delete portfolio message: {e}")
        await query.answer("Failed to delete the portfolio message. Please try again later.")

# Back to Portfolio handler (manual back to menu)
async def handle_back_to_portfolio(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    user_id = query.from_user.id

    # Trigger hide_portfolio_job to show "View Again" or "Send to Private" options
    await hide_portfolio_job(context, chat_id=chat_id, message_id=message_id, user_id=user_id)

# Handlers for the new buttons:
async def handle_back_to_portfolio(update: Update, context: CallbackContext) -> None:
    # This would take the user back to send_portfolio function
    user_id = update.callback_query.from_user.id
    await send_portfolio(user_id, update.callback_query, context, show_in_group=True)

async def handle_menu_hide_portfolio(update: Update, context: CallbackContext) -> None:
    # This would trigger the hide_portfolio function
    job_data = {"chat_id": update.callback_query.message.chat.chat_id, "message_id": update.callback_query.message.message_id, "user_id": update.callback_query.from_user.id}
    await hide_portfolio_job(context)






# Step 4: Send portfolio function and display buttons for further actions
async def send_portfolio(user_id, query, context: CallbackContext, show_in_group: bool) -> None:
    portfolio_data = fetch_all(
        'SELECT symbol, SUM(amount) as amount, AVG(initial_price) as initial_price, MAX(timestamp) as timestamp '
        'FROM portfolio WHERE user_id = ? GROUP BY symbol',
        (user_id,)
    )

    if portfolio_data:
        message = "📊 <b>Your Crypto Portfolio</b> 📊\n\n"
        message += "<i>Here’s a summary of your investments:</i>\n\n"

        # Loop through portfolio data and build the message
        for row in portfolio_data:
            symbol, amount, initial_price, timestamp = row
            message += (
                f"🔹 <b>{symbol}</b>\n"
                f"   • <b>Amount:</b> <code>{amount}</code>\n"
                f"   • <b>Avg. Price:</b> <code>${initial_price:.2f}</code>\n"
                f"   • <b>Last Added:</b> <code>{timestamp}</code>\n\n"
            )
    else:
        message = "🚫 <b>Your portfolio is currently empty.</b>\n\n"
        message += "💡 <i>Start by adding some coins to your portfolio using the options below.</i>"

    # Portfolio action buttons (keep them active regardless of admin status)
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Coins", callback_data='portfolio_add_coins'),
            InlineKeyboardButton("➖ Remove Coins", callback_data='portfolio_remove_coins')
        ],
        [
            InlineKeyboardButton("📊 View Performance", callback_data='performance'),
            InlineKeyboardButton("🔙 Back to Menu", callback_data='menu_hide_portfolio')
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the message
    if show_in_group:
        try:
            await query.message.edit_text(text=message, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to edit portfolio message: {e}")
    else:
        try:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to send portfolio message to private chat: {e}")





# Add Coins Button Handler - updates the main message with admin check
async def add_coin_handler(update: Update, context: CallbackContext) -> None:
    is_admin = await is_bot_admin(update, context)
    
    # If the bot is not an admin, show a warning and stop the process
    if not is_admin:
        await update.callback_query.answer(
            "⚠️ Permission Denied: The bot needs admin rights to perform this action. Please grant admin access and try again.",
            show_alert=True
        )
        return

    # Proceed with adding coins functionality if bot is an admin
    query = update.callback_query
    user_id = query.from_user.id

    # Set the context for adding coins and store the user ID
    context.chat_data['awaiting_add'] = True
    context.chat_data['adding_user_id'] = user_id

    # Prompt for coin input with a back button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data='cancel_add_and_back')]
    ])
    
    sent_message = await query.message.edit_text(
        '💰 <b>Add Coins to Your Portfolio</b> 💰\n\n'
        '📥 <i>Please enter the symbols and amounts in the format:</i>\n'
        '<pre>BTC100 ETH200 ...</pre>\n\n'
        '📝 <b>Example:</b> <i>To add 100 BTC and 200 ETH, type:</i>\n<pre>BTC100 ETH200</pre>\n\n'
        '⏳ <i>Wait while we verify the symbols and prices.</i>\n\n'
        '🔙 <i>Press "Back" if you want to cancel.</i>',
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Schedule to hide message and reset add mode after 30 seconds of inactivity
    context.job_queue.run_once(
        reset_add_mode_on_timeout, 
        30, 
        data={'chat_id': sent_message.chat_id, 'message_id': sent_message.message_id, 'user_id': user_id, 'chat_data': context.chat_data},
        name=f"add_timeout_{user_id}"
    )

    logging.info(f"User {user_id} is in add coin mode.")





# Callback handler for the back button - cancel add mode and go back to portfolio
async def cancel_add_and_back_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    # Cancel the add mode timeout job if it exists
    job_name = context.chat_data.get('add_timeout_job')
    if job_name:
        current_job = context.job_queue.get_jobs_by_name(job_name)
        if current_job:
            current_job[0].schedule_removal()  # Cancel the timeout job
            logging.info(f"Timeout job {job_name} canceled for user {user_id}")
        context.chat_data.pop('add_timeout_job', None)  # Remove the job from chat_data

    # Reset the add mode using chat_data, not full context
    reset_add_state(context.chat_data)

    # Fetch the user's portfolio and display it again in the same message
    await send_portfolio(user_id, query, context, show_in_group=True)


# Function to reset the add mode if the user doesn't respond in time
async def reset_add_mode_on_timeout(context: CallbackContext) -> None:
    job_data = context.job.data
    user_id = job_data['user_id']
    chat_id = job_data['chat_id']
    message_id = job_data['message_id']
    chat_data = job_data['chat_data']  # Get chat_data from job_data

    logging.info(f"Timeout reached. Resetting add mode for user {user_id}")

    # Check if 'awaiting_add' exists in chat_data
    if chat_data.get('awaiting_add'):
        # Reset the add mode (directly handling reset in this function)
        reset_add_state(chat_data)  # Pass chat_data instead of full context

        # Edit the message to inform the user that the session has expired
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    '⏳ <b>Add Coins Session Expired</b>\n\n'
                    '<i>It looks like you took too long to respond.</i>\n'
                    '<b>Please start the process again to add coins to your portfolio.</b>\n\n'
                    'Thank you for your understanding! 😊'
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logging.error(f"Failed to update message after timeout: {e}")
    else:
        logging.info(f"No active add mode found for user {user_id}.")








# Improved regex pattern to match inputs like "BTC100", "100ETH", "BTC100.5", "100.5ETH", etc.
symbols_pattern = re.compile(r'\b(?:(\d+(\.\d+)?)([A-Z]+)|([A-Z]+)(\d+(\.\d+)?))\b', re.IGNORECASE)

# Function to handle coin inputs
async def handle_coin_input(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    action_add = context.chat_data.get('awaiting_add')
    action_remove = context.chat_data.get('awaiting_remove')

    logging.info(f"User {user_id} entered message: {update.message.text}. Action Add: {action_add}, Action Remove: {action_remove}")

    # Ignore regular text input if no coin action is awaited
    if not (action_add or action_remove):
        logging.info(f"User {user_id} entered a regular message, no coin action to process.")
        return  # Ignore the message if no action to add/remove coins

    # Cancel the timeout job if the user submits input in time
    current_job_name = f"add_timeout_{user_id}"
    current_job = context.job_queue.get_jobs_by_name(current_job_name)
    if current_job:
        current_job[0].schedule_removal()

    # Update the existing message to show "Verifying your input..."
    message_id = update.message.message_id  # ID of the message to update
    chat_id = update.message.chat_id  # Chat ID to edit the same message

    # Show "Verifying your input" message while coins are being processed
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=(
            '🔄 <b>Verifying your input...</b> ⏳\n\n'
            '<i>Please wait while we verify the symbols and amounts.</i>'
        ),
        parse_mode='HTML'
    )

    # Handle adding coins
    if action_add and not action_remove:
        message_text = update.message.text.upper().strip()
        matches = symbols_pattern.findall(message_text)

        if matches:
            # Process the input to add coins and collect success/failure messages
            result_messages = await handle_multiple_coins(update, context, matches)
        else:
            result_messages = ["⚠️ Invalid format. Please use the correct format like BTC100."]
            logging.info(f"User {user_id} entered an invalid format.")

        # After adding or showing an error, offer the user options to add more or go back
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add More", callback_data='portfolio_add_coins')],
            [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')]
        ])

        # Update the same message with the result and provide options to continue
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="\n".join(result_messages),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        logging.info(f"Updated message with result for user {user_id}: {' '.join(result_messages)}")

        # Ensure reset of the 'add' mode
        reset_add_state(context)

        # Schedule deletion of both user input and result after timeout
        await reset_add_timeout(update, context, user_id)

# Function to reset the add state after completion
def reset_add_state(context: CallbackContext) -> None:
    # Reset 'awaiting_add' flag in the context or similar state reset logic
    context.chat_data['awaiting_add'] = None
    context.chat_data['awaiting_remove'] = None



def reset_add_timeout(context: CallbackContext, user_id: int) -> None:
    # Create a job name based on the user_id
    current_job_name = f"add_timeout_{user_id}"

    # Fetch the job by name
    current_job = context.job_queue.get_jobs_by_name(current_job_name)

    # If the job exists, cancel it
    if current_job:
        current_job[0].schedule_removal()
        logging.info(f"Timeout job reset for user {user_id}.")
    else:
        logging.info(f"No timeout job found for user {user_id}.")


        

# Function to reset the add state for the user
def reset_add_state(context: CallbackContext) -> None:
    # Reset the 'awaiting_add' state after processing
    context.chat_data['awaiting_add'] = None
    context.chat_data['awaiting_remove'] = None
    logging.info("Resetting add state.")

# Your existing function for processing multiple coins
async def handle_multiple_coins(update: Update, context: CallbackContext, matches: list) -> list:
    result = []
    # Process the matches and add coins to the portfolio
    for symbol, amount in matches:
        # Add the logic to add the coin to the user's portfolio (database or in-memory)
        result.append(f"✅ Added {symbol}{amount} to your portfolio.")
    return result


# Handle adding multiple coins from the input and return result messages
async def handle_multiple_coins(update: Update, context: CallbackContext, matches) -> None:
    user_id = update.message.from_user.id
    messages = []

    for match in matches:
        try:
            # Unpack based on the match (handling both symbol before and after amount)
            if match[0]:
                amount = match[0]  # amount
                symbol = match[2]  # symbol
            else:
                amount = match[4]  # amount
                symbol = match[3]  # symbol

            # Convert amount to Decimal for precision
            amount = Decimal(amount)

            # Try to fetch the price for the coin symbol
            price_data = get_crypto_price_cc(symbol)

            if price_data and 'price' in price_data:
                current_price = Decimal(price_data['price'])

                # Format amount and current_price for the output message
                amount_str = "{:.3f}".format(amount)
                current_price_str = "{:.3f}".format(current_price)

                # Add the coin using your add_coin method
                await add_coin(update, context, symbol, amount, messages)
            else:
                messages.append(f"❌ Could not fetch price for {symbol}. Skipping.")

        except (IndexError, ValueError, InvalidOperation) as e:
            messages.append(f"⚠️ Invalid entry: {match}. Skipping. Error: {str(e)}")
            continue

    # After processing, show buttons to add more or go back
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add More", callback_data='portfolio_add_coins')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio')]
    ])

    # Send a reply to the user with the results and buttons
    if messages:
        sent_message = await update.message.reply_text("\n".join(messages), parse_mode='HTML', reply_markup=keyboard)
    else:
        sent_message = await update.message.reply_text("✅ No new coins added. Please check your input.", reply_markup=keyboard)

    # Schedule deletion of both user input and result message after 15 seconds
    context.job_queue.run_once(delete_message_job, 15, data={
        'chat_id': update.message.chat_id,
        'message_id': sent_message.message_id
    })
    context.job_queue.run_once(delete_message_job, 15, data={
        'chat_id': update.message.chat_id,
        'message_id': update.message.message_id
    })

    # Reset the state after processing
    reset_add_state(context)



# Function to add coins and append the success message to result_messages
async def add_coin(update: Update, context: CallbackContext, symbol: str, amount: Decimal, result_messages: list) -> None:
    user_id = update.message.from_user.id
    price_data = get_crypto_price_cc(symbol)  # Replace with actual price fetch logic

    if price_data and 'price' in price_data:
        current_price = Decimal(price_data['price'])  # Store price as Decimal
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        query = 'INSERT INTO portfolio (user_id, symbol, amount, initial_price, timestamp) VALUES (?, ?, ?, ?, ?)'
        amount_str = "{:.3f}".format(amount)  # Format amount as a string with 3 decimal places
        parameters = (user_id, symbol, amount_str, str(current_price), timestamp)  # Convert Decimal to str
        execute_query(query, parameters)
        result_messages.append(f"✅ Successfully added {amount:.3f} {symbol} at ${current_price:.3f} per coin.")
        logging.info(f"Successfully added {amount} {symbol} for user {user_id}")
    else:
        result_messages.append(f"⚠️ Could not fetch price for {symbol}. Skipping.")
        logging.error(f"Could not fetch price for {symbol} for user {user_id}")


    

# Job to delete messages after a specified time
async def delete_message_job(context: CallbackContext) -> None:
    job_data = context.job.data
    try:
        await context.bot.delete_message(job_data['chat_id'], job_data['message_id'])
        logging.info(f"Deleted message for chat {job_data['chat_id']}")
    except Exception as e:
        logging.error(f"Failed to delete message: {e}")







# Remove Coins Button Handler - with check before proceeding
async def remove_coins_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    # Check if bot is admin before proceeding
    is_admin = await is_bot_admin(update, context)
    
# If the bot is not an admin, show a warning and stop the process
    if not is_admin:
        await update.callback_query.answer(
            "⚠️ Permission Denied: The bot needs admin rights to perform this action. Please grant admin access and try again.",
            show_alert=True
        )
        return


    # If the bot is an admin, proceed with the coin removal options
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖ Specific", callback_data='portfolio_remove_specific'),
            InlineKeyboardButton("🗑️ Remove All", callback_data='portfolio_remove_all')
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio'),
            InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')
        ]
    ])

    try:
        # Edit the same message to display options for removing coins
        await query.message.edit_text(
            "<b>⚠️ Remove Coins</b>\n\n"
            "Please choose one of the options below:\n"
            "➖ <i>Remove a specific coin from your portfolio.</i>\n"
            "🗑️ <i>Remove all coins from your portfolio.</i>\n"
            "🔙 <i>Go back to the main portfolio menu.</i>\n"
            "❌ <i>Cancel the operation.</i>",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    except BadRequest as e:
        logger.error(f"Failed to edit message: {e}")
        await query.message.reply_text(
            "❌ Could not edit the message. Please try again.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio'),
                    InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')
                ]
            ])
        )
    await query.answer()



# Function to reset remove mode if the user doesn't respond in time
async def reset_remove_mode_on_timeout(context: CallbackContext) -> None:
    job_data = context.job.data
    user_id = job_data['user_id']
    chat_id = job_data['chat_id']
    message_id = job_data['message_id']
    chat_data = job_data['chat_data']

    logging.info(f"Timeout reached. Resetting remove mode for user {user_id}")

    # Check if 'awaiting_remove' exists in chat_data
    if chat_data.get('awaiting_remove'):
        # Reset the remove mode
        reset_remove_state(chat_data)

        # Edit the message to inform the user that the session has expired
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    '⏳ <b>Remove Coins Session Expired</b>\n\n'
                    '<i>It seems you took too long to respond.</i>\n'
                    '<b>Please start the process again to remove coins from your portfolio.</b>\n\n'
                    'Thank you for your patience! 😊'
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logging.error(f"Failed to update message after timeout: {e}")
    else:
        logging.info(f"No active remove mode found for user {user_id}.")




# Remove Specific Coin Handler - prompts user to input symbol and amount
async def remove_specific_coin_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    # Set context for removing a specific coin
    context.chat_data['awaiting_remove'] = True
    context.chat_data['removing_user_id'] = user_id

    # Prompt user to enter the symbol and amount to remove
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')]  # Added Cancel button
    ])

    try:
        await query.message.edit_text(
            'Please enter the symbol and amount to remove in the format: BTC100 ETH200 ...',
            reply_markup=keyboard
        )
    except BadRequest as e:
        logger.error(f"Failed to edit message: {e}")
        await query.message.reply_text(
            "❌ Could not edit the message. Please try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')],
                [InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')]  # Added Cancel button
            ])
        )
    await query.answer()

# Cancel Remove Handler
async def cancel_remove(update: Update, context: CallbackContext) -> None:
    query = update.callback_query

    # Cancel the timeout job if it exists
    remove_timeout_job = context.chat_data.get('remove_timeout_job')
    if remove_timeout_job:
        current_job = context.job_queue.get_jobs_by_name(remove_timeout_job)
        if current_job:
            current_job[0].schedule_removal()  # Cancel the timeout job
            logging.info(f"Remove timeout job canceled for user {query.from_user.id}")

    # Reset removal context and return to the portfolio
    reset_remove_state(context.chat_data)

    await query.message.edit_text(
        'Remove operation has been cancelled.',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio')]
        ])
    )
    await query.answer()



# Process removing multiple coins based on user input - send notification and delete after 15 sec
async def process_coin_removal(update: Update, context: CallbackContext, matches) -> None:
    user_id = update.message.from_user.id
    removal_messages = []

    for match in matches:
        try:
            if match[0]:  # amount before symbol
                amount = match[0]
                symbol = match[2]
            else:  # symbol before amount
                amount = match[4]
                symbol = match[3]

            amount = Decimal(amount)
        except (IndexError, ValueError, InvalidOperation) as e:
            removal_messages.append(f"⚠️ Invalid entry: {match}. Skipping. Error: {str(e)}")
            continue

        # Fetch the user's portfolio for the given symbol
        rows = fetch_all('SELECT id, amount FROM portfolio WHERE user_id = ? AND symbol = ?', (user_id, symbol))
        if not rows:
            removal_messages.append(f"❌ {symbol} is not in your portfolio.")
            continue

        remaining_amount = amount

        # Process each row in the portfolio
        for row in rows:
            if remaining_amount <= 0:
                break
            id, current_amount = row
            current_amount = Decimal(current_amount)

            if current_amount >= remaining_amount:
                new_amount = current_amount - remaining_amount
                if new_amount == 0:
                    execute_query('DELETE FROM portfolio WHERE id = ?', (id,))
                else:
                    execute_query('UPDATE portfolio SET amount = ? WHERE id = ?', (float(new_amount), id))
                remaining_amount = Decimal(0)
            else:
                remaining_amount -= current_amount
                execute_query('DELETE FROM portfolio WHERE id = ?', (id,))

        if remaining_amount > 0:
            removal_messages.append(f"❌ Could not remove {amount:.3f} {symbol}. Only {amount - remaining_amount:.3f} was available.")
        else:
            removal_messages.append(f"✅ Successfully removed {amount:.3f} {symbol} from your portfolio.")

    # Send notification for removal results
    sent_message = await update.message.reply_text(
        "\n".join(removal_messages),
        parse_mode='HTML'
    )

    reset_remove_state(context)

    # Schedule deletion of both user input and result message after 15 seconds
    context.job_queue.run_once(delete_message_job, 15, data={
        'chat_id': update.message.chat_id,
        'message_id': sent_message.message_id
    })
    context.job_queue.run_once(delete_message_job, 15, data={
        'chat_id': update.message.chat_id,
        'message_id': update.message.message_id
    })


# Remove All Coins Handler - edit the same message and confirm removal
async def remove_all_coins(update: Update, context: CallbackContext) -> None:
    query = update.callback_query

    # Ask for confirmation before removing all coins
    keyboard = InlineKeyboardMarkup([
        # Two buttons in one line
        [InlineKeyboardButton("✅ Confirm Remove All", callback_data='confirm_remove_all'),
         InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')],
        # One button in the next line
        [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')]
    ])

    try:
        await query.message.edit_text(
            "<b>⚠️ Confirm Removal</b>\n\n"
            "Are you sure you want to remove <b>all coins</b> from your portfolio?\n\n"
            "Please confirm your action below:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    except BadRequest as e:
        logger.error(f"Failed to edit message: {e}")
        await query.message.reply_text(
            "❌ Could not edit the message. Please try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')]
            ])
        )
    await query.answer()



# Confirm Remove All Coins Handler
async def confirm_remove_all_coins(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    # Fetch the portfolio and check if there are coins to remove
    rows = fetch_all('SELECT id FROM portfolio WHERE user_id = ?', (user_id,))
    if rows:
        execute_query('DELETE FROM portfolio WHERE user_id = ?', (user_id,))
        message = "✅ All coins have been removed from your portfolio."
    else:
        message = "❌ Your portfolio is already empty."

    # Edit the same message to reflect the changes and show back button
    await query.message.edit_text(
        message,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')]
        ])
    )
    await query.answer()

# Cancel the removal operation
async def cancel_remove(update: Update, context: CallbackContext) -> None:
    query = update.callback_query

    # Reset removal context and return to the portfolio
    reset_remove_state(context.chat_data)

    await query.message.edit_text(
        'Remove operation has been cancelled.',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Portfolio", callback_data='back_to_portfolio')]
        ])
    )
    await query.answer()






# Function to check the performance of the portfolio with enhanced UI and formatting for total summary
async def check_performance(update: Update, context: CallbackContext) -> None:
    query = update.callback_query

    # Immediately update message with a "processing" message
    try:
        await query.message.edit_text(
            "🔍 <b>Analyzing Your Portfolio Performance...</b> ⏳\n\n"
            "<i>We're gathering the latest market data for your assets.</i>\n"
            "<i>This may take a few moments.</i> 📡\n\n"
            "🌐 <i>Connecting to the latest market trends...</i>\n"
            "📈 <i>Calculating gains and losses...</i>\n"
            "💡 <i>Generating your portfolio insights...</i>",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Failed to update with 'processing' message: {e}")
        return


    user_id = query.from_user.id
    
    # Fetch portfolio data
    rows = fetch_all(
        'SELECT symbol, SUM(amount) as amount, AVG(initial_price) as initial_price, MAX(timestamp) as timestamp '
        'FROM portfolio WHERE user_id = ? GROUP BY symbol',
        (user_id,)
    )

    # Set a limit for the maximum number of coins to display
    max_coins = 16

    if rows:
        # Initialize variables for totals
        message = "<b>📊 Portfolio Performance Overview</b>\n\n"
        total_initial_value = 0
        total_current_value = 0

        # Process each coin in the portfolio
        for i, row in enumerate(rows):
            if i >= max_coins:
                message += "<b>⚠️ Maximum coins displayed. Some coins are not shown.</b>\n\n"
                break

            symbol, amount, initial_price, timestamp = row
            price_data = get_crypto_price_cc(symbol)
            
            if price_data is not None and 'price' in price_data:
                current_price = price_data['price']
                initial_value = initial_price * amount
                current_value = current_price * amount
                value_change = current_value - initial_value
                percentage_change = (value_change / initial_value) * 100 if initial_value != 0 else 0

                total_initial_value += initial_value
                total_current_value += current_value

                # Append detailed performance information for each asset with formatted current value
                message += (
                    f"<b>{symbol}:</b>\n"
                    f"• <i>Amount:</i> {amount}\n"
                    f"• <i>Initial Value:</i> ${initial_value:.2f}\n"
                    f"• <i>Current Value:</i> ${current_value:.2f} "
                    f"({format_number(current_value)})\n"  # Add formatted current value here
                    f"• <i>Value Change:</i> ${value_change:.2f} "
                    f"({percentage_change:.2f}%)\n"
                    f"📅 <i>Last Added:</i> {timestamp}\n\n"
                )
            else:
                message += f"❌ Could not fetch current price for <b>{symbol}</b>\n\n"
        
        # Calculate total value change and percentage
        total_value_change = total_current_value - total_initial_value
        total_percentage_change = (total_value_change / total_initial_value) * 100 if total_initial_value != 0 else 0
        
        # Append the total portfolio summary with bold text and emojis for better separation
        message += (
            "=============================\n"  # Acts as a visual separator
            f"🟢 <b>💼 Total Portfolio Summary</b> 🟢\n\n"  # Bold text and emojis to highlight total portfolio
            f"• <b>Initial Value:</b> ${total_initial_value:.2f}\n"  # Bold for Initial Value
            f"• <b>Current Value:</b> ${total_current_value:.2f} "
            f"({format_number(total_current_value)})\n"  # Add formatted total current value
            f"• <b>Total Value Change:</b> ${total_value_change:.2f} "
            f"({total_percentage_change:.2f}%)\n\n"
            "=============================\n"
            f"👉 <i>Stay updated with @CoinradarAlerts</i>"
        )

        # Buttons for navigation (2 buttons in the first row, 1 button in the second row)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio'),  # Back to where they came from (send_portfolio)
                InlineKeyboardButton("📋 Menu", callback_data='menu_hide_portfolio')  # Go to hide_portfolio
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data='cancel_operation')  # Cancel button
            ]
        ])

        # Edit the message to show performance with buttons
        await query.message.edit_text(message, parse_mode='HTML', reply_markup=keyboard)

    else:
        # If the portfolio is empty, show an appropriate message
        await query.message.edit_text(
            "<b>❌ Your portfolio is empty.</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio'),
                    InlineKeyboardButton("📋 Menu", callback_data='menu_hide_portfolio')
                ],
                [
                    InlineKeyboardButton("❌ Cancel", callback_data='cancel_operation')
                ]
            ])
        )





# Adjusted Command handler to check for known cryptocurrency symbols and amounts
async def check_crypto_symbol(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    message_text = update.message.text.upper().strip()
    logging.info(f"check_crypto_symbol triggered with message: {message_text}")

    # Extract matches for coin symbols and amounts
    matches = symbols_pattern.findall(message_text)

    # Check if the message contains known symbols
    if matches:
        # If the user is in "add mode"
        if context.chat_data.get('awaiting_add') and context.chat_data.get('adding_user_id') == user_id:
            logging.info(f"Adding coins for user {user_id}")
            await handle_multiple_coins(update, context, matches)
        # If the user is in "remove mode"
        elif context.chat_data.get('awaiting_remove') and context.chat_data.get('removing_user_id') == user_id:
            logging.info(f"Removing coins for user {user_id}")
            await process_coin_removal(update, context, matches)
        else:
            logging.info(f"Ignoring input because user is not in 'add' or 'remove' mode.")
    else:
        logging.info(f"Unrecognized symbol or format: {message_text}")
        # If no valid action is expected from the user (not in 'add' or 'remove' mode), don't show the warning
        if context.chat_data.get('awaiting_add') or context.chat_data.get('awaiting_remove'):
            await update.message.reply_text(f"Unrecognized format: {message_text}. Please use a valid format like 'BTC100' or '100ETH'.")



# Inline button callback handler
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        # Handle inline help
        if data == 'inline_help':
            await inline_help_command(update, context)

        # Handle start command
        elif data == 'start':
            await start(update, context)

        # Handle refresh buttons (gas and calculator)
        elif data.startswith('refresh_'):
            if data == 'refresh_gas':
                data = get_eth_gas_price()
                if 'error' in data:
                    await query.message.edit_text(f"Error: {data['error']}\n\n@CoinradarAlerts")
                    return

                gas_data = data['result']
                base_fee = float(gas_data.get('suggestBaseFee', 0.0))
                message = (
                    f"<b>Ethereum Gas Prices</b>🚥\n"
                    f"🚶‍♂️Safe: {float(gas_data['SafeGasPrice']): .2f} Gwei\n"
                    f"🚗Propose: {float(gas_data['ProposeGasPrice']): .2f} Gwei\n"
                    f"🚀Fast: {float(gas_data['FastGasPrice']): .2f} Gwei\n"
                    f"Base Fee: {base_fee: .2f} Gwei\n"
                    f"👉@CoinradarAlerts"
                )

                keyboard = [
                    [InlineKeyboardButton("🔄 Refresh", callback_data='refresh_gas')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.message.edit_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

            elif data.startswith('refresh_calc_'):
                _, _, symbol, amount = data.split('_', 3)
                amount = float(amount)
                await convert_command(update, context, symbol, amount)
            else:
                symbol = data.split('_', 1)[1]
                data = get_crypto_price_cmc(symbol)
                if 'error' in data:
                    await query.message.edit_text(f"Error: {data['error']}\n\n@CoinradarAlerts")
                    return

                name = data['name']
                price = float(data['price'])  # Current price from CMC
                market_cap = data['market_cap']
                volume_24h = data['volume_24h']
                percent_change_1h = data['percent_change_1h']
                percent_change_24h = data['percent_change_24h']
                percent_change_7d = data['percent_change_7d']
                circulating_supply = data['circulating_supply']
                fdv = data['fdv']

                # Fetch high and low data from CryptoCompare
                historical_data = get_historical_data(symbol)  # Implement this function
                if 'error' in historical_data:
                    await update.message.reply_text("Failed to fetch high and low prices. Please try again later.")
                    return

                high_value = historical_data['high']
                low_value = historical_data['low']

                # Prepare the message
                message = (
                    f"<b><a href='https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/' "
                    f"disable_web_page_preview='true'>{name}</a> ({symbol})</b>\n"
                    f"<b>Price</b>: ${format_dynamic_price(price)} USD\n"
                    f"H|L: ${format_dynamic_price(high_value)} | ${format_dynamic_price(low_value)}\n"
                    f"1h:   {percent_change_1h:.2f}%\n"
                    f"24h:  {percent_change_24h:.2f}%\n"
                    f"7d:   {percent_change_7d:.2f}%\n"
                    f"Supply: {format_number(circulating_supply)}\n"
                    f"FDV: ${format_number(fdv)} USD\n"
                    f"Cap: ${format_number(market_cap)}\n"
                    f"Vol: ${format_number(volume_24h)}\n"
                    f"👉@CoinradarAlerts"
                )

                keyboard = [
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f'refresh_{symbol}')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.message.edit_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        # Handle double and half calculation buttons
        elif data.startswith('double_'):
            _, symbol, amount = data.split('_', 2)
            amount = float(amount) * 2
            await convert_command(update, context, symbol, amount)

        elif data.startswith('half_'):
            _, symbol, amount = data.split('_', 2)
            amount = float(amount) / 2
            await convert_command(update, context, symbol, amount)

        # Handle message deletion
        elif data == 'delete_message':
            await query.message.delete()

        # Handle portfolio management buttons
        elif data == 'portfolio_add_coins':
            await add_coin_handler(update, context)

        elif data == 'portfolio_remove_coins':
            # Check if bot is admin
            is_admin = await is_bot_admin(update, context)
            if not is_admin:
                await update.callback_query.answer(
                    "⚠️ Permission Denied: The bot needs admin rights to perform this action. Please grant admin access and try again.",
                    show_alert=True
                )
                return

            # Improved UI with better text formatting and button layout
            keyboard = [
                [
                    InlineKeyboardButton("➖ Specific", callback_data='portfolio_remove_specific'),
                    InlineKeyboardButton("🗑️ Remove All", callback_data='portfolio_remove_all')
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio'),
                    InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.edit_text(
                "<b>⚠️ Remove Coins</b>\n\n"
                "Please choose an option below:\n"
                "➖ <i>Remove a specific coin from your portfolio.</i>\n"
                "🗑️ <i>Remove all coins from your portfolio.</i>\n"
                "🔙 <i>Go back to the main portfolio menu.</i>",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

        elif data == 'portfolio_remove_specific':
            # Set remove mode and activate timeout
            context.chat_data['awaiting_remove'] = True
            context.chat_data['removing_user_id'] = query.from_user.id
            logging.info(f"Single remove mode activated for user {query.from_user.id}")

            # Edit the current message with enhanced text and buttons for coin removal
            sent_message = await query.message.edit_text(
                "<b>⚠️ Remove Specific Coin</b>\n\n"
                "Please enter the <b>symbol</b> and <b>amount</b> you want to remove in the format:\n"
                "<i>e.g., BTC10</i>\n\n"
                "Choose an action below:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔙 Back", callback_data='back_to_portfolio_remove'),
                        InlineKeyboardButton("❌ Cancel", callback_data='cancel_remove')
                    ]
                ]),
                parse_mode='HTML'
            )

            # Schedule timeout to reset remove mode if user doesn't provide input
            context.job_queue.run_once(
                reset_remove_mode_on_timeout, 
                30,  # Adjust the timeout duration as needed
                data={'chat_id': sent_message.chat_id, 'message_id': sent_message.message_id, 'user_id': query.from_user.id, 'chat_data': context.chat_data},
                name=f"remove_timeout_{query.from_user.id}"
            )



        # Remove All Handler
        elif data == 'remove_all':
            await remove_all_coins(update, context)

        # Confirm Remove All Handler
        elif data == 'confirm_remove_all':
            await confirm_remove_all_coins(update, context)

        # Cancel Remove Handler
        elif data == 'cancel_remove':
            await cancel_remove(update, context)

        # Performance handler
        elif data == 'performance':
            await check_performance(update, context)

        # Back to Portfolio handler (navigates back to the main portfolio options)
        elif data == 'back_to_portfolio':
            query = update.callback_query
            user_id = query.from_user.id
            
            # Use the hide_portfolio_job to return to the main portfolio menu
            await hide_portfolio_job(context, query.message.chat_id, query.message.message_id, user_id)
      
        # Example usage when calling hide_portfolio_job
        elif data == 'menu_hide_portfolio':
            chat_id = query.message.chat_id
            message_id = query.message.message_id
            user_id = query.from_user.id
            await hide_portfolio_job(context, chat_id, message_id, user_id)

        # Handle the Cancel Operation
        elif data == 'cancel_operation':
            await query.message.edit_text(
                "<b>Operation cancelled.</b> You are back at the menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data='menu_hide_portfolio')]
                ]),
                parse_mode='HTML'
            )
        
        # Callback handler for the back button - return to remove_coins_handler
        elif data == 'back_to_portfolio_remove':
            # Cancel the timeout job if it exists
            remove_timeout_job = context.chat_data.get('remove_timeout_job')
            if remove_timeout_job:
                current_job = context.job_queue.get_jobs_by_name(remove_timeout_job)
                if current_job:
                    current_job[0].schedule_removal()  # Cancel the timeout job
                    logging.info(f"Remove timeout job canceled for user {query.from_user.id}")

            # Reset remove mode
            reset_remove_state(context.chat_data)

            # Return to the remove coins handler
            await remove_coins_handler(update, context)  # Call the remove_coins_handler again


        # Callback handler for the back button - cancel add mode and go back to portfolio
        elif data == 'cancel_add_and_back':
            # Reset add mode by passing context.chat_data (not context itself)
            reset_add_state(context.chat_data)  # Fix here: pass context.chat_data instead of context

            # Get user ID from callback query
            user_id = update.callback_query.from_user.id

            # Fetch the user's portfolio and display it again in the same message
            query = update.callback_query

            # Display the portfolio again in the same message
            await send_portfolio(user_id, query, context, show_in_group=True)



    except BadRequest as e:
        if str(e) == "Message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message":
            logger.info("No changes detected in the message content and reply markup.")
        else:
            raise e

        
        
# Handler for when the bot is added to a group
async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_member = update.my_chat_member
        if chat_member.new_chat_member.user.id == context.bot.id and chat_member.new_chat_member.status in ["member", "administrator"]:
            keyboard = [
                [
                    InlineKeyboardButton("Help", callback_data='inline_help'),
                    InlineKeyboardButton("Join Channel", url='https://t.me/CoinradarAlerts'),
                ],
                [
                    InlineKeyboardButton("⚙️ Settings", url='https://t.me/Coinradarprobot')  # This will open a private chat with the bot
                ]
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=chat_member.chat.id, text="Hello everyone! I'm the Crypto Bot. Please choose an option:", reply_markup=reply_markup)
    except AttributeError as e:
        logger.error(f"AttributeError: {e}")
    except Exception as e:
        logger.error(f"An error occurred in my_chat_member_handler: {e}")


# function to set bot commands
async def set_bot_commands(context: ContextTypes.DEFAULT_TYPE) -> None:
    # General commands available to everyone in private chats
    general_commands = [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="p", description="Get the latest price of a cryptocurrency"),
        BotCommand(command="gas", description="Get the latest Ethereum gas prices"),
        BotCommand(command="mp", description="Get prices of multiple coins"),
        BotCommand(command="top10", description="Get the top 10 cryptocurrencies by market cap"),
        BotCommand(command="my", description="Save your favorite symbols and view their prices"),
        BotCommand(command="help", description="List available commands"),
        BotCommand(command="calc", description="Convert to USD and BTC"),
        BotCommand(command="portfolio", description="Manage your portfolio"),
        BotCommand(command="ath", description="Get all-time high prices"),
        BotCommand(command="atl", description="Get all-time low prices"),
        BotCommand(command="cap", description="Get global market cap details"),
        BotCommand(command="h", description="Get historical price data"),
        BotCommand(command="market", description="Get market details for a coin"),
        BotCommand(command="alert", description="Set a price alert"),
        BotCommand(command="check_alerts", description="Check your alerts"),
        BotCommand(command="trending", description="Get the top 20 trending coins"),
        BotCommand(command="whales", description="Get the latest whale alerts"),
        BotCommand(command="fng", description="Get the Crypto Fear and Greed Index"),
        BotCommand(command="news", description="Get the latest crypto news"),
        BotCommand(command="events", description="Get upcoming crypto events"),
    ]

    # General commands available to everyone in group chats
    group_general_commands = [
        BotCommand(command="p", description="Get the latest price of a cryptocurrency"),
        BotCommand(command="gas", description="Get the latest Ethereum gas prices"),
        BotCommand(command="mp", description="Get prices of multiple coins"),
        BotCommand(command="top10", description="Get the top 10 cryptocurrencies by market cap"),
        BotCommand(command="my", description="Save your favorite symbols and view their prices"),
        BotCommand(command="help", description="List available commands"),
        BotCommand(command="calc", description="Convert to USD and BTC"),
        BotCommand(command="portfolio", description="Manage your portfolio"),
        BotCommand(command="ath", description="Get all-time high prices"),
        BotCommand(command="atl", description="Get all-time low prices"),
        BotCommand(command="cap", description="Get global market cap details"),
        BotCommand(command="h", description="Get historical price data"),
        BotCommand(command="market", description="Get market details for a coin"),
        BotCommand(command="alert", description="Set a price alert"),
        BotCommand(command="check_alerts", description="Check your alerts"),
        BotCommand(command="trending", description="Get the top 20 trending coins"),
        BotCommand(command="whales", description="Get the latest whale alerts"),
        BotCommand(command="fng", description="Get the Crypto Fear and Greed Index"),
        BotCommand(command="news", description="Get the latest crypto news"),
        BotCommand(command="events", description="Get upcoming crypto events"),
    ]

    # Sensitive admin commands
    admin_commands = [
        BotCommand(command="reload", description="Reload the bot"),
        BotCommand(command="backup", description="Backup the database"),
        BotCommand(command="restore", description="Restore the database from backup"),
        BotCommand(command="clean", description="Clean the database"),
        BotCommand(command="status", description="Check the bot status"),
    ]

    # Combine general commands with admin commands for the primary admin
    primary_admin_commands = general_commands + admin_commands

    # Set commands for all users in private chats (default scope)
    await context.bot.set_my_commands(general_commands, scope=BotCommandScopeDefault())

    # Set commands for all users (default scope)
    await context.bot.set_my_commands(group_general_commands, scope=BotCommandScopeDefault())

    # Set commands for primary admin (individual chat)
    for primary_admin_id in PRIMARY_ADMIN_USER_IDS:
        await context.bot.set_my_commands(primary_admin_commands, scope=BotCommandScopeChat(chat_id=int(primary_admin_id)))

# Main function to run the bot
def main():
    init_db()
    init_api_keys()  # Initialize API keys in the database
    for admin_id in PRIMARY_ADMIN_USER_IDS:
        add_primary_admin(admin_id)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler('help', command_help))
    application.add_handler(CommandHandler("p", p_command))
    application.add_handler(CommandHandler("gas", gas_command))
    application.add_handler(CommandHandler("h", history_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("top10", top10))
    application.add_handler(CommandHandler("ath", ath_command_cc))  
    application.add_handler(CommandHandler("atl", atl_command_cc))  
    application.add_handler(CommandHandler("cap", cap_command))
    application.add_handler(CommandHandler("whales", whales_command))
    application.add_handler(CommandHandler("market", market_command))
    application.add_handler(CommandHandler("mp", mp_command))
    application.add_handler(CommandHandler("calc", convert_command))
    application.add_handler(CommandHandler("my", my_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("restore", restore_command))
    application.add_handler(CommandHandler("clean", clean_database))
    application.add_handler(CommandHandler("reload", reload_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check_alerts", check_alerts_command))
    application.add_handler(CommandHandler("fng", fng_command))
    application.add_handler(CommandHandler("news", news_command)) 
    application.add_handler(CommandHandler("trending", trend_command))  
    application.add_handler(CommandHandler("events", events_command)) 
    application.add_handler(CommandHandler("portfolio", show_portfolio))
    application.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

   

    # --- Add Command Handlers ---
    
   # Handlers for commands and callback queries
    application.add_handler(CommandHandler("portfolio", show_portfolio))
    application.add_handler(CallbackQueryHandler(handle_portfolio_choice, pattern='view_portfolio_'))
    application.add_handler(CallbackQueryHandler(handle_back_to_portfolio, pattern=r'^back_to_portfolio$'))
    application.add_handler(CallbackQueryHandler(add_coin_handler, pattern='portfolio_add_coins'))
    application.add_handler(CallbackQueryHandler(remove_coins_handler, pattern='portfolio_remove_coins'))



    # Inline button callback handler
    application.add_handler(CallbackQueryHandler(button))

    # Add the message handler for file uploads
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file_upload))

    # Cryptocurrency symbol handling (case-insensitive)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(symbols_pattern), check_crypto_symbol))

    # Add the message handler for handling the input during the remove coin operation
    message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coin_input)
    application.add_handler(message_handler)

    # Register handlers for button clicks
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_crypto_symbol))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_coin_input))



    # General message handler (for adding/removing coins)
    application.add_handler(CommandHandler("portfolio", portfolio_handler))

    
    # --- Job Scheduling ---

    # Delay job processing slightly to ensure everything initializes
    time.sleep(5)

    # Set the initial commands at startup (delayed to ensure bot is added to groups)
    application.job_queue.run_once(set_bot_commands, when=timedelta(seconds=20))

    # Check alerts every 30 seconds
    application.job_queue.run_repeating(check_alerts, interval=timedelta(seconds=57), first=timedelta(seconds=30))

    # Daily job to send report at midnight (12:00 AM)
    application.job_queue.run_daily(send_daily_report, time=dt_time(hour=0, minute=0))

    # Schedule weekly backup at 10:00 AM every Sunday Singapore time
    schedule_weekly_backup(application)

    # Schedule nightly reload at 4:00 AM Singapore time
    schedule_nightly_reload(application)

    # Periodic job to check API statuses every hour
    application.job_queue.run_repeating(periodic_check_api_status, interval=timedelta(hours=1), first=timedelta(minutes=3))

    # Periodic job to sync API keys every hour
    application.job_queue.run_repeating(periodic_sync_keys, interval=timedelta(hours=1), first=timedelta(minutes=1))

    # --- Startup Notification ---
    
    # Record bot start time
    application.bot_data["start_time"] = datetime.now()

    # Notify primary admins about successful bot startup
    async def notify_startup(context: ContextTypes.DEFAULT_TYPE):
        for admin_id in PRIMARY_ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text="Bot has started successfully.")
            except Exception as e:
                logger.error(f"Failed to send startup notification to primary admin {admin_id}: {e}")

    # Schedule the startup notification after a slight delay
    application.job_queue.run_once(notify_startup, when=timedelta(seconds=30))

    # --- Start Polling ---
    
    # Run the bot until user presses Ctrl-C
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()




#All Working Fine I will add more features in future
# Name : CoinRadar Peo
# version : 3.0.1
# Developer : @CoinRadarAlerts
# All Issues Fixed and Working Fine
