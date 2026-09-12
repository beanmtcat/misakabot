export function Toolbar({ query, onQuery, filter, onFilter, filterOptions, onRefresh, onSync, syncLabel, searchPlaceholder, syncing = false }) {
  return <div className="toolbar">
    <label className="search-field"><span>⌕</span><input value={query} onChange={(event) => onQuery(event.target.value)} placeholder={searchPlaceholder || '搜索'} /></label>
    {filterOptions && <select value={filter} onChange={(event) => onFilter(event.target.value)}>
      {filterOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select>}
    <button className="secondary icon-button" onClick={onRefresh} title="刷新当前列表">↻ <span>刷新</span></button>
    {onSync && <button className={syncing ? 'sync-button syncing' : 'sync-button'} onClick={onSync} disabled={syncing} aria-busy={syncing}>{syncing ? '同步中…' : syncLabel}<span>{syncing ? '↻' : '→'}</span></button>}
  </div>;
}

export function DataTable({ headers, children, empty, loading }) {
  return <div className="table-card"><div className="table-wrap"><table><thead><tr>
    {headers.map((header) => <th key={header}>{header}</th>)}
  </tr></thead><tbody>{children}</tbody></table>
  </div>{(empty || loading) && <p className="empty">{loading ? '正在获取最新数据…' : '这里还没有符合条件的数据。'}</p>}</div>;
}
