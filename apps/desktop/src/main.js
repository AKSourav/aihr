const path = require("node:path");
const fs = require("node:fs/promises");
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { KeycloakAuth } = require("./auth");

const config = {
  apiUrl: (process.env.AIHR_API_URL || "http://localhost:8000").replace(/\/$/, ""),
  keycloakUrl: process.env.AIHR_KEYCLOAK_URL || "http://localhost:8080",
  realm: process.env.AIHR_KEYCLOAK_REALM || "aihr",
  clientId: process.env.AIHR_KEYCLOAK_CLIENT_ID || "aihr-desktop",
  redirectUri: "http://127.0.0.1:47823/callback",
};

const auth = new KeycloakAuth(config);
let mainWindow = null;

const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "DELETE"]);

async function send(method, apiPath, body, token) {
  try {
    const response = await fetch(config.apiUrl + apiPath, {
      method,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
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

// The renderer can only call the aihr API, by path, through here. It never sees a token.
async function apiRequest(_event, { method = "GET", path: apiPath, body } = {}) {
  if (!ALLOWED_METHODS.has(method) || typeof apiPath !== "string" || !apiPath.startsWith("/") || apiPath.startsWith("//")) {
    return { ok: false, status: 400, data: { detail: "invalid request" } };
  }

  const token = await auth.accessToken();
  if (!token) return { ok: false, status: 401, data: { detail: "signed out" } };

  return send(method, apiPath, body, token);
}

// Signup is the one call made before there is anything to authenticate with.
async function signupRequest(_event, body) {
  return send("POST", "/signup", body);
}

function validPath(apiPath) {
  return typeof apiPath === "string" && apiPath.startsWith("/") && !apiPath.startsWith("//");
}

// Files cross IPC as Uint8Arrays, so the renderer never learns a path and still never sees a token.
async function apiUpload(_event, { path: apiPath, fields = {}, field = "files", files = [] } = {}) {
  if (!validPath(apiPath)) return { ok: false, status: 400, data: { detail: "invalid request" } };

  const token = await auth.accessToken();
  if (!token) return { ok: false, status: 401, data: { detail: "signed out" } };

  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value !== null && value !== undefined) form.append(key, String(value));
  }
  for (const file of files) {
    form.append(field, new Blob([file.bytes], { type: "application/pdf" }), file.name);
  }

  try {
    const response = await fetch(config.apiUrl + apiPath, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
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
    return { ok: false, status: 0, data: { detail: `Cannot reach the API (${err.message})` } };
  }
}

// The window's CSP sets no object-src/frame-src, so an embedded PDF would be blocked. Hand it
// to the OS viewer instead, from a 0600 temp file that is removed on quit.
const tempFiles = new Set();

async function apiOpenFile(_event, { path: apiPath, name = "resume.pdf" } = {}) {
  if (!validPath(apiPath)) return { ok: false, status: 400, data: { detail: "invalid request" } };

  const token = await auth.accessToken();
  if (!token) return { ok: false, status: 401, data: { detail: "signed out" } };

  try {
    const response = await fetch(config.apiUrl + apiPath, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      const text = await response.text();
      let data = { detail: text };
      try {
        data = text ? JSON.parse(text) : data;
      } catch {}
      return { ok: false, status: response.status, data };
    }

    const bytes = Buffer.from(await response.arrayBuffer());
    const safe = String(name).replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100) || "resume.pdf";
    const file = path.join(app.getPath("temp"), `aihr-${Date.now()}-${safe}`);
    await fs.writeFile(file, bytes, { mode: 0o600 });
    tempFiles.add(file);

    const error = await shell.openPath(file);
    if (error) return { ok: false, status: 0, data: { detail: error } };
    return { ok: true, status: 200, data: null };
  } catch (err) {
    return { ok: false, status: 0, data: { detail: `Could not open the file (${err.message})` } };
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
ipcMain.handle("api:signup", signupRequest);
ipcMain.handle("api:upload", apiUpload);
ipcMain.handle("api:open-file", apiOpenFile);

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("will-quit", async () => {
  await Promise.all([...tempFiles].map((file) => fs.unlink(file).catch(() => {})));
  tempFiles.clear();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
