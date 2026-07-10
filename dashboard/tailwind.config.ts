import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Trading desk dark theme — warm, low-glare, premium
        ink: {
          950: "#070a14", // page background
          900: "#0c111e", // surface
          800: "#131a2b", // elevated surface
          700: "#1c2438", // border
          600: "#2a3450", // subtle border
          500: "#3d4870", // muted text on light
        },
        cream: {
          50: "#f7f4ed",
          100: "#efe9da",
        },
        // Numbers should pop — terminal green for gains, warm red for losses
        gain: {
          DEFAULT: "#10b981",
          dim: "#0a8a64",
        },
        loss: {
          DEFAULT: "#ef6b5a",
          dim: "#b54b3d",
        },
        // Warm gold accent for primary actions
        gold: {
          DEFAULT: "#e6a93b",
          dim: "#b78522",
          glow: "#f5c463",
        },
        muted: {
          DEFAULT: "#7d869e",
          dim: "#525a72",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "Inter", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "JetBrains Mono", "ui-monospace", "monospace"],
        display: ["var(--font-display)", "Inter", "system-ui", "sans-serif"],
      },
      keyframes: {
        "pulse-gain": {
          "0%, 100%": { backgroundColor: "rgba(16, 185, 129, 0.0)" },
          "50%": { backgroundColor: "rgba(16, 185, 129, 0.15)" },
        },
        "pulse-loss": {
          "0%, 100%": { backgroundColor: "rgba(239, 107, 90, 0.0)" },
          "50%": { backgroundColor: "rgba(239, 107, 90, 0.15)" },
        },
        "fade-in": {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "pulse-gain": "pulse-gain 1.2s ease-in-out 1",
        "pulse-loss": "pulse-loss 1.2s ease-in-out 1",
        "fade-in": "fade-in 0.2s ease-out",
      },
    },
  },
  plugins: [],
};
export default config;
