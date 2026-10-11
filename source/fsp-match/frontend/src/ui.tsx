import { useEffect, useId, useState, type ReactNode } from 'react';
import { request, specs, grades, workFormats, type Row } from './api';
export function Icon({ name = 'code', size = 20 }: { name?: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    code: <><path d="m8 8-4 4 4 4m8-8 4 4-4 4m-3-11-2 14" /></>,
    home: <><path d="m3 10 9-7 9 7v10H3Z" /><path d="M9 20v-7h6v7" /></>,
    user: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" /><path d="m8 11 3 3 5-5" /></>,
    search: <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6" /></>,
    mail: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 6 9 7 9-7" /></>,
    bell: <><path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5Zm4 4h6" /></>,
    briefcase: <><rect x="3" y="7" width="18" height="14" rx="2" /><path d="M8 7V3h8v4M3 12h18m-9-2v4" /></>,
    file: <><path d="M5 3h9l5 5v13H5Zm9 0v6h5M8 13h8m-8 4h5" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    plus: <path d="M12 4v16M4 12h16" />,
    settings: <><path d="M4 6h16M4 12h16M4 18h16" /><circle cx="9" cy="6" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="8" cy="18" r="2" /></>,
    bookmark: <path d="M6 3h12v18l-6-4-6 4Z" />,
    logout: <><path d="M10 4H4v16h6m-1-8h12m-4-4 4 4-4 4" /></>,
    close: <path d="m5 5 14 14M19 5 5 19" />,
    spark: <><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z" /></>,
    menu: <path d="M3 6h18M3 12h18M3 18h18" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.code}</svg>;
}
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  const id = useId();
  return <div className="field"><label id={id}>{label}</label><div aria-labelledby={id}>{children}</div>{hint && <small>{hint}</small>}</div>;
}
export function Banner({ children, kind = 'info' }: { children: ReactNode; kind?: 'info' | 'error' | 'success' | 'warning' }) { return <div className={`banner ${kind}`} role={kind === 'error' ? 'alert' : 'status'}><Icon name={kind === 'success' ? 'check' : 'shield'} /><div>{children}</div></div>; }
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function PageHead({ eyebrow, title, text, action }: { eyebrow?: string; title: string; text?: string; action?: ReactNode }) { return <header className="page-head"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h1>{title}</h1>{text && <p className="muted">{text}</p>}</div>{action}</header>; }
export function Empty({ title, text, action }: { title: string; text?: string; action?: ReactNode }) { return <div className="empty"><span className="empty-icon"><Icon name="search" size={28} /></span><h3>{title}</h3>{text && <p>{text}</p>}{action}</div>; }
export function Loading() { return <div className="loading" role="status"><span className="spinner" /> Загружаем данные…</div>; }
export function useData(path: string | null) {
  const [data, setData] = useState<Row | null>(null); const [error, setError] = useState(''); const [loading, setLoading] = useState(Boolean(path)); const [version, setVersion] = useState(0);
  useEffect(() => { let active = true; setError(''); if (!path) { setLoading(false); setData(null); return; } setLoading(true); request(path).then(v => { if (active) setData(v); }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); }); return () => { active = false; }; }, [path, version]);
  return { data, error, loading, reload: () => setVersion(v => v + 1), setData };
}
export function DataState({ state, children }: { state: ReturnType<typeof useData>; children: ReactNode }) { return state.loading ? <Loading /> : state.error ? <Banner kind="error">{state.error} <button className="text-button" onClick={state.reload}>Повторить</button></Banner> : <>{children}</>; }
// Presentation only: stored text and editable values keep their original punctuation.
export const displayText = (value: unknown) => String(value ?? '').replace(/\u2014/g, '–');
export const split = (v: string) => [...new Set(v.split(/[,;\n]/).map(s => s.trim()).filter(Boolean))];
export const stringify = (v: unknown) => typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v, null, 2);
export const profileOf = (v: Row | null) => v?.profile ?? v ?? {};
export function ParsedFieldsPreview({fields}:{fields:Row|undefined|null}) {
  const labels:Record<string,string>={full_name:'Имя и фамилия',headline:'Профессиональный заголовок',bio:'О себе',title:'Название потребности',description:'Описание потребности',specialization:'Специализация',declared_grade:'Предполагаемый уровень',grade:'Требуемый уровень',skills:'Заявленные навыки',roles:'Профессиональные роли',experience_years:'Стаж, лет',soft_skills:'Soft skills',required_skills:'Обязательные навыки',preferred_skills:'Желательные навыки',location:'Город',work_format:'Формат работы'};
  const proposed=fields??{};
  const rows=Object.keys(labels).filter(key=>proposed[key]!==null&&proposed[key]!==undefined&&proposed[key]!==''&&(!Array.isArray(proposed[key])||proposed[key].length));
  const value=(key:string)=>{
    const raw=proposed[key];
    if(Array.isArray(raw))return <div className="tags">{raw.map((v,i)=><Badge key={i}>{['roles','soft_skills'].includes(key)?displayText(v):String(v)}</Badge>)}</div>;
    const rawText=key==='specialization'?(specs[raw]??String(raw)):['grade','declared_grade'].includes(key)?(grades[raw]??String(raw)):key==='work_format'?(workFormats[raw]??String(raw)):key==='experience_years'&&typeof raw==='number'?raw.toLocaleString('ru-RU'):String(raw);
    const text=['headline','bio','title','description','roles','soft_skills'].includes(key)?displayText(rawText):rawText;
    return ['bio','description'].includes(key)&&text.length>220?<><p>{text.slice(0,180).trimEnd()}…</p><details><summary>{labels[key]} – показать полный текст</summary><p className="pre-wrap">{text}</p></details></>:<span className="pre-wrap">{text}</span>;
  };
  const salary=(key:string)=>typeof proposed[key]==='number'&&Number.isFinite(proposed[key])?proposed[key].toLocaleString('ru-RU'):'не указано';
  const hasSalary=proposed.salary_min!=null||proposed.salary_max!=null;
  return rows.length||hasSalary?<dl className="parsed-fields" aria-label="Распознанные поля для проверки">{rows.map(key=><div className="parsed-field" key={key}><dt>{labels[key]}</dt><dd>{value(key)}</dd></div>)}{hasSalary&&<div className="parsed-field"><dt>Вилка, ₽ в месяц до вычета НДФЛ</dt><dd>От {salary('salary_min')} до {salary('salary_max')}</dd></div>}</dl>:<p className="muted">Поля не распознаны. Заполните их вручную.</p>;
}
export function useDialogAccessibility() {
  useEffect(() => {
    const bound = new Map<HTMLElement, { listener: (e: KeyboardEvent) => void; previous: HTMLElement | null }>();
    const sync = () => {
      for (const [dialog, state] of bound) if (!dialog.isConnected || dialog.getAttribute('role')!=='dialog') { dialog.removeEventListener('keydown', state.listener); bound.delete(dialog); state.previous?.focus(); }
      document.querySelectorAll<HTMLElement>('[role="dialog"]').forEach(dialog => {
        if (bound.has(dialog)) return;
        const previous = document.activeElement as HTMLElement | null;
        const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex="0"]'));
        const listener = (e: KeyboardEvent) => { if (e.key === 'Escape') dialog.querySelector<HTMLButtonElement>('button[aria-label^="Закрыть"]')?.click(); if (e.key !== 'Tab') return; const nodes=focusable();if(!nodes.length)return; const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();} };
        dialog.addEventListener('keydown',listener);bound.set(dialog,{listener,previous});queueMicrotask(()=>{if(!dialog.contains(document.activeElement))focusable()[0]?.focus();});
      });
    };
    const observer=new MutationObserver(sync);observer.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['role']});sync();
    return()=>{observer.disconnect();for(const [d,s] of bound)d.removeEventListener('keydown',s.listener);};
  },[]);
}

