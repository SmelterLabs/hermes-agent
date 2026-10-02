import { describe, expect, it } from 'vitest'

import {
  fillBitwardenLogin,
  parseBitwardenLoginItems,
  previewGuestBelongsToSender,
  validateCredentialUrl
} from './bitwarden-autofill'

describe('parseBitwardenLoginItems', () => {
  it('keeps usable logins and excludes non-logins and empty passwords', () => {
    const parsed = parseBitwardenLoginItems(
      JSON.stringify([
        { id: 'one', name: 'Primary', type: 1, login: { username: 'casey@example.com', password: 'secret' } },
        { id: 'note', name: 'Note', type: 2, notes: 'not a login' },
        { id: 'empty', name: 'No password', type: 1, login: { username: 'casey', password: '' } }
      ])
    )

    expect(parsed).toEqual([
      { id: 'one', name: 'Primary', username: 'casey@example.com', password: 'secret' }
    ])
  })

  it('rejects malformed CLI output', () => {
    expect(() => parseBitwardenLoginItems('{bad json')).toThrow('Bitwarden returned invalid data')
    expect(() => parseBitwardenLoginItems('{}')).toThrow('Bitwarden returned an unexpected response')
  })
})

describe('validateCredentialUrl', () => {
  it('allows only normal http(s) pages', () => {
    expect(validateCredentialUrl('https://example.com/login')).toBe('https://example.com/login')
    expect(validateCredentialUrl('http://localhost:3000/login')).toBe('http://localhost:3000/login')
    expect(() => validateCredentialUrl('file:///C:/secret.html')).toThrow('Bitwarden autofill is only available')
    expect(() => validateCredentialUrl('javascript:alert(1)')).toThrow('Bitwarden autofill is only available')
  })
})

describe('previewGuestBelongsToSender', () => {
  it('accepts only a live guest hosted by the invoking renderer', () => {
    expect(previewGuestBelongsToSender(42, { isDestroyed: () => false, hostWebContents: { id: 42 } })).toBe(true)
    expect(previewGuestBelongsToSender(42, { isDestroyed: () => false, hostWebContents: { id: 7 } })).toBe(false)
    expect(previewGuestBelongsToSender(42, { isDestroyed: () => true, hostWebContents: { id: 42 } })).toBe(false)
    expect(previewGuestBelongsToSender(42, null)).toBe(false)
  })
})

describe('fillBitwardenLogin', () => {
  it('keeps credentials out of page JavaScript and types without submitting', async () => {
    const scripts: string[] = []
    const inserted: string[] = []

    const guest = {
      getURL: () => 'https://example.com/login',
      executeJavaScript: async (script: string) => {
        scripts.push(script)

        return true
      },
      insertText: async (text: string) => {
        inserted.push(text)
      }
    }

    await expect(
      fillBitwardenLogin(guest as never, 'https://example.com/login', {
        id: 'one',
        name: 'Example',
        username: 'casey@example.com',
        password: 'secret-password'
      })
    ).resolves.toEqual({ usernameFilled: true, passwordFilled: true })

    expect(inserted).toEqual(['casey@example.com', 'secret-password'])
    expect(scripts.join('\n')).not.toContain('casey@example.com')
    expect(scripts.join('\n')).not.toContain('secret-password')
    expect(scripts.join('\n')).not.toMatch(/submit\s*\(/i)
  })

  it('fails closed when the guest navigated after selection', async () => {
    const guest = {
      getURL: () => 'https://evil.example/login',
      executeJavaScript: async () => true,
      insertText: async () => undefined
    }

    await expect(
      fillBitwardenLogin(guest as never, 'https://example.com/login', {
        id: 'one',
        name: 'Example',
        username: 'casey@example.com',
        password: 'secret-password'
      })
    ).rejects.toThrow('page changed')
  })
})
