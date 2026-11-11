import { describe, expect, it } from 'vitest'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc.js'

import {
  channelDisplayToUnix,
  formatChannelDateTime,
  getAccountGuideClock,
  getGuideOffsetMinutes,
  isGuideTimeZone,
  normalizeGuideClock,
  utcToChannelInput,
} from './channelTime'

dayjs.extend(utc)

// 2026-11-10 17:55 UTC: winter, Rome is UTC+1.
const WINTER = { start_timestamp: Date.UTC(2026, 10, 10, 17, 55) / 1000 }
// 2026-07-10 17:55 UTC: summer, Rome is UTC+2.
const SUMMER = { start_timestamp: Date.UTC(2026, 6, 10, 17, 55) / 1000 }

describe('guide time zones', () => {
  it('follows daylight saving per programme', () => {
    expect(formatChannelDateTime(WINTER, 'start', 'Europe/Rome', 'HH:mm')).toBe('18:55')
    expect(formatChannelDateTime(SUMMER, 'start', 'Europe/Rome', 'HH:mm')).toBe('19:55')
    expect(formatChannelDateTime(WINTER, 'start', 'Asia/Jerusalem', 'HH:mm')).toBe('19:55')
  })

  it('keeps fixed hour offsets working as before', () => {
    expect(formatChannelDateTime(WINTER, 'start', 2, 'HH:mm')).toBe('19:55')
    expect(formatChannelDateTime(WINTER, 'start', 0, 'HH:mm')).toBe('17:55')
  })

  it('round-trips typed wall-clock times across a DST change', () => {
    expect(channelDisplayToUnix('2026-11-10T18:55', 'Europe/Rome')).toBe(WINTER.start_timestamp)
    expect(channelDisplayToUnix('2026-07-10T19:55', 'Europe/Rome')).toBe(SUMMER.start_timestamp)
    expect(utcToChannelInput(dayjs.unix(SUMMER.start_timestamp), 'Europe/Rome')).toBe('2026-07-10T19:55')
  })

  it('reports offsets in minutes, including half-hour zones', () => {
    expect(getGuideOffsetMinutes('Asia/Kolkata', dayjs.unix(WINTER.start_timestamp))).toBe(330)
  })

  it('prefers the account zone over its hour offset, and ignores junk', () => {
    expect(getAccountGuideClock({ guide_timezone: 'Europe/Rome', guide_offset_hours: 3 })).toBe('Europe/Rome')
    expect(getAccountGuideClock({ guide_timezone: 'Nowhere/Land', guide_offset_hours: 3 })).toBe(3)
    expect(isGuideTimeZone('2')).toBe(false)
    expect(normalizeGuideClock('bogus')).toBe(0)
  })
})
