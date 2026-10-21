import { contextBridge, ipcRenderer } from "electron";
contextBridge.exposeInMainWorld("sagWorkspaces", Object.freeze({
  list: () => ipcRenderer.invoke("workspace:list"),
  save: (input: unknown) => ipcRenderer.invoke("workspace:save", input),
  open: (id: string) => ipcRenderer.invoke("workspace:open", id),
  hostLogin: (id: string) => ipcRenderer.invoke("workspace:host-login", id),
  remove: (id: string) => ipcRenderer.invoke("workspace:remove", id),
  logout: (id: string) => ipcRenderer.invoke("workspace:logout", id),
  check: (id: string) => ipcRenderer.invoke("workspace:check", id),
}));
