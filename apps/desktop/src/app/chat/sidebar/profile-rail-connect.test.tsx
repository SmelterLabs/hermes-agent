import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProfileRail } from './profile-switcher'

// The rail's discoverability pills are navigation, not identity — assert the
// multi-gateway entry point deep-links to Settings → Connections instead of
// relying on someone finding the pane three levels into Settings (the exact
// gap reported against the multi-connection registry launch).

const navigate = vi.fn()
const requestGateway = vi.fn(async (): Promise<{ data?: string; found: boolean }> => ({ found: false }))

vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ gateway: { id: 'test-gateway' }, requestGateway })
}))

vi.mock('react-router', () => ({
  useNavigate: () => navigate
}))

// The local overlay ships the fleet rail disabled (selected-box-only strip);
// force it on here so the upstream multi-gateway behavior stays tested.
vi.mock('./fleet-rail', async importOriginal => ({
  ...(await importOriginal<typeof import('./fleet-rail')>()),
  FLEET_RAIL_ENABLED: true
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      profiles: {
        allProfiles: 'All profiles',
        connectGateway: 'Manage gateways…',
        failedLoadSoul: 'Failed to load SOUL.md',
        failedSaveSoul: 'Failed to save SOUL.md',
        importProfile: 'Import profile…',
        manageProfiles: 'Manage profiles…',
        newProfile: 'New profile',
        remoteOverride: {
          badge: (host: string) => `Runs on ${host}`,
          menuItem: 'Connect to a remote host…'
        },
        saveSoul: 'Save',
        saving: 'Saving…',
        showAllProfiles: 'Show all profiles',
        soulSaved: 'SOUL.md saved',
        switchToProfile: (name: string) => `Switch to ${name}`,
        title: 'Profiles'
      }
    }
  })
}))

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: atom('default'),
  $profileColors: atom({}),
  $profileCreateRequest: atom(0),
  $profileOrder: atom([]),
  $profiles: atom([{ is_default: true, name: 'default' }]),
  $profilesConnectionId: atom<null | string>(null),
  $profileScope: atom('default'),
  // The rail's status summary (profile-dot-state) rides the real session
  // stores, whose import graph reaches $showAllProfiles through layout state.
  $showAllProfiles: atom(false),
  ALL_PROFILES: '*',
  normalizeProfileKey: (name: string) => name,
  prewarmProfilePick: vi.fn(),
  profileLabel: (profile: { display_name?: string; name: string }) =>
    (profile.display_name ?? '').trim() || profile.name,
  refreshActiveProfile: vi.fn().mockResolvedValue(undefined),
  selectProfile: vi.fn(),
  setProfileColor: vi.fn(),
  setProfileOrder: vi.fn(),
  setShowAllProfiles: vi.fn(),
  sortByProfileOrder: (profiles: unknown[]) => profiles
}))

vi.mock('@/store/connections', () => ({
  $activeConnectionId: atom<null | string>(null),
  $connectionsRegistry: atom(null),
  $hasMultipleConnections: atom(false),
  $pendingConnectionId: atom<null | string>(null),
  selectConnection: vi.fn()
}))

vi.mock('@/store/profile-share', () => ({
  runExportProfileFlow: vi.fn(),
  runImportProfileFlow: vi.fn()
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), notePointerMove: vi.fn(), startPrewarm: vi.fn() })
}))

vi.mock('@/hermes', () => ({
  getProfileSoul: vi.fn().mockResolvedValue({ content: '' }),
  updateProfileSoul: vi.fn()
}))

vi.mock('@/components/chat/code-editor', () => ({ CodeEditor: () => null }))
vi.mock('../../profiles/create-profile-dialog', () => ({ CreateProfileDialog: () => null }))
vi.mock('../../profiles/delete-profile-dialog', () => ({ DeleteProfileDialog: () => null }))
vi.mock('../../profiles/rename-profile-dialog', () => ({ RenameProfileDialog: () => null }))

const { $activeConnectionId, $hasMultipleConnections, $pendingConnectionId } = await import('@/store/connections')

const activeConnectionId = $activeConnectionId as ReturnType<typeof atom<null | string>>
const hasMultipleConnections = $hasMultipleConnections as ReturnType<typeof atom<boolean>>
const pendingConnectionId = $pendingConnectionId as ReturnType<typeof atom<null | string>>
const { $profiles, $profilesConnectionId } = await import('@/store/profile')

const profiles = $profiles as ReturnType<
  typeof atom<Array<{ display_name?: string; is_default: boolean; name: string }>>
>

