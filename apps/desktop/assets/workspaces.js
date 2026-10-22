"use strict";
const bridge = window.sagWorkspaces;
const element = id => document.getElementById(id);
const message = (text, error = false) => {
  element("message").textContent = text;
  element("message").dataset.error = String(error);
};
let busy = false;
let pendingFocus = null;
const focus = id => { if (busy) pendingFocus = id; else element(id).focus(); };
async function run(work) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button, input, select").forEach(control => { control.disabled = true; });
  try { await work(); }
  catch (error) { message(error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, "") : "操作失败，请重试", true); }
  finally {
    busy = false; document.querySelectorAll("button, input, select").forEach(control => { control.disabled = false; });
    if (pendingFocus) { element(pendingFocus).focus(); pendingFocus = null; }
  }
}
function button(text, action, className = "") {
  const node = document.createElement("button"); node.type = "button"; node.textContent = text; node.className = className;
  node.addEventListener("click", () => { node.closest("details")?.removeAttribute("open"); void run(action); });
  return node;
}
function edit(profile) {
  element("connection-form").reset();
  element("profile-id").value = profile?.id || "";
  element("name").value = profile?.name || "";
  element("web-url").value = profile ? profile.webOrigin + profile.appPrefix : "";
  element("deployment").value = profile?.deployment || "fnos";
  element("advanced").open = false;
  element("form-title").textContent = profile ? "编辑连接" : "添加连接";
  element("connections").hidden = true; element("add").hidden = true; element("editor").hidden = false;
  message(""); focus("web-url");
}
function closeEditor() {
  element("connection-form").reset(); element("profile-id").value = ""; element("advanced").open = false;
  element("editor").hidden = true; element("connections").hidden = false; element("add").hidden = false;
  focus("add");
}
async function load() {
  const state = await bridge.list(); element("connections").replaceChildren();
  if (state.message) message(state.message);
  for (const profile of state.profiles) {
    const row = document.createElement("div"); row.className = "connection"; row.dataset.profileId = profile.id;
    const info = document.createElement("div"); info.className = "connection-info";
    const heading = document.createElement("div"); heading.className = "connection-title";
    const title = document.createElement("strong"); title.textContent = profile.name; heading.append(title);
    if (state.activeId === profile.id) { const active = document.createElement("span"); active.className = "badge"; active.textContent = "当前连接"; heading.append(active); }
    const address = document.createElement("p"); address.className = "address";
    address.textContent = profile.kind === "local-managed" ? "在这台电脑运行" : profile.webOrigin + profile.appPrefix;
    info.append(heading, address); row.append(info);
    const actions = document.createElement("div"); actions.className = "actions";
    const open = button("打开", async () => {
      const result = await bridge.open(profile.id); await load();
      if (result.opened) message("已打开知识库。");
    });
    open.dataset.action = "open"; actions.append(open);
    if (profile.kind === "remote-web") {
      const more = document.createElement("details"); more.className = "more";
      const toggle = document.createElement("summary"); toggle.textContent = "更多"; toggle.setAttribute("aria-label", `${profile.name}的更多操作`);
      const menu = document.createElement("div"); menu.className = "menu";
      menu.append(button("编辑连接", async () => { edit(profile); }));
      menu.append(button("检查连接", async () => { const result = await bridge.check(profile.id); message(result.message); }));
      if (profile.deployment === "fnos") menu.append(button("登录 NAS 宿主", async () => { await bridge.hostLogin(profile.id); await load(); }));
      menu.append(button("重新登录", async () => {
        await bridge.logout(profile.id);
        if (profile.deployment === "fnos") await load();
        else message("已清除此连接的登录状态，请打开知识库重新登录。");
      }));
      menu.append(button("删除连接", async () => {
        if (!window.confirm("删除此连接并清除其登录状态？服务器上的知识库和任务会保留。")) return;
        await bridge.remove(profile.id); await load();
      }, "destructive"));
      more.append(toggle, menu); actions.append(more);
    }
    row.append(actions); element("connections").append(row);
  }
}
element("connection-form").addEventListener("submit", event => {
  event.preventDefault(); void run(async () => {
    const webUrl = element("web-url").value.trim();
    await bridge.save({ id: element("profile-id").value || undefined,
      name: element("name").value.trim() || new URL(webUrl).hostname,
      webUrl, deployment: element("deployment").value });
    await load(); closeEditor(); message("连接已保存，点击“打开”进入知识库。");
  });
});
element("add").addEventListener("click", () => { if (!busy) edit(); });
element("cancel").addEventListener("click", closeEditor);
document.addEventListener("keydown", event => {
  if (event.key !== "Escape" || busy) return;
  if (!element("editor").hidden) closeEditor();
  document.querySelectorAll("details.more[open]").forEach(menu => { menu.open = false; });
});
void run(load);
