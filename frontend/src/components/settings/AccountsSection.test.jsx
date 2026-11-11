import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { renderWithQuery } from '../../test/renderWithProviders'
import { accountsApi, epgApi, settingsApi } from '../../api'
import AccountsSection, { accountSourceLabel, catchupStyleLabel } from './AccountsSection'

vi.mock('../../api', () => ({
  accountsApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    test: vi.fn(),
  },
  epgApi: { status: vi.fn(), refresh: vi.fn() },
  settingsApi: { get: vi.fn(), update: vi.fn() },
}))

const baseAccount = {
  id: 1,
  name: 'Provider One',
  server_url: 'https://provider.example.com',
  username: 'user',
  is_active: true,
  guide_offset_hours: 0,
  catchup_url_style: 'auto',
  catchup_url_style_resolved: null,
}

function mockAccounts(overrides = {}) {
  accountsApi.list.mockResolvedValue([{ ...baseAccount, ...overrides }])
  settingsApi.get.mockResolvedValue({ default_account_id: 1 })
  epgApi.status.mockResolvedValue({ running: false })
}

describe('catchupStyleLabel', () => {
  it('reports the probed query style while on automatic', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: 'query' })
    ).toBe('Automatic — using query style')
  })

  it('reports the probed path style while on automatic', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: 'path' })
    ).toBe('Automatic — using path style')
  })

  it('says the style is undetermined when nothing has been probed yet', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: null })
    ).toBe('Automatic — style not yet determined')
  })

  it('reports a forced path style', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'path', catchup_url_style_resolved: 'query' })
    ).toBe('Catchup URL: path style')
  })

  it('reports a forced query style', () => {
    expect(catchupStyleLabel({ catchup_url_style: 'query' })).toBe('Catchup URL: query style')
  })

  it('falls back to automatic when the account has no setting', () => {
    expect(catchupStyleLabel({})).toBe('Automatic — style not yet determined')
    expect(catchupStyleLabel(null)).toBe('Automatic — style not yet determined')
  })
})

describe('AccountsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the resolved catchup style on the account card', async () => {
    mockAccounts({ catchup_url_style_resolved: 'query' })

    renderWithQuery(<AccountsSection />)

    expect(await screen.findByText('Automatic — using query style')).toBeInTheDocument()
  })

  it('shows the forced catchup style on the account card', async () => {
    mockAccounts({ catchup_url_style: 'query', catchup_url_style_resolved: null })

    renderWithQuery(<AccountsSection />)

    expect(await screen.findByText('Catchup URL: query style')).toBeInTheDocument()
  })

  it('round-trips the catchup URL style select into the update payload', async () => {
    mockAccounts({ catchup_url_style: 'auto' })
    accountsApi.update.mockResolvedValue({})

    renderWithQuery(<AccountsSection />)

    fireEvent.click(await screen.findByRole('button', { name: /Edit/ }))

    const select = await screen.findByRole('textbox', { name: 'Catchup URL style' })
    expect(select).toHaveValue('Automatic')

    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'Query (/streaming/timeshift.php)' }))
    expect(select).toHaveValue('Query (/streaming/timeshift.php)')

    fireEvent.click(screen.getByRole('button', { name: 'Update' }))

    await waitFor(() => {
      expect(accountsApi.update).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ catchup_url_style: 'query' })
      )
    })
  })

  it('defaults new accounts to automatic', async () => {
    mockAccounts()
    accountsApi.create.mockResolvedValue({})

    renderWithQuery(<AccountsSection />)

    fireEvent.click(await screen.findByRole('button', { name: 'Add Account' }))

    const select = await screen.findByRole('textbox', { name: 'Catchup URL style' })
    expect(select).toHaveValue('Automatic')
  })

  it('shows only the host of an M3U playlist, never its token, and no username', async () => {
    mockAccounts({
      provider_type: 'm3u',
      server_url: 'https://hls.example/pl/61/secrettoken/playlist.m3u8',
      username: '',
    })

    renderWithQuery(<AccountsSection />)

    expect(await screen.findByText('M3U playlist · hls.example')).toBeInTheDocument()
    expect(screen.queryByText(/secrettoken/)).not.toBeInTheDocument()
    expect(screen.queryByText(/User:/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Automatic — /)).not.toBeInTheDocument()
  })

  it('creates an M3U account from the playlist URL alone, keeping its case', async () => {
    mockAccounts()
    accountsApi.create.mockResolvedValue({})

    renderWithQuery(<AccountsSection />)

    fireEvent.click(await screen.findByRole('button', { name: 'Add Account' }))
    fireEvent.click(await screen.findByText('M3U playlist'))

    expect(screen.queryByRole('textbox', { name: 'Username' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Catchup URL style' })).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Account Name' }), { target: { value: 'TV Team' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Playlist URL' }), {
      target: { value: 'https://hls.example/pl/61/AbC/playlist.m3u8' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Add Account' }).at(-1))

    await waitFor(() => {
      expect(accountsApi.create).toHaveBeenCalled()
    })
    expect(accountsApi.create.mock.calls[0][0]).toEqual({
      name: 'TV Team',
      provider_type: 'm3u',
      server_url: 'https://hls.example/pl/61/AbC/playlist.m3u8',
      epg_url: null,
      guide_offset_hours: 0,
    })
  })
})

describe('accountSourceLabel', () => {
  it('shows the Xtream server URL as is', () => {
    expect(accountSourceLabel({ server_url: 'https://x.example' })).toBe('https://x.example')
  })

  it('falls back when an M3U URL does not parse', () => {
    expect(accountSourceLabel({ provider_type: 'm3u', server_url: 'not a url' })).toBe('M3U playlist')
  })
})
