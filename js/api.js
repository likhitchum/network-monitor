const SESSION_KEY='networkpulse-session';
function readSession(){return JSON.parse(localStorage.getItem(SESSION_KEY)||'null')}
export function apiHeaders(){const session=readSession();return {'Content-Type':'application/json',...(session?.token?{Authorization:`Bearer ${session.token}`}:{})}}
export function saveSession(session){localStorage.setItem(SESSION_KEY,JSON.stringify(session))}
export function clearSession(){localStorage.removeItem(SESSION_KEY)}
export async function apiFetch(path, options={}){
  const response=await fetch(path,{...options,headers:{...apiHeaders(),...(options.headers||{})}});
  const data=await response.json().catch(()=>({}));
  if(response.status===401&&!path.includes('/api/auth/login')&&localStorage.getItem(SESSION_KEY)&&document.querySelector('.app-shell:not(.is-hidden)')){clearSession();window.location.reload()}
  if(!response.ok)throw new Error(data.detail||`Request failed (${response.status})`);
  return data;
}
