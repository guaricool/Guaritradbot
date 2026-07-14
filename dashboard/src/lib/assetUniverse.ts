// Asset universe for the dashboard's /charts page.
//
// Sprint 59: expanded from 7 to 15 assets across three categories
// (crypto / forex / stocks+ETFs). These are READ-ONLY on the
// dashboard -- the trading bot still only trades the 7 assets it
// had before (BTC-USD, ETH-USD, SOL-USD, SPY, QQQ, GLD, USO).
// Forex and the extra stocks are pure visualization.
//
// yfinance ticker conventions:
//   - Crypto:   "BTC-USD"   (dash, not slash)
//   - Stocks:   "AAPL"      (plain ticker)
//   - Forex:    "EURUSD=X"  (=X suffix marks FX pair)
//
// If the bot's tradeable universe ever grows, add new entries here
// and they'll automatically appear in the /charts grid (grouped by
// category) and in the click-to-expand modal.

import type { AssetCategory } from "./types";

export interface AssetEntry {
  /** yfinance-formatted ticker passed to /api/candles */
  ticker: string;
  /** Human label shown in the chart card / modal header */
  label: string;
  /** Group used for visual sectioning + the category badge */
  category: AssetCategory;
  /**
   * For forex pairs the dashboard should swap the base/quote
   * display (e.g. show "1.0823" instead of "0.00092 EUR per USD").
   * yfinance's EURUSD=X gives the price in USD per EUR, which is
   * the convention humans expect.
   */
}

export const ASSET_UNIVERSE: AssetEntry[] = [
  // ---- Crypto (bot currently trades these) ----
  { ticker: "BTC-USD", label: "Bitcoin",   category: "crypto" },
  { ticker: "ETH-USD", label: "Ethereum",  category: "crypto" },
  { ticker: "SOL-USD", label: "Solana",    category: "crypto" },

  // ---- Forex (visualization only -- bot does NOT trade FX) ----
  // yfinance uses "=X" suffix for currency pairs and returns
  // price in terms of the SECOND currency (EURUSD=X -> USD per
  // EUR, GBPUSD=X -> USD per GBP, etc.). Standard FX convention.
  { ticker: "EURUSD=X", label: "EUR / USD", category: "forex"  },
  { ticker: "GBPUSD=X", label: "GBP / USD", category: "forex"  },
  { ticker: "USDJPY=X", label: "USD / JPY", category: "forex"  },
  { ticker: "USDCAD=X", label: "USD / CAD", category: "forex"  },
  { ticker: "AUDUSD=X", label: "AUD / USD", category: "forex"  },

  // ---- Stocks + ETFs (bot trades SPY/QQQ/GLD/USO; AAPL/NVDA/TSLA
  // are visualization only) ----
  { ticker: "SPY",  label: "S&P 500 ETF",     category: "equity" },
  { ticker: "QQQ",  label: "Nasdaq-100 ETF",  category: "equity" },
  { ticker: "GLD",  label: "Gold ETF",        category: "equity" },
  { ticker: "USO",  label: "Oil ETF",         category: "equity" },
  { ticker: "AAPL", label: "Apple",           category: "equity" },
  { ticker: "NVDA", label: "Nvidia",          category: "equity" },
  { ticker: "TSLA", label: "Tesla",           category: "equity" },
];

/**
 * Group assets by category for the /charts page sectioning. Order
 * of categories is fixed so the layout is stable: crypto first
 * (the bot's actual tradeable universe), then forex, then stocks.
 */
export function groupAssetsByCategory(
  assets: AssetEntry[] = ASSET_UNIVERSE,
): Record<AssetCategory, AssetEntry[]> {
  const out: Record<AssetCategory, AssetEntry[]> = {
    crypto: [],
    forex:  [],
    equity: [],
  };
  for (const a of assets) out[a.category].push(a);
  return out;
}

export const CATEGORY_LABELS: Record<AssetCategory, string> = {
  crypto: "Crypto",
  forex:  "Forex",
  equity: "Stocks & ETFs",
};

export const CATEGORY_ORDER: AssetCategory[] = ["crypto", "forex", "equity"];
