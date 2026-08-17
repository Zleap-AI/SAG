"use client";

import { ThemeProvider } from "next-themes";

import { StorageBootstrapGate } from "@/components/features/storage-bootstrap-gate";
import { Toaster } from "@/components/ui/sonner";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      <StorageBootstrapGate>{children}</StorageBootstrapGate>
      <Toaster />
    </ThemeProvider>
  );
}
