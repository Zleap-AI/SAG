"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { ArrowRight, Github, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { PRODUCT_NAME } from "@/lib/branding";
import { buildSingleUserSetupRequest } from "@/lib/login";
import { LanguageToggle } from "@/components/features/language-toggle";
import { ThemeToggle } from "@/components/features/theme-toggle";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

export default function LaunchPage() {
  const t = useTranslations("Login");
  const router = useRouter();
  const [name, setName] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [setupRequired, setSetupRequired] = React.useState(false);

  React.useEffect(() => {
    let alive = true;
    api.singleUserSession()
      .then((session) => {
        if (!alive) return;
        if (session.setup_required) {
          setSetupRequired(true);
          setLoading(false);
          return;
        }
        router.replace("/chat");
      })
      .catch(() => {
        if (!alive) return;
        setSetupRequired(true);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const nextName = name.trim();
    if (!nextName) return;
    setLoading(true);
    try {
      const request = buildSingleUserSetupRequest(nextName);
      const response = await api.initializeSingleUser(request.name);
      toast.success(t("welcome", { name: response.user?.name ?? request.name }));
      router.replace("/chat");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : t("failed");
      toast.error(message);
      setLoading(false);
    }
  }

  return (
    <main className="relative flex min-h-[100svh] w-full flex-col px-5 py-5 sm:px-9 sm:py-7">
      <header className="relative z-30 flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5" aria-label={PRODUCT_NAME}>
          <span className="grid size-8 place-items-center rounded-md border bg-background/70 shadow-soft backdrop-blur-md">
            <Sparkles className="size-4" />
          </span>
          <span className="font-display text-sm font-semibold">{PRODUCT_NAME}</span>
        </div>
        <div className="flex items-center gap-1">
          <LanguageToggle className="bg-background/68 shadow-soft backdrop-blur-md" />
          <ThemeToggle />
          <a
            href="https://github.com/Zleap-AI/SAG"
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-9 items-center gap-2 rounded-md border bg-background/68 px-3 text-xs font-medium text-muted-foreground shadow-soft outline-none backdrop-blur-md transition-colors hover:bg-background hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Github className="size-4" />
            <span className="hidden sm:inline">Zleap-AI/SAG</span>
            <span className="sm:hidden">GitHub</span>
          </a>
        </div>
      </header>

      <section className="relative z-20 flex flex-1 items-center justify-center py-8 sm:py-10">
        <div className="w-full max-w-[352px] animate-fade-in text-center">
          <h1 className="font-display text-3xl font-semibold tracking-normal sm:text-4xl">
            {t("hero")}
          </h1>

          {loading && !setupRequired ? (
            <div className="mt-6 flex justify-center"><Spinner /></div>
          ) : <form
            onSubmit={onSubmit}
            className="mt-6 flex flex-col gap-4 rounded-lg border bg-background/76 p-5 text-left shadow-lift backdrop-blur-xl"
          >
            <Field>
              <FieldLabel htmlFor="name">{t("nameLabel")}</FieldLabel>
              <Input
                id="name"
                required
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t("namePlaceholder")}
                autoComplete="name"
                autoFocus
              />
            </Field>
            <Button
              type="submit"
              size="lg"
              disabled={loading || !name.trim()}
              className="mt-1 w-full"
            >
              {loading ? <Spinner /> : <ArrowRight className="size-4" />}
              {loading ? t("submitting") : t("submit")}
            </Button>
          </form>}
        </div>
      </section>
    </main>
  );
}
