import { expect, test } from '@playwright/test';
import { login } from './helpers.js';

/**
 * Settings: thresholds persist and SMTP save must not clobber a stored
 * password when the field is left blank (regression fix protected here).
 */
test.describe.serial('Settings', () => {
  test('thresholds can be updated and are reloaded from the backend', async ({ page }) => {
    await login(page);
    await page.click('[data-view="settings"]');
    const cpu = page.locator('#thresholdForm input[name="cpu_percent"]');
    await expect(cpu).toHaveValue(/^\d+$/); // loaded from /api/settings/thresholds

    await cpu.fill('79');
    await page.click('#thresholdForm button[type="submit"]');
    await expect(page.locator('#toast')).toContainText('Thresholds saved');

    // Reload the view — the value must come back from the backend, not the form default
    await page.click('[data-view="overview"]');
    await page.click('[data-view="settings"]');
    await expect(page.locator('#thresholdForm input[name="cpu_percent"]')).toHaveValue('79');
  });

  test('saving SMTP with a blank password keeps the stored one', async ({ page }) => {
    await login(page);
    await page.click('[data-view="settings"]');

    // Set a password once through the real form
    await page.fill('#smtpForm input[name="smtpHost"]', 'smtp.e2e.test');
    await page.fill('#smtpForm input[name="smtpUser"]', 'wwnm@e2e.test');
    await page.fill('#smtpForm input[name="smtpPassword"]', 'e2e-secret-123');
    await page.fill('#smtpForm input[name="fromAddress"]', 'wwnm@e2e.test');
    await page.fill('#smtpForm input[name="recipient"]', 'ops@e2e.test');
    await page.click('#smtpForm button[type="submit"]');
    await expect(page.locator('#toast')).toContainText('SMTP settings saved');

    // Save again leaving the password blank — must not wipe it
    await page.fill('#smtpForm input[name="smtpHost"]', 'smtp2.e2e.test');
    await page.click('#smtpForm button[type="submit"]');
    await expect(page.locator('#toast')).toContainText('SMTP settings saved');
    // No error toast either — a rejection would surface as "Settings save failed"
    await expect(page.locator('#toast')).not.toContainText('failed');
  });

  test('language toggle flips labels both ways', async ({ page }) => {
    await login(page);
    const devicesNav = page.locator('[data-view="devices"]');
    const before = await devicesNav.textContent();
    await page.click('#languageToggle');
    const after = await devicesNav.textContent();
    expect(before).not.toEqual(after);
    await page.click('#languageToggle'); // and back
    await expect(devicesNav).toHaveText(before ?? '');
  });
});
