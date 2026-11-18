const path = require("node:path");
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { KeycloakAuth } = require("./auth");

const config = {
  apiUrl: (process.env.AIHR_API_URL || "http://localhost:8000").replace(/\/$/, ""),
  keycloakUrl: process.env.AIHR_KEYCLOAK_URL || "http://keycloak:8080",
  realm: process.env.AIHR_KEYCLOAK_REALM || "aihr",
  clientId: process.env.AIHR_KEYCLOAK_CLIENT_ID || "aihr-desktop",
  redirectUri: "http://127.0.0.1:47823/callback",
};

const auth = new KeycloakAuth(config);
let mainWindow = null;

const ALLOWED_METHODS = new Set(["GET", "POST"]);

// The renderer can only call the aihr API, by path, through here. It never sees a token.
async function apiRequest(_event, { method = "GET", path: apiPath, body } = {}) {
  if (!ALLOWED_METHODS.has(method) || typeof apiPath !== "string" || !apiPath.startsWith("/") || apiPath.startsWith("//")) {
    return { ok: false, status: 400, data: { detail: "invalid request" } };
  }

  const token = await auth.accessToken();
  if (!token) return { ok: false, status: 401, data: { detail: "signed out" } };

  try {
    const response = await fetch(config.apiUrl + apiPath, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text };
    }
    return { ok: response.ok, status: response.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { detail: `Cannot reach the API at ${config.apiUrl} (${err.cause?.code || err.message})` } };
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    title: "aihr",
    backgroundColor: "#f4f5f7",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

ipcMain.handle("auth:status", () => ({ signedIn: auth.isSignedIn(), apiUrl: config.apiUrl, keycloakUrl: config.keycloakUrl }));
ipcMain.handle("auth:login", async () => {
  try {
    await auth.login(mainWindow);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});
ipcMain.handle("auth:logout", async () => {
  await auth.logout();
  return { ok: true };
});
ipcMain.handle("api:request", apiRequest);

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
