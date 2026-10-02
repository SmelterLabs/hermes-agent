import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const gatewayState = vi.hoisted(() => ({ current: null as null | object }))
const requestGateway = vi.hoisted(() => vi.fn())

vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ gateway: gatewayState.current, requestGateway })
}))

// Keep store/profile's side-effecting imports inert (gateway socket layer +
// REST client) — same seam as store/profile.test.ts.
vi.mock('@/store/gateway', () => ({
  $gateway: atom<unknown>(null),
  activeGateway: () => null,
  activeGatewayConnectionId: () => null,
  ensureGatewayForProfile: vi.fn(async () => undefined)
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph: vi.fn() }))

const { ProfileTag } = await import('./profile-tag')
const { $profiles, $profilesConnectionId, setProfileColor } = await import('@/store/profile')

beforeEach(() => {
  gatewayState.current = {}
  requestGateway.mockReset().mockResolvedValue({ found: false })
})

afterEach(() => {
  cleanup()
  $profiles.set([])
  $profilesConnectionId.set(null)
})

describe('ProfileTag', () => {
  it('shows the profile initial with an accessible owner label', () => {
    render(<ProfileTag profile="xavier" />)

    const tag = screen.getByRole('img', { name: 'Profile: xavier' })
    expect(tag.textContent).toBe('x')
  })

  it('normalizes an empty profile to default, which shows the home glyph', () => {
    render(<ProfileTag profile="" />)

    const tag = screen.getByRole('img', { name: 'Profile: default' })
    // The default profile has no initial and no identity color — it is the
    // home icon, same as the profiles panel and the rail.
    expect(tag.textContent).toBe('')
    expect(tag.querySelector('.codicon-home')).not.toBeNull()
    expect(tag.style.color).toBe('')
  })

  it('shows the configured default owner identity instead of the generic home glyph', () => {
    $profiles.set([
      {
        display_name: 'Clyde',
        has_env: true,
        is_default: true,
        model: null,
        name: 'default',
        path: 'C:/Users/casey/AppData/Local/hermes',
        provider: null,
        skill_count: 0
      }
    ])
    render(<ProfileTag profile="default" />)

    const tag = screen.getByRole('img', { name: 'Profile: Clyde' })
    expect(tag.textContent).toBe('C')
    expect(tag.querySelector('.codicon-home')).toBeNull()
    expect(tag.classList.contains('rounded-full')).toBe(true)
    expect(tag.classList.contains('ring-1')).toBe(true)
    expect(tag.style.backgroundColor).toContain('24%')
  })

  it('does not borrow a renamed default owner from another gateway', () => {
    $profilesConnectionId.set('forge')
    $profiles.set([
      {
        display_name: 'Bellow',
        has_env: true,
        is_default: true,
        model: null,
        name: 'default',
        path: '/home/forge/.hermes',
        provider: null,
        skill_count: 0
      }
    ])

    render(<ProfileTag connectionId="local" profile="default" />)

    const tag = screen.getByRole('img', { name: 'Profile: default' })
    expect(tag.querySelector('.codicon-home')).not.toBeNull()
    expect(screen.queryByRole('img', { name: 'Profile: Bellow' })).toBeNull()
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('uses the same saved owner avatar as the active profile tile', async () => {
    requestGateway.mockResolvedValue({ data: 'data:image/webp;base64,Y2x5ZGU=', found: true })
    $profiles.set([
      {
        display_name: 'Clyde',
        has_env: true,
        is_default: true,
        model: null,
        name: 'default',
        path: 'C:/Users/casey/AppData/Local/hermes',
        provider: null,
        skill_count: 0
      }
    ])

    render(<ProfileTag profile="default" />)

    await waitFor(() => expect(screen.getByRole('img', { name: 'Profile: Clyde' }).tagName).toBe('IMG'))
    expect(screen.getByRole('img', { name: 'Profile: Clyde' }).getAttribute('src')).toBe(
      'data:image/webp;base64,Y2x5ZGU='
    )
  })

  it('uses the profile identity color (user override wins)', () => {
    setProfileColor('xavier', 'hsl(120 68% 58%)')

    render(<ProfileTag profile="xavier" />)

    const tag = screen.getByRole('img', { name: 'Profile: xavier' })
    // jsdom normalizes hsl() to rgb(); assert the override landed, not the format.
    expect(tag.style.color).toBe('rgb(75, 221, 75)')
  })
})
