"use client";

import { useEffect, useRef, useState } from "react";
import { api, getToken } from "./api";
import type { WsMessage } from "./types";

export type WsStatus = "connecting" | "open" | "closed" | "error";

export interface UseLiveOptions {
  /** Reconnect automatically on close (default true). */
  autoReconnect?: boolean;
  /** Reconnect delay in ms (default 2000). */
  reconnectDelayMs?: number;
  /** Optional message handler — fired for every parsed message. */
  onMessage?: (msg: WsMessage) => void;
}

/**
 * useLive — connects to /ws/live with the current bearer token and surfaces
 * the connection status + the most recent positions snapshot.
 *
 * Reconnects automatically with exponential-ish backoff (capped at 15s).
 * Stops trying after 8 consecutive failures so a permanently down bot
 * doesn't hammer the API.
 */
export function useLive(opts: UseLiveOptions = {}) {
  const {
    autoReconnect = true,
    reconnectDelayMs = 2000,
    onMessage,
  } = opts;
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const onMessageRef = useRef(onMessage);
  const stopRef = useRef(false);

  // Keep the latest onMessage in a ref so the connection effect doesn't
  // tear down + reconnect on every parent re-render.
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    stopRef.current = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (stopRef.current) return;
      const token = getToken();
      if (!token) {
        setStatus("closed");
        return;
      }
      // Build ws URL from api.baseUrl
      const base = api.baseUrl.replace(/^http/, "ws").replace(/\/+$/, "");
      const url = `${base}/ws/live?token=${encodeURIComponent(token)}`;

      setStatus("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        setStatus("error");
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        retriesRef.current = 0;
        setStatus("open");
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as WsMessage;
          setLastMessage(msg);
          onMessageRef.current?.(msg);
        } catch {
          // ignore non-JSON
        }
      };

      ws.onerror = () => {
        setStatus("error");
      };

      ws.onclose = (ev) => {
        setStatus("closed");
        wsRef.current = null;
        // 4401 = our auth-failure close code from server.py
        if (ev.code === 4401) {
          stopRef.current = true;
          return;
        }
        if (autoReconnect) scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (stopRef.current) return;
      if (retriesRef.current >= 8) {
        setStatus("error");
        return;
      }
      retriesRef.current += 1;
      const delay = Math.min(reconnectDelayMs * retriesRef.current, 15_000);
      timer = setTimeout(connect, delay);
    }

    connect();

    return () => {
      stopRef.current = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [autoReconnect, reconnectDelayMs]);

  function send(data: string) {
    wsRef.current?.send(data);
  }

  function disconnect() {
    stopRef.current = true;
    wsRef.current?.close();
  }

  return { status, lastMessage, send, disconnect };
}
