// The exact-value field of a setting slider holds the text as it is typed, so
// '1.' and '0.0' stay on screen on the way to 1.5 and 0.05. The parent only
// ever receives a finite number, or '' while the text is no number yet.

/** The number the typed text stands for: finite, or '' when there is none.
 *  A single comma counts as the decimal point, the way some keypads type it. */
export function parseDraft(text) {
  const raw = String(text ?? '').trim()
  if (raw === '') return ''
  const num = Number(raw.includes('.') ? raw : raw.replace(',', '.'))
  return Number.isFinite(num) ? num : ''
}

/** The text the field shows. The draft stays while it stands for the value the
 *  parent holds; a value changed elsewhere (a slider drag, a reload) shows as
 *  itself. */
export function draftText(draft, value) {
  const held = value == null || value === '' ? '' : Number(value)
  if (draft != null && parseDraft(draft) === held) return draft
  return String(value ?? '')
}
