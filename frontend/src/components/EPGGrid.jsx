import { useMemo, useRef, useEffect } from 'react'
import { Stack, Text, Badge, ScrollArea, Box, Tooltip } from '@mantine/core'
import { IconClock, IconDownload, IconCalendar, IconPlayerPlay } from '@tabler/icons-react'
import {
  getChannelDisplayTime,
  getGuideOffsetHours,
  normalizeGuideClock,
  getNowUtc,
  getProgramInstant,
} from '../utils/channelTime'
import classes from './EPGGrid.module.css'

const previousDownloadStatusMeta = {
  pending: { label: 'Queued', color: 'yellow' },
  downloading: { label: 'Downloading', color: 'yellow' },
  processing: { label: 'Processing', color: 'violet' },
  completed: { label: 'Downloaded', color: 'green' },
  failed: { label: 'Failed', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
}

function formatRowDuration(minutes) {
  const parsed = Number(minutes)
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  const hours = Math.floor(parsed / 60)
  const mins = Math.round(parsed % 60)
  if (hours > 0) {
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
  }
  return `${mins}m`
}

function ProgramRow({
  program,
  onClick,
  isCurrent,
  isDownloadable,
  isSchedulable,
  isExpired,
  elementId,
  previousDownload,
  guideOffsetHours,
}) {
  const startTime = getChannelDisplayTime(program, 'start', guideOffsetHours)
  const endTime = getChannelDisplayTime(program, 'end', guideOffsetHours)
  const previousStatus = previousDownload?.status
  const previousMeta = (previousStatus && previousDownloadStatusMeta[previousStatus]) || null
  const duration = formatRowDuration(program.duration_minutes)

  const stateClass = isCurrent
    ? classes.live
    : isDownloadable
    ? classes.downloadable
    : isSchedulable
    ? classes.future
    : classes.expired

  const handleRowClick = () => {
    if (isDownloadable) {
      onClick(program, { action: 'download' })
    } else if (isCurrent) {
      onClick(program, { action: 'preview', mode: 'live' })
    } else if (isSchedulable) {
      onClick(program, { action: 'schedule' })
    }
  }

  return (
    <div id={elementId} className={`${classes.row} ${stateClass}`} onClick={handleRowClick}>
      <div className={classes.time}>
        <div className={classes.timeStart}>{startTime?.format('h:mm A') || 'Unknown'}</div>
        <div className={classes.timeEnd}>{endTime?.format('h:mm A') || ''}</div>
      </div>
      <div className={classes.main}>
        <div className={classes.title}>
          <span className={classes.titleText}>{program.title}</span>
          {isCurrent && (
            <Badge size="xs" variant="outline" color="gray" style={{ flexShrink: 0 }}>
              In progress
            </Badge>
          )}
          {isExpired && (
            <Tooltip label="No longer available — outside this channel's catchup window" withArrow>
              <Badge size="xs" variant="outline" color="gray" style={{ flexShrink: 0 }}>
                Expired
              </Badge>
            </Tooltip>
          )}
          {previousMeta && (
            <Badge size="xs" variant="light" color={previousMeta.color} style={{ flexShrink: 0 }}>
              {previousMeta.label}
            </Badge>
          )}
        </div>
        {program.description && <div className={classes.desc}>{program.description}</div>}
      </div>
      <div className={classes.side}>
        {duration && <span className={classes.dur}>{duration}</span>}
        {isCurrent && (
          <button
            type="button"
            className={`${classes.act} ${classes.actGhost}`}
            title="Preview"
            onClick={(e) => {
              e.stopPropagation()
              onClick(program, { action: 'preview', mode: 'live' })
            }}
          >
            <IconPlayerPlay size={13} />
            <span className={classes.actLabel}>Preview</span>
          </button>
        )}
        {isDownloadable && (
          <>
            <button
              type="button"
              className={`${classes.act} ${classes.actGhost}`}
              title="Preview"
              onClick={(e) => {
                e.stopPropagation()
                onClick(program, { action: 'preview', mode: 'catchup' })
              }}
            >
              <IconPlayerPlay size={13} />
              <span className={classes.actLabel}>Preview</span>
            </button>
            <button
              type="button"
              className={`${classes.act} ${classes.actDownload}`}
              title="Download"
              onClick={(e) => {
                e.stopPropagation()
                onClick(program, { action: 'download' })
              }}
            >
              <IconDownload size={13} />
              <span className={classes.actLabel}>Download</span>
            </button>
          </>
        )}
        {!isCurrent && isSchedulable && (
          <button
            type="button"
            className={`${classes.act} ${classes.actSchedule}`}
            title="Schedule"
            onClick={(e) => {
              e.stopPropagation()
              onClick(program, { action: 'schedule' })
            }}
          >
            <IconCalendar size={13} />
            <span className={classes.actLabel}>Schedule</span>
          </button>
        )}
      </div>
    </div>
  )
}

function DaySection({ date, programs, onProgramClick, getProgramPreviousDownload, guideOffsetHours, archiveCutoff }) {
  const now = getNowUtc()
  const displayNow = now.add(getGuideOffsetHours(guideOffsetHours), 'hour')
  const isToday = date.isSame(displayNow, 'day')

  const sortedPrograms = useMemo(() => {
    return [...programs].sort((a, b) =>
      (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
    )
  }, [programs])

  return (
    <div>
      <div className={classes.dayHead}>
        {isToday ? (
          <span className={classes.dayToday}>TODAY</span>
        ) : (
          <span className={classes.dayDate}>{date.format('ddd, MMM D')}</span>
        )}
        <span className={classes.dayRule} />
        <span className={classes.dayCount}>{sortedPrograms.length}</span>
      </div>

      {sortedPrograms.map((program, idx) => {
        const start = getProgramInstant(program, 'start')
        const end = getProgramInstant(program, 'end')
        const isPast = Boolean(end?.isBefore(now))
        const isCurrent = Boolean(start?.isBefore(now) && end?.isAfter(now))
        const isExpired = Boolean(
          isPast && program.has_archive && archiveCutoff && end?.isBefore(archiveCutoff)
        )
        const isDownloadable = isPast && program.has_archive && !isExpired
        const isSchedulable = !isPast
        const elementId = `epg-program-${program.epg_id || program.id || idx}`
        const previousDownload = getProgramPreviousDownload
          ? getProgramPreviousDownload(program)
          : null

        return (
          <ProgramRow
            key={program.id || idx}
            program={program}
            onClick={onProgramClick}
            isCurrent={isCurrent}
            isDownloadable={isDownloadable}
            isSchedulable={isSchedulable}
            isExpired={isExpired}
            elementId={elementId}
            previousDownload={previousDownload}
            guideOffsetHours={guideOffsetHours}
          />
        )
      })}
    </div>
  )
}

export default function EPGGrid({
  epgData,
  onProgramClick,
  showFuture = false,
  getProgramPreviousDownload = null,
  guideOffsetHours = 0,
  archiveDays = null,
}) {
  const scrollAreaRef = useRef(null)
  const didAutoScrollRef = useRef(false)
  const normalizedGuideOffsetHours = normalizeGuideClock(guideOffsetHours)
  const now = useMemo(() => getNowUtc(), [epgData, showFuture])

  // Programs that ended before this instant are outside the provider's
  // catchup window and would 404 if queued for download.
  const archiveCutoff = useMemo(() => {
    const parsed = Number(archiveDays)
    if (!Number.isFinite(parsed) || parsed <= 0) return null
    return now.subtract(parsed, 'day')
  }, [archiveDays, now])

  const visiblePrograms = useMemo(() => {
    if (!epgData) return epgData
    if (showFuture) return epgData
    return epgData.filter((program) => getProgramInstant(program, 'end')?.isBefore(now))
  }, [epgData, showFuture, now])

  const programsByDay = useMemo(() => {
    const grouped = {}

    if (!visiblePrograms) return []

    visiblePrograms.forEach((program) => {
      const date = getChannelDisplayTime(program, 'start', normalizedGuideOffsetHours)?.startOf('day')
      if (!date) return

      const key = date.format('YYYY-MM-DD')
      if (!grouped[key]) {
        grouped[key] = {
          date,
          programs: [],
        }
      }
      grouped[key].programs.push(program)
    })

    return Object.values(grouped).sort((a, b) => b.date.valueOf() - a.date.valueOf())
  }, [normalizedGuideOffsetHours, visiblePrograms])

  const scrollTargetId = useMemo(() => {
    let target = null
    for (const { programs } of programsByDay) {
      const sorted = [...programs].sort((a, b) =>
        (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
      )
      for (const program of sorted) {
        const start = getProgramInstant(program, 'start')
        const end = getProgramInstant(program, 'end')
        const isCurrent = Boolean(start?.isBefore(now) && end?.isAfter(now))
        if (isCurrent) {
          target = program
          break
        }
      }
      if (target) break
    }

    if (!target) {
      for (const { programs } of programsByDay) {
        const sorted = [...programs].sort((a, b) =>
          (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
        )
        for (const program of sorted) {
          const end = getProgramInstant(program, 'end')
          if (end?.isBefore(now)) {
            target = program
            break
          }
        }
        if (target) break
      }
    }

    return target ? `epg-program-${target.epg_id || target.id || 'target'}` : null
  }, [now, programsByDay])

  useEffect(() => {
    didAutoScrollRef.current = false
  }, [epgData, showFuture])

  useEffect(() => {
    if (!scrollTargetId || didAutoScrollRef.current) return
    const target = document.getElementById(scrollTargetId)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      didAutoScrollRef.current = true
    }
  }, [scrollTargetId])

  if (!visiblePrograms || visiblePrograms.length === 0) {
    return (
      <Stack align="center" justify="center" h={200}>
        <IconClock size={48} opacity={0.3} />
        <Text c="dimmed">No EPG data available</Text>
      </Stack>
    )
  }

  return (
    <ScrollArea
      style={{ flex: 1, minHeight: 0, height: '100%' }}
      ref={scrollAreaRef}
      className={classes.scope}
    >
      {programsByDay.map(({ date, programs }) => (
        <Box key={date.format('YYYY-MM-DD')} id={`epg-day-${date.format('YYYY-MM-DD')}`}>
          <DaySection
            date={date}
            programs={programs}
            onProgramClick={onProgramClick}
            getProgramPreviousDownload={getProgramPreviousDownload}
            guideOffsetHours={normalizedGuideOffsetHours}
            archiveCutoff={archiveCutoff}
          />
        </Box>
      ))}
    </ScrollArea>
  )
}
