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
(async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'deli-browser-'));
  const db=path.join(temp,'test.sqlite3');
  let server, browser;
  try {
    server=await startServer(db);
    browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
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
    await page.locator('#account-role').click();await page.locator('#personnel-link').click();await page.locator('#personnel-page').waitFor({state:'visible'});
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
    fs.writeFileSync(path.join(screenshots,'results.json'),JSON.stringify({matrix,errors,interactions:'passed',restartPersistence:'passed'},null,2));
    console.log(JSON.stringify({matrix:matrix.length,errors,interactions:'PASS',restartPersistence:'PASS',screenshots}));
  } finally {
    if(browser)await browser.close();
    if(server&&server.child.exitCode===null)await stopServer(server.child);
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1});
