/** Official marks are served byte-for-byte from the supplied FSP brand assets. */
export function FSPBrand({ compact = false, inverse = false }: { compact?: boolean; inverse?: boolean }) {
  const theme = inverse ? 'dark' : 'light';
  const file = compact ? `fsp-short-${theme}.svg` : `fsp-lockup-${theme}.svg`;
  return <aside className={`fsp-context${compact ? ' compact' : ''}${inverse ? ' inverse' : ''}`} aria-label="Специальный трек Федерации спортивного программирования">
    <div className="fsp-logo-safe-area">
      <img src={`/brand/fsp/${file}`} alt="Федерация спортивного программирования России" width={compact ? 120 : 360} height={compact ? 40 : 45} />
    </div>
    <div className="fsp-context-copy">
      <strong>Специальный трек ЛЦТ 2026</strong>
      {!compact && <span>Решение команды unecon.tech</span>}
    </div>
  </aside>;
}
