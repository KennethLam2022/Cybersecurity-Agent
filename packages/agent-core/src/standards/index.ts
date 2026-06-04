import type { StandardEntry } from '@cybersec/shared/types.js'
import { djbhStandards } from './djbh-data.js'
import { dataSecurityStandards } from './data-security-data.js'

const allStandards: StandardEntry[] = [...djbhStandards, ...dataSecurityStandards]

export function searchStandards(query: string): StandardEntry[] {
  const q = query.toLowerCase()
  return allStandards.filter(
    (s) =>
      s.title.toLowerCase().includes(q) ||
      s.content.toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q)) ||
      s.standardNo.toLowerCase().includes(q)
  )
}

export function getStandardById(id: string): StandardEntry | undefined {
  return allStandards.find((s) => s.id === id)
}

export { allStandards }