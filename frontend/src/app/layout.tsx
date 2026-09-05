import type { Metadata, Viewport } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  // metadataBase lets the OG image URLs resolve absolutely; without it Next
  // warns and crawlers may ignore the tags.
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000"),
  title: {
    default: "AI 对话平台",
    template: "%s · AI 对话平台",
  },
  description: "私有化 AI 对话与知识库平台",
  applicationName: "AI 对话平台",
  manifest: "/manifest.json",
  openGraph: {
    type: "website",
    siteName: "AI 对话平台",
    title: "AI 对话平台",
    description: "私有化 AI 对话与知识库平台",
    locale: "zh_CN",
  },
  twitter: {
    card: "summary",
    title: "AI 对话平台",
    description: "私有化 AI 对话与知识库平台",
  },
  icons: {
    icon: [{ url: "/icon.svg", type: "image/svg+xml" }],
    apple: [{ url: "/icon.svg" }],
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
  // Prevent mobile browsers from zooming the page when the composer input is
  // focused but keep pinch-zoom available (a11y).
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="min-h-screen bg-background antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
