import os
import sys
import json
import time
import asyncio
import logging
import datetime
import re
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

import discord
from discord.ext import commands
from aiohttp import web

# Configure UTF-8 & logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("HacTuocMusicCluster")

load_dotenv()

PORT = int(os.getenv("PORT", 10000))

# Tokens for Music Bots 1, 2, 3
TOKEN_1 = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_TOKEN_1")
TOKEN_2 = os.getenv("DISCORD_TOKEN_2")
TOKEN_3 = os.getenv("DISCORD_TOKEN_3")

# ==============================================================================
# 🚀 ZERO-LAG HIGH-SPEED ZING MP3 ENGINE WITH IN-MEMORY MULTI-LEVEL CACHE
# ==============================================================================

class Song:
    """Song data model containing pre-resolved metadata and streaming URL"""
    def __init__(self, data: Dict[str, Any], requester: discord.Member):
        self.id = data.get("id", "")
        self.title = data.get("title", "Không rõ tiêu đề")
        self.artists = data.get("artists", "Nhiều nghệ sĩ")
        self.thumbnail = data.get("thumbnail", "")
        self.stream_url = data.get("streamUrl", "")
        self.duration_sec = data.get("duration", 0)
        self.duration_str = data.get("durationStr", "0:00")
        self.requester = requester
        self.created_at = datetime.datetime.now()

