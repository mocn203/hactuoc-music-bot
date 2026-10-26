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
        console.log(JSON.stringify({ error: "Bài hát này thuộc diện VIP hoặc không có luồng phát miễn phí" }));
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
    } else {
      console.log(JSON.stringify({ error: "Unknown action" }));
    }
  } catch (err) {
    console.log(JSON.stringify({ error: err.message }));
  }
}

main();
