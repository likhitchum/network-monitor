import { expect } from '@playwright/test';

export const ADMIN = {
  email: process.env.E2E_ADMIN_EMAIL ?? 'admin@example.com',
  password: process.env.E2E_ADMIN_PASSWORD ?? 'admin123',
};

export const USER = {
  email: process.env.E2E_USER_EMAIL ?? 'user@example.com',
  password: process.env.E2E_USER_PASSWORD ?? 'user123',
};

/**
 * Login through the real form. Forces the UI language to English first (the
 * app persists the last chosen language, so fresh contexts start in Thai and
 * text assertions would otherwise depend on the stored preference).
 */
export async function login(page, account = ADMIN) {
  await page.addInitScript(() => localStorage.setItem('networkpulse-language', 'en'));
  await page.goto('/');
  await page.fill('#loginEmail', account.email);
  await page.fill('#loginPassword', account.password);
  await page.click('.login-submit');
  await expect(page.locator('.app-shell')).toBeVisible();
}

/**
 * Delete devices created by the E2E suite via the API (cleanup so reruns stay
 * deterministic — the monitor loop re-checks devices and re-raises alerts).
 */
export async function deleteDevicesByName(page, name) {
  const loginResponse = await (await page.request.post('/api/auth/login', {
    data: { email: ADMIN.email, password: ADMIN.password },
  })).json();
  const headers = { Authorization: `Bearer ${loginResponse.access_token}` };
  const devices = await (await page.request.get('/api/devices', { headers })).json();
  for (const device of devices.filter((d) => d.name === name)) {
    await page.request.delete(`/api/devices/${device.id}`, { headers });
  }
}

/** Unique-ish device name so concurrent reruns and old data never collide. */
export function uniqueName(prefix) {
  return `${prefix} ${Date.now()}`;
}