class ZingEngine:
    """In-memory cached Zing MP3 engine with pre-buffering & zero latency"""
    def __init__(self):
        # Cache dictionaries with TTL
        self.search_cache: Dict[str, Dict[str, Any]] = {}  # query -> {data, expiry}
        self.stream_cache: Dict[str, Dict[str, Any]] = {}  # song_id -> {data, expiry}
        self.SEARCH_TTL = 3600       # 1 hour
        self.STREAM_TTL = 7200       # 2 hours

    def _clean_cache(self):
        now = time.time()
        self.search_cache = {k: v for k, v in self.search_cache.items() if v["expiry"] > now}
        self.stream_cache = {k: v for k, v in self.stream_cache.items() if v["expiry"] > now}

    async def _call_node(self, action: str, query: str) -> Dict[str, Any]:
        """Execute node zing.js asynchronously"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", "zing.js", action, query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.error(f"Zing Node Error ({action}): {err_msg}")
                return {"error": f"Lỗi Zing MP3: {err_msg[:100]}"}
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.error(f"Error calling zing.js ({action}): {e}")
            return {"error": str(e)}

    async def search(self, query: str) -> Dict[str, Any]:
        """Search Zing MP3 with in-memory caching"""
        self._clean_cache()
        key = query.strip().lower()
        now = time.time()
        if key in self.search_cache and self.search_cache[key]["expiry"] > now:
            logger.info(f"⚡ [Cache Hit] Search Zing MP3: '{query}'")
            return self.search_cache[key]["data"]

        res = await self._call_node("search", query)
        if res.get("success") and res.get("songs"):
            self.search_cache[key] = {
                "data": res,
                "expiry": now + self.SEARCH_TTL
            }
        return res

    async def get_stream(self, song_id: str) -> Dict[str, Any]:
        """Get 128kbps stream URL with in-memory caching"""
        self._clean_cache()
        key = song_id.strip()
        now = time.time()
        if key in self.stream_cache and self.stream_cache[key]["expiry"] > now:
            logger.info(f"⚡ [Cache Hit] Stream Zing MP3: '{song_id}'")
            return self.stream_cache[key]["data"]

        res = await self._call_node("stream", song_id)
        if res.get("success") and res.get("streamUrl"):
            self.stream_cache[key] = {
                "data": res,
                "expiry": now + self.STREAM_TTL
            }
        return res

    async def resolve_song(self, query_or_url: str, requester: discord.Member) -> Optional[Song]:
        """Resolve query or Zing URL directly into a playable Song object with 0 delay"""
        query_or_url = query_or_url.strip()
        song_id = None

        if "zingmp3.vn" in query_or_url:
            match = re.search(r"/([A-Za-z0-9]+)\.html", query_or_url)
            if match:
                song_id = match.group(1)

        if not song_id:
            search_res = await self.search(query_or_url)
            if "error" in search_res or not search_res.get("songs"):
                return None
            song_id = search_res["songs"][0]["id"]

        stream_res = await self.get_stream(song_id)
        if "error" in stream_res or not stream_res.get("streamUrl"):
            return None

        return Song(stream_res, requester)

# Global shared Zing engine instance
zing_engine = ZingEngine()

# ==============================================================================
# 🎵 GUILD PLAYER (PER-VOICE-ROOM MANAGER)
# ==============================================================================

class GuildPlayer:
    def __init__(self, guild_id: int, bot_instance: "MusicBotInstance"):
        self.guild_id = guild_id
        self.bot_instance = bot_instance
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.last_np_msg_id: Optional[int] = None
        self.lock = asyncio.Lock()

# ==============================================================================
# 🤖 MUSIC BOT INSTANCE DEFINITION
# ==============================================================================

class MusicBotInstance:
    def __init__(
        self,
        bot_id: int,
        name: str,
        token: Optional[str],
        prefixes: List[str],
        embed_color: int,
        room_label: str
    ):
        self.bot_id = bot_id
        self.name = name
        self.token = token
        self.prefixes = prefixes
        self.embed_color = embed_color
        self.room_label = room_label
        self.players: Dict[int, GuildPlayer] = {}

        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        self.bot = commands.Bot(
            command_prefix=self.prefixes,
            intents=intents,
            help_command=None
        )

        self._register_events_and_commands()

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(guild_id, self)
        return self.players[guild_id]

    def _register_events_and_commands(self):
        bot = self.bot
        instance = self

        @bot.event
        async def on_ready():
            logger.info("=" * 60)
            logger.info(f"✨ [{instance.name}] ĐÃ SẴN SÀNG! Tag: {bot.user} (ID: {bot.user.id})")
            logger.info(f"Prefix: {instance.prefixes[0]} | Phòng phục vụ: {instance.room_label}")
            logger.info(f"Đang kết nối: {len(bot.guilds)} máy chủ")
            logger.info("=" * 60)
            status_text = f"{instance.prefixes[0]}play | {instance.room_label} 🎶"
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=status_text
                ),
                status=discord.Status.online
            )

        @bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            """Auto disconnect after 120s if voice room becomes empty"""
            if member.id != bot.user.id and before.channel:
                guild = member.guild
                vc = guild.voice_client
                if vc and vc.channel == before.channel:
                    real_members = [m for m in before.channel.members if not m.bot]
                    if len(real_members) == 0:
                        await asyncio.sleep(120)
                        if vc and vc.is_connected():
                            check_again = [m for m in before.channel.members if not m.bot]
                            if len(check_again) == 0:
                                player = instance.get_player(guild.id)
                                player.queue.clear()
                                player.current = None
                                await vc.disconnect()
                                if player.text_channel:
                                    embed = discord.Embed(
                                        description=f"💤 {instance.name} tự động rời phòng `{before.channel.name}` vì không còn ai nghe nhạc.",
                                        color=instance.embed_color
                                    )
                                    await player.text_channel.send(embed=embed)

        @bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            """Reaction controls: ⏯️, ⏭️, 📜, ⏹️ (Bypasses Cloudflare Component button router)"""
            if payload.user_id == bot.user.id:
                return
            guild = bot.get_guild(payload.guild_id)
            if not guild:
                return
            player = instance.get_player(payload.guild_id)
            if not player or payload.message_id != player.last_np_msg_id:
                return

            member = guild.get_member(payload.user_id)
            if not member or member.bot:
                return

            channel = guild.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.remove_reaction(payload.emoji, member)
                except Exception:
                    pass

            emoji_name = str(payload.emoji)
            vc = guild.voice_client

            if emoji_name == "⏯️":
                if not vc:
                    if channel:
                        await channel.send("❌ Bot không có trong phòng Voice!", delete_after=5)
                    return
                if vc.is_playing():
                    vc.pause()
                    if channel:
                        await channel.send("⏸️ Đã tạm dừng phát nhạc.", delete_after=5)
                elif vc.is_paused():
                    vc.resume()
                    if channel:
                        await channel.send("▶️ Đã tiếp tục phát nhạc.", delete_after=5)
            elif emoji_name == "⏭️":
                if not vc or not vc.is_playing():
                    if channel:
                        await channel.send("❌ Không có bài hát nào đang phát để bỏ qua!", delete_after=5)
                    return
                vc.stop()
                if channel:
                    await channel.send(f"⏭️ {member.mention} đã bỏ qua bài hát hiện tại!")
            elif emoji_name == "📜":
                if not player.current and not player.queue:
                    if channel:
                        await channel.send("📭 Hàng đợi hiện đang trống!", delete_after=5)
                    return
                desc = ""
                if player.current:
                    desc += f"🎶 **Đang phát:** **[{player.current.title}](https://zingmp3.vn)** - `{player.current.artists}` `[{player.current.duration_str}]`\n\n"
                if player.queue:
                    desc += "📋 **Danh sách chờ tiếp theo:**\n"
                    for i, s in enumerate(player.queue[:10], 1):
                        desc += f"`{i}.` **{s.title}** - `{s.artists}` `[{s.duration_str}]` (bởi {s.requester.display_name})\n"
                    if len(player.queue) > 10:
                        desc += f"\n*... và còn {len(player.queue) - 10} bài khác.*"
                embed = discord.Embed(
                    title=f"📜 ₊˚ HÀNG ĐỢI • {instance.name.upper()} ✦",
                    description=desc,
                    color=instance.embed_color
                )
                if channel:
                    await channel.send(embed=embed, delete_after=20)
            elif emoji_name == "⏹️":
                if not vc:
                    return
                player.queue.clear()
                player.current = None
                vc.stop()
                await vc.disconnect()
                if channel:
                    await channel.send(f"⏹️ {member.mention} đã bấm dừng phát nhạc và mời {instance.name} rời phòng.")

        # --- BOT COMMANDS ---

        @bot.command(name="play", aliases=["p"])
        async def cmd_play(ctx: commands.Context, *, query: Optional[str] = None):
            """Phát nhạc từ Zing MP3"""
            prefix = instance.prefixes[0]
            if not query:
                return await ctx.reply(f"⚠️ Vui lòng nhập tên bài hát hoặc link Zing MP3! (Ví dụ: `{prefix}play Cắt Đôi Nỗi Sầu`)")

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

            searching_msg = await ctx.send(f"🔍 Đang tìm & nạp luồng siêu tốc **{query}**... Chờ xíu nha 🌸")

            song = await zing_engine.resolve_song(query, ctx.author)
            if not song:
                return await searching_msg.edit(content=f"❌ Không thể tìm thấy hoặc lấy luồng nhạc bài **{query}** (có thể là bài VIP).")

            player = instance.get_player(ctx.guild.id)
            player.text_channel = ctx.channel

            if vc.is_playing() or vc.is_paused():
                player.queue.append(song)
                embed = discord.Embed(
                    title=f"📥 ₊˚ ĐÃ THÊM VÀO HÀNG ĐỢI • {instance.name} ✦",
                    description=(
                        f"🎵 **Bài hát:** **{song.title}**\n"
                        f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                        f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                        f"🔢 **Vị trí hàng đợi:** `#{len(player.queue)}`\n"
                        f"👤 **Người yêu cầu:** {ctx.author.mention}"
                    ),
                    color=instance.embed_color
                )
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                embed.set_footer(text=f"Gõ {prefix}queue để xem toàn bộ danh sách chờ")
                await searching_msg.delete()
                await ctx.send(embed=embed)
            else:
                player.queue.append(song)
                await searching_msg.delete()
                await instance.start_next_song(ctx.guild.id)

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
                await ctx.send(f"⏸️ Đã tạm dừng phát nhạc. Gõ `{instance.prefixes[0]}resume` để tiếp tục!")
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
            """Dừng phát nhạc và rời phòng Voice"""
            vc = ctx.guild.voice_client
            if not vc:
                return await ctx.reply("❌ Bot không có trong phòng Voice!")
            player = instance.get_player(ctx.guild.id)
            player.queue.clear()
            player.current = None
            vc.stop()
            await vc.disconnect()
            await ctx.send(f"⏹️ Đã dừng phát nhạc, xóa sạch hàng đợi và {instance.name} đã rời phòng.")

        @bot.command(name="queue", aliases=["q"])
        async def cmd_queue(ctx: commands.Context):
            """Xem danh sách bài hát đang chờ"""
            player = instance.get_player(ctx.guild.id)
            if not player.current and not player.queue:
                return await ctx.reply(f"📭 Hàng đợi hiện đang trống! Dùng `{instance.prefixes[0]}play <tên bài>` để gọi nhạc nhé.")

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
                title=f"📜 ₊˚ HÀNG ĐỢI • {instance.name.upper()} ✦",
                description=desc,
                color=instance.embed_color
            )
            embed.set_footer(text=f"Tổng số bài trong hàng đợi: {len(player.queue)} bài • Prefix: {instance.prefixes[0]}")
            await ctx.send(embed=embed)

        @bot.command(name="nowplaying", aliases=["np"])
        async def cmd_nowplaying(ctx: commands.Context):
            """Xem bài hát đang phát"""
            player = instance.get_player(ctx.guild.id)
            if not player.current:
                return await ctx.reply("❌ Hiện không có bài hát nào đang phát!")
            song = player.current
            prefix = instance.prefixes[0]
            embed = discord.Embed(
                title=f"🎶 ₊˚ ĐANG PHÁT HIỆN TẠI • {instance.name} ✦",
                description=(
                    f"🎵 **Bài hát:** **{song.title}**\n"
                    f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                    f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                    f"👤 **Yêu cầu bởi:** {song.requester.mention}\n\n"
                    "💡 **Phím điều khiển nhanh (Bấm biểu cảm bên dưới):**\n"
                    "⏯️ `Tạm dừng / Tiếp tục` • ⏭️ `Bỏ qua bài`\n"
                    "📜 `Xem hàng đợi` • ⏹️ `Dừng & Rời phòng`"
                ),
                color=instance.embed_color
            )
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.set_footer(text=f"Bấm biểu cảm bên dưới hoặc gõ {prefix}s, {prefix}q, {prefix}stop 🎧")
            msg = await ctx.send(embed=embed)
            player.last_np_msg_id = msg.id
            for emo in ["⏯️", "⏭️", "📜", "⏹️"]:
                try:
                    await msg.add_reaction(emo)
                except Exception:
                    pass

        @bot.command(name="help", aliases=["h"])
        async def cmd_help(ctx: commands.Context):
            """Hướng dẫn sử dụng bot nhạc Zing MP3"""
            p = instance.prefixes[0]
            embed = discord.Embed(
                title=f"ᕱ⑅ᕱ ₊˚ HƯỚNG DẪN • {instance.name.upper()} ✦",
                description=(
                    f"**{instance.name}** chuyên phụ trách phòng âm nhạc: `{instance.room_label}`\n\n"
                    f"🎵 `{p}play <tên bài / link>` (hoặc `{p}p`): Phát nhạc Zing MP3 siêu tốc\n"
                    f"⏭️ `{p}skip` (hoặc `{p}s`): Bỏ qua bài hát hiện tại\n"
                    f"⏸️ `{p}pause`: Tạm dừng phát nhạc\n"
                    f"▶️ `{p}resume`: Tiếp tục phát bài hát\n"
                    f"⏹️ `{p}stop` (hoặc `{p}leave`): Dừng phát, xóa hàng đợi & rời phòng\n"
                    f"📜 `{p}queue` (hoặc `{p}q`): Xem danh sách bài hát đang chờ\n"
                    f"🎧 `{p}np`: Xem thông tin bài hát đang phát & mở phím điều khiển\n\n"
                    "*(Hạ tầng Zero-Lag tự động đệm trước luồng nhạc và khôi phục sự cố mạng tức thì 🚀)*"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text=f"Prefix: {p} • HacTuoc Music Cluster 24/7")
            await ctx.send(embed=embed)

    def play_next(self, guild_id: int):
        asyncio.run_coroutine_threadsafe(self.start_next_song(guild_id), self.bot.loop)

    async def start_next_song(self, guild_id: int):
        """Zero-Lag Playback: Pop pre-resolved song and initiate instant FFmpeg stream"""
        player = self.get_player(guild_id)
        guild = self.bot.get_guild(guild_id)
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
                        description=f"✨ Đã phát hết danh sách bài hát trong hàng đợi! Dùng `{self.prefixes[0]}play <tên bài>` để tiếp tục nghe nhạc nhé 🎶",
                        color=self.embed_color
                    )
                    await player.text_channel.send(embed=embed)
                return

            # Song is already pre-resolved into direct CDN URL
            song = player.queue.pop(0)
            player.current = song

            # 🚀 FFmpeg Zero-Lag & Anti-Jitter configuration
            ffmpeg_opts = {
                'before_options': (
                    '-reconnect 1 '
                    '-reconnect_streamed 1 '
                    '-reconnect_delay_max 3 '
                    '-reconnect_on_network_error 1 '
                    '-probesize 64k '
                    '-analyzeduration 0'
                ),
                'options': '-vn -filter:a "volume=0.85" -buffer_size 1024k'
            }

            try:
                source = discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_opts)
                vc.play(source, after=lambda e: self.play_next(guild_id))

                if player.text_channel:
                    embed = discord.Embed(
                        title=f"🎶 ₊˚ ĐANG PHÁT TỪ ZING MP3 • {self.name} ✦",
                        description=(
                            f"🎵 **Bài hát:** **{song.title}**\n"
                            f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                            f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                            f"👤 **Yêu cầu bởi:** {song.requester.mention}\n\n"
                            "💡 **Phím điều khiển nhanh (Bấm biểu cảm bên dưới):**\n"
                            "⏯️ `Tạm dừng / Tiếp tục` • ⏭️ `Bỏ qua bài`\n"
                            "📜 `Xem hàng đợi` • ⏹️ `Dừng & Rời phòng`"
                        ),
                        color=self.embed_color
                    )
                    if song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)
                    embed.set_footer(text=f"Bấm biểu cảm bên dưới hoặc gõ {self.prefixes[0]}s, {self.prefixes[0]}q, {self.prefixes[0]}stop 🎧")
                    msg = await player.text_channel.send(embed=embed)
                    player.last_np_msg_id = msg.id
                    for emo in ["⏯️", "⏭️", "📜", "⏹️"]:
                        try:
                            await msg.add_reaction(emo)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Lỗi khi phát bài ở guild {guild_id}: {e}")
                if player.text_channel:
                    await player.text_channel.send(f"❌ Có lỗi khi phát bài `{song.title}`: {e}")
                self.play_next(guild_id)

    async def run(self):
        """Run bot with automatic reconnect and 429 rate limit backoff"""
        if not self.token:
            logger.warning(f"⚠️ [{self.name}] Bỏ qua vì chưa có Token (DISCORD_TOKEN_{self.bot_id}).")
            return

        while True:
            try:
                logger.info(f"🔄 [{self.name}] Đang kết nối tới Discord Gateway...")
                await self.bot.start(self.token)
            except discord.HTTPException as e:
                if e.status == 429:
                    logger.warning(f"⚠️ [{self.name}] Discord API 429 Rate Limit (chờ 30s trước khi thử lại)...")
                    await asyncio.sleep(30)
                else:
                    logger.error(f"❌ [{self.name}] Lỗi Discord HTTP: {e}. Thử lại sau 15s...")
                    await asyncio.sleep(15)
            except Exception as e:
                logger.error(f"❌ [{self.name}] Lỗi kết nối bot: {e}. Thử lại sau 15s...")
                await asyncio.sleep(15)

# ==============================================================================
# 🌐 CLUSTER MANAGER & RENDER WEB SERVER
# ==============================================================================

# Initialize 3 Bot Instances
bot1 = MusicBotInstance(
    bot_id=1,
    name="Nhân Sâm Music 1",
    token=TOKEN_1,
    prefixes=["m!", "M!", "!"],
    embed_color=0x8A2BE2,  # Cute Purple
    room_label="Phòng Âm Nhạc 1"
)

bot2 = MusicBotInstance(
    bot_id=2,
    name="Nhân Sâm Music 2",
    token=TOKEN_2,
    prefixes=["m2!", "M2!"],
    embed_color=0x00E5FF,  # Neon Cyan
    room_label="Phòng Âm Nhạc 2"
)

bot3 = MusicBotInstance(
    bot_id=3,
    name="Nhân Sâm Music 3",
    token=TOKEN_3,
    prefixes=["m3!", "M3!"],
    embed_color=0xFF6B81,  # Rose Pink
    room_label="Phòng Âm Nhạc 3"
)

CLUSTER_BOTS = [bot1, bot2, bot3]

async def start_web_server():
    """Render Web Server for 24/7 Keep-Alive & Cluster Health Monitoring"""
    app = web.Application()

    async def handle_index(request):
        bot_statuses = []
        for b in CLUSTER_BOTS:
            bot_statuses.append({
                "id": b.bot_id,
                "name": b.name,
                "prefix": b.prefixes[0],
                "configured": bool(b.token),
                "online": b.bot.is_ready(),
                "guilds": len(b.bot.guilds) if b.bot.is_ready() else 0,
                "ping_ms": round(b.bot.latency * 1000, 2) if b.bot.is_ready() else 0,
                "active_voice_connections": sum(1 for p in b.players.values() if p.current is not None)
            })

        return web.json_response({
            "status": "online",
            "cluster": "HacTuoc Music Bots (Vô Địch Thiên Hạ)",
            "engine": "Zing MP3 Zero-Lag Pre-Buffer Engine",
            "active_bots_count": sum(1 for b in CLUSTER_BOTS if b.bot.is_ready()),
            "cached_searches": len(zing_engine.search_cache),
            "cached_streams": len(zing_engine.stream_cache),
            "bots": bot_statuses,
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
    logger.info(f"🚀 Render Web Server started on http://0.0.0.0:{PORT} (Health Check Active)")

async def main():
    # 1. Start Web Server
    await start_web_server()

    # 2. Launch each configured bot with staggered boot to avoid Discord Gateway 429
    tasks = []
    stagger_delay = 0
    for b in CLUSTER_BOTS:
        if b.token:
            async def run_bot_with_stagger(bot_inst=b, delay=stagger_delay):
                if delay > 0:
                    logger.info(f"⏳ Đang xếp hàng khởi động [{bot_inst.name}] sau {delay}s...")
                    await asyncio.sleep(delay)
                await bot_inst.run()

            tasks.append(asyncio.create_task(run_bot_with_stagger()))
            stagger_delay += 6  # 6-second stagger
        else:
            logger.info(f"ℹ️ [{b.name}] chưa cài đặt TOKEN. Đang ở chế độ chờ kích hoạt.")

    if not tasks:
        logger.error("❌ Không có bot nào có Token! Vui lòng thiết lập DISCORD_TOKEN trong biến môi trường.")
        while True:
            await asyncio.sleep(3600)
    else:
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
