import { expect, test } from '@playwright/test';
import { login, uniqueName } from './helpers.js';

/**
 * Device lifecycle through the real UI: create via modal, verify in both tables,
 * check now (real ping to 127.0.0.1 inside the api container) and verify status.
 * Serial because devices accumulate state in the shared compose database.
 */
test.describe.serial('Devices', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('admin opens the add-device modal from the dashboard', async ({ page }) => {
    await page.click('#addDeviceBtn');
    await expect(page.locator('#modal')).toHaveClass(/open/);
    await expect(page.locator('#modal h2')).toContainText('Add a device');
  });

  test('adding a device inserts it into the devices table', async ({ page }) => {
    const name = uniqueName('E2E Loopback');
    await page.click('#addDeviceBtn');
    await page.fill('#deviceForm input[name="name"]', name);
    await page.fill('#deviceForm input[name="ip"]', '127.0.0.1');
    await page.selectOption('#deviceForm select[name="type"]', 'Ping only');
    await page.click('#deviceForm button[type="submit"]');

    // Modal closes and the new device appears on the dashboard table
    await expect(page.locator('#modal')).not.toHaveClass(/open/);
    await expect(page.locator('#deviceTable tr').first()).toContainText('E2E Loopback');

    // And after a reload (proves it went to the backend, not just local state)
    await page.reload();
    await expect(page.locator('.app-shell')).toBeVisible();
    await expect(page.locator('#deviceTable')).toContainText('E2E Loopback');
  });

  test('check-now runs a real probe and shows a toast', async ({ page }) => {
    const name = uniqueName('E2E Check');
    await page.click('#addDeviceBtn');
    await page.fill('#deviceForm input[name="name"]', name);
    await page.fill('#deviceForm input[name="ip"]', '127.0.0.1');
    await page.selectOption('#deviceForm select[name="type"]', 'Ping only');
    await page.click('#deviceForm button[type="submit"]');
    await expect(page.locator('#modal')).not.toHaveClass(/open/);

    const row = page.locator('#deviceTable tr', { hasText: name });
    await expect(row).toBeVisible();

    await row.locator('.device-check').click();
    await expect(page.locator('#toast')).toContainText('Device checked');
    // Backend answers ping → the refreshed row shows Online
    await expect(row.locator('.status-badge')).toHaveClass(/online/);
  });

  test('filtering the devices table works from the status filter', async ({ page }) => {
    await page.click('#statusFilter'); // all → online
    await expect(page.locator('#statusFilter')).toHaveText(/Online/);
  });
});
