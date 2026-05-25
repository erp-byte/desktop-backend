# 1. Frontend — Authentication & Authorization

> **What this file is:** a self-contained prompt to paste into a frontend coding
> session (Claude Code / Cursor / Aider / etc.) to implement auth + RBAC for
> both clients of the Candor Consumption backend. The backend contract is
> locked; do **not** change it.

---

## 0. Mission

Build the authentication and authorization layer for **two** clients of the
Candor Consumption backend:

| Target       | Stack                                                                                                    |
|--------------|----------------------------------------------------------------------------------------------------------|
| Desktop app  | Electron 28+, React 18+, TypeScript 5+, React Router 6, `axios`, Zustand (or React Context), `electron-store` |
| Android app  | Native Java 17, package `com.candor.ims`, minSdk 26, Material Components, Retrofit 2.11 + OkHttp + Gson, ViewBinding |

Both clients hit the **same** FastAPI backend:

```
Base URL (prod):    https://desktop-backend-vhf0.onrender.com
Base URL (local):   http://localhost:8000
Auth path prefix:   /api/v1/auth
Header on every call after login:    Authorization: Bearer <access_token>
```

Deliver, per client:
1. Token storage (encrypted at rest)
2. An `apiClient` that auto-attaches `Authorization: Bearer …`, transparently refreshes on 401, retries the original request once, and forces re-login on refresh failure
3. An auth state store with `currentUser`, `permissions[]`, `roles[]`, `isAdmin`, plus a `can(module, subModule, action)` helper
4. The 8 user-facing screens listed in §7
5. Route / activity guards (`<RequireAuth>`, `<RequirePermission>` for React; `BaseAuthActivity` + `PermissionGuard` for Android)
6. The 6 admin-only screens listed in §7.2 — only mounted/visible when `isAdmin === true`

The frontend permission check is a UX **hint** to hide buttons / routes early; the server is authoritative on every request.

---

## 1. Hard rules (do not violate)

1. **NEVER** persist `access_token` or `refresh_token` in plain `localStorage`, plain `SharedPreferences`, plain IndexedDB, or any place readable by another app/process.
   - **Electron**: encrypt with `safeStorage.encryptString` (main process), persist the ciphertext in `electron-store`. Expose only an `apiClient.request(...)` IPC channel to the renderer; tokens never enter `window`.
   - **Android**: persist via `EncryptedSharedPreferences` (AndroidX Security Crypto) with `MasterKey.KeyScheme.AES256_GCM`.
2. **NEVER** send the `refresh_token` on any endpoint except `/api/v1/auth/refresh` and `/api/v1/auth/logout`. The access token goes on every other request.
3. **NEVER** log token values. Mask as `Bearer ****` in any debug output. Strip them from crash reports.
4. **NEVER** decode the JWT to make security decisions on the client. Treat tokens as opaque blobs. You MAY decode `exp` to schedule a proactive refresh, but the server is the only authority on validity.
5. The `permissions[]` array from `/me` is a UX hint only. Do not assume it grants access — every server call is independently authorized.
6. **Always** capture the `X-Request-ID` header (or `details.request_id`) from error responses and surface it in user-facing error toasts: `"Error <error_code> — request ID abc-123"`. Support uses this to grep backend logs.
7. **Phone numbers**: send the user's raw input. Do not normalize on the client. The backend accepts `9876543210`, `09876543210`, `+919876543210`, `919876543210` and normalizes server-side.
8. **Refresh rotation**: every successful `/refresh` returns BOTH a new `access_token` AND a new `refresh_token`. Replace both in storage **atomically** before continuing. Reusing an old refresh token revokes the entire token family — the user is force-logged-out on every device.
9. After `/password/change`, the server revokes every OTHER active refresh token for this user (the current session keeps working). Show a toast: *"Other devices have been signed out."*
10. **HTTP 423 (`account_locked`) and 429 (`rate_limit_exceeded`)** are user-recoverable. Show the unlock / retry-after time. Do **not** auto-retry.
11. **Concurrent 401s**: when N requests in flight all 401, do exactly **one** refresh call and queue the others on the result. Multiple parallel `/refresh` calls would trigger reuse-detection and revoke the whole chain.
12. On any of `token_reuse_detected`, `invalid_refresh_token`, `token_expired` (during refresh), `account_disabled`, `account_suspended`, or a 401 from `/refresh` itself: clear tokens, kick to login, and show the appropriate message.

---

## 2. Backend contract — the only truth

### 2.1 Universal error envelope

Every non-2xx response has this shape:

```json
{
  "error": "<machine_code>",
  "message": "<human_readable>",
  "request_id": "<uuid>",
  "timestamp": "2026-05-07T10:23:45.123Z",
  "details": {}
}
```

The `X-Request-ID` response header always carries the same UUID. Every response (success or error) also carries `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

### 2.2 Token model

| Token       | TTL (default)         | Where it goes                                   | Storage                |
|-------------|-----------------------|-------------------------------------------------|------------------------|
| `access`    | 15 min (`900` s)      | `Authorization: Bearer …` on every API call     | encrypted, short-lived |
| `refresh`   | 8 hours (`28800` s)   | only request body of `/refresh` and `/logout`   | encrypted, longer-lived |

Both are JWTs signed `HS256`. Issuer is `candor-consumption`. The exact TTL is in the login/refresh response (`expires_in` and `refresh_expires_in`) — read it from there, never hardcode.

The refresh token rotates on every `/refresh`. The server keeps a refresh-token chain per login event; a reused jti fails reuse-detection and revokes the entire chain.

### 2.3 The 8 user-facing endpoints

```
POST    /api/v1/auth/login                     → 200 LoginResponse
POST    /api/v1/auth/refresh                   → 200 RefreshResponse
POST    /api/v1/auth/logout                    → 204
POST    /api/v1/auth/logout-all                → 200 { revoked_count }
GET     /api/v1/auth/me                        → 200 MeResponse
POST    /api/v1/auth/password/change           → 200 { message, revoked_count }
GET     /api/v1/auth/sessions                  → 200 SessionsResponse
DELETE  /api/v1/auth/sessions/{token_id}       → 204
```

#### POST `/login`  *(no auth)*
Request:
```json
{
  "phone": "9876543210",
  "password": "MyPass1234!",
  "device_info": {
    "device_id": "stable-uuid-per-install",
    "device_name": "Kaushal's Pixel 8",
    "app_version": "1.1.0",
    "platform": "android"
  }
}
```
- `phone`: required, string, min 1. Send raw user input.
- `password`: required, string, min 1.
- `device_info`: optional but recommended. Free-form JSON. The backend stores it on the refresh-token row so the user sees it in `/sessions`.

Response 200:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 28800,
  "must_change_password": false,
  "user": {
    "user_id": "1",
    "phone": "+919876543210",
    "full_name": "Kaushal Patel",
    "email": "kaushal@candorfoods.in",
    "is_admin": true,
    "roles": [
      { "role_id": "1", "code": "admin", "label": "Full unrestricted access", "is_admin": true }
    ]
  }
}
```

