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
          minCell:Math.min(...[...document.querySelectorAll('.lane')].map(e=>e.getBoundingClientRect().height)),
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
    await page.locator('#team').selectOption('south-b');assert.equal(await page.locator('.team-row').count(),1);
    await page.locator('#team').selectOption('all');
    await page.locator('#status').selectOption('已完成');assert.equal(await page.locator('.job').count(),2);
    await page.locator('#status').selectOption('all');
    await page.locator('#month').click();await page.locator('.day-button.is-today').click();assert.equal(await page.locator('#week').getAttribute('aria-pressed'),'true');
    await page.locator('#queue [data-order="WO-1110"]').click();
    assert.equal(await page.locator('input[name="edit-team"]:checked').inputValue(),'south-a');
    await page.locator('#edit-date').fill('2030-01-02');
    await page.locator('#edit-start').fill('09:00');await page.locator('#edit-end').fill('10:00');
    await page.locator('button[type="submit"]').filter({hasText:'儲存排程'}).click();
    await page.getByText('已儲存。',{exact:true}).waitFor();
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    const saved=await page.evaluate(async()=>(await (await fetch('/api/dashboard')).json()).assignments.find(a=>a.workOrderId==='WO-1110'));
    assert.equal(saved.date,'2030-01-02');assert.equal(saved.teamId,'south-a');
    // Same cookie and database must survive a full backend restart.
    const oldPort=Number(new URL(server.url).port);await stopServer(server.child);server=await startServer(db,oldPort);
    await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
    const afterRestart=await page.evaluate(async()=>(await (await fetch('/api/dashboard')).json()).assignments.find(a=>a.workOrderId==='WO-1110'));
    assert.deepEqual(afterRestart,saved);
    await page.locator('#queue [data-order="WO-1109"]').click();
    await page.locator('input[name="edit-team"][value="south-a"]').check();await page.locator('#edit-date').fill('2030-01-02');
    await page.locator('#edit-start').fill('09:30');await page.locator('#edit-end').fill('10:30');
    await page.locator('button[type="submit"]').filter({hasText:'儲存排程'}).click();
    await page.getByText(/此團隊同時段已有派工/).waitFor();
    await page.locator('#close').click();
    await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
    assert.equal(await page.evaluate(async()=>(await fetch('/api/me')).status),401);
    await page.locator('#username').fill('user');await page.locator('#password').fill('wrong');await page.locator('#login-submit').click();
    await page.getByText('帳號或密碼不正確。',{exact:true}).waitFor();
    await page.locator('#password').fill('user123!');await page.locator('#login-submit').click();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator('#account-role').innerText(),'user');
    await page.locator('.job').first().click();assert(await page.locator('#assignment-form').isHidden());
    const forbidden=await page.evaluate(async()=>(await fetch('/api/assignments',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({workOrderId:'WO-1109'})})).status);
    assert.equal(forbidden,403);
    await page.screenshot({path:path.join(screenshots,'user-readonly.png'),fullPage:true});
    await page.locator('#logout').click();await page.locator('#login-screen').waitFor({state:'visible'});
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
