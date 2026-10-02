import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { resolveProfileColor } from '@/lib/profile-color'
import { cn } from '@/lib/utils'
import { $activeConnectionId, $pendingConnectionId } from '@/store/connections'
import { $profileColors, $profiles, $profilesConnectionId, normalizeProfileKey, profileLabel } from '@/store/profile'

type AvatarAsset = { data?: string; found?: boolean }

// A sidebar can mount dozens of rows for one owner at once. Share the in-flight
// request per gateway/profile so matching the rail avatar costs one RPC, not one
// RPC per visible session.
const avatarRequests = new WeakMap<object, Map<string, Promise<null | string>>>()

/** Owning-profile chip: the shared {@link ProfileGlyph}, resolved against the
 *  live profile colors and labelled with the full name. Identity, not status —
 *  session state dots keep their own semantics (#66003). */
export function ProfileTag({
  className,
  connectionId,
  profile
}: {
  className?: string
  connectionId?: null | string
  profile: null | string | undefined
}) {
  const { t } = useI18n()
  const { gateway, requestGateway } = useGatewayRequest()
  const colors = useStore($profileColors)
  const profiles = useStore($profiles)
  const profilesConnectionId = useStore($profilesConnectionId)
  const activeConnectionId = useStore($activeConnectionId)
  const pendingConnectionId = useStore($pendingConnectionId)
  const key = normalizeProfileKey(profile)
  const ownerConnectionId = String(connectionId || activeConnectionId || '').trim() || null

  const cacheMatchesOwner =
    !pendingConnectionId &&
    (ownerConnectionId ? profilesConnectionId === ownerConnectionId : profilesConnectionId === null)

  const info = cacheMatchesOwner ? profiles.find(item => normalizeProfileKey(item.name) === key) : undefined
  const name = info ? profileLabel(info) : key
  const label = t.sidebar.row.ownedByProfile(name)
  const genericDefault = key === 'default' && name === 'default'
  const ownerDefault = key === 'default' && !genericDefault
  const colorKey = ownerDefault ? name : key
  const [avatar, setAvatar] = useState<{ data: string; gateway: object; key: string } | null>(null)
  const avatarSrc = avatar?.gateway === gateway && avatar.key === key ? avatar.data : null

  useEffect(() => {
    let live = true

    if (!gateway || !ownerDefault) {
      return () => {
        live = false
      }
    }

    let requests = avatarRequests.get(gateway)

    if (!requests) {
      requests = new Map()
      avatarRequests.set(gateway, requests)
    }

    let request = requests.get(key)

    if (!request) {
      request = requestGateway<AvatarAsset>('profiles.get_asset', { asset: 'avatar', name: key })
        .then(asset => (asset.found && asset.data ? asset.data : null))
        .catch(() => null)
      requests.set(key, request)
    }

    void request.then(data => {
      if (live && data) {
        setAvatar({ data, gateway, key })
      }
    })

    return () => {
      live = false
    }
  }, [gateway, key, ownerDefault, requestGateway])

  return (
    <Tip label={label}>
      {avatarSrc ? (
        <img
          alt={label}
          className={cn('size-4 shrink-0 rounded-full object-cover', className)}
          onError={() => setAvatar(null)}
          src={avatarSrc}
        />
      ) : (
        <ProfileGlyph
          aria-label={label}
          className={cn(
            ownerDefault && 'rounded-full ring-1 ring-current ring-offset-1 ring-offset-background',
            className
          )}
          color={resolveProfileColor(colorKey, colors)}
          isDefault={genericDefault}
          name={name}
          role="img"
          softPercent={ownerDefault ? 24 : undefined}
        />
      )}
    </Tip>
  )
}
