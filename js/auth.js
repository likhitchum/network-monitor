import {apiFetch,clearSession,saveSession} from './api.js';
import {loadDevicesFromApi} from './devices.js';
import {loadSummary} from './summary.js';
import {loadDashboardAlerts} from './alerts.js';

const demoAccounts={'admin@example.com':'admin123','user@example.com':'user123'};
const loginScreen=document.getElementById('loginScreen');
const appShell=document.querySelector('.app-shell');
const loginForm=document.getElementById('loginForm');
const loginError=document.getElementById('loginError');
function accountFromUser(user,email){const name=user.name||email.split('@')[0];return {name,role:user.role,label:user.role==='admin'?'Administrator':'Monitoring user',initials:name.split(' ').map(part=>part[0]).join('').slice(0,2).toUpperCase()}}
function applyRole(account){
  appShell.classList.remove('is-hidden');
  loginScreen.classList.add('is-hidden');
  document.getElementById('currentUserName').textContent=account.name;
  document.getElementById('currentUserRole').textContent=account.label;
  document.getElementById('topUserAvatar').textContent=account.initials;
  document.getElementById('roleBadge').textContent=account.role.toUpperCase();
  document.getElementById('addDeviceBtn').style.display=account.role==='admin'?'inline-flex':'none';
  document.querySelectorAll('[data-view="reports"],[data-view="settings"]').forEach(item=>item.style.display=account.role==='admin'?'flex':'none');
  document.body.dataset.role=account.role;
  loadDevicesFromApi().catch(error=>showToast('Unable to load devices',error.message));
  loadSummary();
  loadDashboardAlerts();
}
function signOut(){
  clearSession();
  appShell.classList.add('is-hidden');
  loginScreen.classList.remove('is-hidden');
  loginForm.reset();
  loginError.textContent='';
}
loginForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const email=document.getElementById('loginEmail').value.trim().toLowerCase();
  const password=document.getElementById('loginPassword').value;
  loginError.textContent='';
  try{
    const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.detail||'Invalid email or password');
    saveSession({email,role:result.user.role,token:result.access_token});
    applyRole(accountFromUser(result.user,email));
    showToast(`Welcome, ${result.user.name||email}`,`${result.user.role==='admin'?'Administrator':'Monitoring user'} access enabled`);
  }catch(error){loginError.textContent=error.message||'Cannot connect to NetworkPulse API'}
});
document.querySelectorAll('[data-demo-role]').forEach(button=>button.addEventListener('click',()=>{
  const email=button.dataset.demoRole==='admin'?'admin@example.com':'user@example.com';
  document.getElementById('loginEmail').value=email;
  document.getElementById('loginPassword').value=demoAccounts[email];
  loginError.textContent='';
  loginForm.requestSubmit();
}));
document.getElementById('forgotPassword').addEventListener('click',event=>{event.preventDefault();loginError.textContent='Demo mode: use one of the accounts below'});
document.getElementById('logoutBtn').addEventListener('click',signOut);
(async()=>{
  const existingSession=JSON.parse(localStorage.getItem('networkpulse-session')||'null');
  if(existingSession&&existingSession.token){
    try{const me=await apiFetch('/api/me');applyRole(accountFromUser(me,existingSession.email))}
    catch(error){clearSession()}
  }else clearSession();
})();
