"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Database,
  HardDrive,
  RotateCw,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { getToken, setToken, clearToken } from "@/lib/auth";
import { createStorageBootstrapPoller } from "@/lib/storage-bootstrap";
import type {
  AuthStatus,
  StorageBootstrapStatus,
  StorageChoice,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

let sharedInitialRequest: Promise<StorageBootstrapStatus> | null = null;

function loadInitialStatus(): Promise<StorageBootstrapStatus> {
  if (!sharedInitialRequest) {
    sharedInitialRequest = api.storageBootstrap().finally(() => {
      queueMicrotask(() => {
        sharedInitialRequest = null;
      });
    });
  }
  return sharedInitialRequest;
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function stageKey(stage: string | null): string {
  const knownStages = new Set([
    "queued",
    "detect",
    "select",
    "backup",
    "relational",
    "checkpoints",
    "vectors",
    "verify",
    "swap",
    "rollback",
    "fresh_journal",
    "fresh_target",
    "fresh_backup",
    "copying",
  ]);
  return stage && knownStages.has(stage) ? stage : "working";
}

interface StorageBootstrapGateViewProps {
  status: StorageBootstrapStatus;
  authenticated: boolean;
  selectedChoice: StorageChoice | null;
  submitting: boolean;
  loginName: string;
  loginEmail: string;
  loginPassword: string;
  authStatus: AuthStatus;
  loginLoading: boolean;
  errorMessage: string | null;
  onSelectChoice: (choice: StorageChoice) => void;
  onCancelChoice: () => void;
  onConfirmChoice: () => void;
  onLoginNameChange: (name: string) => void;
  onLoginEmailChange: (email: string) => void;
  onLoginPasswordChange: (password: string) => void;
  onLogin: (event: React.FormEvent) => void;
  onRetry: () => void;
  children: React.ReactNode;
}

export function StorageBootstrapGateView({
  status,
  authenticated,
  selectedChoice,
  submitting,
  loginName,
  loginEmail,
  loginPassword,
  authStatus,
  loginLoading,
  errorMessage,
  onSelectChoice,
  onCancelChoice,
  onConfirmChoice,
  onLoginNameChange,
  onLoginEmailChange,
  onLoginPasswordChange,
  onLogin,
  onRetry,
  children,
}: StorageBootstrapGateViewProps) {
  const t = useTranslations("StorageBootstrap");
  if (status.phase === "ready") return <>{children}</>;

  const preservedPath = authenticated ? status.preserved_path : null;

  return (
    <main className="flex min-h-[100svh] w-full items-center justify-center bg-background px-5 py-10">
      <section className="w-full max-w-xl">
        <header className="mb-7 text-center">
          <span className="mx-auto mb-4 grid size-11 place-items-center rounded-md border bg-muted">
            <Database className="size-5" />
          </span>
          <h1 className="font-display text-2xl font-semibold tracking-normal">
            {t("title")}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {t("description")}
          </p>
        </header>

        {errorMessage ? (
          <div role="alert" className="mb-4 rounded-md border border-destructive/35 bg-destructive/5 p-3 text-sm text-destructive">
            {errorMessage}
          </div>
        ) : null}

        {(status.phase === "choice_required" || status.phase === "failed") &&
        !authenticated ? (
          <form onSubmit={onLogin} className="rounded-lg border bg-card p-5 shadow-soft">
            <h2 className="text-base font-semibold">
              {authStatus.registration_required ? t("setupTitle") : t("loginTitle")}
            </h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              {authStatus.registration_required ? t("setupDescription") : t("loginDescription")}
            </p>
            {authStatus.mode === "local" || authStatus.registration_required ? (
              <Field className="mt-5">
                <FieldLabel htmlFor="storage-bootstrap-name">
                  {t("nameLabel")}
                </FieldLabel>
                <Input
                  id="storage-bootstrap-name"
                  required={authStatus.mode === "local"}
                  maxLength={120}
                  autoComplete="name"
                  value={loginName}
                  onChange={(event) => onLoginNameChange(event.target.value)}
                  placeholder={t("namePlaceholder")}
                />
              </Field>
            ) : null}
            {authStatus.mode === "password" ? (
              <>
                <Field className="mt-5">
                  <FieldLabel htmlFor="storage-bootstrap-email">{t("emailLabel")}</FieldLabel>
                  <Input
                    id="storage-bootstrap-email"
                    type="email"
                    required
                    maxLength={255}
                    autoComplete="email"
                    value={loginEmail}
                    onChange={(event) => onLoginEmailChange(event.target.value)}
                    placeholder={t("emailPlaceholder")}
                  />
                </Field>
                <Field className="mt-5">
                  <FieldLabel htmlFor="storage-bootstrap-password">{t("passwordLabel")}</FieldLabel>
                  <Input
                    id="storage-bootstrap-password"
                    type="password"
                    required
                    minLength={authStatus.registration_required ? 8 : undefined}
                    maxLength={128}
                    autoComplete={authStatus.registration_required ? "new-password" : "current-password"}
                    value={loginPassword}
                    onChange={(event) => onLoginPasswordChange(event.target.value)}
                    placeholder={t("passwordPlaceholder")}
                  />
                </Field>
              </>
            ) : null}
            <Button
              type="submit"
              className="mt-5 w-full"
              disabled={
                loginLoading ||
                (authStatus.mode === "local"
                  ? !loginName.trim()
                  : !loginEmail.trim() || !loginPassword)
              }
            >
              {loginLoading ? <Spinner /> : <ArrowRight />}
              {loginLoading ? t("loggingIn") : t("login")}
            </Button>
          </form>
        ) : null}

        {status.phase === "choice_required" && authenticated && !selectedChoice ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => onSelectChoice("migrate")}
              className="rounded-lg border bg-card p-5 text-left shadow-soft transition-colors hover:border-foreground/30 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <HardDrive className="mb-4 size-5" />
              <strong className="block text-sm">{t("migrateTitle")}</strong>
              <span className="mt-2 block text-sm leading-6 text-muted-foreground">
                {t("migrateSummary")}
              </span>
            </button>
            <button
              type="button"
              onClick={() => onSelectChoice("fresh")}
              className="rounded-lg border bg-card p-5 text-left shadow-soft transition-colors hover:border-foreground/30 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <CheckCircle2 className="mb-4 size-5" />
              <strong className="block text-sm">{t("freshTitle")}</strong>
              <span className="mt-2 block text-sm leading-6 text-muted-foreground">
                {t("freshSummary")}
              </span>
            </button>
          </div>
        ) : null}

        {status.phase === "choice_required" && authenticated && selectedChoice ? (
          <div className="rounded-lg border bg-card p-5 shadow-soft">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-600" />
              <div>
                <h2 className="text-base font-semibold">{t("confirmTitle")}</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  {selectedChoice === "migrate"
                    ? t("migrateConfirmation")
                    : t("freshConfirmation")}
                </p>
              </div>
            </div>
            {preservedPath ? (
              <div className="mt-4 rounded-md bg-muted p-3 text-xs">
                <span className="font-medium">{t("preservedPath")}</span>
                <code className="mt-1 block break-all text-muted-foreground">
                  {preservedPath}
                </code>
              </div>
            ) : null}
            <div className="mt-5 flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={submitting}
                onClick={onCancelChoice}
              >
                {t("back")}
              </Button>
              <Button
                type="button"
                disabled={submitting}
                onClick={onConfirmChoice}
              >
                {submitting ? <Spinner /> : null}
                {submitting ? t("submitting") : t("confirm")}
              </Button>
            </div>
          </div>
        ) : null}

        {status.phase === "processing" ? (
          <div className="rounded-lg border bg-card p-6 text-center shadow-soft" aria-live="polite">
            <Spinner className="mx-auto size-6" />
            <h2 className="mt-4 text-base font-semibold">{t("processingTitle")}</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              {t(`stages.${stageKey(status.stage)}` as "stages.working")}
            </p>
            <p className="mt-4 text-xs text-muted-foreground">
              {t("processingGuidance")}
            </p>
          </div>
        ) : null}

        {status.phase === "failed" && authenticated ? (
          <div className="rounded-lg border border-destructive/35 bg-card p-5 shadow-soft">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" />
              <div>
                <h2 className="text-base font-semibold">{t("failedTitle")}</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  {status.error ?? t("failedDescription")}
                </p>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  {t("recoveryGuidance")}
                </p>
              </div>
            </div>
            {status.recoverable ? (
              <Button type="button" className="mt-5" onClick={onRetry} disabled={submitting}>
                <RotateCw />
                {t("retry")}
              </Button>
            ) : null}
          </div>
        ) : null}
      </section>
    </main>
  );
}

