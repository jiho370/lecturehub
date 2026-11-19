// Web trial: the real Lecture Hub screen in a browser. Read calls answer with responses recorded from the real app
// on the synthetic trial semester; recording, live transcript and note making are played back (20x speed).
// Nothing leaves the browser.
(function () {
  const CALLS = window.__DEMO_CALLS || {};
  const SPEED = 20;  // one second here = 20 seconds of lecture
  const APP_ONLY = { ok: false, error: "웹 체험판에서는 이 기능을 쓸 수 없어요 · 설치한 앱에서 해 보세요" };
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const key = (name, args) => name + JSON.stringify(args);
  const byName = {};
  for (const k of Object.keys(CALLS)) { const n = k.slice(0, k.indexOf("[")); if (!(n in byName)) byName[n] = CALLS[k]; }

  // ---- the trial lecture, its note, its transcript
  const lectureKey = Object.keys(CALLS).find((k) => k.startsWith("lecture["));
  const LECTURE = lectureKey ? CALLS[lectureKey].lecture : null;
  const LECTURE_ID = LECTURE && LECTURE.id;
  const NOTES = LECTURE ? clone(LECTURE.notes || []) : [];
  NOTES.forEach((n) => { if (n.exists && !n.url) n.url = "demo:note"; });  // recorded without the local media server
  const SEGMENTS = (LECTURE && LECTURE.segments || []).map((s) => ({ ms: s.start_ms, text: s.text }));
  const LAST_MS = SEGMENTS.length ? SEGMENTS[SEGMENTS.length - 1].ms + 25000 : 600000;

  const saved = (() => { try { return JSON.parse(sessionStorage.getItem("lh-demo") || "{}"); } catch (e) { return {}; } })();
  const sim = { noteMade: !!saved.noteMade, phase: "idle", t0: 0, pausedAt: 0, pausedMs: 0, cap: false, job: null, done: null };
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
    if (sim.phase === "idle") return base;
    const ms = elapsed();
    if (sim.phase === "recording" && ms >= LAST_MS) stop();  // the synthetic class is over
    const course = (LECTURE && LECTURE.course) || "";
    if (sim.phase === "starting" || sim.phase === "recording") {
      if (sim.phase === "starting" && now() - sim.t0 > 900) sim.phase = "recording";
      return { ...base, phase: sim.phase, status: { course, elapsed_ms: ms, rms: sim.pausedAt ? null : -32 + Math.sin(now() / 170) * 7 + Math.random() * 4,
        device: "데모 마이크", mode: "mic", paused: !!sim.pausedAt, dropped_samples: 0 } };
    }
    const since = now() - sim.stoppedAt;
    if (since < 900) return { ...base, phase: "stopping" };
    if (since < 2200) return { ...base, phase: "finalizing", summary: { total_ms: sim.total } };
    return { ...base, phase: "done", summary: { total_ms: sim.total, dropped_samples: 0, session_dir: "C:\\Users\\me\\AppData\\Roaming\\lecturehub\\체험 학기\\_lecturehub\\recordings\\demo", master: "demo.mp3" },
      link: { state: "linked", media_id: "demo-media", lecture_id: LECTURE_ID, confidence: "high", reason: "시간표의 수업 시간과 맞음" },
      transcription: { state: "done", label: "완료 · Google (체험판은 미리 만든 전사)" } };
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

  // ---- note making playback: the real result (with one invented sentence caught) appears after a few seconds
  function makeNote() {
    sim.job = { state: "running", lecture_id: LECTURE_ID, at: now() };
    return { ok: true };
  }
  function noteStatus() {
    const j = sim.job;
    if (!j) return { state: "idle" };
    if (now() - j.at < 6000) return { state: "running", lecture_id: j.lecture_id };
    sim.noteMade = true; persist();
    const res = { state: "done", lecture_id: j.lecture_id, html: NOTES[0] && NOTES[0].path, rejected: 1, blocks: 8 };
    sim.job = null;
    setTimeout(() => toastOnce("근거 검사: 전사에 없는 말이 든 문장 1개를 빼고 저장했어요 (체험용으로 일부러 넣은 문장)"), 600);
    return res;
  }
  function toastOnce(text) {
    const t = document.createElement("div");
    t.className = "demo-note";
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 7000);
  }

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
  const conn = {};  // provider -> true
  function connStatus() {
    const v = clone(CALLS["conn_status[]"] || { ok: true, keys: [] });
    (v.keys || []).forEach((k) => { k.connected = !!conn[k.id]; k.source = conn[k.id] ? "keyring" : null; });
    return v;
  }
  function connConnect(provider, key) {
    if (String(key || "").trim().length < 8) return { ok: false, error: "키가 너무 짧아요 (체험판: 아무 글자 8자 이상이면 연결된 것으로 보여 줘요)" };
    conn[provider] = true;
    return { ok: true };
  }

  // ---- eTL: connected after a token is "connected" above; [지금 가져오기] fills synthetic notices, deadlines, files
  const etl = { synced: 0 };
  const iso = (d) => d.toISOString().slice(0, 10);
  const day = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return d; };
  const COURSE = (LECTURE && LECTURE.course) || "유체역학 체험";
  function etlBoard() {
    if (!etl.synced) return clone(CALLS["etl_board[]"] || { ok: true, announcements: [], files: [], have: 0, total: 0, closed: [] });
    return { ok: true, at: etl.synced / 1000, have: 2, total: 3, closed: [],
      announcements: [
        { id: "a1", course: COURSE, posted: iso(day(-1)) + "T18:20", title: "쪽지 퀴즈 안내", text: "다음 주 수요일 수업 시작 10분 동안 오늘 배운 손실수두 계산으로 쪽지 퀴즈를 봅니다. 계산기를 가져오세요.", attachments: [] },
        { id: "a2", course: COURSE, posted: iso(day(-3)) + "T09:02", title: "과제 1 공지", text: "관수로 마찰손실 문제 3개. 풀이 과정과 단위를 모두 쓰세요.", attachments: ["과제1_관수로.pdf"] },
      ],
      files: [{ id: 101, course: COURSE, name: "3주차_관수로흐름.pdf", size: 2400000, updated: iso(day(-1)) + "T17:55", state: "missing", big: false }] };
  }
  function etlSync() {
    if (!conn.etl) return { ok: false, error: "먼저 설정 › 계정 연결에서 eTL 토큰을 연결하세요 (체험판: 아무 글자 8자 이상)" };
    etl.synced = Date.now();
    return { ok: true, count: 1, added: 1, updated: 0, board: { announcements: 2, files: 1 } };
  }
  const extraEvents = [];
  function calendar(year, month) {
    const v = clone(CALLS[key("calendar", [year, month])] || byName.calendar || { ok: true, events: [], upcoming: [] });
    if (v.ok === undefined) v.ok = true;
    v.year = year; v.month = month;
    const mine = extraEvents.slice();
    if (etl.synced) mine.push({ id: "etl-1", date: iso(day(9)), title: "과제 1 관수로 마찰손실", kind: "assignment", course: COURSE, source: "etl", status: "todo", time: "23:59" });
    v.events = (v.events || []).concat(mine.filter((e) => e.date && e.date.startsWith(`${year}-${String(month).padStart(2, "0")}`)));
    v.upcoming = (v.upcoming || []).concat(mine.filter((e) => e.date >= iso(new Date())));
    return v;
  }

  // ---- 그스룸 (study room) booking: the same screen; booking/return/cancel run as a short fake job
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
    room.log.unshift({ ts: now, kind: j.action === "list" ? "list" : j.action, room: result.room || "", ok: true, by: "app", detail: "체험판" });
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

  const LIVE = {
    conn_status: connStatus, conn_connect: connConnect, conn_test: (p) => (conn[p] ? { ok: true } : { ok: false, error: "연결된 키가 없어요" }),
    conn_remove: (p) => { delete conn[p]; return { ok: true }; },
    conn_open_help: (p) => { const k = (CALLS["conn_status[]"].keys || []).find((x) => x.id === p); if (k && k.help_url) window.open(k.help_url, "_blank", "noopener"); return { ok: true }; },
    etl_status: () => (conn.etl ? { ok: true, connected: true, ok_at: etl.synced ? etl.synced / 1000 : null, count: etl.synced ? 1 : 0 } : { ok: true, connected: false }),
    etl_board: etlBoard, etl_sync: etlSync,
    etl_file_take: () => ({ ok: false, error: "체험판은 파일을 내려받지 않아요 · 앱에서는 과목 폴더의 '강의자료'로 받아요" }),
    etl_file_keep: () => ({ ok: true }),
    calendar, calendar_add: (v) => { extraEvents.push(Object.assign({ id: `me-${extraEvents.length + 1}`, source: "user", status: "todo" }, v)); return { ok: true }; },
    calendar_status: () => ({ ok: true }), calendar_update: () => ({ ok: true }),
    room: roomView, room_refresh: () => roomJob("list", "예약 목록 확인", []),
    room_book: (d, s, u, r, h) => roomJob("book", "예약", [d, s, u, r, h]),
    room_cancel: (ids) => roomJob("cancel", "예약 취소", [ids]), room_return: (id) => roomJob("return", "반납", [id]),
    room_stop: () => { room.job = { state: "stopped", action: room.job.action }; return { ok: true }; },
    room_chat: roomChat, room_chat_confirm: roomChatConfirm, room_rules: () => ({ ok: true }),
    gsr_rooms_set: () => ({ ok: true }), lab_set: () => ({ ok: true }), gsr_companions_set: () => ({ ok: true }),
    clova_sync: () => ({ ok: true, result: { message: "체험판: Clova Note에서 새 녹음 1개를 찾은 것처럼 보여 줘요 (앱에서는 Aside 브라우저로 내려받아요)" } }),
    state, start, stop, pause: (flag) => pause(!!flag),
    captions_check: () => ({ ok: true, needs_consent: false, once_only: false }),
    captions_start: () => { sim.cap = true; return { ok: true }; },
    captions_status: captions,
    captions_stop: () => { sim.cap = false; return { ok: true }; },
    captions_pause: (flag) => pause(!!flag),
    recording_permission: () => ({ ok: true, allowed: true }),
    consent_confirm: () => ({ ok: true, yes: window.confirm("정리본을 만들 때는 이 과목의 전사본 글을 고른 AI(학교 ChatGPT Edu 계정의 Codex)로 보냅니다. 소리는 보내지 않습니다.\n\n(웹 체험판은 실제로 보내지 않고, 미리 만든 결과를 보여 줘요.)"), token: "demo" }),
    make_note: makeNote, note_status: noteStatus,
    search: (q) => search(q),
    probe: () => ({ ok: true }),
    suggest_course: () => ({ course: COURSE }),  // the trial timetable's class, whatever day the page is opened
    sidebar_state: () => ({ ok: true }), set_selection: () => ({ ok: true }), notices_seen: () => ({ ok: true }),
    class_reminder_set: () => ({ ok: true }), show_mini: () => ({ ok: true }),
  };
  function answer(name, args) {
    if (name in LIVE) return LIVE[name](...args);
    if (name === "init") {
      const v = clone(CALLS["init[]"]);
      v.trial = false;  // the web bar replaces the app's "내 학기 폴더 정하기" bar
      v.labs = Object.assign({}, v.labs, { gsr: true });  // the study-room booking (a labs feature) is on to try
      v.config_error = null;  // recorded before the trial wrote recorder.toml (fixed in the app)
      v.features = Object.assign({}, v.features, { captions: { state: "ready" } });
      v.course = (LECTURE && LECTURE.course) || v.course;
      return v;
    }
    const exact = CALLS[key(name, args)];
    if (exact !== undefined) return withNotes(exact);
    if (name in byName) return withNotes(byName[name]);
    return APP_ONLY;
  }
  window.pywebview = { api: new Proxy({}, { get(_, name) {
    if (typeof name !== "string" || name === "then") return undefined;
    return (...args) => new Promise((resolve) => setTimeout(() => {
      const out = clone(answer(name, args) ?? { ok: true });
      (window.__demoLog || (window.__demoLog = [])).push([name, out && out.ok === false ? out.error || out.reason : ""]);
      if (window.__demoLog.length > 300) window.__demoLog.shift();
      resolve(out);
    }, 40));
  } }) };

  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.className = "trial-bar";
    bar.innerHTML = "<span><b>웹 체험판</b> · 실제 Lecture Hub 화면이에요. 오른쪽 아래 <b>녹음 시작</b>을 누르면 합성 수업이 20배 빠르게 흘러가며 실시간 전사가 나오고, 끝나면 회차를 열어 <b>정리본 만들기</b>까지 해 볼 수 있어요. 아무것도 밖으로 보내지 않아요.</span><button id=\"demoReset\">처음부터</button>";
    document.body.prepend(bar);
    bar.querySelector("#demoReset").onclick = () => { try { sessionStorage.clear(); localStorage.clear(); } catch (e) { /* */ } location.reload(); };
    const css = document.createElement("style");
    css.textContent = ".demo-note{position:fixed;left:50%;bottom:84px;transform:translateX(-50%);z-index:9999;max-width:min(560px,92vw);padding:12px 16px;border-radius:12px;background:var(--accent);color:#fff;font-size:14px;box-shadow:0 8px 28px rgba(0,0,0,.18)}";
    document.head.appendChild(css);
  });
})();
