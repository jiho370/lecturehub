# Lecture Hub

수업을 녹음하고, 글로 바꾸고, 슬라이드와 이어서 **근거가 달린 강의 정리본**을 만드는 Windows 데스크톱 앱입니다.
서울대학교 학생이 자기 학기를 위해 만들었고, 다른 학생도 쓸 수 있게 다듬었습니다. (English: a Windows desktop app that records
lectures, transcribes them with several engines, links the slides and builds study notes where every claim points back to a
recording timestamp or slide page.)

## 무엇을 하나요

| 하는 일 | 어떻게 |
|---|---|
| 녹음 | 마이크와 PC 소리를 동시에. 앱이 꺼져도 녹음은 계속되고, 끊기면 복구합니다. 소리가 끊기면 장치를 다시 열어 이어 쓰고, 끊긴 구간은 기록에 남깁니다. 끝나면 확인한 MP3가 녹음 본체입니다. 기본으로는 무손실 사본을 남기지 않고, 앱이 만든 WAV 사본은 MP3를 확인한 뒤 정리합니다. 무손실 FLAC 사본을 남기는 것은 선택 사항(`settings.json`의 `keep_lossless`)입니다. |
| 글로 바꾸기 | Gemini, Groq Whisper 등 여러 엔진을 나란히 돌리고, 엔진끼리 어긋나는 곳을 표시합니다. 보낼 때마다 **과목별 동의**를 확인합니다. |
| 강의 정리본 | 기본 엔진 Codex가 전사·자료 글로 노트를 만듭니다. 앱이 인용 ID·구간·자료 쪽을 확인하고, 근거가 없는 본문 블록은 버립니다. 별도 HTML 도우미도 열 수 있습니다. |
| 평가 준비 | 과목별 평가 일정, 수업 중 강조한 내용, 대비 자료 모음. |
| 홈 화면 | 이번 주 시간표(휴강·보강 반영)와 월 달력, 오늘 할 일을 한 화면에서 봅니다. |
| 놓치지 않게 | 앱이 켜져 있는 동안(창을 닫아 알림 영역에 남겨 둔 때 포함) 미제출 과제 마감(24시간·3시간 전)과 평가·퀴즈(전날 21시·1시간 전, 시각 미정이면 당일 8시)를 알려 줍니다. 오전 7시 이후 하루 한 번 오늘 일정을 알립니다. Windows를 켤 때 함께 켜지게 할 수 있습니다. 알림은 종류별·과목별로 끄거나 한꺼번에 고를 수 있고, 계산과 발송 기록은 이 PC 안에만 있습니다. |
| 학교 연동 | eTL(Canvas) 공지·과제·자료를 내 토큰으로 읽기만 합니다. 다른 학교 Canvas도 주소를 넣어 연결합니다. |
| 수업 중 공지 | 전사가 바뀐 수업을 ChatGPT(이 PC의 Codex CLI, 로그인한 ChatGPT 계정)로 읽어 휴강·시험·과제 마감 공지를 찾고, 달력에 **후보**로 넣습니다. 보낼 과목은 하나씩 허용하고, 설정에서 Google Gemini로 바꾸거나 끌 수 있습니다. 녹음 중에는 시작하지 않습니다. |
| AI 앱 연결 | 쓰던 AI 앱(ChatGPT 데스크톱·Codex, Claude 데스크톱, Claude Code)이 내가 허용한 과목의 전사·정리본·강의자료 글·일정·공지를 물어볼 수 있습니다. Codex는 버튼 한 번으로 연결합니다. 'AI 앱이 고칠 수 있게'를 따로 켜면 AI 앱에 말해서 일정을 넣거나 정리본을 새 판으로 저장할 수 있고, 바뀐 것은 앱의 'AI가 바꾼 것'에서 되돌립니다. 기본은 꺼져 있고, Lecture Hub와 AI 앱은 이 PC 안에서 주고받으며(중간에 인터넷 서버 없음), 소리 파일은 건네지 않습니다. AI 앱이 받은 글을 어떻게 다루는지는 그 앱의 약관을 따릅니다. 로컬(표준 입출력) 방식의 MCP 서버를 지원하는 AI 앱이면 쓸 수 있을 것으로 보지만, 확인한 것은 Codex 명령줄 도구와 Claude Code입니다. 이 연결을 쓰는 스킬 `week-brief`(이번 주 수업 브리핑), `review-plan`(복습 계획), `note-from-lecture`(회차 정리본), `check-notices`(공지 누락·오독 확인), `app-checkup`(앱 설정 점검)이 `skills/` 폴더에 있습니다. 연결하는 법은 [docs/INSTALL.md](docs/INSTALL.md), 무엇이 건너가는지는 [docs/PRIVACY.md](docs/PRIVACY.md)에 있습니다. |
| 선택 기능 | 실시간 자막은 직접 켭니다. Clova 가져오기는 기본 꺼짐입니다(설정 → 선택 기능 → 실험실). |

