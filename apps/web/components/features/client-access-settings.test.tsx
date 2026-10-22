// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ClientAccessSettings } from './client-access-settings';
const { translate } = vi.hoisted(() => ({ translate: (key: string, values?: { device: string; user: string }) => values ? `${key}: ${values.device} / ${values.user}` : key }));
vi.mock('next-intl', () => ({ useTranslations: () => translate }));
vi.mock('@/lib/api', () => ({ api: { clientAccessInfo: vi.fn(), clientAccessSessions: vi.fn(), clientAccessInspect: vi.fn(), clientAccessApprove: vi.fn(), clientAccessPolicy: vi.fn(), clientAccessRevoke: vi.fn() } }));
import { api } from '@/lib/api';
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.clientAccessInfo).mockResolvedValue({ protocol: 'client-v1', deployment: 'fnos', server_id: 's', enabled: true, api_url: 'https://nas.example', verification_uri: 'https://nas.example/settings', can_manage_policy: true, host_lifecycle_synchronized: false });
  vi.mocked(api.clientAccessSessions).mockResolvedValue([]);
  vi.mocked(api.clientAccessInspect).mockResolvedValue({ client_name: 'CI runner', user_name: 'Alice', scope: 'knowledge:read knowledge:write' });
});
afterEach(cleanup);
it('shows sessions and explicit connection metadata without admin controls', async () => {
  vi.mocked(api.clientAccessInfo).mockResolvedValue({ protocol: 'client-v1', deployment: 'fnos', server_id: 's', enabled: true, api_url: 'https://nas.example/client', verification_uri: 'https://nas.example/app/sag/settings', can_manage_policy: false, host_lifecycle_synchronized: false });
  vi.mocked(api.clientAccessSessions).mockResolvedValue([{ session_id: 'one', client_name: 'Build machine', scope: 'knowledge:read knowledge:write', access_expires_at: '2026-11-20T00:00:00Z', refresh_expires_at: '2027-01-19T00:00:00Z', absolute_expires_at: '2027-10-21T00:00:00Z', last_used_at: null, revoked_at: null }]);
  render(<ClientAccessSettings />);
  await waitFor(() => expect(screen.getByText('Build machine')).toBeTruthy());
  expect(screen.getByText('https://nas.example/client')).toBeTruthy();
  expect(screen.queryByText('savePolicy')).toBeNull();
});

it('previews the client and account before approving and invalidates preview when code changes', async () => {
  render(<ClientAccessSettings />);
  const code = await screen.findByLabelText('userCode');
  fireEvent.change(code, { target: { value: 'abcd1234' } });
  expect((screen.getByText('approve') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText('inspect'));
  await screen.findByText('approveDevice: CI runner / Alice');
  expect(api.clientAccessInspect).toHaveBeenCalledWith('ABCD1234');
  await waitFor(() => expect((screen.getByText('approve') as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText('approve'));
  await waitFor(() => expect(api.clientAccessApprove).toHaveBeenCalledWith('ABCD1234', true));
  await screen.findByText('approved');
  fireEvent.change(code, { target: { value: 'different' } });
  expect(screen.queryByText('approveDevice: CI runner / Alice')).toBeNull();
  expect((screen.getByText('approve') as HTMLButtonElement).disabled).toBe(true);
});
it('denies the entered request and does not claim success when the operation fails', async () => {
  vi.mocked(api.clientAccessApprove).mockRejectedValueOnce(new Error('unavailable'));
  render(<ClientAccessSettings />);
  fireEvent.change(await screen.findByLabelText('userCode'), { target: { value: 'abcd1234' } });
  fireEvent.click(screen.getByText('deny'));
  await screen.findByText('actionFailed');
  expect(api.clientAccessApprove).toHaveBeenCalledWith('ABCD1234', false);
  expect(screen.queryByText('denied')).toBeNull();
  fireEvent.click(screen.getByText('deny'));
  await screen.findByText('denied');
});
it('saves administrator policy with complete edited URLs', async () => {
  render(<ClientAccessSettings />);
  fireEvent.click(await screen.findByLabelText('enable'));
  fireEvent.change(screen.getByLabelText('apiUrl'), { target: { value: 'https://new.example/client' } });
  fireEvent.change(screen.getByLabelText('webUrl'), { target: { value: 'https://new.example/settings' } });
  fireEvent.click(screen.getByText('savePolicy'));
  await waitFor(() => expect(api.clientAccessPolicy).toHaveBeenCalledWith({ enabled: false, api_url: 'https://new.example/client', verification_uri: 'https://new.example/settings' }));
  await screen.findByText('saved');
});
it('revokes the selected device and reloads the device list', async () => {
  vi.mocked(api.clientAccessSessions).mockResolvedValue([{ session_id: 'one', client_name: 'Build machine', scope: 'knowledge:read knowledge:write', access_expires_at: '2026-11-20T00:00:00Z', refresh_expires_at: '2027-01-19T00:00:00Z', absolute_expires_at: '2027-10-21T00:00:00Z', last_used_at: null, revoked_at: null }]);
  render(<ClientAccessSettings />);
  await screen.findByText('Build machine');
  vi.mocked(api.clientAccessSessions).mockResolvedValue([]);
  fireEvent.click(screen.getByText('revoke'));
  await waitFor(() => expect(api.clientAccessRevoke).toHaveBeenCalledWith('one'));
  await screen.findByText('empty');
  expect(screen.queryByText('Build machine')).toBeNull();
});

it('ignores an obsolete inspection response and guards direct form submission', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof api.clientAccessInspect>>) => void;
  vi.mocked(api.clientAccessInspect).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  render(<ClientAccessSettings />);
  const code = await screen.findByLabelText('userCode');
  fireEvent.change(code, { target: { value: 'aaaa1111' } });
  fireEvent.click(screen.getByText('inspect'));
  fireEvent.change(code, { target: { value: 'bbbb2222' } });
  resolve({ client_name: 'Device A', user_name: 'Alice', scope: 'knowledge:read knowledge:write' });
  await waitFor(() => expect((screen.getByText('inspect') as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByText('approveDevice: Device A / Alice')).toBeNull();
  expect((screen.getByText('approve') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.submit(code.closest('form')!);
  expect(api.clientAccessApprove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('inspect'));
  await screen.findByText('approveDevice: CI runner / Alice');
  await waitFor(() => expect((screen.getByText('approve') as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText('approve'));
  await waitFor(() => expect(api.clientAccessApprove).toHaveBeenCalledWith('BBBB2222', true));
});
