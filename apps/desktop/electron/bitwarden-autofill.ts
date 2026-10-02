import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import type { WebContents } from 'electron'

export interface BitwardenLogin {
  id: string
  name: string
  username: string
  password: string
}

interface PreviewGuestLike {
  hostWebContents?: { id?: number } | null
  isDestroyed?: () => boolean
}

const PROCESS_TIMEOUT_MS = 15_000
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024

function runFile(
  file: string,
  args: string[],
  options: { env?: NodeJS.ProcessEnv } = {}
): Promise<{ stderr: string; stdout: string }> {
  return new Promise((resolve, reject) => {
    execFile(
      file,
      args,
      {
        encoding: 'utf8',
        env: options.env,
        maxBuffer: MAX_OUTPUT_BYTES,
        timeout: PROCESS_TIMEOUT_MS,
        windowsHide: true
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(stderr.trim() || error.message))

          return
        }

        resolve({ stderr, stdout })
      }
    )
  })
}

export function validateCredentialUrl(rawUrl: string): string {
  try {
    const url = new URL(rawUrl)

    if (url.protocol !== 'http:' && url.protocol !== 'https:') {
      throw new Error()
    }

    return url.toString()
  } catch {
    throw new Error('Bitwarden autofill is only available on http(s) pages.')
  }
}

export function parseBitwardenLoginItems(raw: string): BitwardenLogin[] {
  let items: unknown

  try {
    items = JSON.parse(raw)
  } catch {
    throw new Error('Bitwarden returned invalid data.')
  }

  if (!Array.isArray(items)) {
    throw new Error('Bitwarden returned an unexpected response.')
  }

  const logins: BitwardenLogin[] = []

  for (const item of items) {
    if (!item || typeof item !== 'object' || (item as any).type !== 1) {
      continue
    }

    const login = (item as any).login
    const id = typeof (item as any).id === 'string' ? (item as any).id : ''
    const name = typeof (item as any).name === 'string' ? (item as any).name.trim() : ''
    const username = typeof login?.username === 'string' ? login.username : ''
    const password = typeof login?.password === 'string' ? login.password : ''

    if (id && name && password) {
      logins.push({ id, name, username, password })
    }
  }

  return logins
}

export function previewGuestBelongsToSender(senderId: number, guest: PreviewGuestLike | null): boolean {
  return Boolean(guest && !guest.isDestroyed?.() && guest.hostWebContents?.id === senderId)
}

function bitwardenCliPath(): string {
  const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming')

  return path.join(appData, 'npm', 'node_modules', '@bitwarden', 'cli', 'build', 'bw.js')
}

function nodeExecutablePath(): string {
  const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')

  const candidates = [
    path.join(localAppData, 'hermes', 'node', 'node.exe'),
    process.env.ProgramFiles ? path.join(process.env.ProgramFiles, 'nodejs', 'node.exe') : ''
  ]

  const executable = candidates.find(candidate => candidate && fs.existsSync(candidate))

  if (!executable) {
    throw new Error('Node.js is unavailable; Bitwarden CLI could not start.')
  }

  return executable
}

function sessionHelperPath(): string {
  return path.join(os.homedir(), '.claude', 'scripts', 'bw-session.ps1')
}

export async function listBitwardenLogins(rawUrl: string): Promise<BitwardenLogin[]> {
  if (process.platform !== 'win32') {
    throw new Error('Bitwarden autofill is currently available on Windows only.')
  }

  const url = validateCredentialUrl(rawUrl)

  const sessionResult = await runFile('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    sessionHelperPath()
  ])

  const sessionKey = sessionResult.stdout.trim()

  if (!sessionKey) {
    throw new Error('Bitwarden is locked and could not be unlocked automatically.')
  }

  const env = {
    ...process.env,
    BW_SESSION: sessionKey
  }

  const result = await runFile(nodeExecutablePath(), [bitwardenCliPath(), 'list', 'items', '--url', url], { env })

  return parseBitwardenLoginItems(result.stdout)
}

const FOCUS_USERNAME_SCRIPT = String.raw`(() => {
  const visible = element => {
    const style = getComputedStyle(element)
    const rect = element.getBoundingClientRect()
    return !element.disabled && !element.readOnly && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0
  }
  const password = [...document.querySelectorAll('input[type="password"]')].find(visible)
  if (!password) return false
  const scope = password.form || document
  const inputs = [...scope.querySelectorAll('input')]
  const candidates = inputs.filter(input => input !== password && visible(input) && /^(email|text|tel|url)?$/i.test(input.type || ''))
  const beforePassword = candidates.filter(input => inputs.indexOf(input) < inputs.indexOf(password))
  const ranked = (beforePassword.length ? beforePassword : candidates).sort((a, b) => {
    const score = input => /username|email/i.test([input.autocomplete, input.name, input.id, input.type].join(' ')) ? 1 : 0
    return score(b) - score(a)
  })
  const username = ranked[0]
  if (!username) return false
  username.focus()
  username.select()
  return true
})()`

const FOCUS_PASSWORD_SCRIPT = String.raw`(() => {
  const visible = element => {
    const style = getComputedStyle(element)
    const rect = element.getBoundingClientRect()
    return !element.disabled && !element.readOnly && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0
  }
  const password = [...document.querySelectorAll('input[type="password"]')].find(visible)
  if (!password) return false
  password.focus()
  password.select()
  return true
})()`

export async function fillBitwardenLogin(
  guest: WebContents,
  expectedUrl: string,
  login: BitwardenLogin
): Promise<{ passwordFilled: boolean; usernameFilled: boolean }> {
  if (validateCredentialUrl(guest.getURL()) !== expectedUrl) {
    throw new Error('The page changed before Bitwarden could fill it. Try again on the current page.')
  }

  let usernameFilled = false

  if (login.username) {
    usernameFilled = Boolean(await guest.executeJavaScript(FOCUS_USERNAME_SCRIPT, true))

    if (usernameFilled) {
      await guest.insertText(login.username)
    }
  }

  const passwordFilled = Boolean(await guest.executeJavaScript(FOCUS_PASSWORD_SCRIPT, true))

  if (!passwordFilled) {
    throw new Error('No visible password field was found on this page.')
  }

  await guest.insertText(login.password)

  return { passwordFilled, usernameFilled }
}