## 설치와 첫 실행

**Microsoft Store에서 설치하는 것을 권합니다: https://apps.microsoft.com/detail/9NSZ9V0H0FBR**
Store판은 Microsoft가 서명해 경고 창 없이 설치되고, 새 버전도 Store가 알아서 받습니다.
Store를 쓸 수 없으면 릴리스 페이지의 설치 프로그램(`LectureHub-<버전>-win64-setup.exe`)을 받아 실행합니다. 어느 쪽이든 Python이나 ffmpeg를 따로 설치할 필요가 없습니다.
경고 창 넘기는 법, 새 버전으로 바꾸기, 지우기, 문제 보고는 [docs/INSTALL.md](docs/INSTALL.md)에 있습니다.

처음 켜면 마법사가 다음 순서로 안내합니다.

1. 녹음 전 확인사항
2. 학기 폴더
3. ChatGPT(Codex) 연결: [Codex에 연결]을 누르면 확인 창을 한 번 거쳐 연결합니다. 건너뛸 수 있습니다.
4. 시간표: 직접 입력하거나, 시간표 사진을 Codex가 읽게 합니다. 읽은 내용은 저장 전에 앱이 다시 확인합니다.
5. 필요한 프로그램 확인

이후 **설정** 화면에서 Gemini·Groq 키와 eTL 토큰을 버튼으로 연결하고 테스트합니다.

### 설치 전에 둘러보기

- 브라우저에서 보기: https://jiho370.github.io/lecture-hub-demo/ (예시 학기로 화면을 눌러 볼 수 있습니다. 녹음과 AI 실행은 재생으로 보여 줍니다.)
- 설치한 뒤: 마법사의 [예시 학기로 둘러보기]를 누르면 키나 녹음 없이 예시 학기가 열립니다. 설정 → AI → [Codex에 연결] 뒤 Codex에서 "이번 주 수문학 수업에서 과제에 대해 뭐라고 하셨는지 날짜와 시각을 달아 알려 줘"처럼 물어볼 수 있습니다.
- AI 앱에서 쓰는 스킬은 [skills/](skills/) 폴더에 있습니다.

## 알려진 한계

- 릴리스 페이지의 설치 프로그램과 zip에는 코드 서명이 없습니다. 처음 실행할 때 Windows SmartScreen이 경고합니다. 파일의 SHA-256은 릴리스 페이지의 `SHA256SUMS.txt`에 있습니다. Microsoft Store판은 Microsoft가 서명합니다.
- eTL 연동은 서울대 eTL이 기본이고, 다른 학교의 Canvas도 주소를 넣어 연결할 수 있습니다(과제 마감·자료·공지). 다른 학교 Canvas는 가짜 서버로만 시험했습니다.
- 지금까지 PC 한 기종에서만 써 보았습니다. 다른 소리 장치에서 되는지는 확인하지 못했습니다.
- 녹음 중 절전했다 깨어남과 장치를 뺐다 꽂음은 가짜 장치로만 시험했고, 아직 직접 해 보지는 않았습니다.
- 실시간 자막 중 Chirp(gRPC)는 Windows 인증서 저장소를 따르지 않아, HTTPS를 검사하는 네트워크에서는 실패할 수 있습니다.
- AI 앱 연결은 예시 학기에서 Codex 명령줄 도구와 Claude Code로 확인했습니다(수업 기록 묻기, 일정·보강 넣기, 정리본 저장, 시간표 사진 읽기, 주간 브리핑 스킬). 앱의 확인 창을 거쳐 일정이 들어가는 것은 실제 창에서 Codex로 한 번 확인했습니다. 처음 설정의 Codex 연결 단계와 나머지 스킬은 실제 AI 앱으로 끝까지 돌려 보지 않았습니다. ChatGPT 데스크톱 앱과 Claude 데스크톱 앱은 직접 해 보지 않았습니다. ChatGPT 데스크톱은 프로젝트 밖의 대화에서 로컬 MCP 도구가 보이지 않는 문제가 보고되어 있습니다(보고된 문제이며 직접 확인한 것은 아닙니다). ChatGPT·Claude 웹과 휴대폰 앱은 쓸 수 없습니다.
- 앱에 ffmpeg가 들어 있어 따로 설치할 필요는 없습니다. 화면에는 Windows의 Microsoft Edge WebView2 Runtime이 필요합니다. 정리본은 Codex CLI 또는 Claude Code를 따로 설치하고 로그인해야 만들 수 있습니다. 수업 중 공지 찾기도 기본(ChatGPT)으로 쓰려면 Codex CLI 설치와 로그인이 필요합니다. 없으면 보내지 않고 설정 화면에 그렇게 나옵니다. 그 밖의 선택 프로그램은 [docs/INSTALL.md](docs/INSTALL.md)에 있습니다.

