/**
 * 常见问题折叠区。
 */
import { useState } from 'react'
import { FAQS } from './features'

export default function FaqSection() {
  const [open, setOpen] = useState<number | null>(null)

  return (
    <div className="gd-faq">
      {FAQS.map((f, i) => (
        <div key={i} className={`gd-faq-item ${open === i ? 'gd-faq-open' : ''}`}>
          <button
            className="gd-faq-q"
            aria-expanded={open === i}
            onClick={() => setOpen(open === i ? null : i)}
          >
            <span>{f.q}</span>
            <span className={`gd-card-chevron ${open === i ? 'gd-chevron-up' : ''}`}>▾</span>
          </button>
          {open === i && <div className="gd-faq-a">{f.a}</div>}
        </div>
      ))}
    </div>
  )
}
