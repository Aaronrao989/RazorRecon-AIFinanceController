import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Finance Controller",
  description:
    "Autonomous multi-source reconciliation — deterministic rules + a bounded, guardrailed investigative agent, with honest metrics.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // suppressHydrationWarning: the theme (.dark) class is applied on the client
  // after mount, so the server/client class on <html> can differ on first paint.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
