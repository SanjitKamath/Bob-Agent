// The app's connection to bobd: one WebSocket for everything it shows, and HTTP for
// requests. The token stays in the main process; windows never see it.
import { EventEmitter } from "node:events";
import WebSocket from "ws";
import type { ApiRequest, ApiResult, ConnectionState, ServerMessage } from "../shared/types";
import { readToken } from "./token";

const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 15000;
const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

export interface BackendOptions {
  serverUrl: () => string;
  bobHome: () => string;
}

export declare interface Backend {
  on(event: "message", listener: (message: ServerMessage) => void): this;
  on(event: "state", listener: (state: ConnectionState, detail: string) => void): this;
}

export class Backend extends EventEmitter {
  private socket: WebSocket | null = null;
  private token: string | null = null;
  private retryMs = RETRY_MIN_MS;
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;
  private keychainAsked = false;
  state: ConnectionState = "connecting";
  detail = "";

  constructor(private readonly options: BackendOptions) {
    super();
  }

  private setState(state: ConnectionState, detail = ""): void {
    if (state === this.state && detail === this.detail) return;
    this.state = state;
    this.detail = detail;
    this.emit("state", state, detail);
  }

  async start(): Promise<void> {
    this.stopped = false;
    await this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.socket?.removeAllListeners();
    this.socket?.close();
    this.socket = null;
  }

  /** Drop the connection and connect again, for example after the server URL changed. */
  async reconnect(): Promise<void> {
    this.stop();
    this.token = null;
    this.keychainAsked = false;
    this.retryMs = RETRY_MIN_MS;
    await this.start();
  }

  private scheduleRetry(): void {
    if (this.stopped || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.connect();
    }, this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS);
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    // The Keychain is asked once per connection attempt you start (launch, Refresh, a
    // settings change). Automatic retries do not ask again.
    this.token = this.token ?? (await readToken(this.options.bobHome(), !this.keychainAsked));
    this.keychainAsked = true;
    if (!this.token) {
      this.setState(
        "unauthorized",
        "Bob could not read its API token. Allow Keychain access when macOS asks, then press Refresh.",
      );
      this.scheduleRetry();
      return;
    }
    const url = `${this.options.serverUrl().replace(/^http/, "ws")}/v1/chat`;
    const socket = new WebSocket(url, { headers: { Authorization: `Bearer ${this.token}` }, handshakeTimeout: 5000 });
    this.socket = socket;
    socket.on("open", () => {
      this.retryMs = RETRY_MIN_MS;
      this.setState("connected");
    });
    socket.on("message", (data) => {
      try {
        this.emit("message", JSON.parse(data.toString()) as ServerMessage);
      } catch {
        // A frame that is not JSON is not from bobd's protocol; ignore it.
      }
    });
    socket.on("unexpected-response", (_request, response) => {
      // bobd refuses the handshake when the token is wrong.
      this.token = null;
      this.setState("unauthorized", `bobd refused the connection (${response.statusCode}).`);
      socket.terminate();
    });
    socket.on("error", () => {
      // "close" follows and schedules the retry.
    });
    socket.on("close", (code) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (code === 4401) {
        this.token = null;
        this.setState("unauthorized", "The API token was not accepted.");
      } else if (this.state !== "unauthorized") {
        this.setState("offline", "bobd is not running. Start it with: bob serve");
      }
      this.scheduleRetry();
    });
  }

  send(message: Record<string, unknown>): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  /** An HTTP request to bobd's user API. Windows can only reach paths under /v1/. */
  async request(request: ApiRequest, timeoutMs = 900000): Promise<ApiResult> {
    if (!ALLOWED_METHODS.has(request.method) || !/^\/v1\/[\w\-./?=&%:~+]*$/.test(request.path)) {
      return { ok: false, status: 400, error: "that request is not allowed" };
    }
    this.token = this.token ?? (await readToken(this.options.bobHome(), false));
    if (!this.token) return { ok: false, status: 401, error: "Bob could not read its API token." };
    try {
      const response = await fetch(`${this.options.serverUrl()}${request.path}`, {
        method: request.method,
        headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
        body: request.body === undefined ? undefined : JSON.stringify(request.body),
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await response.text();
      let data: unknown = text;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        // Not JSON: keep the text.
      }
      if (response.ok) return { ok: true, data };
      if (response.status === 401) this.token = null;
      const detail = (data as { detail?: unknown } | null)?.detail;
      const error = typeof detail === "string" ? detail : `bobd answered ${response.status}`;
      return { ok: false, status: response.status, error, data: detail };
    } catch (error) {
      const offline = error instanceof TypeError;
      return { ok: false, status: 0, error: offline ? "bobd is not running." : "The request timed out." };
    }
  }
}
