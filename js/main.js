import {showToast} from './toast.js';
import {apiFetch} from './api.js';
import {renderDevices,loadDevicesFromApi,prependDevice} from './devices.js';
import {loadSummary} from './summary.js';
import {loadDashboardAlerts} from './alerts.js';
import {initViews,openView} from './views.js';
import {initLanguage} from './i18n.js';
import './auth.js';

initLanguage(showToast);
initViews(showToast);
document.getElementById('refreshBtn').addEventListener('click',()=>{const btn=document.querySelector('.refresh-icon');btn.style.transform='rotate(360deg)';setTimeout(()=>btn.style.transform='',500);document.getElementById('lastUpdated').textContent='Updated just now';document.getElementById('syncTime').textContent='just now';loadDevicesFromApi().catch(error=>showToast('Unable to load devices',error.message));loadSummary();loadDashboardAlerts();showToast()});
document.getElementById('globalSearch').addEventListener('input',()=>renderDevices());
document.getElementById('statusFilter').addEventListener('click',()=>{const filter=document.getElementById('statusFilter');const states=['all','online','warning','critical'];const next=states[(states.indexOf(filter.dataset.state||'all')+1)%states.length];filter.dataset.state=next;filter.textContent={all:'Status: All⌄',online:'Status: Online⌄',warning:'Status: Warning⌄',critical:'Status: Critical⌄'}[next];renderDevices()});
const modal=document.getElementById('modal');
function closeModal(){modal.classList.remove('open')}
document.getElementById('addDeviceBtn').addEventListener('click',()=>modal.classList.add('open'));
document.getElementById('closeModal').addEventListener('click',closeModal);
document.getElementById('cancelModal').addEventListener('click',closeModal);
modal.addEventListener('click',e=>{if(e.target===modal)closeModal()});
document.getElementById('deviceForm').addEventListener('submit',async e=>{e.preventDefault();const data=new FormData(e.target);const typeMap={'Ping only':'ping','VPN gateway':'vpn','SNMP v2c':'snmp','HTTP / HTTPS':'https'};try{const result=await apiFetch('/api/devices',{method:'POST',body:JSON.stringify({name:data.get('name').trim(),host:data.get('ip').trim(),monitor_type:typeMap[data.get('type')],interval_seconds:Number(data.get('interval')||60),snmp_community:data.get('snmpCommunity')?.trim()||'public',snmp_port:Number(data.get('snmpPort')||161)})});prependDevice(result);closeModal();e.target.reset();showToast('Device added successfully','Monitoring has started for the new device')}catch(error){showToast('Unable to add device',error.message)}});