const profilesConnectionId = $profilesConnectionId as ReturnType<typeof atom<null | string>>

afterEach(() => {
  cleanup()
  requestGateway.mockReset()
  requestGateway.mockResolvedValue({ found: false })
  hasMultipleConnections.set(false)
  activeConnectionId.set(null)
  pendingConnectionId.set(null)
  profilesConnectionId.set(null)
  profiles.set([{ is_default: true, name: 'default' }])
})

describe('ProfileRail multi-gateway entry point', () => {
  it('deep-links to the unified Settings → Gateways page from the rail', () => {
    render(<ProfileRail />)

    const pill = screen.getByRole('button', { name: 'Manage gateways…' })
    fireEvent.click(pill)

    expect(navigate).toHaveBeenCalledWith('/settings?tab=gateway')
  })

  it('keeps the entry point visible for single-profile users', () => {
    render(<ProfileRail />)

    // The whole point is first-run discoverability: the pill must not be
    // gated behind multiProfile the way the default↔all toggle is.
    expect(screen.getByRole('button', { name: 'Manage gateways…' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Manage profiles…' })).toBeTruthy()
  })

  it('keeps the active profile explicit when gateway identity moves to the statusbar', () => {
    hasMultipleConnections.set(true)
    render(<ProfileRail />)

    expect(screen.getByRole('button', { name: 'default' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Manage gateways…' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Manage profiles…' })).toBeTruthy()
  })

  it('shows a renamed default owner and its saved avatar', async () => {
    requestGateway.mockResolvedValue({ data: 'data:image/png;base64,YmVsbG93', found: true })
    profiles.set([{ display_name: 'Bellow', is_default: true, name: 'default' }])
    render(<ProfileRail />)

    const owner = screen.getByRole('button', { name: 'Bellow' })
    expect(owner.getAttribute('aria-pressed')).toBe('true')
    await waitFor(() => expect(owner.querySelector('img')?.getAttribute('src')).toContain('data:image/png'))
    expect(requestGateway).toHaveBeenCalledWith('profiles.get_asset', { asset: 'avatar', name: 'default' })
  })

  it('does not paint a prior gateway profile cache while a source switch is pending', () => {
    activeConnectionId.set('forge')
    profilesConnectionId.set('local')
    pendingConnectionId.set('forge')
    profiles.set([{ display_name: 'Bellow', is_default: true, name: 'default' }])
    render(<ProfileRail />)

    expect(screen.queryByRole('button', { name: 'Bellow' })).toBeNull()
  })

  // Regression: selecting a local NAMED profile (Basham/Picasso/Founder) makes
  // activeGatewayConnectionId() return null, because gatewayForProfile()
  // creates that secondary with `connectionId: null`. refreshProfiles() stamps
  // the null onto $profilesConnectionId, and the old guard read
  // null !== 'local' as a cross-gateway mismatch and blanked every square —
  // while the fleet rail hides the active gateway by design, so the local
  // profiles vanished from BOTH surfaces until the user re-picked This device.
  it('keeps the local strip painted when the active named profile resolves no connection id', () => {
    activeConnectionId.set('local')
    profilesConnectionId.set(null)
    pendingConnectionId.set(null)
    profiles.set([{ display_name: 'Bellow', is_default: true, name: 'default' }])
    render(<ProfileRail />)

    expect(screen.getByRole('button', { name: 'Bellow' })).toBeTruthy()
  })

  it('still blanks the strip on a known cross-gateway mismatch', () => {
    activeConnectionId.set('forge')
    profilesConnectionId.set('local')
    pendingConnectionId.set(null)
    profiles.set([{ display_name: 'Bellow', is_default: true, name: 'default' }])
    render(<ProfileRail />)

    expect(screen.queryByRole('button', { name: 'Bellow' })).toBeNull()
  })

  it('keeps thirteen profiles direct and condenses the fourteenth', () => {
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 12 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    const { unmount } = render(<ProfileRail />)

    expect(screen.queryByRole('button', { name: 'Profiles' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Profile 12' })).toBeTruthy()
    unmount()

    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 13 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    render(<ProfileRail />)

    expect(screen.getByRole('button', { name: 'Profiles' })).toBeTruthy()
  })

  it('stays shrinkable with many profiles and multiple gateways', () => {
    hasMultipleConnections.set(true)
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 13 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    render(<ProfileRail />)

    expect(screen.getByRole('group', { name: 'Profiles' }).className).toContain('min-w-0')
    expect(screen.getByRole('button', { name: 'Profiles' })).toBeTruthy()
  })
})
