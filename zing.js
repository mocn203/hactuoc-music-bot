const axios = require("axios");

// Patch axios to spoof Vietnamese IP headers for Zing MP3 API
const origCreate = axios.create;
axios.create = function(config) {
  const instance = origCreate.call(axios, config);
  instance.interceptors.request.use(req => {
    req.headers["X-Forwarded-For"] = "14.161.35.12"; // Viettel VN IP
    req.headers["X-Real-IP"] = "14.161.35.12";
    req.headers["Client-IP"] = "14.161.35.12";
    req.headers["CF-Connecting-IP"] = "14.161.35.12";
    req.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";
    return req;
  });
  return instance;
};

const { ZingMp3 } = require("zingmp3-api-full");

function formatDuration(sec) {
  if (!sec) return "0:00";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s < 10 ? "0" : ""}${s}`;
}

async function main() {
  const args = process.argv.slice(2);
  const action = args[0];
  const query = args.slice(1).join(" ");

  try {
    if (action === "search") {
      const res = await ZingMp3.search(query);
      if (!res || !res.data || !res.data.songs || res.data.songs.length === 0) {
        console.log(JSON.stringify({ error: "Không tìm thấy bài hát trên Zing MP3" }));
        return;
      }
      const songs = res.data.songs.slice(0, 5).map(s => ({
        id: s.encodeId,
        title: s.title,
        artists: s.artistsNames || (s.artists ? s.artists.map(a => a.name).join(", ") : "Nhiều nghệ sĩ"),
        thumbnail: s.thumbnailM || s.thumbnail || "",
        duration: s.duration || 0,
        durationStr: formatDuration(s.duration || 0)
      }));
      console.log(JSON.stringify({ success: true, songs }));
    } else if (action === "stream") {
      const songId = query.trim();
      const [streamRes, infoRes] = await Promise.all([
        ZingMp3.getSong(songId).catch(() => null),
        ZingMp3.getInfoSong(songId).catch(() => null)
      ]);

      const streamUrl = streamRes?.data?.["128"] || streamRes?.data?.["320"];
      if (!streamUrl || streamUrl === "VIP") {
        console.log(JSON.stringify({ 
          error: "Bài hát này thuộc diện VIP hoặc không có luồng phát miễn phí",
          rawStream: streamRes,
          rawInfo: infoRes
        }));
        return;
      }

      const info = infoRes?.data || {};
      console.log(JSON.stringify({
        success: true,
        streamUrl,
        id: songId,
        title: info.title || "Không rõ tiêu đề",
        artists: info.artistsNames || "Nhiều nghệ sĩ",
        thumbnail: info.thumbnailM || info.thumbnail || "",
        duration: info.duration || 0,
        durationStr: formatDuration(info.duration || 0)
      }));
    } else if (action === "playlist" || action === "album") {
      let playlistId = query.trim();
      const urlMatch = playlistId.match(/(?:album|playlist)\/(?:[^/]+\/)?([A-Za-z0-9]+)(?:\.html)?/);
      if (urlMatch) {
        playlistId = urlMatch[1];
      }
      const res = await ZingMp3.getDetailPlaylist(playlistId);
      if (!res || res.err !== 0 || !res.data) {
        console.log(JSON.stringify({ error: res?.msg || "Không tìm thấy Album / Playlist trên Zing MP3" }));
        return;
      }
      const data = res.data;
      let rawSongs = data.song?.items || [];
      if (rawSongs.length === 0 && Array.isArray(data.items)) {
        rawSongs = data.items;
      }
      if (rawSongs.length === 0 && Array.isArray(data.sections)) {
        for (const sec of data.sections) {
          if (sec.items && Array.isArray(sec.items)) {
            rawSongs = sec.items;
            break;
          }
        }
      }
      const songs = rawSongs.filter(s => s && s.encodeId).map(s => ({
        id: s.encodeId,
        title: s.title || "Không rõ tiêu đề",
        artists: s.artistsNames || (Array.isArray(s.artists) ? s.artists.map(a => a.name).join(", ") : "Nhiều nghệ sĩ"),
        thumbnail: s.thumbnailM || s.thumbnail || "",
        duration: s.duration || 0,
        durationStr: formatDuration(s.duration || 0)
      }));

      if (songs.length === 0) {
        console.log(JSON.stringify({ error: "Album / Playlist không có bài hát khả dụng" }));
        return;
      }

      console.log(JSON.stringify({
        success: true,
        title: data.title || "Album / Playlist",
        thumbnail: data.thumbnailM || data.thumbnail || (songs[0] ? songs[0].thumbnail : ""),
        total: songs.length,
        songs: songs
      }));
    } else {
      console.log(JSON.stringify({ error: "Unknown action" }));
    }
  } catch (err) {
    console.log(JSON.stringify({ error: err.message }));
  }
}

main();
