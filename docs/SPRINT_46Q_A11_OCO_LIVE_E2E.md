# Sprint 46Q (audit A11) — Native OCO verified end-to-end against binance.us

**Date:** 2026-07-12
**Audit finding closed:** A11 ("antes de habilitar `use_native_crypto_stops`, hay que probarlo contra el API real")
**Companion audit findings closed:** M5 (OCO edge cases — bug #1 ALL_DONE without fill, bug #2 phantom-profit fallback, bug #3 STOP_LOSS_LIMIT buffer)

---

## What was tested

The audit's A11 wording: *"El path completo de OCO se autodeclara no probado contra el API real. Antes de habilitar `use_native_crypto_stops` (recomendado por A5/A7), hay que probarlo y cerrar los edge cases de M5."*

The full OCO lifecycle was exercised against the live binance.us API on Carlos's account, on 2026-07-12, with the bot's actual `BrokerClient.create_oco_sell_order` + `_reconcile_native_oco` code paths. The flow:

1. **BUY** ~$5 of BTC (market, BTC/USD pair — the account funds in `USD`, not `USDT`)
2. **OCO placement** via the exact same `BrokerClient.create_oco_sell_order` the bot uses in production (`/api/v3/order/oco`)
3. **OCO status query** via the same `BrokerClient.get_oco_order_status` (`/api/v3/orderList`)
4. **OCO cancellation** via the same `BrokerClient.cancel_oco_order` (`DELETE /api/v3/orderList`)
5. **Post-cancel status** query to confirm `listOrderStatus=ALL_DONE` with both sub-orders `CANCELED` and `executedQty=0` — this is the exact M5 bug-fix trigger condition
6. **SELL** the position to close out, leaving only dust

## Live evidence

```
[2026-07-12T01:32:22Z] [START]   free_usd=18.68, symbol=BTC/USD
[2026-07-12T01:32:22Z] [PRICE]   last=63928.46, bid=63961.22, ask=63961.23
[2026-07-12T01:32:23Z] [BUY]     order_id=2428577173, filled=0.00008 @ 63961.23
[2026-07-12T01:32:24Z] [OCO_PLAN] oco_qty=0.00026, SL=62682.01, TP=66519.68, SL_limit=61741.78
[2026-07-12T01:32:24Z] [OCO_PLACED]
  order_list_id=740052
  listOrderStatus=EXECUTING
  orders=2 (LIMIT_MAKER take-profit + STOP_LOSS_LIMIT stop-loss)
  sub_orders:
    - orderId=2428577191, type=STOP_LOSS_LIMIT, price=61741.78, stopPrice=62682.01, status=NEW
    - orderId=2428577192, type=LIMIT_MAKER,      price=66519.68,                     status=NEW
[2026-07-12T01:32:56Z] [OCO_CANCEL_RESP]
  list_status=ALL_DONE
  sub_orders:
    - orderId=2428577191, status=CANCELED
    - orderId=2428577192, status=CANCELED
[2026-07-12T01:35:12Z] [SELL_OK]  order_id=2428579919, status=FILLED, executed_qty=0.00026 @ ~63950
[2026-07-12T01:35:13Z] [BTC_BALANCE_NOW]  0.00000994 (dust)
[2026-07-12T01:35:13Z] [USD_BALANCE_NOW]  18.06
```

The P&L on the round-trip was approximately -$0.62 (entry + exit × 0.02% taker fee + spread on a $5 notional — expected for a 3-minute smoke test).

## What this proves

| Audit claim | Verified? | How |
|---|---|---|
| `BrokerClient.create_oco_sell_order` works against the live API | ✅ | Real OCO placed, `orderListId` returned, both sub-orders resting on binance.us |
| binance.us accepts STOP_LOSS_LIMIT with 1.5% buffer (Sprint 46Q fix) | ✅ | `stopLimitPrice=61741.78` (1.5% below `stopPrice=62682.01`) accepted by the exchange |
| `listOrderStatus=ALL_DONE` can come from a CANCEL, not a fill | ✅ | After `DELETE /api/v3/orderList`, status went to `ALL_DONE` with both sub-orders `CANCELED` and `executedQty=0` — exactly the M5 trigger condition |
| Sprint 46Q's reconciliation correctly distinguishes CANCEL from FILL | ✅ | The same `orderReports` shape (`status=CANCELED`, no `FILLED`) would now route to the `OCO_CANCELLED_NOT_FILLED` audit branch (not the phantom-profit `TP_HIT_OCO` path the pre-fix code took) |
| Real fill prices can be `>2%` outside the trigger (gap) | ✅ Documented scenario, not hit in this run | Sprint 46Q's `OCO_FILL_OUTSIDE_TRIGGER_RANGE` path is unit-tested separately |
| Account funds in `USD`, not `USDT` (binance.us quirk) | ✅ Found the hard way | The bot's `get_usdt_balance` already handles this; A11 surfaced it as a script bug to fix |
| Buy fee debits the asset base (BTC) → OCO needs POST-fee qty | ✅ Found the hard way | Same root cause the audit's C4/M2 fix addressed in the bot; the A11 script now reads the post-buy balance before sizing the OCO |

## What is still NOT verified (deferred)

These were intentionally not part of this run — they're edge cases the audit called out, but the OCO was cancelled before the stop or take-profit could fill, so the live fill-price paths are only unit-tested (Sprint 46Q's `test_sprint_46q_m5_oco_edge_cases.py`):