If `must_change_password === true` → after persisting tokens, redirect to the **Force Change Password** screen. The user can call `/me` and `/password/change`, but **block all other navigation** until the password is changed.

#### POST `/refresh`  *(no auth header — refresh token in body)*
Request:
```json
{ "refresh_token": "<current refresh JWT>" }
```
Response 200:
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 28800
}
```
On any 401 from `/refresh` → clear tokens, redirect to login.

#### POST `/logout`  *(bearer required)*
Request:
```json
{ "refresh_token": "<current refresh JWT>" }
```
Response: `204 No Content`. Idempotent — silently succeeds even on a stolen / cross-user / already-revoked token. Always clear local tokens after calling, regardless of HTTP status.

#### POST `/logout-all`  *(bearer required)*
Response 200:
```json
{ "revoked_count": 3 }
```
After this call, ALL refresh tokens for the user are revoked, including the current one. Clear local tokens and route to login.

#### GET `/me`  *(bearer required)*
Response 200:
```json
{
  "user_id": "1",
  "phone": "+919876543210",
  "full_name": "Kaushal Patel",
  "email": "kaushal@candorfoods.in",
  "status": "active",
  "must_change_password": false,
  "is_admin": true,
  "roles": [
    { "role_id": "1", "code": "admin", "label": "Full unrestricted access", "is_admin": true }
  ],
  "permissions": [
    { "module": "production", "sub_module": "plans", "action": "view" },
    { "module": "production", "sub_module": "job_cards", "action": "view" }
  ],
  "entities": ["cfpl"],
  "warehouses": ["W202"],
  "floors": [],
  "last_login_at": "2026-05-07T10:00:00Z",
  "password_changed_at": "2026-04-30T08:30:00Z"
}
```
Call `/me` immediately after login to populate the auth store, then again whenever the user re-launches the app with a still-valid access token. The `permissions[]` is your authoritative source for the `can()` helper. Note: server-side per-permission scope (entity / warehouse / floor) is NOT included in this list — only the user-level defaults are exposed via `entities` / `warehouses` / `floors`. Use those as the default scope for hint-level UI, but always send the relevant `entity` / `floor` query params on protected endpoints; the server enforces the real scope.

`status` ∈ `"active" | "suspended" | "disabled"`. Anything other than `"active"` → force re-login with the appropriate message.

#### POST `/password/change`  *(bearer required)*
Request:
```json
{
  "old_password": "OldPass1234",
  "new_password": "NewStrongPass5678",
  "confirm_password": "NewStrongPass5678"
}
```
Response 200:
```json
{
  "message": "Password changed successfully. All sessions revoked.",
  "revoked_count": 2
}
```
After success: the **current** session keeps working. All other sessions are revoked. Show toast `"Other devices have been signed out (n)"`. Then clear `must_change_password` flag in local state and unblock navigation.

#### GET `/sessions`  *(bearer required)*
Response 200:
```json
{
  "sessions": [
    {
      "token_id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
      "token_type": "refresh",
      "issued_at": "2026-05-07T08:00:00Z",
      "expires_at": "2026-05-07T16:00:00Z",
      "ip": "203.0.113.42",
      "user_agent": "Mozilla/5.0 ...",
      "device_info": {
        "device_id": "stable-uuid",
        "device_name": "Kaushal's Pixel 8",
        "platform": "android"
      },
      "is_current": true
    }
  ],
  "total": 1
}
```

#### DELETE `/sessions/{token_id}`  *(bearer required)*
Path param is the `token_id` (jti) from `/sessions`. Returns `204` on success, `404` (`session_not_found`) if it doesn't exist OR belongs to another user (no leak).

If the user revokes their **own current** session (`is_current: true`) — clear local tokens and route to login.

### 2.4 Admin-only endpoints (only render UI when `isAdmin === true`)

```
POST    /api/v1/auth/users                     → create user
GET     /api/v1/auth/users                     → list users
PUT     /api/v1/auth/users/{user_id}           → edit user (partial)
DELETE  /api/v1/auth/users/{user_id}           → deactivate user (revokes all their sessions)
POST    /api/v1/auth/users/{user_id}/reset-password → admin reset (forces must_change_password=TRUE)

GET     /api/v1/auth/roles                     → list roles
POST    /api/v1/auth/roles                     → create role
GET     /api/v1/auth/roles/{role_id}/permissions → role's permissions + scope
PUT     /api/v1/auth/roles/{role_id}/permissions → set role permissions (replaces all)

GET     /api/v1/auth/permissions               → list permissions  (?module=production filter)
GET     /api/v1/auth/permissions/hierarchy     → 3-level tree
POST    /api/v1/auth/permissions/create        → create one permission
PUT     /api/v1/auth/permissions/{id}          → edit permission (partial)
DELETE  /api/v1/auth/permissions/{id}          → delete permission

GET     /api/v1/auth/modules                   → list modules with sub_modules + perm count
POST    /api/v1/auth/modules                   → create module + auto view/create/edit/delete perms
```

Schemas — see `app/modules/auth/router.py` for exact wire format. Key shapes:

```jsonc
// POST /users
{
  "phone": "9876543211",
  "password": "TempPass1234",
  "full_name": "Ramesh Kumar",
  "role_id": 4,
  "email": "ramesh@candorfoods.in",   // optional
  "entity": "cfpl",                   // optional: "cfpl" | "cdpl" | null
  "allowed_warehouses": ["W202"]      // optional
}

// PUT /users/{id}  (partial — server allowlists fields)
{ "role_id": 2, "entity": "cfpl", "is_active": false, "allowed_warehouses": ["W202"] }
// Editable: full_name, email, role_id, entity, is_active, allowed_warehouses

// POST /users/{id}/reset-password  (admin only)
{ "new_password": "TempStrongPass1234" }
// → response: { user_id, message, revoked_count, temp_password_set: true }
// Forces must_change_password=TRUE on the target. Validates same rules as /password/change.

// PUT /roles/{id}/permissions  (replaces all perms for that role)
{
  "permission_ids": [1, 2, 5, 10, 24, 25],
  "allowed_entities": ["cfpl"],       // null = all
  "allowed_warehouses": ["W202"],     // null = all
  "allowed_floors": ["1st Floor"]     // null = all
}

