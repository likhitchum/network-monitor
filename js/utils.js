export const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[character]));
export const safeClass=(value,allowed,fallback)=>allowed.includes(value)?value:fallback;
export function deviceStatus(device){return device.enabled===0?'paused':device.status==='up'?'online':device.status==='warning'?'warning':device.status==='down'?'critical':'unknown'}
