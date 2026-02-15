import os
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
import openai
import aiohttp
import base64
from collections import defaultdict
import json
from datetime import datetime, timedelta
import asyncio
from flask import Flask
from threading import Thread

# --- KOYEB KEEP-ALIVE SERVER ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_web_server():
    # Koyeb passes the port via environment variable, default to 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# --- BOT CONFIGURATION ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# Environment Variables
POE_API_KEY = os.getenv("POE_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_IDS = os.getenv("ADMIN_IDS", "").split(",")
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "Admin")

# Persistent storage files (Note: Koyeb filesystem is ephemeral)
RATE_LIMITS_FILE = "rate_limits.json"
BOT_STATE_FILE = "bot_state.json"
USER_ACCEPTANCES_FILE = "user_acceptances.json"

tutor_conversation_history = defaultdict(list)
standard_conversation_history = defaultdict(list)
MAX_HISTORY_LENGTH = 50

rate_limits = {"global": {}, "users": {}}
bot_state = {"enabled": True, "disable_until": None}
user_messages = defaultdict(lambda: defaultdict(list))
user_acceptances = {}

custom_prompt = """# Mr. Tutor – Core Guidelines
You are in a roleplay as "Mr. Tutor"!
Act like a teacher. Never reveal the final answer directly. 
Guide, question, and encourage the learner to discover the solution themselves."""

poe_client = openai.OpenAI(
    api_key=POE_API_KEY,
    base_url="https://api.poe.com/v1",
)

COMMAND_CONFIGS = [
    ("tutorplus", "Gemini-2.5-Flash-Tut", True, "plus"),
    ("tutorminus", "Gemini-2.5-Flash-Lite", True, "minus"),
    ("imageplus", "GPT-Image-1-Mini", False, "imageplus"),
    ("standardplus", "Gemini-2.5-Flash-Tut", False, "nonplus"),
    ("standardminus", "Gemini-2.5-Flash-Lite", False, "nonminus"),
    ("tutor", "tester-kimi-k2-non", True, "normal"),
    ("image", "FLUX-schnell", False, "image"),
    ("standard", "tester-kimi-k2-non", False, "nonnormal"),
    ("tut+", "Gemini-2.5-Flash-Tut", True, "plus"),
    ("tut-", "Gemini-2.5-Flash-Lite", True, "minus"),
    ("tut", "tester-kimi-k2-non", True, "normal"),
    ("ti+", "GPT-Image-1-Mini", False, "imageplus"),
    ("ti", "FLUX-schnell", False, "image"),
    ("tn+", "Gemini-2.5-Flash-Tut", False, "nonplus"),
    ("tn-", "Gemini-2.5-Flash-Lite", False, "nonminus"),
    ("tn", "tester-kimi-k2-non", False, "nonnormal"),
    ("t+", "Gemini-2.5-Flash-Tut", True, "plus"),
    ("t-", "Gemini-2.5-Flash-Lite", True, "minus"),
    ("t", "tester-kimi-k2-non", True, "normal"),
]

# --- HELPER FUNCTIONS ---
def load_json(filename, default):
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving {filename}: {e}")

def load_persistent_data():
    global rate_limits, bot_state, user_acceptances
    rate_limits = load_json(RATE_LIMITS_FILE, {"global": {}, "users": {}})
    bot_state = load_json(BOT_STATE_FILE, {"enabled": True, "disable_until": None})
    user_acceptances = load_json(USER_ACCEPTANCES_FILE, {})

def save_rate_limits(): save_json(RATE_LIMITS_FILE, rate_limits)
def save_bot_state(): save_json(BOT_STATE_FILE, bot_state)
def save_user_acceptances(): save_json(USER_ACCEPTANCES_FILE, user_acceptances)

def is_admin(user_id, member=None):
    if str(user_id) in ADMIN_IDS and ADMIN_IDS[0] != "": return True
    if member and hasattr(member, 'roles'):
        for role in member.roles:
            if role.name == ADMIN_ROLE_NAME: return True
    return False

def check_bot_state():
    if not bot_state["enabled"] and bot_state["disable_until"]:
        if datetime.now().timestamp() >= bot_state["disable_until"]:
            bot_state["enabled"] = True
            bot_state["disable_until"] = None
            save_bot_state()
    return bot_state["enabled"]

def check_rate_limit(user_id, command):
    now = datetime.now().timestamp()
    user_id_str = str(user_id)
    if user_id in user_messages and command in user_messages[user_id]:
        user_messages[user_id][command] = [ts for ts in user_messages[user_id][command] if now - ts < 3600]
    
    if user_id_str in rate_limits["users"] and command in rate_limits["users"][user_id_str]:
        limit = rate_limits["users"][user_id_str][command]
        if "expires" in limit and limit["expires"] and now >= limit["expires"]:
            del rate_limits["users"][user_id_str][command]
            save_rate_limits()
        else:
            # Simple check logic
            pass 
    return True, None