// POST /modules  (auto-creates view/create/edit/delete for each sub_module)
{ "module": "quality", "sub_modules": ["inspections", "calibration"] }
// → 8 permissions created
```

### 2.5 Auth error codes you must handle

| HTTP | `error`                  | `details` | Frontend behaviour                                                                                  |
|------|--------------------------|-----------|------------------------------------------------------------------------------------------------------|
| 400  | `weak_password`          | `rules: string[]` (rule keys, see §6.1) | On Force-Change-Password / Admin reset: render rule-key → human message and inline-highlight  |
| 400  | `password_mismatch`      | —         | "New password and confirmation don't match" — highlight confirm field                               |
| 401  | `invalid_credentials`    | —         | "Invalid phone or password" — generic; do NOT distinguish unknown-phone vs wrong-password           |
| 401  | `invalid_access_token`   | —         | Trigger refresh-and-retry; on success replay the original request once. Failure → re-login          |
| 401  | `invalid_refresh_token`  | —         | Clear tokens, route to login                                                                        |
| 401  | `token_expired`          | —         | If from `/refresh` → clear + login. If from a normal call → impossible (interceptor handles)        |
| 401  | `token_reuse_detected`   | —         | Clear tokens, route to login, show: *"Security alert: please sign in again."*                       |
| 401  | `invalid_old_password`   | —         | Highlight Old Password field on the Change Password screen                                          |
| 401  | `unauthorized`           | —         | Generic; treat like `invalid_access_token`                                                          |
| 403  | `account_suspended`      | —         | "Your account is suspended. Contact your admin."                                                    |
| 403  | `account_disabled`       | —         | "Your account is disabled."                                                                         |
| 403  | `forbidden`              | `module`, `sub_module`, `action` | Redirect to `/forbidden` screen; show which permission was missing               |
| 404  | `session_not_found`      | —         | (Sessions screen) "Session no longer exists — refreshing list"                                      |
| 404  | `user_not_found`         | —         | (Admin reset-password) "User not found"                                                             |
| 409  | varies                   | —         | Show `message` (e.g. "Phone number already registered")                                             |
| 422  | `validation_error`       | `errors`  | FastAPI body validation; show first error inline                                                    |
| 423  | `account_locked`         | `locked_until`, `failed_login_count` | Disable submit; show countdown to `locked_until`                          |
| 429  | `rate_limit_exceeded`    | `retry_after_seconds`, `limit`, `window_seconds` | Disable submit; show retry-after timer; also honour `Retry-After` header |
| 500  | `internal_error`         | —         | Show generic + the request_id from the envelope                                                     |

### 2.6 Phone normalization (server-side, for reference)

The backend accepts and normalizes:

| Input                | Normalized E.164    |
|----------------------|---------------------|
| `9876543210`         | `+919876543210`     |
| `09876543210`        | `+919876543210`     |
| `919876543210`       | `+919876543210`     |
| `+919876543210`      | `+919876543210`     |

Show the user's input verbatim in fields. Display normalized phone only in `/me` / `/sessions` views since that is what the server returns.

---

## 3. Token storage — concrete recipes

### 3.1 Electron (TypeScript)

**Main process (`main/auth-store.ts`):**
```ts
import Store from 'electron-store';
import { safeStorage } from 'electron';

type StoredTokens = {
  // both fields are base64 ciphertext produced by safeStorage
  accessTokenEnc: string | null;
  refreshTokenEnc: string | null;
  expiresAt: number | null;        // ms epoch — when access expires
  refreshExpiresAt: number | null; // ms epoch — when refresh expires
};

const store = new Store<{ tokens: StoredTokens }>({
  name: 'candor-auth',
  encryptionKey: undefined,        // Electron handles via safeStorage
  defaults: { tokens: { accessTokenEnc: null, refreshTokenEnc: null, expiresAt: null, refreshExpiresAt: null } },
});

export const tokenStore = {
  async save(access: string, refresh: string, expiresIn: number, refreshExpiresIn: number) {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('safeStorage not available — refusing to persist tokens in plaintext');
    }
    store.set('tokens', {
      accessTokenEnc: safeStorage.encryptString(access).toString('base64'),
      refreshTokenEnc: safeStorage.encryptString(refresh).toString('base64'),
      expiresAt: Date.now() + expiresIn * 1000,
      refreshExpiresAt: Date.now() + refreshExpiresIn * 1000,
    });
  },
  async loadAccess(): Promise<string | null> {
    const t = store.get('tokens');
    if (!t.accessTokenEnc) return null;
    return safeStorage.decryptString(Buffer.from(t.accessTokenEnc, 'base64'));
  },
  async loadRefresh(): Promise<string | null> {
    const t = store.get('tokens');
    if (!t.refreshTokenEnc) return null;
    return safeStorage.decryptString(Buffer.from(t.refreshTokenEnc, 'base64'));
  },
  async clear() {
    store.set('tokens', { accessTokenEnc: null, refreshTokenEnc: null, expiresAt: null, refreshExpiresAt: null });
  },
};
```

**IPC bridge (`main/auth-ipc.ts`):** expose `auth:request`, `auth:login`, `auth:logout`, `auth:me`, `auth:can`. The renderer never holds tokens.

**Renderer (`preload.ts`):**
```ts
import { contextBridge, ipcRenderer } from 'electron';
contextBridge.exposeInMainWorld('candor', {
  request: (cfg: { method: string; path: string; body?: unknown; query?: Record<string,string> }) =>
    ipcRenderer.invoke('auth:request', cfg),
  login: (phone: string, password: string, deviceInfo?: object) =>
    ipcRenderer.invoke('auth:login', { phone, password, deviceInfo }),
  logout: () => ipcRenderer.invoke('auth:logout'),
  logoutAll: () => ipcRenderer.invoke('auth:logoutAll'),
  me: () => ipcRenderer.invoke('auth:me'),
  changePassword: (old: string, neu: string, confirm: string) =>
    ipcRenderer.invoke('auth:changePassword', { old, neu, confirm }),
  listSessions: () => ipcRenderer.invoke('auth:listSessions'),
  revokeSession: (tokenId: string) => ipcRenderer.invoke('auth:revokeSession', tokenId),
});
```

> If you choose NOT to use IPC isolation (smaller team / faster ship), then store tokens in the renderer using `safeStorage` round-tripped through a single IPC `crypto:encrypt` / `crypto:decrypt` channel — never write the plaintext tokens to disk OR to React state that survives a reload.

### 3.2 Android (Java)

**Gradle add (already present except `androidx.security:security-crypto`):**
```gradle
// app/build.gradle
implementation 'androidx.security:security-crypto:1.1.0-alpha06'
```

**`AuthStore.java`:**
```java
package com.candor.ims.auth;

import android.content.Context;
import android.content.SharedPreferences;
import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;

public final class AuthStore {
    private static final String FILE = "candor_auth";
    private static final String K_ACCESS = "access_token";
    private static final String K_REFRESH = "refresh_token";
    private static final String K_ACCESS_EXP = "access_expires_at";
    private static final String K_REFRESH_EXP = "refresh_expires_at";

    private final SharedPreferences prefs;

    public AuthStore(Context ctx) throws Exception {
        MasterKey key = new MasterKey.Builder(ctx)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build();
        this.prefs = EncryptedSharedPreferences.create(
            ctx, FILE, key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        );
    }

    public synchronized void save(String access, String refresh, long expiresIn, long refreshExpiresIn) {
        long now = System.currentTimeMillis();
        prefs.edit()
            .putString(K_ACCESS, access)
            .putString(K_REFRESH, refresh)
            .putLong(K_ACCESS_EXP, now + expiresIn * 1000L)
            .putLong(K_REFRESH_EXP, now + refreshExpiresIn * 1000L)
            .apply();
    }

