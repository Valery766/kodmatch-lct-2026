import { request } from './api';
type BridgeWindow = Window & { WebApp?: { initData?: string; ready?: () => void } };
let launchPromise:Promise<unknown>|null=null;
export function launchMessenger(){return launchPromise??=(launch());}
async function launch(){
  if (!location.pathname.startsWith('/mini/max')) return null;
  const platform = 'max';
  const src = 'https://st.max.ru/js/max-web-app.js';
  await new Promise<void>((resolve,reject) => { const script = document.createElement('script'); script.src=src;script.async=true;const fail=()=>reject(new Error('Не удалось загрузить интерфейс мессенджера. Откройте обычный web-кабинет.'));const timer=setTimeout(fail,10000);script.onload=()=>{clearTimeout(timer);resolve();};script.onerror=()=>{clearTimeout(timer);fail();};document.head.appendChild(script); });
  const bridge = (window as BridgeWindow).WebApp; bridge?.ready?.();
  if (!bridge?.initData) throw new Error('Подписанные данные запуска отсутствуют. Откройте кабинет кнопкой нового бота или войдите по email.');
  return request('/integrations/messenger/launch','POST',{platform,init_data:bridge.initData});
}
