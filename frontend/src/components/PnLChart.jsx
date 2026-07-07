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

export default function PnLChart({ data }) {
  const canvas = useRef(null)
  const chart = useRef(null)

  useEffect(() => {
    if (!canvas.current || data.length === 0) return
    if (chart.current) chart.current.destroy()

    const values = data.map((d) => d.cumulative_pnl)
    const final = values[values.length - 1] ?? 0
    // color the line by whether the trader is up or down over the window —
    // green when in profit, red when underwater (matches the app's pos/neg cue)
    const up = final >= 0
    const line = up ? '#0b9e63' : '#d64545'
    const fill = up ? 'rgba(11, 158, 99, 0.08)' : 'rgba(214, 69, 69, 0.08)'
    // a single closed trade is one point — a 0-radius line draws nothing, so
    // show dots when the series is short (and always mark the latest point).
    const dot = data.length <= 45 ? 3 : 0
    // decimals for the y-axis: on a narrow $ range, integer ticks collapse into
    // duplicate labels (-$2, -$2, -$3…) — scale precision to the span.
    const span = Math.max(...values) - Math.min(...values)
    const yDec = span < 5 ? 2 : span < 50 ? 1 : 0

    chart.current = new Chart(canvas.current, {
      type: 'line',
      data: {
        labels: data.map((d) => d.date),
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
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
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
              label: (c) => `${c.parsed.y >= 0 ? '+' : '-'}$${Math.abs(c.parsed.y).toFixed(2)}`,
            },
          },
        },
        scales: {
          x: { grid: { color: 'rgba(20, 32, 26, 0.07)' }, ticks: { color: '#5c6b62', font: MONO, maxRotation: 0, autoSkipPadding: 16 } },
          y: {
            grid: { color: 'rgba(20, 32, 26, 0.07)' },
            ticks: {
              color: '#5c6b62', font: MONO, maxTicksLimit: 6,
              callback: (v) => `${v >= 0 ? '' : '-'}$${Math.abs(v).toFixed(yDec)}`,
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
  }, [data])

  return (
    <div className="chart-box">
      {data.length === 0 ? (
        <div className="muted">no closed trades yet — your realized PnL curve appears here after your first exit</div>
      ) : (
        <canvas ref={canvas} />
      )}
    </div>
  )
}