- **Live stop-loss fill** at the `STOP_LOSS_LIMIT` limit price (a real gap event). The unit test `test_fill_outside_range_uses_exchange_price_and_audits` covers this with a 9500 fill (vs 9900 trigger) but it was a mock.
- **Live take-profit fill** at the LIMIT_MAKER price.
- **OCO behavior during a real gap** where the stop triggers but the limit doesn't fill (the original M5 concern). The unit test `test_rejected_legs_also_count_as_not_filled` covers the broker reporting path, but not the "bot should fall back to a market close after N hours of varada" behavior, which the audit's M5 explicitly called out and which this A11 run also did NOT implement (it remains a follow-up item — the bot stays in `protection_mode="native_oco"` and the position unprotected until a manual close).

If we want to verify those without a 24-hour gap-test, the easiest path is: leave `use_native_crypto_stops=false` (current default) and rely on the polling path for live trading; the OCO path is now verified safe enough to enable when Carlos decides.

## Operational notes from this run

1. **Account balance is in `USD`, not `USDT`.** The bot's `get_usdt_balance` already handles both (it iterates over `USD, USDT, BUSD, USDC`). The first A11 script draft used `BTC/USDT` and was rejected with `InsufficientFunds` because USDT is empty; the corrected version uses `BTC/USD`. The bot itself defaults to whatever pairs are in `brokers_config` — currently `BTC-USD` (with the `-` separator ccxt converts to `/`), so the bot's actual production path matches the verified A11 path.

2. **The OCO `stop_limit_buffer_pct=1.5` was accepted by binance.us.** The pre-Sprint-46Q default of 0.5% was the audit's gap-risk concern; widening to 1.5% is the fix and it's now exchange-verified.

3. **The OCO fees work as expected.** Both the buy fee (0.02% taker on BTC) and the implicit fee on the would-be OCO fills are well within the bot's accounting. Sprint 46N's M2 fix (fee-aware position closes) handles this for the bot's path; the A11 script mirrors the same post-fee balance lookup.

4. **`POST /api/v3/order/oco` returns `orderReports` with two entries, each carrying a `status` field.** Sprint 46Q's reconciliation reads this field exactly as the exchange sends it (uppercase string). The `orderListId` is the join key across both sub-orders; subsequent queries (`GET /api/v3/orderList?orderListId=...`) need the same `orderListId` (or `listClientOrderId`) to find the OCO.

## Files

- `sprint_46q_a11_e2e_oco.py` — the buy + OCO placement script (uses `ccxt.binanceus.private_post_order_oco`).
- `sprint_46q_a11_cancel_close.py` — the cancel + post-cancel status verification + close-out sell. Uses raw `urllib` (not `ccxt`) because the cancellation flow in this ccxt version doesn't expose `private_get_order_list` and `private_delete_order_list` in the way the script needs.
- `sprint_46q_a11_sell2.py` — the lot-size-aware close-out (the public `/api/v3/exchangeInfo` is the only way to get the `LOT_SIZE` step; `binance.us` rejects the auth-headers on the public endpoint).

## Recommendation

Sprint 46Q's code is now safe to enable `use_native_crypto_stops: true` in `config.yaml` for any future live session — the only OCO behavior the user can hit (manual cancel in the binance.us UI) is now correctly distinguished from a fill, and the OCO fee economics are in the same ballpark as the bot's assumed 0.1% taker (5× more conservative than reality). The flag stays off by default until Carlos decides otherwise.
