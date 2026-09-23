import { expect, test } from '@playwright/test';
import { deleteDevicesByName, login, uniqueName } from './helpers.js';

/**
 * Alert lifecycle: force a real failing check (device points at a reserved,
 * unreachable address → down → alert created by persist_result), then resolve
 * it from the Alert center and verify every panel agrees.
 */
test.describe.serial('Alerts', () => {
  test('a failing check creates an alert that can be resolved from the UI', async ({ page }) => {
    await login(page);

    // Create a device that will always fail (TEST-NET-3, non-routable)
    const name = uniqueName('E2E Down');
    await page.click('#addDeviceBtn');
    await page.fill('#deviceForm input[name="name"]', name);
    await page.fill('#deviceForm input[name="ip"]', '203.0.113.1');
    await page.selectOption('#deviceForm select[name="type"]', 'Ping only');
    await page.click('#deviceForm button[type="submit"]');
    await expect(page.locator('#modal')).not.toHaveClass(/open/);

    // Force a failing check from the devices view
    await page.click('[data-view="devices"]');
    const row = page.locator('#viewDeviceTable tr', { hasText: name });
    await expect(row).toBeVisible();
    await row.locator('.device-check').click();
    await expect(page.locator('#toast')).toContainText('Device checked');
    await expect(row.locator('.status-badge')).toHaveClass(/critical/);

    // Alert center lists the new active alert with a Resolve button
    await page.click('[data-view="alerts"]');
    const alertRow = page.locator('.alert-row', { hasText: name });
    await expect(alertRow).toBeVisible();
    const resolveBtn = alertRow.locator('.resolve-alert');
    await expect(resolveBtn).toBeVisible();

    // Resolve it — row switches to resolved state and the button disappears
    await resolveBtn.click();
    await expect(page.locator('#toast')).toContainText('Alert resolved');
    await expect(alertRow).toContainText('resolved');
    await expect(alertRow.locator('.resolve-alert')).toHaveCount(0);

    // Cleanup: delete the always-down device so the monitor loop cannot raise
    // a fresh alert for it after this run (keeps reruns deterministic)
    await deleteDevicesByName(page, name);
  });

  test('user role cannot see management views', async ({ page }) => {
    await login(page, {
      email: process.env.E2E_USER_EMAIL ?? 'user@example.com',
      password: process.env.E2E_USER_PASSWORD ?? 'user123',
    });
    await expect(page.locator('#roleBadge')).toHaveText(/USER/i);
    // reports/settings nav items are hidden for non-admins, and so is Add device
    await expect(page.locator('[data-view="settings"]')).toBeHidden();
    await expect(page.locator('[data-view="reports"]')).toBeHidden();
    await expect(page.locator('#addDeviceBtn')).toBeHidden();
  });
});
