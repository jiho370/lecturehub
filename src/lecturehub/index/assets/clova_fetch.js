// 클로바노트에서 노트 하나를 받는 aside repl 스크립트(clova_script.build가 한 줄로 합쳐 넘긴다).
// 규칙: 한 줄 주석만 쓴다(줄 끝 주석 금지), 문장은 세미콜론으로 끝낸다. 자리표시: __KNOWN__(이미 받은 노트 id 배열).
// 받은 파일은 Aside가 다운로드 폴더에 바로 저장한다(saveAs는 세션 폴더에 남지 않았다). 그 위치(download.path())만 알려 주고, 이름 바꾸기는 앱이 한다.
// 하는 일: 로그인 확인 → 목록(화면이 스스로 부른 응답을 읽음) → 아직 안 받은 완료 노트 1개 → 전사(.txt, 시간·참석자) + 녹음 다운로드.
// 하지 않는 일: 삭제·공유·수정, 값 입력(로그인은 비밀번호 관리자가 채운 칸을 확인하고 버튼만 누름), 캡차·추가 인증 통과.
let cnOut; let cnTab; let cnStep = '시작'; try {
const cnKnown = new Set(__KNOWN__);
cnTab = await openTab('https://clovanote.naver.com/');
await sleep(1200);
const cnUrl = async () => String(await cnTab.url());
const cnStart = cnTab.getByRole('button', {name: '로그인'}).first();
if (!/\/w\//.test(await cnUrl()) && await cnStart.count()) { await cnStart.click(); for (let i = 0; i < 40; i++) { await sleep(250); if (/nid\.naver\.com|\/w\//.test(await cnUrl())) break; } }
cnStep = '로그인';
if (/nid\.naver\.com/.test(await cnUrl())) {
  await cnTab.locator('#id').click();
  let cnFilled = false;
  for (let i = 0; i < 12 && !cnFilled; i++) { await sleep(250); cnFilled = await cnTab.evaluate("['id','pw'].every(i => (document.getElementById(i) || {value: ''}).value.length > 0)"); }
  // '로그인 상태 유지'(#loginStay)를 켜 둔다 — 다음 주 확인 때 다시 로그인하지 않도록. 안 켜져도 로그인은 계속.
  if (cnFilled) { await cnTab.evaluate("(() => { const k = document.getElementById('loginStay'); if (k && !k.checked) k.click(); return !!(k && k.checked); })()"); }
  if (cnFilled) { await cnTab.getByRole('button', {name: '로그인', exact: true}).last().click(); for (let i = 0; i < 40; i++) { await sleep(300); if (!/nid\.naver\.com/.test(await cnUrl())) break; } }
}
if (!/clovanote\.naver\.com\/w\//.test(await cnUrl())) {
  cnOut = {ok: false, login_required: true, error: 'Aside에서 네이버 로그인 필요 (' + (await cnUrl()).slice(0, 60) + ')'};
} else {
  await cnTab.evaluate("window.__cnNotes = []; const cnOpen = XMLHttpRequest.prototype.open; XMLHttpRequest.prototype.open = function (m, u) { if (/\\/notes\\?/.test(String(u))) this.addEventListener('load', () => { try { window.__cnNotes.push(JSON.parse(this.responseText)); } catch (e) {} }); return cnOpen.apply(this, arguments); };");
  cnStep = '목록';
  await cnTab.getByRole('link', {name: '전체 노트'}).first().click();
  let cnList = null;
  for (let i = 0; i < 40 && !cnList; i++) { await sleep(250); cnList = await cnTab.evaluate("(window.__cnNotes[0] && window.__cnNotes[0].contents && window.__cnNotes[0].contents.noteInfoSetList) || null"); }
  if (!cnList) {
    cnOut = {ok: false, error: '노트 목록을 읽지 못했습니다'};
  } else {
    const cnInfo = cnList.map((x) => x.noteInfo).filter(Boolean);
    const cnDone = (n) => n.noteStatus === 'DONE' && n.audioDuration > 0 && !n.deletedFlag && !(n.audioDeletion && n.audioDeletion.deleted);
    const cnSkipped = cnInfo.filter((n) => !cnDone(n) && !n.deletedFlag).length;
    const cnPick = cnInfo.filter((n) => cnDone(n) && !cnKnown.has(n.noteId)).sort((a, b) => String(b.audioStartDate || b.createdDate).localeCompare(String(a.audioStartDate || a.createdDate)))[0];
    const cnLocal = (s) => { const d = new Date(s); const p = (v) => String(v).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); };
    const cnNotes = [];
    if (cnPick) {
      const cnNote = {id: cnPick.noteId, created: cnLocal(cnPick.audioStartDate || cnPick.createdDate), duration_ms: cnPick.audioDuration, title: cnPick.noteName || '', files: []};
      cnNotes.push(cnNote);
      const cnWs = (await cnUrl()).match(/\/w\/([^/]+)/)[1];
      await cnTab.goto('https://clovanote.naver.com/w/' + cnWs + '/note-detail/' + cnPick.noteId);
      await sleep(2500);
      // Aside locator는 이어 쓸 수 없어(창.getByRole 불가) 창 안의 [다운로드] 버튼은 페이지에서 찾아 표시(data-cn-go)한 뒤 누른다.
      const cnDialogGo = async () => { const found = await cnTab.evaluate("(() => { document.querySelectorAll('[data-cn-go]').forEach(x => x.removeAttribute('data-cn-go')); const d = Array.from(document.querySelectorAll('[role=dialog]')).pop(); if (!d) return false; const b = Array.from(d.querySelectorAll('button')).filter(x => x.innerText.trim() === '다운로드').pop(); if (!b) return false; b.setAttribute('data-cn-go', '1'); return true; })()"); if (found) { await cnTab.locator('[data-cn-go]').last().click(); } return found; };
      const cnMenu = async () => { const t = await snapshot(cnTab, {interactive: true}); const ref = (t.tree.match(/button "다운로드" \[ref=(e\d+)\]/) || [])[1]; if (!ref) throw new Error('다운로드 버튼을 찾지 못함'); await cnTab.locator(ref).click(); await sleep(800); };
      const cnSave = async (dl, kind) => { const name = (typeof dl.suggestedFilename === 'function' ? dl.suggestedFilename() : dl.suggestedFilename) || ''; const ext = (name.match(/\.[A-Za-z0-9]{2,4}$/) || [''])[0] || (kind === 'text' ? '.txt' : '.m4a'); const where = await dl.path(); if (!where) throw new Error((kind === 'text' ? '전사' : '녹음') + ' 다운로드 위치를 모름'); cnNote.files.push({kind: kind, path: String(where), ext: ext, suggested: name}); };
      cnStep = '전사 메뉴';
      await cnMenu();
      await cnTab.getByRole('button', {name: '음성 기록 다운로드'}).first().click();
      await sleep(800);
      cnStep = '전사 창';
      // 텍스트 문서(.txt) + 시간 기록 + 참석자. Aside locator에는 isChecked가 없어 페이지에서 상태를 읽고, 빠진 것만 누른다.
      const cnWant = ['radio_down_type_1_txt', 'checkbox_time', 'checkbox_participant'];
      const cnOff = await cnTab.evaluate("['radio_down_type_1_txt','checkbox_time','checkbox_participant'].filter(i => { const e = document.getElementById(i); return !e || !e.checked; })");
      for (const id of cnOff) { if (!cnWant.includes(id)) continue; await cnTab.locator('#' + id).click(); await sleep(200); }
      const cnStill = await cnTab.evaluate("['radio_down_type_1_txt','checkbox_time','checkbox_participant'].filter(i => { const e = document.getElementById(i); return !e || !e.checked; })");
      if (cnStill.length) throw new Error('전사 형식(txt·시간 기록·참석자)을 고르지 못함: ' + cnStill.join(','));
      const cnTextDl = cnTab.waitForEvent('download', {timeout: 30000});
      if (!(await cnDialogGo())) throw new Error('전사 창의 다운로드 버튼을 찾지 못함');
      cnStep = '전사 저장';
      await cnSave(await cnTextDl, 'text');
      await sleep(800);
      cnStep = '녹음 메뉴';
      await cnMenu();
      const cnAudioDl = cnTab.waitForEvent('download', {timeout: 70000});
      await cnTab.getByRole('button', {name: '음성 파일 다운로드'}).first().click();
      await sleep(1200);
      await cnDialogGo();
      cnStep = '녹음 저장';
      await cnSave(await cnAudioDl, 'audio');
    }
    cnOut = {ok: true, notes: cnNotes, skipped_uploading: cnSkipped, more: cnInfo.filter((n) => cnDone(n) && !cnKnown.has(n.noteId)).length > 1};
  }
}
} catch (e) { cnOut = {ok: false, error: 'Aside 실행 중 오류(' + cnStep + '): ' + String((e && e.message) || e).slice(0, 200)}; }
finally { if (cnTab) { try { await closeTab(cnTab); } catch (e) {} } }
console.log('RESULT: ' + JSON.stringify(cnOut));
