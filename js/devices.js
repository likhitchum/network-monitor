import {escapeHtml,safeClass,deviceStatus} from './utils.js';
import {apiFetch} from './api.js';
import {showToast} from './toast.js';

const statusLabels={online:'Online',warning:'Warning',critical:'Critical',unknown:'Unknown',paused:'Paused'};
let devices=[];
const table=document.getElementById('deviceTable');
function deviceView(device){return {...device,type:device.monitor_type==='http'||device.monitor_type==='https'?'server':device.monitor_type==='snmp'?'switch':'router',status:deviceStatus(device),ip:device.host,response:device.last_response_ms==null?'—':`${device.last_response_ms} ms`,trend:'M12 16 C25 14 30 17 40 10 S57 13 72 5'} }
function sensorSummary(device){const sensors=device.sensors||{};const values=[];if(sensors.cpu_percent!=null)values.push(`CPU ${sensors.cpu_percent}%`);if(sensors.memory_percent!=null)values.push(`RAM ${sensors.memory_percent}%`);if(sensors.disk_percent!=null)values.push(`Disk ${sensors.disk_percent}%`);return values.join(' · ')}
function filteredDevices(){
  const search=document.getElementById('globalSearch');
  const filter=document.getElementById('statusFilter');
  const query=(search?.value||'').trim().toLowerCase();
  const state=filter?.dataset.state||'all';
  let list=devices;
  if(state!=='all')list=list.filter(device=>deviceStatus(device)===state);
  if(query)list=list.filter(device=>`${device.name} ${device.host} ${device.monitor_type}`.toLowerCase().includes(query));
  return list;
}
function renderDevices(list){
  table.innerHTML=(list??filteredDevices()).map(raw=>{const d=deviceView(raw);return `<tr data-device-id="${escapeHtml(d.id)}"><td><div class="device-cell"><span class="device-icon ${d.type}">${d.type==='server'?'▣':d.type==='router'?'⌁':'▤'}</span><span>${escapeHtml(d.name)}<small>${escapeHtml(d.monitor_type.toUpperCase())} monitor</small></span></div></td><td><span class="status-badge ${d.status}">${statusLabels[d.status]}</span></td><td>${escapeHtml(d.ip)}</td><td class="response">${escapeHtml(d.response)} <span>${escapeHtml(sensorSummary(raw)||'avg.')}</span></td><td><svg class="sparkline ${d.status==='warning'?'warning-line':''}" viewBox="0 0 84 22"><path d="${d.trend}"/></svg></td><td><button class="more-btn device-check" data-device-id="${escapeHtml(d.id)}" title="Check now">↻</button></td></tr>`}).join('');
}
function syncDeviceTable(){const viewTable=document.getElementById('viewDeviceTable');if(viewTable)viewTable.innerHTML=table.innerHTML}
async function checkDevice(deviceId, button){
  button?.setAttribute('disabled','disabled');
  try{const result=await apiFetch(`/api/devices/${deviceId}/check`,{method:'POST'});await loadDevicesFromApi();showToast('Device checked',`${result.status} · ${result.response_ms ?? '—'} ms`)}catch(error){showToast('Check failed',error.message)}finally{button?.removeAttribute('disabled')}
}
async function loadDevicesFromApi(){
  const result=await apiFetch('/api/devices');
  devices=result;renderDevices();syncDeviceTable();
  return result;
}
function updateDevicesViewSummary(summary){const set=(id,value)=>{const element=document.getElementById(id);if(element)element.textContent=value};set('devicesTotal',summary?.total_devices??devices.length);set('devicesOnline',summary?.online??devices.filter(device=>deviceStatus(device)==='online').length);set('devicesAlerts',summary?.active_alerts??0)}
function prependDevice(device){devices.unshift(device);renderDevices();syncDeviceTable()}
table.addEventListener('click',event=>{const button=event.target.closest('.device-check');if(button)checkDevice(Number(button.dataset.deviceId),button)});
export {devices,table,statusLabels,renderDevices,checkDevice,loadDevicesFromApi,updateDevicesViewSummary,prependDevice};
