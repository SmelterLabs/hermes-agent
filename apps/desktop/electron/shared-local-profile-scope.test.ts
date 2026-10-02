import { describe, expect, it } from 'vitest'

import { pathForRegistryBackendRequest } from './connection-config'
import { registryGatewayWsUrl } from './plugin-profile-routes'

const shared = { profile: 'picasso', sharedPrimary: true }

describe('shared local descriptors preserve profile scope', () => {
  it('scopes REST configuration and tool requests', () => {
    expect(pathForRegistryBackendRequest('/api/config?include_defaults=false', 'picasso', shared)).toBe(
      '/api/config?include_defaults=false&profile=picasso'
    )
    expect(pathForRegistryBackendRequest('/api/tools', 'picasso', shared)).toBe('/api/tools?profile=picasso')
  })

  it('scopes registry WebSockets used outside the primary gateway', () => {
    const url = new URL(registryGatewayWsUrl(shared, 'ws://127.0.0.1:4242/api/ws?token=fixture'))
    expect(url.searchParams.get('profile')).toBe('picasso')
    expect(url.searchParams.get('token')).toBe('fixture')
  })

  it('leaves isolated descriptors and explicit REST scopes unchanged', () => {
    expect(pathForRegistryBackendRequest('/api/config', 'picasso', {})).toBe('/api/config')
    expect(pathForRegistryBackendRequest('/api/config?profile=founder', 'picasso', shared)).toBe(
      '/api/config?profile=founder'
    )
    expect(registryGatewayWsUrl({ profile: 'picasso' }, 'ws://127.0.0.1:4242/api/ws')).toBe(
      'ws://127.0.0.1:4242/api/ws'
    )
  })
})
