import os
import sys
import json
import asyncio
import logging
import datetime
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

import discord
from discord.ext import commands
from aiohttp import web

# Configure UTF-8 & logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("HacTuocMusicBot")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 10000))

if not TOKEN:
    logger.error("THIẾU BIẾN MÔI TRƯỜNG DISCORD_TOKEN! Vui lòng cấu hình DISCORD_TOKEN trong Render Environment Variables.")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix=["m!", "M!", "s! ", "S! "], intents=intents, help_command=None)

# --- Music Queue & Player State ---
class Song:
    def __init__(self, data: Dict[str, Any], requester: discord.Member):
        self.id = data.get("id")
        self.title = data.get("title", "Không rõ tiêu đề")
        self.artists = data.get("artists", "Nhiều nghệ sĩ")
        self.thumbnail = data.get("thumbnail", "")
        self.stream_url = data.get("streamUrl", "")
        self.duration_sec = data.get("duration", 0)
        self.duration_str = data.get("durationStr", "0:00")
        self.requester = requester
        self.created_at = datetime.datetime.now()

class MusicPlayerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Tạm Dừng / Tiếp Tục", style=discord.ButtonStyle.primary, emoji="⏯️", custom_id="btn_pause_resume")
    async def btn_pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ Bot không có trong phòng Voice!", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Đã tạm dừng phát nhạc.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Đã tiếp tục phát nhạc.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Hiện không có bài hát nào đang phát.", ephemeral=True)

    @discord.ui.button(label="Bỏ Qua (Skip)", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="btn_skip")
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("❌ Không có bài hát nào để bỏ qua!", ephemeral=True)
        vc.stop()
        await interaction.response.send_message(f"⏭️ {interaction.user.mention} đã bỏ qua bài hát hiện tại!", ephemeral=False)

    @discord.ui.button(label="Hàng Đợi", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="btn_queue")
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild_id)
        if not player.current and not player.queue:
            return await interaction.response.send_message("📭 Hàng đợi hiện đang trống!", ephemeral=True)
        
        desc = ""
        if player.current:
            desc += f"🎶 **Đang phát:** **[{player.current.title}](https://zingmp3.vn)** - `{player.current.artists}` `[{player.current.duration_str}]` (bởi {player.current.requester.mention})\n\n"
        
        if player.queue:
            desc += "📋 **Danh sách chờ tiếp theo:**\n"
            for i, s in enumerate(player.queue[:10], 1):
                desc += f"`{i}.` **{s.title}** - `{s.artists}` `[{s.duration_str}]` (bởi {s.requester.display_name})\n"
            if len(player.queue) > 10:
                desc += f"\n*... và còn {len(player.queue) - 10} bài hát khác trong hàng đợi.*"
        
        embed = discord.Embed(
            title="📜 ₊˚ HÀNG ĐỢI PHÁT NHẠC ZING MP3 ✦",
            description=desc,
            color=0x8A2BE2
        )
        embed.set_footer(text="HacTuoc School Music • Zing MP3 24/7 🎶")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Dừng & Rời Phòng", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="btn_stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ Bot không có trong phòng Voice!", ephemeral=True)
        player = get_player(interaction.guild_id)
        player.queue.clear()
        player.current = None
        vc.stop()
        await vc.disconnect()
        await interaction.response.send_message(f"⏹️ {interaction.user.mention} đã dừng phát nhạc và mời bot rời phòng.", ephemeral=False)

class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.lock = asyncio.Lock()

players: Dict[int, GuildPlayer] = {}

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer(guild_id)
    return players[guild_id]

# --- Zing MP3 Helper via Node.js ---
async def fetch_zing_api(action: str, query: str) -> Dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "zing.js", action, query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"Zing Node Error: {stderr.decode('utf-8', errors='replace')}")
            return {"error": "Lỗi khi gọi API Zing MP3"}
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.error(f"Error calling zing.js: {e}")
        return {"error": str(e)}

def play_next(guild_id: int):
    asyncio.run_coroutine_threadsafe(start_next_song(guild_id), bot.loop)

async def start_next_song(guild_id: int):
    player = get_player(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        player.queue.clear()
        player.current = None
        return

    async with player.lock:
        if not player.queue:
            player.current = None
            if player.text_channel:
                embed = discord.Embed(
                    description="✨ Đã phát hết danh sách bài hát trong hàng đợi! Dùng lệnh `m!play <tên bài>` để tiếp tục nghe nhạc nhé 🎶",
                    color=0x8A2BE2
                )
                await player.text_channel.send(embed=embed)
            return

        song = player.queue.pop(0)
        player.current = song

        # FFmpeg options for streaming stability
        ffmpeg_opts = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }

        try:
            source = discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_opts)
            vc.play(source, after=lambda e: play_next(guild_id))

            if player.text_channel:
                embed = discord.Embed(
                    title="🎶 ₊˚ ĐANG PHÁT TỪ ZING MP3 ✦",
                    description=(
                        f"🎵 **Bài hát:** **{song.title}**\n"
                        f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                        f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                        f"👤 **Yêu cầu bởi:** {song.requester.mention}\n"
                    ),
                    color=0x8A2BE2
                )
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                embed.set_footer(text="HacTuoc School Music • Zing MP3 Lossless 128kbps 🎧")
                await player.text_channel.send(embed=embed, view=MusicPlayerView(guild_id))
        except Exception as e:
            logger.error(f"Error playing audio in guild {guild_id}: {e}")
            if player.text_channel:
                await player.text_channel.send(f"❌ Có lỗi khi phát bài `{song.title}`: {e}")
            play_next(guild_id)

