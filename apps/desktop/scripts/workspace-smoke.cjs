// Runs actual compiled Electron windows against a controlled HTTP workbench.
const assert = require("node:assert/strict");
const http = require("node:http");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");
const { app, BrowserWindow, Menu, session } = require("electron");
const directory = mkdtempSync(path.join(tmpdir(), "sag-electron-smoke-"));
app.setPath("appData", directory);
app.setAppPath(path.resolve(__dirname, ".."));
const upstreamRequests = [];
const server = http.createServer((req, res) => {
  upstreamRequests.push(req.url);
  if (req.url === "/host-login" && req.method === "POST") {
    res.setHeader("Set-Cookie", "nas-session=controlled-login; Path=/; HttpOnly; SameSite=Lax");
    res.writeHead(303, { Location: "/" }); res.end();
  }
  else if (req.url === "/") {
    res.setHeader("Content-Type", "text/html");
    res.end(req.headers.cookie?.includes("nas-session=controlled-login")
      ? "<title>Controlled host</title><h1>Host signed in</h1>"
      : '<title>Controlled host login</title><form method="post" action="/host-login"><button>Sign in to host</button></form>');
  }
  else if (req.url === "/app/sag/chat" && req.headers.cookie?.includes("nas-session=controlled-login")) {
    res.setHeader("Content-Type", "text/html"); res.end("<h1>Knowledge sees host session</h1>");
  }
  else if (req.url.endsWith("/system/capabilities")) { res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify({ llm_configured: false })); }
  else { res.setHeader("Content-Type", "text/html"); res.end('<title>Controlled NAS</title><h1>Controlled NAS workspace</h1><a href="/app/sag/login">Login</a>'); }
});
const wait = async predicate => { for (let i = 0; i < 200; i++) { const result = predicate(); if (result) return result; await new Promise(resolve => setTimeout(resolve, 50)); } throw new Error("Timed out waiting for Electron UI"); };
let failed = false;
server.listen(0, "127.0.0.1", async () => {
  process.env.SAG_DESKTOP_DEV_WEB_URL = `http://127.0.0.1:${server.address().port}/fixture-local`;
  require("../dist/main.js");
  try {
    const local = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === process.env.SAG_DESKTOP_DEV_WEB_URL && !window.webContents.isLoading()));
    assert.ok(local, "fresh install opens local knowledge directly");
    assert.equal(BrowserWindow.getAllWindows().some(window => window.webContents.getURL().endsWith("workspaces.html")), false);
    Menu.getApplicationMenu().items.find(item => item.label === "工作区").submenu.items[0].click();
    const manager = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL().endsWith("workspaces.html")));
    await wait(() => !manager.webContents.isLoading());
    const url = `http://127.0.0.1:${server.address().port}/app/sag`;
    const first = await manager.webContents.executeJavaScript(`window.sagWorkspaces.save(${JSON.stringify({name:"Test NAS",webUrl:url,deployment:"fnos"})})`);
    assert.equal(await manager.webContents.executeJavaScript('typeof window.sagDesktop'), "undefined");
    await manager.webContents.executeJavaScript(`window.sagWorkspaces.open(${JSON.stringify(first.id)})`);
    let remote = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === `${url}/chat`));
    await wait(() => !remote.webContents.isLoading());
    assert.equal(await remote.webContents.executeJavaScript('typeof window.sagDesktop + ":" + typeof window.sagWorkspaces'), "undefined:undefined");
    assert.equal(remote.webContents.getLastWebPreferences().preload, undefined);
    assert.equal(remote.webContents.getLastWebPreferences().sandbox, true);
    await session.fromPartition(first.partition).cookies.set({url,name:"test-login",value:"identity-one"});
    const beforePopupWindows = BrowserWindow.getAllWindows().length;
    for (const target of [url + "/authorize?state=unrecognized-state", url + "/callback?state=mismatch&code=unrecognized", "https://unrecognized.example/authorize?state=unknown"]) {
      assert.equal(await remote.webContents.executeJavaScript(`window.open(${JSON.stringify(target)}) === null`), true);
    }
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(BrowserWindow.getAllWindows().length, beforePopupWindows);
    assert.equal(upstreamRequests.some(target => target.includes("/authorize") || target.includes("/callback")), false);
    await wait(() => !manager.webContents.isLoading());
    assert.match(await manager.webContents.executeJavaScript('document.getElementById("message").textContent'), /弹窗.*尚不支持/);
    // The ordinary same-origin main-window login route remains available.
    await remote.loadURL(url + "/login");
    assert.equal(await remote.webContents.executeJavaScript('typeof window.sagDesktop + ":" + typeof window.sagWorkspaces'), "undefined:undefined");
    const second = await manager.webContents.executeJavaScript(`window.sagWorkspaces.save(${JSON.stringify({name:"Second identity",webUrl:url,deployment:"fnos"})})`);
    assert.equal((await session.fromPartition(second.partition).cookies.get({url})).length, 0);
    assert.equal((await manager.webContents.executeJavaScript(`window.sagWorkspaces.check(${JSON.stringify(first.id)})`)).ok, true);
    assert.equal(await manager.webContents.executeJavaScript(`typeof window.sagWorkspaces.hostLogin`), "function");
    assert.equal(await manager.webContents.executeJavaScript('[...document.querySelectorAll("button")].some(button => button.textContent === "登录 NAS 宿主")'), true);
    await manager.webContents.executeJavaScript('[...document.querySelectorAll("button")].find(button => button.textContent === "登录 NAS 宿主").click(); true');
    const originRoot = new URL(url).origin + "/";
    const host = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === originRoot));
    await wait(() => !host.webContents.isLoading());
    assert.equal(host.webContents.session, session.fromPartition(first.partition));
    assert.equal(await host.webContents.executeJavaScript('typeof window.sagDesktop + ":" + typeof window.sagWorkspaces'), "undefined:undefined");
    const signedIn = new Promise(resolve => host.webContents.once("did-finish-load", resolve));
    await host.webContents.executeJavaScript('document.querySelector("form").requestSubmit(); true');
    await signedIn;
    assert.match(await host.webContents.executeJavaScript('document.body.textContent'), /Host signed in/);
    await manager.webContents.executeJavaScript(`document.querySelector('[data-profile-id="${first.id}"] button[data-action="open"]').click(); true`);
    remote = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === `${url}/chat`));
    await wait(() => !remote.webContents.isLoading());
    assert.match(await remote.webContents.executeJavaScript('document.body.textContent'), /Knowledge sees host session/);
    assert.equal(await remote.webContents.executeJavaScript('typeof window.sagDesktop + ":" + typeof window.sagWorkspaces'), "undefined:undefined");
    assert.equal((await session.fromPartition(second.partition).cookies.get({url})).length, 0);
    const standard = await manager.webContents.executeJavaScript(`window.sagWorkspaces.save(${JSON.stringify({name:"Standard Web",webUrl:url,deployment:"standard"})})`);
    await assert.rejects(manager.webContents.executeJavaScript(`window.sagWorkspaces.hostLogin(${JSON.stringify(standard.id)})`), /fnOS/);
    await manager.webContents.executeJavaScript(`window.sagWorkspaces.logout(${JSON.stringify(first.id)})`);
    assert.equal((await session.fromPartition(first.partition).cookies.get({url})).length, 0);
    const reloginHost = await wait(() => BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === originRoot));
    await wait(() => !reloginHost.webContents.isLoading());
    assert.match(await reloginHost.webContents.executeJavaScript('document.body.textContent'), /Sign in to host/);
    await new Promise(resolve => server.close(resolve));
    reloginHost.reload();
    await wait(() => manager.isVisible());
    await wait(() => !manager.webContents.isLoading());
    await wait(() => manager.webContents.getURL().endsWith("workspaces.html"));
    await new Promise(resolve => setTimeout(resolve, 500));
    assert.match(await manager.webContents.executeJavaScript('document.getElementById("message").textContent'), /连接不可用|加载失败|无法连接/);
    console.log("PASS actual Electron default local startup, optional connection settings, remote no preload, partition isolation, popup denial, host login, fnOS-only routing, logout and offline recovery");
  } catch (error) { failed = true; console.error(error); }
  finally { server.close(); app.exit(failed ? 1 : 0); }
});
app.on("quit", () => rmSync(directory, {recursive:true,force:true}));
