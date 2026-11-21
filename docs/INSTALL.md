# 설치

Windows 10(1809 이상) 또는 Windows 11, 64비트가 필요합니다.

## 1. Microsoft Store에서 설치 (권장)

1. https://apps.microsoft.com/detail/9NSZ9V0H0FBR 를 열거나, Microsoft Store 앱에서 "Lecture Hub"를 찾습니다.
2. **받기**(또는 **설치**)를 누릅니다. 경고 창 없이 설치됩니다. Store판은 Microsoft가 서명합니다.
3. 시작 메뉴의 **Lecture Hub**로 엽니다.

새 버전은 Store가 받으므로 앱 안의 새 버전 알림은 꺼져 있습니다. Store판을 제거하면 앱 설정과 앱 기록도 함께 지워지지만, 학기 폴더(녹음·전사·노트)는 그대로 남습니다.
Store판과 아래 설치 프로그램판을 함께 설치할 필요는 없습니다. 하나만 쓰세요.

## 1-1. 설치 프로그램으로 설치 (Store를 쓸 수 없을 때)

1. 릴리스 페이지에서 `LectureHub-<버전>-win64-setup.exe`를 받습니다.
2. 받은 파일을 실행합니다. 관리자 권한은 필요 없고, 내 계정 폴더(`%LOCALAPPDATA%\Programs\LectureHub`)에 설치됩니다.
3. 시작 메뉴의 **Lecture Hub**로 엽니다.

ffmpeg는 앱에 들어 있습니다. Python을 따로 설치할 필요가 없습니다.

### "Windows의 PC 보호" 창이 뜰 때

이 앱은 아직 코드 서명이 없어서, 처음 실행할 때 Windows가 파란 경고 창("알 수 없는 게시자")을 띄웁니다.

- **추가 정보**를 누른 뒤 **실행**을 누르면 설치가 계속됩니다.
- 받은 파일이 진짜인지는 해시로 확인할 수 있습니다. PowerShell에서 아래 명령을 실행하고, 결과를 릴리스 페이지의 `SHA256SUMS.txt`와 비교합니다.

```
Get-FileHash .\LectureHub-<버전>-win64-setup.exe -Algorithm SHA256
```

값이 다르면 설치하지 마세요.

### 설치 없이 쓰기 (zip)

`LectureHub-<버전>-win64.zip`을 원하는 폴더에 풀고 `LectureHub.exe`를 실행합니다. 시작 메뉴 등록과 제거 프로그램은 없습니다.

### 화면이 안 뜨고 WebView2 안내가 나올 때

