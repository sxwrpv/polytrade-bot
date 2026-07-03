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
    chart.current = new Chart(canvas.current, {
      type: 'line',
      data: {
        labels: data.map((d) => d.date),
        datasets: [
          {
            data: data.map((d) => d.cumulative_pnl),
            borderColor: '#0b9e63',
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            backgroundColor: 'rgba(11, 158, 99, 0.08)',
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
            borderColor: '#0b9e63',
            borderWidth: 1,
            titleColor: '#5c6b62',
            bodyColor: '#14201a',
            titleFont: MONO,
            bodyFont: MONO,
          },
        },
        scales: {
          x: { grid: { color: 'rgba(20, 32, 26, 0.07)' }, ticks: { color: '#5c6b62', font: MONO } },
          y: { grid: { color: 'rgba(20, 32, 26, 0.07)' }, ticks: { color: '#5c6b62', font: MONO } },
        },
      },
    })
    return () => {
      if (chart.current) chart.current.destroy()
    }
  }, [data])

  return (
    <div className="chart-box">
      {data.length === 0 ? <div className="muted">no pnl history yet</div> : <canvas ref={canvas} />}
    </div>
  )
}
