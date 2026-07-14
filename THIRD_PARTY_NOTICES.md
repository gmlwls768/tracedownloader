# Third-party notices

This project's own code is MIT-licensed (see [LICENSE](LICENSE)). It relies
on the following external tools, each under its own license. None of them
are imported as libraries — they're invoked as separate command-line
processes (via `subprocess`), so this project's license is unaffected by
theirs.

## yt-dlp

- <https://github.com/yt-dlp/yt-dlp>
- License: [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE) (public domain)
- Used to download video/audio and resolve playlists/channels.

## gallery-dl

- <https://github.com/mikf/gallery-dl>
- License: [GPL-2.0](https://github.com/mikf/gallery-dl/blob/master/LICENSE)
- Used to download image galleries.

## ffmpeg / ffprobe

- <https://ffmpeg.org/>
- License: LGPL or GPL depending on the build (the Windows build this
  project auto-downloads, from <https://www.gyan.dev/ffmpeg/builds/>,
  includes GPL-licensed encoders).
- Used to merge audio/video streams and probe resolution during the
  resolution-check maintenance tool. Not bundled in this repository or in
  the packaged executable — both the Linux install script and the Windows
  app download it directly from the links above the first time they run.

## Windows builds only

The packaged Windows `.exe` also downloads prebuilt binaries of the three
tools above on first run (see `app.py`); the same licenses apply to those
binaries as listed here.
