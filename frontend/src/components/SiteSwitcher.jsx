const DESTINATIONS = [
  ['home', '/', 'Home'],
  ['screener', '/screener', 'Screener'],
  ['docs', '/docs', 'Docs'],
]

export default function SiteSwitcher({ active }) {
  return (
    <nav className="site-switcher" aria-label="PolyTrade sites">
      {DESTINATIONS.map(([key, href, label]) => (
        <a
          key={key}
          href={href}
          aria-current={active === key ? 'page' : undefined}
        >{label}</a>
      ))}
    </nav>
  )
}
