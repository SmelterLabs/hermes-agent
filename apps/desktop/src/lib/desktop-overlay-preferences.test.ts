import { describe, expect, it } from 'vitest'

import { FLEET_RAIL_ENABLED } from '@/app/chat/sidebar/fleet-rail'
import { ENUM_OPTIONS } from '@/app/settings/constants'
import { SESSION_RESUME_REQUEST_TIMEOUT_MS } from '@/hermes'

describe('accepted Desktop overlay preferences', () => {
  it('shows the selected box strip, not the full fleet rail', () => {
    expect(FLEET_RAIL_ENABLED).toBe(false)
  })

  it('retains the cold resume budget', () => {
    expect(SESSION_RESUME_REQUEST_TIMEOUT_MS).toBe(300_000)
  })

  it('retains the complete accepted built-in xAI voice picker', () => {
    expect(ENUM_OPTIONS['tts.xai.voice_id']).toEqual([
      'eve', 'altair', 'ara', 'atlas', 'aurora', 'carina', 'castor',
      'celeste', 'cosmo', 'helios', 'helix', 'iris', 'kepler', 'leo',
      'liora', 'lumen', 'luna', 'lux', 'naksh', 'orion', 'perseus',
      'rex', 'rigel', 'sal', 'sirius', 'ursa', 'zagan', 'zenith'
    ])
  })
})