export function StorageBootstrapGate({ children }: { children: React.ReactNode }) {
  const t = useTranslations("StorageBootstrap");
  const [status, setStatus] = React.useState<StorageBootstrapStatus | null>(null);
  const [authenticated, setAuthenticated] = React.useState(false);
  const [selectedChoice, setSelectedChoice] = React.useState<StorageChoice | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [loginName, setLoginName] = React.useState("");
  const [loginEmail, setLoginEmail] = React.useState("");
  const [loginPassword, setLoginPassword] = React.useState("");
  const [authStatus, setAuthStatus] = React.useState<AuthStatus>({
    mode: "local",
    registration_required: false,
    registration_open: false,
  });
  const [loginLoading, setLoginLoading] = React.useState(false);
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);
  const mountedRef = React.useRef(false);
  const submittingRef = React.useRef(false);

  const loadStatus = React.useCallback(
    async (loader: () => Promise<StorageBootstrapStatus>) => {
      const requestedWithToken = Boolean(getToken());
      try {
        const nextStatus = await loader();
        if (mountedRef.current) setAuthenticated(requestedWithToken);
        return nextStatus;
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 401)) throw error;
        clearToken();
        if (mountedRef.current) setAuthenticated(false);
        return api.storageBootstrap();
      }
    },
    [],
  );

  const load = React.useCallback(async () => {
    setErrorMessage(null);
    try {
      const nextStatus = await loadStatus(loadInitialStatus);
      const nextAuthStatus = await api.authStatus();
      if (!mountedRef.current) return;
      setStatus(nextStatus);
      setAuthStatus(nextAuthStatus);
    } catch (error) {
      if (mountedRef.current) {
        setErrorMessage(errorText(error, t("loadFailed")));
      }
    }
  }, [loadStatus, t]);

  React.useEffect(() => {
    mountedRef.current = true;
    void load();
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  React.useEffect(() => {
    if (status?.phase !== "processing") return;
    return createStorageBootstrapPoller(
      () => loadStatus(api.storageBootstrap),
      (nextStatus) => {
        if (mountedRef.current) {
          setErrorMessage(null);
          setStatus(nextStatus);
        }
      },
      (error) => {
        if (mountedRef.current) {
          setErrorMessage(errorText(error, t("pollFailed")));
        }
      },
    );
  }, [loadStatus, status?.phase, t]);

  async function handleLogin(event: React.FormEvent) {
    event.preventDefault();
    const name = loginName.trim();
    const email = loginEmail.trim();
    if (loginLoading) return;
    if (authStatus.mode === "local" && !name) return;
    if (authStatus.mode === "password" && (!email || !loginPassword)) return;
    setLoginLoading(true);
    setErrorMessage(null);
    try {
      const response = authStatus.registration_required
        ? await api.register({ name, email, password: loginPassword })
        : authStatus.mode === "password"
          ? await api.login({ email, password: loginPassword })
          : await api.login({ name });
      setToken(response.access_token);
      const nextStatus = await loadStatus(api.storageBootstrap);
      if (!mountedRef.current) return;
      setStatus(nextStatus);
    } catch (error) {
      if (mountedRef.current) {
        setErrorMessage(errorText(error, t("loginFailed")));
      }
    } finally {
      if (mountedRef.current) setLoginLoading(false);
    }
  }

  async function submitChoice(choice: StorageChoice) {
    if (submittingRef.current) return;
    const previousStatus = status;
    submittingRef.current = true;
    setSubmitting(true);
    setErrorMessage(null);
    setSelectedChoice(null);
    setStatus((current) => current ? {
      ...current,
      phase: "processing",
      choices: [],
      stage: "queued",
      error: null,
      recoverable: false,
      runtime_ready: false,
      accepted_choice: choice,
    } : current);
    try {
      const nextStatus = await api.chooseStorageBootstrap(choice);
      if (!mountedRef.current) return;
      setStatus(nextStatus);
    } catch (error) {
      if (!mountedRef.current) return;
      if (error instanceof ApiError && error.status === 401) {
        clearToken();
        setAuthenticated(false);
        setStatus(previousStatus);
        return;
      }
      try {
        const authoritativeStatus = await loadStatus(api.storageBootstrap);
        if (!mountedRef.current) return;
        if (authoritativeStatus.phase !== "choice_required") {
          setStatus(authoritativeStatus);
          return;
        }
        setStatus(previousStatus);
        setErrorMessage(errorText(error, t("submitFailed")));
      } catch {
        // The POST and reconciliation result are both unknown. Keep the gate
        // locked in processing so the status poller can converge safely.
      }
    } finally {
      submittingRef.current = false;
      if (mountedRef.current) setSubmitting(false);
    }
  }

  async function handleConfirmChoice() {
    if (!selectedChoice) return;
    await submitChoice(selectedChoice);
  }

  async function handleRetry() {
    const acceptedChoice = authenticated ? status?.accepted_choice : null;
    if (status?.phase === "failed" && status.recoverable && acceptedChoice) {
      await submitChoice(acceptedChoice);
      return;
    }
    await load();
  }

  if (!status) {
    return (
      <main className="grid min-h-[100svh] place-items-center bg-background px-5">
        <div className="text-center">
          <Spinner className="mx-auto size-6" />
          <p className="mt-3 text-sm text-muted-foreground">
            {errorMessage ?? t("loading")}
          </p>
          {errorMessage ? (
            <Button type="button" variant="outline" className="mt-4" onClick={() => void load()}>
              <RotateCw />
              {t("retry")}
            </Button>
          ) : null}
        </div>
      </main>
    );
  }

  return (
    <StorageBootstrapGateView
      status={status}
      authenticated={authenticated}
      selectedChoice={selectedChoice}
      submitting={submitting}
      loginName={loginName}
      loginEmail={loginEmail}
      loginPassword={loginPassword}
      authStatus={authStatus}
      loginLoading={loginLoading}
      errorMessage={errorMessage}
      onSelectChoice={setSelectedChoice}
      onCancelChoice={() => setSelectedChoice(null)}
      onConfirmChoice={() => void handleConfirmChoice()}
      onLoginNameChange={setLoginName}
      onLoginEmailChange={setLoginEmail}
      onLoginPasswordChange={setLoginPassword}
      onLogin={(event) => void handleLogin(event)}
      onRetry={() => void handleRetry()}
    >
      {children}
    </StorageBootstrapGateView>
  );
}
