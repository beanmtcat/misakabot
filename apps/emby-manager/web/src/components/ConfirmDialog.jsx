import { createContext, useContext, useEffect, useState } from 'react';
import './ConfirmDialog.css';

const ConfirmContext = createContext(null);

export function ConfirmProvider({ children }) {
  const [dialog, setDialog] = useState(null);

  function confirm(options) {
    return new Promise((resolve) => setDialog({
      title: '确认操作',
      description: '',
      confirmLabel: '确认',
      ...options,
      resolve,
    }));
  }

  function close(accepted) {
    dialog?.resolve(accepted);
    setDialog(null);
  }

  useEffect(() => {
    if (!dialog) return undefined;
    const onKeyDown = (event) => {
      if (event.key === 'Escape') close(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [dialog]);

  return <ConfirmContext.Provider value={confirm}>
    {children}
    {dialog && <div className="confirm-backdrop" role="presentation" onMouseDown={() => close(false)}>
      <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title" onMouseDown={(event) => event.stopPropagation()}>
        <p className="confirm-kicker">需要确认</p><h2 id="confirm-title">{dialog.title}</h2><p>{dialog.description}</p>
        <footer><button className="secondary" onClick={() => close(false)}>取消</button><button onClick={() => close(true)} autoFocus>{dialog.confirmLabel}</button></footer>
      </section>
    </div>}
  </ConfirmContext.Provider>;
}

export function useConfirm() {
  const confirm = useContext(ConfirmContext);
  if (!confirm) throw new Error('useConfirm must be used inside ConfirmProvider');
  return confirm;
}
