/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ClientAccessSettings } from "./client-access-settings";
const calls = vi.hoisted(() => ({ translate: (key: string) => key, authStatus: vi.fn(), clientAccessInfo: vi.fn(), clientAccessSessions: vi.fn(),
  clientAccessDevice: vi.fn(), approveClientAccess: vi.fn(), revokeClientAccess: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: calls }));
vi.mock("next-intl", () => ({ useTranslations: () => calls.translate }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ user: { email: "owner@example.test" } }) }));
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
afterEach(async () => { if (root) await act(async () => root.unmount()); container?.remove(); vi.clearAllMocks(); });
async function mount(mode: string) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  calls.authStatus.mockResolvedValue({ mode });
  calls.clientAccessInfo.mockResolvedValue({server_id:"sag-home",api_url:"https://api.example",scope:"knowledge:read knowledge:write"});
  calls.clientAccessSessions.mockResolvedValue([]);
  container = document.createElement("div");document.body.append(container);root = createRoot(container);
  await act(async () => root.render(<ClientAccessSettings />));
}
it("requires password identity before displaying approval controls", async () => {
  await mount("local");
  expect(container.textContent).toContain("passwordRequired");
  expect(container.querySelector("#client-user-code")).toBeNull();
  expect(calls.clientAccessInfo).not.toHaveBeenCalled();
});
it("shows target identity and inspects client before explicit approval", async () => {
  await mount("password");
  expect(container.textContent).toContain("sag-home");
  expect(container.textContent).toContain("owner@example.test");
  calls.clientAccessDevice.mockResolvedValue({client_name:"Laptop alpha",user_code:"1234ABCD",scope:"knowledge:read knowledge:write"});
  const input=container.querySelector<HTMLInputElement>("#client-user-code")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input,"1234ABCD");
    input.dispatchEvent(new Event("input",{bubbles:true}));
  });
  await act(async () => container.querySelector<HTMLButtonElement>('[data-action="inspect"]')!.click());
  expect(container.textContent).toContain("Laptop alpha");
  expect(calls.approveClientAccess).not.toHaveBeenCalled();
  await act(async () => container.querySelector<HTMLButtonElement>('[data-action="approve"]')!.click());
  expect(calls.approveClientAccess).toHaveBeenCalledWith("1234ABCD",true);
});
