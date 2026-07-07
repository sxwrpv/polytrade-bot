import { useState } from 'react'
import { api } from '../api'

const ADDR = /^0x[0-9a-fA-F]{40}$/

export default function CopyWalletForm({ defaults, onAdded }) {
  const [addr, setAddr] = useState('')
  const [ratio, setRatio] = useState(1)
  const [maxPos, setMaxPos] = useState(defaults?.default_max_position_usd ?? 15)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  async function add() {
    const a = addr.trim()
    if (!ADDR.test(a)) {
      setMsg('INVALID ADDRESS (0x + 40 HEX)')
      return
    }
    setBusy(true)
    setMsg('')
    try {
      await api.follow(a, { copy_ratio_pct: Number(ratio), max_position_usd: Number(maxPos) })
      setAddr('')
      setMsg('COPYING ✓')
      onAdded?.()
    } catch (e) {
      setMsg(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <input placeholder="0x… trader wallet to copy" value={addr} onChange={(e) => setAddr(e.target.value)} />
      <div className="pc-row" style={{ marginTop: 10 }}>
        <label className="fld" style={{ flex: 1, margin: 0 }}>
          RATIO % (of leader)
          <input value={ratio} onChange={(e) => setRatio(e.target.value)} />
        </label>
        <label className="fld" style={{ flex: 1, margin: 0 }}>
          MAX / TRADE (pUSD)
          <input value={maxPos} onChange={(e) => setMaxPos(e.target.value)} />
        </label>
      </div>
      <button className="btn" style={{ marginTop: 10 }} disabled={busy} onClick={add}>
        {busy ? 'ADDING…' : 'COPY WALLET'}
      </button>
      {msg && <div className="muted" style={{ marginTop: 8 }}>{msg}</div>}
    </div>
  )
}
