# 구조

```
src/lecturehub/
  recorder/       녹음(마이크·PC 소리), 이어 쓰기 복구, 마무리(MP3), 원본 소리 무손실 사본(FLAC, 기본은 꺼짐: `keep_lossless`를 켤 때만)
  transcription/  전사 엔진(Gemini, Groq Whisper, 실시간 자막), 조각 나누기, 동의, 한도 관리
  index/          학기 폴더 색인(SQLite), 회차 연결, 강의자료 연결, eTL, 시험 준비, 폴더 정리
  materials/      PDF·슬라이드 글 뽑기와 쪽 이미지(pypdfium2)
  parsers/        엔진별 전사본 형식 읽기
  core/           설정(학기 폴더, 선택 기능), 연결(키 테스트), 프로그램 찾기, DB와 마이그레이션
  ui/             창(pywebview) + 화면(ui/web) + 첫 실행 마법사 + 설정 화면
  booking/        그스룸 예약(선택 기능)
  ai/             Engine 프로토콜, Codex·Claude 호출, 기능별 엔진 선택
  agent_skills/   AI 도우미용 스킬(강의 정리본)
  presets/        전사 엔진 설정
```

- **데이터 폴더**: 사용자가 고른 학기 폴더. 주 작업은 `_lecturehub`에 저장합니다. 녹음 보관용 복사·과목 정리본·사용자가 실행한 파일 정리는 해당 폴더에도 씁니다.
- **설정**: `%APPDATA%\LectureHub\settings.json`에 폴더·선택 기능·화면·AI 선택을 저장합니다. 과목별 녹음 허락과 제공자 동의는 학기 폴더의 `_lecturehub` JSON 파일이며 DB 스키마와 별개입니다.
- **자동 노트**: `study/note_job.py`가 동의를 확인하고 전사·자료 글을 선택한 `Engine.run_json`에 넘깁니다. 기본 Codex는 빈 폴더·읽기 전용으로 실행합니다. JSON 형식을 맞춘 뒤 `notes.validate`가 실제 인용 ID·구간·쪽을 검사합니다. 검증 실패 결과는 기존 노트를 대신하지 않습니다.
- **HTML 도우미**: 패키지 스킬을 `%LOCALAPPDATA%\LectureHub\agent-workspace`의 `.agents/skills`와 `.claude/skills`에 복사합니다. Codex 콘솔은 `$lecture-note <ID>`를 실행하며, 완성 HTML은 사용자 확인이 필요합니다.
- **그스룸**: AI 해석과 브라우저 조작을 분리합니다. 모델의 계획은 서비스에서 다시 검사합니다. 공개판은 자동·정기 예약과 3시간 초과 체인을 막고, 방·시각을 매번 확인합니다.
- **원칙**: 원본 보호 · 시간은 정수 ms · 근거 검사 · 제공자·과목별 전송 동의 · 금지된 과목 녹음·자막 차단.

## 설치판

- **실행 파일 둘**: `LectureHub.exe`(창)와 `LectureHubWorker.exe`(녹음·전사 같은 백그라운드 작업; `python -m lecturehub …`와 같은 인자를 받음). 다른 프로그램에게 앱을 부르는 법을 알려 줄 때는 `core/proc.console_python()`을 씁니다.
- **기록**: `core/applog.py`가 `%LOCALAPPDATA%\LectureHub\logs`에 씁니다. 잡지 못한 오류도 남기고, 키처럼 보이는 글자는 가립니다. 강의 내용은 적지 않습니다.
- **문제 보고 파일**: `core/diagnostics.py`. 경로를 가린 기록과 버전·개수 정보만 담습니다. 앱이 보내지 않습니다.
- **수업 목록 형식 올리기**: 설치판은 처음 열 때 백업한 뒤 스스로 올립니다(`core/db.upgrade`). 앱보다 새 형식은 아무것도 쓰지 않고 거절합니다.
- **새 버전 알림**: `core/update.py`. 하루 한 번 버전 번호만 읽고, 설치는 하지 않습니다.
- **빌드**: `tools/build_app.py`가 공개판 내보내기 → PyInstaller → 고정된 LGPL ffmpeg(`tools/fetch_tools.py`, 주소와 SHA-256) → 들어간 패키지의 라이선스 목록 → zip과 설치 프로그램(`packaging/installer.iss`) → `SHA256SUMS.txt` 순서로 만듭니다. 패키지 버전은 `requirements/release.txt`에 해시와 함께 고정되어 있습니다.
- **자체 점검**: `LectureHubWorker.exe selfcheck`와 `LectureHub.exe --smoke`가 임시 폴더에서 첫 설정 → 수업 목록 → 체험 학기 → 문제 보고 파일 → (숨긴 창) 화면 불러오기를 확인합니다. `--smoke`는 숨긴 창으로 수업·재생기·노트·설정·미니 창과 화면 밖으로 나가려는 시도까지 실제 화면을 돌아봅니다. GitHub Actions(`.github/workflows`)가 깨끗한 Windows에서 테스트 전체, 빌드, 자체 점검, 조용한 설치·제거까지 돌립니다.

`python tools/ui_headless.py serve --demo --scratch <빈 폴더>`는 가상 과목으로 화면을 보여 줍니다.
`--first-run`은 빈 프로필 마법사입니다. 설정·키·AI 작업 폴더도 임시 프로필에 격리하고 외부 AI·계정 작업을 차단합니다.
