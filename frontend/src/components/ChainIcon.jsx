// Simplified, abstracted geometric marks (not literal brand logos) — sharp
// lines, no fills/gradients, inherit color via currentColor so they match the
// chip's active/inactive state automatically.
const ICONS = {
  evm: (
    <svg viewBox="0 0 20 20" width="16" height="16">
      <polygon points="10,1 18,10 10,19 2,10" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <line x1="10" y1="1" x2="10" y2="19" stroke="currentColor" strokeWidth="1" />
    </svg>
  ),
  svm: (
    <svg viewBox="0 0 20 20" width="16" height="16">
      <line x1="2" y1="5" x2="18" y2="5" stroke="currentColor" strokeWidth="1.6" />
      <line x1="4" y1="10" x2="20" y2="10" stroke="currentColor" strokeWidth="1.6" transform="translate(-2,0)" />
      <line x1="2" y1="15" x2="18" y2="15" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  ),
  tron: (
    <svg viewBox="0 0 20 20" width="16" height="16">
      <polygon points="10,2 18,7 15,18 5,18 2,7" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  ),
  btc: (
    <svg viewBox="0 0 20 20" width="16" height="16">
      <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <text x="10" y="14" textAnchor="middle" fontSize="10" fill="currentColor" fontFamily="inherit">
        ₿
      </text>
    </svg>
  ),
}

export default function ChainIcon({ chain }) {
  return ICONS[chain] || null
}
