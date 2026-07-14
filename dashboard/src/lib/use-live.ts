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
 * useLive — connects to the bot's live update stream.
 *
 * Sprint 57: switched from WebSocket (`/ws/live`) to Server-Sent
 * Events (`/api/events`). The Traefik reverse proxy fronting
 * the bot on this VPS rejects every HTTP/1.1 upgrade request
 * with 403 Forbidden, which killed the WebSocket path
 * regardless of router config or middleware (Sprint 55.4
 * documented the issue; Sprint 55.4 was a polling
 * workaround). SSE is plain HTTP/1.1 chunked transfer — no
 * upgrade, no special headers — and works with any proxy.
 *
 * The wire format on `/api/events` is identical to the
 * WebSocket's: each event is a JSON object with `type: "hello"`,
 * `"audit"`, `"positions"`, or `"heartbeat"`. The shape of
 * `WsMessage` is unchanged so downstream code doesn't need
 * to know whether the messages came over WS or SSE.
 *
 * EventSource auto-reconnects on disconnect with a default
 * backoff (3s). We don't need a manual reconnect loop like
 * the old WebSocket path.
 */
export function useLive(opts: UseLiveOptions = {}) {
  const {
    autoReconnect = true,
    reconnectDelayMs = 2000,
    onMessage,
  } = opts;
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const onMessageRef = useRef(onMessage);
  const stopRef = useRef(false);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    stopRef.current = false;

    function connect() {
      if (stopRef.current) return;
      const token = getToken();
      if (!token) {
        setStatus("closed");
        return;
      }
      // SSE URL — same origin as the REST API. We use the
      // dedicated `/api/events` endpoint (added in Sprint 57)
      // instead of the broken `/ws/live` WebSocket. The token
      // is passed as a query param because EventSource does
      // not support custom request headers.
      const base = api.baseUrl.replace(/\/+$/, "");
      const url = `${base}/api/events?token=${encodeURIComponent(token)}`;

      setStatus("connecting");
      let es: EventSource;
      try {
        es = new EventSource(url);
      } catch {
        setStatus("error");
        return;
      }
      esRef.current = es;

      es.onopen = () => {
        setStatus("open");
      };

      // SSE: each `data: <json>` line fires `onmessage`. There's
      // no separate event-type channel in our implementation
      // (the bot emits a single stream with `type` inside the
      // JSON), so the default `onmessage` handler is enough.
      es.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as WsMessage;
          setLastMessage(msg);
          onMessageRef.current?.(msg);
        } catch {
          // ignore non-JSON
        }
      };

      es.onerror = () => {
        // EventSource auto-reconnects on its own; we just
        // surface the state change. If the server returned
        // a 401 (bad token) the EventSource enters a
        // CLOSED state and won't auto-reconnect — which is
        // the behavior we want for auth failures.
        if (es.readyState === EventSource.CLOSED) {
          setStatus("closed");
          if (!autoReconnect) {
            stopRef.current = true;
          }
        } else {
          setStatus("error");
        }
      };
    }

    connect();

    return () => {
      stopRef.current = true;
      esRef.current?.close();
    };
  }, [autoReconnect, reconnectDelayMs]);

  function disconnect() {
    stopRef.current = true;
    esRef.current?.close();
  }

  return { status, lastMessage, send: undefined, disconnect };
}