# --- Commands ---
@bot.command(name="play", aliases=["p"])
async def cmd_play(ctx: commands.Context, *, query: Optional[str] = None):
    """Phát nhạc từ Zing MP3"""
    if not query:
        return await ctx.reply("⚠️ Vui lòng nhập tên bài hát hoặc link Zing MP3! (Ví dụ: `m!play Cắt Đôi Nỗi Sầu`)")

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.reply("❌ Bạn phải tham gia vào một phòng Voice trước khi gọi bot phát nhạc nhé!")

    voice_channel = ctx.author.voice.channel
    vc = ctx.guild.voice_client

    if vc is None:
        try:
            vc = await voice_channel.connect()
        except Exception as e:
            return await ctx.reply(f"❌ Không thể tham gia phòng Voice: {e}")
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    searching_msg = await ctx.send(f"🔍 Đang tìm kiếm **{query}** trên Zing MP3... Chờ xíu nha 🌸")

    # 1. Trích xuất ID nếu là link Zing MP3, ngược lại search
    song_id = None
    if "zingmp3.vn" in query:
        # Pattern link Zing: https://zingmp3.vn/bai-hat/.../ID.html
        import re
        match = re.search(r"/([A-Za-z0-9]+)\.html", query)
        if match:
            song_id = match.group(1)

    if not song_id:
        search_res = await fetch_zing_api("search", query)
        if "error" in search_res or not search_res.get("songs"):
            return await searching_msg.edit(content=f"❌ {search_res.get('error', 'Không tìm thấy bài hát nào trên Zing MP3!')}")
        top_song = search_res["songs"][0]
        song_id = top_song["id"]

    # 2. Lấy stream link
    stream_res = await fetch_zing_api("stream", song_id)
    if "error" in stream_res or not stream_res.get("streamUrl"):
        return await searching_msg.edit(content=f"❌ {stream_res.get('error', 'Không thể lấy luồng nhạc bài này (có thể là bài VIP).')}")

    song = Song(stream_res, ctx.author)
    player = get_player(ctx.guild.id)
    player.text_channel = ctx.channel

    if vc.is_playing() or vc.is_paused():
        player.queue.append(song)
        embed = discord.Embed(
            title="📥 ₊˚ ĐÃ THÊM VÀO HÀNG ĐỢI ✦",
            description=(
                f"🎵 **Bài hát:** **{song.title}**\n"
                f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                f"🔢 **Vị trí hàng đợi:** `#{len(player.queue)}`\n"
                f"👤 **Người yêu cầu:** {ctx.author.mention}"
            ),
            color=0x8A2BE2
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text="Gõ m!queue để xem toàn bộ danh sách chờ")
        await searching_msg.delete()
        await ctx.send(embed=embed)
    else:
        player.queue.append(song)
        await searching_msg.delete()
        await start_next_song(ctx.guild.id)

@bot.command(name="skip", aliases=["s", "next"])
async def cmd_skip(ctx: commands.Context):
    """Bỏ qua bài hát hiện tại"""
    vc = ctx.guild.voice_client
    if not vc or not vc.is_playing():
        return await ctx.reply("❌ Không có bài hát nào đang phát để bỏ qua!")
    vc.stop()
    await ctx.send(f"⏭️ {ctx.author.mention} đã bỏ qua bài hát hiện tại!")

@bot.command(name="pause")
async def cmd_pause(ctx: commands.Context):
    """Tạm dừng bài hát"""
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Đã tạm dừng phát nhạc. Gõ `m!resume` để tiếp tục!")
    else:
        await ctx.reply("❌ Không có bài hát nào đang phát!")

@bot.command(name="resume")
async def cmd_resume(ctx: commands.Context):
    """Tiếp tục phát nhạc"""
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Đã tiếp tục phát nhạc!")
    else:
        await ctx.reply("❌ Nhạc không ở trạng thái tạm dừng!")

@bot.command(name="stop", aliases=["leave"])
async def cmd_stop(ctx: commands.Context):
    """Dừng phát nhạc và mời bot rời phòng Voice"""
    vc = ctx.guild.voice_client
    if not vc:
        return await ctx.reply("❌ Bot không có trong phòng Voice!")
    player = get_player(ctx.guild.id)
    player.queue.clear()
    player.current = None
    vc.stop()
    await vc.disconnect()
    await ctx.send("⏹️ Đã dừng phát nhạc, xóa sạch hàng đợi và rời phòng Voice.")

