# Web demonstration · 1.1.0

The real Lecture Hub UI, with reads recorded from the real Python bridge on an invented example semester.
For D-085 this build uses **this worktree’s `src/lecturehub/ui/web`**, because the older export predates the home changes.
The publisher must rebuild from a fresh PUBLIC export. `app.js` and `app.css` come from the supplied web folder; playback is injected before the app starts. No app installation,
accounts, server API, external requests, native dialogs or notifications are needed. Nothing is published by these
scripts. Do not commit `work/`, the recordings, QA screenshots or `dist/`.

## Rebuild (exact Windows commands)

Run from **C:\dev\lh-trial**, on branch **web-trial**. The shared Python executable below is used read-only.
No `uv run`, PowerShell script, real semester or real settings are involved.

```powershell
$trialPython = 'C:\dev\lecture-hub-stable\.venv\Scripts\python.exe'
$publicWeb = 'C:\dev\lh-trial\src\lecturehub\ui\web'  # D-085 QA; publishing: use a fresh PUBLIC export
$env:PYTHONPATH = 'C:\dev\lh-trial\src'
$env:PYTHONDONTWRITEBYTECODE = '1'
git branch --show-current
git status --short

& $trialPython tools/web_trial/prepare.py
$trialScratch = Get-Content work/scratch-root.txt -Raw -Encoding UTF8
$env:LECTUREHUB_SETTINGS = "$trialScratch/settings.json"
$env:LECTUREHUB_RECORDER_CONFIG = "$trialScratch/recorder.toml"
$env:LECTUREHUB_EDITION = 'public'
Remove-Item Env:LECTUREHUB_ROOT -ErrorAction SilentlyContinue
& $trialPython tools/web_trial/record.py work/calls.json $publicWeb
& $trialPython tools/web_trial/record2.py work/calls2.json
& $trialPython tools/web_trial/build.py $publicWeb work
```

`prepare.py` creates a **new** `work/trial-110-*` directory and sets both configuration overrides before running
`python -m lecturehub.index.demo`. It then uses `Library.make_trial()` to point only those scratch settings and
recorder config at the new semester. It seeds enabled AI read/write switches with `save_note` automatic and the
other kinds asking. It never changes a pre-existing semester or the user's configuration.

`safety.py` requires the overrides to be inside this worktree's `work/`, redirects application/home/AI configuration
directories into that scratch directory, disables credential readers and the OS keyring, and rejects credential-file
reads and network connections during recording. Neither recorder runs write actions, native confirmations,
program login checks or startup registry changes. A failed required read aborts recording instead of shipping an
error response. `record2.py` records only optional public-edition reads that are actually available.

To rebuild from the already recorded fixtures, run **only the build command**; it does not import Lecture Hub,
construct a backend, inspect accounts or read the synthetic semester. The source web folder is always read-only to the build. The build
replaces local paths with relative/display-only examples and checks every shipped text asset for the builder's
username, Windows/UNC/user-home paths, email addresses and common key/token/private-key patterns. A leak rejects
the build without printing the match. Generated IDs and synthetic audio are not credentials.

## What the visitor can try

| Screen | Playback |
| --- | --- |
| Home | Three course colours, this week's cancelled class and makeup, week arrows/return, month calendar, today's classes, a deadline within 24 hours and an exam within seven days. Click a course, day or event to open its real screen. |
| Calendar | Browse months, open events, add/edit/cancel an event in this tab, expand **제출함 2건**, read transcript/eTL notice candidates with verbatim quotes, confirm or dismiss a candidate. The synthetic eTL list is available immediately; refresh rereads it without logging in. |
| Course / lecture | Course tree, files, a real synthetic transcript, source time markers and a generated silent 50-minute timeline (not speech). No localhost media server is required. The note is ready immediately. |
| Search | Search the recorded synthetic text (e.g. 유출계수, 도달시간, 단위도, 투수계수, 퀴즈), then jump to the matching lecture/time. |
| 정리본 | Browse the collection, open the checked note, inspect its citations and slide-page references, or replay note making. The real note pipeline validates every prepared paragraph against transcript/slide citations before recording. |
| 확인함 | The app's real empty-state view; calendar candidates are in the calendar, not invented matching holds. |
| Settings, all seven groups | Basic, recording/transcription, AI, connections, consent, extras and about all load. Theme, recording permission/consent, local reminder switches, course mute and stay/startup switches are kept in this tab. |
| AI app link | Toggle read/write, switch each of nine kinds between 묻기/자동, cancel or accept the in-page confirmation, **모두 자동**, **모두 다시 묻기**, simulate **Codex에 연결** or disconnect, and undo one/all-since of four seeded AI changes (calendar, note, makeup and notice-rescan request). The connection dialog explains that the installed app writes Codex's settings file and backs it up. |
| Timetable editor | Open the current timetable and edit its form. **사진으로 시간표 넣기** explains the installed app's file picker, transfer to the user's Codex/OpenAI account, review and apply flow. This trial never picks or sends a file. |
| Recording | The existing 20× synthetic recording/stop/live-text/note-generation playback stays in-browser; it does not open capture devices or call a provider. The PUBLIC edition determines which controls are visible. |

Changes are memory-only except the replayed note-made flag in session storage and the real UI's browser
preferences. **처음부터** resets the trial. A full reload resets simulated calendar and settings changes.

External program/file/help opening, actual AI execution/transcription, downloads/imports, timetable **saving**,
file organization/deletion/recovery, setup changes, diagnostics/update installation, OS settings/exit, and other
non-simulated actions explain in plain words what the installed app does there, through a successful response and an in-page message. They never operate on the PC.
Connection/key inputs only simulate status; do not enter real secrets. Unsupported actions are explicitly listed
in `APP_ONLY_CALLS`; an unexpected method is logged separately as an unanswered call. Any bridge exception or
the app's `client_error` report is separately recorded as a QA failure.

