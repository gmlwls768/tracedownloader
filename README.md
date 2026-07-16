# TraceDownloader

A queue-based batch downloader built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and [gallery-dl](https://github.com/mikf/gallery-dl). Paste a URL and it
figures out on its own whether yt-dlp or gallery-dl should handle it — no
per-site configuration required.

Two front ends share the same engine:
- **Windows version**: a native desktop app (tkinter) — double-click the
  `.exe`, no browser or server involved.
- **Web server version**: a small local web server (FastAPI), for running
  headless on a Linux home server/NAS and controlling it from any device's
  browser.

![state](https://img.shields.io/badge/status-active-brightgreen)
![version](https://img.shields.io/badge/version-1.0.0-blue)

## Features

- **Automatic routing** — every URL is tried against yt-dlp first, then
  gallery-dl, so channels, playlists, single videos, and image galleries
  all just work without picking a mode.
- **Two download modes, your choice per URL pattern** — anything matching
  a pattern you configure (e.g. `youtube.com`) is tracked persistently:
  resumable, deduplicated against what's already downloaded, and
  re-checkable for newly uploaded items (e.g. point it at a whole channel,
  then re-check later to pull only what's new). Everything else downloads
  once and only shows up in the current session's list (nothing written to
  the database). A lone `*` tracks everything, and a `!` prefix excludes:
  `*` plus `!youtube.com` tracks everything except YouTube.
- **Channels, playlists, and gallery artists as groups** — a YouTube
  channel, a playlist, or an image-site artist page each becomes one
  tracked group; re-checking it downloads only what was added since.
- **Scheduled re-checks** — optionally re-check every completed group for
  new uploads on a fixed interval (e.g. every 3 days), and exclude
  individual groups from bulk/scheduled re-checks.
- **Live progress**, pause/resume, priority downloads, drag-to-reorder,
  search.
- **Maintenance tools** — bulk re-check for new uploads, retry
  errored/skipped items, redownload anything below a resolution or file
  size threshold, find files the database thinks exist but don't.
- **Cookies** — paste a cookies.txt once and it's used by both tools, for
  any site that needs a login.
- **yt-dlp/gallery-dl stay up to date on their own** — both change often
  enough (site breakage, new extractors) that a copy downloaded once and
  never touched again goes stale fast. A background check refreshes them
  automatically (can be turned off), plus a "check now" button in Settings.
- **English and Korean UI**, switchable from Settings.

## Quick start

### Windows

1. Download the latest `tracedownloader.exe` from
   [Releases](../../releases) and run it.
2. On first run it downloads yt-dlp, gallery-dl, and ffmpeg into a `bin/`
   folder next to the .exe. Everything else (the database, downloaded
   files) also lives next to the .exe, so the whole thing is portable —
   move the folder anywhere and it keeps working.

No browser, no port, nothing to open — it's a normal desktop window.

### Web server (Linux)

```bash
git clone <this repo>
cd tracedownloader
bash deploy/install.sh
```

The script sets up a venv, downloads yt-dlp/gallery-dl/ffmpeg into `bin/`,
and prints the command to start the web server. To run it as a systemd
service instead of manually, see `deploy/app.service.example`.

### From source

```bash
# Windows desktop app
python app.py

# Web server (Linux/Mac)
python3 -m venv venv && venv/bin/pip install -r requirements.txt
APP_HOME=./data venv/bin/uvicorn server:app --host 127.0.0.1 --port 8686
```

yt-dlp and gallery-dl need to be reachable — either drop them in a `bin/`
folder next to `engine/`, or have them on your `PATH`.

## Configuration

| Setting | What it does |
|---|---|
| Concurrent downloads | How many videos download at once |
| Output folder | Where files are saved (default: `./download`) |
| Scheduled re-check interval | Every N days, automatically re-check all completed groups for new uploads (0 = off); individual groups can opt out via the right-click menu |
| Track permanently (patterns) | One substring per line; URLs matching one are tracked in the database (resumable/deduplicated/re-checkable), everything else is session-only. `*` alone tracks everything; a `!` prefix excludes (e.g. `!youtube.com`) |
| Per-site output folders | One `pattern => subfolder` per line; a matching URL downloads under Output folder/subfolder instead |
| Gallery folder name format | Template for gallery folder names, e.g. `[{artist}] {title} ({id})` |
| Cookies | A cookies.txt (Netscape format), sent to both yt-dlp and gallery-dl |
| Resolution / size thresholds | Used by the "check resolution" / "check size" maintenance tools |
| Keep tools updated automatically | Periodic yt-dlp/gallery-dl auto-update, plus a manual "check now" |
| Language | English or Korean |

## Project layout

```
engine/          task queue, SQLite-backed state, all yt-dlp/gallery-dl calls,
                 auto-update logic - the shared core, no UI code
  __init__.py      Engine class assembly, init/load-save/shutdown
  models.py        DB, Task, module-level constants/helpers, M()
  ephemeral.py     session-only (non-persistent) video/gallery download
  resolve.py       URL intake + persistent-group playlist resolve
  workers.py       download queue/workers, start-stop-reorder actions
  maintenance.py   done-tab tools, delete, apply_action() dispatch
  updater.py       background yt-dlp/gallery-dl self-update
  settings.py      settings get/set, cookies, state snapshot
i18n.py          English/Korean text shared by both front ends
app.py           Windows desktop app (tkinter) - imports engine directly,
                 no server involved
server.py        web server version: FastAPI REST + Server-Sent Events on
                 top of engine
static/index.html web UI served by server.py (no build step)
client/          optional helper for the web server version: clipboard
                 watching + "open on this PC" (only useful if the server
                 runs on a different machine than the one you browse from)
deploy/          web server (Linux) install script, systemd unit template,
                 Windows PyInstaller build script
```

## Development

```bash
# Windows app
python app.py

# Web server, with auto-reload
venv/bin/uvicorn server:app --reload --host 127.0.0.1 --port 8686
```

`engine/` has no UI dependency either way and can be exercised directly
if you want to script against it.

## License

MIT — see [LICENSE](LICENSE). This project drives yt-dlp and gallery-dl as
external command-line tools (never imported as libraries); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for their licenses.

---

# TraceDownloader (한국어)

[yt-dlp](https://github.com/yt-dlp/yt-dlp)와
[gallery-dl](https://github.com/mikf/gallery-dl) 기반의 큐 방식 대량
다운로더입니다. URL만 붙여넣으면 yt-dlp와 gallery-dl 중 어느 쪽으로
처리할지 자동으로 판단합니다 — 사이트별 설정이 필요 없습니다.

두 프론트엔드가 같은 엔진을 공유합니다:
- **Windows 버전**: 네이티브 데스크톱 앱(tkinter) — exe 더블클릭,
  브라우저나 서버 없음.
- **웹서버 버전**: 가벼운 로컬 웹서버(FastAPI) — 리눅스 홈서버/NAS에
  headless로 띄워두고 아무 기기 브라우저로 제어.

## 주요 기능

- **자동 판별** — 모든 URL을 yt-dlp로 먼저 시도하고, 안 되면 gallery-dl로
  시도합니다. 채널/재생목록/단일 영상/이미지 갤러리 전부 모드 선택 없이
  그냥 동작합니다.
- **URL 패턴별 두 가지 다운로드 방식** — 설정에서 지정한 패턴(예:
  `youtube.com`)에 걸리는 URL은 영구 추적됩니다: 이어받기, 중복 방지,
  새 영상 재확인이 모두 됩니다(채널 전체를 넣고, 나중에 재확인하면 새로
  올라온 것만 받습니다). 그 외 URL은 한 번만 받고 이번 세션 목록에만
  표시되며 DB에는 전혀 기록되지 않습니다. `*` 한 줄이면 전체 추적,
  `!` 접두어는 제외 — `*` + `!youtube.com`이면 유튜브만 빼고 전부 추적.
- **채널·재생목록·갤러리 아티스트를 그룹으로 추적** — 유튜브 채널,
  재생목록, 이미지 사이트의 아티스트 페이지가 각각 하나의 그룹이 되고,
  재확인하면 그 뒤에 추가된 것만 다운로드됩니다.
- **예약 재확인** — 완료된 그룹 전체를 정해진 주기(예: 3일)마다 자동으로
  재확인해 새 업로드를 받아옵니다. 특정 그룹만 전체/예약 재확인에서
  제외할 수도 있습니다.
- **실시간 진행률**, 일시정지/재개, 최우선 다운로드, 드래그 순서변경, 검색.
- **유지보수 도구** — 완료 그룹 일괄 재확인, 오류/건너뜀 재시도, 해상도·
  용량 기준 미달 재다운로드, DB엔 있는데 실제로는 없는 파일 찾기.
- **쿠키** — cookies.txt를 한 번 붙여넣으면 두 도구 모두에 전달되어,
  로그인이 필요한 사이트도 지원합니다.
- **yt-dlp/gallery-dl 자동 업데이트** — 둘 다 사이트 변경에 따라 자주
  업데이트가 필요한 도구라, 한 번 받고 방치하면 금방 낡습니다. 백그라운드
  자동 업데이트(끌 수 있음) + 설정의 "지금 확인" 버튼으로 즉시 갱신.
- **영어/한국어 UI**, 설정에서 전환 가능.

## 빠른 시작

### Windows

1. [Releases](../../releases)에서 최신 `tracedownloader.exe`를 받아
   실행합니다.
2. 처음 실행할 때 yt-dlp/gallery-dl/ffmpeg를 exe 옆 `bin/` 폴더에 자동으로
   받습니다. DB와 다운로드 파일도 전부 exe 옆에 저장되므로 폴더째로
   옮겨도 그대로 동작합니다(포터블).

브라우저나 포트 없이, 그냥 평범한 데스크톱 창입니다.

### 웹서버 버전 (Linux)

```bash
git clone <this repo>
cd tracedownloader
bash deploy/install.sh
```

venv 생성, yt-dlp/gallery-dl/ffmpeg를 `bin/`에 다운로드까지 스크립트가
처리하고, 웹서버 실행 명령을 출력해줍니다. 수동 실행 대신 systemd
서비스로 등록하려면 `deploy/app.service.example`을 참고하세요.

## 설정

| 설정 | 설명 |
|---|---|
| 동시 다운로드 수 | 동시에 받는 영상 개수 |
| 출력 폴더 | 저장 위치 (기본값: `./download`) |
| 예약 재확인 주기 | N일마다 완료 그룹 전체를 자동 재확인 (0=끄기). 우클릭 메뉴로 그룹별 제외 가능 |
| 영구 추적 패턴 | 한 줄에 하나씩. 이 패턴에 걸리는 URL만 DB에 영구 저장(이어받기/중복방지/재확인), 나머지는 세션 한정. `*` 한 줄이면 전체, `!` 접두어는 제외 (예: `!youtube.com`) |
| 사이트별 출력 폴더 | 한 줄에 `패턴 => 하위폴더` 형식. 패턴에 걸리는 URL은 출력 폴더/하위폴더 밑에 저장 |
| 갤러리 폴더명 형식 | 예: `[{artist}] {title} ({id})` |
| 쿠키 | cookies.txt(Netscape 형식), yt-dlp·gallery-dl 양쪽에 전달 |
| 해상도/용량 기준 | "해상도 검사"/"용량 검사" 도구가 사용하는 기준값 |
| 자동 업데이트 | yt-dlp/gallery-dl 주기적 자동 업데이트 + 수동 "지금 확인" |
| 언어 | 영어 또는 한국어 |

## 라이선스

MIT — [LICENSE](LICENSE) 참고. yt-dlp와 gallery-dl은 외부 실행파일로
호출만 할 뿐 라이브러리로 가져오지 않습니다 — 각 도구의 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하세요.
