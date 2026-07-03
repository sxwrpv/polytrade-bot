export default function Modal({ title, children, onClose, accent = 'green' }) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={`modal ${accent === 'red' ? 'modal-red' : ''}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{title}</div>
        {children}
        <button className="modal-x" onClick={onClose}>[ CLOSE ]</button>
      </div>
    </div>
  )
}
