import { useEffect, useRef } from 'react'
import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Filler,
  Tooltip,
} from 'chart.js'

Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Filler, Tooltip)

const MONO = { family: 'JetBrains Mono', size: 10 }

// short "MM-DD HH:MM" label from an ISO timestamp (falls back to a raw date)
function label(t) {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return String(t)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// Generic time-series line chart for the Performance tab.
//   data: [{ t: isoString, y: number }]
//   kind: 'equity' (account value — up = rose over the window)
//       | 'pnl'    (profit/loss — up = positive)
export default function PnLChart({ data, kind = 'pnl' }) {
  const canvas = useRef(null)
  const chart = useRef(null)

  useEffect(() => {
    if (!canvas.current || !data || data.length === 0) return
    if (chart.current) chart.current.destroy()

    const values = data.map((d) => d.y)
    const first = values[0] ?? 0
    const final = values[values.length - 1] ?? 0
    // equity is always positive, so color by direction over the window; pnl by sign
    const up = kind === 'equity' ? final >= first : final >= 0
    const line = up ? '#0b9e63' : '#d64545'
    const fill = up ? 'rgba(11, 158, 99, 0.08)' : 'rgba(214, 69, 69, 0.08)'
    // a 0-radius line through one point draws nothing — show dots when sparse
    const dot = data.length <= 45 ? 3 : 0
    const span = Math.max(...values) - Math.min(...values)
    const yDec = span < 5 ? 2 : span < 50 ? 1 : 0
    const money = (v) => `${v < 0 ? '-' : ''}$${Math.abs(v).toFixed(yDec)}`
    const signed = (v) => `${v >= 0 ? '+' : '-'}$${Math.abs(v).toFixed(2)}`

    chart.current = new Chart(canvas.current, {
      type: 'line',
      data: {
        labels: data.map((d) => label(d.t)),
        datasets: [
          {
            data: values,
            borderColor: line,
            borderWidth: 2,
            pointRadius: (ctx) => (ctx.dataIndex === values.length - 1 ? 4 : dot),
            pointBackgroundColor: line,
            pointHoverRadius: 5,
            fill: true,
            backgroundColor: fill,
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#ffffff',
            borderColor: line,
            borderWidth: 1,
            titleColor: '#5c6b62',
            bodyColor: '#14201a',
            titleFont: MONO,
            bodyFont: MONO,
            callbacks: {
              label: (c) => (kind === 'equity' ? money(c.parsed.y) : signed(c.parsed.y)),
            },
          },
        },
        scales: {
          x: {
            grid: { color: 'rgba(20, 32, 26, 0.07)' },
            ticks: { color: '#5c6b62', font: MONO, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
          },
          y: {
            grid: { color: 'rgba(20, 32, 26, 0.07)' },
            ticks: {
              color: '#5c6b62', font: MONO, maxTicksLimit: 6,
              callback: (v) => money(v),
            },
            // pad a flat single-point series so its dot sits mid-box, not on an axis
            ...(new Set(values).size === 1
              ? { min: values[0] - Math.max(1, Math.abs(values[0]) * 0.5),
                  max: values[0] + Math.max(1, Math.abs(values[0]) * 0.5) }
              : {}),
          },
        },
      },
    })
    return () => {
      if (chart.current) chart.current.destroy()
    }
  }, [data, kind])

  return (
    <div className="chart-box">
      {!data || data.length === 0 ? (
        <div className="muted">
          collecting snapshots — your {kind === 'equity' ? 'equity' : 'PnL'} curve fills in as the
          bot records your account every few minutes
        </div>
      ) : (
        <canvas ref={canvas} />
      )}
    </div>
  )
}
