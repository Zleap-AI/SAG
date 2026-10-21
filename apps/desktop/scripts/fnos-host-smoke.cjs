// Controlled host lifetime fixture around real compiled main, with isolated user data.
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),electron=require('electron');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(probe,timeout=15000){const start=Date.now();while(Date.now()-start<timeout){const result=await probe();if(result)return result;await sleep(50);}throw Error('Controlled Electron state timed out');}
async function worker({app,BrowserWindow,Menu,session}){
 const directory=process.env.SAG_HOST_SMOKE_DATA;app.setPath('appData',directory);app.setAppPath(path.resolve(__dirname,'..'));
 require(process.env.SAG_HOST_SMOKE_MAIN);
 app.on('before-quit',event=>console.log('QUIT before-quit',JSON.stringify({prevented:event.defaultPrevented,windows:BrowserWindow.getAllWindows().length})));
 app.on('will-quit',()=>console.log('QUIT will-quit'));app.on('quit',()=>console.log('QUIT complete'));
 let passed=false;
 try{
  await app.whenReady();const origin=process.env.SAG_HOST_SMOKE_ORIGIN;let manager;
  const loaded=url=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url&&!w.webContents.isLoading());
  const findManager=()=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().endsWith('workspaces.html')&&!w.webContents.isLoading());
  if(process.env.SAG_HOST_SMOKE_ROUND==='first'){
   manager=await wait(findManager);
   const saved=await manager.webContents.executeJavaScript(`sagWorkspaces.save(${JSON.stringify({name:'Controlled NAS',webUrl:origin+'/app/sag',deployment:'fnos'})})`);
   fs.writeFileSync(path.join(directory,'profile.json'),JSON.stringify(saved));
   await manager.webContents.executeJavaScript(`sagWorkspaces.hostLogin(${JSON.stringify(saved.id)})`);
   const host=await wait(()=>loaded(origin+'/'));
   await host.webContents.executeJavaScript('document.querySelector("form").requestSubmit();true');
   await wait(()=>host.webContents.executeJavaScript('document.body.textContent.includes("Host signed in")'));
   await wait(async()=>(await manager.webContents.executeJavaScript(`sagWorkspaces.check(${JSON.stringify(saved.id)})`)).ok);
   await manager.webContents.executeJavaScript(`sagWorkspaces.open(${JSON.stringify(saved.id)})`);
  }
  const knowledge=await wait(()=>loaded(origin+'/app/sag/chat'));
  assert.match(await knowledge.webContents.executeJavaScript('document.body.textContent'),/Knowledge authorized by live host/);
  const host=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===origin+'/');
  assert.ok(host&&!host.isDestroyed(),'knowledge must keep its host context alive');assert.equal(host.webContents.session,knowledge.webContents.session);
  for(const window of [host,knowledge]){assert.equal(await window.webContents.executeJavaScript('typeof sagDesktop+":"+typeof sagWorkspaces'),'undefined:undefined');assert.equal(window.webContents.getLastWebPreferences().sandbox,true);}
  await sleep(200);assert.equal(host.isVisible(),false,'late ready-to-show must not reveal hidden host');
  if(!manager){Menu.getApplicationMenu().items.find(i=>i.label==='工作区').submenu.items[0].click();manager=await wait(findManager);}
  const peer=await manager.webContents.executeJavaScript(`sagWorkspaces.save(${JSON.stringify({name:'Other identity',webUrl:origin+'/app/sag',deployment:'fnos'})})`);
  assert.equal((await session.fromPartition(peer.partition).cookies.get({url:origin})).length,0);
  assert.equal(await host.webContents.executeJavaScript(`window.open(${JSON.stringify(origin+'/authorize?state=unknown')})===null`),true);
  if(process.env.SAG_HOST_SMOKE_ROUND==='restart'){
   await wait(()=>!manager.webContents.isLoading());const saved=JSON.parse(fs.readFileSync(path.join(directory,'profile.json')));
   const openingPeer=manager.webContents.executeJavaScript(`sagWorkspaces.open(${JSON.stringify(peer.id)})`);
   const peerHost=await wait(()=>{const candidate=loaded(origin+'/');return candidate&&candidate!==host&&candidate.webContents.session===session.fromPartition(peer.partition)?candidate:undefined;});
   assert.equal(host.isDestroyed(),true);assert.equal(knowledge.isDestroyed(),true,
    'the old renderer must be gone before a pending new host allows its partition to be cleared');
   const logout=await manager.webContents.executeJavaScript(`sagWorkspaces.logout(${JSON.stringify(saved.id)})`);
   assert.equal(logout.opened,true);assert.equal((await openingPeer).opened,false);
   assert.equal(peerHost.isDestroyed(),true);
   assert.equal(host.isDestroyed(),true);assert.equal(knowledge.isDestroyed(),true);
   assert.equal((await session.fromPartition(saved.partition).cookies.get({url:origin})).length,0);
   const login=await wait(()=>loaded(origin+'/'));assert.match(await login.webContents.executeJavaScript('document.body.textContent'),/Sign in/);
   const activeLogout=await manager.webContents.executeJavaScript(`sagWorkspaces.logout(${JSON.stringify(saved.id)})`);
   assert.equal(activeLogout.opened,true,'active logout must reopen its fresh host instead of canceling its own generation');
   assert.equal(login.isDestroyed(),true);
   await wait(()=>loaded(origin+'/'));
  }
  passed=true;console.log('PASS compiled main live host, readiness, isolation, remote no bridge, popup denial',process.env.SAG_HOST_SMOKE_ROUND);
 }catch(error){console.error(error);}
 finally{fs.writeFileSync(process.env.SAG_HOST_SMOKE_RESULT,JSON.stringify({passed}));setTimeout(()=>app.quit(),100);}
}
async function driver(executable){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'sag-fnos-host-main-'));let liveHosts=0,logins=0;const streams=new Set();
 const server=http.createServer(async(req,res)=>{
  const signedIn=req.headers.cookie?.includes('host-fixture=signed-in');
  if(req.url==='/sign-in'&&req.method==='POST'){logins++;res.setHeader('Set-Cookie','host-fixture=signed-in; Max-Age=3600; Path=/; HttpOnly; SameSite=Lax');res.writeHead(303,{Location:'/'});res.end();}
  else if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(signedIn?'<h1>Host signed in</h1><script>window.hostConnection=new EventSource("/host-live")</script>':'<form method="post" action="/sign-in"><button>Sign in</button></form>');}
  else if(req.url==='/host-live'&&signedIn){liveHosts++;streams.add(res);res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('data: ready\n\n');req.on('close',()=>{liveHosts--;streams.delete(res);});}
  else if(req.url.endsWith('/system/capabilities')){if(!signedIn||liveHosts===0){res.writeHead(403);res.end('invalid token');}else{res.setHeader('Content-Type','application/json');res.end('{"llm_configured":false}');}}
  else if(req.url==='/app/sag/chat'){await sleep(100);res.setHeader('Content-Type','text/html');res.end(signedIn&&liveHosts>0?'<h1>Knowledge authorized by live host</h1>':'invalid token');}
  else{res.writeHead(404);res.end();}
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const index=process.argv.indexOf('--main');const main=index>=0?path.resolve(process.argv[index+1]):path.resolve(__dirname,'../dist/main.js');let successful=true;
 try{
  for(const round of ['first','restart']){
   const result=path.join(directory,round+'.json');let output='';const child=spawn(executable,[__filename,'--use-mock-keychain'],{env:{...process.env,SAG_HOST_SMOKE_DATA:directory,SAG_HOST_SMOKE_MAIN:main,SAG_HOST_SMOKE_ROUND:round,SAG_HOST_SMOKE_RESULT:result,SAG_HOST_SMOKE_ORIGIN:`http://127.0.0.1:${server.address().port}`}});
   child.stdout.on('data',chunk=>output+=chunk);child.stderr.on('data',chunk=>output+=chunk);const exited=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
   await wait(()=>fs.existsSync(result),30000);const exit=await Promise.race([exited,sleep(5000).then(()=>null)]);console.log(output);console.log('ROUND',JSON.stringify({round,result:JSON.parse(fs.readFileSync(result)),exit,liveHosts}));
   if(!exit){child.kill('SIGTERM');await exited;}if(!exit||exit.code!==0||!JSON.parse(fs.readFileSync(result)).passed){successful=false;break;}await wait(()=>liveHosts===0);
  }
  assert.equal(successful,true,'controlled main flow or programmatic quit failed');assert.equal(logins,1,'same-userdata restart must reuse normal host login');console.log('PASS controlled same-userdata restart, one fixture login and app.quit; native Quit remains manual acceptance');
 }finally{for(const stream of streams)stream.destroy();server.close();fs.rmSync(directory,{recursive:true,force:true});}
}
if(typeof electron==='string')driver(electron).catch(error=>{console.error(error);process.exitCode=1;});else void worker(electron);
