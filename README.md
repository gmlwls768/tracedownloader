# yt-dlp & gallery-dl GUI

A local web UI for queuing up bulk downloads with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and [gallery-dl](https://github.com/mikf/gallery-dl). Paste a URL and it
figures out on its own whether yt-dlp or gallery-dl should handle it — no
per-site configuration required.

![state](https://img.shields.io/badge/status-active-brightgreen)

## Features

- **Automatic routing** — every URL is tried against yt-dlp first, then
  gallery-dl, so channels, playlists, single videos, and image galleries
  all just work without picking a mode.
- **Two download modes, your choice per URL pattern** — anything matching
  a pattern you configure (e.g. `youtube.com`) is tracked persistently:
  resumable, deduplicated against what's already downloaded, and
  re-checkable for newly uploaded items. Everything else downloads once
  and only shows up in the current session's list (nothing written to the
  database).
- **Live progress over SSE** — queue, pause/resume, priority downloads,
  drag-to-reorder, search, all updating in real time without polling.
- **Maintenance tools** — bulk re-check for new uploads, retry
  errored/skipped items, redownload anything below a resolution or file
  size threshold, find files the database thinks exist but don't.
- **Cookies** — paste a cookies.txt once and it's used by both tools, for
  any site that needs a login.
- **English and Korean UI**, switchable from Settings.
- Runs as a small local web server: a single Windows `.exe`, or a plain
  Python process on Linux (systemd unit optional).

## Quick start

### Windows

1. Download the latest `ytdlp-gallery-dl-gui.exe` from
   [Releases](../../releases).
2. Run it. A console window opens, and your browser opens automatically to
   `http://127.0.0.1:8686`.
3. On first run it downloads yt-dlp, gallery-dl, and ffmpeg into a `bin/`
   folder next to the .exe. Everything else (the database, downloaded
   files) also lives next to the .exe, so the whole thing is portable —
   move the folder anywhere and it keeps working.

### Linux

```bash
git clone <this repo>
cd ytdlp-gallery-dl-gui
bash deploy/install.sh
```

The script sets up a venv, downloads yt-dlp/gallery-dl/ffmpeg into `bin/`,
and prints the command to start it. To run it as a systemd service instead
of manually, see `deploy/app.service.example`.

### From source (either OS)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt     # Linux/Mac
# venv\Scripts\pip install -r requirements.txt   # Windows

APP_HOME=./data venv/bin/uvicorn server:app --host 127.0.0.1 --port 8686
```

yt-dlp and gallery-dl need to be reachable — either drop them in a `bin/`
folder next to `engine.py`, or have them on your `PATH`.

## Configuration

Everything is in the ⚙ Settings screen in the web UI:

| Setting | What it does |
|---|---|
| Concurrent downloads | How many videos download at once |
| Output folder | Where files are saved (default: `./download`) |
| Track permanently (patterns) | One substring per line; URLs matching one are tracked in the database (resumable/deduplicated/re-checkable), everything else is session-only |
| Gallery folder name format | Template for gallery folder names, e.g. `[{artist}] {title} ({id})` |
| Cookies | A cookies.txt (Netscape format), sent to both yt-dlp and gallery-dl |
| Resolution / size thresholds | Used by the "check resolution" / "check size" maintenance tools |
| Language | English or Korean |

## Project layout

```
engine.py        task queue, SQLite-backed state, all yt-dlp/gallery-dl calls
server.py        FastAPI app: REST + Server-Sent Events on top of engine.py
static/index.html single-file web UI (no build step)
app.py           desktop launcher used by the packaged Windows .exe
client/          optional helper: clipboard watching + "open on this PC"
                 (only useful if the server runs on a different machine
                 than the one you browse from)
deploy/          Linux install script, systemd unit template,
                 Windows PyInstaller build script
```

## Development

```bash
venv/bin/uvicorn server:app --reload --host 127.0.0.1 --port 8686
```

`engine.py` has no web-framework dependency and can be exercised directly
if you want to script against it.

## License

MIT — see [LICENSE](LICENSE). This project drives yt-dlp and gallery-dl as
external command-line tools (never imported as libraries); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for their licenses.

---

# yt-dlp & gallery-dl GUI (한국어)

[yt-dlp](https://github.com/yt-dlp/yt-dlp)와
[gallery-dl](https://github.com/mikf/gallery-dl)로 대량 다운로드를 큐에
쌓아 처리하는 로컬 웹 UI입니다. URL만 붙여넣으면 yt-dlp와 gallery-dl 중
어느 쪽으로 처리할지 자동으로 판단합니다 — 사이트별 설정이 필요 없습니다.

## 주요 기능

- **자동 판별** — 모든 URL을 yt-dlp로 먼저 시도하고, 안 되면 gallery-dl로
  시도합니다. 채널/재생목록/단일 영상/이미지 갤러리 전부 모드 선택 없이
  그냥 동작합니다.
- **URL 패턴별 두 가지 다운로드 방식** — 설정에서 지정한 패턴(예:
  `youtube.com`)에 걸리는 URL은 영구 추적됩니다: 이어받기, 중복 방지,
  새 영상 재확인이 모두 됩니다. 그 외 URL은 한 번만 받고 이번 세션
  목록에만 표시되며 DB에는 전혀 기록되지 않습니다.
- **SSE 기반 실시간 진행률** — 큐, 일시정지/재개, 최우선 다운로드,
  드래그 순서변경, 검색이 폴링 없이 실시간으로 갱신됩니다.
- **유지보수 도구** — 완료 그룹 일괄 재확인, 오류/건너뜀 재시도, 해상도·
  용량 기준 미달 재다운로드, DB엔 있는데 실제로는 없는 파일 찾기.
- **쿠키** — cookies.txt를 한 번 붙여넣으면 두 도구 모두에 전달되어,
  로그인이 필요한 사이트도 지원합니다.
- **영어/한국어 UI**, 설정에서 전환 가능.
- 가벼운 로컬 웹서버로 동작: Windows에서는 단일 `.exe`, 리눅스에서는
  일반 파이썬 프로세스(systemd 서비스 등록은 선택 사항)로 실행됩니다.

## 빠른 시작

### Windows

1. [Releases](../../releases)에서 최신 `ytdlp-gallery-dl-gui.exe`를 받습니다.
2. 실행합니다. 콘솔 창이 뜨고 브라우저가 자동으로
   `http://127.0.0.1:8686`을 엽니다.
3. 처음 실행할 때 yt-dlp/gallery-dl/ffmpeg를 exe 옆 `bin/` 폴더에 자동으로
   받습니다. DB와 다운로드 파일도 전부 exe 옆에 저장되므로 폴더째로
   옮겨도 그대로 동작합니다(포터블).

### Linux

```bash
git clone <this repo>
cd ytdlp-gallery-dl-gui
bash deploy/install.sh
```

venv 생성, yt-dlp/gallery-dl/ffmpeg를 `bin/`에 다운로드까지 스크립트가
처리하고, 실행 명령을 출력해줍니다. 수동 실행 대신 systemd 서비스로
등록하려면 `deploy/app.service.example`을 참고하세요.

## 설정

웹 UI ⚙ 설정 화면에서 전부 조정합니다:

| 설정 | 설명 |
|---|---|
| 동시 다운로드 수 | 동시에 받는 영상 개수 |
| 출력 폴더 | 저장 위치 (기본값: `./download`) |
| 영구 추적 패턴 | 한 줄에 하나씩. 이 패턴에 걸리는 URL만 DB에 영구 저장(이어받기/중복방지/재확인), 나머지는 세션 한정 |
| 갤러리 폴더명 형식 | 예: `[{artist}] {title} ({id})` |
| 쿠키 | cookies.txt(Netscape 형식), yt-dlp·gallery-dl 양쪽에 전달 |
| 해상도/용량 기준 | "해상도 검사"/"용량 검사" 도구가 사용하는 기준값 |
| 언어 | 영어 또는 한국어 |

## 라이선스

MIT — [LICENSE](LICENSE) 참고. yt-dlp와 gallery-dl은 외부 실행파일로
호출만 할 뿐 라이브러리로 가져오지 않습니다 — 각 도구의 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하세요.
