import "./globals.css";
import "katex/dist/katex.min.css";

import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";

import { PRODUCT_NAME } from "@/lib/branding";
import { Providers } from "@/components/providers";
import { localeDocumentTag } from "@/i18n/config";
import { fontVars } from "./fonts";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("Metadata");
  return { title: PRODUCT_NAME, description: t("description") };
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <html lang={localeDocumentTag(locale)} suppressHydrationWarning className={fontVars}>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <NextIntlClientProvider locale={locale} messages={messages}>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
