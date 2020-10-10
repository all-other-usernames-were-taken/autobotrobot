import discord
import toml
import logging
import subprocess
import discord.ext.commands as commands
import discord.ext.tasks as tasks
import re
import asyncio
import json
import argparse
import traceback
import random
import rolldice
from datetime import timezone, datetime

import tio
import db
import util

def timestamp(): return int(datetime.now(tz=timezone.utc).timestamp())

# TODO refactor this
database = None

config = toml.load(open("config.toml", "r"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s %(message)s", datefmt="%H:%M:%S %d/%m/%Y")

bot = commands.Bot(command_prefix=config["prefix"], description="AutoBotRobot, the most useless bot in the known universe.", case_insensitive=True)
bot._skip_check = lambda x, y: False

def make_embed(*, fields=[], footer_text=None, **kwargs):
    embed = discord.Embed(**kwargs)
    for field in fields:
        if len(field) > 2:
            embed.add_field(name=field[0], value=field[1], inline=field[2])
        else:
            embed.add_field(name=field[0], value=field[1], inline=False)
    if footer_text:
        embed.set_footer(text=footer_text)
    return embed

def error_embed(msg, title="Error"):
    return make_embed(color=config["colors"]["error"], description=msg, title=title)

cleaner = discord.ext.commands.clean_content()
def clean(ctx, text):
    return cleaner.convert(ctx, text)

@bot.event
async def on_message(message):
    words = message.content.split(" ")
    if len(words) == 10 and message.author.id == 435756251205468160:
        await message.channel.send(util.unlyric(message.content))
    else:
        if message.author.id == bot.user.id: return
        ctx = await bot.get_context(message)
        if not ctx.valid: return
        await bot.invoke(ctx)

@bot.event
async def on_command_error(ctx, err):
    print(ctx, err)
    if isinstance(err, commands.CommandNotFound, commands.CheckFailure): return
    try:
        trace = re.sub("\n\n+", "\n", "\n".join(traceback.format_exception(err, err, err.__traceback__)))
        print(trace)
        await ctx.send(embed=error_embed(gen_codeblock(trace), title="Internal error"))
    except Exception as e: print("meta-error:", e)

@bot.command(help="Gives you a random fortune as generated by `fortune`.")
async def fortune(ctx):
    await ctx.send(subprocess.run(["fortune"], stdout=subprocess.PIPE, encoding="UTF-8").stdout)

@bot.command(help="Generates an apioform type.")
async def apioform(ctx):
    await ctx.send(util.apioform())

@bot.command(help="Says Pong.")
async def ping(ctx):
    await ctx.send("Pong.")

@bot.command(help="Deletes the specified target.", rest_is_raw=True)
async def delete(ctx, *, raw_target):
    target = await clean(ctx, raw_target.strip().replace("\n", " "))
    if len(target) > 256:
        await ctx.send(embed=error_embed("Deletion target must be max 256 chars"))
        return
    async with ctx.typing():
        await ctx.send(f"Deleting {target}...")
        await asyncio.sleep(1)
        await database.execute("INSERT INTO deleted_items (timestamp, item) VALUES (?, ?)", (timestamp(), target))
        await database.commit()
        await ctx.send(f"Deleted {target} successfully.")

@bot.command(help="View recently deleted things, optionally matching a filter.")
async def list_deleted(ctx, search=None):
    acc = "Recently deleted:\n"
    if search: acc = f"Recently deleted (matching {search}):\n"
    csr = None
    if search:
        csr = database.execute("SELECT * FROM deleted_items WHERE item LIKE ? ORDER BY timestamp DESC LIMIT 100", (f"%{search}%",))
    else:
        csr = database.execute("SELECT * FROM deleted_items ORDER BY timestamp DESC LIMIT 100")
    async with csr as cursor:
        async for row in cursor:
            to_add = "- " + row[2].replace("```", "[REDACTED]") + "\n"
            if len(acc + to_add) > 2000:
                break
            acc += to_add
    await ctx.send(acc)

# Python, for some *very intelligent reason*, makes the default ArgumentParser exit the program on error.
# This is obviously undesirable behavior in a Discord bot, so we override this.
class NonExitingArgumentParser(argparse.ArgumentParser):
    def exit(self, status=0, message=None):
        if status:
            raise Exception(f'Flag parse error: {message}')
        exit(status)

EXEC_REGEX = "^(.*)```([a-zA-Z0-9_\\-+]+)?\n(.*)```$"

exec_flag_parser = NonExitingArgumentParser(add_help=False)
exec_flag_parser.add_argument("--verbose", "-v", action="store_true")
exec_flag_parser.add_argument("--language", "-L")

def gen_codeblock(content):
    return "```\n" + content.replace("```", "\\`\\`\\`")[:1900] + "\n```"

@bot.command(rest_is_raw=True, help="Execute provided code (in a codeblock) using TIO.run.")
async def exec(ctx, *, arg):
    match = re.match(EXEC_REGEX, arg, flags=re.DOTALL)
    if match == None:
        await ctx.send(embed=error_embed("Invalid format. Expected a codeblock."))
        return
    flags_raw = match.group(1)
    flags = exec_flag_parser.parse_args(flags_raw.split())
    lang = flags.language or match.group(2)
    if not lang:
        await ctx.send(embed=error_embed("No language specified. Use the -L flag or add a language to your codeblock."))
        return
    lang = lang.strip()
    code = match.group(3)

    async with ctx.typing():
        ok, real_lang, result, debug = await tio.run(lang, code)
        if not ok:
            await ctx.send(embed=error_embed(gen_codeblock(result), "Execution failed"))
        else:
            out = result
            if flags.verbose: 
                debug_block = "\n" + gen_codeblock(f"""{debug}\nLanguage:  {real_lang}""")
                out = out[:2000 - len(debug_block)] + debug_block
            else:
                out = out[:2000]
            await ctx.send(out)

@bot.command(help="List supported languages, optionally matching a filter.")
async def supported_langs(ctx, search=None):
    langs = sorted(tio.languages())
    acc = ""
    for lang in langs:
        if len(acc + lang) > 2000:
            await ctx.send(acc)
            acc = ""
        if search == None or search in lang: acc += lang + " "
    if acc == "": acc = "No results."
    await ctx.send(acc)

@bot.command(brief="Set a reminder to be reminded about later.", rest_is_raw=True, help="""Sets a reminder which you will (probably) be reminded about at/after the specified time.
All times are UTC.
Reminders are checked every minute, so while precise times are not guaranteed, reminders should under normal conditions be received within 2 minutes of what you specify.""")
async def remind(ctx, time, *, reminder):
    reminder = reminder.strip()
    if len(reminder) > 512:
        await ctx.send(embed=error_embed("Maximum reminder length is 512 characters", "Foolish user error"))
        return
    extra_data = {
        "author_id": ctx.author.id,
        "channel_id": ctx.message.channel.id,
        "message_id": ctx.message.id,
        "guild_id": ctx.message.guild and ctx.message.guild.id,
        "original_time_spec": time
    }
    try:
        time = util.parse_time(time)
    except:
        await ctx.send(embed=error_embed("Invalid time"))
        return
    await database.execute("INSERT INTO reminders (remind_timestamp, created_timestamp, reminder, expired, extra) VALUES (?, ?, ?, ?, ?)", 
        (time.timestamp(), timestamp(), reminder, 0, json.dumps(extra_data, separators=(',', ':'))))
    await database.commit()
    await ctx.send(f"Reminder scheduled for {util.format_time(time)}.")

async def send_to_channel(info, text):
    channel = bot.get_channel(info["channel_id"])
    if not channel: raise Exception(f"channel {info['channel_id']} unavailable/nonexistent")
    await channel.send(text)

async def send_by_dm(info, text):
    user = bot.get_user(info["author_id"])
    if not user: raise Exception(f"user {info['author_id']} unavailable/nonexistent")
    if not user.dm_channel: await user.create_dm()
    await user.dm_channel.send(text)

async def send_to_guild(info, text):
    guild = bot.get_guild(info["guild_id"])
    member = guild.get_member(info["author_id"])
    self = guild.get_member(bot.user.id)
    # if member is here, find a channel they can read and the bot can send in
    if member:
        for chan in guild.text_channels:
            if chan.permissions_for(member).read_messages and chan.permissions_for(self).send_messages:
                await chan.send(text)
                return
    # if member not here or no channel they can read messages in, send to any available channel
    for chan in guild.text_channels:
        if chan.permissions_for(self).send_messages:
            await chan.send(text)
            return
    raise Exception(f"guild {info['author_id']} has no (valid) channels")

remind_send_methods = [
    ("original channel", send_to_channel),
    ("direct message", send_by_dm),
    ("originating guild", send_to_guild)
]

@tasks.loop(seconds=60)
async def remind_worker():
    csr = database.execute("SELECT * FROM reminders WHERE expired = 0 AND remind_timestamp < ?", (timestamp(),))
    to_expire = []
    async with csr as cursor:
        async for row in cursor:
            rid, remind_timestamp, created_timestamp, reminder_text, _, extra = row
            try:
                remind_timestamp = datetime.utcfromtimestamp(remind_timestamp)
                created_timestamp = datetime.utcfromtimestamp(created_timestamp)
                extra = json.loads(extra)
                uid = extra["author_id"]
                text = f"<@{uid}> Reminder queued at {util.format_time(created_timestamp)}: {reminder_text}"

                for method_name, func in remind_send_methods:
                    print("trying", method_name, rid)
                    try:
                        await func(extra, text)
                        to_expire.append(rid)
                        break
                    except Exception as e: logging.warning("failed to send %d to %s", rid, method_name, exc_info=e)
            except Exception as e:
                logging.warning("Could not send reminder %d", rid, exc_info=e)
    for expiry_id in to_expire:
        logging.info("Expiring reminder %d", expiry_id)
        await database.execute("UPDATE reminders SET expired = 1 WHERE id = ?", (expiry_id,))
    await database.commit()

@bot.command(help="Get some information about the bot.")
async def about(ctx):
    await ctx.send("""**AutoBotRobot: The least useful Discord bot ever designed.**
AutoBotRobot has many features, but not necessarily any practical ones.
It can execute code via TIO.run, do reminders, print fortunes, and not any more!
AutoBotRobot is open source - the code is available at <https://github.com/osmarks/autobotrobot> - and you could run your own instance if you wanted to and could get around the complete lack of user guide or documentation.
You can also invite it to your server: <https://discordapp.com/oauth2/authorize?&client_id=509849474647064576&scope=bot&permissions=68608>
""")

@bot.command(help="Randomly generate an integer using dice syntax.", name="random", rest_is_raw=True)
async def random_int(ctx, *, dice):
    await ctx.send(rolldice.roll_dice(dice)[0])

bad_things = ["lyric", "endos", "solarflame", "lyric", "319753218592866315", "andrew", "6", "c++"]
good_things = ["potato", "heav", "gollark", "helloboi", "bees", "hellboy", "rust", "ferris", "crab", "transistor"]
negations = ["not", "bad", "un", "kill", "n't"]
def weight(thing):
    lthing = thing.lower()
    weight = 1.0
    if lthing == "c": weight *= 0.3
    for bad_thing in bad_things:
        if bad_thing in lthing: weight *= 0.5
    for good_thing in good_things:
        if good_thing in lthing: weight *= 2.0
    for negation in negations:
        for _ in range(lthing.count(negation)): weight = 1 / weight
    return weight

@bot.command(help="'Randomly' choose between the specified options.", name="choice", aliases=["choose"])
async def random_choice(ctx, *choices):
    choicelist = list(choices)
    samples = 1
    try:
        samples = int(choices[0])
        choicelist.pop(0)
    except: pass

    if samples > 1e4:
        await ctx.send("No.")
        return

    choices = random.choices(choicelist, weights=map(weight, choicelist), k=samples)

    if len(choices) == 1:
        await ctx.send(choices[0])
    else:
        counts = {}
        for choice in choices:
            counts[choice] = counts.get(choice, 0) + 1
        await ctx.send("\n".join(map(lambda x: f"{x[0]} x{x[1]}", counts.items())))

async def admin_check(ctx):
    if not await bot.is_owner(ctx.author):
        # apparently this has to be a pure function because ++help calls it for some reason because of course
        #await ctx.send(embed=error_embed(f"{ctx.author.name} is not in the sudoers file. This incident has been reported."))
        return False
    return True

@bot.check
async def andrew_bad(ctx):
    return ctx.message.author.id != 543131534685765673

@bot.group()
@commands.check(admin_check)
async def magic(ctx):
    if ctx.invoked_subcommand == None:
        return await ctx.send("Invalid magic command.")

@magic.command(rest_is_raw=True)
async def py(ctx, *, code):
    code = util.extract_codeblock(code)
    try:
        loc = {
            **locals(),
            "bot": bot,
            "ctx": ctx,
            "db": database
        }
        result = await asyncio.wait_for(util.async_exec(code, loc, globals()), timeout=5.0)
        if result != None:
            if isinstance(result, str):
                await ctx.send(result[:1999])
            else:
                await ctx.send(gen_codeblock(repr(result)))
    except TimeoutError:
        await ctx.send(embed=error_embed("Timed out."))
    except BaseException as e:
        await ctx.send(embed=error_embed(gen_codeblock(traceback.format_exc())))

@magic.command(rest_is_raw=True)
async def sql(ctx, *, code):
    code = util.extract_codeblock(code)
    try:
        csr = database.execute(code)
        out = ""
        async with csr as cursor:
            async for row in cursor:
                out += " ".join(map(repr, row)) + "\n"
        await ctx.send(gen_codeblock(out))
        await database.commit()
    except Exception as e:
        await ctx.send(embed=error_embed(gen_codeblock(traceback.format_exc())))

@bot.event
async def on_ready():
    logging.info("Connected as " + bot.user.name)
    await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.listening, name=f"commands beginning with {config['prefix']}"))
    remind_worker.start()

async def run_bot():
    global database
    database = await db.init(config["database"])
    await bot.start(config["token"])

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_bot())
    except KeyboardInterrupt:
        remind_worker.cancel()
        loop.run_until_complete(bot.logout())
    finally:
        loop.close()