    public synchronized String getAccess()  { return prefs.getString(K_ACCESS, null); }
    public synchronized String getRefresh() { return prefs.getString(K_REFRESH, null); }
    public synchronized boolean isLoggedIn() { return getRefresh() != null; }
    public synchronized void clear() { prefs.edit().clear().apply(); }
}
```

Make `AuthStore` a singleton (one instance per process). The `synchronized` blocks protect the rotate-on-refresh write/read race.

---

## 4. HTTP client + transparent refresh interceptor

Both implementations must satisfy:
- Single in-flight refresh: if N requests get 401 simultaneously, exactly **one** `/refresh` runs and the others wait on its result.
- The original request retries **once** after a successful refresh; if it 401s again, give up and force re-login.
- Skip the refresh dance for the auth endpoints themselves (`/login`, `/refresh`, `/logout`).
- A 401 on `/refresh` → clear tokens, emit a "force re-login" event the UI subscribes to.

### 4.1 Electron / TS — axios interceptor

```ts
// main/api-client.ts
import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';
import { tokenStore } from './auth-store';

const BASE_URL = process.env.CANDOR_API_BASE_URL ?? 'https://desktop-backend-vhf0.onrender.com';

const SKIP_AUTH = new Set(['/api/v1/auth/login', '/api/v1/auth/refresh']);
let refreshInFlight: Promise<string | null> | null = null;

export function createApiClient(onForceLogout: (reason: string) => void): AxiosInstance {
  const client = axios.create({ baseURL: BASE_URL, timeout: 30_000 });

  client.interceptors.request.use(async (cfg) => {
    if (SKIP_AUTH.has(cfg.url ?? '')) return cfg;
    const tok = await tokenStore.loadAccess();
    if (tok) cfg.headers.Authorization = `Bearer ${tok}`;
    return cfg;
  });

  client.interceptors.response.use(
    (r) => r,
    async (err: AxiosError<any>) => {
      const original = err.config as (AxiosRequestConfig & { _retried?: boolean }) | undefined;
      if (!original || err.response?.status !== 401 || original._retried) throw err;
      if (SKIP_AUTH.has(original.url ?? '')) throw err;

      // Single-flight refresh
      const newAccess = await (refreshInFlight ??= refreshOnce(client, onForceLogout));
      refreshInFlight = null;
      if (!newAccess) throw err; // forced logout already fired

      original._retried = true;
      original.headers = { ...(original.headers ?? {}), Authorization: `Bearer ${newAccess}` };
      return client.request(original);
    },
  );

  return client;
}

async function refreshOnce(client: AxiosInstance, onForceLogout: (r: string) => void): Promise<string | null> {
  const refresh = await tokenStore.loadRefresh();
  if (!refresh) { onForceLogout('no_refresh'); return null; }
  try {
    const { data } = await client.post('/api/v1/auth/refresh', { refresh_token: refresh });
    await tokenStore.save(data.access_token, data.refresh_token, data.expires_in, data.refresh_expires_in);
    return data.access_token as string;
  } catch (e: any) {
    await tokenStore.clear();
    const code = e?.response?.data?.error ?? 'refresh_failed';
    onForceLogout(code);
    return null;
  }
}
```

### 4.2 Android Java — OkHttp `Authenticator` + `Interceptor`

```java
// AuthInterceptor.java — attaches access token to every request except auth endpoints
package com.candor.ims.auth;

import okhttp3.*;
import org.jetbrains.annotations.NotNull;
import java.io.IOException;
import java.util.Set;

public final class AuthInterceptor implements Interceptor {
    private static final Set<String> SKIP = Set.of("/api/v1/auth/login", "/api/v1/auth/refresh");
    private final AuthStore store;

    public AuthInterceptor(AuthStore store) { this.store = store; }

    @NotNull @Override
    public Response intercept(@NotNull Chain chain) throws IOException {
        Request req = chain.request();
        if (SKIP.contains(req.url().encodedPath())) return chain.proceed(req);
        String access = store.getAccess();
        if (access == null) return chain.proceed(req);
        return chain.proceed(req.newBuilder()
            .header("Authorization", "Bearer " + access)
            .build());
    }
}
```

```java
// TokenAuthenticator.java — runs on 401, attempts refresh, retries once
package com.candor.ims.auth;

import okhttp3.*;
import org.jetbrains.annotations.NotNull;
import java.io.IOException;

public final class TokenAuthenticator implements Authenticator {
    private final AuthStore store;
    private final AuthApi authApi;            // Retrofit interface, see §4.3
    private final ForceLogoutBus bus;         // emits "please re-login"
    private final Object lock = new Object(); // single-flight refresh

    public TokenAuthenticator(AuthStore store, AuthApi authApi, ForceLogoutBus bus) {
        this.store = store; this.authApi = authApi; this.bus = bus;
    }

    @Override
    public Request authenticate(Route route, @NotNull Response response) throws IOException {
        // Don't loop: if we already retried, give up.
        if (response.priorResponse() != null) return null;
        // Skip auth endpoints (their 401 means bad creds, not stale token)
        String path = response.request().url().encodedPath();
        if (path.equals("/api/v1/auth/login") || path.equals("/api/v1/auth/refresh")) return null;

        synchronized (lock) {
            // Another thread may have just refreshed — try the latest access first.
            String latest = store.getAccess();
            String stale = response.request().header("Authorization");
            String latestHdr = latest != null ? "Bearer " + latest : null;
            if (latest != null && !latestHdr.equals(stale)) {
                return response.request().newBuilder()
                    .header("Authorization", latestHdr).build();
            }
            // Otherwise, do the refresh ourselves.
            String refresh = store.getRefresh();
            if (refresh == null) { bus.forceLogout("no_refresh"); return null; }
            try {
                retrofit2.Response<RefreshResponse> r = authApi.refresh(new RefreshRequest(refresh)).execute();
                if (!r.isSuccessful() || r.body() == null) {
                    store.clear(); bus.forceLogout("refresh_failed"); return null;
                }
                RefreshResponse rb = r.body();
                store.save(rb.access_token, rb.refresh_token, rb.expires_in, rb.refresh_expires_in);
                return response.request().newBuilder()
                    .header("Authorization", "Bearer " + rb.access_token).build();
            } catch (Exception e) {
                store.clear(); bus.forceLogout("refresh_exception"); return null;
            }
        }
    }
}
```

OkHttp's `Authenticator` only retries the failed request once (it sets `priorResponse`); reuse that mechanic instead of rolling your own retry counter.

### 4.3 Retrofit interfaces (Android)

```java
public interface AuthApi {
    @POST("api/v1/auth/login")          Call<LoginResponse> login(@Body LoginRequest body);
    @POST("api/v1/auth/refresh")        Call<RefreshResponse> refresh(@Body RefreshRequest body);
    @POST("api/v1/auth/logout")         Call<Void> logout(@Body LogoutRequest body);
    @POST("api/v1/auth/logout-all")     Call<LogoutAllResponse> logoutAll();
    @GET("api/v1/auth/me")              Call<MeResponse> me();
    @POST("api/v1/auth/password/change") Call<ChangePasswordResponse> changePassword(@Body ChangePasswordRequest body);
    @GET("api/v1/auth/sessions")        Call<SessionsResponse> sessions();
    @DELETE("api/v1/auth/sessions/{id}") Call<Void> revokeSession(@Path("id") String tokenId);

