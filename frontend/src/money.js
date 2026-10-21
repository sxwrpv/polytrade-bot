// Dollar amounts with cents and grouped thousands, so 1846.5 prints as
// "1,846.50" rather than "1846.50". The "$" and any sign stay with the caller.
export const dollars = (v) => Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
