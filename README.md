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
| 시험 준비 | 과목별 시험 일정, 수업 중 "시험에 나온다"고 한 말, 기출 파일 모음. |
| 학교 연동 | eTL(Canvas) 공지·과제·자료·게시판 글을 내 토큰으로 읽기만 합니다. 다른 학교 Canvas도 주소를 넣어 연결합니다. |
| 선택 기능 | 실시간 자막은 직접 켭니다. Clova 가져오기·그스룸 예약은 기본 꺼짐입니다(설정 → 실험실). 공개판은 자동·정기 예약과 3시간 초과 이어 예약을 막고 예약마다 방·시각을 확인합니다. |

## 설치와 첫 실행

**Microsoft Store에서 설치하는 것을 권합니다: https://apps.microsoft.com/detail/9NSZ9V0H0FBR**
Store판은 Microsoft가 서명해 경고 창 없이 설치되고, 새 버전도 Store가 알아서 받습니다.
Store를 쓸 수 없으면 릴리스 페이지의 설치 프로그램(`LectureHub-<버전>-win64-setup.exe`)을 받아 실행합니다. 어느 쪽이든 Python이나 ffmpeg를 따로 설치할 필요가 없습니다.
경고 창 넘기는 법, 새 버전으로 바꾸기, 지우기, 문제 보고는 [docs/INSTALL.md](docs/INSTALL.md)에 있습니다. 처음 켜면 마법사가 (1) 녹음 전 확인사항 (2) 학기 폴더 (3) 시간표 (4) 필요한 프로그램 확인을
차례로 안내합니다. 이후 **설정** 화면에서 Gemini·Groq 키와 eTL 토큰을 버튼으로 연결하고 테스트합니다.

## 알려진 한계

- 릴리스 페이지의 설치 프로그램과 zip에는 코드 서명이 없습니다. 처음 실행할 때 Windows SmartScreen이 경고합니다. 파일의 SHA-256은 릴리스 페이지의 `SHA256SUMS.txt`에 있습니다. Microsoft Store판은 Microsoft가 서명합니다.
- 그스룸 기능은 서울대학교 전용입니다. eTL 연동은 서울대 eTL이 기본이고, 다른 학교의 Canvas도 주소를 넣어 연결할 수 있습니다(과제 마감·자료·공지. 게시판은 LearningX 게시판이 있는 학교만). 다른 학교 Canvas는 가짜 서버로만 시험했습니다.
- 지금까지 PC 한 기종에서만 써 보았습니다. 다른 소리 장치에서 되는지는 확인하지 못했습니다.
- 녹음 중 절전했다 깨어남과 장치를 뺐다 꽂음은 가짜 장치로만 시험했고, 아직 직접 해 보지는 않았습니다.
- 실시간 자막 중 Chirp(gRPC)는 Windows 인증서 저장소를 따르지 않아, HTTPS를 검사하는 네트워크에서는 실패할 수 있습니다.
- 앱에 ffmpeg가 들어 있어 따로 설치할 필요는 없습니다. 화면에는 Windows의 Microsoft Edge WebView2 Runtime이 필요합니다. 정리본은 Codex CLI 또는 Claude Code를 따로 설치하고 로그인해야 만들 수 있습니다. 그 밖의 선택 프로그램은 [docs/INSTALL.md](docs/INSTALL.md)에 있습니다.

## Codex와 ChatGPT Edu

기본 AI 엔진은 **Codex CLI**입니다. 설정에서 기본값과 정리본·그스룸 지시 해석에 쓸 엔진을 고릅니다.
`LECTUREHUB_AI_ENGINE=codex|claude|off` 환경 변수가 설정에 우선합니다. Claude Code는 호환 선택지로 남아 있습니다.

- **정리본 만들기**: 제공자·과목별 전송 동의를 확인하고 전사·자료 글을 보냅니다. Codex는 빈 작업 폴더와 읽기 전용 샌드박스로 한 번 실행됩니다. 결과는 앱의 결정적 검증기를 거쳐 저장됩니다.
- **HTML 도우미 열기**: 사용자 작업 폴더의 `$lecture-note <회차 ID>` 스킬을 Codex 콘솔에서 실행합니다. 이 콘솔은 자료를 읽고 완성 HTML을 쓰는 권한이 있으므로 작업과 결과를 직접 확인하세요.
- **그스룸 채팅**: 입력 지시·최근 대화·내 예약의 방·날짜·번호를 보내기 전에 확인합니다. AI는 해석만 하고 예약은 앱이 다시 검사합니다. 동반자 이름·학번은 해석기로 보내지 않습니다. 브라우저 조작은 Aside 우선, 없으면 Claude in Chrome을 쓰는 기존 경로입니다.

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