    // Admin
    @POST("api/v1/auth/users")          Call<UserOut> createUser(@Body CreateUserRequest body);
    @GET("api/v1/auth/users")           Call<List<AdminUser>> listUsers();
    @PUT("api/v1/auth/users/{id}")      Call<UpdateAck> editUser(@Path("id") long userId, @Body Map<String,Object> body);
    @DELETE("api/v1/auth/users/{id}")   Call<UpdateAck> deactivateUser(@Path("id") long userId);
    @POST("api/v1/auth/users/{id}/reset-password")
                                        Call<AdminResetResponse> resetPassword(@Path("id") long userId, @Body ResetPasswordRequest body);
    @GET("api/v1/auth/roles")           Call<List<RoleSummary>> listRoles();
    @POST("api/v1/auth/roles")          Call<RoleSummary> createRole(@Body CreateRoleRequest body);
    @GET("api/v1/auth/roles/{id}/permissions") Call<RolePermissions> getRolePermissions(@Path("id") long roleId);
    @PUT("api/v1/auth/roles/{id}/permissions") Call<UpdateAck> setRolePermissions(@Path("id") long roleId, @Body SetRolePermissionsRequest body);
    @GET("api/v1/auth/permissions")     Call<List<Permission>> listPermissions(@Query("module") String moduleFilter);
    @GET("api/v1/auth/permissions/hierarchy") Call<JsonObject> permissionsHierarchy();
    @POST("api/v1/auth/permissions/create") Call<Permission> createPermission(@Body CreatePermissionRequest body);
    @PUT("api/v1/auth/permissions/{id}") Call<UpdateAck> editPermission(@Path("id") long permId, @Body Map<String,Object> body);
    @DELETE("api/v1/auth/permissions/{id}") Call<UpdateAck> deletePermission(@Path("id") long permId);
    @GET("api/v1/auth/modules")         Call<List<ModuleSummary>> listModules();
    @POST("api/v1/auth/modules")        Call<ModuleCreatedAck> createModule(@Body CreateModuleRequest body);
}
```

Use the Gson converter; all responses are camelCase enough that defaults work, but `must_change_password`, `expires_in`, `refresh_expires_in`, `access_token`, `refresh_token`, `token_id`, `user_id`, `role_id`, `role_name`, `is_admin`, `is_current`, `device_info`, `is_active`, `password_changed_at`, `last_login_at`, `revoked_count`, `temp_password_set` are all `snake_case`. **Either** match the field names exactly **or** annotate with `@SerializedName("snake_case_name")` on each Java field. **Do not** turn on Gson's `LOWER_CASE_WITH_UNDERSCORES` policy globally — it breaks request payloads other modules send.

### 4.4 Error envelope parser (both platforms)

```ts
// TS
export interface ErrorEnvelope {
  error: string;
  message: string;
  request_id: string;
  timestamp: string;
  details?: Record<string, unknown>;
}
export function parseError(err: unknown): ErrorEnvelope {
  const fallback: ErrorEnvelope = { error: 'unknown', message: 'Unexpected error', request_id: '', timestamp: '' };
  const data = (err as any)?.response?.data;
  if (data && typeof data === 'object' && 'error' in data) return data as ErrorEnvelope;
  return fallback;
}
```

```java
// Java
public final class ErrorEnvelope {
    public String error;
    public String message;
    public String request_id;
    public String timestamp;
    public Map<String, Object> details;

    public static ErrorEnvelope from(retrofit2.Response<?> resp, Gson gson) {
        try {
            if (resp.errorBody() == null) return fallback();
            return gson.fromJson(resp.errorBody().string(), ErrorEnvelope.class);
        } catch (Exception e) { return fallback(); }
    }
    private static ErrorEnvelope fallback() {
        ErrorEnvelope e = new ErrorEnvelope();
        e.error = "unknown"; e.message = "Unexpected error";
        e.request_id = ""; e.timestamp = "";
        return e;
    }
}
```

---

## 5. Auth state store + `can()` helper

### 5.1 TypeScript (Zustand)

```ts
// stores/auth-store.ts
import { create } from 'zustand';

export type Role = { role_id: string; code: string; label: string; is_admin: boolean };
export type Permission = { module: string; sub_module: string | null; action: string };
export type Me = {
  user_id: string;
  phone: string;
  full_name: string | null;
  email: string | null;
  status: 'active' | 'suspended' | 'disabled';
  must_change_password: boolean;
  is_admin: boolean;
  roles: Role[];
  permissions: Permission[];
  entities: string[];
  warehouses: string[];
  floors: string[];
};

type State = {
  me: Me | null;
  isLoading: boolean;
  setMe: (me: Me | null) => void;
  can: (module: string, subModule: string | null, action: string) => boolean;
  reset: () => void;
};

export const useAuth = create<State>((set, get) => ({
  me: null,
  isLoading: false,
  setMe: (me) => set({ me }),
  can: (module, subModule, action) => {
    const me = get().me;
    if (!me) return false;
    if (me.is_admin) return true;
    return me.permissions.some(p =>
      p.module === module && p.action === action && (
        // exact (module, sub_module, action) ...
        (subModule !== null && p.sub_module === subModule) ||
        // ... or broader (module, null, action)
        (p.sub_module === null)
      )
    );
  },
  reset: () => set({ me: null, isLoading: false }),
}));
```

The `/me` payload doesn't carry `sub_sub_module`, so the frontend `can()` operates at module/sub_module/action granularity. For more specific gating (e.g. `production.plans.approve.create`), gate on the closest `(module, sub_module, action)` you have and let the server reject precise sub_sub_module misses with 403 forbidden — show the user "Permission denied" and surface `details.module / sub_module / action` from the envelope.

### 5.2 Android (Java)

```java
public final class AuthManager {
    private static volatile AuthManager INSTANCE;
    public static AuthManager get() { return INSTANCE; }
    public static void init(AuthManager m) { INSTANCE = m; }

    private volatile MeResponse me;
    public MeResponse getMe() { return me; }
    public void setMe(MeResponse m) { this.me = m; }
    public void clear() { this.me = null; }

    public boolean isAdmin() {
        return me != null && me.is_admin;
    }

