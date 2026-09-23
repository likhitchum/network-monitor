import { expect, test } from '@playwright/test';
import { login } from './helpers.js';

/**
 * Login flow — the entry point every other suite depends on.
 * Accounts are seeded by the backend on first boot from ADMIN_EMAIL/ADMIN_PASSWORD.
 * The shared login helper pins the UI language to English.
 */
test.describe('Login', () => {
  test('shows the login screen with demo accounts', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#loginScreen')).toBeVisible();
    await expect(page.locator('.login-heading h1')).toHaveText(/Welcome back|ยินดีต้อนรับกลับมา/);
    await expect(page.locator('[data-demo-role="admin"]')).toBeVisible();
    await expect(page.locator('[data-demo-role="user"]')).toBeVisible();
  });

  test('rejects wrong credentials without leaving the login screen', async ({ page }) => {
    await page.goto('/');
    await page.fill('#loginEmail', 'admin@example.com');
    await page.fill('#loginPassword', 'totally-wrong-password');
    await page.click('.login-submit');
    await expect(page.locator('#loginError')).not.toBeEmpty();
    await expect(page.locator('#loginScreen')).toBeVisible();
    await expect(page.locator('.app-shell')).toBeHidden();
  });

  test('logs in as admin and lands on the dashboard', async ({ page }) => {
    await login(page);
    await expect(page.locator('#pageTitle')).toHaveText('Network overview');
    // Identity comes from the real API, not the demo constants
    await expect(page.locator('#currentUserName')).not.toHaveText('Alex Morgan');
    await expect(page.locator('#roleBadge')).toHaveText(/ADMIN/i);
    // Summary numbers load from /api/summary
    await expect(page.locator('#summaryDevices')).toBeVisible();
  });

  test('logs out and returns to the login screen', async ({ page }) => {
    await login(page);
    await page.click('#logoutBtn');
    await expect(page.locator('#loginScreen')).toBeVisible();
    await expect(page.locator('.app-shell')).toBeHidden();
  });
});
