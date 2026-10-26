import os
import sys
import re
import time
import json
import asyncio
import logging
import urllib.request
import html
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

import yt_dlp
import discord

logger = logging.getLogger("MultiSourceEngine")

def format_duration(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def build_spotify_search_query(title: str, artists: List[str]) -> str:
    """Build a search-engine friendly YouTube query from Spotify metadata without nested quotes or noise"""
    clean_title = html.unescape(title or "")
    clean_title = re.sub(r'["\'`]', '', clean_title)
    
    # Extract feature artists inside title e.g. "(feat. ...)" or "(ft. ...)"
    m_feat = re.search(r'\((?:feat|ft)\.?\s*([^)]+)\)', clean_title, flags=re.I)
    feat_artists = m_feat.group(1) if m_feat else ''
    core_title = re.sub(r'\((?:feat|ft)\.?\s*[^)]+\)', '', clean_title, flags=re.I).strip()
    
    clean_artists = []
    for a in artists:
        a_clean = html.unescape(a or "")
        a_clean = re.sub(r'["\'`]', '', a_clean).strip()
        if a_clean and a_clean.lower() not in core_title.lower():
            clean_artists.append(a_clean)
            
    top_artists = clean_artists[:2]
    if not top_artists and feat_artists:
        top_artists = [feat_artists.split(',')[0].strip()]
        
    query = f"{core_title} {' '.join(top_artists)}".strip()
    return re.sub(r'\s+', ' ', query)

class Song:
    """Universal Song model supporting Zing MP3, YouTube, SoundCloud, Spotify, and Direct Streams"""
    def __init__(self, data: Dict[str, Any], requester: discord.Member):
        self.id = data.get("id", "")
        self.title = data.get("title", "Không rõ tiêu đề")
        self.artists = data.get("artists", "Nhiều nghệ sĩ")
        self.thumbnail = data.get("thumbnail", "")
        self.stream_url = data.get("streamUrl", "")
        self.duration_sec = int(data.get("duration", 0))
        self.duration_str = data.get("durationStr", format_duration(self.duration_sec))
        self.source_type = data.get("source_type", "zing")  # zing, youtube, soundcloud, spotify, direct
        self.webpage_url = data.get("webpage_url", "")
        self.resolved_at = data.get("resolved_at", time.time())
        self.requester = requester
        self.created_at = datetime.now()

    @property
    def platform_badge(self) -> str:
        badges = {
            "zing": "🟣 Zing MP3",
            "youtube": "🔴 YouTube",
            "soundcloud": "🟠 SoundCloud",
            "spotify": "🟢 Spotify",
            "direct": "📻 Direct Stream"
        }
        return badges.get(self.source_type, "🎵 Music")


class MultiSourceEngine:
    """High-Performance Unified Music Extractor Engine with Multi-Platform Support & Zero-Stutter Caching"""

    def __init__(self, zing_engine=None):
        self.zing_engine = zing_engine
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.CACHE_TTL = 3600  # 1 hour cache
        self.MAX_CACHE_ENTRIES = 300
        self.last_error: str = ""

        # High-resilience yt-dlp config with YouTube bot bypass (android/ios player clients)
        self.ytdl_base_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch",
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios"]
                }
            }
        }

        self.ytdl_playlist_opts = {
            "format": "bestaudio/best",
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios"]
                }
            }
        }

    def _clean_cache(self):
        now = time.time()
        self.cache = {k: v for k, v in self.cache.items() if v["expiry"] > now}
        if len(self.cache) > self.MAX_CACHE_ENTRIES:
            sorted_items = sorted(self.cache.items(), key=lambda x: x[1]["expiry"], reverse=True)
            self.cache = dict(sorted_items[:self.MAX_CACHE_ENTRIES])

    def detect_platform(self, query: str) -> str:
        """Detect which platform or extractor should handle the query"""
        q = query.strip()
        lower = q.lower()

        if lower.startswith("yt:") or "youtube.com" in lower or "youtu.be" in lower:
            return "youtube"
        if lower.startswith("sc:") or "soundcloud.com" in lower:
            return "soundcloud"
        if "open.spotify.com" in lower:
            return "spotify"
        if re.search(r"\.(mp3|ogg|aac|m3u8|wav|flac)(\?.*)?$", lower) or (
            (lower.startswith("http://") or lower.startswith("https://")) and
            not any(domain in lower for domain in ["zingmp3.vn", "youtube.com", "youtu.be", "soundcloud.com", "spotify.com"])
        ):
            return "direct"
        if "zingmp3.vn" in lower:
            return "zing"

        # Default query: Zing MP3 first, falls back to YouTube automatically
        return "zing_default"

    async def _extract_ytdl_async(self, query_or_url: str, is_playlist: bool = False) -> Optional[Dict[str, Any]]:
        """Run yt-dlp extraction in an executor thread with timeout protection"""
        loop = asyncio.get_running_loop()
        opts = self.ytdl_playlist_opts if is_playlist else self.ytdl_base_opts

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(query_or_url, download=False)

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=25.0)
        except asyncio.TimeoutError:
            self.last_error = f"TimeoutError: yt-dlp extraction timed out after 25s for '{query_or_url}'"
            logger.error(self.last_error)
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"yt-dlp extraction error for '{query_or_url}': {e}")
            return None

    async def resolve_youtube(self, query: str, requester: discord.Member) -> Optional[Song]:
        """Extract and resolve audio from YouTube URL or Search query"""
        clean_q = re.sub(r"^(?:yt:|ytsearch\d*:)\s*", "", query.strip(), flags=re.IGNORECASE)
        is_url = bool(re.match(r"^https?://", clean_q))
        target = clean_q if is_url else f"ytsearch1:{clean_q}"

        self._clean_cache()
        cache_key = f"yt:{target}"
        now = time.time()
        if cache_key in self.cache and self.cache[cache_key]["expiry"] > now:
            logger.info(f"⚡ [Cache Hit] YouTube: '{target}'")
            cached_data = self.cache[cache_key]["data"]
            return Song(cached_data, requester)

        info = await self._extract_ytdl_async(target, is_playlist=False)
        if not info:
            return None

        entry = info["entries"][0] if "entries" in info and info["entries"] else info
        stream_url = entry.get("url")
        if not stream_url:
            return None

        is_live = bool(entry.get("is_live"))
        duration = int(entry.get("duration") or 0)
        duration_str = "🔴 Live Stream" if is_live else format_duration(duration)
        webpage_url = entry.get("webpage_url") or entry.get("original_url") or clean_q
        song_data = {
            "id": entry.get("id", ""),
            "title": entry.get("title", "Không rõ tiêu đề"),
            "artists": entry.get("uploader", "Nhiều nghệ sĩ"),
            "thumbnail": entry.get("thumbnail", ""),
            "streamUrl": stream_url,
            "duration": duration,
            "durationStr": duration_str,
            "source_type": "youtube",
            "webpage_url": webpage_url,
            "resolved_at": now
        }

        self.cache[cache_key] = {
            "data": song_data,
            "expiry": now + self.CACHE_TTL
        }
        return Song(song_data, requester)

    async def resolve_soundcloud(self, query: str, requester: discord.Member) -> Optional[Song]:
        """Extract audio from SoundCloud URL or search query"""
        clean_q = re.sub(r"^(?:sc:|scsearch\d*:)\s*", "", query.strip(), flags=re.IGNORECASE)
        is_url = bool(re.match(r"^https?://", clean_q))
        target = clean_q if is_url else f"scsearch1:{clean_q}"

        self._clean_cache()
        cache_key = f"sc:{target}"
        now = time.time()
        if cache_key in self.cache and self.cache[cache_key]["expiry"] > now:
            logger.info(f"⚡ [Cache Hit] SoundCloud: '{target}'")
            return Song(self.cache[cache_key]["data"], requester)

        info = await self._extract_ytdl_async(target, is_playlist=False)
        if not info:
            return None

        entry = info["entries"][0] if "entries" in info and info["entries"] else info
        stream_url = entry.get("url")
        if not stream_url:
            return None

        duration = int(entry.get("duration") or 0)
        webpage_url = entry.get("webpage_url") or entry.get("original_url") or clean_q
        song_data = {
            "id": entry.get("id", ""),
            "title": entry.get("title", "Không rõ tiêu đề"),
            "artists": entry.get("uploader", "Nghệ sĩ SoundCloud"),
            "thumbnail": entry.get("thumbnail", ""),
            "streamUrl": stream_url,
            "duration": duration,
            "durationStr": format_duration(duration),
            "source_type": "soundcloud",
            "webpage_url": webpage_url,
            "resolved_at": now
        }

        self.cache[cache_key] = {
            "data": song_data,
            "expiry": now + self.CACHE_TTL
        }
        return Song(song_data, requester)

    async def resolve_spotify(self, url: str, requester: discord.Member) -> Optional[Song]:
        """Convert Spotify track URL to metadata, then resolve stream via YouTube/Zing"""
        clean_url = url.split("?")[0].strip()
        loop = asyncio.get_running_loop()

        def _fetch_spotify_metadata() -> Tuple[Optional[str], List[str], Optional[str], int]:
            title, artists, thumb, duration_sec = None, [], None, 0
            m_tr = re.search(r"open\.spotify\.com/track/([a-zA-Z0-9]+)", clean_url)
            
            # 1. Primary: Spotify Embed JSON (most complete & reliable structured metadata)
            if m_tr:
                track_id = m_tr.group(1)
                embed_url = f"https://open.spotify.com/embed/track/{track_id}"
                try:
                    req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with urllib.request.urlopen(req, timeout=6.0) as resp:
                        text = resp.read().decode("utf-8", errors="ignore")
                    m_data = re.search(r'<script id=[\'"]__NEXT_DATA__[\'"][^>]*>(.*?)</script>', text, flags=re.DOTALL)
                    if m_data:
                        d = json.loads(m_data.group(1))
                        ent = d.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                        if ent:
                            title = ent.get("name")
                            artists = [a.get("name") for a in ent.get("artists", []) if a.get("name")]
                            dur_ms = ent.get("duration", 0)
                            if dur_ms:
                                duration_sec = int(dur_ms / 1000)
                            visuals = ent.get("visualIdentity", {}).get("image", [])
                            if visuals:
                                thumb = visuals[0].get("url")
                except Exception as e:
                    logger.debug(f"Spotify Embed fetch error: {e}")

            # 2. Secondary fallback: Spotify oEmbed API
            if not title:
                try:
                    oembed_url = f"https://open.spotify.com/oembed?url={clean_url}"
                    req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=6.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        title = data.get("title")
                        thumb = data.get("thumbnail_url")
                except Exception as e:
                    logger.debug(f"Spotify oEmbed error: {e}")

            # 3. Tertiary fallback: OpenGraph HTML meta tags
            if not title or not artists:
                try:
                    req_html = urllib.request.Request(clean_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with urllib.request.urlopen(req_html, timeout=6.0) as resp:
                        html_text = resp.read().decode("utf-8", errors="ignore")
                    if not title:
                        tm = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
                        if tm:
                            title = tm.group(1)
                    if not thumb:
                        im = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
                        if im:
                            thumb = im.group(1)
                    if not artists:
                        dm = re.search(r'<meta property="og:description" content="([^"]+)"', html_text)
                        if dm:
                            desc = dm.group(1)
                            parts = [p.strip() for p in desc.split("·")]
                            if parts:
                                artists = [parts[0]]
                except Exception as e:
                    logger.debug(f"Spotify HTML parse error: {e}")

            if not title:
                return None, [], None, 0

            title = html.unescape(title)
            artists = [html.unescape(a) for a in artists]
            return title, artists, thumb, duration_sec

        title, artists, thumb, duration_sec = await loop.run_in_executor(None, _fetch_spotify_metadata)
        if not title:
            logger.warning(f"Could not extract Spotify metadata from '{clean_url}'")
            return None

        artist_str = ", ".join(artists) if artists else "Nghệ sĩ Spotify"
        search_query = build_spotify_search_query(title, artists)
        logger.info(f"🟢 Spotify resolved: '{title}' - '{artist_str}'. Searching query: '{search_query}'...")

        # Resolve stream via YouTube
        song = await self.resolve_youtube(search_query, requester)
        if not song and search_query != title:
            # Fallback to pure title search if compound search had 0 results
            song = await self.resolve_youtube(title, requester)

        # Fallback to Zing if YouTube fails
        if not song and self.zing_engine:
            song = await self.zing_engine.resolve_song(search_query, requester)
            if not song and search_query != title:
                song = await self.zing_engine.resolve_song(title, requester)

        if song:
            song.title = title
            if artists:
                song.artists = artist_str
            if thumb:
                song.thumbnail = thumb
            if duration_sec > 0:
                song.duration_sec = duration_sec
                song.duration_str = format_duration(duration_sec)
            song.source_type = "spotify"
            song.webpage_url = clean_url
            return song

        return None

    def resolve_direct(self, url: str, requester: discord.Member) -> Song:
        """Create a Song object for direct audio/radio streams"""
        clean_url = url.strip()
        filename = clean_url.split("/")[-1].split("?")[0]
        title = filename or "Direct Stream / Web Radio"

        song_data = {
            "id": clean_url,
            "title": title,
            "artists": "Direct Audio Stream",
            "thumbnail": "https://cdn-icons-png.flaticon.com/512/3669/3669986.png",
            "streamUrl": clean_url,
            "duration": 0,
            "durationStr": "🔴 Live / Direct",
            "source_type": "direct",
            "webpage_url": clean_url,
            "resolved_at": time.time()
        }
        return Song(song_data, requester)

    async def resolve_song(self, query_or_url: str, requester: discord.Member) -> Optional[Song]:
        """Universal Song Resolver: auto-detects platform with smart Zing->YouTube fallback"""
        query_or_url = query_or_url.strip()
        platform = self.detect_platform(query_or_url)

        if platform == "youtube":
            return await self.resolve_youtube(query_or_url, requester)
        elif platform == "soundcloud":
            return await self.resolve_soundcloud(query_or_url, requester)
        elif platform == "spotify":
            return await self.resolve_spotify(query_or_url, requester)
        elif platform == "direct":
            return self.resolve_direct(query_or_url, requester)
        elif platform == "zing":
            if self.zing_engine:
                song = await self.zing_engine.resolve_song(query_or_url, requester)
                if song:
                    song.source_type = "zing"
                    return song
            # Fallback to YouTube if Zing fails
            return await self.resolve_youtube(query_or_url, requester)
        else: # "zing_default"
            # 1. Try Zing MP3 first for Vietnamese queries
            if self.zing_engine:
                try:
                    song = await self.zing_engine.resolve_song(query_or_url, requester)
                    if song and song.stream_url:
                        song.source_type = "zing"
                        return song
                except Exception as e:
                    logger.debug(f"Zing engine failed: {e}")

            # 2. Seamless fallback to YouTube for VIP songs or unindexed tracks
            logger.info(f"🔄 Zing MP3 returned no free stream for '{query_or_url}'. Falling back to YouTube...")
            song = await self.resolve_youtube(query_or_url, requester)
            return song

    async def refresh_stream_url(self, song: Song, force: bool = False) -> str:
        """Anti-Stuttering Refresh: Ensures direct audio URLs are never expired right before playback"""
        now = time.time()
        # Refresh YouTube if empty, forced, or older than 60 seconds (prevents CDN token expiry/throttle)
        if song.source_type == "youtube" and song.webpage_url:
            if not song.stream_url or force or (now - song.resolved_at > 60):
                logger.info(f"🔄 [Anti-Stutter] Refreshing direct audio stream for: {song.title}")
                refreshed = await self._extract_ytdl_async(song.webpage_url, is_playlist=False)
                if refreshed:
                    entry = refreshed["entries"][0] if "entries" in refreshed and refreshed["entries"] else refreshed
                    new_url = entry.get("url")
                    if new_url:
                        song.stream_url = new_url
                        song.resolved_at = now
                        return new_url
        elif song.source_type == "spotify":
            if not song.stream_url or force or (now - song.resolved_at > 60):
                logger.info(f"🔄 [Anti-Stutter] Resolving audio stream for Spotify track: {song.title}")
                search_q = build_spotify_search_query(song.title, [song.artists]) if song.artists else song.title
                yt_song = await self.resolve_youtube(search_q, song.requester)
                if not yt_song and search_q != song.title:
                    yt_song = await self.resolve_youtube(song.title, song.requester)
                if yt_song and yt_song.stream_url:
                    song.stream_url = yt_song.stream_url
                    song.resolved_at = now
                    return song.stream_url
        elif song.source_type == "zing" and song.id:
            if not song.stream_url or force:
                if self.zing_engine:
                    res = await self.zing_engine.get_stream(song.id)
                    if res.get("success") and res.get("streamUrl"):
                        song.stream_url = res["streamUrl"]
                        song.resolved_at = now
                        return song.stream_url
                # Seamless fallback to YouTube for VIP songs in playlist
                logger.info(f"🔄 Zing VIP or no stream for '{song.title}'. Falling back to YouTube stream...")
                search_q = f"{song.title} {song.artists}"
                yt_song = await self.resolve_youtube(search_q, song.requester)
                if yt_song and yt_song.stream_url:
                    song.stream_url = yt_song.stream_url
                    song.resolved_at = now
                    return song.stream_url

        return song.stream_url

    async def resolve_playlist(self, query_or_url: str, requester: discord.Member) -> Tuple[str, List[Song]]:
        """Multi-Platform Playlist Resolver: Supports YouTube, SoundCloud, Spotify, and Zing MP3"""
        query_or_url = query_or_url.strip()
        lower = query_or_url.lower()

        # 1. YouTube Playlist
        if "youtube.com" in lower or "youtu.be" in lower or "list=" in lower:
            info = await self._extract_ytdl_async(query_or_url, is_playlist=True)
            if info:
                pl_title = info.get("title") or "YouTube Playlist"
                raw_entries = info.get("entries") or []
                songs = []
                for entry in raw_entries:
                    if not entry:
                        continue
                    v_id = entry.get("id") or ""
                    v_url = entry.get("url") or f"https://www.youtube.com/watch?v={v_id}"
                    dur = int(entry.get("duration") or 0)
                    s_data = {
                        "id": v_id,
                        "title": entry.get("title", "YouTube Track"),
                        "artists": entry.get("uploader", "Nghệ sĩ YouTube"),
                        "thumbnail": entry.get("thumbnail", ""),
                        "streamUrl": "",  # Lazily refreshed right before play
                        "duration": dur,
                        "durationStr": format_duration(dur),
                        "source_type": "youtube",
                        "webpage_url": v_url,
                        "resolved_at": 0
                    }
                    songs.append(Song(s_data, requester))
                return pl_title, songs

        # 2. SoundCloud Playlist / Set
        if "soundcloud.com" in lower:
            info = await self._extract_ytdl_async(query_or_url, is_playlist=True)
            if info:
                pl_title = info.get("title") or "SoundCloud Playlist"
                raw_entries = info.get("entries") or []
                songs = []
                for entry in raw_entries:
                    if not entry:
                        continue
                    v_id = entry.get("id") or ""
                    v_url = entry.get("url") or entry.get("webpage_url") or query_or_url
                    dur = int(entry.get("duration") or 0)
                    s_data = {
                        "id": str(v_id),
                        "title": entry.get("title", "SoundCloud Track"),
                        "artists": entry.get("uploader", "Nghệ sĩ SoundCloud"),
                        "thumbnail": entry.get("thumbnail", ""),
                        "streamUrl": "",
                        "duration": dur,
                        "durationStr": format_duration(dur),
                        "source_type": "soundcloud",
                        "webpage_url": v_url,
                        "resolved_at": 0
                    }
                    songs.append(Song(s_data, requester))
                return pl_title, songs

        # 3. Spotify Album or Playlist
        if "open.spotify.com" in lower:
            loop = asyncio.get_running_loop()
            def _extract_spotify_playlist():
                clean_url = query_or_url.split("?")[0]
                m = re.search(r"open\.spotify\.com/(album|playlist)/([a-zA-Z0-9]+)", clean_url)
                if not m:
                    return None, []
                kind, sp_id = m.group(1), m.group(2)
                embed_url = f"https://open.spotify.com/embed/{kind}/{sp_id}"
                req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                try:
                    with urllib.request.urlopen(req, timeout=8.0) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', html)
                    if match:
                        data = json.loads(match.group(1))
                        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                        title = entity.get("title") or entity.get("name") or f"Spotify {kind.capitalize()}"
                        track_list = entity.get("trackList", [])
                        return title, track_list
                except Exception as e:
                    logger.error(f"Error fetching Spotify embed data: {e}")
                return None, []

            pl_title, track_list = await loop.run_in_executor(None, _extract_spotify_playlist)
            if track_list:
                songs = []
                for t in track_list:
                    dur_sec = int((t.get("duration") or 0) / 1000)
                    s_data = {
                        "id": t.get("uri", ""),
                        "title": t.get("title", "Spotify Track"),
                        "artists": t.get("subtitle", "Nghệ sĩ"),
                        "thumbnail": "",
                        "streamUrl": "",  # Lazily resolved right before play
                        "duration": dur_sec,
                        "durationStr": format_duration(dur_sec),
                        "source_type": "spotify",
                        "webpage_url": f"https://open.spotify.com/track/{t.get('id', '')}" if t.get('id') else query_or_url,
                        "resolved_at": 0
                    }
                    songs.append(Song(s_data, requester))
                return pl_title or "Spotify Playlist", songs

        # 4. Zing MP3 Album / Playlist
        if self.zing_engine:
            pl_match = re.search(r"/(?:album|playlist)/(?:[^/]+/)?([A-Za-z0-9]+)(?:\.html)?", query_or_url)
            playlist_id = pl_match.group(1) if pl_match else query_or_url
            pl_data = await self.zing_engine.get_playlist(playlist_id)
            if pl_data.get("success") and pl_data.get("songs"):
                pl_title = pl_data.get("title", "Zing MP3 Playlist")
                songs = []
                for s in pl_data["songs"]:
                    s["source_type"] = "zing"
                    songs.append(Song(s, requester))
                return pl_title, songs

        return "Không xác định", []
