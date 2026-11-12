const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const root = path.resolve(__dirname,'..');
const screenshots = path.join(root,'.verification');
fs.mkdirSync(screenshots,{recursive:true});

async function startServer(db, port=0) {
  const child = spawn('python3',['-u','server.py','--port',String(port),'--db',db],{cwd:root,stdio:['ignore','pipe','pipe']});
  const url = await new Promise((resolve,reject) => {
    const timeout = setTimeout(()=>{child.kill();reject(new Error('Server startup timed out'));},10000);
    child.once('error',reject);
    child.stdout.on('data',chunk => { const found=chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(found){clearTimeout(timeout);resolve(found[0]);} });
    child.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Server exited ${code}`));});
  });
  return {child,url};
}
async function stopServer(child) { const stopped=once(child,'exit');child.kill();await stopped; }
async function login(page,url,username='admin') {
  await page.goto(url);
  await page.locator('#login-screen').waitFor({state:'visible'});
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(username+'123!');
  await page.locator('#login-submit').click();
  await page.locator('#dashboard').waitFor({state:'visible'});
}
async function reviewFixChecks(browser,url) {
  const out=path.join(screenshots,'review-fixes');fs.mkdirSync(out,{recursive:true});
  const signIn=async(page,name)=>{await page.locator('#username').fill(name);await page.locator('#password').fill(name+'123!');await page.locator('#login-submit').click();await page.locator('#dashboard').waitFor({state:'visible'});};
  const revoke=page=>page.evaluate(async()=>{await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});});
  const discard=async page=>{page.once('dialog',d=>d.accept());await page.locator('#close').click();assert(await page.locator('#editor').isHidden());};
  let page=await browser.newPage({viewport:{width:1440,height:900}});
  await login(page,url);await page.locator('.job').first().click();
  const original=await page.locator('#edit-title').inputValue();
  await page.locator('#edit-title').fill('未儲存標題');await page.locator('#edit-notes').fill('未儲存備註');
  page.once('dialog',d=>d.dismiss());await page.locator('#new-order').click();assert.equal(await page.locator('#edit-title').inputValue(),'未儲存標題');
  page.once('dialog',d=>d.dismiss());await page.locator('#queue-panel summary').click();await page.locator('.queue-item').first().click();assert.equal(await page.locator('#edit-title').inputValue(),'未儲存標題');
  let escDialog=false;const unexpected=d=>{escDialog=true;d.dismiss();};page.on('dialog',unexpected);
  await page.locator('#edit-notes').focus();await page.keyboard.press('Escape');assert(await page.locator('#editor').isVisible());assert(!escDialog);page.off('dialog',unexpected);
  page.once('dialog',d=>d.dismiss());await page.locator('#close').click();assert(await page.locator('#editor').isVisible());
  await discard(page);await page.locator('.job').first().click();assert.equal(await page.locator('#edit-title').inputValue(),original);await page.locator('#close').click();
  await page.locator('#new-order').click();await page.locator('#edit-title').fill('跨登入新單草稿');await page.locator('#edit-customer').fill('草稿門市');await page.locator('#edit-notes').fill('草稿內容保護');
  await revoke(page);await page.locator('#save-order').click();await page.locator('#login-screen').waitFor({state:'visible'});
  await signIn(page,'user');assert(await page.locator('#editor').isHidden());assert(await page.locator('#new-order').isHidden());
  await page.locator('#account-role').click();await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
  await signIn(page,'admin');assert(await page.locator('#editor').isVisible());assert.equal(await page.locator('#edit-title').inputValue(),'跨登入新單草稿');assert.equal(await page.locator('#edit-notes').inputValue(),'草稿內容保護');
  await discard(page);await page.locator('#account-role').click();await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
  await signIn(page,'user');await page.locator('.job').first().click();const orderId=(await page.locator('#editor-meta').innerText()).split(' · ')[0];
  const before=await page.evaluate(async(id)=>(await(await fetch('/api/dashboard')).json()).orders.find(o=>o.id===id).notes,orderId);
  await page.locator('#edit-notes').fill('登入過期仍保留的備註');await revoke(page);await page.locator('#save-order').click();await page.locator('#login-screen').waitFor({state:'visible'});await signIn(page,'user');
  assert.equal(await page.locator('#edit-notes').inputValue(),'登入過期仍保留的備註');assert(await page.locator('#edit-title').isHidden());
  assert.equal(await page.evaluate(async(id)=>(await(await fetch('/api/dashboard')).json()).orders.find(o=>o.id===id).notes,orderId),before);
  await page.locator('#save-order').click();await page.getByText('已儲存。',{exact:true}).waitFor();assert.equal(await page.evaluate(()=>sessionStorage.getItem('deli.orderDraft.2')),null);
  await page.locator('#edit-notes').fill('502時保留的內容');
  await page.route('**/api/orders/*',r=>r.fulfill({status:502,contentType:'text/html',body:'<html>Bad Gateway</html>'}));
  await page.locator('#save-order').click();await page.getByText('服務暫時無法使用，請稍後重試。',{exact:true}).waitFor();assert.equal(await page.locator('#edit-notes').inputValue(),'502時保留的內容');
  await page.unroute('**/api/orders/*');await discard(page);
  await page.route('**/api/dashboard',r=>r.fulfill({status:502,contentType:'text/html',body:'<html>Bad Gateway</html>'}));
  await page.reload();await page.locator('#service-error').waitFor({state:'visible'});assert(await page.locator('#login-screen').isHidden());
  await page.screenshot({path:path.join(out,'service-retry.png')});await page.unroute('**/api/dashboard');await page.locator('#service-retry').click();await page.locator('#dashboard').waitFor({state:'visible'});assert.equal(await page.locator('#account-role').innerText(),'user');
  await page.locator('.job').first().click();await page.locator('#edit-notes').fill('送出的內容');
  let release,seen;const gate=new Promise(r=>release=r),captured=new Promise(r=>seen=r);
  await page.route('**/api/orders/*',async r=>{seen();await gate;await r.continue();});
  await page.locator('#save-order').click();await captured;await page.locator('#edit-notes').fill('儲存途中繼續輸入');release();
  await page.getByText('已儲存送出的內容，目前仍有未儲存變更。',{exact:true}).waitFor();assert.equal(await page.locator('#edit-notes').inputValue(),'儲存途中繼續輸入');
  await page.unroute('**/api/orders/*');await discard(page);await page.close();
  const accountContext=await browser.newContext({viewport:{width:1440,height:900}});
  page=await accountContext.newPage();await login(page,url);
  await page.locator('.job').first().click();await page.locator('#edit-notes').fill('帳號 A 已送出版本');
  let releaseAccount,accountSeen;const accountGate=new Promise(r=>releaseAccount=r),accountCaptured=new Promise(r=>accountSeen=r);
  await page.route('**/api/orders/*',async r=>{accountSeen();await accountGate;await r.continue();});
  await page.route('**/api/dashboard',r=>r.fulfill({status:503,contentType:'text/html',body:'<html>Unavailable</html>'}));
  await page.locator('#save-order').click();await accountCaptured;await page.locator('#edit-notes').fill('帳號 A 未送出草稿');releaseAccount();
  await page.locator('#global-error').waitFor({state:'visible'});await page.unroute('**/api/orders/*');await page.unroute('**/api/dashboard');
  const otherTab=await page.context().newPage();await otherTab.goto(url);await otherTab.locator('#dashboard').waitFor({state:'visible'});
  await otherTab.locator('#account-role').click();await otherTab.locator('#logout').click();await otherTab.locator('#login-screen').waitFor({state:'visible'});await signIn(otherTab,'user');
  await page.locator('#retry').click();await page.getByText('登入身份已變更，原帳號的未儲存內容已暫存。',{exact:true}).waitFor();
  assert(await page.locator('#editor').isHidden());assert.equal(await page.locator('#account-role').innerText(),'user');
  const drafts=await page.evaluate(()=>({a:JSON.parse(sessionStorage.getItem('deli.orderDraft.1')),b:sessionStorage.getItem('deli.orderDraft.2')}));
  assert.equal(drafts.a.values.notes,'帳號 A 未送出草稿');assert.equal(drafts.b,null);
  await otherTab.close();await page.locator('#account-role').click();await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});await signIn(page,'admin');
  assert.equal(await page.locator('#edit-notes').inputValue(),'帳號 A 未送出草稿');await discard(page);
  await page.locator('#new-order').click();await page.locator('#edit-title').fill('回讀失敗也只建立一次');await page.locator('#edit-customer').fill('回讀測試門市');
  await page.route('**/api/dashboard',r=>r.fulfill({status:502,contentType:'text/html',body:'<html>Bad Gateway</html>'}));
  await page.locator('#save-order').click();await page.getByText('已儲存，但畫面更新失敗，請按重試。',{exact:true}).waitFor();
  await page.unroute('**/api/dashboard');await page.locator('#retry').click();await page.locator('#global-error').waitFor({state:'hidden'});
  await page.locator('#save-order').click();await page.getByText('已儲存。',{exact:true}).waitFor();
  const created=await page.evaluate(async()=>(await(await fetch('/api/dashboard')).json()).orders.filter(o=>o.title==='回讀失敗也只建立一次'));assert.equal(created.length,1);await page.locator('#close').click();
  const urgentDate=await page.evaluate(()=>dateKey(addDays(weekStart(today),2)));
  const urgentResult=await page.evaluate(async(date)=>{const r=await fetch('/api/orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'藏在其餘件數的急件',customer:'急件測試門市',priority:'urgent',assignment:{teamId:'south-b',date,start:'16:00',end:'17:00'}})});return r.status;},urgentDate);assert.equal(urgentResult,201);
  await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#month').click();
  const more=page.locator(`.month-cell:has([data-day="${urgentDate}"]) .month-more`);assert(await more.evaluate(e=>e.classList.contains('has-urgent')));assert.match(await more.innerText(),/急/);assert((await page.locator('.mjob .urgent').count())>0);
  await page.screenshot({path:path.join(out,'month-urgent.png')});
  await page.locator('#week').click();await page.locator('#expand-all').click();
  const target=page.locator('[data-team-row="south-b"] .job').first();await target.scrollIntoViewIfNeeded();const targetId=await target.getAttribute('data-order');await target.click();await page.locator('#close').click();
  const returned=page.locator(`[data-team-row="south-b"] .job[data-order="${targetId}"]`);const box=await returned.boundingBox();assert(box.y>=0&&box.y+box.height<=900);
  await page.locator('[data-team-row="south-b"] .team-name').scrollIntoViewIfNeeded();const head=await page.locator('.week-head').boundingBox();assert(head.y>=-1&&head.y<=1);
  await page.locator('#collapse-all').click();await page.locator('[data-toggle-team="north-a"]').click();
  const savedState=await page.evaluate(()=>localStorage.getItem('deli.expandedTeams'));
  await page.locator('[data-focus-person="p3"]').click();assert.equal(await page.locator('[data-toggle-team="south-a"]').getAttribute('aria-expanded'),'true');assert.equal(await page.evaluate(()=>localStorage.getItem('deli.expandedTeams')),savedState);
  await page.locator('[data-toggle-team="south-a"]').click();assert((await page.locator('[data-team-row="south-a"] .lane-summary.is-person-match').count())>0);
  await page.locator('[data-focus-person="p3"]').click();assert.equal(await page.locator('[data-toggle-team="south-a"]').getAttribute('aria-expanded'),'false');assert.equal(await page.locator('.lane-zero').count(),0);
  await accountContext.close();
  for(const width of [1440,728,390]){
    page=await browser.newPage({viewport:{width,height:900},colorScheme:'dark'});await login(page,url,'user');
    const date=await page.evaluate(()=>dateKey(today));
    const visibleToday=()=>page.evaluate(key=>{const r=document.querySelector('.week-scroll').getBoundingClientRect(),d=document.querySelector(`.lane[data-date="${key}"]`).getBoundingClientRect();return d.left>=r.left+95&&d.right<=r.right+1;},date);
    if(width<1000){assert(await visibleToday());await page.locator('.week-scroll').evaluate(e=>e.scrollLeft=0);await page.locator('#today').click();assert(await visibleToday());}
    await page.locator('#account-role').click();await page.keyboard.press('Escape');assert.equal(await page.locator('#account-menu').evaluate(e=>e.open),false);
    await page.locator('#account-role').click();await page.locator('#range').click();assert.equal(await page.locator('#account-menu').evaluate(e=>e.open),false);
    if(width===390){assert(await page.locator('#calendar-legend').evaluate(e=>e.classList.contains('is-collapsed')));assert.equal(await page.locator('.person-name').first().evaluate(e=>getComputedStyle(e).display),'none');assert((await page.locator('.calendar-bar').boundingBox()).height<115);}
    if(width>=728) assert(await page.locator('#legend-content').isVisible());
    if(width===390){await page.locator('.legend-toggle').click();assert(await page.locator('#legend-content').isVisible());await page.locator('.legend-toggle').click();assert(await page.locator('#legend-content').isHidden());}
    await page.locator('.job').first().click();
    if(width===1440){const primary=await page.locator('#detail-person1').boundingBox(),secondary=await page.locator('#detail-person2').boundingBox();assert(Math.abs(primary.y-secondary.y)<1);}
    if(width===390){assert.equal(await page.evaluate(()=>document.activeElement.id),'editor-title');const close=await page.locator('#close').boundingBox(),header=await page.locator('.editor-header').boundingBox();assert(Math.abs(close.y-header.y)<1);}
    await page.screenshot({path:path.join(out,width+'-editor.png'),fullPage:true});await page.close();
  }
  return 'dirty guards, account-isolated drafts including cross-tab login changes, session expiry, retry/502, save-race protection, date/return positioning, urgency, sticky dates, menu, transient highlight and mobile layout passed';
}

(async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'deli-browser-'));
  const db=path.join(temp,'test.sqlite3');
  let server, browser;
  try {
    server=await startServer(db);
    browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
    if (process.env.REVIEW_FIXES_ONLY==='1') {
      const reviewFixes=await reviewFixChecks(browser,server.url);
      fs.writeFileSync(path.join(screenshots,'review-fixes','results.json'),JSON.stringify({reviewFixes},null,2));
      console.log(JSON.stringify({reviewFixes}));return;
    }
    const errors=[],matrix=[];
    for(const width of [1440,728,390])for(const colorScheme of ['light','dark']) {
      const page=await browser.newPage({viewport:{width,height:1000},colorScheme});
      page.on('pageerror',e=>errors.push(e.message));
      await login(page,server.url);
      for(const state of ['week','month','editor']) {
        if(state==='month')await page.locator('#month').click();
        if(state==='editor') {await page.locator('#week').click();await page.locator('#queue [data-order="WO-1110"]').click();}
        const metric=await page.evaluate(()=>({
          pageOverflow:document.documentElement.scrollWidth>innerWidth,
          dayColumns:[...document.querySelectorAll('.week-head>div')].slice(1).map(e=>e.textContent),
          teamRows:document.querySelectorAll('.team-row').length,
          maxOrders:Math.max(0,...[...document.querySelectorAll('.lane')].map(e=>e.querySelectorAll('.job').length)),
          minCell:Math.min(...[...document.querySelectorAll('.team-row:not(.is-collapsed) .lane')].map(e=>e.getBoundingClientRect().height)),
          clipped:[...document.querySelectorAll('.lane')].some(e=>e.scrollHeight>e.clientHeight),
          timesWrap:[...document.querySelectorAll('.job time')].some(e=>e.scrollWidth>e.clientWidth),
          scrollable:!!document.querySelector('.week-scroll')&&document.querySelector('.week-scroll').scrollWidth>document.querySelector('.week-scroll').clientWidth
        }));
        assert(!metric.pageOverflow,`${width}/${colorScheme}/${state} page overflow`);
        if(state!=='month') {
          assert.equal(metric.teamRows,3);assert.equal(metric.maxOrders,5);assert(metric.minCell>=380);assert(!metric.clipped);assert(!metric.timesWrap);
          assert.deepEqual(metric.dayColumns.map(s=>s.slice(0,2)),['週一','週二','週三','週四','週五','週六','週日']);
          if(width<1000)assert(metric.scrollable);
        }
        await page.screenshot({path:path.join(screenshots,`${width}-${colorScheme}-${state}.png`),fullPage:true});
        matrix.push({width,colorScheme,state,...metric});
      }
      await page.close();
    }
    const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    await login(page,server.url);
    assert.equal(await page.locator('.team-name[aria-expanded="true"]').count(),1);
    await page.locator('[data-toggle-team="south-a"]').click();
    assert.equal(await page.locator('.team-name[aria-expanded="true"]').count(),2);
    await page.locator('[data-toggle-team="north-a"]').click();
    assert.equal(await page.locator('[data-toggle-team="south-a"]').getAttribute('aria-expanded'),'true');
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-toggle-team="north-a"]').getAttribute('aria-expanded'),'false');
    assert.equal(await page.locator('[data-toggle-team="south-a"]').getAttribute('aria-expanded'),'true');
    await page.locator('#collapse-all').click();assert.equal(await page.locator('.team-name[aria-expanded="true"]').count(),0);
    await page.locator('.lane-summary[data-expand-team="north-a"]').first().click();
    assert.equal(await page.locator('[data-toggle-team="north-a"]').getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('.team-name[aria-expanded="true"]').count(),1);
    await page.locator('#expand-all').click();assert.equal(await page.locator('.team-name[aria-expanded="true"]').count(),3);
    await page.locator('#collapse-all').click();await page.locator('[data-toggle-team="north-a"]').click();
    assert.equal(await page.locator('[data-focus-person]').count(),7);
    assert.equal(await page.locator('.job').first().locator('.person-code').count(),2);
    assert.deepEqual(await page.locator('.job').first().locator('.person-code').allInnerTexts(),['P1','P2']);
    await page.locator('[data-focus-person="p5"]').click();
    assert.equal(await page.locator('[data-toggle-team="south-b"]').getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('.job.is-person-match').count(),2);
    assert((await page.locator('.job.is-person-dim').count())>0);
    assert.equal(await page.locator('.job.is-person-dim').first().evaluate(e=>getComputedStyle(e).opacity),'0.3');
    await page.locator('[data-focus-person="p5"]').click();assert.equal(await page.locator('.is-person-dim').count(),0);
    await page.locator('#month').click();await page.locator('[data-focus-person="p1"]').click();
    assert((await page.locator('.mjob.is-person-match').count())>0);assert((await page.locator('.mjob.is-person-dim').count())>0);
    await page.locator('#week').click();assert.equal(await page.locator('[data-focus-person="p1"]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-focus-person=""]').click();assert.equal(await page.locator('.is-person-dim').count(),0);
    await page.locator('#collapse-all').click();await page.locator('[data-toggle-team="north-a"]').click();
    await page.locator('#new-order').click();assert.equal(await page.locator('#editor-title').innerText(),'建立新工單');
    assert.equal(await page.locator('#edit-title').inputValue(),'');
    await page.locator('#edit-title').fill('新建測試工單');await page.locator('#edit-customer').fill('新建測試門市');
    await page.locator('#edit-person1').selectOption('p1');await page.locator('#edit-person2').selectOption('p2');await page.locator('#edit-notes').fill('新建備註');
    await page.locator('#editor').screenshot({path:path.join(screenshots,'create-order.png')});
    await page.locator('#save-order').click();await page.getByText('已建立工單。',{exact:true}).waitFor();
    let created=await page.evaluate(async()=>{const data=await(await fetch('/api/dashboard')).json();return data.orders.find(o=>o.title==='新建測試工單');});
    assert.equal(created.notes,'新建備註');assert.equal(created.person1Id,'p1');assert.equal(created.person2Id,'p2');
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator(`#queue [data-order="${created.id}"]`).count(),1);
    await page.locator('#new-order').click();await page.locator('#edit-title').fill('新建帶排程');await page.locator('#edit-customer').fill('排程測試門市');
    await page.locator('#edit-person1').selectOption('p1');await page.locator('#edit-person2').selectOption('p2');await page.locator('#edit-scheduled').check();
    await page.locator('#edit-date').fill('2033-01-01');await page.locator('#edit-start').fill('09:00');await page.locator('#edit-end').fill('10:00');
    await page.locator('#save-order').click();await page.getByText('已建立工單。',{exact:true}).waitFor();
    assert.equal(await page.locator('.job').count(),1);
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator('.calendar-key').innerText(),'北A 北部 A 組\n南A 南部 A 組\n南B 南部 B 組\n急 急件\nWO 工單編號');
    await page.locator('#team').selectOption('south-b');assert.equal(await page.locator('.team-row').count(),1);
    await page.locator('#team').selectOption('all');
    await page.locator('#month').click();await page.locator('.day-button.is-today').click();assert.equal(await page.locator('#week').getAttribute('aria-pressed'),'true');
    await page.locator('#queue [data-order="WO-1110"]').click();
    assert.equal(await page.locator('#edit-person1 option').count(),7);
    assert.equal(await page.locator('#queue-panel').evaluate(e=>e.open),false);
    const horizontal=await page.evaluate(()=>{
      const a=document.querySelector('#edit-title').getBoundingClientRect(),b=document.querySelector('#edit-notes').getBoundingClientRect(),c=document.querySelector('.schedule-block').getBoundingClientRect();
      return {basic:a.x,notes:b.x,schedule:c.x,height:document.querySelector('#editor').getBoundingClientRect().height};
    });
    assert(horizontal.basic<horizontal.notes&&horizontal.notes<horizontal.schedule);
    assert(horizontal.height<430);

    assert.equal(await page.locator('#edit-person1').inputValue(),'p3');
    await page.locator('#edit-title').fill('更新後保養項目');
    await page.locator('#edit-customer').fill('更新門市');
    await page.locator('#edit-address').fill('更新後地址');
    await page.locator('#edit-phone').fill('06-0000-1234');
    await page.locator('#edit-content').fill('第一行\n第二行');
    await page.locator('#edit-person1').selectOption('p5');
    await page.locator('#edit-person2').selectOption('p6');
    await page.locator('#edit-notes').fill('admin 備註');
    await page.locator('#edit-scheduled').check();
    await page.locator('input[name="edit-team"][value="south-b"]').check();
    await page.locator('#edit-date').fill('2030-01-02');
    await page.locator('#edit-start').fill('09:00');await page.locator('#edit-end').fill('10:00');
    await page.locator('#save-order').click();await page.getByText('已儲存。',{exact:true}).waitFor();
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    let data=await page.evaluate(async()=>(await (await fetch('/api/dashboard')).json()));
    const saved=data.orders.find(o=>o.id==='WO-1110'),assignment=data.assignments.find(a=>a.workOrderId==='WO-1110');
    assert.equal(saved.title,'更新後保養項目');assert.equal(saved.notes,'admin 備註');assert.equal(saved.person1,'人員 5');assert.equal(assignment.date,'2030-01-02');
    const oldPort=Number(new URL(server.url).port);await stopServer(server.child);server=await startServer(db,oldPort);
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    data=await page.evaluate(async()=>(await (await fetch('/api/dashboard')).json()));
    assert.deepEqual(data.orders.find(o=>o.id==='WO-1110'),saved);
    assert.deepEqual(data.assignments.find(a=>a.workOrderId==='WO-1110'),assignment);
    await page.locator('#queue [data-order="WO-1109"]').click();
    await page.locator('#edit-title').fill('不得儲存的衝突項目');
    await page.locator('#edit-scheduled').check();
    await page.locator('input[name="edit-team"][value="south-b"]').check();await page.locator('#edit-date').fill('2030-01-02');
    await page.locator('#edit-start').fill('09:30');await page.locator('#edit-end').fill('10:30');
    await page.locator('#save-order').click();await page.getByText(/此團隊同時段已有派工/).waitFor();
    data=await page.evaluate(async()=>(await (await fetch('/api/dashboard')).json()));assert.equal(data.orders.find(o=>o.id==='WO-1109').title,'設備漏水檢修');
    await page.locator('#account-role').click();page.once('dialog',d=>d.accept());await page.locator('#personnel-link').click();await page.locator('#personnel-page').waitFor({state:'visible'});
    assert.equal(await page.locator('.person-row').count(),6);
    await page.locator('.person-row[data-person="p1"] [name="name"]').fill('管理員甲');
    await page.locator('.person-row[data-person="p1"] [name="short"]').fill('MJ');
    await page.locator('.person-row[data-person="p1"] button').click();await page.getByText('人員資料已儲存。',{exact:true}).waitFor();
    await page.locator('#new-person-name').fill('新增人員 7');await page.locator('#new-person button').click();
    await page.locator('.person-row').nth(6).waitFor();assert.equal(await page.locator('.person-row').count(),7);
    await page.screenshot({path:path.join(screenshots,'personnel-management.png'),fullPage:true});
    await page.locator('#back-calendar').click();await page.locator('#calendar-page').waitFor({state:'visible'});
    await page.locator('#queue [data-order="WO-1109"]').click();assert.equal(await page.locator('#edit-person1 option[value="p1"]').innerText(),'管理員甲');
    assert.equal(await page.locator('#edit-person1 option').count(),8);
    assert.equal(await page.locator('[data-focus-person="p1"] b').innerText(),'MJ');
    await page.locator('#account-role').click();await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
    await page.locator('#username').fill('user');await page.locator('#password').fill('user123!');await page.locator('#login-submit').click();await page.locator('#dashboard').waitFor({state:'visible'});
    assert(await page.locator('#new-order').isHidden());
    await page.locator('.job').first().click();assert(await page.locator('#edit-title').isHidden());assert(await page.locator('#readonly-fields').isVisible());
    assert.equal(await page.locator('#detail-person1').innerText(),'管理員甲');
    assert.equal(await page.locator('#detail-person2').innerText(),'人員 2');
    assert.equal(await page.locator('#detail-phone').getAttribute('href'),'tel:0200000000');
    await page.locator('#edit-notes').fill('user 新增備註');await page.locator('#save-order').click();await page.getByText('已儲存。',{exact:true}).waitFor();
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('.job').first().click();
    assert.equal(await page.locator('#edit-notes').inputValue(),'user 新增備註');
    const forbidden=await page.evaluate(async()=>({
      order:(await fetch('/api/orders/WO-1101',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({notes:'越權備註',title:'越權名稱'})})).status,
      personnel:(await fetch('/api/personnel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'越權人員'})})).status
    }));assert.deepEqual(forbidden,{order:403,personnel:403});
    await page.screenshot({path:path.join(screenshots,'user-notes.png'),fullPage:true});
    await page.goto(server.url+'/admin/personnel');await page.locator('#access-denied').waitFor({state:'visible'});assert(await page.locator('#personnel-page').isHidden());
    await page.locator('#account-role').click();assert(await page.locator('#personnel-link').isHidden());await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
    await page.screenshot({path:path.join(screenshots,'login.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const reviewFixes=await reviewFixChecks(browser,server.url);
    fs.writeFileSync(path.join(screenshots,'results.json'),JSON.stringify({matrix,errors,interactions:'passed',restartPersistence:'passed',reviewFixes},null,2));
    console.log(JSON.stringify({matrix:matrix.length,errors,interactions:'PASS',restartPersistence:'PASS',reviewFixes,screenshots}));
  } finally {
    if(browser)await browser.close();
    if(server&&server.child.exitCode===null)await stopServer(server.child);
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1});
