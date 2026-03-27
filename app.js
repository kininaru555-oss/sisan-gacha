(function () {
  "use strict";

  // ─────────────────────────────────────────────
  // 設定
  // ─────────────────────────────────────────────
  const API_BASE =
    window.APP_CONFIG?.API_BASE ||
    localStorage.getItem("API_BASE") ||
    "";

  const CSRF_STORAGE_KEY = "csrf_token";
  const USER_ID_STORAGE_KEY = "user_id";
  const CSRF_HEADER_NAME = "X-CSRF-Token";

  // ─────────────────────────────────────────────
  // 内部ユーティリティ
  // ─────────────────────────────────────────────
  function normalizeApiPath(path) {
    if (!path) return API_BASE;
    if (/^https?:\/\//i.test(path)) return path;
    if (!API_BASE) return path;
    if (path.startsWith("/")) return `${API_BASE}${path}`;
    return `${API_BASE}/${path}`;
  }

  function safeJsonParse(text, fallback = {}) {
    try {
      return JSON.parse(text);
    } catch (_) {
      return fallback;
    }
  }

  function getToastElement() {
    return document.getElementById("toast");
  }

  // ─────────────────────────────────────────────
  // Toast
  // ─────────────────────────────────────────────
  function showToast(message, type = "") {
    const toast = getToastElement();
    if (!toast) {
      console[type === "error" ? "error" : "log"](message);
      return;
    }

    toast.textContent = message;
    toast.className = "show" + (type ? " " + type : "");

    if (toast._timer) {
      clearTimeout(toast._timer);
    }

    toast._timer = setTimeout(() => {
      toast.className = "";
    }, 2800);
  }

  // ─────────────────────────────────────────────
  // CSRF / Session
  // ─────────────────────────────────────────────
  function getCsrfToken() {
    return localStorage.getItem(CSRF_STORAGE_KEY) || "";
  }

  function setCsrfToken(token) {
    if (!token) {
      localStorage.removeItem(CSRF_STORAGE_KEY);
      return;
    }
    localStorage.setItem(CSRF_STORAGE_KEY, token);
  }

  function getUserId() {
    return localStorage.getItem(USER_ID_STORAGE_KEY) || "";
  }

  function setUserId(userId) {
    if (!userId) {
      localStorage.removeItem(USER_ID_STORAGE_KEY);
      return;
    }
    localStorage.setItem(USER_ID_STORAGE_KEY, userId);
  }

  function clearClientSession() {
    localStorage.removeItem(USER_ID_STORAGE_KEY);
    localStorage.removeItem(CSRF_STORAGE_KEY);
  }

  async function fetchCsrf() {
    const res = await fetch(normalizeApiPath("/auth/csrf"), {
      method: "GET",
      credentials: "include",
    });

    const text = await res.text();
    const data = safeJsonParse(text, {});

    if (!res.ok) {
      throw new Error(data.detail || "CSRF取得失敗");
    }

    const csrfToken = data.csrf_token || "";
    setCsrfToken(csrfToken);
    return csrfToken;
  }

  function shouldAttachCsrf(method) {
    const m = (method || "GET").toUpperCase();
    return ["POST", "PUT", "PATCH", "DELETE"].includes(m);
  }

  // ─────────────────────────────────────────────
  // 共通 fetch
  // ─────────────────────────────────────────────
  async function fetchWithAuth(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    const hasBody = options.body !== undefined && options.body !== null;

    if (hasBody && !headers.has("Content-Type") && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }

    if (shouldAttachCsrf(method)) {
      let csrfToken = getCsrfToken();
      if (!csrfToken) {
        csrfToken = await fetchCsrf();
      }
      if (csrfToken) {
        headers.set(CSRF_HEADER_NAME, csrfToken);
      }
    }

    return fetch(normalizeApiPath(path), {
      ...options,
      method,
      headers,
      credentials: "include",
    });
  }

  async function requestJson(path, options = {}) {
    const res = await fetchWithAuth(path, options);
    const text = await res.text();
    const data = safeJsonParse(text, {});

    if (!res.ok) {
      const message =
        data.detail ||
        data.message ||
        `HTTP ${res.status}`;
      throw new Error(message);
    }

    return data;
  }

  async function request(path, options = {}) {
    const res = await fetchWithAuth(path, options);

    if (!res.ok) {
      let message = `HTTP ${res.status}`;
      try {
        const text = await res.text();
        const data = safeJsonParse(text, {});
        message = data.detail || data.message || message;
      } catch (_) {
        // ignore
      }
      throw new Error(message);
    }

    return res;
  }

  // ─────────────────────────────────────────────
  // 認証API
  // ─────────────────────────────────────────────
  async function login(userId, password) {
    const data = await requestJson("/auth/login", {
      method: "POST",
      body: JSON.stringify({
        user_id: userId,
        password: password,
      }),
    });

    setUserId(data.user_id || userId);
    await fetchCsrf();
    return data;
  }

  async function register(userId, password) {
    const data = await requestJson("/auth/register", {
      method: "POST",
      body: JSON.stringify({
        user_id: userId,
        password: password,
      }),
    });
    return data;
  }

  async function refreshSession() {
    const data = await requestJson("/auth/refresh", {
      method: "POST",
    });

    if (data.user_id) {
      setUserId(data.user_id);
    }
    await fetchCsrf();
    return data;
  }

  async function logout() {
    try {
      await requestJson("/auth/logout", {
        method: "POST",
      });
    } finally {
      clearClientSession();
    }
    return { status: "ok" };
  }

  async function getAuthStatus() {
    try {
      const csrfToken = await fetchCsrf();
      return {
        is_logged_in: Boolean(csrfToken),
        csrf_token: csrfToken,
        user_id: getUserId(),
      };
    } catch (_) {
      return {
        is_logged_in: false,
        csrf_token: "",
        user_id: "",
      };
    }
  }

  // ─────────────────────────────────────────────
  // ページ補助
  // ─────────────────────────────────────────────
  function requireLogin(redirectTo = "/login.html") {
    const userId = getUserId();
    if (!userId) {
      location.href = redirectTo;
      return false;
    }
    return true;
  }

  function go(path) {
    location.href = path;
  }

  // ─────────────────────────────────────────────
  // エラーハンドリング共通
  // ─────────────────────────────────────────────
  function handleError(error, fallbackMessage = "通信エラーが発生しました") {
    const message = error?.message || fallbackMessage;
    showToast(message, "error");
    return message;
  }

  // ─────────────────────────────────────────────
  // 公開API
  // ─────────────────────────────────────────────
  window.App = {
    API_BASE,

    // session
    getUserId,
    setUserId,
    getCsrfToken,
    setCsrfToken,
    clearClientSession,
    fetchCsrf,
    getAuthStatus,

    // fetch
    fetchWithAuth,
    request,
    requestJson,

    // auth
    login,
    register,
    refreshSession,
    logout,

    // ui
    showToast,
    handleError,
    requireLogin,
    go,
  };
})();