export function PdfLink({path,children}:{path:string;children:ReactNode}){
  const[error,setError]=useState('');const[busy,setBusy]=useState(false);
  return <><a className="button secondary" href={path} target="_blank" rel="noreferrer" aria-disabled={busy} onClick={e=>{
    if(!location.pathname.startsWith('/mini/max'))return;
    e.preventDefault();if(busy)return;const max=(window as Window&{WebApp?:{downloadFile?:(url:string,name:string)=>Promise<unknown>}}).WebApp;
    if(!max?.downloadFile){setError('Скачивание PDF недоступно в этом клиенте MAX. Откройте web-кабинет в обычном браузере.');return;}
    setError('');setBusy(true);Promise.resolve(max.downloadFile(new URL(path,location.origin).href,'codematch-profile.pdf')).then(r=>{if(r&&typeof r==='object'&&'error' in r)throw Error('MAX не завершил скачивание. Откройте web-кабинет в обычном браузере.');}).catch(()=>setError('Не удалось скачать PDF через MAX. Откройте web-кабинет в обычном браузере.')).finally(()=>setBusy(false));
  }}><Icon name="file"/>{busy?'Скачиваем…':children}</a>{error&&<Banner kind="error">{error}<br/><button className="text-button" onClick={()=>{const max=(window as Window&{WebApp?:{openLink?:(url:string)=>void}}).WebApp;const url=new URL('/',location.origin).href;if(max?.openLink)max.openLink(url);else location.assign(url);}}>Открыть web-кабинет</button></Banner>}</>;
}
