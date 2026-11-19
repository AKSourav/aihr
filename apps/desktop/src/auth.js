// Keycloak sign-in for a desktop app: authorization code flow with PKCE (RFC 7636 / RFC 8252).
//
// The login page opens in its own window. Keycloak redirects to a loopback URL that nothing
// listens on; we intercept that navigation, take the code, and exchange it for tokens.
// Tokens live only in this (main) process and never reach the renderer.

const crypto = require("node:crypto");
const { BrowserWindow, session } = require("electron");

const AUTH_PARTITION = "aihr-auth";
const REFRESH_SKEW_MS = 30_000;

function base64url(buffer) {
  return buffer.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

class KeycloakAuth {
  constructor({ keycloakUrl, realm, clientId, redirectUri }) {
    this.issuer = `${keycloakUrl.replace(/\/$/, "")}/realms/${realm}`;
    this.clientId = clientId;
    this.redirectUri = redirectUri;
    this.tokens = null;
    this.refreshing = null;
  }

  endpoint(name) {
    return `${this.issuer}/protocol/openid-connect/${name}`;
  }

  isSignedIn() {
    return this.tokens !== null;
  }

  login(parent) {
    const verifier = base64url(crypto.randomBytes(32));
    const challenge = base64url(crypto.createHash("sha256").update(verifier).digest());
    const state = base64url(crypto.randomBytes(16));

    const url = new URL(this.endpoint("auth"));
    url.search = new URLSearchParams({
      client_id: this.clientId,
      redirect_uri: this.redirectUri,
      response_type: "code",
      scope: "openid email profile",
      code_challenge: challenge,
      code_challenge_method: "S256",
      state,
    }).toString();

    return new Promise((resolve, reject) => {
      const win = new BrowserWindow({
        parent,
        modal: true,
        width: 480,
        height: 680,
        title: "Sign in",
        autoHideMenuBar: true,
        webPreferences: { partition: AUTH_PARTITION, contextIsolation: true, sandbox: true },
      });
      let settled = false;

      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        if (!win.isDestroyed()) win.close();
        error ? reject(error) : resolve(value);
      };

      const onNavigate = (event, target) => {
        if (!target.startsWith(this.redirectUri)) return;
        event.preventDefault();
        const params = new URL(target).searchParams;
        if (params.get("state") !== state) return finish(new Error("Sign-in state mismatch, please try again."));
        if (params.get("error")) return finish(new Error(params.get("error_description") || params.get("error")));
        this.exchange(params.get("code"), verifier).then(() => finish(null, true), (err) => finish(err));
      };

      win.webContents.on("will-redirect", onNavigate);
      win.webContents.on("will-navigate", onNavigate);
      win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      win.on("closed", () => finish(new Error("Sign-in window was closed.")));
      win.loadURL(url.toString()).catch((err) => {
        if (!settled) finish(new Error(`Could not reach Keycloak at ${this.issuer} (${err.code || err.message}).`));
      });
    });
  }

  async exchange(code, verifier) {
    await this.tokenRequest({
      grant_type: "authorization_code",
      code,
      redirect_uri: this.redirectUri,
      code_verifier: verifier,
    });
  }

  async tokenRequest(form) {
    const response = await fetch(this.endpoint("token"), {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: this.clientId, ...form }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      this.tokens = null;
      throw new Error(body.error_description || body.error || `Token request failed (${response.status})`);
    }
    this.tokens = {
      access: body.access_token,
      refresh: body.refresh_token,
      idToken: body.id_token,
      expiresAt: Date.now() + body.expires_in * 1000,
    };
  }

  // Returns a valid access token, refreshing it if needed, or null when signed out.
  async accessToken() {
    if (!this.tokens) return null;
    if (Date.now() < this.tokens.expiresAt - REFRESH_SKEW_MS) return this.tokens.access;
    if (!this.refreshing) {
      this.refreshing = this.tokenRequest({ grant_type: "refresh_token", refresh_token: this.tokens.refresh })
        .catch(() => {
          this.tokens = null;
        })
        .finally(() => {
          this.refreshing = null;
        });
    }
    await this.refreshing;
    return this.tokens ? this.tokens.access : null;
  }

  async logout() {
    const tokens = this.tokens;
    this.tokens = null;
    if (tokens && tokens.refresh) {
      await fetch(this.endpoint("logout"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ client_id: this.clientId, refresh_token: tokens.refresh }),
      }).catch(() => {});
    }
    await session.fromPartition(AUTH_PARTITION).clearStorageData();
  }
}

module.exports = { KeycloakAuth };
