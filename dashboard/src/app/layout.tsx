import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Guaritradbot — Trading Desk",
  description: "Live dashboard for the Guaritradbot autonomous trading system.",
};

export const viewport: Viewport = {
  themeColor: "#070a14",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body
        className={`${inter.variable} ${jetbrains.variable} min-h-screen bg-ink-950 text-cream-50 antialiased`}
      >
        <div className="relative z-10">{children}</div>
      </body>
    </html>
  );
}
