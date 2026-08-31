import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "StudyFlow",
  description: "Canvas-synced notes, calendar, and quizzes for students.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
