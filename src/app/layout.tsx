import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: "Field Capture",
  description: "Offline-capable field photo and inspection capture.",
  // iOS ignores maskable manifest icons and wants apple-touch-icon instead, so both
  // are declared rather than assuming the manifest covers every platform.
  appleWebApp: { capable: true, title: "Capture", statusBarStyle: "black-translucent" },
  icons: { apple: "/icon-192.png" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Pinch-zoom is deliberately NOT disabled. This is read one-handed in sunlight on a
  // roof, and blocking zoom on a form someone squints at is an accessibility defect,
  // not a polish decision.
  themeColor: "#0c1222",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          background: "#0c1222",
          color: "#e8edf7",
          fontFamily:
            "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
          WebkitTextSizeAdjust: "100%",
        }}
      >
        {children}
      </body>
    </html>
  );
}
