'use strict'
function tokenFromHost (host, suffix) {
  if (typeof host !== 'string' || typeof suffix !== 'string') return null
  const name = host.toLowerCase().replace(/\.$/, '')
  const ending = '.' + suffix.toLowerCase().replace(/\.$/, '')
  if (!name.endsWith(ending)) return null
  const token = name.slice(0, -ending.length)
  return /^[a-f0-9]{32}$/.test(token) ? token : null
}
function verifiedProfile (client) {
  // profile is set by minecraft-protocol only after sessionserver.hasJoined succeeds.
  const profile = client.profile
  if (!profile || typeof profile.id !== 'string' || !/^[a-f0-9]{32}$/i.test(profile.id)) return null
  if (!/^[A-Za-z0-9_]{3,16}$/.test(profile.name)) return null
  if (client.uuid.replace(/-/g, '').toLowerCase() !== profile.id.toLowerCase()) return null
  return { uuid: profile.id.toLowerCase(), name: profile.name }
}
module.exports = { tokenFromHost, verifiedProfile }
