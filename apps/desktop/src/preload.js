const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aihr", {
  status: () => ipcRenderer.invoke("auth:status"),
  login: () => ipcRenderer.invoke("auth:login"),
  logout: () => ipcRenderer.invoke("auth:logout"),
  get: (path) => ipcRenderer.invoke("api:request", { method: "GET", path }),
  post: (path, body) => ipcRenderer.invoke("api:request", { method: "POST", path, body }),
  put: (path, body) => ipcRenderer.invoke("api:request", { method: "PUT", path, body }),
  signup: (body) => ipcRenderer.invoke("api:signup", body),
});
