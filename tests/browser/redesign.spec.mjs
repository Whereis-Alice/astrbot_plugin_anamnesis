/** Real Edge/Chromium visual and interaction regression with synthetic data only.
 * Set DASHBOARD_ARTIFACT_DIR. Optional DASHBOARD_BASE_URL, PLAYWRIGHT_MODULE_PATH,
 * CHROMIUM_EXECUTABLE_PATH, DASHBOARD_QA_WIDTHS and DASHBOARD_QA_THEMES. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {installRedesignFixture} from './redesign-fixture.mjs';

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : 'playwright-core');
const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error('Set DASHBOARD_ARTIFACT_DIR to a task artifact directory');
mkdirSync(output, {recursive:true});
const base = process.env.DASHBOARD_BASE_URL || 'http://127.0.0.1:8778';
let server;
if (!process.env.DASHBOARD_BASE_URL) {
  server=spawn(process.env.PYTHON || 'python',['tests/browser/serve.py','--port','8778'],{stdio:'ignore',windowsHide:true});
  server.on('error', error=>{throw error;});
  for(let i=0;i<100;i++){try{await fetch(base);break;}catch{await new Promise(resolve=>setTimeout(resolve,100));}}
}
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE_PATH || (process.platform==='win32'?'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe':undefined)});
const widths=(process.env.DASHBOARD_QA_WIDTHS || '1280,1024,768,390').split(',').map(Number);
const themes=(process.env.DASHBOARD_QA_THEMES || 'light,dark').split(',');
const modules=['graph','memory','recall','profiles','system','prompts','settings','administration'];
const contentSelectors={graph:'#graph-canvas canvas',memory:'.memory-summary-text',recall:'.recall-result-item',profiles:'.profile-user',system:'#session-list .session-item',prompts:'.prompt-item',settings:'.settings-field',administration:'.admin-record'};
const secondaryControls={graph:['#graph-session-filter','#graph-overview-btn','#graph-memory-id','#graph-focus-btn'],memory:['#mem-session','#mem-status','#mem-type','#mem-sort','#mem-page-size','#mem-export','#mem-import'],recall:['#recall-mode','#recall-persona','#recall-start-time','#recall-end-time'],profiles:[],system:['#cons-run-btn'],prompts:['.prompt-edit-btn'],settings:['#settings-save'],administration:['#admin-preview','#admin-import','#admin-evaluate','#admin-event','#admin-repair']};
const checks=[],measurements=[],failures=[];
const nextFrame=page=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
async function revealControl(page,selector){
  const control=page.locator(selector).first();
  const closed=await control.evaluate(element=>{const result=[];for(let parent=element.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS'&&!parent.open)result.unshift(parent);return result.map(element=>element.id || null);});
  if(closed.some(id=>!id)) {
    // Find each ancestor's actual summary; changing open directly would skip
    // keyboard/pointer behavior, so mark only the fixture's locator target.
    await control.evaluate(element=>{let index=0;for(let parent=element.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS'&&!parent.open)parent.dataset.qaDetails=String(index++);});
    const count=await page.locator('details[data-qa-details]').count();
    for(let i=count-1;i>=0;i--){const details=page.locator(`details[data-qa-details="${i}"]`);if(await details.getAttribute('open')===null)await details.locator(':scope > summary').click();}
    await page.locator('details[data-qa-details]').evaluateAll(elements=>elements.forEach(element=>delete element.dataset.qaDetails));
  }else for(const id of closed)await page.locator(`#${id} > summary`).click();
  await control.scrollIntoViewIfNeeded();
  return control;
}
async function navigate(page,name){
  const mobile=page.viewportSize().width<=760;
  if(mobile&&!await page.locator('#app').evaluate(element=>element.classList.contains('mobile-nav-open')))await page.locator('#sidebar-toggle').click();
  await page.locator(`.nav-item[data-page="${name}"]`).click();
  await page.waitForFunction(name=>document.querySelector('.page.active')?.id==='page-'+name,name);
  if(mobile)assert.equal(await page.locator('#app').evaluate(element=>element.classList.contains('mobile-nav-open')),false,'selecting a module closes the drawer');
  await page.locator(contentSelectors[name]).first().waitFor();
  if(name==='graph')await page.waitForFunction(()=>window.Graph2D?.getDiagnostics().renderedNodes>0&&window.Graph2D.renderer._labelBoxes.length>0&&!document.getElementById('graph-canvas-state').textContent);
  await page.locator('.page.active').evaluate(element=>{element.scrollTop=0;for(const child of element.querySelectorAll('.page-body,.table-scroll'))child.scrollTop=0;});
  await nextFrame(page);
}
async function noOverflow(page,label){
  const result=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
  assert.ok(result.scroll<=result.width+1&&result.body<=result.width+1,`${label}: document overflows ${JSON.stringify(result)}`);
}
async function reachable(page,selector,label){
  const control=await revealControl(page,selector);
  if (!await control.isDisabled()) await control.click({trial:true});
  const box=await control.boundingBox(),viewport=page.viewportSize();
  assert.ok(box&&box.x>=-1&&box.x+box.width<=viewport.width+1,`${label}: ${selector} horizontally clipped ${JSON.stringify(box)}`);
  assert.ok(box.y>=-1&&box.y+box.height<=viewport.height+1,`${label}: ${selector} vertically unreachable ${JSON.stringify(box)}`);
}
async function capture(page,label){
  await page.mouse.move(1,1);
  await page.screenshot({path:join(output,label+'.png'),animations:'disabled'});
  const metric=await page.evaluate(()=>{
    const active=document.querySelector('.page.active'),main=document.querySelector('#workspace-main'),bar=document.querySelector('.workbench-bar');
    const box=element=>{const r=element?.getBoundingClientRect();return r?{x:r.x,y:r.y,width:r.width,height:r.height}:null;};
    const prose=[...active.querySelectorAll('.memory-summary-text,.profile-fact-value,.profile-preview-value,.recall-result-content,.prompt-item-desc,.admin-prose')];
    const visible=prose.filter(element=>{const r=element.getBoundingClientRect();return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0;});
    const area=visible.reduce((sum,element)=>{const r=element.getBoundingClientRect();return sum+Math.max(0,Math.min(innerWidth,r.right)-Math.max(0,r.left))*Math.max(0,Math.min(innerHeight,r.bottom)-Math.max(0,r.top));},0);
    return {viewport:{width:innerWidth,height:innerHeight},main:box(main),bar:box(bar),body:box(active.querySelector('.page-body')),visibleProseBlocks:visible.length,visibleProseArea:Math.round(area),proseViewportRatio:Number((area/(innerWidth*innerHeight)).toFixed(3)),firstProse:box(visible[0])};
  });
  measurements.push({label,...metric});
  assert.ok(metric.bar.height<=56,`${label}: compact topbar is ${metric.bar.height}px`);
  assert.ok(metric.main.width>=metric.viewport.width*.8,`${label}: main does not use the available reading width`);
  await noOverflow(page,label);
}
async function shellChecks(page,width,theme){
  const app=page.locator('#app'),sidebar=page.locator('#workspace-sidebar');
  assert.equal(await app.evaluate(element=>element.classList.contains('sidebar-expanded')),false,'desktop defaults to the narrow rail');
  assert.equal(await page.locator('#focus-toggle').getAttribute('aria-pressed'),'false');
  if(width>760){
    assert.ok(Math.abs((await sidebar.boundingBox()).width-60)<2,'default rail is 60px');
    await page.locator('#sidebar-toggle').click();await nextFrame(page);
    await page.waitForFunction(()=>Math.abs(document.querySelector('#workspace-sidebar').getBoundingClientRect().width-184)<2);
    assert.equal(await page.evaluate(()=>localStorage.getItem('anamnesis:sidebar-expanded')),'true');
    await page.reload();await page.waitForFunction(()=>document.getElementById('gs-total')?.textContent==='1286');
    assert.equal(await app.evaluate(element=>element.classList.contains('sidebar-expanded')),true,'expanded preference survives reload');
    await page.locator('#sidebar-toggle').click();await page.waitForFunction(()=>Math.abs(document.querySelector('#workspace-sidebar').getBoundingClientRect().width-60)<2);
    assert.equal(await page.evaluate(()=>localStorage.getItem('anamnesis:sidebar-expanded')),'false');
    checks.push(`PASS ${width}/${theme}: 60px rail, 184px sidebar and persisted preference`);
  }else{
    assert.equal(await sidebar.getAttribute('inert'),'','closed mobile navigation is inert');
    for(const close of ['escape','backdrop','button']){
      await page.locator('#sidebar-toggle').click();
      await page.waitForFunction(()=>document.querySelector('#app').classList.contains('mobile-nav-open'));
      assert.equal(await page.locator('#workspace-main').getAttribute('inert'),'');
      await page.locator('.nav-item[data-page="settings"]').click({trial:true});
      await page.screenshot({path:join(output,`drawer-${width}-${theme}-${close}.png`),animations:'disabled'});
      if(close==='escape')await page.keyboard.press('Escape');
      if(close==='backdrop')await page.locator('#sidebar-backdrop').click({position:{x:width-10,y:100}});
      if(close==='button')await page.locator('#sidebar-close').click();
      await page.waitForFunction(()=>!document.querySelector('#app').classList.contains('mobile-nav-open'));
      assert.equal(await page.locator('#workspace-main').getAttribute('inert'),null);
      assert.equal(await page.evaluate(()=>document.activeElement?.id),'sidebar-toggle');
    }
    assert.equal(await page.evaluate(()=>localStorage.getItem('anamnesis:sidebar-expanded')),null,'mobile interactions do not change the desktop preference');
    checks.push(`PASS ${width}/${theme}: mobile drawer, inert main, backdrop/Escape/close, focus return`);
  }
}
try{
  for(const width of widths)for(const theme of themes){
    const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
    await context.addInitScript(installRedesignFixture);
    const page=await context.newPage(),errors=[];
    page.setDefaultTimeout(8000);page.on('pageerror',error=>errors.push(error.message));
    try{
      await page.goto(`${base}/pages/dashboard/index.html?theme=${theme}`);
      await page.waitForFunction(()=>document.getElementById('gs-total')?.textContent==='1286');
      await page.evaluate(()=>document.fonts.ready);
      assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
      await shellChecks(page,width,theme);
      for(const name of modules){
        try{
          if(name==='recall'){
            // The form renders before a request has results.
            if(width<=760)await page.locator('#sidebar-toggle').click();
            await page.locator('.nav-item[data-page="recall"]').click();
            await (await revealControl(page,'#recall-query')).fill('周末有什么安排，平时喜欢读什么书？');
            await (await revealControl(page,'#recall-search-btn')).click();
          }
          await navigate(page,name);
          await capture(page,`${name}-${width}-${theme}`);
          if(name==='graph')assert.ok(await page.evaluate(()=>window.Graph2D.renderer._labelBoxes.every(box=>box.x1>=0&&box.x2<=window.Graph2D.renderer.width)),'overview labels fit the canvas horizontally');
          if(name==='system'&&width<=760)assert.ok((await page.locator('#system-stat-cards').boundingBox()).height<180,'mobile statistics fit in two compact rows');
          const primary={graph:'#graph-search-btn',memory:'#mem-keyword',recall:'#recall-search-btn',profiles:'#profile-refresh',system:'#cons-run-btn',prompts:'.prompt-edit-btn',settings:'#settings-search',administration:'#administration-refresh'}[name];
          await reachable(page,primary,`${name}/${width}/${theme}`);
          const details=page.locator('.page.active details.toolbar-details:visible').first();
          if(await details.count()){
            if(await details.getAttribute('open')!==null)await details.locator(':scope > summary').click();
            await details.locator(':scope > summary').click();
            assert.notEqual(await details.getAttribute('open'),null);
            await noOverflow(page,`${name} expanded tools/${width}/${theme}`);
            await page.screenshot({path:join(output,`${name}-tools-${width}-${theme}.png`),animations:'disabled'});
            await details.locator(':scope > summary').click();
          }
          for(const selector of secondaryControls[name])await reachable(page,selector,`${name}/${width}/${theme}`);
          // Restore the default reading state after checking advanced controls.
          while(await page.locator('.page.active details.toolbar-details[open]:visible').count())await page.locator('.page.active details.toolbar-details[open]:visible').last().locator(':scope > summary').click();
          await page.locator('.page.active').evaluate(element=>{element.scrollTop=0;for(const child of element.querySelectorAll('.page-body,.table-scroll'))child.scrollTop=0;});
          if(name==='memory'){
            const metric=measurements.at(-1);
            assert.ok(metric.visibleProseBlocks>=3,`at least three memory summaries are visible at ${width}px`);
            const normal=(await page.locator('#workspace-main').boundingBox()).width;
            await page.locator('#focus-toggle').click();
            assert.equal(await page.locator('#focus-toggle').getAttribute('aria-pressed'),'true');
            await nextFrame(page);await capture(page,`memory-focus-${width}-${theme}`);
            const focused=(await page.locator('#workspace-main').boundingBox()).width;
            if(width>760)assert.ok(focused>=normal+58,'focus mode returns sidebar space to reading');
            await reachable(page,'#focus-toggle','focus exit');await page.locator('#focus-toggle').click();
            assert.equal(await page.locator('#focus-toggle').getAttribute('aria-pressed'),'false');
            assert.match(await page.locator('#workbench-page-title').textContent(),/记忆/);
          }
          if(name==='profiles'){
            await page.locator('[data-profile-user="0"]').click();
            await page.locator('.profile-fact-value').first().waitFor();
            await capture(page,`profiles-reader-${width}-${theme}`);
            const facts=await page.locator('.profile-fact-value').evaluateAll(elements=>elements.map(element=>({width:element.clientWidth,scroll:element.scrollWidth,clamp:getComputedStyle(element).webkitLineClamp,font:parseFloat(getComputedStyle(element).fontSize)})));
            assert.equal(facts.length,6);assert.ok(facts.every(item=>item.scroll<=item.width+1&&item.clamp==='none'&&item.font>=14),'all six profile facts remain readable and untruncated');
            await page.locator('#focus-toggle').click();await nextFrame(page);
            await capture(page,`profiles-focus-${width}-${theme}`);
            await reachable(page,'#focus-toggle','profile focus exit');await page.locator('#focus-toggle').click();
            await reachable(page,'#profile-alias','profile settings');
            await page.locator('#profile-reader-tools > summary').click();
            await reachable(page,'#profile-person-close','return to directory');await page.locator('#profile-person-close').click();
            await page.locator('.profile-user').first().waitFor();
          }
          checks.push(`PASS ${name}/${width}/${theme}: real data, screenshot, no overflow, primary control reachable`);
        }catch(error){failures.push(`${name}/${width}/${theme}: ${error.message}`);await page.screenshot({path:join(output,`FAIL-${name}-${width}-${theme}.png`),animations:'disabled'});}
        finally{if(await page.locator('#focus-toggle').getAttribute('aria-pressed')==='true')await page.locator('#focus-toggle').click();}
      }
      assert.deepEqual(errors,[],`page errors at ${width}/${theme}`);
    }catch(error){failures.push(`shell/${width}/${theme}: ${error.message}`);await page.screenshot({path:join(output,`FAIL-shell-${width}-${theme}.png`),animations:'disabled'});}
    finally{await context.close();}
    console.log(`Completed ${width}/${theme}; ${failures.length} failure(s)`);
  }
}finally{
  writeFileSync(join(output,'redesign-results.json'),JSON.stringify({checks,failures,measurements},null,2)+'\n');
  writeFileSync(join(output,'redesign-results.txt'),[...checks,...failures.map(line=>'FAIL '+line)].join('\n')+'\n');
  await browser.close();server?.kill();
}
console.log(checks.join('\n'));console.log(`Artifacts: ${output}`);
assert.deepEqual(failures,[],failures.join('\n'));
