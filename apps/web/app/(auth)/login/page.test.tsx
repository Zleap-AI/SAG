/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api } from "@/lib/api";
import type { AuthStatus } from "@/lib/types";
import LaunchPage from "./page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.restoreAllMocks();
});

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function mount(status: AuthStatus) {
  vi.spyOn(api, "authStatus").mockResolvedValue(status);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <LaunchPage />
      </NextIntlClientProvider>,
    );
  });
  return { container, root };
}

async function enter(input: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const passwordStatus: AuthStatus = {
  mode: "password",
  registration_required: false,
  registration_open: false,
};

it("shows email and password fields when password authentication is enabled", async () => {
  const { container, root } = await mount(passwordStatus);

  expect(container.querySelector('input[type="email"]')).not.toBeNull();
  expect(container.querySelector('input[type="password"]')).not.toBeNull();
  expect(container.querySelector('input[autocomplete="name"]')).toBeNull();

  await act(async () => root.unmount());
});

it("submits email and password without a display name for an existing account", async () => {
  const login = vi.spyOn(api, "login").mockResolvedValue({
    access_token: "owner-token",
    token_type: "bearer",
    user: { id: "owner", email: "owner@example.test", name: "Owner", created_at: "2026-08-30" },
  });
  const { container, root } = await mount(passwordStatus);
  const email = container.querySelector('input[type="email"]');
  const password = container.querySelector('input[type="password"]');
  if (!(email instanceof HTMLInputElement) || !(password instanceof HTMLInputElement)) {
    throw new Error("credential inputs not found");
  }

  await enter(email, "owner@example.test");
  await enter(password, "StrongPassword123");
  await act(async () => {
    container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });

  expect(login).toHaveBeenCalledWith({ email: "owner@example.test", password: "StrongPassword123" });
  await act(async () => root.unmount());
});

it("creates the first credential account when password setup is required", async () => {
  const register = vi.spyOn(api, "register").mockResolvedValue({
    access_token: "owner-token",
    token_type: "bearer",
    user: { id: "owner", email: "owner@example.test", name: "Owner", created_at: "2026-08-30" },
  });
  const { container, root } = await mount({
    mode: "password",
    registration_required: true,
    registration_open: true,
  });
  const name = container.querySelector('input[autocomplete="name"]');
  const email = container.querySelector('input[type="email"]');
  const password = container.querySelector('input[type="password"]');
  if (
    !(name instanceof HTMLInputElement) ||
    !(email instanceof HTMLInputElement) ||
    !(password instanceof HTMLInputElement)
  ) {
    throw new Error("setup inputs not found");
  }

  await enter(name, "Owner");
  await enter(email, "owner@example.test");
  await enter(password, "StrongPassword123");
  await act(async () => {
    container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });

  expect(register).toHaveBeenCalledWith({
    name: "Owner",
    email: "owner@example.test",
    password: "StrongPassword123",
  });
  await act(async () => root.unmount());
});
