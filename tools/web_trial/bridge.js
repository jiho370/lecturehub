// Web trial: the real Lecture Hub screen in a browser. Read calls answer with responses recorded from the real app
// on the synthetic trial semester; recording, live transcript and note making are played back (20x speed).
// Nothing leaves the browser.
(function () {
  const CALLS = window.__DEMO_CALLS || {};
  const NativeDate = window.Date;
  const recorded = (CALLS.__meta || {}).recorded_at;
  const opened = NativeDate.now();
  // Pin recorded wall-clock time in every visitor's timezone; durations still advance normally.
  const anchor = recorded ? new NativeDate(recorded.slice(0, 19)).getTime() : opened;
  const epochShift = anchor - ((CALLS.__meta || {}).recorded_epoch_ms || anchor);
  // Keep numeric fetch/change timestamps aligned with the same wall clock (including overseas visitors).
  function alignEpochs(value) {
    if (!value || typeof value !== "object") return;
    for (const [k, v] of Object.entries(value)) {
      if (typeof v === "number" && /(?:_at|^at|at_ms)$/.test(k) && v > 1000000000) value[k] = v + epochShift / (v > 1000000000000 ? 1 : 1000);
      else alignEpochs(v);
    }
  }
  alignEpochs(CALLS);
  window.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [anchor + NativeDate.now() - opened])); }
    static now() { return anchor + NativeDate.now() - opened; }
  };
  window.__demoUnanswered = [];
  window.__demoFailures = [];
  window.__demoLog = [];
  // Explicitly unsupported actions have a friendly toast; an unexpected method is a QA failure.
  const APP_ONLY_CALLS = new Set(`about_open add_extra ai_select audio_cleanup_set bookmark choose_transcript
    class_over_answer confirm_pending conn_etl_site conn_save_unchecked correct discard discard_plan
    discard_recording discard_restore discard_session engine_start etl_board_check etl_file_take_all
    exam_page_add exam_page_open exam_page_pick exam_page_remove file_sort files_undo folder_layout_set
    gsr_companions_get gsr_rooms_get hold_answer hold_defer hold_reject hold_undo import_commit import_pick
    import_probe join join_candidates lecture_discard lecture_discard_plan mai_prepare material_preview
    next_class_answer open_folder open_material open_mic_settings open_path qa_ask qa_check qa_start qa_stop
    quit_app range_cut range_keep range_plan range_revert recover_session relink report_make report_plan
    save_memo setup_apply setup_close setup_pick_folder short_answer side_view start_companion startup_settings
    stop_companion transcript_save transcription_check transcription_start trial_start update_open`.split(/\s+/));
  const SPEED = 20;  // one second here = 20 seconds of lecture
  const APP_ONLY = { ok: true, message: "설치한 앱에서는 여기서 이 작업을 진행합니다." };
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const key = (name, args) => name + JSON.stringify(args);
  const byName = {};
  for (const k of Object.keys(CALLS).filter((k) => k.includes("["))) { const n = k.slice(0, k.indexOf("[")); if (!(n in byName)) byName[n] = CALLS[k]; }

  // ---- the trial lecture, its note, its transcript
  const lectureKey = Object.keys(CALLS).find((k) => k.startsWith("lecture[") && CALLS[k].lecture?.course === "수문학" && CALLS[k].lecture?.segments?.some(s => s.text.includes("합리식은")));
  const LECTURE = lectureKey ? CALLS[lectureKey].lecture : null;
  const LECTURE_ID = LECTURE && LECTURE.id;
  const NOTES = LECTURE ? clone(LECTURE.notes || []) : [];
  Object.keys(CALLS).filter(k => k.startsWith("lecture[")).forEach(k => (CALLS[k].lecture?.notes || []).forEach(n => { if(n.exists && !n.url) n.url = "demo:note:" + n.path; }));
  NOTES.forEach((n) => { if (n.exists && !n.url) n.url = "demo:note"; });  // recorded without the local media server
  const SEGMENTS = (LECTURE && LECTURE.segments || []).map((s) => ({ ms: s.start_ms, text: s.text }));
  const LAST_MS = SEGMENTS.length ? SEGMENTS[SEGMENTS.length - 1].ms + 25000 : 600000;

  const saved = (() => { try { return JSON.parse(sessionStorage.getItem("lh-demo") || "{}"); } catch (e) { return {}; } })();
  const sim = { noteMade: saved.noteMade !== false, phase: "idle", t0: 0, pausedAt: 0, pausedMs: 0, cap: false, job: null, done: null };
  const persist = () => { try { sessionStorage.setItem("lh-demo", JSON.stringify({ noteMade: sim.noteMade })); } catch (e) { /* private mode */ } };

  // ---- the web trial starts with the note NOT made yet, so [정리본 만들기] can be pressed
  function withNotes(value) {
    const v = clone(value);
    if (v && v.lecture && v.lecture.id === LECTURE_ID) v.lecture.notes = sim.noteMade ? clone(NOTES) : [];
    if (v && Array.isArray(v.courses)) v.courses.forEach((c) => (c.lectures || []).forEach((l) => {
      if (l.id !== LECTURE_ID) return;
      l.notes = sim.noteMade ? clone(NOTES) : [];
      c.with_notes = sim.noteMade ? 1 : 0;
    }));
    return v;
  }

  // ---- recording playback
  const now = () => Date.now();
  function elapsed() {
    if (!sim.t0) return 0;
    const end = sim.pausedAt || now();
    return Math.min(LAST_MS, (end - sim.t0 - sim.pausedMs) * SPEED);
  }
  function state() {
    const base = clone(CALLS["state[]"] || { phase: "idle" });
    base.probe = { running: false, verdict: "ok", peak: -18, remaining_ms: 0 };
    if (sim.phase === "idle") return base;
    const ms = elapsed();
    if (sim.phase === "recording" && ms >= LAST_MS) stop();  // the synthetic class is over
    const course = (LECTURE && LECTURE.course) || "";
    if (sim.phase === "starting" || sim.phase === "recording") {
      if (sim.phase === "starting" && now() - sim.t0 > 900) sim.phase = "recording";
      return { ...base, phase: sim.phase, status: { course, elapsed_ms: ms, rms: sim.pausedAt ? null : -32 + Math.sin(now() / 170) * 7 + Math.random() * 4,
        device: "기본 마이크", mode: "mic", paused: !!sim.pausedAt, dropped_samples: 0 } };
    }
    const since = now() - sim.stoppedAt;
    if (since < 900) return { ...base, phase: "stopping" };
    if (since < 2200) return { ...base, phase: "finalizing", summary: { total_ms: sim.total } };
    return { ...base, phase: "done", summary: { total_ms: sim.total, dropped_samples: 0, session_dir: "예시 학기/_lecturehub/recordings/수문학", master: "master.m4a" },
      link: { state: "linked", media_id: "demo-media", lecture_id: LECTURE_ID, confidence: "high", reason: "시간표의 수업 시간과 맞음" },
      transcription: { state: "done", label: "완료 · Google (미리 만든 전사)" } };
  }
  function start() { Object.assign(sim, { phase: "starting", t0: now(), pausedAt: 0, pausedMs: 0 }); return { ok: true }; }
  function stop() {
    if (!["starting", "recording"].includes(sim.phase)) return { ok: true };
    sim.total = elapsed(); sim.phase = "stopped"; sim.stoppedAt = now(); sim.cap = false;
    return { ok: true };
  }
  function pause(flag) {
    if (flag && !sim.pausedAt) sim.pausedAt = now();
    if (!flag && sim.pausedAt) { sim.pausedMs += now() - sim.pausedAt; sim.pausedAt = 0; }
    return { ok: true };
  }
  function captions() {
    if (!sim.cap) return { state: "none", running: false, paused: false };
    const ms = elapsed();
    const lines = SEGMENTS.filter((s) => s.ms + 4000 <= ms).map((s) => ({ at: s.ms / 1000, text: s.text }));
    const next = SEGMENTS.find((s) => s.ms <= ms && s.ms + 4000 > ms);
    return { state: sim.pausedAt ? "paused" : "running", running: true, paused: !!sim.pausedAt, elapsed_s: ms / 1000, lines,
      interim: next ? next.text.slice(0, Math.max(4, Math.round(next.text.length * (ms - next.ms) / 4000))) : "" };
  }

  // ---- note making playback: the evidence-checked result appears after a few seconds
  function makeNote() {
    sim.job = { state: "running", lecture_id: LECTURE_ID, at: now() };
    return { ok: true };
  }
  function noteStatus() {
    const j = sim.job;
    if (!j) return { state: "idle" };
    if (now() - j.at < 6000) return { state: "running", lecture_id: j.lecture_id };
    sim.noteMade = true; persist();
    const res = { state: "done", lecture_id: j.lecture_id, html: NOTES[0] && NOTES[0].path, rejected: 0, blocks: 8 };
    sim.job = null;
    setTimeout(() => toastOnce("정리본을 저장했습니다. 각주에서 전사 시각과 자료 쪽을 확인할 수 있습니다."), 600);
    return res;
  }
  function toastOnce(text) {
    const t = document.createElement("div");
    t.className = "demo-note";
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 7000);
  }
  // In-page confirmations only: never open a native window or operate on local files.
  function demoConfirm(message) {
    return new Promise((resolve) => {
      const scrim = document.createElement("div");
      scrim.className = "demo-confirm";
      scrim.innerHTML = '<section role="dialog" aria-modal="true" aria-label="안내 안내"><h3>안내 안내</h3><p></p><div><button data-no>취소</button><button data-yes>확인</button></div></section>';
      scrim.querySelector("p").textContent = message;
      const done = (yes) => { scrim.remove(); resolve(yes); };
      scrim.querySelector("[data-no]").onclick = () => done(false);
      scrim.querySelector("[data-yes]").onclick = () => done(true);
      scrim.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } });
      document.body.appendChild(scrim);
      scrim.querySelector("[data-no]").focus();
    });
  }
  const explain = (text) => { const message = text; toastOnce(message); return { ok: true, message }; };
  const APP_ACTION_TEXT = {
    open_material: "설치한 앱에서는 연결된 강의자료를 기본 PDF·문서 프로그램으로 엽니다.",
    open_folder: "설치한 앱에서는 이 과목 폴더를 탐색기로 엽니다.",
    open_path: "설치한 앱에서는 이 파일을 기본 프로그램으로 엽니다.",
    transcript_save: "설치한 앱에서는 전사본을 텍스트 파일로 저장합니다.",
    transcription_start: "설치한 앱에서는 과목별 전송 동의를 확인하고 남은 무료 한도 안에서 녹음을 전사합니다.",
    material_preview: "설치한 앱에서는 강의자료의 해당 쪽을 미리 봅니다.",
    import_pick: "설치한 앱에서는 가져올 녹음·전사·자료 파일을 고릅니다.",
    setup_pick_folder: "설치한 앱에서는 내 학기 폴더를 고릅니다.",
    trial_start: "설치한 앱에서는 예시 학기를 만들어 엽니다.",
    about_open: "설치한 앱에서는 사용 안내와 설치 페이지를 엽니다.",
    report_make: "설치한 앱에서는 포함할 항목을 확인한 뒤 문제 보고 파일을 만듭니다.",
    quit_app: "설치한 앱에서는 Lecture Hub를 종료합니다.",
  };
  window.open = () => { explain("설치한 앱에서는 연결한 프로그램이나 도움말을 엽니다."); return null; };
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (a && !a.getAttribute("href").startsWith("#")) { e.preventDefault(); explain("설치한 앱에서는 해당 링크나 파일의 원문을 엽니다."); }
  }, true);

  // ---- search over the recorded rows
  const searchRows = [].concat(...Object.keys(CALLS).filter((k) => k.startsWith("search[")).map((k) => CALLS[k].results || []));
  function search(q) {
    const want = String(q || "").trim(), seen = new Set();
    return { ok: true, results: searchRows.filter((r) => {
      const id = `${r.lecture_id}:${r.start_ms}:${r.kind}`;
      if (!want || !(r.text || "").includes(want) || seen.has(id)) return false;
      seen.add(id); return true;
    }) };
  }

  // ---- key connection: kept in this tab only (never stored, never sent); all keys start disconnected
  const conn = { gemini: true, groq: true, etl: true };  // explicit scenario presence, no stored keys
  function connStatus() {
    const v = clone(CALLS["conn_status[]"] || { ok: true, keys: [] });
    (v.keys || []).forEach((k) => { k.connected = !!conn[k.id]; k.source = conn[k.id] ? "keyring" : null; });
    return v;
  }
  function connConnect(provider, key) {
    conn[provider] = true;
    return explain("설치한 앱에서는 키를 제공자에 확인한 뒤 이 PC에 저장합니다.");
  }

  // ---- eTL: connected after a token is "connected" above; [지금 가져오기] fills synthetic notices, deadlines, files
  const etl = { synced: 0 };
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const day = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return d; };
  const COURSE = (LECTURE && LECTURE.course) || "수문학";
  function etlBoard() {
    return clone(CALLS["etl_board[]"] || { ok: true, announcements: [], files: [], have: 0, total: 0, closed: [] });

  }
  function etlSync() {
    etl.synced = Date.now();
    return explain("설치한 앱에서는 eTL의 새 과제·공지·강의자료 목록을 가져옵니다.");
  }
  const extraEvents = [];
  const changedEvents = {};
  function calendar(year, month) {
    const v = clone(CALLS[key("calendar", [year, month])] || byName.calendar || { ok: true, events: [], upcoming: [] });
    if (v.ok === undefined) v.ok = true;
    v.year = year; v.month = month;
    const mine = extraEvents.slice();
    for (const k of ["events", "upcoming", "candidates"]) v[k] = (v[k] || []).map((e) => ({ ...e, ...(changedEvents[e.id] || {}) }));
    for (const e of v.candidates.filter((e) => e.status === "confirmed")) mine.push(e);
    v.candidates = v.candidates.filter((e) => e.status === "candidate");
    const prefix = `${year}-${String(month).padStart(2, "0")}`;
    v.events = v.events.filter((e) => e.status !== "cancelled" && e.date.startsWith(prefix));
    v.upcoming = v.upcoming.filter((e) => e.status !== "cancelled");
    v.events = v.events.concat(mine.filter((e) => e.status !== "cancelled" && e.date && e.date.startsWith(prefix)));
    v.upcoming = v.upcoming.concat(mine.filter((e) => e.status !== "cancelled" && e.date >= iso(new Date())));
    v.upcoming.forEach((e) => { e.d_day = dayDiff(e.date, iso(new Date())); });
    v.next_exam = v.upcoming.filter((e) => ["exam", "quiz"].includes(e.kind)).sort((a, b) => a.date.localeCompare(b.date))[0] || null;
    return v;
  }
  function dayDiff(a, b) { const utc = (s) => Date.UTC(...s.split("-").map((n, i) => +n - (i === 1 ? 1 : 0))); return Math.floor((utc(a) - utc(b)) / 86400000); }
  const link = clone(CALLS["ai_link_status[]"]);
  const codexApp = link.apps.find(a => a.app === "codex"); if(codexApp) codexApp.state = "connected";
  link.codex_found = true;
  const reminders = clone(CALLS["reminders_get[]"]);
  const stay = clone(CALLS["stay_get[]"]);
  const table = clone(CALLS["timetable_get[]"]);
  function aiLinkStatus() {
    return { ...clone(link), enabled: set.link, courses: set.courses.map((course) => ({ course, allowed: has("ai_app_link", course) })) };
  }
  function undoChange(id, since = false) {
    const picked = link.changes.find((c) => c.id === id);
    let n = 0;
    for (const c of link.changes) if (c.undoable && (since ? c.at_ms >= (picked || {}).at_ms : c.id === id)) {
      c.undoable = false; c.undone = true; n++;
      for (const event of (byName.calendar.upcoming || [])) if (c.summary === `${event.title} 일정 추가`) changedEvents[event.id] = { status: "cancelled" };
    }
    return { ok: true, message: `이 탭의 변경 ${n}건을 되돌렸어요.` };
  }
  function weekView(offset = 0) {
    if (CALLS[key("week_view", [offset])]) {
      const v = clone(CALLS[key("week_view", [offset])]);
      v.days.forEach((d) => { d.items = overlayItems(d.items, d.date, d.date); });
      return v;
    }
    const v = clone(CALLS["week_view[0]"]);
    const shift = (text) => { const d = new Date(text + "T12:00:00"); d.setDate(d.getDate() + offset * 7); return iso(d); };
    v.start = shift(v.start); v.end = shift(v.end);
    v.days = v.days.filter((d) => new Date(d.date + "T12:00:00").getDay() % 6 !== 0).map((d) => ({ date: shift(d.date), items: [],
      classes: d.classes.filter((c) => c.kind === "정규").map((c) => ({ ...c, date: shift(d.date) })) }));
    return v;
  }
  function overlayItems(rows, first, last) {
    const kept = rows.filter((r) => (changedEvents[String(r.id).replace(/^calendar:/, "")] || {}).status !== "cancelled");
    const extras = extraEvents.concat((byName.calendar.candidates || []).filter((c) => (changedEvents[c.id] || {}).status === "confirmed"));
    for (const e of extras) if (["assignment", "report", "presentation", "exam", "quiz"].includes(e.kind) && e.status !== "cancelled" && e.date >= first && e.date <= last && !kept.some((r) => r.id === `calendar:${e.id}`)) {
      kept.push({ id: `calendar:${e.id}`, course: e.course, title: e.title, kind: e.kind, timed: e.start_ms != null,
        at: e.date + "T" + (e.start_ms != null ? `${two(Math.floor(e.start_ms / 3600000))}:${two(Math.floor(e.start_ms / 60000) % 60)}` : "23:59") });
    }
    return kept;
  }
  function todayBriefing() {
    const v = clone(CALLS["today_briefing[]"]), today = iso(new Date());
    const end = day(7);
    const rows = overlayItems(v.dues.concat(v.exams), today, iso(end));
    v.dues = rows.filter((r) => !["exam", "quiz"].includes(r.kind) && new Date(r.at) - new Date() >= 0 && new Date(r.at) - new Date() <= 86400000);
    v.exams = rows.filter((r) => ["exam", "quiz"].includes(r.kind));
    return v;
  }

  // ---- study-room booking (owner edition only; the public screens never call these): booking/return/cancel run as a short fake job
  const room = { active: [], past: [], log: [], job: { state: "idle" }, updated_at: null, n: 0 };
  const two = (n) => String(n).padStart(2, "0");
  function roomView() {
    const v = clone(byName.room || { ok: true, days: [], starts: [], uses: [], hours: [3], weekdays: [], suggest: [], rules: [] });
    if (room.job.state === "running" && Date.now() - room.job.at > 2500) finishRoomJob();
    return Object.assign(v, { job: clone(room.job), active: clone(room.active), past: clone(room.past), log: clone(room.log),
      updated_at: room.updated_at, updated_by: room.updated_at ? "app" : null, browser: "aside" });
  }
  function roomJob(action, label, args) { room.job = { state: "running", action, label, at: Date.now(), args }; return { ok: true }; }
  function finishRoomJob() {
    const j = room.job, now = new Date().toISOString();
    let result = { ok: true };
    if (j.action === "book") {
      const [date, start, use, name, hours] = j.args;
      const seat = (name || "G407").toUpperCase();
      const [h, m] = start.split(":").map(Number), endMin = h * 60 + m + Math.round((hours || 3) * 60);
      const end = `${two(Math.floor(endMin / 60) % 24)}:${two(endMin % 60)}`;
      const id = `demo-${++room.n}`;
      room.active.push({ id, seat, monitor: ["G405", "G407", "G411", "G413"].includes(seat), when: `${date.slice(5).replace("-", "/")} ${start}~${end}`,
        status_name: `예약됨 · ${use}`, tone: "ok", active: true, can_cancel: true, can_return: false });
      result = { ok: true, room: seat, date: date.replace(/-/g, ""), start: start.replace(":", ""), end: end.replace(":", ""), minutes: (hours || 3) * 60 };
    } else if (j.action === "cancel") {
      const ids = j.args[0];
      const gone = room.active.filter((b) => ids.includes(b.id));
      room.active = room.active.filter((b) => !ids.includes(b.id));
      result = { ok: true, cancelled: gone.map((b) => [b.seat, "2026-01-01 " + b.when.split(" ")[1].split("~")[0], b.when.split("~")[1]]) };
    } else if (j.action === "return") {
      const b = room.active.find((x) => x.id === j.args[0]);
      room.active = room.active.filter((x) => x.id !== j.args[0]);
      if (b) room.past.unshift({ ...b, active: false, tone: "done", status_name: "반납함", can_cancel: false });
      result = { ok: true, room: b && b.seat };
    }
    room.log.unshift({ ts: now, kind: j.action === "list" ? "list" : j.action, room: result.room || "", ok: true, by: "app", detail: "안내" });
    room.updated_at = now;
    room.job = { state: "done", action: j.action, result };
  }
  function roomChat(text) {
    const t = String(text || "");
    const when = /내일/.test(t) ? 1 : /모레/.test(t) ? 2 : 0;
    const hh = (t.match(/(\d{1,2})\s*시/) || [])[1];
    const hours = Number((t.match(/(\d(?:\.\d)?)\s*시간/) || [])[1] || 3);
    if (/반납/.test(t) && room.active.length) return { ok: true, say: `${room.active[0].seat} 예약을 반납할까요?`, confirm: { intent: "return", ids: [room.active[0].id] } };
    if (/취소/.test(t) && room.active.length) return { ok: true, say: `${room.active[0].seat} ${room.active[0].when} 예약을 취소할까요?`, confirm: { intent: "cancel", ids: [room.active[0].id] } };
    if (!hh) return { ok: true, say: "언제 쓸지 알려 주세요. 예: '내일 3시부터 2시간 과제'" };
    let h = Number(hh); if (h < 8) h += 12;
    const date = iso(day(when)), start = `${two(h)}:00`, use = /공부|과제/.test(t) || true ? "과제" : "기타";
    return { ok: true, say: `${date.slice(5).replace("-", "/")} ${start}부터 ${hours}시간, ${use} 용도로 G407(모니터 방)을 잡을까요?`,
      confirm: { intent: "book", ids: [], booking: { date, start, use, room: "G407", hours } } };
  }
  function roomChatConfirm(intent, ids, booking) {
    if (intent === "book" && booking) return roomJob("book", "예약", [booking.date, booking.start, booking.use, booking.room, booking.hours]);
    if (intent === "cancel") return roomJob("cancel", "예약 취소", [ids]);
    return roomJob("return", "반납", [ids[0]]);
  }

  // ---- settings screen: synthetic answers for the calls the trial recording does not have (kept in this tab only).
  // All course names come from the synthetic recording.
  const PERMISSION_STATES = { allowed: "허락함", syllabus: "강의계획서 허용", unknown: "아직 확인 안 함", forbidden: "금지" };
  const CONSENT_LABELS = { ai_app_link: "AI 앱 연결 · 내가 연결한 AI 앱", codex_notices: "OpenAI Codex · 수업 중 공지 찾기", codex_etl_notices: "OpenAI Codex · eTL 공지에서 일정 찾기",
    codex_note: "OpenAI Codex · 정리본", gemini_transcribe: "Google · 녹음 전사" };
  const set = {
    courses: (CALLS["init[]"].courses || []).map((c) => c.folder),
    permission: {}, consent: {}, link: true, noticesOn: true, engine: "codex", live: "quick", update: true, classReminder: false,
    notify: { assignment_new: true, due_changed: true, files_new: true },
  };
  const NOTIFY_LABELS = { assignment_new: "새 eTL 과제", due_changed: "eTL 과제 마감이 바뀜", files_new: "eTL에 새 강의자료" };
  const has = (provider, course) => !!(set.consent[provider] && set.consent[provider][course]);
  const grant = (provider, courses) => { set.consent[provider] = set.consent[provider] || {}; courses.forEach((c) => { set.consent[provider][c] = Date.now(); }); return { ok: true }; };
  const revoke = (provider, courses) => { const m = set.consent[provider] || {}; let n = 0; courses.forEach((c) => { if (m[c]) { delete m[c]; n += 1; } }); return { ok: true, revoked: n }; };
  for (const c of set.courses) for (const provider of ["ai_app_link", "codex_notices", "codex_etl_notices"]) grant(provider, [c]);
  const DEMO_CONFIRM = "\n\n이 페이지의 변경은 이 탭에만 반영됩니다.";
  const settingsCalls = {
    recording_permissions: () => ({ ok: true, states: PERMISSION_STATES, courses: set.courses.map((c) => ({ course: c, state: set.permission[c] || "unknown", label: PERMISSION_STATES[set.permission[c] || "unknown"] })) }),
    recording_permission: (course, state) => { if (state) set.permission[course] = state; return { ok: true, allowed: (set.permission[course] || "unknown") !== "forbidden", state: set.permission[course] || "unknown" }; },
    recording_permission_confirm_many: async (state, names) => ({ ok: true, yes: await demoConfirm(`아래 과목 ${names.length}개의 녹음 허락을 모두 '${PERMISSION_STATES[state]}'(으)로 바꿉니다.\n${names.map((n) => "· " + n).join("\n")}${DEMO_CONFIRM}`), token: "demo" }),
    recording_permission_all: (state, names) => { names.forEach((c) => { set.permission[c] = state; }); return { ok: true, changed: names.length }; },
    consent_list: () => ({ ok: true, rows: [].concat(...Object.entries(set.consent).map(([p, m]) => Object.entries(m).map(([c, at]) => ({ provider: p, course: c, label: CONSENT_LABELS[p] || p, granted_at: at, all_courses: false })))) }),
    consent_confirm_many: async (provider, names) => ({ ok: true, yes: await demoConfirm(`${CONSENT_LABELS[provider] || provider}\n과목 ${names.length}개:\n${names.map((n) => "· " + n).join("\n")}${DEMO_CONFIRM}`), token: "demo" }),
    consent_revoke: (provider, course) => revoke(provider, [course]), consent_revoke_many: (provider, names) => revoke(provider, names),
    ai_link_enable: (on) => { set.link = !!on; return { ok: true, enabled: set.link }; },
    ai_link_allow: (course) => grant("ai_app_link", [course]), ai_link_allow_all: (names) => grant("ai_app_link", names),
    notices_setting_get: () => noticesSetting(), notices_setting_set: (on) => { set.noticesOn = !!on; return noticesSetting(); },
    notices_engine_set: (name) => { set.engine = name === "google" ? "google" : "codex"; return noticesSetting(); },
    notices_allow: (course) => grant("codex_notices", [course]), notices_allow_all: (names) => grant("codex_notices", names),
    notices_attention: () => ({ ok: true, show: false }), notices_attention_seen: () => ({ ok: true }),
    etl_notices_allow: (course) => grant("codex_etl_notices", [course]), etl_notices_allow_all: (names) => grant("codex_etl_notices", names),
    etl_notify_get: () => ({ ok: true, switches: set.notify, labels: NOTIFY_LABELS }),
    etl_notify_set: (kind, on) => { set.notify[kind] = !!on; return { ok: true, switches: set.notify, labels: NOTIFY_LABELS }; },
    etl_news_new: () => ({ ok: true, items: [] }), etl_news_seen: () => ({ ok: true, seen: 0 }),
    live_engine_get: () => ({ ok: true, engine: set.live, choices: { quick: "Gemini Live — 말하자마자 글로(무료 Gemini 키, 소음에 약함)",
      chirp: "Chirp 3 — 더 정확, 15~25초 늦게 나옴(Google Cloud 로그인, 월 60분 무료 뒤 유료)", both: "둘 다 — 빠른 줄을 먼저 보이고 Chirp 줄로 확정" } }),
    live_engine_set: (v) => { set.live = v; return { ok: true, engine: v }; },
    update_status: () => ({ ok: true, possible: true, on: set.update, available: false }), update_set: (on) => { set.update = !!on; return { ok: true }; },
    about: () => clone(CALLS["about[]"]),
  };
  function noticesSetting() {
    const needs = set.engine === "codex" ? set.courses.filter((c) => !has("codex_notices", c)).slice(0, 2) : [];
    return { ok: true, on: set.noticesOn, engine: set.engine, engines: { codex: "ChatGPT Edu (Codex)", google: "Google Gemini" }, default_engine: "codex",
      readings: 2, characters: 41000, by_engine: { codex: 2 }, codex_found: true, unavailable: null, needs_consent: needs, recording_held: false,
      courses: set.courses.map((c) => ({ course: c, allowed: has("codex_notices", c) })),
      etl_courses: set.courses.map((c) => ({ course: c, allowed: has("codex_etl_notices", c) })), etl_needs_consent: [] };
  }

  const LIVE = {
    ...settingsCalls,
    ai_link_status: aiLinkStatus,
    ai_link_state: () => ({ ok: true, enabled: set.link, write: link.write }),
    ai_link_write: (on) => { link.write = !!on; return aiLinkStatus(); },
    ai_link_enable: (on) => { set.link = !!on; return aiLinkStatus(); },
    ai_link_auto_confirm: async (kind) => ({ ok: true, yes: await demoConfirm(`${kind === "*" ? "모든 종류" : (link.auto.find((k) => k.kind === kind) || {}).label}를 자동으로 바꿀까요?\n실제 앱에서는 이후 이 종류의 변경을 묻지 않고 적용합니다. 시간표에서 과목을 빼는 요청은 늘 묻습니다.\n이 탭에서만 바뀝니다.`), token: "demo" }),
    ai_link_auto: (kind, on) => { link.auto.forEach((k) => { if (kind === "*" || k.kind === kind) k.auto = !!on; }); return aiLinkStatus(); },
    ai_link_auto_reset: () => { link.auto.forEach((k) => { k.auto = false; }); return aiLinkStatus(); },
    ai_link_connect_confirm: async (app) => ({ ok: true, yes: await demoConfirm(`실제 앱에서는 ${app === "codex" ? "Codex의 config.toml" : "Claude 데스크톱의 설정 파일"}에 Lecture Hub 연결 설정을 씁니다. 기존 설정은 백업하고 연결 해제로 되돌릴 수 있습니다.\n파일을 쓰거나 프로그램을 실행하지 않고 연결된 모습만 보여 드려요.`), token: "demo" }),
    ai_link_connect: (app) => { const row = link.apps.find((a) => a.app === app); if (row) row.state = "connected"; return { ok: true }; },
    ai_link_disconnect: (app) => { const row = link.apps.find((a) => a.app === app); if (row) row.state = "off"; return { ok: true }; },
    ai_link_open_codex: () => explain("설치한 앱에서는 이 회차와 Lecture Hub 도구가 연결된 Codex를 엽니다."),
    ai_change_undo: (id) => undoChange(id), ai_change_undo_since: (id) => undoChange(id, true),
    timetable_photo_confirm: async () => ({ ok: true, yes: await demoConfirm("실제 앱에서는 시간표 사진을 고르고 전송을 확인한 뒤 내 Codex(ChatGPT)로 엽니다. AI가 읽은 표를 먼저 확인하고 시간표에 넣습니다.\n파일을 고르거나 전송하지 않으며, 사진은 브라우저 밖으로 나가지 않아요."), token: "demo" }),
    timetable_photo_open: () => explain("설치한 앱에서는 시간표 사진을 Codex로 보내 읽은 표를 확인합니다."),
    timetable_get: () => clone(table),
    timetable_save: () => explain("설치한 앱에서는 이전 시간표를 백업하고 새 시간표를 적용합니다."),
    reminders_get: () => clone(reminders),
    reminders_set: (changes) => { Object.assign(reminders, changes); reminders.courses.forEach((c) => { c.muted = reminders.muted_courses.includes(c.course); }); return clone(reminders); },
    stay_get: () => clone(stay),
    stay_set: (on) => { stay.keep_running = !!on; return clone(stay); },
    startup_set: (on) => { stay.startup.enabled = !!on; return clone(stay.startup); },
    week_view: weekView,
    today_briefing: todayBriefing,
    qa_status: () => ({ ok: true, state: "idle" }), qa_packs: () => ({ ok: true, packs: [] }),
    etl_board_new: () => ({ ok: true, posts: [] }), etl_board_seen: () => ({ ok: true }),
    client_error: (message) => { window.__demoFailures.push(["client_error", message]); return { ok: true }; },
    appearance: (theme, size) => { const v = CALLS["appearance[]"]; if (theme) v.theme = theme; if (size) v.text_size = size; return clone(v); },
    conn_status: connStatus, conn_connect: connConnect, conn_test: (p) => (conn[p] ? { ok: true } : { ok: false, error: "연결된 키가 없어요" }),
    conn_remove: (p) => { delete conn[p]; return { ok: true }; },
    conn_open_help: () => explain("설치한 앱에서는 제공자의 연결 안내를 엽니다."),
    etl_status: () => ({ ...clone(CALLS["etl_status[]"]), ok: true, connected: true }),
    etl_board: etlBoard, etl_sync: etlSync,
    etl_file_take: () => explain("설치한 앱에서는 과목 폴더의 강의자료로 받습니다."),
    etl_file_take_all: () => explain("설치한 앱에서는 새 eTL 자료를 과목 폴더에 받고, 같은 이름의 파일이 있으면 먼저 확인합니다."),
    etl_file_keep: () => ({ ok: true }),
    calendar, calendar_add: (v) => { const id = `me-${extraEvents.length + 1}`; extraEvents.push({ ...v, id, source: "user", status: "confirmed", date_precision: "day", course: ((byName.calendar.courses || []).find((c) => c.id === v.course_id) || {}).folder,
      kind_label: { exam: "시험", quiz: "퀴즈", assignment: "과제", personal: "개인", other: "기타" }[v.kind] || v.kind,
      start_ms: v.start ? v.start.split(":").reduce((h, m) => +h * 60 + +m) * 60000 : null,
      end_ms: v.end ? v.end.split(":").reduce((h, m) => +h * 60 + +m) * 60000 : null }); return { ok: true, id }; },
    calendar_status: (id, status) => { changedEvents[id] = { ...(changedEvents[id] || {}), status }; const e = extraEvents.find((e) => e.id === id); if (e) e.status = status; return { ok: true }; },
    calendar_update: (id, values) => { const v = { ...values, date: values.day || values.date };
      for (const k of ["start", "end"]) if (typeof values[k] === "string") v[k + "_ms"] = values[k] ? values[k].split(":").reduce((h, m) => +h * 60 + +m) * 60000 : null;
      if ("course_id" in values) v.course = ((byName.calendar.courses || []).find((c) => c.id === values.course_id) || {}).folder || null;
      changedEvents[id] = { ...(changedEvents[id] || {}), ...v }; const e = extraEvents.find((e) => e.id === id); if (e) Object.assign(e, changedEvents[id]); return { ok: true }; },
    room: roomView, room_refresh: () => roomJob("list", "예약 목록 확인", []),
    room_book: (d, s, u, r, h) => roomJob("book", "예약", [d, s, u, r, h]),
    room_cancel: (ids) => roomJob("cancel", "예약 취소", [ids]), room_return: (id) => roomJob("return", "반납", [id]),
    room_stop: () => { room.job = { state: "stopped", action: room.job.action }; return { ok: true }; },
    room_chat: roomChat, room_chat_confirm: roomChatConfirm, room_rules: () => ({ ok: true }),
    gsr_rooms_set: () => ({ ok: true }),
    lab_set: () => { explain("설치한 앱에서는 필요한 선택 기능을 켤 수 있습니다."); return { ok: true, labs: { ...(CALLS["init[]"].labs || {}), gsr: false } }; },
    gsr_companions_set: () => ({ ok: true }),
    clova_sync: () => explain("설치한 앱에서는 Aside 브라우저로 Clova Note의 녹음을 가져옵니다."),
    state, start, stop, pause: (flag) => pause(!!flag),
    captions_check: () => ({ ok: true, needs_consent: false, once_only: false }),
    captions_start: () => { sim.cap = true; return { ok: true }; },
    captions_status: captions,
    captions_stop: () => { sim.cap = false; return { ok: true }; },
    captions_pause: (flag) => pause(!!flag),
    consent_confirm: async (provider, course) => (provider !== "codex_note" ? { ok: true, yes: await demoConfirm(`${course} · ${CONSENT_LABELS[provider] || provider}${DEMO_CONFIRM}`), token: "demo" } : { ok: true, yes: await demoConfirm("설치한 앱에서는 이 과목의 전사 글을 내 ChatGPT(Edu) 계정의 Codex로 보내 정리본을 만듭니다. 소리는 보내지 않습니다."), token: "demo" }),
    make_note: makeNote, note_status: noteStatus,
    search: (q) => search(q),
    probe: () => ({ ok: true }),
    suggest_course: () => ({ course: COURSE }),  // the trial timetable's class, whatever day the page is opened
    sidebar_state: () => ({ ok: true }), set_selection: () => ({ ok: true }), notices_seen: () => ({ ok: true }),
    class_reminder_set: (on) => { set.classReminder = !!on; return { ok: true, reminder: set.classReminder }; },
  };
  function answer(name, args) {
    if (name in LIVE) return LIVE[name](...args);
    if (name === "init") {
      const v = clone(CALLS["init[]"]);
      v.trial = false;  // the web bar replaces the app's "내 학기 폴더 정하기" bar
      v.config_error = null;  // recorded before the trial wrote recorder.toml (fixed in the app)
      v.class_reminder = set.classReminder;  // avoid interrupting a judge on first open; the setting remains interactive
      v.features = Object.assign({}, v.features, { captions: { state: "ready" } });
      v.course = (LECTURE && LECTURE.course) || v.course;
      return v;
    }
    const exact = CALLS[key(name, args)];
    if (exact !== undefined) return withNotes(exact);
    if (name in byName) return withNotes(byName[name]);
    if (!APP_ONLY_CALLS.has(name)) window.__demoUnanswered.push([name, args]);
    return explain(APP_ACTION_TEXT[name] || "설치한 앱에서는 여기서 선택한 수업 자료와 설정에 작업을 적용합니다.");
  }
  window.pywebview = { api: new Proxy({}, { get(_, name) {
    if (typeof name !== "string" || name === "then") return undefined;
    return (...args) => new Promise((resolve) => setTimeout(async () => {
      let out;
      try { out = clone(await answer(name, args) ?? { ok: true }); }
      catch (error) { window.__demoFailures.push([name, String(error)]); out = clone(APP_ONLY); toastOnce(out.error); }
      (window.__demoLog || (window.__demoLog = [])).push([name, out && out.ok === false ? out.error || out.reason : ""]);
      resolve(out);
    }, 40));
  } }) };

  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.className = "trial-bar";
    bar.innerHTML = "<span>Lecture Hub 1.1.0 · 예시 학기 · 앱을 보여 주려고 만든 데이터 · 설치: Microsoft Store</span><button id=\"demoReset\">처음부터</button>";
    if (new URLSearchParams(location.search).get("bare") !== "1") document.body.prepend(bar);
    bar.querySelector("#demoReset").onclick = () => { try { sessionStorage.clear(); localStorage.clear(); } catch (e) { /* */ } location.reload(); };
    const css = document.createElement("style");
    css.textContent = ".trial-bar{font-size:11px!important;padding:3px 12px!important;min-height:24px!important}.trial-bar>span{flex:1;min-width:0}.demo-confirm{position:fixed;inset:0;z-index:20000;background:#0008;display:grid;place-items:center;padding:20px}.demo-confirm section{background:var(--bg,#fff);color:var(--ink,#222);border-radius:16px;padding:24px;max-width:540px;box-shadow:0 16px 60px #0005}.demo-confirm p{white-space:pre-line;line-height:1.7}.demo-confirm section>div{display:flex;gap:12px;justify-content:flex-end}.demo-note{position:fixed;left:50%;bottom:84px;transform:translateX(-50%);z-index:9999;max-width:min(560px,92vw);padding:12px 16px;border-radius:12px;background:var(--accent);color:#fff;font-size:14px;box-shadow:0 8px 28px rgba(0,0,0,.18)}";
    document.head.appendChild(css);
  });
})();
