import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc.js'

dayjs.extend(utc)

function coerceTimestamp(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return null
  }
  return Math.trunc(numeric)
}

function parseIsoUtc(value) {
  if (!value) {
    return null
  }
  const parsed = dayjs.utc(value)
  return parsed.isValid() ? parsed : null
}

// A guide clock is what an account's times are shown on: either a fixed number
// of hours from UTC (the account's guide offset) or an IANA time zone such as
// "Europe/Rome", whose offset follows daylight saving and so is worked out for
// each instant rather than once.

const zoneFormatters = new Map()

function zoneFormatter(timeZone) {
  let formatter = zoneFormatters.get(timeZone)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
    zoneFormatters.set(timeZone, formatter)
  }
  return formatter
}

/** Whether a value names a time zone this browser knows. */
export function isGuideTimeZone(value) {
  if (typeof value !== 'string' || !value.trim() || Number.isFinite(Number(value))) {
    return false
  }
  try {
    zoneFormatter(value.trim())
    return true
  } catch {
    return false
  }
}

/** The clock to show an account on: its time zone when it has a valid one, else its hour offset. */
export function getAccountGuideClock(account) {
  if (isGuideTimeZone(account?.guide_timezone)) {
    return account.guide_timezone.trim()
  }
  return getGuideOffsetHours(account?.guide_offset_hours)
}

/** A clock value made safe to pass on: a valid time zone name as is, anything else as hours. */
export function normalizeGuideClock(value) {
  return isGuideTimeZone(value) ? value.trim() : getGuideOffsetHours(value)
}

function zoneOffsetMinutes(timeZone, instant) {
  const date = instant.toDate()
  const parts = {}
  for (const part of zoneFormatter(timeZone).formatToParts(date)) {
    if (part.type !== 'literal') parts[part.type] = Number(part.value)
  }
  const wallAsUtc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour % 24, parts.minute, parts.second)
  const exact = Math.floor(date.getTime() / 1000) * 1000
  return Math.round((wallAsUtc - exact) / 60000)
}

/** Minutes the clock is ahead of UTC at an instant (now by default). */
export function getGuideOffsetMinutes(clock, instant = dayjs.utc()) {
  if (isGuideTimeZone(clock)) {
    return zoneOffsetMinutes(clock.trim(), instant)
  }
  return getGuideOffsetHours(clock) * 60
}

/** Hours the clock is ahead of UTC: a fixed offset as is, a time zone at an instant (now by default). */
export function getGuideOffsetHours(value, instant) {
  if (isGuideTimeZone(value)) {
    return zoneOffsetMinutes(value.trim(), instant || dayjs.utc()) / 60
  }
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : 0
}

export function getProgramInstant(program, kind = 'start') {
  if (!program) {
    return null
  }

  let timestampKeys = ['start_timestamp']
  let isoKeys = ['start_time', 'program_start']

  if (kind === 'end') {
    timestampKeys = ['stop_timestamp']
    isoKeys = ['end_time', 'program_end']
  } else if (kind === 'available') {
    timestampKeys = []
    isoKeys = ['available_at', 'end_time', 'program_end']
  }

  for (const key of timestampKeys) {
    const timestamp = coerceTimestamp(program[key])
    if (timestamp != null) {
      return dayjs.unix(timestamp).utc()
    }
  }

  for (const key of isoKeys) {
    const parsed = parseIsoUtc(program[key])
    if (parsed) {
      return parsed
    }
  }

  return null
}

export function getChannelDisplayTime(program, kind = 'start', guideOffsetHours = 0) {
  const instant = getProgramInstant(program, kind)
  if (!instant) {
    return null
  }
  return instant.add(getGuideOffsetMinutes(guideOffsetHours, instant), 'minute')
}

export function formatChannelDateTime(
  program,
  kind = 'start',
  guideOffsetHours = 0,
  format = 'MMM D, YYYY h:mm A'
) {
  const displayTime = getChannelDisplayTime(program, kind, guideOffsetHours)
  return displayTime ? displayTime.format(format) : ''
}

