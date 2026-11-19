// MAI playground에 조각 하나를 올리고 결과를 읽는 aside repl 스크립트(mai_aside.build가 한 줄로 합쳐 넘긴다).
// 규칙: 한 줄 주석만 쓴다(줄 끝 주석 금지), 문장은 세미콜론으로 끝낸다. 자리표시: __NAME__(조각 파일 이름), __FETCH__(이 PC 안 임시 주소).
// Aside의 fs·setInputFiles는 세션 폴더 밖 파일을 열지 못한다: 앱이 127.0.0.1에 그 조각 하나만 내주고, 여기서 받아 세션 폴더에 쓴 뒤 첨부한다.
// 하는 일: 로그인 확인(풀려 있으면 Log in → 저장된 계정 하나를 클릭만) → Verbatim 확인 → 첨부 → 칩과 'Submit audio'가 둘 다 있을 때만 전송 → 결과 읽기.
// 하지 않는 일: 값 입력(비밀번호 칸·계정 여러 개·추가 인증이면 멈춤), Clean 모드, 마이크('Start recording') 버튼, 결과 고치기.
// 결과: RESULT: {ok, login_required?, err?(refused|signin|couldnt_read|submit_check|timeout|empty|not_ready), error?, logged_in?, secs, blocks, text}
let maOut = {ok: false}; let maTab = null; const maT0 = Date.now(); try {
const maName = __NAME__;
const maUrl = 'https://playground.microsoft.ai/chat?model=mai-transcribe-2';
const maRun = (fn, arg) => maTab.evaluate('(' + fn.toString() + ')(' + JSON.stringify(arg === undefined ? null : arg) + ')');
const maState = () => { const vis = (e) => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; }; const tiles = [...document.querySelectorAll('[role=button],button')].filter(vis).filter((e) => /@/.test(e.getAttribute('data-test-id') || '') || /@/.test(e.getAttribute('aria-label') || '')).filter((e) => !/다른 계정|another account/i.test(e.innerText || '')); return {url: location.href, vis: document.visibilityState, pw: [...document.querySelectorAll('input[type=password]')].some(vis), login: [...document.querySelectorAll('button')].some((b) => vis(b) && ((b.getAttribute('aria-label') || '') === 'Log in' || (b.innerText || '').trim() === 'Log in')), tiles: tiles.length, verbatim: (document.querySelector('input[value=verbatim]') || {}).checked === true, vb: !!document.querySelector('input[value=verbatim]'), acct: !!document.querySelector('[data-telemetry-action=account_menu]')}; };
const maPickTile = () => { const vis = (e) => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; }; const tiles = [...document.querySelectorAll('[role=button],button')].filter(vis).filter((e) => /@/.test(e.getAttribute('data-test-id') || '') || /@/.test(e.getAttribute('aria-label') || '')).filter((e) => !/다른 계정|another account/i.test(e.innerText || '')); if (tiles.length !== 1) return false; tiles[0].click(); return true; };
const maSubmit = (file) => { const chip = [...document.querySelectorAll('span,div,p')].some((e) => e.children.length === 0 && e.textContent.trim() === file); const btn = [...document.querySelectorAll('button')].find((x) => (x.getAttribute('aria-label') || '') === 'Submit audio' && !x.disabled); const bad = /Couldn't read this audio/.test(document.body.innerText); if (!chip || !btn || bad) return {ok: false, chip: chip, btn: !!btn, bad: bad}; btn.click(); return {ok: true}; };
const maExtract = () => { const heads = [...document.querySelectorAll('span')].filter((e) => e.children.length === 0 && /^\d{1,2}:\d{2}(:\d{2})?$/.test(e.textContent.trim()) && /^Speaker\s+\d+/.test(e.parentElement.innerText.trim())); const pad = (t) => { const p = t.split(':').map(Number); const s = p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]; const h = Math.floor(s / 3600); const m = Math.floor(s % 3600 / 60); const x = s % 60; return (h ? String(h).padStart(2, '0') + ':' : '') + String(m).padStart(2, '0') + ':' + String(x).padStart(2, '0'); }; return heads.map((e) => { const head = e.parentElement; const block = head.parentElement; const sp = head.innerText.trim().match(/^Speaker\s+(\d+)/)[1]; const body = [...block.children].filter((c) => c !== head).map((c) => c.innerText.trim()).join('\n').trim(); return {t: pad(e.textContent.trim()), s: sp, body: body}; }); };
const maPoll = () => { if (/Couldn't read this audio/.test(document.body.innerText)) return {err: 'couldnt_read'}; if (/Please sign in/i.test(document.body.innerText)) return {err: 'signin'}; if (/refused this request/i.test(document.body.innerText)) return {err: 'refused'}; const more = [...document.querySelectorAll('button')].find((x) => /Show full transcript/i.test(x.textContent)); if (more) more.click(); return {more: !!more}; };
// 고정 대기 대신 화면이 다 뜰 때까지(로그인 버튼·비밀번호 칸, 또는 Verbatim 칸과 계정 메뉴) 0.5초마다 최대 20초 본다(L-027).
const maReady = async () => { let st = {}; for (let i = 0; i < 40; i++) { await sleep(500); try { st = await maRun(maState); } catch (e) { continue; } if (st.login || st.pw || (st.vb && st.acct)) break; } return st; };
maTab = await openTab(maUrl);
let maS = await maReady();
if (maS.login) {
  maOut.logged_in = 'tried';
  await maTab.locator('button[aria-label="Log in"]').first().click();
  let maClicked = false;
  for (let i = 0; i < 30; i++) { await sleep(1000); try { maS = await maRun(maState); } catch (e) { continue; } if (maS.pw) break; if (!maClicked && maS.tiles > 0) { maClicked = await maRun(maPickTile); if (!maClicked) break; continue; } if (maClicked && /playground\.microsoft\.ai/.test(maS.url) && !maS.login) break; }
  if (maClicked && /playground\.microsoft\.ai/.test(maS.url) && !maS.login && !maS.pw) { maOut.logged_in = 'clicked'; await maTab.goto(maUrl); maS = await maReady(); }
}
if (maS.login || !/playground\.microsoft\.ai/.test(maS.url)) {
  maOut = {ok: false, login_required: true, logged_in: maOut.logged_in, error: 'Aside에서 MAI 로그인 필요(' + (maS.pw ? '비밀번호 칸이 나옴' : maS.tiles > 1 ? '계정이 여러 개' : String(maS.url).slice(0, 60)) + ')'};
} else {
  if (!maS.verbatim && maS.vb) { await maTab.locator('input[value=verbatim]').first().check({force: true, timeout: 5000}); await sleep(500); maS = await maRun(maState); }
  if (!maS.verbatim) { maOut = {ok: false, err: maS.vb ? 'submit_check' : 'not_ready', error: maS.vb ? 'Verbatim 모드를 켜지 못했습니다' : 'MAI 화면에 Verbatim 칸이 없습니다(화면이 다 뜨지 않음)', logged_in: maOut.logged_in}; } else {
    const maRes = await fetch(__FETCH__);
    if (!maRes.ok) throw new Error('이 PC 안 임시 주소에서 조각을 받지 못했습니다(' + maRes.status + ')');
    await fs.writeFile('./' + maName, Buffer.from(await maRes.arrayBuffer()));
    await maTab.locator('input[type=file]').first().setInputFiles('./' + maName);
    await sleep(2500);
    let maSent = await maRun(maSubmit, maName);
    if (!maSent.ok) { await sleep(2500); maSent = await maRun(maSubmit, maName); }
    if (!maSent.ok) { maOut = {ok: false, err: maSent.bad ? 'couldnt_read' : 'submit_check', error: '전송 전 확인 실패 ' + JSON.stringify(maSent), logged_in: maOut.logged_in}; } else {
      const maSentAt = Date.now(); let last = -1; let stable = 0; let maErr = null;
      while (Date.now() - maT0 < 108000) { await sleep(3000); const p = await maRun(maPoll); if (p.err) { maErr = p.err; break; } const n = (await maRun(maExtract)).length; if (n > 0 && n === last && !p.more) { stable += 1; if (stable >= 2) break; } else { stable = 0; } last = n; }
      const maBlocks = maErr ? [] : await maRun(maExtract);
      const maSecs = (Date.now() - maSentAt) / 1000;
      if (maErr) { maOut = {ok: false, err: maErr, error: maErr === 'refused' ? 'MAI가 요청을 거절했습니다' : maErr === 'signin' ? 'MAI 로그인이 풀렸습니다(Please sign in)' : '조각을 읽지 못했습니다', secs: maSecs, logged_in: maOut.logged_in}; }
      else if (!maBlocks.length) { maOut = {ok: false, err: 'empty', error: '결과가 비어 있습니다', secs: maSecs, logged_in: maOut.logged_in}; }
      else if (stable < 2) { maOut = {ok: false, err: 'timeout', error: '결과가 시간 안에 다 나오지 않았습니다(' + maBlocks.length + '블록)', secs: maSecs, logged_in: maOut.logged_in}; }
      else { maOut = {ok: true, secs: maSecs, blocks: maBlocks.length, logged_in: maOut.logged_in, text: maBlocks.map((x) => x.t + '\n[S' + x.s + '] ' + x.body + '\n').join('\n')}; }
    }
  }
}
} catch (e) { maOut = {ok: false, error: 'Aside 실행 중 오류: ' + String((e && e.message) || e).slice(0, 200), logged_in: maOut.logged_in}; }
finally { if (maTab) { try { await closeTab(maTab); } catch (e) {} } }
console.log('RESULT: ' + JSON.stringify(maOut));
