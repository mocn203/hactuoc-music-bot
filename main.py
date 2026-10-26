import os
import sys
import json
import time
import asyncio
import logging
import datetime
import re
import gc
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

import discord
from discord import app_commands, ui
from discord.ext import commands
from aiohttp import web
from multi_source import MultiSourceEngine, Song

def format_duration(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_progress_bar(current_sec: int, total_sec: int, bar_len: int = 12) -> str:
    if total_sec <= 0:
        return f"`{format_duration(current_sec)} / 0:00`"
    ratio = min(1.0, max(0.0, current_sec / total_sec))
    filled = int(round(ratio * bar_len))
    bar = "━" * filled + "🔘" + "━" * (bar_len - filled)
    return f"`{format_duration(current_sec)}` {bar} `{format_duration(total_sec)}`"

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

def get_memory_mb() -> float:
    """Measure resident memory (RSS) in MB across Linux/Docker/Windows"""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        try:
            if os.path.exists('/proc/self/status'):
                with open('/proc/self/status') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            return round(int(line.split()[1]) / 1024, 2)
        except Exception:
            pass
        return 0.0

# ==============================================================================
# 🚀 ZERO-LAG HIGH-SPEED ZING MP3 & MULTI-PLATFORM ENGINE (YOUTUBE/SC/SPOTIFY/DIRECT)
# ==============================================================================

class ZingEngine:
    """In-memory cached Zing MP3 engine with pre-buffering & zero latency"""
    def __init__(self):
        # Cache dictionaries with TTL
        self.search_cache: Dict[str, Dict[str, Any]] = {}  # query -> {data, expiry}
        self.stream_cache: Dict[str, Dict[str, Any]] = {}  # song_id -> {data, expiry}
        self.playlist_cache: Dict[str, Dict[str, Any]] = {} # playlist_id -> {data, expiry}
        self.SEARCH_TTL = 3600       # 1 hour
        self.STREAM_TTL = 7200       # 2 hours
        self.PLAYLIST_TTL = 3600     # 1 hour
        self.MAX_CACHE_ENTRIES = 250 # Zero-leak memory bound

    def _clean_cache(self):
        now = time.time()
        self.search_cache = {k: v for k, v in self.search_cache.items() if v["expiry"] > now}
        self.stream_cache = {k: v for k, v in self.stream_cache.items() if v["expiry"] > now}
        self.playlist_cache = {k: v for k, v in self.playlist_cache.items() if v["expiry"] > now}

        # Zero-leak bound: limit max cache size to prevent unbounded memory growth
        if len(self.search_cache) > self.MAX_CACHE_ENTRIES:
            sorted_search = sorted(self.search_cache.items(), key=lambda item: item[1]["expiry"], reverse=True)
            self.search_cache = dict(sorted_search[:self.MAX_CACHE_ENTRIES])

        if len(self.stream_cache) > self.MAX_CACHE_ENTRIES:
            sorted_stream = sorted(self.stream_cache.items(), key=lambda item: item[1]["expiry"], reverse=True)
            self.stream_cache = dict(sorted_stream[:self.MAX_CACHE_ENTRIES])

        if len(self.playlist_cache) > self.MAX_CACHE_ENTRIES:
            sorted_playlist = sorted(self.playlist_cache.items(), key=lambda item: item[1]["expiry"], reverse=True)
            self.playlist_cache = dict(sorted_playlist[:self.MAX_CACHE_ENTRIES])

    async def _call_node(self, action: str, query: str) -> Dict[str, Any]:
        """Execute node zing.js asynchronously with 12s timeout to prevent process leaks"""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", "zing.js", action, query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=12.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                await proc.wait()
                logger.error(f"Zing Node Timeout ({action}): '{query}'")
                return {"error": "Zing MP3 phản hồi quá lâu (Timeout)"}

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.error(f"Zing Node Error ({action}): {err_msg}")
                return {"error": f"Lỗi Zing MP3: {err_msg[:100]}"}
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.error(f"Error calling zing.js ({action}): {e}")
            return {"error": str(e)}
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

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

    async def get_playlist(self, playlist_id: str) -> Dict[str, Any]:
        """Fetch playlist/album details and songs with in-memory caching"""
        self._clean_cache()
        key = playlist_id.strip()
        now = time.time()
        if key in self.playlist_cache and self.playlist_cache[key]["expiry"] > now:
            logger.info(f"⚡ [Cache Hit] Playlist Zing MP3: '{playlist_id}'")
            return self.playlist_cache[key]["data"]

        res = await self._call_node("playlist", playlist_id)
        if res.get("success") and res.get("songs"):
            self.playlist_cache[key] = {
                "data": res,
                "expiry": now + self.PLAYLIST_TTL
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

# Global shared Zing engine & Multi-Source Unified Engine
zing_engine = ZingEngine()
multi_engine = MultiSourceEngine(zing_engine=zing_engine)

import socket

def make_tcp_nodelay_socket(addr_info):
    """Creates low-latency TCP sockets with TCP_NODELAY enabled"""
    sock = socket.socket(addr_info[0], addr_info[1], addr_info[2])
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    return sock

def optimize_voice_socket(vc: Optional[discord.VoiceClient]):
    """Sets DSCP Expedited Forwarding (IP_TOS 0xb8 / DSCP 46) on UDP voice socket for jitter-free real-time audio"""
    if not vc:
        return
    try:
        sock = getattr(vc, "socket", None)
        if not sock or sock is discord.utils.MISSING:
            conn = getattr(vc, "_connection", None)
            if conn:
                sock = getattr(conn, "socket", None)

        if sock and sock is not discord.utils.MISSING and isinstance(sock, socket.socket):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xb8)
            logger.info("⚡ [Voice Opt] Applied DSCP 46 (IP_TOS 0xb8) to UDP voice socket")
    except Exception as e:
        logger.debug(f"Could not apply DSCP socket option: {e}")

# ==============================================================================
# 🎛️ BASS BOOST & EQUALIZER PROFILES (DYNAMIC NORMALIZER ANTI-CLIPPING)
# ==============================================================================

BOOST_PROFILES: Dict[str, Dict[str, str]] = {
    "off": {
        "name": "Tắt (Chuẩn Studio)",
        "filter": "volume=0.85",
        "emoji": "🔇",
        "desc": "Âm thanh nguyên bản trung thực, cân bằng tự nhiên"
    },
    "low": {
        "name": "Nhẹ (Acoustic / Vocal)",
        "filter": "volume=0.90,bass=g=5,dynaudnorm=f=150:g=15",
        "emoji": "🎵",
        "desc": "Tăng nhẹ dải trầm, ấm tiếng, giữ vocal trong trẻo"
    },
    "medium": {
        "name": "Vừa (Club / EDM / Pop)",
        "filter": "volume=0.95,bass=g=10,dynaudnorm=f=150:g=15",
        "emoji": "🔊",
        "desc": "Bass căng uy lực, chống vỡ tiếng (Khuyên dùng)"
    },
    "high": {
        "name": "Mạnh (Vinahouse / Hardstyle)",
        "filter": "volume=1.00,bass=g=15,dynaudnorm=f=150:g=15",
        "emoji": "🚀",
        "desc": "Âm trầm siêu lực, rung chuyển phòng Voice"
    },
    "extreme": {
        "name": "Cực Đại (Ultra Bass Max)",
        "filter": "volume=1.10,bass=g=20,dynaudnorm=f=150:g=15",
        "emoji": "💥",
        "desc": "Bass tối đa, công suất đỉnh cao vô địch"
    }
}

def make_audio_source(stream_url: str, boost_level: str = "off", start_pos_sec: int = 0) -> discord.AudioSource:
    """Creates a high-performance direct Opus audio source with anti-jitter 2048KB buffer & EQ boost.
    Bypasses Python PCM transcoding overhead, dropping CPU usage and delivering sub-5ms Opus packets."""
    boost_cfg = BOOST_PROFILES.get(boost_level, BOOST_PROFILES["off"])
    audio_filter = boost_cfg["filter"]

    before_opts = [
        "-reconnect 1",
        "-reconnect_streamed 1",
        "-reconnect_delay_max 5",
        "-reconnect_on_network_error 1",
        "-probesize 64k",
        "-analyzeduration 0"
    ]
    if start_pos_sec > 0:
        before_opts.insert(0, f"-ss {start_pos_sec}")

    before_options = " ".join(before_opts)

    # Opus Native: direct libopus encoding at 128k VBR with sub-5ms packet emission & anti-jitter 2048k buffer
    options = (
        f'-vn '
        f'-filter:a "{audio_filter}" '
        f'-buffer_size 2048k '
        f'-application audio '
        f'-vbr on '
        f'-compression_level 10'
    )

    try:
        # Primary: Direct Opus stream bypassing Python PCM transcoding
        return discord.FFmpegOpusAudio(
            stream_url,
            bitrate=128,
            before_options=before_options,
            options=options
        )
    except Exception as e:
        logger.warning(f"⚠️ FFmpegOpusAudio fallback to PCMAudio due to: {e}")
        pcm_options = f'-vn -filter:a "{audio_filter}" -buffer_size 2048k'
        return discord.FFmpegPCMAudio(
            stream_url,
            before_options=before_options,
            options=pcm_options
        )

# ==============================================================================
# 🎵 GUILD PLAYER (PER-VOICE-ROOM MANAGER)
# ==============================================================================

class GuildPlayer:
    def __init__(self, guild_id: int, bot_instance: "MusicBotInstance"):
        self.guild_id = guild_id
        self.bot_instance = bot_instance
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.current_source: Optional[discord.AudioSource] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.last_np_msg_id: Optional[int] = None
        self.boost_level: str = "off"
        self.song_start_time: Optional[float] = None
        self.pause_start_time: Optional[float] = None
        self.total_paused_sec: float = 0.0
        self.is_manual_stop: bool = False
        self.lock = asyncio.Lock()

    def get_position_sec(self) -> int:
        """Returns accurate current track playback position in seconds"""
        if not self.song_start_time:
            return 0
        current_time = time.time()
        paused = self.total_paused_sec
        if self.pause_start_time:
            paused += (current_time - self.pause_start_time)
        return max(0, int(current_time - self.song_start_time - paused))

    def cleanup_current_source(self):
        """Zero-Leak: Terminate FFmpeg subprocess & destroy stream pipes immediately"""
        if self.current_source:
            try:
                self.current_source.cleanup()
            except Exception as e:
                logger.debug(f"Error cleaning up AudioSource: {e}")
            self.current_source = None

class BoostSelectView(discord.ui.View):
    """Interactive UI buttons for instant Bass Boost adjustment"""
    def __init__(self, player: GuildPlayer, bot_instance: "MusicBotInstance", author_id: int):
        super().__init__(timeout=60)
        self.player = player
        self.bot_instance = bot_instance
        self.author_id = author_id

    async def _handle_boost_change(self, interaction: discord.Interaction, target: str):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Chỉ người gọi lệnh mới có quyền chỉnh chế độ này!", ephemeral=True)

        self.player.boost_level = target
        cfg = BOOST_PROFILES[target]
        vc = interaction.guild.voice_client

        applied_live = False
        if vc and (vc.is_playing() or vc.is_paused()) and self.player.current:
            try:
                elapsed = self.player.get_position_sec()
                new_source = make_audio_source(self.player.current.stream_url, self.player.boost_level, start_pos_sec=elapsed)
                old_source = vc.source
                vc.source = new_source
                self.player.current_source = new_source
                self.player.song_start_time = time.time() - elapsed
                self.player.total_paused_sec = 0.0
                self.player.pause_start_time = time.time() if vc.is_paused() else None
                if old_source:
                    try:
                        old_source.cleanup()
                    except Exception:
                        pass
                applied_live = True
            except Exception as e:
                logger.warning(f"Could not live-swap source: {e}")

        embed = discord.Embed(
            title=f"{cfg['emoji']} Chế Độ Âm Thanh: {cfg['name']}",
            description=(
                f"**Mô tả:** {cfg['desc']}\n"
                f"**Bộ lọc:** `{cfg['filter']}`\n\n"
                + ("⚡ **Đã áp dụng ngay lập tức cho bài hát đang phát!**" if applied_live
                   else "🎵 Sẽ áp dụng khi phát bài tiếp theo!")
            ),
            color=self.bot_instance.embed_color
        )
        embed.set_footer(text="HacTuoc Music Cluster • Opus Native Engine 24/7 🚀")
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Tắt (Studio)", style=discord.ButtonStyle.secondary, emoji="🔇")
    async def btn_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_boost_change(interaction, "off")

    @discord.ui.button(label="Nhẹ", style=discord.ButtonStyle.primary, emoji="🎵")
    async def btn_low(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_boost_change(interaction, "low")

    @discord.ui.button(label="Vừa (Chuẩn)", style=discord.ButtonStyle.success, emoji="🔊")
    async def btn_med(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_boost_change(interaction, "medium")

    @discord.ui.button(label="Mạnh", style=discord.ButtonStyle.danger, emoji="🚀")
    async def btn_high(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_boost_change(interaction, "high")

    @discord.ui.button(label="Cực Đại", style=discord.ButtonStyle.danger, emoji="💥")
    async def btn_extreme(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_boost_change(interaction, "extreme")

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

            # Sync slash commands (/boost, /nowplaying)
            if not getattr(instance, "_synced", False):
                try:
                    for g in bot.guilds:
                        bot.tree.copy_global_to(guild=g)
                        await bot.tree.sync(guild=g)
                    await bot.tree.sync()
                    instance._synced = True
                    logger.info(f"[{instance.name}] Đã đồng bộ slash commands (/boost, /nowplaying) thành công!")
                except Exception as e:
                    logger.error(f"[{instance.name}] Lỗi đồng bộ slash command: {e}")

        @bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            """Auto disconnect after 120s if voice room becomes empty (Zero-Leak reclamation)"""
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
                                player.is_manual_stop = True
                                player.queue.clear()
                                player.song_start_time = None
                                player.pause_start_time = None
                                player.total_paused_sec = 0.0
                                player.cleanup_current_source()
                                player.current = None
                                await vc.disconnect()
                                gc.collect()
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
                    player.pause_start_time = time.time()
                    vc.pause()
                    if channel:
                        await channel.send("⏸️ Đã tạm dừng phát nhạc.", delete_after=5)
                elif vc.is_paused():
                    if player.pause_start_time:
                        player.total_paused_sec += (time.time() - player.pause_start_time)
                        player.pause_start_time = None
                    vc.resume()
                    if channel:
                        await channel.send("▶️ Đã tiếp tục phát nhạc.", delete_after=5)
            elif emoji_name == "⏭️":
                if not vc or (not vc.is_playing() and not vc.is_paused()):
                    if channel:
                        await channel.send("❌ Không có bài hát nào đang phát để bỏ qua!", delete_after=5)
                    return
                player.song_start_time = None
                player.pause_start_time = None
                player.total_paused_sec = 0.0
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
                player.is_manual_stop = True
                player.queue.clear()
                player.song_start_time = None
                player.pause_start_time = None
                player.total_paused_sec = 0.0
                player.current = None
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                player.cleanup_current_source()
                await vc.disconnect()
                gc.collect()
                if channel:
                    await channel.send(f"⏹️ {member.mention} đã bấm dừng phát nhạc và mời {instance.name} rời phòng.")

        # --- BOT COMMANDS ---

        @bot.command(name="play", aliases=["p"])
        async def cmd_play(ctx: commands.Context, *, query: Optional[str] = None):
            """Phát nhạc từ Zing MP3 hoặc toàn bộ Album/Playlist"""
            prefix = instance.prefixes[0]
            if not query:
                return await ctx.reply(f"⚠️ Vui lòng nhập tên bài hát hoặc link Zing MP3! (Ví dụ: `{prefix}play Cắt Đôi Nỗi Sầu` hoặc link Album/Playlist)")

            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.reply("❌ Bạn phải tham gia vào một phòng Voice trước khi gọi bot phát nhạc nhé!")

            voice_channel = ctx.author.voice.channel

            # Detect playlist / album URLs (Zing, YouTube list=, Spotify album/playlist, SoundCloud sets)
            is_playlist = bool(
                re.search(r"/(?:album|playlist)/(?:[^/]+/)?([A-Za-z0-9]+)(?:\.html)?", query) or
                ("youtube.com" in query.lower() and "list=" in query.lower()) or
                ("open.spotify.com" in query.lower() and ("/album/" in query.lower() or "/playlist/" in query.lower())) or
                ("soundcloud.com" in query.lower() and "/sets/" in query.lower())
            )
            if is_playlist:
                searching_msg = await ctx.send("🔍 Đang nạp toàn bộ Album / Playlist... Chờ xíu nha 💿")
                return await instance.play_playlist_helper(
                    ctx,
                    query.strip(),
                    ctx.author,
                    voice_channel,
                    ctx.channel,
                    searching_msg=searching_msg,
                    is_slash=False
                )

            vc = ctx.guild.voice_client

            if vc is None:
                try:
                    vc = await voice_channel.connect()
                    optimize_voice_socket(vc)
                except Exception as e:
                    return await ctx.reply(f"❌ Không thể tham gia phòng Voice: {e}")
            elif vc.channel != voice_channel:
                await vc.move_to(voice_channel)
                optimize_voice_socket(vc)

            searching_msg = await ctx.send(f"🔍 Đang tìm & nạp luồng siêu tốc **{query}**... Chờ xíu nha 🌸")

            song = await multi_engine.resolve_song(query, ctx.author)
            if not song:
                return await searching_msg.edit(content=f"❌ Không thể tìm thấy hoặc lấy luồng phát cho bài **{query}**.")

            player = instance.get_player(ctx.guild.id)
            player.text_channel = ctx.channel

            if vc.is_playing() or vc.is_paused():
                player.queue.append(song)
                embed = discord.Embed(
                    title=f"📥 ₊˚ ĐÃ THÊM VÀO HÀNG ĐỢI • {instance.name} ✦",
                    description=(
                        f"🎵 **Bài hát:** **{song.title}**\n"
                        f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                        f"📻 **Nền tảng:** `{song.platform_badge}`\n"
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

        @bot.command(name="playlist", aliases=["album", "pl"])
        async def cmd_playlist(ctx: commands.Context, *, query: Optional[str] = None):
            """Phát toàn bộ Album hoặc Playlist từ YouTube, Zing MP3, Spotify, SoundCloud"""
            prefix = instance.prefixes[0]
            if not query:
                return await ctx.reply(f"⚠️ Vui lòng nhập link hoặc ID Album / Playlist! (Ví dụ: `{prefix}playlist <link YouTube / Spotify / Zing MP3>` hoặc ID Zing)")

            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.reply("❌ Bạn phải tham gia vào một phòng Voice trước khi gọi bot phát nhạc nhé!")

            voice_channel = ctx.author.voice.channel
            searching_msg = await ctx.send(f"🔍 Đang nạp toàn bộ Album / Playlist **{query.strip()}**... Chờ xíu nha 💿")
            await instance.play_playlist_helper(
                ctx,
                query.strip(),
                ctx.author,
                voice_channel,
                ctx.channel,
                searching_msg=searching_msg,
                is_slash=False
            )

        @bot.command(name="skip", aliases=["s", "next"])
        async def cmd_skip(ctx: commands.Context):
            """Bỏ qua bài hát hiện tại"""
            vc = ctx.guild.voice_client
            if not vc or (not vc.is_playing() and not vc.is_paused()):
                return await ctx.reply("❌ Không có bài hát nào đang phát để bỏ qua!")
            player = instance.get_player(ctx.guild.id)
            player.song_start_time = None
            player.pause_start_time = None
            player.total_paused_sec = 0.0
            vc.stop()
            await ctx.send(f"⏭️ {ctx.author.mention} đã bỏ qua bài hát hiện tại!")

        @bot.command(name="pause")
        async def cmd_pause(ctx: commands.Context):
            """Tạm dừng bài hát"""
            vc = ctx.guild.voice_client
            if vc and vc.is_playing():
                player = instance.get_player(ctx.guild.id)
                player.pause_start_time = time.time()
                vc.pause()
                await ctx.send(f"⏸️ Đã tạm dừng phát nhạc. Gõ `{instance.prefixes[0]}resume` để tiếp tục!")
            else:
                await ctx.reply("❌ Không có bài hát nào đang phát!")

        @bot.command(name="resume")
        async def cmd_resume(ctx: commands.Context):
            """Tiếp tục phát nhạc"""
            vc = ctx.guild.voice_client
            if vc and vc.is_paused():
                player = instance.get_player(ctx.guild.id)
                if player.pause_start_time:
                    player.total_paused_sec += (time.time() - player.pause_start_time)
                    player.pause_start_time = None
                vc.resume()
                await ctx.send("▶️ Đã tiếp tục phát nhạc!")
            else:
                await ctx.reply("❌ Nhạc không ở trạng thái tạm dừng!")

        @bot.command(name="stop", aliases=["leave"])
        async def cmd_stop(ctx: commands.Context):
            """Dừng phát nhạc và rời phòng Voice (Giải phóng bộ nhớ triệt để)"""
            vc = ctx.guild.voice_client
            if not vc:
                return await ctx.reply("❌ Bot không có trong phòng Voice!")
            player = instance.get_player(ctx.guild.id)
            player.is_manual_stop = True
            player.queue.clear()
            player.song_start_time = None
            player.pause_start_time = None
            player.total_paused_sec = 0.0
            player.current = None
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            player.cleanup_current_source()
            await vc.disconnect()
            gc.collect()
            await ctx.send(f"⏹️ Đã dừng phát nhạc, giải phóng bộ nhớ và {instance.name} đã rời phòng.")

        @bot.command(name="queue", aliases=["q"])
        async def cmd_queue(ctx: commands.Context):
            """Xem danh sách bài hát đang chờ"""
            player = instance.get_player(ctx.guild.id)
            if not player.current and not player.queue:
                return await ctx.reply(f"📭 Hàng đợi hiện đang trống! Dùng `{instance.prefixes[0]}play <tên bài>` để gọi nhạc nhé.")

            desc = ""
            if player.current:
                cur_link = player.current.webpage_url or "https://zingmp3.vn"
                desc += f"🎶 **Đang phát:** **[{player.current.title}]({cur_link})** - `{player.current.artists}` `[{player.current.duration_str}]` `[{player.current.platform_badge}]` (bởi {player.current.requester.mention})\n\n"

            if player.queue:
                desc += "📋 **Danh sách chờ tiếp theo:**\n"
                for i, s in enumerate(player.queue[:10], 1):
                    desc += f"`{i}.` **{s.title}** - `{s.artists}` `[{s.duration_str}]` `[{s.platform_badge}]` (bởi {s.requester.display_name})\n"
                if len(player.queue) > 10:
                    desc += f"\n*... và còn {len(player.queue) - 10} bài hát khác trong hàng đợi.*"

            embed = discord.Embed(
                title=f"📜 ₊˚ HÀNG ĐỢI • {instance.name.upper()} ✦",
                description=desc,
                color=instance.embed_color
            )
            embed.set_footer(text=f"Tổng số bài trong hàng đợi: {len(player.queue)} bài • Prefix: {instance.prefixes[0]}")
            await ctx.send(embed=embed)

        @bot.command(name="ping", aliases=["latency", "ms"])
        async def cmd_ping(ctx: commands.Context):
            """Kiểm tra độ trễ Discord Gateway và Voice Ping thời gian thực"""
            vc = ctx.guild.voice_client
            gw_ping = round(bot.latency * 1000, 1) if bot.latency is not None else 0.0
            voice_ping_str = "Chưa kết nối"
            if vc and vc.is_connected():
                lat = getattr(vc, "latency", None)
                if lat is not None and not (isinstance(lat, float) and (lat != lat or lat < 0)):
                    voice_ping_str = f"{round(lat * 1000, 1)} ms"
                else:
                    avg_lat = getattr(vc, "average_latency", None)
                    if avg_lat is not None and not (isinstance(avg_lat, float) and (avg_lat != avg_lat or avg_lat < 0)):
                        voice_ping_str = f"{round(avg_lat * 1000, 1)} ms"
                    else:
                        voice_ping_str = f"~{gw_ping} ms (Đang đồng bộ)"

            embed = discord.Embed(
                title=f"📶 ₊˚ ĐỘ TRỄ HỆ THỐNG • {instance.name.upper()} ✦",
                description=(
                    f"⚡ **Gateway Latency (Ping Bot):** `{gw_ping} ms`\n"
                    f"🎙️ **Voice Ping (Âm thanh Voice):** `{voice_ping_str}`\n"
                    f"🛡️ **Bộ đệm Anti-Jitter:** `2048 KB (~128s chống giật/dựt)`\n"
                    f"🚀 **Đường truyền:** `Opus Native (Sub-5ms) + DSCP Priority`\n"
                    f"🌐 **Nền tảng hỗ trợ:** `YouTube, Zing MP3, SoundCloud, Spotify, Direct`"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text=f"Server: {ctx.guild.name} • Đạt chuẩn Zero-Lag Zero-Stutter")
            await ctx.reply(embed=embed)

        @bot.command(name="nowplaying", aliases=["np"])
        async def cmd_nowplaying(ctx: commands.Context):
            """Xem bài hát đang phát kèm thanh tiến trình thời gian thực"""
            player = instance.get_player(ctx.guild.id)
            if not player.current:
                return await ctx.reply("❌ Hiện không có bài hát nào đang phát!")
            song = player.current
            prefix = instance.prefixes[0]
            boost_cfg = BOOST_PROFILES.get(player.boost_level, BOOST_PROFILES["off"])
            boost_tag = f" • {boost_cfg['emoji']} `{boost_cfg['name']}`" if player.boost_level != "off" else ""
            pos_sec = player.get_position_sec()
            progress = format_progress_bar(pos_sec, song.duration_sec)
            gw_ping = round(bot.latency * 1000, 1) if bot.latency is not None else 0.0
            vc = ctx.guild.voice_client
            voice_ping_str = f"{round(vc.latency * 1000, 1)}ms" if vc and getattr(vc, "latency", None) and vc.latency >= 0 else f"{gw_ping}ms"

            embed = discord.Embed(
                title=f"🎶 ₊˚ ĐANG PHÁT HIỆN TẠI • {instance.name} ✦",
                description=(
                    f"🎵 **Bài hát:** **{song.title}**\n"
                    f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                    f"📻 **Nền tảng:** `{song.platform_badge}`\n"
                    f"⏱️ **Tiến trình:** {progress}{boost_tag}\n"
                    f"👤 **Yêu cầu bởi:** {song.requester.mention}\n\n"
                    "💡 **Phím điều khiển nhanh (Bấm biểu cảm bên dưới):**\n"
                    "⏯️ `Tạm dừng / Tiếp tục` • ⏭️ `Bỏ qua bài`\n"
                    "📜 `Xem hàng đợi` • ⏹️ `Dừng & Rời phòng`\n"
                    f"🚀 Gõ `{prefix}boost` hoặc `/boost` để chỉnh Bass Boost cực xịn!"
                ),
                color=instance.embed_color
            )
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.set_footer(text=f"Opus Native Sub-5ms • Buffer 2048KB • Ping: {gw_ping}ms / Voice: {voice_ping_str} 🎧")
            msg = await ctx.send(embed=embed)
            player.last_np_msg_id = msg.id
            for emo in ["⏯️", "⏭️", "📜", "⏹️"]:
                try:
                    await msg.add_reaction(emo)
                except Exception:
                    pass

        @bot.command(name="boost", aliases=["bass", "b", "bassboost", "equalizer"])
        async def cmd_boost(ctx: commands.Context, level: Optional[str] = None):
            """🚀 Tăng cường âm trầm (Bass Boost) & chất lượng âm thanh cực xịn"""
            player = instance.get_player(ctx.guild.id)
            vc = ctx.guild.voice_client

            if level:
                level_clean = level.strip().lower()
                alias_map = {
                    "0": "off", "tat": "off", "off": "off", "none": "off", "false": "off",
                    "1": "low", "nhe": "low", "soft": "low", "low": "low",
                    "2": "medium", "vua": "medium", "med": "medium", "norm": "medium",
                    "3": "high", "manh": "high", "hard": "high",
                    "4": "extreme", "ultra": "extreme", "max": "extreme", "cucdai": "extreme"
                }
                target = alias_map.get(level_clean)
                if not target or target not in BOOST_PROFILES:
                    embed = discord.Embed(
                        title="⚠️ Mức Boost Không Hợp Lệ",
                        description=(
                            "Vui lòng chọn một trong các mức sau:\n"
                            "• `off` / `0`: Tắt (Chuẩn Studio)\n"
                            "• `low` / `1`: Nhẹ (Acoustic / Vocal)\n"
                            "• `medium` / `2`: Vừa (EDM / Pop - Khuyên dùng)\n"
                            "• `high` / `3`: Mạnh (Vinahouse / Hardstyle)\n"
                            "• `extreme` / `4`: Cực Đại (Ultra Bass Max)\n\n"
                            f"Ví dụ: `{instance.prefixes[0]}boost medium`"
                        ),
                        color=instance.embed_color
                    )
                    return await ctx.send(embed=embed)

                player.boost_level = target
                cfg = BOOST_PROFILES[target]

                applied_live = False
                if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                    try:
                        elapsed = player.get_position_sec()
                        new_source = make_audio_source(player.current.stream_url, player.boost_level, start_pos_sec=elapsed)
                        old_source = vc.source
                        vc.source = new_source
                        player.current_source = new_source
                        player.song_start_time = time.time() - elapsed
                        player.total_paused_sec = 0.0
                        player.pause_start_time = time.time() if vc.is_paused() else None
                        if old_source:
                            try:
                                old_source.cleanup()
                            except Exception:
                                pass
                        applied_live = True
                    except Exception as e:
                        logger.warning(f"Could not live-swap source: {e}")

                embed = discord.Embed(
                    title=f"{cfg['emoji']} Chế Độ Âm Thanh: {cfg['name']}",
                    description=(
                        f"**Mô tả:** {cfg['desc']}\n"
                        f"**Bộ lọc:** `{cfg['filter']}`\n\n"
                        + ("⚡ **Đã áp dụng ngay lập tức cho bài hát đang phát!**" if applied_live
                           else "🎵 Sẽ áp dụng khi phát bài tiếp theo!")
                    ),
                    color=instance.embed_color
                )
                embed.set_footer(text="Opus Native Pipeline + Dynamic Normalizer 🚀")
                return await ctx.send(embed=embed)

            # Interactive UI buttons
            view = BoostSelectView(player, instance, ctx.author.id)
            current_cfg = BOOST_PROFILES.get(player.boost_level, BOOST_PROFILES["off"])
            embed = discord.Embed(
                title=f"🚀 ₊˚ TRÌNH TỐI ƯU & BASS BOOST • {instance.name.upper()} ✦",
                description=(
                    f"Chế độ hiện tại: **{current_cfg['name']}** {current_cfg['emoji']}\n\n"
                    "Chọn mức độ Bass Boost để kích hoạt ngay âm thanh sống động, mạnh mẽ:\n"
                    "🔇 **Tắt:** Âm thanh phòng thu trung thực\n"
                    "🎵 **Nhẹ:** Tăng nhẹ âm trầm, vocal sắc nét\n"
                    "🔊 **Vừa:** Chuẩn Club / EDM / Pop cực căng (Khuyên dùng)\n"
                    "🚀 **Mạnh:** Siêu trầm, đập rung màng loa\n"
                    "💥 **Cực Đại:** Công suất đỉnh cao vô địch\n\n"
                    "*(Tích hợp bộ lọc Dynamic Audio Normalizer tự động chống vỡ tiếng và triệt tiêu méo âm 🎧)*"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text="Bấm một trong các nút bên dưới để đổi chế độ")
            await ctx.send(embed=embed, view=view)

        @bot.tree.command(name="boost", description="🚀 Tăng cường âm trầm (Bass Boost) & chất lượng âm thanh cực xịn cho phòng Voice")
        @app_commands.describe(muc_do="Chọn mức độ Boost âm thanh")
        @app_commands.choices(muc_do=[
            app_commands.Choice(name="🔇 Tắt (Chuẩn Studio)", value="off"),
            app_commands.Choice(name="🎵 Nhẹ (Acoustic / Vocal)", value="low"),
            app_commands.Choice(name="🔊 Vừa (Club / EDM / Pop - Khuyên dùng)", value="medium"),
            app_commands.Choice(name="🚀 Mạnh (Vinahouse / Hardstyle)", value="high"),
            app_commands.Choice(name="💥 Cực Đại (Ultra Bass Max)", value="extreme")
        ])
        async def slash_boost(interaction: discord.Interaction, muc_do: Optional[app_commands.Choice[str]] = None):
            guild = interaction.guild
            if not guild:
                return await interaction.response.send_message("❌ Lệnh này chỉ dùng trong máy chủ!", ephemeral=True)
            player = instance.get_player(guild.id)
            vc = guild.voice_client

            if muc_do:
                target = muc_do.value
                player.boost_level = target
                cfg = BOOST_PROFILES[target]

                applied_live = False
                if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                    try:
                        elapsed = player.get_position_sec()
                        new_source = make_audio_source(player.current.stream_url, player.boost_level, start_pos_sec=elapsed)
                        old_source = vc.source
                        vc.source = new_source
                        player.current_source = new_source
                        player.song_start_time = time.time() - elapsed
                        player.total_paused_sec = 0.0
                        player.pause_start_time = time.time() if vc.is_paused() else None
                        if old_source:
                            try:
                                old_source.cleanup()
                            except Exception:
                                pass
                        applied_live = True
                    except Exception as e:
                        logger.warning(f"Could not live-swap source: {e}")

                embed = discord.Embed(
                    title=f"{cfg['emoji']} Chế Độ Âm Thanh: {cfg['name']}",
                    description=(
                        f"**Mô tả:** {cfg['desc']}\n"
                        f"**Bộ lọc:** `{cfg['filter']}`\n\n"
                        + ("⚡ **Đã áp dụng ngay lập tức cho bài hát đang phát!**" if applied_live
                           else "🎵 Sẽ áp dụng khi phát bài tiếp theo!")
                    ),
                    color=instance.embed_color
                )
                embed.set_footer(text="Opus Native Pipeline + Dynamic Normalizer 🚀")
                return await interaction.response.send_message(embed=embed)

            view = BoostSelectView(player, instance, interaction.user.id)
            current_cfg = BOOST_PROFILES.get(player.boost_level, BOOST_PROFILES["off"])
            embed = discord.Embed(
                title=f"🚀 ₊˚ TRÌNH TỐI ƯU & BASS BOOST • {instance.name.upper()} ✦",
                description=(
                    f"Chế độ hiện tại: **{current_cfg['name']}** {current_cfg['emoji']}\n\n"
                    "Chọn mức độ Bass Boost để kích hoạt ngay âm thanh sống động, mạnh mẽ:\n"
                    "🔇 **Tắt:** Âm thanh phòng thu trung thực\n"
                    "🎵 **Nhẹ:** Tăng nhẹ âm trầm, vocal sắc nét\n"
                    "🔊 **Vừa:** Chuẩn Club / EDM / Pop cực căng (Khuyên dùng)\n"
                    "🚀 **Mạnh:** Siêu trầm, đập rung màng loa\n"
                    "💥 **Cực Đại:** Công suất đỉnh cao vô địch\n\n"
                    "*(Tích hợp bộ lọc Dynamic Audio Normalizer tự động chống vỡ tiếng và triệt tiêu méo âm 🎧)*"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text="Bấm một trong các nút bên dưới để đổi chế độ")
            await interaction.response.send_message(embed=embed, view=view)

        @bot.tree.command(name="nowplaying", description="🎶 Xem bài hát đang phát và thanh tiến trình thời gian thực")
        async def slash_nowplaying(interaction: discord.Interaction):
            guild = interaction.guild
            if not guild:
                return await interaction.response.send_message("❌ Lệnh này chỉ dùng trong máy chủ!", ephemeral=True)
            player = instance.get_player(guild.id)
            if not player.current:
                return await interaction.response.send_message("❌ Hiện không có bài hát nào đang phát!", ephemeral=True)
            song = player.current
            prefix = instance.prefixes[0]
            boost_cfg = BOOST_PROFILES.get(player.boost_level, BOOST_PROFILES["off"])
            boost_tag = f" • {boost_cfg['emoji']} `{boost_cfg['name']}`" if player.boost_level != "off" else ""
            pos_sec = player.get_position_sec()
            progress = format_progress_bar(pos_sec, song.duration_sec)
            gw_ping = round(bot.latency * 1000, 1) if bot.latency is not None else 0.0
            vc = guild.voice_client
            voice_ping_str = f"{round(vc.latency * 1000, 1)}ms" if vc and getattr(vc, "latency", None) and vc.latency >= 0 else f"{gw_ping}ms"

            embed = discord.Embed(
                title=f"🎶 ₊˚ ĐANG PHÁT HIỆN TẠI • {instance.name} ✦",
                description=(
                    f"🎵 **Bài hát:** **{song.title}**\n"
                    f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                    f"📻 **Nền tảng:** `{song.platform_badge}`\n"
                    f"⏱️ **Tiến trình:** {progress}{boost_tag}\n"
                    f"👤 **Yêu cầu bởi:** {song.requester.mention}\n\n"
                    f"🚀 Gõ `{prefix}boost` hoặc `/boost` để chỉnh Bass Boost cực xịn!"
                ),
                color=instance.embed_color
            )
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.set_footer(text=f"Opus Native Sub-5ms • Buffer 2048KB • Ping: {gw_ping}ms / Voice: {voice_ping_str} 🎧")
            await interaction.response.send_message(embed=embed)

        @bot.tree.command(name="ping", description="📶 Kiểm tra độ trễ Discord Gateway và Voice Ping thời gian thực")
        async def slash_ping(interaction: discord.Interaction):
            guild = interaction.guild
            if not guild:
                return await interaction.response.send_message("❌ Lệnh này chỉ dùng trong máy chủ!", ephemeral=True)
            vc = guild.voice_client
            gw_ping = round(bot.latency * 1000, 1) if bot.latency is not None else 0.0
            voice_ping_str = "Chưa kết nối"
            if vc and vc.is_connected():
                lat = getattr(vc, "latency", None)
                if lat is not None and not (isinstance(lat, float) and (lat != lat or lat < 0)):
                    voice_ping_str = f"{round(lat * 1000, 1)} ms"
                else:
                    avg_lat = getattr(vc, "average_latency", None)
                    if avg_lat is not None and not (isinstance(avg_lat, float) and (avg_lat != avg_lat or avg_lat < 0)):
                        voice_ping_str = f"{round(avg_lat * 1000, 1)} ms"
                    else:
                        voice_ping_str = f"~{gw_ping} ms (Đang đồng bộ)"

            embed = discord.Embed(
                title=f"📶 ₊˚ ĐỘ TRỄ HỆ THỐNG • {instance.name.upper()} ✦",
                description=(
                    f"⚡ **Gateway Latency (Ping Bot):** `{gw_ping} ms`\n"
                    f"🎙️ **Voice Ping (Âm thanh Voice):** `{voice_ping_str}`\n"
                    f"🛡️ **Bộ đệm Anti-Jitter:** `2048 KB (~128s chống giật/dựt)`\n"
                    f"🚀 **Đường truyền:** `Opus Native (Sub-5ms) + DSCP Priority`\n"
                    f"🌐 **Nền tảng hỗ trợ:** `YouTube, Zing MP3, SoundCloud, Spotify, Direct`"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text=f"Server: {guild.name} • Đạt chuẩn Zero-Lag Zero-Stutter")
            await interaction.response.send_message(embed=embed)

        @bot.tree.command(name="play", description="🎵 Phát nhạc từ YouTube, Zing MP3, SoundCloud, Spotify hoặc Direct Stream")
        @app_commands.describe(query="Tên bài hát hoặc link YouTube / Zing MP3 / SoundCloud / Spotify / Radio")
        async def slash_play(interaction: discord.Interaction, query: str):
            guild = interaction.guild
            if not guild:
                return await interaction.response.send_message("❌ Lệnh này chỉ dùng trong máy chủ!", ephemeral=True)
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
                return await interaction.response.send_message("❌ Bạn phải tham gia vào một phòng Voice trước khi gọi bot phát nhạc nhé!", ephemeral=True)

            await interaction.response.defer()
            voice_channel = member.voice.channel

            # Detect playlist / album URLs (Zing, YouTube list=, Spotify album/playlist, SoundCloud sets)
            is_playlist = bool(
                re.search(r"/(?:album|playlist)/(?:[^/]+/)?([A-Za-z0-9]+)(?:\.html)?", query) or
                ("youtube.com" in query.lower() and "list=" in query.lower()) or
                ("open.spotify.com" in query.lower() and ("/album/" in query.lower() or "/playlist/" in query.lower())) or
                ("soundcloud.com" in query.lower() and "/sets/" in query.lower())
            )
            if is_playlist:
                return await instance.play_playlist_helper(
                    interaction,
                    query.strip(),
                    member,
                    voice_channel,
                    interaction.channel,
                    searching_msg=None,
                    is_slash=True
                )

            vc = guild.voice_client
            if vc is None:
                try:
                    vc = await voice_channel.connect()
                    optimize_voice_socket(vc)
                except Exception as e:
                    return await interaction.followup.send(f"❌ Không thể tham gia phòng Voice: {e}")
            elif vc.channel != voice_channel:
                await vc.move_to(voice_channel)
                optimize_voice_socket(vc)

            song = await multi_engine.resolve_song(query, member)
            if not song:
                return await interaction.followup.send(f"❌ Không thể tìm thấy hoặc lấy luồng phát cho bài **{query}**.")

            player = instance.get_player(guild.id)
            player.text_channel = interaction.channel

            if vc.is_playing() or vc.is_paused():
                player.queue.append(song)
                embed = discord.Embed(
                    title=f"📥 ₊˚ ĐÃ THÊM VÀO HÀNG ĐỢI • {instance.name} ✦",
                    description=(
                        f"🎵 **Bài hát:** **{song.title}**\n"
                        f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                        f"📻 **Nền tảng:** `{song.platform_badge}`\n"
                        f"⏱️ **Thời lượng:** `{song.duration_str}`\n"
                        f"🔢 **Vị trí hàng đợi:** `#{len(player.queue)}`\n"
                        f"👤 **Người yêu cầu:** {member.mention}"
                    ),
                    color=instance.embed_color
                )
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                embed.set_footer(text=f"Gõ {instance.prefixes[0]}queue để xem toàn bộ danh sách chờ")
                await interaction.followup.send(embed=embed)
            else:
                player.queue.append(song)
                await instance.start_next_song(guild.id)
                await interaction.followup.send(f"🎶 Đang chuẩn bị phát: **{song.title}** - `{song.artists}` ({song.platform_badge})")

        @bot.tree.command(name="playlist", description="💿 Phát toàn bộ Album hoặc Playlist từ YouTube, Zing MP3, Spotify, SoundCloud")
        @app_commands.describe(query="Link hoặc ID Album/Playlist (YouTube / Zing MP3 / Spotify / SoundCloud)")
        async def slash_playlist(interaction: discord.Interaction, query: str):
            guild = interaction.guild
            if not guild:
                return await interaction.response.send_message("❌ Lệnh này chỉ dùng trong máy chủ!", ephemeral=True)
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
                return await interaction.response.send_message("❌ Bạn phải tham gia vào một phòng Voice trước khi gọi bot phát nhạc nhé!", ephemeral=True)

            await interaction.response.defer()
            voice_channel = member.voice.channel

            await instance.play_playlist_helper(
                interaction,
                query.strip(),
                member,
                voice_channel,
                interaction.channel,
                searching_msg=None,
                is_slash=True
            )

        @bot.command(name="help", aliases=["h"])
        async def cmd_help(ctx: commands.Context):
            """Hướng dẫn sử dụng bot nhạc đa nền tảng không giựt"""
            p = instance.prefixes[0]
            embed = discord.Embed(
                title=f"ᕱ⑅ᕱ ₊˚ HƯỚNG DẪN • {instance.name.upper()} ✦",
                description=(
                    f"**{instance.name}** chuyên phụ trách phòng âm nhạc: `{instance.room_label}`\n\n"
                    f"🎵 `{p}play <tên bài / link>` (hoặc `/play`): Phát nhạc YouTube, Zing MP3, SoundCloud, Spotify, Radio\n"
                    f"💿 `{p}playlist <link / ID>` (hoặc `/playlist`, `{p}album`): Phát toàn bộ Album / Playlist Zing MP3\n"
                    f"🚀 `/boost` hoặc `{p}boost [mức độ]` (hoặc `{p}b`): Bật Bass Boost cực xịn (Tắt/Nhẹ/Vừa/Mạnh/Cực đại)\n"
                    f"📶 `{p}ping` (hoặc `/ping`): Kiểm tra độ trễ mạng Discord Gateway và Voice Ping thời gian thực\n"
                    f"⏭️ `{p}skip` (hoặc `{p}s`): Bỏ qua bài hát hiện tại\n"
                    f"⏸️ `{p}pause`: Tạm dừng phát nhạc\n"
                    f"▶️ `{p}resume`: Tiếp tục phát bài hát\n"
                    f"⏹️ `{p}stop` (hoặc `{p}leave`): Dừng phát, xóa hàng đợi & rời phòng\n"
                    f"📜 `{p}queue` (hoặc `{p}q`): Xem danh sách bài hát đang chờ\n"
                    f"🎧 `/nowplaying` hoặc `{p}np`: Xem thông tin bài hát đang phát & thanh tiến trình\n\n"
                    "*(Công nghệ Opus Native sub-5ms + 2048KB Anti-Jitter Buffer + Multi-Platform Engine không giật lag 🚀)*"
                ),
                color=instance.embed_color
            )
            embed.set_footer(text=f"Prefix: {p} • HacTuoc Music Cluster 24/7")
            await ctx.send(embed=embed)

    def handle_track_finished(self, guild_id: int, error: Optional[Exception] = None):
        """Zero-Leak Memory Optimizer & Error Recovery on track completion"""
        player = self.get_player(guild_id)
        if error:
            logger.error(f"⚠️ [{self.name}] Audio error in guild {guild_id}: {error}")

        # Explicitly destroy ffmpeg subprocess, close pipe descriptors
        player.cleanup_current_source()
        player.current = None
        player.song_start_time = None
        player.pause_start_time = None
        player.total_paused_sec = 0.0

        # Aggressive memory cleanup
        try:
            gc.collect()
        except Exception:
            pass

        # If manual stop was initiated, don't auto-start next song
        if player.is_manual_stop:
            player.is_manual_stop = False
            return

        # Smooth transition to next song
        asyncio.run_coroutine_threadsafe(self.start_next_song(guild_id), self.bot.loop)

    async def _prefetch_song(self, song: Song):
        """Warm up cache for upcoming track in background"""
        try:
            await multi_engine.refresh_stream_url(song)
        except Exception:
            pass

    async def start_next_song(self, guild_id: int):
        """Zero-Lag Playback: Pop pre-resolved song and initiate instant FFmpeg Opus stream"""
        player = self.get_player(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            player.queue.clear()
            player.cleanup_current_source()
            player.current = None
            player.song_start_time = None
            player.pause_start_time = None
            player.total_paused_sec = 0.0
            gc.collect()
            return

        async with player.lock:
            # Clean up old audio source
            player.cleanup_current_source()

            if not player.queue:
                player.current = None
                player.song_start_time = None
                player.pause_start_time = None
                player.total_paused_sec = 0.0
                gc.collect()
                if player.text_channel and not player.is_manual_stop:
                    embed = discord.Embed(
                        description=f"✨ Đã phát hết danh sách bài hát trong hàng đợi! Dùng `{self.prefixes[0]}play <tên bài>` để tiếp tục nghe nhạc nhé 🎶",
                        color=self.embed_color
                    )
                    await player.text_channel.send(embed=embed)
                return

            song = None
            while player.queue:
                candidate = player.queue.pop(0)
                # Anti-Stutter refresh right before playback
                candidate.stream_url = await multi_engine.refresh_stream_url(candidate, force=True)
                if not candidate.stream_url:
                    if player.text_channel:
                        await player.text_channel.send(f"⚠️ Bỏ qua `{candidate.title}` (không có luồng phát khả dụng).")
                    continue
                song = candidate
                break

            if not song:
                player.current = None
                player.song_start_time = None
                player.pause_start_time = None
                player.total_paused_sec = 0.0
                gc.collect()
                if player.text_channel and not player.is_manual_stop:
                    embed = discord.Embed(
                        description=f"✨ Đã phát hết danh sách bài hát trong hàng đợi! Dùng `{self.prefixes[0]}play <tên bài>` để tiếp tục nghe nhạc nhé 🎶",
                        color=self.embed_color
                    )
                    await player.text_channel.send(embed=embed)
                return

            player.current = song

            try:
                # 🚀 Opus Native Pipeline
                source = make_audio_source(song.stream_url, player.boost_level)
                player.current_source = source
                player.song_start_time = time.time()
                player.pause_start_time = None
                player.total_paused_sec = 0.0
                player.is_manual_stop = False
                vc.play(source, after=lambda err: self.handle_track_finished(guild_id, err))

                # Background pre-fetch next song in queue to ensure 0ms gap
                if player.queue:
                    asyncio.create_task(self._prefetch_song(player.queue[0]))

                if player.text_channel:
                    boost_cfg = BOOST_PROFILES.get(player.boost_level, BOOST_PROFILES["off"])
                    boost_tag = f" • {boost_cfg['emoji']} `{boost_cfg['name']}`" if player.boost_level != "off" else ""
                    embed = discord.Embed(
                        title=f"🎶 ₊˚ ĐANG PHÁT • {self.name} ✦",
                        description=(
                            f"🎵 **Bài hát:** **{song.title}**\n"
                            f"🎤 **Nghệ sĩ:** `{song.artists}`\n"
                            f"📻 **Nền tảng:** `{song.platform_badge}`\n"
                            f"⏱️ **Thời lượng:** `{song.duration_str}`{boost_tag}\n"
                            f"👤 **Yêu cầu bởi:** {song.requester.mention}\n\n"
                            "💡 **Phím điều khiển nhanh (Bấm biểu cảm bên dưới):**\n"
                            "⏯️ `Tạm dừng / Tiếp tục` • ⏭️ `Bỏ qua bài`\n"
                            "📜 `Xem hàng đợi` • ⏹️ `Dừng & Rời phòng`\n"
                            f"🚀 Gõ `{self.prefixes[0]}boost` để chỉnh Bass Boost cực xịn!"
                        ),
                        color=self.embed_color
                    )
                    if song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)
                    embed.set_footer(text=f"Opus Native Pipeline (Sub-5ms) • {self.prefixes[0]}s, {self.prefixes[0]}q, {self.prefixes[0]}stop 🎧")
                    msg = await player.text_channel.send(embed=embed)
                    player.last_np_msg_id = msg.id
                    for emo in ["⏯️", "⏭️", "📜", "⏹️"]:
                        try:
                            await msg.add_reaction(emo)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Lỗi khi phát bài ở guild {guild_id}: {e}")
                player.cleanup_current_source()
                if player.text_channel:
                    await player.text_channel.send(f"❌ Có lỗi khi phát bài `{song.title}`: {e}")
                self.handle_track_finished(guild_id, e)

    async def play_playlist_helper(
        self,
        ctx_or_interaction,
        playlist_id: str,
        user: discord.Member,
        voice_channel: discord.VoiceChannel,
        channel: discord.TextChannel,
        searching_msg=None,
        is_slash: bool = False
    ):
        """Helper to fetch, queue and play an entire Album / Playlist from Zing MP3"""
        guild = user.guild
        vc = guild.voice_client

        if vc is None:
            try:
                vc = await voice_channel.connect()
                optimize_voice_socket(vc)
            except Exception as e:
                err_text = f"❌ Không thể tham gia phòng Voice: {e}"
                if searching_msg:
                    return await searching_msg.edit(content=err_text)
                elif is_slash:
                    return await ctx_or_interaction.followup.send(err_text)
                else:
                    return await ctx_or_interaction.reply(err_text)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
            optimize_voice_socket(vc)

        pl_title, song_objs = await multi_engine.resolve_playlist(playlist_id, user)
        if not song_objs:
            err_text = "❌ Không tìm thấy Album / Playlist hoặc danh sách bài hát trống! (Hỗ trợ Zing MP3, YouTube, SoundCloud, Spotify)"
            if searching_msg:
                return await searching_msg.edit(content=err_text)
            elif is_slash:
                return await ctx_or_interaction.followup.send(err_text)
            else:
                return await ctx_or_interaction.reply(err_text)

        player = self.get_player(guild.id)
        player.text_channel = channel

        is_idle = not vc.is_playing() and not vc.is_paused()
        actual_count = len(song_objs)

        if is_idle:
            queue_pos_desc = f"▶️ Đang phát bài 1 (`#1` đến `#{actual_count}`)" if actual_count > 1 else "▶️ Đang phát ngay"
            player.queue.extend(song_objs)
            asyncio.create_task(self.start_next_song(guild.id))
        else:
            start_pos = len(player.queue) + 1
            end_pos = len(player.queue) + actual_count
            queue_pos_desc = f"`#{start_pos}` đến `#{end_pos}`"
            player.queue.extend(song_objs)

        embed = discord.Embed(
            title="💿 ĐÃ THÊM TOÀN BỘ ALBUM / PLAYLIST ✦",
            description=(
                f"📀 **Album / Playlist:** **{pl_title}**\n"
                f"🔢 **Tổng số bài hát đã thêm:** `{actual_count}` bài\n"
                f"📍 **Vị trí hàng đợi:** {queue_pos_desc}\n"
                f"👤 **Người yêu cầu:** {user.mention}\n\n"
                f"💡 Gõ `{self.prefixes[0]}queue` để xem danh sách chờ • `{self.prefixes[0]}skip` để bỏ qua bài"
            ),
            color=self.embed_color
        )
        embed.set_footer(text=f"Multi-Source Zero-Stutter Engine • {self.name} • {self.prefixes[0]}pl 🎶")

        if searching_msg:
            try:
                await searching_msg.delete()
            except Exception:
                pass
            await channel.send(embed=embed)
        elif is_slash:
            await ctx_or_interaction.followup.send(embed=embed)
        else:
            await channel.send(embed=embed)

        if is_idle:
            await self.start_next_song(guild.id)

    async def memory_optimizer_loop(self):
        """Periodic memory maintenance: prune cache, reap dead tasks, prune idle players, run GC"""
        while True:
            try:
                await asyncio.sleep(300) # every 5 minutes
                zing_engine._clean_cache()

                # Zero-Leak: prune idle guild players
                idle_guild_ids = []
                for gid, player in list(self.players.items()):
                    guild = self.bot.get_guild(gid)
                    vc = guild.voice_client if guild else None
                    if not vc and not player.current and not player.queue:
                        player.cleanup_current_source()
                        idle_guild_ids.append(gid)
                for gid in idle_guild_ids:
                    self.players.pop(gid, None)

                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Memory optimizer loop error: {e}")

    async def run(self):
        """Run bot with automatic reconnect, 429 rate limit backoff, and periodic memory optimizer"""
        if not self.token:
            logger.warning(f"⚠️ [{self.name}] Bỏ qua vì chưa có Token (DISCORD_TOKEN_{self.bot_id}).")
            return

        asyncio.create_task(self.memory_optimizer_loop())

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
            "engine": "MultiSourceEngine (YouTube, SoundCloud, Spotify, Zing MP3, Direct)",
            "audio_pipeline": "discord.FFmpegOpusAudio (Direct Opus 128k VBR, sub-5ms packet, 2048KB anti-stutter buffer)",
            "socket_optimization": "DSCP 46 Expedited Forwarding (IP_TOS 0xb8) + TCP_NODELAY",
            "memory_optimizer": "Zero-Leak Aggressive GC & Process Reaper Active",
            "rss_memory_mb": get_memory_mb(),
            "gc_counts": list(gc.get_count()),
            "active_bots_count": sum(1 for b in CLUSTER_BOTS if b.bot.is_ready()),
            "cached_multi_queries": len(multi_engine.cache),
            "cached_searches": len(zing_engine.search_cache),
            "cached_streams": len(zing_engine.stream_cache),
            "cached_playlists": len(zing_engine.playlist_cache),
            "bots": bot_statuses,
            "timestamp": datetime.datetime.now().isoformat()
        })

    async def handle_health(request):
        return web.Response(text="OK", status=200)

    async def handle_debug(request):
        q = request.query.get("q", "một bước yêu vạn dặm đau")
        sid = request.query.get("id", "B4Dxqgg21UI")
        pl_id = request.query.get("pl", "ZWZB969E")
        yt_q = request.query.get("yt")

        s_res = await zing_engine._call_node("search", q)
        st_res = await zing_engine._call_node("stream", sid)
        pl_res = await zing_engine._call_node("playlist", pl_id)

        yt_probe = None
        if yt_q:
            info = await multi_engine._extract_ytdl_async(f"ytsearch1:{yt_q}", is_playlist=False)
            if info:
                entry = info["entries"][0] if "entries" in info and info["entries"] else info
                yt_probe = {
                    "title": entry.get("title"),
                    "uploader": entry.get("uploader"),
                    "duration": entry.get("duration"),
                    "has_url": bool(entry.get("url"))
                }

        return web.json_response({
            "engine": "MultiSourceEngine Active (YouTube/SoundCloud/Spotify/Zing/Direct)",
            "query": q,
            "search_result": s_res,
            "stream_id": sid,
            "stream_result": st_res,
            "playlist_id": pl_id,
            "playlist_result": {
                "success": pl_res.get("success"),
                "title": pl_res.get("title"),
                "total": pl_res.get("total"),
                "songs_count": len(pl_res.get("songs", []))
            },
            "yt_probe": yt_probe
        })

    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/debug", handle_debug)

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