@bot.command(name="queue", aliases=["q"])
async def cmd_queue(ctx: commands.Context):
    """Xem danh sách bài hát đang chờ"""
    player = get_player(ctx.guild.id)
    if not player.current and not player.queue:
        return await ctx.reply("📭 Hàng đợi hiện đang trống! Dùng `m!play <tên bài>` để gọi nhạc nhé.")

    desc = ""
    if player.current:
        desc += f"🎶 **Đang phát:** **[{player.current.title}](https://zingmp3.vn)** - `{player.current.artists}` `[{player.current.duration_str}]` (bởi {player.current.requester.mention})\n\n"

    if player.queue:
        desc += "📋 **Danh sách chờ tiếp theo:**\n"
        for i, s in enumerate(player.queue[:10], 1):
            desc += f"`{i}.` **{s.title}** - `{s.artists}` `[{s.duration_str}]` (bởi {s.requester.display_name})\n"
        if len(player.queue) > 10:
            desc += f"\n*... và còn {len(player.queue) - 10} bài hát khác trong hàng đợi.*"

    embed = discord.Embed(
        title="📜 ₊˚ HÀNG ĐỢI PHÁT NHẠC ZING MP3 ✦",
        description=desc,
        color=0x8A2BE2
    )
    embed.set_footer(text=f"Tổng số bài trong hàng đợi: {len(player.queue)} bài")
    await ctx.send(embed=embed)

@bot.command(name="nowplaying", aliases=["np"])
async def cmd_nowplaying(ctx: commands.Context):
    """Xem bài hát đang phát"""
    player = get_player(ctx.guild.id)
    if not player.current:
        return await ctx.reply("❌ Hiện không có bài hát nào đang phát!")
    song = player.current
    embed = discord.Embed(
        title="🎶 ₊˚ ĐANG PHÁT HIỆN TẠI ✦",
        description=(
            f"🎵 **Bài hát:** **{song.title}**\n"
            f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
            f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
            f"👤 **Yêu cầu bởi:** {song.requester.mention}\n"
        ),
        color=0x8A2BE2
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text="HacTuoc School Music • Zing MP3 Lossless 128kbps 🎧")
    await ctx.send(embed=embed, view=MusicPlayerView(ctx.guild.id))

@bot.command(name="help", aliases=["h"])
async def cmd_help(ctx: commands.Context):
    """Hướng dẫn sử dụng bot nhạc Zing MP3"""
    embed = discord.Embed(
        title="ᕱ⑅ᕱ ₊˚ HƯỚNG DẪN LỆNH BOT NHẠC ZING MP3 • HACTUOC SCHOOL ✦",
        description=(
            "Bot hỗ trợ phát trực tiếp nhạc từ Zing MP3 vào phòng Voice với chất lượng 128kbps mượt mà không giật lag!\n\n"
            "🎵 `m!play <tên bài / link>` (hoặc `m!p`): Tìm kiếm & phát nhạc\n"
            "⏭️ `m!skip` (hoặc `m!s`): Bỏ qua bài hát hiện tại\n"
            "⏸️ `m!pause`: Tạm dừng phát nhạc\n"
            "▶️ `m!resume`: Tiếp tục phát bài hát\n"
            "⏹️ `m!stop` (hoặc `m!leave`): Dừng phát, xóa hàng đợi & rời phòng\n"
            "📜 `m!queue` (hoặc `m!q`): Xem danh sách bài hát đang chờ\n"
            "🎧 `m!np`: Xem thông tin bài hát đang phát\n\n"
            "*(Bạn cũng có thể điều khiển trực tiếp bằng các nút bấm trên giao diện phát nhạc nhé 🌸)*"
        ),
        color=0x8A2BE2
    )
    embed.set_footer(text="Prefix bot nhạc: m! • HacTuoc Music Engine 24/7")
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    logger.info("=" * 60)
    logger.info(f"Bot nhạc Zing MP3 đã kết nối: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")
    logger.info(f"Prefix: m! | Máy chủ kết nối: {len(bot.guilds)}")
    logger.info("=" * 60)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="m!play | Zing MP3 🎶")
    )

# --- Render Mini Web Server for Health Check ---
async def start_web_server():
    app = web.Application()
    async def handle_index(request):
        return web.json_response({
            "status": "online",
            "bot": "HacTuoc Music Bot",
            "source": "Zing MP3 Engine",
            "guilds": len(bot.guilds),
            "ping_ms": round(bot.latency * 1000, 2) if bot.is_ready() else 0,
            "timestamp": datetime.datetime.now().isoformat()
        })
    async def handle_health(request):
        return web.Response(text="OK", status=200)

    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Render Web Server started on http://0.0.0.0:{PORT} (Health Check Active)")

async def main_runner():
    await start_web_server()
    if TOKEN:
        await bot.start(TOKEN)
    else:
        logger.error("Không có TOKEN để khởi động Discord Bot.")

if __name__ == "__main__":
    asyncio.run(main_runner())