export function getNowUtc() {
  return dayjs.utc()
}

// The helpers above all go one way: a UTC instant out of the provider, shown on
// the channel's clock. A time slot is typed rather than read, so it needs the
// reverse — a wall-clock time the user entered against the guide, turned back
// into the UTC instant the provider is asked for.

const CHANNEL_INPUT_FORMAT = 'YYYY-MM-DDTHH:mm'

/**
 * Parse a datetime-local string entered on the channel's clock into a UTC instant.
 * Returns null when the value is absent or unparseable.
 */
export function channelDisplayToUtc(value, guideOffsetHours = 0) {
  if (!value) {
    return null
  }
  const parsed = dayjs.utc(value)
  if (!parsed.isValid()) {
    return null
  }
  // A time zone's offset depends on the instant being asked for, which is what
  // is being worked out: guess with the offset at the wall time, then settle on
  // the offset at the guessed instant.
  const guess = parsed.subtract(getGuideOffsetMinutes(guideOffsetHours, parsed), 'minute')
  return parsed.subtract(getGuideOffsetMinutes(guideOffsetHours, guess), 'minute')
}

/** As channelDisplayToUtc, but as a unix timestamp in seconds. */
export function channelDisplayToUnix(value, guideOffsetHours = 0) {
  const instant = channelDisplayToUtc(value, guideOffsetHours)
  return instant ? instant.unix() : null
}

/** Format a UTC instant as a datetime-local value on the channel's clock. */
export function utcToChannelInput(instant, guideOffsetHours = 0) {
  if (!instant) {
    return ''
  }
  const utcInstant = dayjs.utc(instant)
  return utcInstant.add(getGuideOffsetMinutes(guideOffsetHours, utcInstant), 'minute').format(CHANNEL_INPUT_FORMAT)
}

/** Now on the channel's clock, rounded down, as a datetime-local value. */
export function nowChannelInput(guideOffsetHours = 0, roundToMinutes = 15) {
  const nowUtc = getNowUtc()
  const now = nowUtc.add(getGuideOffsetMinutes(guideOffsetHours, nowUtc), 'minute')
  const step = Math.max(1, roundToMinutes)
  const rounded = now.minute(Math.floor(now.minute() / step) * step).second(0).millisecond(0)
  return rounded.format(CHANNEL_INPUT_FORMAT)
}

/** Shift a datetime-local value by minutes, staying on the channel's clock. */
export function addMinutesToChannelInput(value, minutes) {
  if (!value) {
    return ''
  }
  const parsed = dayjs.utc(value)
  return parsed.isValid() ? parsed.add(minutes, 'minute').format(CHANNEL_INPUT_FORMAT) : ''
}

export function formatAirDateTime(program, kind = 'start', guideOffsetHours = 0) {
  const displayTime = getChannelDisplayTime(program, kind, guideOffsetHours)
  if (!displayTime) return ''
  const nowUtc = getNowUtc()
  const now = nowUtc.add(getGuideOffsetMinutes(guideOffsetHours, nowUtc), 'minute')
  const todayDate = now.format('YYYY-MM-DD')
  const tomorrowDate = now.add(1, 'day').format('YYYY-MM-DD')
  const yesterdayDate = now.subtract(1, 'day').format('YYYY-MM-DD')
  const displayDate = displayTime.format('YYYY-MM-DD')
  const timeStr = displayTime.format('h:mm A')
  if (displayDate === todayDate) {
    return `Today at ${timeStr}`
  }
  if (displayDate === tomorrowDate) {
    return `Tomorrow at ${timeStr}`
  }
  if (displayDate === yesterdayDate) {
    return `Yesterday at ${timeStr}`
  }
  for (let d = 2; d <= 6; d++) {
    if (displayDate === now.add(d, 'day').format('YYYY-MM-DD')) {
      return `${displayTime.format('dddd')} at ${timeStr}`
    }
  }
  return `${displayTime.format('ddd, MMM D, YYYY')} at ${timeStr}`
}