## Date and offline contract

`calls.json.__meta.recorded_at` anchors the browser's `Date` before `app.js` executes. The recorded Korean wall
clock is interpreted in the visitor's local timezone, and recorded ISO timestamps are made timezone-neutral in
the build. Numeric fetch/change timestamps receive the same epoch offset, so relative ages stay aligned too.
Elapsed time advances from that anchor for playback; reopening starts from the recorded date again.
The judge's actual calendar date therefore cannot turn the trial's week, today, countdowns or D-day into stale
data. The week response also carries the recorded date and integer-ms bounds. Seventeen weeks and 25 months
are recorded; more distant weeks extrapolate the regular pattern and omit dated exceptions.

The page CSP sets `connect-src 'none'` and removes the desktop app's file/localhost allowances. Fonts, scripts
and CSS are local; the generated silent audio is a local M4A asset; the note uses a sandboxed srcdoc frame. External links and
`window.open` become in-page explanations. Confirmations use DOM overlays, never `window.confirm` or OS dialogs.

## Headless verification

Install QA dependencies **only into `work/`**, leaving the shared environment untouched. Headless Edge uses a fresh
temporary browser context for each viewport/theme and a loopback static server started/stopped by the runner.

```powershell
& $trialPython -m pip install --target work/qa-deps playwright
& $trialPython tools/web_trial/qa.py
# Same matrix with up to three low-priority headless workers:
& $trialPython tools/web_trial/qa_matrix.py
& $trialPython -m pytest tests/test_demo.py tests/test_settings_tabs.py tests/test_mcp_server.py tests/test_release_window.py tests/test_export_public.py tests/test_public_boundaries.py -q
& $trialPython -m ruff check src tests tools packaging --select E9,F
node --check tools/web_trial/bridge.js
git diff --check
```

`qa.py` visits the requested screens at **1440, 1100, 900, 600 and 390 × 900**, light and dark, with real UI clicks
and assertions for the named mutations. It rejects all requests outside the page's own origin and records console
errors, page errors, bridge failures, unanswered calls and native dialogs. The report is
`work/trial-qa/results.json`. Screenshots are `work/trial-qa/<width>-<theme>-<screen>.png`, with home, calendar,
lecture, settings-ai-controls, settings-ai-card and settings-ai-changes for each combination. Sidebar drawers are
closed after choosing a lecture on narrow screens, as a visitor would do. A single width can be rerun with
`qa.py 390`; `qa.py 1440 --probe` saves boot DOM/calls for diagnosing new read methods.
The 1100px pair deliberately runs with a **2035-05-06 browser clock in America/Los_Angeles**; the other pairs use
Asia/Seoul. Every run asserts that the page still considers the recorded date to be today.

Verified 2026-11-26 after the D-085 rebuild: **298 recorded read responses / 46 read methods**, **195 API names** audited, **10 viewport/theme contexts**, **4,989 bridge calls**, and **190 current screenshots**. All console/page errors, unanswered calls, bridge/client failures, external requests, native dialogs, unexpected error answers, visible wording violations and overflowing text/element rectangles were **0**. Built-file byte scanning also found **0** retired-word occurrences. The built page is **3,271,972 bytes (3.12 MiB)**. The requested pytest scope was **127 passed in 59.16s**; after the final candidate-time label change the UI scope was **77 passed in 18.68s**. Ruff (E9,F), both JavaScript syntax checks and `git diff --check` passed. No full suite, actual capture, account request, push or publication was performed.

D-085 QA results are recorded in `work/trial-qa/results.json`. Run the complete matrix after each rebuild.
The runner compares text-bearing child rectangles and actual text ranges with parent rectangles in the home week, home month,
to-do and calendar areas, allowing normal CSS clipping/scrolling, and rejects retired visible wording.
The build separately rejects that wording in every shipped text file. It also exercises `?bare=1` in each context.
The parallel runner writes separate `results-<width>.json` and `qa-<width>.log` files, then combines only that
run's ten contexts into `results.json`. No other test suite or browser profile is used.

The semester contains only 수문학, 토질역학 and 관개배수공학. A 12-week history includes three classes without
recordings. All other recordings use a compact, locally generated silent 50-minute M4A stream, with measured
duration and codec metadata; no voice or real classroom recording is included. Three full transcripts and two
checked notes cover rainfall–runoff, the rational method, unit hydrographs and soil permeability. PDF and DOCX
material text is extracted with the normal material extractor. Four assignments include two submitted ones,
one due tonight, and one due next week. Candidate quotes use the same transcript segment/time or eTL notice
in the banner and calendar. The moved Friday is computed from the week of the next Wednesday quiz.

The top strip says that this is an example semester made to show the app. The default strip is small;
append **`?bare=1`** when recording the contest video. Unsupported actions describe the installed app's behavior,
never an error. The optional group study room switch is off; the page does not navigate to its screens.
Connection-presence states are explicit local scenario fixtures: the recorder never reads a credential or runs
a logged-in CLI. Free amounts still come from the app's real quota/usage calculations on the scratch semester.

New semester directories are called `예시 학기`. Existing directories and saved data_root settings are not moved
or rewritten. API names (`make_trial`, `trial_start`), `settings.trial`, tool paths and CSS identifiers stay compatible.
The builder also aliases an old example directory basename in recorded layout previews, so it cannot leak into
the page. No new dependency, schema change or actual capture is required.

No full test suite, native capture,
real AI/Canvas request, push or GitHub Pages publication is part of this workflow.
