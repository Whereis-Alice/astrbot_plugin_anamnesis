import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient } from '../../pages/dashboard/modules/api-client.js';

function bridge() {
  return {ready:async()=>({isDark:true}),apiGet:async(path,params)=>({path,params}),apiPost:async(path,body)=>({path,body})};
}
test('discovers SDK injected after client construction and preserves endpoint/query',async()=>{
  global.window={}; const client=new ApiClient({readyTimeoutMs:200});
  const request=client.get('/stats',{session_id:'a:b'});
  window.AstrBotPluginPage=bridge();
  assert.deepEqual(await request,{path:'page/stats',params:{session_id:'a:b'}});
});
test('accepts the current PluginView SDK without the compatibility alias',async()=>{
  global.window={AstrBotPluginView:bridge()};
  assert.deepEqual(await new ApiClient().ready(),{isDark:true});
});
test('ready rejection is recoverable and never dispatches a mutation',async()=>{
  global.window={AstrBotPluginPage:bridge()}; let writes=0;
  window.AstrBotPluginPage.apiPost=async()=>{writes++;return {status:'ok',data:{saved:true}};};
  window.AstrBotPluginPage.ready=async()=>{throw Error('host unavailable');};
  const client=new ApiClient({readyTimeoutMs:30});
  await assert.rejects(client.post('profiles/update',{value:'draft'}),/host unavailable/);
  assert.equal(writes,0);
  window.AstrBotPluginPage.ready=async()=>({});
  assert.deepEqual(await client.post('profiles/update',{value:'draft'}),{saved:true});
  assert.equal(writes,1);
});
test('missing SDK and stalled context time out and can reconnect',async()=>{
  global.window={}; const client=new ApiClient({readyTimeoutMs:10});
  await assert.rejects(client.ready(),/timed out/);
  window.AstrBotPluginPage=bridge();window.AstrBotPluginPage.ready=()=>new Promise(()=>{});
  await assert.rejects(client.ready(),/timed out/);
  window.AstrBotPluginPage.ready=async()=>({});
  assert.deepEqual(await client.ready(),{});
});
test('concurrent requests share readiness and POST failures are not retried',async()=>{
  global.window={AstrBotPluginPage:bridge()};let reads=0,writes=0;
  window.AstrBotPluginPage.ready=async()=>{reads++;return {};};
  window.AstrBotPluginPage.apiPost=async()=>{writes++;throw Error('write failed');};
  const client=new ApiClient();
  await Promise.all([client.get('stats'),client.get('memories')]);
  await assert.rejects(client.post('profiles/update',{}),/write failed/);
  assert.equal(reads,1);assert.equal(writes,1);
});
test('context reads reflect later host updates and replaced SDK',async()=>{
  global.window={AstrBotPluginPage:bridge()}; const client=new ApiClient();
  await client.ready();window.AstrBotPluginPage.getContext=()=>({isDark:false});
  assert.deepEqual(client.getContext(),{isDark:false});
  window.AstrBotPluginPage=bridge();window.AstrBotPluginPage.ready=async()=>({locale:'en-US'});
  assert.deepEqual(await client.ready(),{locale:'en-US'});
});

for(const outcome of ['reject','resolve']) test(`stale pending SDK ${outcome} cannot clear or overwrite its replacement`,async()=>{
  global.window={AstrBotPluginPage:bridge()};let finishA,failA,finishB,callsB=0;
  window.AstrBotPluginPage.ready=()=>new Promise((resolve,reject)=>{finishA=resolve;failA=reject;});
  const client=new ApiClient(), pendingA=client.ready();
  await Promise.resolve();
  window.AstrBotPluginPage=bridge();
  window.AstrBotPluginPage.ready=()=>{callsB++;return new Promise(resolve=>{finishB=resolve;});};
  const pendingB=client.ready();await Promise.resolve();
  if(outcome==='reject') {failA(Error('stale host'));await assert.rejects(pendingA,/stale host/);}
  else {finishA({host:'A'});assert.deepEqual(await pendingA,{host:'A'});assert.deepEqual(client.getContext(),{});}
  const anotherB=client.ready();await Promise.resolve();
  assert.equal(callsB,1);
  finishB({host:'B'});
  assert.deepEqual(await pendingB,{host:'B'});assert.deepEqual(await anotherB,{host:'B'});
  assert.deepEqual(client.getContext(),{host:'B'});
});

test('request waits for a replacement SDK instead of dispatching before its context',async()=>{
  global.window={AstrBotPluginPage:bridge()};let finishA,finishB,calls=0;
  window.AstrBotPluginPage.ready=()=>new Promise(resolve=>{finishA=resolve;});
  const client=new ApiClient(),request=client.get('stats');await Promise.resolve();
  window.AstrBotPluginPage=bridge();
  window.AstrBotPluginPage.ready=()=>new Promise(resolve=>{finishB=resolve;});
  window.AstrBotPluginPage.apiGet=async()=>{calls++;return {host:'B'};};
  finishA({host:'A'});
  for(let n=0;n<10;n++) await Promise.resolve();
  assert.equal(calls,0);assert.equal(typeof finishB,'function');
  finishB({host:'B'});assert.deepEqual(await request,{host:'B'});assert.equal(calls,1);
});
