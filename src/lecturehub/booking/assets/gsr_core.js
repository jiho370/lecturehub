// 관정관 4층 그룹스터디룸(6인실) 예약 코어 — P-019.
// booking/claude_cli.py가 Claude in Chrome으로 k-rsv.snu.ac.kr 탭에 주입한다(앱은 브라우저를 직접 조작하지 않음, D-003).
// 사이트가 수동 예약 때 부르는 요청 순서를 그대로 따른다:
//   getRoomList → roomForm → getUserCheck(동반자별) → setRoomBooking
// 반납은 예약취소와 같은 API(setCancelBooking)라서 목록을 새로 받아 사용중(3)·반납 가능일 때만 보낸다.
// 예약 취소(예약완료만)도 같은 API라서 cancelBookings가 상태를 새로 확인한 뒤에만 보낸다.
// 길이는 30분 단위로 30분~12시간(hours: 0.5·1·1.5…). 사이트는 한 번에 3시간까지라 3시간을 넘으면 블록으로 이어서 예약한다.
// 시작·끝 시각의 한계는 사이트가 준 방 목록(OPENTIME·CLOSETIME·ABLETIME)이 정한다 — 결과의 site_hours로 앱에 알려 준다.
// 호출: JSON.stringify(await GSR.act({action: 'list' | 'book' | 'return' | 'cancel', ...}))
(() => {
  const CFG = {
    companions: [],          // [이름, 학번] 목록은 예약 화면에서 그때그때 적어 req.companions로 들어온다(저장하지 않는다). 신청인(로그인 계정)은 자동으로 맨 앞에 들어간다
    // 구역(sector) 목록: 4층 우선, 2층은 보조(4명 가능한 12103만. 12101 G201 최소 11명·12102 G203/207/208 최소 7명은 제외)
    sectors: [
      { roomNo: '141', sectorNo: '14103', sectorName: '6인실', floor: 4 },   // 4층 6인실 (14101 = 402/403/423/425, 14102 = 401은 제외)
      { roomNo: '121', sectorNo: '12103', sectorName: '6인실', floor: 2 },   // 2층 G204/205/206/211 (최소 4명)
    ],
    monitorRooms: ['G405', 'G407', 'G411', 'G413'],  // TV 모니터가 있는 4층 방(시설 정보, 사이트로 미확인)
    slackMin: 30,            // 예약이 끝난 뒤에도 이만큼(분) 이상 비어 있으면 '뒤에 여유 있음'
    // 방 고르는 취향(req.rooms, useRoomPrefs)은 앱이 보내지 않는다. 기본은 취향 없음.
    preferFloor: null,       // 이 층을 먼저(예: 4)
    endRooms: [],            // 다른 방이 있으면 뒤로 미룰 방(예: 양끝 방) — 뒤에 여유가 있어도, 모니터 방이어도
    evenFirst: false,        // 짝수 번호 방을 먼저(층 다음)
    monitorFirst: false,     // 모니터 방을 먼저
    center: null,            // 이 방 번호(예: 416.5)에 가까운 방부터 — 같은 층에서만
    exclude: ['G431'],       // 개방형(예약 없이 선착순)
    dur: 180,                // 기본 3시간·한 번에 최대 3시간(사이트 규정). 원하는 길이가 안 되면 같은 시작에서 가장 긴 시간으로 줄여 예약
    minDur: 30,
  };
  const BASE = '/NEW_SNU_BOOKING';

  const hhmmOf = (x) => { const d = String(x || '').replace(/\D/g, ''); return d.length >= 4 ? d.slice(0, 4) : ''; };
  const durText = (m) => (m % 60 === 0 ? `${m / 60}시간` : m < 60 ? `${m}분` : `${Math.floor(m / 60)}시간 ${m % 60}분`);
  let seenHours = null;   // 사이트가 방 목록에 적은 운영 시각 [열기, 닫기] ('HHMM'), 마지막으로 본 값
  const toMin = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(2, 4));
  const toHHMM = (m) => String(Math.floor(m / 60)).padStart(2, '0') + String(m % 60).padStart(2, '0');
  const ymd = (d) => d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
  const slotsOf = (start, dur) => {
    const s = toMin(start), out = [];
    for (let m = s; m < s + dur; m += 30) out.push(toHHMM(m));
    return out;
  };

  async function post(path, params, asJson = true) {
    const r = await fetch(BASE + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'OS_KIND': 'WEB',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: new URLSearchParams(params).toString(),
    });
    if (r.status === 999) throw new Error('로그인이 만료됐습니다');
    const t = await r.text();
    if (!asJson) return t;
    try { return JSON.parse(t); }
    catch (e) { throw new Error('서버 응답을 읽지 못했습니다 (로그인 만료 가능성)'); }
  }

  // 4층 방을 먼저, 그다음 2층 방(방 이름순). 어느 방을 먼저 잡을지는 orderRooms가 시각·길이마다 정한다. 2층 조회가 실패해도 4층만으로 진행한다.
  async function getRooms(date) {
    const all = [];
    for (const sec of CFG.sectors) {
      let j;
      try {
        j = await post('/json/getRoomList', { roomNo: sec.roomNo, sectorNo: sec.sectorNo, searchDate: date });
        if (j.successYN !== 'Y') throw new Error(j.msg || '빈 방 조회 실패');
      } catch (e) {
        if (sec.floor === CFG.sectors[0].floor) throw e;
        continue;
      }
      all.push(...(j.BookingTimeList || [])
        .filter((r) => !CFG.exclude.includes(r.NAME))
        .map((r) => ({
          seat: r.SEATNO, name: r.NAME, open: r.OPENTIME, close: r.CLOSETIME, sec,
          able: new Set((r.ABLETIME || '').split(',').filter(Boolean).map((s) => s.slice(11, 16).replace(':', ''))),
        }))
        .sort((a, b) => a.name.localeCompare(b.name)));
    }
    const opens = all.map((r) => hhmmOf(r.open)).filter(Boolean).sort(), closes = all.map((r) => hhmmOf(r.close)).filter(Boolean).sort();
    if (opens.length && closes.length) seenHours = [opens[0], closes[closes.length - 1]];
    return all;
  }

  const fits = (room, start, dur) => slotsOf(start, dur).every((s) => room.able.has(s));

  // ---- 방 고르는 순서: (설정한 층 먼저) → (짝수 방 먼저) → (미룰 방은 맨 뒤) → 뒤에 여유 있는 방 → (모니터 방) → (설정한 방 번호에 가까운 방) → 이름순.
  // 괄호 안은 설정(req.rooms)이 있을 때만 쓴다.
  // 순수 함수: 방 목록·시작·길이만 보고 정한다(네트워크·시계 없음). rooms = {name, able:Set, sec:{floor}}.
  const roomNo = (name) => Number(String(name).replace(/\D/g, ''));
  // 예약이 끝난 뒤에도 slackMin분 이상 이어서 비어 있나
  const hasSlack = (room, start, dur) => {
    const end = toMin(start) + dur;
    for (let m = end; m < end + CFG.slackMin; m += 30) if (!room.able.has(toHHMM(m))) return false;
    return true;
  };
  // dur가 null이면 '뒤에 여유'는 보지 않는다(줄여 잡을 때의 동률 정리용)
  function rankRooms(rooms, start, dur) {
    const centerFloor = CFG.center == null ? null : Math.floor(CFG.center / 100);
    const key = (r) => [
      CFG.preferFloor == null || r.sec.floor === CFG.preferFloor ? 0 : 1,
      CFG.evenFirst && roomNo(r.name) % 2 !== 0 ? 1 : 0,   // (짝수 방 먼저) — 미룰 방·뒤 여유·모니터 방보다 앞(소유자 설정, 사용자 2026-11-09·11-14)
      CFG.endRooms.includes(r.name) ? 1 : 0,
      dur != null && hasSlack(r, start, dur) ? 0 : 1,
      CFG.monitorFirst && CFG.monitorRooms.includes(r.name) ? 0 : 1,
      r.sec.floor === centerFloor ? Math.abs(roomNo(r.name) - CFG.center) : 0,
    ];
    return rooms.map((r) => ({ r, k: key(r) })).sort((a, b) => {
      for (let i = 0; i < a.k.length; i++) if (a.k[i] !== b.k[i]) return a.k[i] - b.k[i];
      return a.r.name.localeCompare(b.r.name);
    }).map((x) => x.r);
  }
  const orderRooms = (rooms, start, dur) => rankRooms(rooms.filter((r) => fits(r, start, dur)), start, dur);

  // start부터 이어서 비어 있는 시간(분), 최대 maxDur
  const freeRun = (room, start, maxDur) => {
    let d = 0;
    while (d < maxDur && room.able.has(toHHMM(toMin(start) + d))) d += 30;
    return d;
  };

  // 원하는 시각에 방이 없을 때: 길이는 그대로 두고 가까운 시작시각 순으로 최대 limit개
  function alternatives(rooms, start, dur, limit = 6) {
    const out = [], s0 = toMin(start);
    const opens = rooms.map((r) => toMin(hhmmOf(r.open) || '0900')), closes = rooms.map((r) => toMin(hhmmOf(r.close) || '2300'));
    const lo = opens.length ? Math.min(...opens) : 540, hi = closes.length ? Math.max(...closes) : 1380;   // 사이트가 준 운영 시각
    for (let off = 30; off <= 840 && out.length < limit; off += 30) {
      for (const m of [s0 + off, s0 - off]) {
        if (m < lo || m + dur > hi || out.length >= limit) continue;
        const room = orderRooms(rooms, toHHMM(m), dur)[0];
        if (room) out.push({ room: room.name, start: toHHMM(m), end: toHHMM(m + dur) });
      }
    }
    return out;
  }

  // 내 예약. 서버는 신청일(REGTIME) 기준으로 거른다. status: 2 예약완료 · 3 사용중 · 4 좌석미반납 · 5 본인반납
  async function myList(days = 14) {
    const now = new Date(), s = new Date(now);
    s.setDate(s.getDate() - days);
    const j = await post('/json/getMyBookingList', { start: ymd(s), end: ymd(now) });
    if (j.successYN !== 'Y') throw new Error(j.msg || '예약 목록 조회 실패');
    return (j.bookingList || []).map((b) => ({
      id: String(b.BSEQNO), seat: b.SEAT_VNAME, start: b.USESTART, end: b.USEEXPIRE,
      status: Number(b.STATUS), statusName: b.STATUS_NAME, returnable: b.RETURN_YN === 'Y',
    }));
  }

  async function book(room, date, start, useDesc, dur = CFG.dur) {
    // 1) 예약 폼(수동 예약 때 여는 페이지)에서 신청인 정보·hidden 필드 확보
    const html = await post('/pc/booking/roomForm', {
      searchDate: date, roomNo: room.sec.roomNo, sectorNo: room.sec.sectorNo, seatNo: room.seat,
      TIME_START: room.open, TIME_END: room.close, ABLETIME: [...room.able].join(','),
      NAME: room.name, sectorName: room.sec.sectorName, roomName: '그룹스터디룸', tab_on: 'tabBooking', libNo: '1',
    }, false);
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const form = doc.getElementById('bookingFrm');
    if (!form) throw new Error('예약 폼을 불러오지 못했습니다 (로그인 만료 또는 예약 불가 상태)');
    const fields = {};
    form.querySelectorAll('input').forEach((i) => { if (i.name) fields[i.name] = i.value; });
    const phone = doc.querySelector('.phoneNum')?.value || '';
    const email = doc.querySelector('.eMail')?.value || '';
    if (!phone || !email) throw new Error('사이트에 연락처/이메일이 비어 있습니다. 한 번 수동으로 입력해 두세요');
    const selfName = (/tmpUserNames\s*=\s*["']([^"']*)/.exec(html) || [])[1];
    const selfNo = (/tmpSchoolNos\s*=\s*["']([^"']*)/.exec(html) || [])[1];
    if (!selfName || !selfNo) throw new Error('신청인 정보를 찾지 못했습니다');

    // 2) 동반이용자 확인 (사이트의 '동반이용자 추가' 버튼과 같은 요청)
    const members = [[selfName, selfNo]];
    for (const [n, no] of CFG.companions) {
      if (no === selfNo) continue;
      const j = await post('/json/getUserCheck', { pRoomNo: room.sec.roomNo, pUserName: n, pSchoolNo: no });
      if (j.successYN !== 'Y') throw new Error(`동반이용자 확인 실패: ${n} (${j.msg || '사유 미상'})`);
      members.push([j.useCheckInfo.NAME, j.useCheckInfo.SCHOOLNO]);
    }
    if (members.length < 4) throw new Error(`최소 4명 필요 (현재 ${members.length}명) — 예약 화면의 '함께 쓰는 사람'에 이름과 학번을 적어 주세요`);

    // 3) 예약
    const end = toHHMM(toMin(start) + dur);
    Object.assign(fields, {
      bookTime: `${start}-${end}`, bookDate: date, useDesc, phoneNum: phone, eMail: email,
      sMemberJsInfo: members.map((m) => m.join(',')).join(','),
    });
    if ('memberCnt' in fields) fields.memberCnt = String(members.length);
    if ('unitCnt' in fields) fields.unitCnt = String(dur / 30);
    const j = await post('/json/setRoomBooking', fields);
    const base = { room: room.name, date, start, end };
    if (j.successYN !== 'Y') {
      const f = j.bookingFailInfo;
      const why = f && f.BSEQNO ? `이미 예약 있음 — ${f.USER_NAME || ''} ${f.USESTART || ''}~${f.USEEXPIRE || ''}` : (j.msg || '예약 실패');
      return { ok: false, ...base, why };
    }
    const at = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6)} ${start.slice(0, 2)}:${start.slice(2)}:00`;
    const mine = await myList(1).catch(() => []);
    const hit = mine.find((b) => b.seat === room.name && b.start === at);
    return { ok: true, ...base, id: hit ? hit.id : null, members: members.map((m) => m[0]) };
  }

  // 3시간을 넘는 예약(3.5·4·6·9·12시간…): 한 번에 3시간까지라 3시간 블록을 이어서 예약하고 마지막 블록만 짧을 수 있다.
  // 전체가 비는 한 방이 먼저, 없으면 블록마다 빈 방(앞 블록과 같은 방 우선). 모든 블록에 빈 방이 있을 때만 시작하고,
  // 사이트가 중간 블록을 거절하면 거기서 멈춘다(앞 블록은 남는다 → partial).
  async function runChain(pool, rooms, date, start, useDesc, total) {
    const hours = total / 60;
    const parts = [];   // [시작, 길이(분)]
    for (let m = 0; m < total; m += CFG.dur) parts.push([toHHMM(toMin(start) + m), Math.min(CFG.dur, total - m)]);
    const end = toHHMM(toMin(start) + total);
    const whole = orderRooms(pool, start, total)[0];
    const plan = [];
    for (const [s, d] of parts) {
      const prev = plan.length ? plan[plan.length - 1] : null;
      const r = whole || (prev && fits(prev, s, d) ? prev : orderRooms(pool, s, d)[0]);
      if (!r) {
        return { ok: false, date, start, end, hours,
          why: `${s.slice(0, 2)}:${s.slice(2)}부터 ${durText(d)} 빈 방이 없습니다 (${durText(total)} 연속 불가, 예약하지 않음)`,
          alternatives: alternatives(rooms, start, CFG.dur) };
      }
      plan.push(r);
    }
    const blocks = [];
    for (let i = 0; i < parts.length; i++) {
      let res;
      try { res = await book(plan[i], date, parts[i][0], useDesc, parts[i][1]); }
      catch (e) { res = { ok: false, why: e.message }; }
      if (!res.ok) {
        return { ok: false, partial: blocks.length > 0, date, start, end, hours, blocks,
          room: [...new Set(blocks.map((b) => b[0]))].join('·'),
          why: `${i + 1}번째 블록(${parts[i][0].slice(0, 2)}:${parts[i][0].slice(2)}) 실패: ${res.why}` };
      }
      blocks.push([res.room, res.start, res.end, res.id]);
    }
    return { ok: true, date, start, end, hours, blocks, room: [...new Set(blocks.map((b) => b[0]))].join('·') };
  }

  // 조건(날짜·시작·길이, 선택: 방)에 맞는 빈 방 중 고르는 순서(orderRooms)의 첫 방을 예약. 없으면 가장 길게 비는 방을 그 길이만큼
  async function run(req = {}) {
    useCompanions(req);
    const { date, start, useDesc = '과제', room: want, hours = 3 } = req;
    const minutes = Math.round(Number(hours) * 60);
    if (Number(req.maxHours) === 3 && minutes > 180) throw new Error('공개판 예약은 최대 3시간입니다');
    if (!(minutes >= CFG.minDur && minutes <= 720 && minutes % 30 === 0)) throw new Error(`길이는 30분 단위로 30분~12시간입니다 (${hours})`);
    const rooms = await getRooms(date);
    const pool = rooms.filter((r) => !want || r.name === want);
    if (minutes > CFG.dur) return runChain(pool, rooms, date, start, useDesc, minutes);
    const dur = minutes;
    const cand = orderRooms(pool, start, dur); // 4층 → 양끝 방은 뒤로 → 뒤에 여유 → 모니터 → 가운데
    if (!cand.length) {
      if (Number(req.maxHours) === 3) {
        return { ok: false, date, start, why: '확인한 방과 시각에 예약할 수 없습니다. 다른 시각을 골라 다시 확인하세요', alternatives: alternatives(rooms, start, dur) };
      }
      // 원하는 길이가 안 되면 같은 시작 시각에서 가장 길게 이어지는 방. 4층에서 먼저 찾고, 없을 때만 2층(동률이면 고르는 순서의 앞 방)
      let best = null, bestDur = 0;
      for (const floor of [...new Set(CFG.sectors.map((x) => x.floor))]) {
        for (const r of rankRooms(pool.filter((x) => x.sec.floor === floor), start, null)) {
          const d = freeRun(r, start, dur);
          if (d > bestDur) { best = r; bestDur = d; }
        }
        if (best) break;
      }
      if (best && bestDur >= CFG.minDur) {
        const res = await book(best, date, start, useDesc, bestDur);
        // 줄여 잡았으니, 원하는 길이가 되는 다른 시작 시각도 추천으로 같이 돌려준다
        return { ...res, shortened: true, minutes: bestDur, requested: dur, alternatives: alternatives(rooms, start, dur) };
      }
      return { ok: false, date, start, why: '그 시각에 빈 방이 없습니다', alternatives: alternatives(rooms, start, dur) };
    }
    return book(cand[0], date, start, useDesc, dur);
  }

  async function returnBooking(id) {
    const b = (await myList()).find((x) => x.id === String(id));
    if (!b) return { ok: false, id: String(id), why: '예약을 찾지 못했습니다' };
    if (b.status !== 3 || !b.returnable) {
      return { ok: false, skipped: true, id: b.id, room: b.seat, why: `반납하지 않음 (상태: ${b.statusName})` };
    }
    const j = await post('/json/setCancelBooking', { bseqno: b.id });
    return j.successYN === 'Y' ? { ok: true, id: b.id, room: b.seat } : { ok: false, id: b.id, room: b.seat, why: j.msg || '반납 실패' };
  }

  // 예약 취소: 예약완료(2)인 예약만. 사용중(3)은 반납(returnBooking)이고 그 밖의 상태는 건드리지 않는다. 한 건이 막히면 거기서 멈춘다.
  // cancelled 행: [방, 'YYYY-MM-DD HH:MM', 끝 'HH:MM', id]
  async function cancelBookings(ids) {
    const list = await myList();
    const cancelled = [];
    for (const id of ids.map(String)) {
      const b = list.find((x) => x.id === id);
      if (!b) return { ok: false, cancelled, id, why: `예약 ${id}를 찾지 못했습니다` };
      if (b.status !== 2) {
        return { ok: false, skipped: cancelled.length === 0, cancelled, id: b.id, room: b.seat,
          why: `취소하지 않음 (상태: ${b.statusName})${b.status === 3 ? ' — 사용중이면 반납을 쓰세요' : ''}` };
      }
      const j = await post('/json/setCancelBooking', { bseqno: b.id });
      if (j.successYN !== 'Y') return { ok: false, cancelled, id: b.id, room: b.seat, why: j.msg || '취소 실패' };
      cancelled.push([b.seat, b.start.slice(0, 16), b.end.slice(11, 16), b.id]);
    }
    return { ok: true, cancelled, room: [...new Set(cancelled.map((c) => c[0]))].join('·') };
  }

  // Claude의 JS 도구는 긴 결과를 자르므로(약 1.5천 자) 최근 예약만 짧은 행으로: [id, 방, 시작, 끝, 상태, 반납가능, 상태이름]
  function compact(list, limit = 10) {
    const since = new Date(Date.now() - 3 * 86400000);
    const iso = `${ymd(since).slice(0, 4)}-${ymd(since).slice(4, 6)}-${ymd(since).slice(6)}`;
    return list.filter((b) => b.end.slice(0, 10) >= iso)
      .sort((a, b) => (a.start < b.start ? 1 : -1)).slice(0, limit)
      .map((b) => [b.id, b.seat, b.start.slice(0, 16), b.end.slice(0, 16), b.status, b.returnable ? 1 : 0, b.statusName]);
  }

  function useCompanions(req) {
    if (!req || !Array.isArray(req.companions)) return;
    CFG.companions = req.companions.filter((c) => Array.isArray(c) && c.length === 2 && c[0] && /^\d{6,12}$/.test(String(c[1])))
      .map((c) => [String(c[0]), String(c[1])]);
  }

  // 방 취향: {floor_first: 4 | null, avoid: ['G404'], prefer_monitor: bool, center: 416.5 | null}. 없는 칸은 그대로 둔다.
  function useRoomPrefs(prefs) {
    if (!prefs || typeof prefs !== 'object') return;
    if ('floor_first' in prefs) CFG.preferFloor = Number.isInteger(prefs.floor_first) ? prefs.floor_first : null;
    if (Array.isArray(prefs.avoid)) CFG.endRooms = prefs.avoid.filter((n) => /^G\d{3}$/.test(String(n))).map(String);
    if ('even_first' in prefs) CFG.evenFirst = !!prefs.even_first;
    if ('prefer_monitor' in prefs) CFG.monitorFirst = !!prefs.prefer_monitor;
    if ('center' in prefs) CFG.center = Number.isFinite(prefs.center) ? prefs.center : null;
  }

  // 한 번의 호출로 작업 + 최신 예약 목록을 돌려준다(앱의 status.json 갱신용)
  async function act(req = {}) {
    let out = { ok: false, action: req.action };
    useCompanions(req);
    useRoomPrefs(req.rooms);
    try {
      if (req.action === 'list') {
        out.ok = true;
        try { await getRooms(ymd(new Date())); } catch (e) { /* 운영 시각을 못 읽어도 목록은 돌려준다 */ }
      }
      else if (req.action === 'book') out = { ...out, ...(await run(req)) };
      else if (req.action === 'return') out = { ...out, ...(await returnBooking(req.id)) };
      else if (req.action === 'cancel') {
        const ids = (req.ids || (req.id ? [req.id] : [])).map(String).filter((s) => /^\d+$/.test(s));
        if (!ids.length) throw new Error('취소할 예약 번호가 없습니다');
        out = { ...out, ...(await cancelBookings(ids)) };
      }
      else throw new Error(`알 수 없는 작업: ${req.action}`);
    } catch (e) {
      out.ok = false;
      out.error = e.message;
      out.login_required = /로그인/.test(e.message);
    }
    try { out.bookings = compact(await myList()); } catch (e) { out.bookings_error = e.message; }
    delete out.members;
    if (seenHours) out.site_hours = seenHours;
    return out;
  }

  window.GSR = { CFG, getRooms, fits, orderRooms, rankRooms, alternatives, durText, myList, book, run, returnBooking, cancelBookings, act, useRoomPrefs };
})();