def record_message(user_id, command):
    user_messages[user_id][command].append(datetime.now().timestamp())

def needs_acceptance(user_id):
    user_id_str = str(user_id)
    if user_id_str not in user_acceptances: return True
    last = datetime.fromtimestamp(user_acceptances[user_id_str])
    return datetime.now() - last > timedelta(days=30)

class AcceptanceView(View):
    def __init__(self, user_id, callback):
        super().__init__(timeout=300)
        self.user_id, self.callback = user_id, callback

    @discord.ui.button(label="Accept & Continue", style=discord.ButtonStyle.green)
    async def accept(self, interaction, button):
        if interaction.user.id != self.user_id: return
        user_acceptances[str(self.user_id)] = datetime.now().timestamp()
        save_user_acceptances()
        await interaction.response.send_message("✅ Accepted!", ephemeral=True)
        await self.callback()
        self.stop()

# --- CORE LOGIC ---
async def process_attachments(attachments):
    contents = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            async with session.get(att.url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if any(att.filename.lower().endswith(e) for e in ['.png', '.jpg', '.jpeg']):
                        b64 = base64.b64encode(data).decode('utf-8')
                        contents.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
                    else:
                        try: contents.append({"type": "text", "text": f"File: {att.filename}\n```{data.decode('utf-8')[:2000]}```"})
                        except: pass
    return contents

def query_poe(user_id, user_prompt, attachment_contents=None, model="tester-kimi-k2-non", use_tutor_prompt=True):
    history = tutor_conversation_history if use_tutor_prompt else standard_conversation_history
    content = [{"type": "text", "text": user_prompt}] + (attachment_contents or [])
    history[user_id].append({"role": "user", "content": content})
    
    messages = []
    if use_tutor_prompt: messages.append({"role": "system", "content": custom_prompt})
    messages.extend(history[user_id][-MAX_HISTORY_LENGTH:])

    try:
        chat = poe_client.chat.completions.create(model=model, messages=messages)
        resp = chat.choices[0].message.content
        history[user_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e: return f"Error: {e}"

async def generate_image(prompt, model):
    try:
        chat = poe_client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}])
        return chat.choices[0].message
    except Exception as e: return str(e)

async def execute_command(channel, user, attachments, model, use_tutor, command_type, user_query, is_image_gen, thinking_msg=None):
    record_message(user.id, command_type)
    if is_image_gen:
        resp = await generate_image(user_query or "AI Art", model)
        if thinking_msg: await thinking_msg.delete()
        content = resp.content if hasattr(resp, 'content') else str(resp)
        await channel.send(f"🎨 **{user.name}'s Image:** {content}")
    else:
        att_data = await process_attachments(attachments)
        reply = query_poe(user.id, user_query or "Hello", att_data, model, use_tutor)
        if thinking_msg: await thinking_msg.delete()
        for i in range(0, len(reply), 2000): await channel.send(reply[i:i+2000])

async def process_command_logic(channel, user, message_content, attachments, model, use_tutor, command_type, user_query, is_image_gen, thinking_msg=None):
    if not use_tutor and not is_image_gen and needs_acceptance(user.id):
        view = AcceptanceView(user.id, lambda: execute_command(channel, user, attachments, model, use_tutor, command_type, user_query, is_image_gen))
        await channel.send("⚠️ You must accept terms for non-tutor models.", view=view)
    else:
        await execute_command(channel, user, attachments, model, use_tutor, command_type, user_query, is_image_gen, thinking_msg)

# --- EVENTS & SLASH COMMANDS ---
@bot.event
async def on_ready():
    load_persistent_data()
    await bot.tree.sync()
    print(f'✅ {bot.user} is online on Koyeb!')

@bot.tree.command(name="tutor", description="Ask Mr. Tutor")
async def slash_tutor(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    thinking = await interaction.followup.send("📚 Thinking...")
    await process_command_logic(interaction.channel, interaction.user, message, [], "tester-kimi-k2-non", True, "normal", message, False, thinking)

@bot.event
async def on_message(message):
    if message.author == bot.user or not check_bot_state(): return
    
    if message.content.startswith("$"):
        content_lower = message.content.lower()
        for prefix, m, tutor, cmd_type in COMMAND_CONFIGS:
            if content_lower.startswith(f"${prefix}"):
                query = message.content[len(prefix)+1:].strip()
                await process_command_logic(message.channel, message.author, message.content, message.attachments, m, tutor, cmd_type, query, "image" in cmd_type)
                break

# --- STARTUP ---
if __name__ == "__main__":
    keep_alive() # Starts the Flask server in a thread
    bot.run(DISCORD_BOT_TOKEN)
