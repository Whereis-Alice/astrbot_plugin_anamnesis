/**
 * API Client - 封装 AstrBot Bridge API 通信
 * 提供统一的 API 请求、错误处理和重试机制
 */

export class ApiClient {
  constructor({ readyTimeoutMs = 8000 } = {}) {
    this._context = null;
    this._readyPromise = null;
    this._readyBridge = null;
    this._readyGeneration = 0;
    this.readyTimeoutMs = readyTimeoutMs;
  }

  get bridge() {
    // AstrBot injects its SDK at the end of the body, after plugin scripts.
    // Never cache an absent SDK while the document is still being parsed.
    return [window.AstrBotPluginPage, window.AstrBotPluginView].find(
      bridge => bridge && typeof bridge.ready === "function" &&
        typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function",
    );
  }

  /**
   * 等待 Bridge 就绪
   * @returns {Promise<Object>} Bridge 上下文
   */
  async ready() {
    if (this._readyPromise && (!this._readyBridge || this._readyBridge === this.bridge)) {
      return this._readyPromise;
    }
    this._readyBridge = null;
    const generation = ++this._readyGeneration;
    let timeout;
    let poll;
    this._readyPromise = new Promise((resolve, reject) => {
      timeout = setTimeout(() => reject(new Error(window.t ? window.t("bridge.timeout") : "AstrBot connection timed out")), this.readyTimeoutMs);
      const discover = () => {
        const bridge = this.bridge;
        if (!bridge) {
          poll = setTimeout(discover, 50);
          return;
        }
        if (generation === this._readyGeneration) this._readyBridge = bridge;
        Promise.resolve().then(() => bridge.ready()).then(resolve, reject);
      };
      discover();
    });
    const pending = this._readyPromise;
    try {
      const context = await pending;
      if (generation === this._readyGeneration) this._context = context;
      return context;
    } catch (e) {
      if (generation === this._readyGeneration) {
        this._readyPromise = null;
        this._readyBridge = null;
      }
      throw e;
    } finally {
      clearTimeout(timeout);
      clearTimeout(poll);
    }
  }

  /**
   * 获取当前上下文
   * @returns {Object} 上下文对象
   */
  getContext() {
    return this.bridge?.getContext?.() || this._context || {};
  }

  /**
   * 构建 endpoint 路径
   *
   * Bridge API 会自动添加插件前缀：
   * apiGet("stats") -> /api/plug/{plugin_name}/stats
   *
   * 后端注册路由格式：/{plugin_name}/page/stats
   * Bridge API 会处理插件前缀，前端只需要提供 "page/xxx" 部分
   *
   * @param {string} path - 原始路径（如 "stats", "memories"）
   * @returns {string} 带 page 前缀的路径（如 "page/stats"）
   */
  buildEndpoint(path) {
    const cleanPath = String(path).replace(/^\/+/, "");
    // 检查路径是否已经包含 page/ 前缀
    if (cleanPath.startsWith("page/")) {
      return cleanPath;
    }
    return "page/" + cleanPath.replace(/\/+/g, "/");
  }

  /**
   * 通用 API 请求（带重试）
   * @param {string} path - API 路径
   * @param {Object} options - 请求选项
   * @param {string} options.method - HTTP 方法（GET/POST）
   * @param {Object} options.body - 请求体（POST）
   * @param {number} options.retries - 重试次数
   * @returns {Promise<any>} API 响应
   */
  async request(path, options = {}) {
    const method = options.method || "GET";
    const body = options.body;
    // GET 幂等可安全重试；POST 可能非幂等（如 memories/update 重建新 ID），
    // 超时重试会造成重复创建，默认不重试，调用方显式传 retries 才重试
    const retries = options.retries ?? (method === "GET" ? 2 : 0);

    let bridge;
    do {
      bridge = this.bridge;
      await this.ready();
    } while (bridge !== this.bridge);
    if (!bridge) throw new Error(window.t ? window.t("bridge.error") : "Bridge not available");

    let lastError;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        if (method === "GET") {
          // 解析 query string
          const qi = path.indexOf("?");
          if (qi !== -1) {
            const base = path.substring(0, qi);
            const qs = path.substring(qi + 1);
            const params = {};
            new URLSearchParams(qs).forEach((v, k) => { params[k] = v; });
            return await bridge.apiGet(this.buildEndpoint(base), params);
          }
          return await bridge.apiGet(this.buildEndpoint(path), {});
        }
        // POST
        return await bridge.apiPost(this.buildEndpoint(path), body || {});
      } catch (e) {
        lastError = e;
        if (attempt === retries) throw e;
        // 指数退避
        await new Promise(resolve => {
          setTimeout(resolve, Math.min(1000 * Math.pow(2, attempt), 5000));
        });
      }
    }
    throw lastError || new Error(window.t ? window.t("misc.requestFailed") : "Request failed");
  }

  /**
   * 解包 API 响应
   *
   * 后端返回格式：
   * - 成功：{status: "ok", data: {...}}
   * - 失败：{status: "error", message: "..."}
   *
   * Dashboard bridge unwraps standard plugin responses before returning them.
   *
   * @param {Object} response - API 响应
   * @returns {any} 解包后的数据
   */
  unwrapResponse(response) {
    if (response && response.status === "ok" && Object.prototype.hasOwnProperty.call(response, "data")) {
      return response.data;
    }
    if (response && response.status === "error") {
      throw new Error(response.message || (window.t ? window.t("misc.requestFailed") : "Request failed"));
    }
    return response || {};
  }

  /**
   * GET 请求
   * @param {string} path - API 路径
   * @param {Object} params - Query 参数
   * @returns {Promise<any>} 响应数据
   */
  async get(path, params = {}) {
    const qs = new URLSearchParams(params).toString();
    const fullPath = qs ? `${path}?${qs}` : path;
    return this.unwrapResponse(await this.request(fullPath, { method: "GET" }));
  }

  /**
   * POST 请求
   * @param {string} path - API 路径
   * @param {Object} body - 请求体
   * @returns {Promise<any>} 响应数据
   */
  async post(path, body = {}, options = {}) {
    return this.unwrapResponse(await this.request(path, {
      method: "POST",
      body,
      retries: options.retries,
    }));
  }
}