    public boolean can(String module, String subModule, String action) {
        MeResponse m = me;
        if (m == null) return false;
        if (m.is_admin) return true;
        for (Permission p : m.permissions) {
            if (!p.module.equals(module) || !p.action.equals(action)) continue;
            // exact (module, sub_module, action) match
            if (subModule != null && subModule.equals(p.sub_module)) return true;
            // broader (module, null, action)
            if (p.sub_module == null) return true;
        }
        return false;
    }
}
```

---

## 6. Password rules — match server policy

### 6.1 Rules (server enforces; replicate for instant feedback)

| Rule key                          | Constraint                                                                  |
|-----------------------------------|-----------------------------------------------------------------------------|
| `length_12_128`                   | 12 ≤ length ≤ 128                                                           |
| `alpha_and_digit`                 | Must contain at least one letter AND one digit                              |
| `not_equals_or_contains_phone`    | Must not equal nor contain the user's phone number (any 7+ digit suffix)    |
| `not_in_common_blocklist`         | Must not match the server's `common_passwords.txt` (case-insensitive)       |

Display map:
```ts
const RULE_LABELS: Record<string, string> = {
  length_12_128: '12–128 characters',
  alpha_and_digit: 'At least one letter and one digit',
  not_equals_or_contains_phone: "Must not contain your phone number",
  not_in_common_blocklist: 'Not a commonly-used password',
};
```

The client should pre-validate (1), (2), (3) for a snappy UX. Only the server can authoritatively check (4) — but go ahead and submit; on 400 `weak_password` the response gives `details.rules: string[]`, render them inline.

### 6.2 Confirmation field

Always require `confirm_password === new_password` client-side before sending. The server returns 400 `password_mismatch` if you don't, but that's a wasted round-trip.

---

## 7. Screens

### 7.1 User-facing (always available when logged in)

| #  | Screen                       | Route (TS) / Activity (Android)                            | Purpose                                                                                  |
|----|------------------------------|------------------------------------------------------------|------------------------------------------------------------------------------------------|
| S1 | **Login**                    | `/login` / `LoginActivity`                                 | Phone + password fields, submit, surface lockout/rate-limit timer, "remember device" toggle |
| S2 | **Force Change Password**    | `/auth/force-change` / `ForceChangePasswordActivity`       | Mounted automatically when `must_change_password === true`. Blocks all other navigation. |
| S3 | **Profile (Me)**             | `/profile` / `ProfileActivity`                             | Shows /me payload, "Change password" button, "Sign out", "Sign out everywhere"           |
| S4 | **Sessions**                 | `/profile/sessions` / `SessionsActivity`                   | Lists `/sessions`; "Revoke" action per row; "is_current" highlighted                    |
| S5 | **Forbidden / Unauthorized** | `/forbidden` / `ForbiddenActivity`                         | Generic "Permission denied" — server's `details.module/sub_module/action` rendered      |

**S1 Login — must-have UX:**
- Phone field: `inputType` numeric (Android `phone`) or HTML `inputmode="tel"` (TS); accept any of the 4 phone formats
- Password field: `type=password`, optional show/hide
- Submit button disabled while in flight
- 423 `account_locked` → disable submit, show "Locked until HH:MM" countdown, re-enable when expires
- 429 `rate_limit_exceeded` → disable submit, show countdown from `details.retry_after_seconds` AND honour `Retry-After` response header
- Error text below the form, never on a per-field level for `invalid_credentials` (avoid phone enumeration)
- **Don't** prefill phone from local storage by default; offer optional "Remember device" that prefills only the phone, never the password
- After success: if `must_change_password` → navigate to S2; else navigate to the last route OR home

**S2 Force Change Password:**
- Three fields: old password, new password, confirm new password
- Client-side rule checklist (length, alpha+digit, not phone) updates on each keystroke
- Submit button enabled only when all three client-side rules pass + confirm matches
- On 400 `weak_password`: render `details.rules` inline
- On 401 `invalid_old_password`: highlight old field
- After success: clear `must_change_password` in auth store, route to home, show toast "Password changed. N other devices signed out."
- `back` is intercepted/disabled — user cannot escape this screen without changing or signing out

**S3 Profile:**
- Render `full_name`, `phone`, `email`, `status`, `last_login_at`, `password_changed_at`
- Roles list: chip per role with `label`
- Scope summary: `entities`, `warehouses`, `floors` (collapsed by default)
- Buttons: **Change password**, **Sign out**, **Sign out from all devices** (`/logout-all`)

**S4 Sessions:**
- One card per row: `device_info.device_name` (fallback `user_agent`), platform, IP, issued_at relative time, expires_at relative time
- Current session has a "This device" badge (`is_current === true`)
- Each row has a "Revoke" button → `DELETE /sessions/{token_id}`
- Revoking the current session signs the user out

**S5 Forbidden:**
- "You don't have permission to do this."
- Show: requested action `<module>/<sub_module>/<action>` (from `details`)
- Show: request_id (for support)
- "Go back" / "Go home" buttons

### 7.2 Admin screens (only when `isAdmin === true`)

| # | Screen                       | Route / Activity                                       | Wraps                                                         |
|---|------------------------------|--------------------------------------------------------|---------------------------------------------------------------|
| A1 | **User list + create**      | `/admin/users` / `AdminUsersActivity`                  | `GET /users`, `POST /users`                                  |
| A2 | **User detail / edit**      | `/admin/users/:id` / `AdminUserDetailActivity`         | `PUT /users/{id}`, `DELETE /users/{id}`, `POST /users/{id}/reset-password` |
| A3 | **Roles + permissions matrix** | `/admin/roles` / `AdminRolesActivity`              | `GET /roles`, `POST /roles`, `GET /roles/{id}/permissions`, `PUT /roles/{id}/permissions` |
| A4 | **Permission catalog**      | `/admin/permissions` / `AdminPermissionsActivity`      | `GET /permissions/hierarchy`, `POST /permissions/create`, `PUT /permissions/{id}`, `DELETE /permissions/{id}` |
| A5 | **Modules**                 | `/admin/modules` / `AdminModulesActivity`              | `GET /modules`, `POST /modules`                              |
| A6 | **Audit (read-only)**       | `/admin/audit` / `AdminAuditActivity`                  | (Out of scope for this prompt — placeholder route + "Coming soon") |

**A1 Users list — UX requirements:**
- Table: phone, full_name, role_name, entity, allowed_warehouses, is_active, status, last_login_at
- "Create user" opens modal with: phone, full_name, role (dropdown from `/roles`), email (opt), entity (cfpl/cdpl/none), allowed_warehouses (multi-select)
- Generate-temp-password helper: produce a 16-char password meeting all 4 rules; suggest in modal for admin convenience
- 409 on duplicate phone → show inline error on phone field

**A2 User detail:**
- All editable fields from §2.4 (`full_name`, `email`, `role_id`, `entity`, `is_active`, `allowed_warehouses`)
- "Reset password" button opens a modal that takes a new password (validated client-side against §6.1) and calls `/users/{id}/reset-password`
- "Deactivate" button → confirmation modal → `DELETE /users/{id}`. Success toast: "User deactivated. All their sessions revoked."

**A3 Roles + permissions matrix:**
- Left rail: role list with permission count
- Main area: permissions tree (from `/permissions/hierarchy`) with checkboxes
- "Save" → `PUT /roles/{id}/permissions` with the full set of selected permission_ids + scope (allowed_entities/warehouses/floors)
- Show a tri-state for parent nodes: all/some/none of children selected
- Scope inputs: comma-separated tags per role-permission row, with empty=all

**A4 Permission catalog:**
- Grouped tree view of `(module, sub_module, sub_sub_module, action)` (use `/permissions/hierarchy`)
- Inline edit description, inline delete (with confirm), "Add permission" modal
- Caution banner: deleting a permission also removes it from every role mapping

**A5 Modules:**
- List of modules + sub_modules + permission_count
- "Create module" modal: module name + comma-separated sub_modules; auto-creates view/create/edit/delete for each (per §2.4)

---

## 8. Route / activity guards

### 8.1 React Router 6

```tsx
// guards/RequireAuth.tsx
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@/stores/auth-store';