## Codex와 ChatGPT Edu

기본 AI 엔진은 **Codex CLI**입니다. 설정에서 기본값과 정리본에 쓸 엔진을 고릅니다.
`LECTUREHUB_AI_ENGINE=codex|claude|off` 환경 변수가 설정에 우선합니다. Claude Code는 호환 선택지로 남아 있습니다.

- **정리본 만들기**: 제공자·과목별 전송 동의를 확인하고 전사·자료 글을 보냅니다. Codex는 빈 작업 폴더와 읽기 전용 샌드박스로 한 번 실행됩니다. 결과는 앱의 결정적 검증기를 거쳐 저장됩니다.
- **수업 중 공지 찾기**: 과목마다 허용(`codex_notices`)한 과목의 전사 글을 그 과목의 시간표·달력 일정과 함께 보냅니다. Codex는 정리본과 같은 방식(빈 작업 폴더, 읽기 전용 샌드박스, 셸 등 도구 끔)으로 한 번 실행됩니다. 공지는 인용이 전사에 그대로 있을 때만 남고 달력에는 후보로만 들어갑니다. Codex가 없거나 로그인되어 있지 않을 때 Google로 바꿔 보내지 않습니다.
- **HTML 도우미 열기**: 사용자 작업 폴더의 `$lecture-note <회차 ID>` 스킬을 Codex 콘솔에서 실행합니다. 이 콘솔은 자료를 읽고 완성 HTML을 쓰는 권한이 있으므로 작업과 결과를 직접 확인하세요.

Codex는 로그인한 ChatGPT 계정을 사용합니다. 학교 Edu 계정과 올바른 작업 공간을 직접 확인하세요.
**CLI 로그인 상태만으로 앱이 Edu 여부를 알아낼 수는 없습니다.** Edu의 데이터 정책과 관리자 설정은
[OpenAI 공식 안내](https://learn.chatgpt.com/docs/pricing)를 확인하세요. 개인 계정의 정책을 Edu와 같다고 가정하지 않습니다.

Codex `exec`는 완성된 결과를 돌려줍니다. 앱은 부분 답변 스트리밍이나 따뜻하게 유지되는 대화 세션을 구현했다고 표시하지 않습니다.

## 책임 있게 쓰기

- 녹음은 **교수님의 허락**이나 강의계획서의 허용을 확인한 수업에서만 합니다. 녹음과 전사는 개인 학습용이며 공유하지 않습니다.
- 소리와 글이 어디로 가는지는 [docs/PRIVACY.md](docs/PRIVACY.md)에, 쓰는 사람이 지킬 일은
  [docs/RESPONSIBLE_USE.md](docs/RESPONSIBLE_USE.md)에 적었습니다.
- 자동 노트의 본문 블록은 녹음 시각·자료 쪽 근거를 검사합니다. 외부 지식은 따로 표시합니다. 인용이 있어도 내용이 맞는지는 원문과 확인하세요. HTML 도우미의 전체 문장을 자동으로 검증하지는 않습니다.

## 개발

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[recorder,transcribe,materials,ui,dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m lecturehub.ui.app
```

작업 규칙은 [AGENTS.md](AGENTS.md), 구조는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), 라이선스는 MIT입니다
([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 참고).

## 비용

Lecture Hub는 무료입니다. 정리본·수업 중 공지 찾기·AI 앱 연결은 내 ChatGPT Edu 계정의 Codex로, 녹음 전사는 Groq(Whisper)·Google(Gemini)의 무료 한도 안에서 씁니다. 앱에서 별도 결제를 설정하지 않습니다. 남은 양은 이 앱이 보낸 양만 셉니다. 전사의 일일 한도와 긴 대기는 재개 시각까지 기다립니다. 짧은 한도 오류는 재시도하고 반복되면 실패로 남습니다. Codex 실패는 별도로 표시합니다.

예시 학기는 앱을 보여 주려고 만든 데이터입니다. 수문학·토질역학·관개배수공학의 일정과 전사, 자료와 정리본을 담았습니다. 녹음 파일은 실제 발언을 담지 않은 생성 음원입니다.
