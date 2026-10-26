/** Uses the real official SDK, appended after plugin scripts as AstrBot does. */
import assert from 'node:assert/strict';
import { readFileSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : 'playwright-core');
const base = process.env.DASHBOARD_BASE_URL || 'http://127.0.0.1:8771';
// A secure, reserved test origin matches deployed sandboxed Pages and avoids
// Chromium's local-network restrictions on an opaque iframe requesting localhost.
const browserBase = 'https://anamnesis.test';
const output = process.env.DASHBOARD_ARTIFACT_DIR || mkdtempSync(join(tmpdir(),'anamnesis-bridge-'));
mkdirSync(output,{recursive:true});
const html = readFileSync('pages/dashboard/index.html','utf8');
let server;
if(!process.env.DASHBOARD_BASE_URL) {
  server = spawn(process.env.PYTHON || 'python',['tests/browser/serve.py','--port','8771'],{stdio:'ignore',windowsHide:true});
  for(let n=0;n<50;n++){try{await fetch(base);break;}catch{await new Promise(resolve=>setTimeout(resolve,100));}}
}
const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE_PATH || (process.platform==='win32'?'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe':undefined)});
const passes=[];
try {
  for(const mode of ['normal','legacy-order','view-only','delayed','missing','rejected']) {
    const context = await browser.newContext({viewport:{width:1280,height:900}});
    await context.route(browserBase+'/**',async route=>{
      const url=new URL(route.request().url());
      const response=await fetch(base+url.pathname+url.search);
      return route.fulfill({status:response.status,contentType:response.headers.get('content-type') || 'text/plain',body:Buffer.from(await response.arrayBuffer())});
    });
    await context.route('**/pages/dashboard/index.html?bridge=*',route=>{
      let content = html;
      if(mode==='legacy-order') content=content.replaceAll('<script defer ','<script ');
      const timer = `<script>const nativeTimer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>nativeTimer(fn,ms===8000?400:ms,...args);</script>`;
      content=content.replace('<head>','<head>'+timer);
      const sdk=mode==='view-only'?'astrbot-plugin-view-v4.29.3.js':'astrbot-plugin-page-v4.27.5.js';
      let injection=mode==='missing'?'':`<script src="/tests/browser/vendor/${sdk}"></script>`;
      if(mode==='view-only') injection+='<script>delete window.AstrBotPluginPage;</script>';
      if(mode==='rejected') injection+='<script>const officialReady=window.AstrBotPluginPage.ready;window.fixtureReject=true;window.AstrBotPluginPage.ready=()=>window.fixtureReject?Promise.reject(new Error("Synthetic host rejection")):officialReady();</script>';
      content=content.replace('</body>',injection+'</body>');
      return route.fulfill({contentType:'text/html',body:content});
    });
    const page = await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('requestfailed',request=>console.error(mode,request.url(),request.failure()?.errorText));
    await page.goto(browserBase+'/tests/browser/bridge-navigation.html?mode='+mode);
    const frame=page.frames().find(frame=>frame.parentFrame());
    try {await frame.waitForFunction(()=>typeof window.lmState==='object');}
    catch(error){console.error(mode,errors,await frame.locator('body').innerText());throw error;}
    if(['normal','legacy-order','view-only'].includes(mode)) {
      // Assert first-load behavior before any navigation or graph button click.
      await frame.waitForFunction(()=>document.getElementById('gs-total').textContent==='7');
      await frame.waitForFunction(()=>document.getElementById('graph-legend').textContent.includes('1'));
      const initial = await page.evaluate(()=>window.navFixtureCalls.filter(call=>call.endpoint==='page/graph/overview'));
      assert.ok(initial.length>0,mode+' must automatically load the initial overview');
      assert.equal(initial[0].params?.full_graph,undefined);
    }
    // Navigation is installed before ready(), even with no host context.
    for(const name of ['administration','memory','recall','system','prompts','profiles','settings','graph']) {
      await frame.locator(`.nav-item[data-page="${name}"]`).click();
      assert.equal(await frame.locator('.page.active').getAttribute('id'),'page-'+name);
    }
    if(['missing','rejected','delayed'].includes(mode)) {
      await frame.locator('#bridge-feedback:not(.hidden) #bridge-retry:not([disabled])').waitFor();
      if(mode==='missing') await frame.addScriptTag({content:readFileSync('tests/browser/vendor/astrbot-plugin-page-v4.27.5.js','utf8')});
      if(mode==='rejected') await frame.evaluate(()=>{window.fixtureReject=false;});
      if(mode==='delayed') await page.evaluate(()=>window.fixtureConnect());
      await frame.locator('#bridge-retry').click();
    }
    await frame.waitForFunction(()=>document.getElementById('bridge-feedback').classList.contains('hidden'));
    await frame.waitForFunction(()=>document.getElementById('gs-total').textContent==='7');
    await page.waitForFunction(()=>window.navFixtureCalls.some(call=>call.endpoint==='page/graph/overview' && !call.params?.full_graph));
    await frame.waitForFunction(()=>document.getElementById('graph-legend').textContent.includes('1'));
    await frame.locator('#graph-overview-btn:not([disabled])').click();
    await page.waitForFunction(()=>window.navFixtureCalls.some(call=>call.endpoint==='page/graph/overview' && call.params?.full_graph==='true'));
    await frame.locator('#graph-overview-btn:not([disabled])').click();
    assert.deepEqual(errors,[],`${mode} page errors`);
    assert.doesNotMatch(await frame.locator('#toast').innerText(),/必须运行|Bridge not available|未找到 AstrBot/);
    if(mode==='normal') await page.screenshot({path:join(output,'graph-auto-load.png')});
    passes.push(`PASS ${mode}: all modules navigate; graph loads automatically; host recovery and overview work`);
    await context.close();
  }
  writeFileSync(join(output,'bridge-results.txt'),passes.join('\n')+'\n');
  console.log(passes.join('\n')); console.log(`Artifacts: ${output}`);
} finally {await browser.close();server?.kill();}