export function RequireAuth({ children }: { children: JSX.Element }) {
  const me = useAuth(s => s.me);
  const loc = useLocation();
  if (!me) return <Navigate to="/login" replace state={{ from: loc }} />;
  if (me.must_change_password && loc.pathname !== '/auth/force-change') {
    return <Navigate to="/auth/force-change" replace />;
  }
  return children;
}

// guards/RequirePermission.tsx
export function RequirePermission(props: {
  module: string; subModule?: string | null; action: string; children: JSX.Element;
}) {
  const can = useAuth(s => s.can);
  if (!can(props.module, props.subModule ?? null, props.action)) {
    return <Navigate to="/forbidden" replace state={{ module: props.module, subModule: props.subModule, action: props.action }} />;
  }
  return props.children;
}

// guards/RequireAdmin.tsx
export function RequireAdmin({ children }: { children: JSX.Element }) {
  const isAdmin = useAuth(s => s.me?.is_admin === true);
  return isAdmin ? children : <Navigate to="/forbidden" replace />;
}
```

Compose at the route level:
```tsx
<Route path="/admin/users" element={
  <RequireAuth><RequireAdmin><AdminUsersScreen/></RequireAdmin></RequireAuth>
}/>
<Route path="/production/plans" element={
  <RequireAuth>
    <RequirePermission module="production" subModule="plans" action="view">
      <PlansScreen/>
    </RequirePermission>
  </RequireAuth>
}/>
```

### 8.2 Android

```java
// BaseAuthActivity.java
public abstract class BaseAuthActivity extends AppCompatActivity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        MeResponse me = AuthManager.get().getMe();
        if (me == null) { redirectToLogin(); return; }
        if (me.must_change_password && !(this instanceof ForceChangePasswordActivity)) {
            startActivity(new Intent(this, ForceChangePasswordActivity.class));
            finish();
            return;
        }
        String[] perm = requiredPermission(); // override in subclass: {module, sub_module, action}
        if (perm != null && !AuthManager.get().can(perm[0], perm[1], perm[2])) {
            Intent i = new Intent(this, ForbiddenActivity.class);
            i.putExtra("module", perm[0]); i.putExtra("sub_module", perm[1]); i.putExtra("action", perm[2]);
            startActivity(i); finish(); return;
        }
        if (requireAdmin() && !AuthManager.get().isAdmin()) {
            startActivity(new Intent(this, ForbiddenActivity.class)); finish(); return;
        }
    }
    protected String[] requiredPermission() { return null; }
    protected boolean requireAdmin() { return false; }

    private void redirectToLogin() {
        Intent i = new Intent(this, LoginActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(i); finish();
    }
}