화면은 Windows의 Microsoft Edge WebView2 Runtime으로 그립니다. Windows 11에는 들어 있고 Windows 10에도 대부분 있습니다. 없으면 앱이 안내 창을 띄웁니다. [WebView2 내려받기](https://developer.microsoft.com/microsoft-edge/webview2/)에서 "Evergreen Bootstrapper"를 받아 설치한 뒤 다시 실행합니다.

## 2. 처음 켰을 때

마법사가 열립니다. 두 가지 중 하나를 고릅니다.

- **체험 학기로 둘러보기**: 합성 강의 하나로 녹음 → 전사 → 정리본 흐름을 봅니다. 내 자료나 키가 없어도 되고, 아무것도 밖으로 보내지 않습니다.
- **내 학기 설정**: 학기 폴더를 고르고 시간표를 적습니다. 수업 시간을 넣지 않은 과목은 '수업 외'로 들어가고, 시간은 나중에 설정의 시간표에서 넣을 수 있습니다.

학기 폴더를 고를 때 아래 같은 곳이면 마법사가 알려 줍니다. 안내만 하고 폴더를 거절하지는 않습니다.

- 이름으로 알아보는 클라우드 동기화 폴더: OneDrive, Dropbox, Google Drive(Google Drive for desktop의 "My Drive"·"내 드라이브"·공유 드라이브 포함), iCloud, MYBOX. 녹음하는 동안 큰 파일이 계속 올라가고 동기화 프로그램이 수업 목록 파일을 잠가 오류가 날 수 있어, 동기화되지 않는 폴더를 권합니다.
- USB·네트워크 드라이브, 경로가 150자를 넘는 폴더. USB는 뽑히면 녹음이 끊깁니다. 이런 드라이브에는 휴지통이 없어서, 앱은 녹음이 끝나 MP3를 확인한 뒤 앱이 만든 WAV 사본만 바로 지웁니다. 그 밖의 삭제(녹음 버리기, 오래된 소리 정리 등)는 하지 않고 파일을 그대로 둡니다.

그다음 **설정**에서 키를 연결합니다.

- Gemini 키: Google AI Studio에서 만듭니다. 연결 버튼을 누르면 키가 맞는지 확인한 뒤 저장합니다.
- Groq 키: Groq 콘솔에서 만듭니다.
- eTL 토큰: eTL 계정 설정의 "새 액세스 토큰"에서 만듭니다. 다른 학교 Canvas를 쓰면 먼저 설정 → 계정 연결의 [다른 학교 Canvas]에 학교 Canvas 주소(https)를 넣고, 그 학교 Canvas의 계정 → 설정에서 토큰을 만듭니다. 토큰은 넣을 때의 주소에만 보내고, 주소를 바꾸면 다시 넣어야 합니다.

키는 이 PC의 Windows 자격 증명 관리자에만 저장되고, 앱 화면이나 파일로 다시 나오지 않습니다.

설정의 **AI 엔진**에서 기본값과 정리본·그스룸 지시 해석별 값을 고릅니다(처음은 Codex).
프로그램 발견과 로그인은 별개이며, [프로그램 다시 찾기]에서 Codex 로그인 상태를 확인할 수 있습니다.
CLI 상태로 Edu 작업 공간인지 판별할 수는 없습니다. 제공자 동의창의 설명을 읽고 개인정보를 뺀 자료만 보내세요.
과목별 **녹음 허락**도 따로 표시하세요. 금지는 녹음·자막을 막고, 미확인은 시작 때 경고합니다.

## 3. 선택 프로그램 (없어도 녹음과 전사는 됩니다)

| 프로그램 | 쓰임 |
|---|---|
| Codex CLI (ChatGPT 로그인) | 기본 정리본 엔진·HTML 도우미. Edu 여부는 계정에서 직접 확인 |
| Claude Code | 설정에서 선택할 수 있는 호환 엔진·그스룸 브라우저 대체 경로 |
| Google Cloud CLI | 실시간 자막(선택 기능) |
| Aside | Clova 가져오기·그스룸 예약. 그스룸만 Claude in Chrome 대체 경로를 제공 |

Codex는 Node.js 설치 후 `npm install -g @openai/codex`, `codex login`으로 본인의 ChatGPT 계정에 로그인합니다.
학교 Edu를 사용할 때는 해당 계정·작업 공간의 Codex 사용 권한을 확인하세요. Windows 앱은 `codex.cmd`를 찾습니다.

앱의 **설정 → 프로그램**에서 무엇이 있고 없는지, 설치 안내 링크가 어디인지 볼 수 있습니다.

## 4. 새 버전으로 바꾸기

새 설치 프로그램을 그대로 실행하면 됩니다. 앱이 열려 있으면 닫으라는 안내가 나옵니다. 학기 폴더, 설정, 키는 그대로 남습니다.

수업 목록(데이터베이스) 형식이 바뀐 버전은 처음 열 때 백업을 만든 뒤 스스로 올립니다. 백업은 학기 폴더의 `_lecturehub\backup`에 있습니다. 예전 버전으로 되돌리면 새 형식의 수업 목록은 열리지 않습니다(녹음은 됩니다).

앱은 하루 한 번 새 버전이 있는지 확인해 알려 줍니다. 내려받기와 설치는 직접 합니다. 설정 맨 아래 **앱 정보**에서 끌 수 있습니다.

## 5. 지우기

Windows **설정 → 앱 → 설치된 앱**에서 Lecture Hub를 제거합니다. 프로그램만 지워지고 아래는 남습니다. 필요 없으면 직접 지우세요.

| 무엇 | 어디 |
|---|---|
| 녹음, 전사, 정리본, 수업 목록 | 내가 고른 학기 폴더 (`_lecturehub` 포함) |
| 설정 | `%APPDATA%\LectureHub` |
| 녹음기 설정 | `%APPDATA%\lecturehub\recorder.toml` |
| 앱 기록, 문제 보고 파일, AI 도우미 작업 폴더 | `%LOCALAPPDATA%\LectureHub` |
| 저장한 키 | Windows 자격 증명 관리자의 `lecturehub` 항목 (앱 설정의 [연결 끊기]로도 지울 수 있음) |

## 6. 문제가 생겼을 때

설정 맨 아래 **앱 정보 · 문제 보고**에서 [문제 보고 파일 만들기]를 누르면, 들어갈 내용을 먼저 보여 준 뒤 zip 파일 하나를 만듭니다. 이 파일을 개발자에게 직접 보내 주세요. 앱이 자동으로 보내는 것은 없습니다. 녹음, 전사, 노트, 키는 들어가지 않고, 경로 속의 사용자 이름과 과목·파일 이름은 가려집니다.

앱이 아예 열리지 않으면 `%LOCALAPPDATA%\LectureHub\logs` 폴더의 파일을 보내 주세요.

## 7. 소스에서 실행하기 (개발자)

Python 3.12와 ffmpeg(`winget install Gyan.FFmpeg`)가 필요합니다.

```
git clone <이 저장소 주소> lecture-hub
cd lecture-hub
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[recorder,transcribe,materials,ui]"
.venv\Scripts\python.exe -m lecturehub.ui.app
```

릴리스와 같은 버전의 패키지로 맞추려면 `python -m pip install --require-hashes -r requirements/release.txt` 뒤에 `python -m pip install --no-deps -e .`를 실행합니다. 설치 파일은 `python tools/build_app.py --installer`로 만듭니다(`tools/build_app.py` 맨 위 설명 참고).
