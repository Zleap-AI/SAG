"use strict";
const bridge = window.sagWorkspaces;
const element = id => document.getElementById(id);
const message = text => { element("message").textContent = text; };
let busy = false;
async function run(work) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try { await work(); } catch (error) { message(error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, "") : "操作失败，请重试"); }
  finally { busy = false; document.querySelectorAll("button").forEach(button => { button.disabled = false; }); }
}
function button(text, action) { const node = document.createElement("button"); node.type = "button"; node.textContent = text; node.addEventListener("click", () => run(action)); return node; }
async function load() {
  const state = await bridge.list(); element("connections").replaceChildren();
  if (state.message) message(state.message);
  for (const profile of state.profiles) {
    const card = document.createElement("div"); card.className = "connection";
    const title = document.createElement("strong"); title.textContent = profile.name; card.append(title);
    if (state.activeId === profile.id) { const active = document.createElement("span"); active.className = "badge"; active.textContent = " · 当前工作区"; card.append(active); }
    const address = document.createElement("p"); address.className = "address";
    address.textContent = profile.kind === "local-managed" ? "在这台电脑运行" : profile.webOrigin + profile.appPrefix; card.append(address);
    card.append(button(profile.kind === "remote-web" && profile.deployment === "fnos" ? "返回知识库" : "打开工作区", async () => { const result = await bridge.open(profile.id); if (result.opened) message("已打开工作区。登录后可在工作台确认当前身份。"); await load(); }));
    if (profile.kind === "remote-web") {
      if (profile.deployment === "fnos") {
        const hint = document.createElement("p"); hint.className = "help";
        hint.textContent = "客户端会先恢复 NAS 登录状态，再打开知识库；需要登录时会显示宿主窗口。登录状态仅保存在此连接中。";
        card.append(hint);
        card.append(button("登录 NAS 宿主", async () => { await bridge.hostLogin(profile.id); await load(); }));
      }
      card.append(button("编辑", async () => { element("profile-id").value = profile.id; element("name").value = profile.name; element("web-url").value = profile.webOrigin + profile.appPrefix; element("deployment").value = profile.deployment; element("form-title").textContent = "编辑连接"; element("name").focus(); }));
      card.append(button("检查连接", async () => { const result = await bridge.check(profile.id); message(result.message); }));
      card.append(button("重新登录", async () => {
        await bridge.logout(profile.id);
        if (profile.deployment === "fnos") await load();
        else message("已清除该连接的登录状态。打开工作区后重新登录。");
      }));
      card.append(button("删除连接", async () => { if (!window.confirm("删除本机连接并清除其登录状态？服务器数据与任务会保留。")) return; await bridge.remove(profile.id); await load(); }));
    }
    element("connections").append(card);
  }
}
element("connection-form").addEventListener("submit", event => { event.preventDefault(); run(async () => {
  await bridge.save({ id: element("profile-id").value || undefined, name: element("name").value,
    webUrl: element("web-url").value, deployment: element("deployment").value });
  element("connection-form").reset(); message("连接已保存。选择打开工作区，完成服务器登录。"); await load();
}); });
element("clear").addEventListener("click", () => { element("connection-form").reset(); element("profile-id").value = ""; element("form-title").textContent = "连接 NAS / 远程工作台"; });
run(load);