// Usage
public class PlansActivity extends BaseAuthActivity {
    @Override protected String[] requiredPermission() {
        return new String[]{"production", "plans", "view"};
    }
}
public class AdminUsersActivity extends BaseAuthActivity {
    @Override protected boolean requireAdmin() { return true; }
}
```

Also subscribe `BaseAuthActivity` to `ForceLogoutBus` (a `LiveData<String>` or simple listener) so an interceptor-driven logout immediately bounces every active activity to `LoginActivity`.

---

## 9. App lifecycle

### 9.1 Cold start

1. Read `refresh_token` from secure storage. If missing → go to login.
2. If present and `refresh_expires_at < now()` → clear and go to login.
3. Else: call `/me` with the access token if it's still valid; on 401 the interceptor refreshes and retries automatically.
4. On `/me` 200 → hydrate auth store, route to home.
5. On `/me` 401 (after refresh attempt) → clear and go to login.

### 9.2 Resume (foreground)

- If access token expires within 60 s → trigger a `/refresh` proactively.
- Else: proceed; rely on the 401 interceptor for natural refresh.

### 9.3 Background / lock-screen

- Don't show the access token or any user PII in app-switcher screenshots.
  - Android: `WindowManager.LayoutParams.FLAG_SECURE` on sensitive screens (Login, ForceChange, Sessions, Admin).
  - Electron: not applicable, but if you implement a lock-screen overlay, render it before any token usage.

### 9.4 Logout flow

1. POST `/logout` with the current refresh token (best-effort; ignore failures).
2. Clear secure storage.
3. Reset auth store.
4. Route to login.

### 9.5 Logout-all flow

1. POST `/logout-all`.
2. Show toast `"Signed out from N devices"` (use `revoked_count`).
3. Then run the normal logout flow (steps 2–4 above) — your current refresh is also gone.

---

## 10. Acceptance tests (must pass before declaring done)

Implement these as integration tests OR a manual checklist. **Do not** ship if any fail.

| # | Scenario                                                                                  | Expected                                                                                  |
|---|-------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| T01 | Login with seed admin (phone `9004464207`, password `Candor@123*` — see `auth_schema.sql`) | 200, both tokens stored encrypted, `/me` returns `is_admin: true`, navigation works       |
| T02 | Login with wrong password 5 times                                                         | After 5th attempt, server returns 423 `account_locked`. UI disables submit + shows timer  |
| T03 | Spam login 10 times within 60 s from same IP                                              | 429 `rate_limit_exceeded`. UI honours `Retry-After`                                       |
| T04 | Login → wait 16 min → make any protected request                                          | Interceptor silently refreshes, the original call succeeds. User sees no interruption     |
| T05 | Login → manually expire the refresh token in DB → make any request                        | Force-logout fires, user lands on login                                                   |
| T06 | Two parallel API calls during a refresh                                                   | Exactly ONE `/refresh` round-trip; both calls succeed after the single refresh            |
| T07 | Admin resets a user's password                                                            | Target user is force-logged-out next request, prompted with Force-Change on next login    |
| T08 | User changes own password                                                                 | Other devices force-logged-out; current device keeps working; toast shows revoked_count   |
| T09 | Non-admin tries to GET `/api/v1/auth/users`                                              | 403 forbidden envelope; `<RequireAdmin>` should prevent the call in normal navigation     |
| T10 | Non-admin team_leader tries to access `/admin/users` directly via deep link              | Route guard kicks them to `/forbidden` BEFORE network call                                |
| T11 | Backend returns 401 `token_reuse_detected` mid-session                                    | Tokens cleared; user lands on login with the security toast                              |
| T12 | Phone in any of the 4 formats logs in                                                     | All four (`9876543210`, `09876543210`, `+919876543210`, `919876543210`) succeed           |
| T13 | New password equal to phone is rejected client-side                                       | Submit blocked with rule `not_equals_or_contains_phone`                                   |
| T14 | New password = `password123` is server-rejected                                           | 400 `weak_password` with `details.rules: ["not_in_common_blocklist", ...]`                |
| T15 | Revoke own current session from Sessions screen                                           | Force-logout immediately                                                                  |
| T16 | Network offline                                                                           | Login shows clear "no connection"; protected screens show cached `/me` until next online  |
| T17 | Token storage tampered (manual edit on disk)                                              | Decrypt fails → treat as logged-out, redirect to login                                    |
| T18 | App backgrounded for >15 min → foregrounded                                                | Proactive refresh fires, no broken UI                                                     |
| T19 | Same phone logged into both desktop and Android simultaneously                            | Both work independently; revoking one in `/sessions` doesn't kick the other               |
| T20 | Permission `production.plans.approve.create` revoked while user is on the Plans screen    | Next call returns 403; UI redirects to `/forbidden` with the missing-permission details   |

---

## 11. Out of scope (do NOT build)

- Single sign-on (SAML/OIDC)
- MFA / TOTP / SMS OTP (no SMS provider is configured)
- Biometric unlock — defer to a follow-up, this prompt is JWT-only
- "Forgot password" self-service flow (admin reset is the only path)
- Email verification
- Account self-registration (admin creates users)
- Audit log UI (placeholder only — A6)
- Server-side WebSocket auth handshake (separate prompt)

---

## 12. Reference: roles & permissions seeded by the backend

(See `app/db/auth_schema.sql` for the source of truth. Roles below are the defaults you can rely on for role dropdowns; new roles can be added through Admin Roles.)

| Role               | is_admin | Summary                                                                          |
|--------------------|----------|----------------------------------------------------------------------------------|
| `admin`            | TRUE     | Full unrestricted access; bypasses all permission checks                         |
| `planner`          | FALSE    | Plans (full CRUD), fulfillment, MRP, indents, AI, orders + view-all              |
| `stores_manager`   | FALSE    | Inventory (full), day-end (full), offgrade + view-all                            |
| `team_leader`      | FALSE    | Job cards (lifecycle, output, annexures) + view-all                              |
| `qc_inspector`     | FALSE    | Job-card annexures + sign-offs + view-all                                        |
| `floor_manager`    | FALSE    | Inventory, day-end, discrepancy + job-card view                                  |
| `purchase_manager` | FALSE    | Purchase module (full), indents + alerts (view/create)                           |
| `viewer`           | FALSE    | Read-only across all modules                                                     |

Modules currently exposed: `production` (14 sub_modules), `purchase` (2 sub_modules), `so` (no sub_modules), `auth` (admin-only: `users`, `roles`).

---

## 13. Reference: backend source pointers

If the AI agent doing the implementation needs to verify behaviour:

| Concern                       | File                                                            |
|-------------------------------|-----------------------------------------------------------------|
| Endpoints + wire shapes       | `app/modules/auth/router.py`, `app/modules/auth/schemas.py`     |
| Login / refresh / rotation    | `app/modules/auth/services/auth_service.py`                     |
| JWT issuance + verification   | `app/modules/auth/services/jwt_service.py`                      |
| Permission engine             | `app/modules/auth/services/permission_service.py`               |
| Password rules                | `app/modules/auth/services/password_rules.py`                   |
| Phone normalization           | `app/modules/auth/services/phone.py`                            |
| Rate limiter                  | `app/modules/auth/services/rate_limiter.py`                     |
| RBAC middleware               | `app/modules/auth/middleware.py`                                |
| Error envelope + middleware   | `app/core/middleware/request_context.py`                        |
| Schema (DB)                   | `app/db/auth_schema.sql`                                        |
| Token TTL / lockout settings  | `app/config.py` (`Settings.ACCESS_TOKEN_TTL_SECONDS` etc.)      |

---

## 14. Deliverables checklist

For each platform, the implementation must produce:

**Electron + React/TS desktop app**
- [ ] `main/auth-store.ts` — encrypted token storage (`safeStorage` + `electron-store`)
- [ ] `main/api-client.ts` — axios instance with request + 401-refresh interceptors
- [ ] `main/auth-ipc.ts` — IPC handlers for all 8 user endpoints + admin endpoints
- [ ] `preload.ts` — `contextBridge` exposing the IPC surface as `window.candor.*`
- [ ] `stores/auth-store.ts` — Zustand store with `me`, `setMe`, `can`, `reset`
- [ ] `guards/RequireAuth.tsx`, `RequirePermission.tsx`, `RequireAdmin.tsx`
- [ ] Screens S1–S5 + A1–A5 (A6 placeholder)
- [ ] `lib/error.ts` — `parseError(err)` returning the envelope
- [ ] All acceptance tests T01–T20 documented as a manual QA checklist or Vitest suite

**Android (Java)**
- [ ] `auth/AuthStore.java` — `EncryptedSharedPreferences`-backed token store
- [ ] `auth/AuthInterceptor.java`, `auth/TokenAuthenticator.java`
- [ ] `auth/AuthApi.java` — Retrofit interface
- [ ] `auth/AuthManager.java` — singleton with `me`, `can`, `isAdmin`
- [ ] `auth/ForceLogoutBus.java` — `LiveData<String>` event bus
- [ ] `auth/BaseAuthActivity.java` — guard activity
- [ ] DTOs: `LoginRequest`, `LoginResponse`, `RefreshRequest`, `RefreshResponse`, `LogoutRequest`, `LogoutAllResponse`, `MeResponse`, `Permission`, `Role`, `ChangePasswordRequest`, `ChangePasswordResponse`, `SessionOut`, `SessionsResponse`, `AdminUser`, `RoleSummary`, `Permission`, `RolePermissions`, `CreateUserRequest`, `UpdateAck`, `AdminResetResponse`, `ResetPasswordRequest`, `SetRolePermissionsRequest`, `CreatePermissionRequest`, `CreateRoleRequest`, `ModuleSummary`, `ModuleCreatedAck`, `CreateModuleRequest`, `ErrorEnvelope`
- [ ] Activities for S1–S5 + A1–A5 (A6 placeholder)
- [ ] Layouts in `res/layout` using ViewBinding + Material Components
- [ ] All acceptance tests T01–T20 documented as a manual QA checklist or Espresso suite
- [ ] Add `androidx.security:security-crypto:1.1.0-alpha06` to `app/build.gradle`

---

**End of prompt.** Paste this whole file into the frontend AI session along with a one-line instruction such as: *"Implement everything described in `1_Authentication_and_Authorization.md`. Start with the Android Java client; then mirror the same flows in the Electron + React/TS desktop app. Run all acceptance tests T01–T20 against the local backend at `http://localhost:8000` before declaring done."*
